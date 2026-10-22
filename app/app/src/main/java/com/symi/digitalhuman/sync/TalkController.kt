package com.symi.digitalhuman.sync

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.util.Log
import com.symi.digitalhuman.audio.SherpaVad
import com.symi.digitalhuman.audio.WavWriter
import com.symi.digitalhuman.audio.toFloatPcm
import com.symi.digitalhuman.data.SettingsStore
import com.symi.digitalhuman.data.TalkMode
import com.symi.digitalhuman.service.CaptureService
import com.symi.digitalhuman.service.CaptureState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File

/**
 * 实时对话编排（Hermes 对接，2026-09-10）。
 *
 * 两条入口，对应设置里的 [TalkMode]：
 *
 * - **MANUAL**：[manualTalk] —— 用户点状态页的「对话」按钮，录 [AppSettings.talkRecordSec]
 *   秒，上传，播放管家回复。触发时机完全由用户掌握。
 * - **AUTO**：[onSegmentSaved] —— 采集管线每落盘一段就送给服务端；服务端转写后
 *   判断有没有唤醒词，有才调管家并回话，手机收到回复就播放。
 *
 * ## 并发与麦克风互斥
 *
 * 手动录音必须**先暂停采集**再开新的 AudioRecord。MIUI 上两个 AudioRecord 同时
 * 抢麦克风时，后开的那个往往拿到的是静音流（不报错，录出来一段无声 wav），
 * 表现为「对着手机说了半天，管家一点反应都没有」。暂停-录音-恢复三步走，
 * 用 600ms 等 service 处理完 ACTION_PAUSE（Intent 是异步投递的）。
 *
 * ## 为什么不阻塞调用方
 *
 * AUTO 模式的调用点在采集的录音循环里（CaptureService.saveSegment）。
 * 那里一旦被网络请求阻塞，VAD 缓冲区就会堆积、丢音。所以 [onSegmentSaved]
 * 是非挂起函数，内部 launch 到自己的 IO 作用域后立即返回。
 */
object TalkController {
    /** 独立的 IO 作用域：不绑任何生命周期，网络失败自生自灭，绝不拖累采集。 */
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** 同一时刻只处理一轮对话。并发多轮会让扬声器里的回复互相叠加。 */
    private val gate = Mutex()

    /** 最近一次对话的状态，供状态页展示（内存态，重启即清）。 */
    @Volatile
    var lastMessage: String = ""
        private set

    /**
     * 播报打断请求（2026-09-10）。置位后 [play] 的等待循环下一拍就退出，
     * 停掉扬声器、缩短静默期，本轮立即结束并释放 [gate]。
     *
     * ## 为什么用标志位而不是直接 `MediaPlayer.stop()`
     *
     * `MediaPlayer` 不是线程安全的。从别的线程调 `stop()` 去打断一个正在
     * `isPlaying` 轮询里的播放器，属于跨线程操作同一对象——大多数情况侥幸能跑，
     * 偶发会抛 `IllegalStateException` 甚至卡住解码线程。更稳的做法是只设标志，
     * 由持有播放器的那条协程自己收尾。代价是最多 50ms 延迟（轮询间隔），
     * 人耳无感。
     */
    @Volatile
    private var playInterrupted = false

    /**
     * 打断当前播报。幂等，未在播报时是空操作。
     *
     * 两个调用点：状态页的「停止播报」按钮（用户主动），
     * 以及新一轮对话发起时（抢占——旧回复还在念，用户已经说了新的话）。
     */
    fun stopPlayback(reason: String) {
        if (!CaptureState.talkPlaying) return
        playInterrupted = true
        Log.i(TAG, "打断播报：$reason")
    }

    sealed class Outcome {
        data class Ok(val asr: String, val reply: String, val via: String, val ms: Long) : Outcome()
        data class Skipped(val asr: String, val reason: String) : Outcome()
        data class Failed(val msg: String) : Outcome()
    }

    /**
     * 手动模式：录音 → 上传 → 播放回复。挂起直到整轮结束，供 UI 显示结果。
     *
     * 调用前会暂停采采、结束后恢复；若调用时采集本来没在跑，则不做无谓的恢复。
     */
    suspend fun manualTalk(ctx: Context): Outcome {
        // 抢占：上一轮回复还在念，用户已经按了「对话」——先把旧播报停掉。
        // 不先停的话 gate 要等它把整段念完（最长十几秒）才轮到本次，
        // 用户感受就是「按了没反应」。必须在取锁之前调，取了锁再调就晚了。
        stopPlayback("手动对话抢占")
        return gate.withLock {
            val s = SettingsStore(ctx).current()
            if (s.talkMode == TalkMode.OFF) {
                return@withLock Outcome.Failed("实时对话已关闭（设置 → 实时对话）")
            }

            val dir = File(ctx.cacheDir, "talk").apply { mkdirs() }
            val wav = File(dir, "manual_${System.currentTimeMillis()}.wav")

            val wasRunning = CaptureState.captureRunning
            if (wasRunning) {
                CaptureService.pause(ctx)
                delay(600)
            }
            // talkRecordSec <= 0 表示「自适应」：录到用户说完为止，而不是固定秒数。
            // 默认值就是自适应——说「大锅，开灯」只用 1.2 秒，固定 6 秒会有 4.8 秒
            // 纯静音：既让用户干等，又把语音占比稀释掉，ASR 更容易出空结果。
            val recOk = if (s.talkRecordSec <= 0) {
                recordVadWav(ctx, TALK_VAD_MAX_SEC, wav)
            } else {
                recordFixedWav(s.talkRecordSec, wav)
            }
            if (wasRunning) {
                CaptureService.resume(ctx, "TALK", "对话结束")
            }
            if (!recOk) {
                wav.delete()
                return@withLock Outcome.Failed("录音失败：麦克风被占用或没有权限")
            }

            // 手动模式没有本地 KWS（是用户主动点的「对话」），直接标 hasAlias=true：
            // 「主动发起」本身就是最明确的对话意图。
            val outcome = send(ctx, s, wav, hasAlias = true)
            wav.delete()
            lastMessage = describe(outcome)
            outcome
        }
    }

    /**
     * 测试播报（2026-09-15，设置页「测试播报」按钮）。
     *
     * 把服务端合成好的一段音频直接从扬声器放出来。刻意复用真实的 [play]：
     * 挂起拾音、播后静默期、打断响应全套保护都走同一条路径——否则「测试通过」
     * 并不代表真实回话可用，而用户点这个按钮的全部意义就在于确认那件事。
     *
     * 不影响 [gate]：测试播报不该把正在进行的真实回话挡住（也不该被它挡住），
     * 最坏情况是两段声音叠在一起，用户再点一次即可。
     */
    fun speakTest(ctx: Context, wav: ByteArray) {
        val app = ctx.applicationContext
        scope.launch { play(app, wav) }
    }

    /**
     * 自动模式：把刚落盘的一段音频送给服务端。
     *
     * 非挂起——调用点在录音循环里，绝不能让它等网络。
     * 服务端对不含唤醒词的段会立刻返回 skipped，不会白跑一次大模型。
     *
     * @param ephemeral 该文件是否是一次性产物。`true` 表示来自
     *   `CaptureService.handleTalkSegment` 的对话专用段（落在 `cacheDir/talk/`），
     *   与日报段不同，它没有任何后续消费者，无论这轮成功失败都必须在 finally 里删掉，
     *   否则缓存目录会随使用时长无限堆积。日报段必须保留（上传管线还要用），传 false。
     */
    fun onSegmentSaved(ctx: Context, file: File, ephemeral: Boolean = false,
                       hasAlias: Boolean = false) {
        val app = ctx.applicationContext
        scope.launch {
            try {
                val s = SettingsStore(app).current()
                if (s.talkMode != TalkMode.AUTO) return@launch
                if (!file.exists()) return@launch
                // gate 已被占用 = 上一轮还在跑（录音 / 上传 / 播报）。
                // **直接丢弃本段，不排队**（2026-09-10 改）。
                //
                // 旧实现用 `gate.withLock` 排队，问题在于用户**完全感知不到排队**：
                // 他以为系统空闲，其实锁后面压着一段几秒前切出来的音频。
                // 上一轮播报刚结束，那一轮立刻开播 —— 实测 18:45:02 切出的第二段
                // 一直等到 18:45:14 第一轮念完才开始，用户正在开口，
                // 听到的就是「我话还没说完就被它打断」。
                //
                // 丢弃的代价是「被打断时那一句没被答」；排队的代价是
                // 「之后每一句都会被插一句错位的回复」。后者的破坏大得多。
                //
                // 顺带：既然不上传，也就不该再调 stopPlayback 去掐当前播报——
                // 「打断」只对**将要应答**的新指令有意义（manualTalk 仍保留该行为）。
                if (!gate.tryLock()) {
                    Log.i(TAG, "上一轮对话尚未结束，丢弃本段（不排队）")
                    return@launch
                }
                try {
                    val outcome = send(app, s, file, hasAlias = hasAlias)
                    lastMessage = describe(outcome)
                    if (outcome is Outcome.Ok) {
                        Log.i(TAG, "自动对话已应答: ${outcome.reply}")
                    }
                } finally {
                    gate.unlock()
                }
            } catch (e: Exception) {
                // 这里兜住所有异常：本协程挂在无生命周期的 SupervisorJob 上，
                // 漏出去就是一条无人处理的崩溃日志。
                Log.w(TAG, "自动对话失败", e)
            } finally {
                if (ephemeral) runCatching { file.delete() }
            }
        }
    }

    // ------------------------------------------------------------------
    // 内部实现
    // ------------------------------------------------------------------

    /**
     * 本次对话的会话 ID。
     *
     * 语义：**同一次会话内的所有对话共用一个 ID，换会话才换 ID**——服务端与
     * Hermes 都按它维护多轮上下文。此前这里写死成 `talkStart(0L)`，等于全世界
     * 的对话都挤在 session `"0"` 里：跨设备、跨天全部串在一起，
     * 「那第二个呢」可能接的是别人昨天的上下文。
     *
     * 取值优先级：
     *  ① 采集会话 id（[CaptureState.sessionId]）——用户点「结束会话」就是天然的
     *     分界线，与他的心智模型一致；
     *  ② 采集没在跑时（手动模式常见）退化为时间窗分桶：同一桶内的连续提问仍算
     *     一次会话，不至于问一句就断上下文。
     *
     * 前缀设备名：采集会话 id 只是 Room 的自增主键，两台手机的 1 号和 1 号会撞。
     */
    private fun conversationSessionId(s: com.symi.digitalhuman.data.AppSettings): String {
        val dev = s.ownerName.trim().ifBlank { android.os.Build.MODEL }
        val sid = CaptureState.sessionId
        return if (sid > 0L) "$dev-$sid"
        else "$dev-t${System.currentTimeMillis() / TALK_SESSION_BUCKET_MS}"
    }

    private suspend fun send(ctx: Context, s: com.symi.digitalhuman.data.AppSettings,
                      file: File, hasAlias: Boolean = true): Outcome = withContext(Dispatchers.IO) {
        val c = MacClient(s)
        // 四种失败分开报：403（空间 ID）和连不上在用户眼里一样，处理方式却相反。
        val turnId = when (val start = c.talkStart(conversationSessionId(s))) {
            is MacClient.StartResult.Ok -> start.turnId
            MacClient.StartResult.TenantRejected -> return@withContext Outcome.Failed(
                "空间 ID「${s.tenantId.trim()}」在 Mac 上不存在。" +
                    "设置 → 空间 ID 留空即用默认空间"
            )
            MacClient.StartResult.AuthRejected -> return@withContext Outcome.Failed(
                "配对 Token 不对，请在「设置 → Mac 服务连接」重新复制"
            )
            is MacClient.StartResult.Failed -> return@withContext Outcome.Failed(
                "连不上 Mac 服务（${start.reason}）"
            )
        }

        // scope 决定服务端还要不要按唤醒词先筛一遍（见 data/TalkScope）。
        // 手动对话不受影响：那条路径 hasAlias=true，服务端一定命中，
        // 与 scope 取什么值无关 —— 用户点「对话」本身就是明确意图。
        val res = c.talkAudio(turnId, file, hasAlias = hasAlias, scope = s.talkScope)
            ?: pollReply(c, turnId)
        if (res == null) {
            c.talkEnd(turnId, 0L)
            return@withContext Outcome.Failed("对话超时：服务端 6 秒内没给出结果")
        }
        // 服务端跳过时 hermes.status 固定是 skipped；补拉路径拿到的可能是
        // 内存态里的原始取值（no_alias / empty / loop_guard / not_owner / dup），
        // 两种都要认，否则会把「没听到唤醒词」误报成「管家没给回复」。
        //
        // `dup` 是 2026-09-10 新增：近重复抑制或 Hermes 回声去重命中。它同样是
        // **防护生效**而非故障——原先 Hermes 回 voice_echo（空 reply）时走的
        // 是下面的 `replyText.isBlank()` 分支，客户端弹「管家没有给出回复」，
        // 用户看到「坏了」，实际是我们替他挡掉了一次重复播报。
        // 服务端明确拒绝（400/413）：音频超时长或超体积。**必须说出来**——
        // 旧实现把非 2xx 一律吞成 null，用户干等 6 秒后只看到「对话超时」，
        // 既不知道原因，那一段内容也被丢弃（日志实测这样丢过 24 轮）。
        if (res.status == "rejected") {
            c.talkEnd(turnId, 0L)
            return@withContext Outcome.Failed(
                when (res.reason) {
                    "audio_too_long" -> "这段话太长，服务端没收下（单句上限 20 秒），请分两句说"
                    else -> "服务端拒绝了这段音频（${res.reason}）"
                }
            )
        }
        // 「判为仅记录」是分流生效，不是故障（2026-09-11）：Hermes 按内容判断
        // 这句不是在跟它说话，于是只落库、不回复。归到 Skipped 一档。
        if (res.status == "skipped" || res.status == "no_alias"
            || res.status == "empty" || res.status == "loop_guard"
            || res.status == "not_owner" || res.status == "dup"
            || res.status == "noted") {
            c.talkEnd(turnId, 0L)
            // 传状态码而不是拼好的人话：文案统一由 describe() 出口，
            // 在这里拼会让「状态 → 文案」的映射散成两处，加一档就得改两个地方。
            return@withContext Outcome.Skipped(res.asrText, res.status)
        }
        if (res.replyText.isBlank()) {
            c.talkEnd(turnId, 0L)
            return@withContext Outcome.Failed("管家没有给出回复")
        }

        var playedMs = 0L
        if (res.audioUrl != null) {
            val bytes = c.downloadTalkAudio(res.audioUrl)
            if (bytes != null) playedMs = play(ctx, bytes)
        }
        // 播完才回调 end：服务端据此统计播报成功率，并删掉对话音频
        c.talkEnd(turnId, playedMs)
        Outcome.Ok(res.asrText, res.replyText, res.via, res.totalMs)
    }

    /**
     * 补拉一轮结果：`/api/talk/audio` 没回来时，回头问服务端要。
     *
     * 该端点是**同步**跑完 ASR → Hermes → TTS 才返回的，Hermes 侧最坏
     * `DH_HERMES_TIMEOUT(30s) × DH_HERMES_RETRY(3)` 会超过客户端读超时。
     * 超时只说明「这一次请求没等到」，不代表服务端没算完——结果早已写进
     * 服务端内存态，再问一次就能拿到，没必要让用户白说一遍。
     *
     * 节奏取 500ms × 12 ≈ 6s（规格 §5.3 给的是 200ms × 30 = 6s，同一时间窗但
     * 请求数少一个量级）。判终态用 [MacClient.TalkResult.terminal] 白名单——
     * 服务端未处理完时回的是 `open` 而不是 `pending`，只认 pending 会立刻误判。
     */
    private suspend fun pollReply(c: MacClient, turnId: String): MacClient.TalkResult? {
        repeat(12) {
            delay(500)
            val r = c.talkReply(turnId)
            if (r != null && r.terminal) return r
        }
        return null
    }

    private fun describe(o: Outcome): String = when (o) {
        is Outcome.Ok -> "管家：${o.reply}"
        is Outcome.Skipped -> when (o.reason) {
            // 分流生效：这句不是在跟管家说话，已记录但不打扰（2026-09-11）
            "noted" -> "已记录（这句不是在跟管家说话）"
            "no_alias" -> "未听到唤醒词"
            // 熔断不是故障，是防护生效。必须给出可执行的下一步，否则用户
            // 只会看到「不响应」并以为程序坏了。
            "loop_guard" -> "检测到回声自激，已暂停应答；请戴耳机或改用「手动」模式"
            "not_owner" -> "这个角色没被管家认出来"
            // 同样是防护生效。说清「为什么没答」，否则用户会以为漏听了。
            "dup" -> "和前一句重复，已跳过"
            else -> "没有有效语音"
        }
        is Outcome.Failed -> o.msg
    }

    /**
     * 开一个 16kHz 单声道 [AudioRecord]（已初始化、**未** start）。失败返回 null。
     *
     * AudioSource 用 `MIC` 而不是 `VOICE_RECOGNITION`：后者在本机 MIUI 上曾被实测
     * 返回纯静音（1e-13 量级，原始音源被系统保留），采集服务为此早就换成了 MIC。
     * 对话录音这里一直没跟上——两个 AudioRecord 走不同音源，一旦系统策略变化就会
     * 出现「手动对话录出一段无声 wav」这种极难定位的故障。统一成 MIC。
     */
    private fun openMic(): AudioRecord? {
        val minBuf = AudioRecord.getMinBufferSize(
            SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.w(TAG, "AudioRecord.getMinBufferSize 返回 $minBuf")
            return null
        }
        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.MIC, SR,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, minBuf * 4
            )
        } catch (e: Exception) {
            Log.w(TAG, "AudioRecord 创建失败", e)
            return null
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "AudioRecord 未初始化")
            rec.release()
            return null
        }
        return rec
    }

    /** 录 [seconds] 秒 16kHz 单声道 PCM 到 WAV。失败返回 false（不抛异常）。 */
    private suspend fun recordFixedWav(seconds: Int, out: File): Boolean =
        withContext(Dispatchers.IO) {
            val rec = openMic() ?: return@withContext false
            val w = WavWriter(out, SR)
            try {
                w.start()
                rec.startRecording()
                val buf = ShortArray(1024)
                val target = SR * seconds
                var got = 0
                while (got < target) {
                    // 用非阻塞的 read：超时返回 0 就继续转，避免 read 卡死整个协程
                    val n = rec.read(buf, 0, minOf(buf.size, target - got))
                    if (n < 0) {
                        Log.w(TAG, "AudioRecord.read 返回 $n")
                        break
                    }
                    if (n > 0) {
                        w.write(buf, 0, n)
                        got += n
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "录音过程异常", e)
            } finally {
                runCatching { rec.stop() }
                runCatching { rec.release() }
                runCatching { w.close() }
            }
            out.exists() && out.length() > 44
        }

    /**
     * 录到「用户说完」为止，而不是固定秒数。返回是否录到了有效内容。
     *
     * 断句用对话专用 VAD（[SherpaVad.forTalk]，静音 0.45s 出段）。**必须在拿到
     * 第一段之前一直录**——「大锅」两个字不到 0.4s，任何固定前置等待都可能把
     * 唤醒词本身切掉。两道兜底：
     *  ① 开口前 [NO_SPEECH_GIVEUP_MS] 内始终没出段 → 判定用户没开口，放弃上传
     *     （比「录满 6 秒再传一段静音」省一次往返，也让用户更早知道没录上）；
     *  ② 总时长到 [maxSeconds] → 硬停，防止有人对着手机长篇大论把请求撑爆。
     *
     * 音频**整段连续写入**（不是只写 VAD 切出的语音段）：服务端要的是自然连续的
     * 音频，中间短停顿由它自己的 VAD/ASR 处理，客户端不该替它裁剪。
     */
    private suspend fun recordVadWav(ctx: Context, maxSeconds: Int, out: File): Boolean =
        withContext(Dispatchers.IO) {
            val rec = openMic() ?: return@withContext false
            val vad = try {
                SherpaVad.forTalk(ctx.assets, maxSpeechSeconds = maxSeconds.toFloat())
            } catch (e: Exception) {
                // 拿不到 VAD 就退回定长录音：宁可多等几秒，也不能直接不让用户说话
                Log.w(TAG, "对话 VAD 创建失败，退回定长 ${TALK_VAD_FALLBACK_SEC}s", e)
                rec.release()
                return@withContext recordFixedWav(TALK_VAD_FALLBACK_SEC, out)
            }

            val w = WavWriter(out, SR)
            val started = System.currentTimeMillis()
            var lastVoiceAt = 0L
            var spoke = false
            try {
                w.start()
                rec.startRecording()
                val buf = ShortArray(1024)
                while (true) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n < 0) {
                        Log.w(TAG, "AudioRecord.read 返回 $n")
                        break
                    }
                    if (n == 0) { delay(5); continue }
                    w.write(buf, 0, n)
                    vad.feed(buf.copyOf(n).toFloatPcm())
                    var hit = false
                    while (vad.hasSegment) {
                        vad.nextSegment()?.let { hit = true }
                        vad.pop()
                    }
                    val now = System.currentTimeMillis()
                    if (hit) {
                        spoke = true
                        lastVoiceAt = now
                    }
                    if (!spoke && now - started >= NO_SPEECH_GIVEUP_MS) {
                        Log.i(TAG, "对话录音：${NO_SPEECH_GIVEUP_MS}ms 内无语音，放弃")
                        break
                    }
                    if (spoke && now - lastVoiceAt >= TALK_TAIL_SILENCE_MS) break
                    if (now - started >= maxSeconds * 1000L) break
                }
            } catch (e: Exception) {
                Log.w(TAG, "对话录音异常", e)
            } finally {
                runCatching { rec.stop() }
                runCatching { rec.release() }
                runCatching { vad.close() }
                runCatching { w.close() }
            }
            Log.i(TAG, "对话录音结束: spoke=$spoke 时长=${(System.currentTimeMillis() - started)}ms")
            spoke && out.exists() && out.length() > 44
        }

    /**
     * 播放一段 wav，返回实际播放时长（ms）。失败返回 0。
     *
     * ## 回声自激防护（2026-09-10 修）
     *
     * AUTO 模式下 TTS 是**外放**的，而麦克风一直在录。播报期间若不挂起拾音，
     * 播报内容会被录进下一段 → 送服务端 → ASR 认出唤醒词（管家回复常以「大锅」
     * 开头）→ 再调管家 → 再外放，形成无限自激环：实测措辞逐轮复制、音色相近
     * （「大锅，演示区灯组已找到，建议呼叫 light.turn_on 服务批量点亮。」反复出现）。
     *
     * 防护分两层：
     *  ① 播报全程置 [CaptureState.playingBack]，`recordLoop` 会排空缓冲而不喂 VAD
     *     （与「录音回放」复用同一道闸，见 `AppViewModel.togglePlayback`）；
     *  ② 播完再保持 [TALK_PLAY_TAIL_MS] 的静默期——扬声器停振 ≠ 声音消失，
     *     腔体余响、房间混响、麦克风 AGC 回落都需要时间，立刻恢复拾音会把
     *     尾巴切成一段「含唤醒词」的短音频，等于把自激环的入口留在原地。
     *
     * 音轨用 `USAGE_ASSISTANT`（回放用的是 `USAGE_MEDIA`）：走语音助手音量，
     * 与媒体音量解耦，用户调小音乐不会误伤管家回话。
     *
     * ⚠️ 静默期的 `delay` 必须包在 [NonCancellable] 里：本函数跑在
     * [scope] 的独立协程上，若此时进程被回收/作用域被取消，普通 `delay` 会立刻
     * 抛 CancellationException 跳过后续赋值，`playingBack` 就永久卡在 true，
     * 表现为「说完一句话后再也不录音了」。
     */
    private suspend fun play(ctx: Context, bytes: ByteArray): Long = withContext(Dispatchers.IO) {
        val f = File(ctx.cacheDir, "talk_reply.wav")
        return@withContext try {
            f.writeBytes(bytes)
            val mp = MediaPlayer()
            var ms = 0L
            var played = 0L
            try {
                mp.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                mp.setDataSource(f.absolutePath)
                mp.prepare()
                ms = mp.duration.toLong()
                played = ms
                // 消费掉上一轮可能残留的打断请求：不清的话「用户打断过 A 轮」
                // 会让紧随其后的 B 轮一开播就被掐掉。
                playInterrupted = false
                // ① 挂起拾音：先置位再 start，宁可早关 1 帧也不漏掉起播瞬间
                CaptureState.talkPlaying = true
                CaptureState.playingBack = true
                val startedAt = System.currentTimeMillis()
                mp.start()
                // 等播放结束再释放，否则会「刚出声就被掐断」
                val deadline = startedAt + ms + 5000
                var interrupted = false
                while (mp.isPlaying && System.currentTimeMillis() < deadline) {
                    if (playInterrupted) { interrupted = true; break }
                    delay(50)
                }
                if (interrupted) {
                    runCatching { mp.stop() }
                    // 报「实际播了多久」而不是音频总时长：打断后若照报总时长，
                    // 服务端统计的播报成功率会虚高。
                    played = (System.currentTimeMillis() - startedAt).coerceAtMost(ms)
                }
                // ② 静默期：清标志放到 delay 之后，让余响彻底衰减掉。
                //    被打断时扬声器已静音，只留一个短尾（不是 0——停振瞬间仍有
                //    腔体余响，立刻恢复拾音会把尾巴切进下一段）。
                withContext(NonCancellable) {
                    delay(if (interrupted) TALK_INTERRUPT_TAIL_MS else TALK_PLAY_TAIL_MS)
                }
            } finally {
                runCatching { mp.release() }
                CaptureState.playingBack = false
                CaptureState.talkPlaying = false
            }
            played
        } catch (e: Exception) {
            Log.w(TAG, "播放失败", e)
            CaptureState.playingBack = false
            CaptureState.talkPlaying = false
            0L
        } finally {
            runCatching { f.delete() }
        }
    }

    private const val TAG = "TalkController"
    private const val SR = 16000

    /**
     * 播报结束后的静默期（ms）。扬声器停振 ≠ 声音消失：腔体余响 + 房间混响 +
     * 麦克风 AGC 从「听到大音量」回落都需要时间，800ms 足以让残余能量衰减到
     * 环境底噪量级。设为 0 会在恢复拾音的瞬间把尾部切成一段短音频。
     */
    private const val TALK_PLAY_TAIL_MS = 800L

    /**
     * 被打断后的静默期（ms）。比自然播完短得多：打断时我们主动停了扬声器，
     * 没有「把最后一句念完」的余响，只剩腔体阻尼的尾巴。400ms 够衰减到环境底噪，
     * 又不至于让用户觉得「按了停止还在等」。
     */
    private const val TALK_INTERRUPT_TAIL_MS = 400L

    /**
     * 会话 ID 的时间分桶宽度（ms）。**只在采集没跑、拿不到采集会话 id 时**用：
     * 同一个 10 分钟窗口内的连续提问算一次会话，共享多轮上下文；隔久了自然
     * 开新会话，避免把「昨天问的那句话」当成上文。
     */
    private const val TALK_SESSION_BUCKET_MS = 10 * 60 * 1000L

    /** 自适应录音的总时长上限（秒）。超过就硬停，防止有人长篇大论撑爆请求。 */
    private const val TALK_VAD_MAX_SEC = 15

    /**
     * 开口超时（ms）：这段时间内 VAD 一段都没切出来，就判定用户没开口。
     * 5 秒比「录满固定秒数再上传」体验好——用户更快知道没录上，也省一次往返。
     */
    private const val NO_SPEECH_GIVEUP_MS = 5000L

    /**
     * 说完之后的确认静默（ms）。VAD 自己的 minSilence 是 0.45s，这里再等
     * 400ms 才收尾：中间停顿超过 0.85s 才算「说完」。纯粹用 VAD 的 0.45s
     * 会把「大锅，把客厅的灯…（停顿）…打开」从中间截断。
     */
    private const val TALK_TAIL_SILENCE_MS = 400L

    /** VAD 不可用时的定长录音兜底秒数（宁可多等，也不能不让用户说话）。 */
    private const val TALK_VAD_FALLBACK_SEC = 6
}
