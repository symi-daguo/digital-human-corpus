package com.symi.digitalhuman.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.symi.digitalhuman.App
import com.symi.digitalhuman.R
import com.symi.digitalhuman.audio.KeywordGate
import com.symi.digitalhuman.audio.SherpaVad
import com.symi.digitalhuman.audio.toFloatPcm
import com.symi.digitalhuman.audio.toShortPcm
import com.symi.digitalhuman.data.AppDatabase
import com.symi.digitalhuman.data.Segment
import com.symi.digitalhuman.data.SegmentStatus
import com.symi.digitalhuman.data.Session
import com.symi.digitalhuman.data.SettingsStore
import com.symi.digitalhuman.data.TalkMode
import com.symi.digitalhuman.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * microphone 前台服务：AudioRecord 16kHz PCM → Silero VAD 切段 → WAV 落盘（WAL）。
 * 只录音不转写；触发源消失时由 GuardService 发 [ACTION_STOP]。
 */
class CaptureService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var wakeLock: PowerManager.WakeLock? = null

    /**
     * 对话段队列：录音循环只负责投递，唤醒词判定 / 落盘 / 上传都在
     * [consumeTalkLoop] 里做。
     *
     * 容量 8：够吸收突发，满了就丢**新**段。对话段是「及时才有效」的时效数据，
     * 堆积几十秒前的旧指令毫无意义；而阻塞录音循环会丢真实的日记语料——两害相权，
     * 丢对话段。
     */
    private val talkQueue = Channel<ShortArray>(capacity = 8)

    private var sessionId: Long = -1L
    private var segIndex = 0
    private var savedSpeechMs = 0L

    /** 前台服务是否已在 onCreate 中成功挂起通知 */
    private var foregroundReady = false

    override fun onCreate() {
        super.onCreate()
        // Android 14+（targetSdk 36）：从后台启动 microphone 前台服务会被系统拒绝，
        // startForeground() 抛 SecurityException。若异常逃出 onCreate，进程直接崩溃，
        // 再叠加 onStartCommand 的 START_STICKY →「启动→崩溃→自动重启」死循环（实测已复现）。
        // 因此这里必须兜住，改为记录事件 + 发恢复通知 + 主动 stopSelf。
        foregroundReady = try {
            startAsForeground()
            true
        } catch (e: Exception) {
            onForegroundBlocked(e)
            false
        }
    }

    /** 前台化失败：绝不把异常抛给框架 */
    private fun onForegroundBlocked(e: Exception) {
        CaptureState.captureRunning = false
        // 去重（2026-09-27）：start() 改成「先试后降级」后，被拒时会**每个探测周期重试一次**，
        // 而每次失败都写一条事件 → 一天能写几千行，正是要消灭的日志/库刷屏。
        // 只在本次「受阻期」的第一次记录 + 挂通知；恢复成功时 pendingRecoverNotify
        // 会被清掉（见 startCapture 路径），下次再受阻仍会如实记一条。
        if (CaptureState.pendingRecoverNotify) { stopSelf(); return }
        CaptureState.pendingRecoverNotify = true
        runCatching { RecoveryNotifier.notify(this, e.message ?: "FGS blocked") }
        val msg = e.message?.take(140) ?: e.javaClass.simpleName
        scope.launch {
            runCatching {
                AppDatabase.get(applicationContext).serviceEventDao().insert(
                    com.symi.digitalhuman.data.ServiceEvent(
                        type = "CAPTURE_BLOCKED",
                        detail = "后台启动麦克风前台服务被系统拒绝：$msg"
                    )
                )
            }
        }
        stopSelf()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 前台化失败时不能再返回 START_STICKY，否则系统会不断重拉本服务
        if (!foregroundReady) {
            stopSelf()
            return START_NOT_STICKY
        }
        when (intent?.action) {
            ACTION_STOP -> { stopCapture("TRIGGER_GONE"); return START_NOT_STICKY }
            ACTION_PAUSE -> { pauseCapture(); return START_STICKY }
            ACTION_RESUME -> { resumeCapture(
                intent.getStringExtra(EXTRA_TRIGGER_TYPE) ?: "AUTO",
                intent.getStringExtra(EXTRA_TRIGGER_DETAIL) ?: "")
                return START_STICKY }
            else -> startCapture(intent?.getStringExtra(EXTRA_TRIGGER_TYPE) ?: "MANUAL",
                                 intent?.getStringExtra(EXTRA_TRIGGER_DETAIL) ?: "",
                                 intent?.getLongExtra(EXTRA_SESSION_ID, -1L) ?: -1L)
        }
        return START_STICKY
    }

    private fun startAsForeground() {
        val n: Notification = buildNotification("正在记录您附近的语音")
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else startForeground(NOTIF_ID, n)
    }

    /**
     * 构建前台通知。文案由 [captureStatusText] 动态生成。
     *
     * 用户 2026-09-11 反馈「讲了 20 分钟没有任何数据」，而通知栏当时只有一句
     * 静态的「正在记录您附近的语音」——在录音却切不出段时看不出任何异常。
     * 现在直接显示「已录 N 段 · 最近 HH:mm」，下拉通知栏即可自检；若连这条
     * 通知都不见了，说明进程已被系统强停（装机 / Force Stop），需要手动打开 App。
     */
    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, App.CHANNEL_CAPTURE)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("语料采集中")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setContentIntent(openAppIntent())
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()

    private fun openAppIntent(): PendingIntent =
        PendingIntent.getActivity(
            this, 12, Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

    /**
     * 采集活性文案：以「最近落盘」为准，而不是录音心跳。
     * 心跳只证明 AudioRecord 还在读数，切不出段时它照样新鲜，看不出问题。
     */
    private fun captureStatusText(): String {
        val last = CaptureState.lastSegmentAt
        if (last <= 0L) return "等待语音…（本次启动后尚无段落盘）"
        val hhmm = SimpleDateFormat("HH:mm", Locale.US).format(Date(last))
        val mins = (System.currentTimeMillis() - last) / 60_000L
        val gap = if (mins >= 10) "，已 $mins 分钟无新段" else ""
        return "已录 $segIndex 段 · 最近 $hhmm$gap"
    }

    /** 刷新前台通知（任何失败都不能影响录音链路） */
    private fun refreshNotification() {
        if (!foregroundReady) return
        runCatching {
            androidx.core.app.NotificationManagerCompat.from(this)
                .notify(NOTIF_ID, buildNotification(captureStatusText()))
        }
    }

    private fun startCapture(triggerType: String, triggerDetail: String, existingSessionId: Long) {
        if (CaptureState.captureRunning) {
            // 服务活着但被触发暂停（AUTO 双触发全离线）→ 不是重复启动，而是恢复采集
            if (CaptureState.triggerPaused) resumeCapture(triggerType, triggerDetail)
            return
        }
        scope.launch {
            val db = AppDatabase.get(applicationContext)
            val sid = if (existingSessionId > 0) existingSessionId
            else db.sessionDao().insert(
                Session(startedAt = System.currentTimeMillis(), triggerType = triggerType, triggerDetail = triggerDetail)
            )
            sessionId = sid
            CaptureState.sessionId = sessionId
            CaptureState.captureRunning = true
            CaptureState.triggerPaused = false
            CaptureState.pendingRecoverNotify = false
            // 采集真的跑起来了 → 撤掉「点击恢复」通知，别让两条矛盾通知并存
            runCatching { RecoveryNotifier.cancel(this@CaptureService) }
            CaptureState.lastCaptureAliveAt = System.currentTimeMillis()
            db.serviceEventDao().insert(
                com.symi.digitalhuman.data.ServiceEvent(type = "CAPTURE_STARTED", detail = "session=$sessionId")
            )
            acquireWakeLock()
            recordLoop()
        }
    }

    private suspend fun recordLoop() {
        val db = AppDatabase.get(applicationContext)
        val minBuf = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = try {
            // AudioSource.MIC：VOICE_RECOGNITION 在此 MIUI 返回纯静音（实测 1e-13 量级，
            // 原始音源被系统保留），只能用 MIC。
            AudioRecord(MediaRecorder.AudioSource.MIC, SR, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, SR * 2))
        } catch (e: Exception) {
            android.util.Log.e(TAG, "AudioRecord 构造失败: ${e.message}", e)
            db.serviceEventDao().insert(com.symi.digitalhuman.data.ServiceEvent(type = "CAPTURE_ERROR", detail = "AudioRecord: ${e.message}"))
            stopCapture("SERVICE_DIED"); return
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            android.util.Log.e(TAG, "AudioRecord 未初始化 state=${record.state} minBuf=$minBuf")
            record.release()
            stopCapture("SERVICE_DIED"); return
        }

        var vad = try { SherpaVad(assets) } catch (e: Exception) {
            android.util.Log.e(TAG, "VAD 初始化失败: ${e.message}", e)
            db.serviceEventDao().insert(com.symi.digitalhuman.data.ServiceEvent(type = "CAPTURE_ERROR", detail = "VAD init: ${e.message}"))
            record.release(); stopCapture("SERVICE_DIED"); return
        }
        // 下一次「段边界重建 VAD」的时刻（0 = 尚未排期）。见循环内的重建说明。
        var nextVadRecycleAt = System.currentTimeMillis() + VAD_RECYCLE_MS
        // 对话专用 VAD（2026-09-10）：参数与日报那套相反（见 SherpaVad.forTalk）。
        // 日报 VAD 的 2.2s 段长门槛 + 5s 碎段合并会把「大锅，开灯」（整句约 1.2s）直接丢掉，
        // 这就是「AUTO 模式无感对话不生效」的根因——不是唤醒词识别问题，是段压根没生成。
        //
        // **无条件创建**，不在启动时判断 talkMode：设置页改完 talkMode 要重启采集才生效
        // 是旧实现的坑（`onSegmentSaved` 以前每段实时读设置，改完立即生效）。改成
        // 「VAD 常开 + 消费者按段读设置」后，设置一保存就生效，无需重启。
        // 代价是非 AUTO 用户也跑一路 Silero VAD，实测 RTF≈0.01（约 1% CPU），可忽略。
        var talkVad = try {
            SherpaVad.forTalk(assets)
        } catch (e: Exception) {
            android.util.Log.w(TAG, "对话 VAD 初始化失败，实时对话本次会话不可用: ${e.message}")
            null
        }
        // 对话段消费者：唤醒词粗筛 + 落盘 + 上传，与录音循环彻底解耦。
        // 放在独立协程里是硬要求——本地 KWS 对 8 秒音频要 ~120ms，在录音循环里
        // 同步跑会直接丢音（VAD 缓冲堆积）。唤醒词模型在消费者里惰性加载。
        val talkJob: Job? = if (talkVad != null) scope.launch { consumeTalkLoop() } else null
        android.util.Log.i(TAG, "recordLoop 开始: session=$sessionId minBuf=$minBuf talkVad=${talkVad != null}")

        val dir = sessionDir()
        val chunk = ShortArray(512)
        var dbgChunks = 0
        // 降噪（2026-09-27）：只在状态变化或 48 秒心跳时打一行，见下方注释。
        var lastDbgState = ""
        // 喂给 VAD 的 FloatArray 强引用环（2026-09-27）。
        //
        // sherpa JNI 对传入数组的生命周期有假设——历史实测「复用同一个数组会让 VAD
        // 永远切不出段」，说明原生侧会**跨调用持有**这个数组的指针。既如此，数组一旦
        // 被 GC 回收或搬移，下一次 acceptWaveform 里的 memcpy 就会踩空：
        // 2026-09-27 在真机上抓到两条完全同源的 tombstone ——
        //   `Fatal signal 11 SIGSEGV in __memcpy_aarch64_simd ← libsherpa-onnx-jni
        //    ← Vad.acceptWaveform ← CaptureService`（线程 DefaultDispatch，
        //    进程已连续运行 37 / 39 小时）。exit-info 里 09-16 ~ 09-24 共 5 次
        //    APP CRASH(NATIVE) 都是它。
        // 这里保留最近若干个数组的强引用，让原生侧即使晚一步读也读到有效内存。
        // 每个 512 float ≈ 2KB，8 个 ≈ 16KB，可忽略。
        val fedPcmKeepAlive = ArrayDeque<FloatArray>()
        record.startRecording()
        try {
            while (CaptureState.captureRunning && kotlinx.coroutines.currentCoroutineContext().isActive) {
                CaptureState.lastCaptureAliveAt = System.currentTimeMillis()
                val n = record.read(chunk, 0, chunk.size)
                if (n <= 0) { delay(10); continue }
                // 暂停拾音 / 触发离线 / 回放中：只排空缓冲不落盘不喂 VAD，波形压平；
                // AudioRecord 保持打开，恢复后无缝续录（回放声也不会被录进去）
                if (CaptureState.capturePaused || CaptureState.triggerPaused || CaptureState.playingBack) {
                    CaptureState.level = 0f
                    continue
                }
                CaptureState.level = rmsLevel(chunk, n)
                // 诊断日志降噪（2026-09-27）：原实现每 150 chunk（≈4.8 秒）无条件打一条
                // Log.d，一天约 1.8 万行——真出问题时 logcat 早被它冲走，反而看不到关键行。
                // 现在：状态（有无段 / 三种暂停）变化时必打，另加每 ~48 秒一条心跳
                // （实测稳态 1 条/48s ≈ 1800 行/天，降噪 10 倍，logcat 缓冲可留 ~30 小时）。
                if (++dbgChunks % 150 == 0) {
                    val st = "${vad.hasSegment}|${CaptureState.capturePaused}" +
                        "|${CaptureState.triggerPaused}|${CaptureState.playingBack}"
                    if (st != lastDbgState || dbgChunks % 1500 == 0) {
                        lastDbgState = st
                        android.util.Log.d(TAG, "dbg: chunks=$dbgChunks level=${CaptureState.level} " +
                            "hasSeg=${vad.hasSegment} paused=${CaptureState.capturePaused}/${CaptureState.triggerPaused}/${CaptureState.playingBack}")
                    }
                    // 每 ~48s 刷新一次前台通知。没有新落盘时「已 N 分钟无新段」
                    // 这句话必须能自己长大，用户才会注意到采集卡住了。
                    if (dbgChunks % 1500 == 0) refreshNotification()
                }
                // 每次新数组：sherpa acceptWaveform 复用传入数组会导致 VAD 永远切不出段（实测回归）
                if (n > 0) {
                    val pcm = chunk.copyOf(n).toFloatPcm()
                    vad.feed(pcm)
                    // 对话 VAD 单独喂一份拷贝：两个实例共用同一数组的行为未实测过，
                    // 按 JNI 生命周期假设的保守做法各喂一份（512 float ≈ 2KB，可忽略）
                    val talkPcm = pcm.copyOf()
                    talkVad?.feed(talkPcm)
                    // 保住强引用（见 fedPcmKeepAlive 注释）：原生侧可能跨调用持有指针，
                    // 数组被 GC 回收/搬移会让 acceptWaveform 里的 memcpy 踩空 → 原生崩溃。
                    fedPcmKeepAlive.addLast(pcm)
                    if (talkVad != null) fedPcmKeepAlive.addLast(talkPcm)
                    while (fedPcmKeepAlive.size > VAD_KEEPALIVE_ARRAYS) fedPcmKeepAlive.removeFirst()
                }
                while (vad.hasSegment) {
                    val seg = vad.nextSegment() ?: break
                    handleSegment(db, dir, seg.samples.toShortPcm())
                    vad.pop()
                }
                while (talkVad != null && talkVad.hasSegment) {
                    val seg = talkVad.nextSegment() ?: break
                    handleTalkSegment(seg.samples.toShortPcm())
                    talkVad.pop()
                }
                // VAD 长跑重建（2026-09-27）：原生 VAD 连跑数十小时必崩——真机实测
                // 进程连续运行 37h / 39h 各抓到一次 SIGSEGV（见 fedPcmKeepAlive 注释）。
                // 这里在**段边界**（两个 VAD 都无待取段，不会丢音）重建实例：
                // Silero VAD 模型很小，重建是毫秒级 + 一次模型读盘，代价可忽略；
                // 换来的是原生状态定期归零，撞不上那个必崩点。
                // 先建后关：新实例建失败就保持原状，绝不让采集断在这一步。
                if (nextVadRecycleAt > 0 && System.currentTimeMillis() >= nextVadRecycleAt) {
                    val newVad = runCatching { SherpaVad(assets) }.getOrNull()
                    val newTalk = if (talkVad == null) null
                    else runCatching { SherpaVad.forTalk(assets) }.getOrNull()
                    // 两个都建成功才替换。只建成一个就替换的话，实时对话会**静默失能**
                    // 直到下次重启采集；宁可这一轮不重建、5 分钟后重试。
                    if (newVad != null && (talkVad == null || newTalk != null)) {
                        runCatching { vad.close() }
                        runCatching { talkVad?.close() }
                        vad = newVad
                        talkVad = newTalk
                        android.util.Log.i(
                            TAG, "VAD 已连续运行 ${VAD_RECYCLE_MS / 60_000} 分钟，" +
                                "按段边界重建（长跑原生崩溃规避）"
                        )
                        fedPcmKeepAlive.clear()   // 旧实例已销毁，其数组引用一起放掉
                        nextVadRecycleAt = 0L
                    } else {
                        android.util.Log.w(TAG, "VAD 重建失败，5 分钟后重试（保持现有实例继续采）")
                        nextVadRecycleAt = System.currentTimeMillis() + VAD_RECYCLE_RETRY_MS
                    }
                }
                delay(10)
            }
            vad.flush()
            talkVad?.flush()
            CaptureState.level = 0f
            while (vad.hasSegment) {
                val seg = vad.nextSegment() ?: break
                handleSegment(db, dir, seg.samples.toShortPcm())
                vad.pop()
            }
            while (talkVad != null && talkVad.hasSegment) {
                val seg = talkVad.nextSegment() ?: break
                handleTalkSegment(seg.samples.toShortPcm())
                talkVad.pop()
            }
            flushPendingSeg(db, dir)
        } finally {
            try { record.stop() } catch (_: Exception) {}
            record.release()
            vad.close()
            talkVad?.close()
            // 先停消费者再退：不 cancel 的话协程会一直挂在 channel 上，
            // 下次 startCapture 又起一个，模型被加载 N 份。
            talkJob?.cancel()
        }
    }

    /**
     * 对话段消费者。串行处理 [talkQueue] 里的每一段：
     * 本地唤醒词粗筛 → 落盘 → 送服务端。
     *
     * 唤醒词模型（4.6MB）在这里**惰性加载**：只在真正有对话段、且当前确实是
     * AUTO 模式时才加载。非对话用户（关闭 / 手动）永远不需要为它付内存与加载时间。
     * 加载失败不致命：降级成「不做本地筛选、所有段都上传」，功能仍可用，只是费流量
     * ——比整个 AUTO 模式失灵好得多。
     */
    private suspend fun consumeTalkLoop() {
        var gate: KeywordGate? = null
        var gateTried = false
        // 本地筛选「没生效」的原因只在第一次说明一次。
        //
        // 为什么必须留痕：`consumeTalkSegment` 只在 gate != null 时才打日志，
        // 于是「开关被关掉」与「模型加载失败」两种降级**在日志里完全静默**，
        // 排查时只能靠「一条唤醒词日志都没有」去反推——2026-09-10 排查
        // 「问了没反应 / 什么都能触发」时就卡在这里。明确打一行，下次一眼可见。
        var filterOffLogged = false
        try {
            for (samples in talkQueue) {
                try {
                    // 每段都重新读一次设置：设置页保存后 recordLoop 不会重启，
                    // 只有实时读取才能让「切成自动 / 切回手动」立即生效。
                    val st = SettingsStore(this).current()
                    if (st.talkMode != TalkMode.AUTO) continue
                    if (!st.talkKwsFilter) {
                        // 本地过滤被关掉：一律上传，唤醒词交给服务端判。
                        // 这是漏检的降级出口（详见 AppSettings.talkKwsFilter）。
                        if (!filterOffLogged) {
                            filterOffLogged = true
                            android.util.Log.i(TAG, "本地唤醒词过滤已关闭（设置项），对话段一律上传")
                        }
                        consumeTalkSegment(null, samples)
                        continue
                    }
                    if (!gateTried) {
                        gateTried = true
                        gate = try {
                            KeywordGate(assets)
                        } catch (e: Exception) {
                            android.util.Log.w(TAG, "唤醒词模型加载失败，本次会话对话段直接上传: ${e.message}")
                            null
                        }
                    }
                    if (gate == null && !filterOffLogged) {
                        filterOffLogged = true
                        android.util.Log.w(TAG, "本地唤醒词模型不可用，对话段一律上传（服务端仍会判唤醒词）")
                    }
                    consumeTalkSegment(gate, samples)
                } catch (e: Exception) {
                    // 单段失败不能拖垮消费者，否则后面所有对话段都堵死
                    android.util.Log.w(TAG, "对话段处理异常", e)
                }
            }
        } finally {
            gate?.close()
        }
    }

    /**
     * 处理一个对话段：本地判一次唤醒词，结果**只作为线索**随请求带上
     * （`X-Has-Alias`），不决定上传与否 —— 2026-09-11 起，要不要应答由智能体
     * 按**内容**判断，本地这一步只是帮它多一条「这是在叫我」的证据。
     *
     * 落盘的临时文件由 TalkController 负责删。
     */
    private suspend fun consumeTalkSegment(gate: KeywordGate?, samples: ShortArray) {
        val durationMs = samples.size * 1000L / SR
        if (durationMs < TALK_SEGMENT_MIN_MS) return
        if (avgRms(samples) < MIN_SEGMENT_RMS) return

        // 本地 KWS：从「硬门」降级为「提示」（2026-09-11 重新规划）。
        //
        // 旧实现在这里直接 `return` —— 本地没听到唤醒词的段**根本不落盘、不上传**，
        // 服务端连它存在过都不知道。叠加服务端自己还有一道同样的门，实测
        // talk_message 里 930 轮有 753 轮（81%）被两侧一起丢掉：用户说了话，
        // 全链路没有任何一处处理它。
        //
        // 现在的分工是「有语意就上传，要不要应答由 Hermes 按内容判断」，
        // 所以本地判定只作为一条线索随请求带上去（`X-Has-Alias`），
        // 不再决定上传与否。噪声与时长门槛（上面的 TALK_SEGMENT_MIN_MS / avgRms）
        // 保留——那是信号质量筛选，与语义无关。
        val kwsHit = gate?.detect(samples.toFloatPcm())
        if (gate != null) {
            if (kwsHit == null) {
                android.util.Log.d(TAG, "本地未命中唤醒词，仍上传交服务端判定 (dur=${durationMs}ms)")
            } else {
                android.util.Log.i(TAG, "本地命中唤醒词「$kwsHit」，上传 (dur=${durationMs}ms)")
            }
        }

        val dir = File(cacheDir, "talk").apply { mkdirs() }
        val file = File(dir, "auto_${System.currentTimeMillis()}.wav")
        try {
            val writer = com.symi.digitalhuman.audio.WavWriter(file, SR)
            writer.start()
            writer.write(samples)
            writer.close()
        } catch (e: Exception) {
            android.util.Log.w(TAG, "对话段落盘失败: ${e.message}")
            return
        }
        // ephemeral=true：临时产物，TalkController 无论成功失败都会删掉它
        com.symi.digitalhuman.sync.TalkController.onSegmentSaved(
            this, file, ephemeral = true, hasAlias = kwsHit != null)
    }

    /** 平滑后的电平（跨 chunk 保持，快攻慢放） */
    private var smoothLevel = 0f

    /**
     * dBFS 电平映射（0~1）——录音电平计的行业标法（微信/WhatsApp 同款思路）：
     *   db = 20·log10(rms / 32768)
     * 正常交谈距离语音 ≈ -30 ~ -18 dBFS，耳语 ≈ -40，环境噪声底 ≈ -55 ~ -45。
     * 线性映射 [-50, -3] dBFS → 0~1：正常讲话约 0.4~0.7 高度，只有贴近大喊才接近满格，
     * 声音的大小界限在柱子上可分辨（旧版 rms/3000 线性归一，正常讲话直接满格）。
     * 平滑采用快攻慢放：上升立即跟随（反应灵敏），回落按系数衰减（柱子不闪跳）。
     * 注：android.media.Visualizer 只支持播放输出，麦克风输入电平计的标准实现即本算法。
     */
    private fun rmsLevel(chunk: ShortArray, n: Int): Float {
        var sum = 0.0
        for (i in 0 until n) { val v = chunk[i].toDouble(); sum += v * v }
        val r = kotlin.math.sqrt(sum / kotlin.math.max(1, n))
        if (r < 1.0) {                       // 纯静音：只衰减不清零，柱子平滑落底
            smoothLevel *= LEVEL_RELEASE
            return smoothLevel
        }
        val db = 20.0 * kotlin.math.log10(r / 32768.0)
        val norm = ((db - LEVEL_DB_FLOOR) / (LEVEL_DB_CEIL - LEVEL_DB_FLOOR)).toFloat()
            .coerceIn(0f, 1f)
        smoothLevel = if (norm > smoothLevel) norm
                      else smoothLevel * LEVEL_RELEASE + norm * (1 - LEVEL_RELEASE)
        return smoothLevel
    }

    /** 段平均电平（线性 RMS，16bit 满幅 32768）：人声通常 >300，键盘/环境噪声 <150 */
    private fun avgRms(samples: ShortArray): Double {
        var sum = 0.0
        for (v in samples) { val x = v.toDouble(); sum += x * x }
        return kotlin.math.sqrt(sum / samples.size)
    }

    // ---------- 碎段合并缓冲 ----------

    /**
     * 远场语音的 Silero 概率贴着阈值抖动，连续说话会被切成大量 0.4~1.2s 碎段
     * （2026-09-04 实测 8 段只有 1 段过 2.2s 门槛，其余全是高能量人声碎段被丢弃
     * → 日报零更新）。合并策略：相邻段墙钟间隔 ≤5s 视为同一句话的抖动切片，
     * 拼接（补 320ms 静音防词硬切）后凑够 [MIN_SEGMENT_MS] 落盘；
     * 超时未凑够则交给 saveSegment 的门槛兜底（大概率是键盘/瞬态噪声，丢弃）。
     */
    private var pendingSeg: ShortArray? = null
    private var pendingEndAt = 0L

    private suspend fun handleSegment(db: AppDatabase, dir: File, samples: ShortArray) {
        val now = System.currentTimeMillis()
        val prev = pendingSeg
        if (prev != null && now - pendingEndAt <= MERGE_GAP_MS) {
            val gap = ShortArray((SR * 0.32f).toInt())
            pendingSeg = prev + gap + samples
        } else {
            flushPendingSeg(db, dir)
            pendingSeg = samples
        }
        pendingEndAt = now
        val cur = pendingSeg
        if (cur != null && cur.size * 1000L / SR >= MIN_SEGMENT_MS) {
            android.util.Log.d(TAG, "merged seg out: dur=${cur.size * 1000L / SR}ms rms=${avgRms(cur)}")
            saveSegment(db, dir, cur)
            pendingSeg = null
        }
    }

    /** 有新段落盘 → 立即安排一次增量上传（KEEP 去重），日报说完话约 1 分钟内更新 */
    private suspend fun scheduleUploadAfterCapture() {
        try {
            // 网络约束跟随设置（2026-09-21）：关闭「移动网络下也上传」才回到只走 WiFi。
            // 读失败按 true 处理——宁可多花点流量，也别让语料静默传不上去。
            val allowMetered = runCatching { SettingsStore(this).current().uploadOnMetered }
                .getOrDefault(true)
            com.symi.digitalhuman.worker.WorkScheduler.uploadAfterCapture(this, allowMetered)
        } catch (e: Exception) {
            android.util.Log.w(TAG, "schedule upload failed: ${e.message}")
        }
    }

    private suspend fun flushPendingSeg(db: AppDatabase, dir: File) {
        val p = pendingSeg ?: return
        pendingSeg = null
        saveSegment(db, dir, p)
    }

    /**
     * 段级过滤（用户实测反馈：键盘声、设备瞬响被录成大量 <1s 的无效段）：
     * ① 时长过滤——正常讲话不会低于 2.2s，更短的直接丢弃不落盘不入库
     * ② 能量过滤——键盘敲击/桌面碰撞的平均电平远低于人声，低于阈值丢弃
     * 两关都过才写 WAV + 入库。
     */
    private suspend fun saveSegment(db: AppDatabase, dir: File, samples: ShortArray) {
        if (samples.isEmpty()) return
        val durationMs = samples.size * 1000L / SR
        android.util.Log.d(TAG, "seg out: dur=${durationMs}ms rms=${avgRms(samples)}")
        if (durationMs < MIN_SEGMENT_MS) return
        if (avgRms(samples) < MIN_SEGMENT_RMS) return
        segIndex++
        val file = File(dir, "seg_%04d.wav".format(segIndex))
        val writer = com.symi.digitalhuman.audio.WavWriter(file, SR)
        writer.start()
        writer.write(samples)
        writer.close()
        savedSpeechMs += durationMs
        db.segmentDao().insert(
            Segment(
                sessionId = sessionId, filePath = file.absolutePath,
                // 段的**真实起点** = 落盘时刻 − 时长（2026-10-21 修）。
                // 此前用 System.currentTimeMillis()（落盘时刻），后果有两个：
                //   ① 同一批切出的多段挤在同一时间点，日报时间轴错位；
                //   ② 服务端无法按「起始时间接近 + 文本包含」判重——
                //      实测同一语音被 VAD 输出两次时两段 start 只差 19ms，
                //      而真实语音相隔数秒，去重逻辑没法用。
                startOffsetMs = System.currentTimeMillis() - durationMs,
                durationMs = durationMs, hasSpeech = true,
                status = SegmentStatus.PENDING, speechRatio = 1f,
            )
        )
        // 落盘即刷新活性：前台通知与状态页都以「最近落盘」判断采集是否真在产出
        CaptureState.lastSegmentAt = System.currentTimeMillis()
        refreshNotification()
        scheduleUploadAfterCapture()
        // ⚠️ 这里**不再**触发实时对话（2026-09-10 修）。
        //
        // 旧实现在此处调了一次 `TalkController.onSegmentSaved`，而对话 VAD 那条
        // 路径（`consumeTalkSegment`）末尾也调了一次 —— 同一路 pcm 被两个 VAD
        // 各切一份，两个 VAD 的段边界不同，于是**同一句话触发两轮对话、播两遍**。
        // 实测 18:45:02 / 18:45:14：日报段切出 2810ms（「让大郭帮我查一下…」），
        // 对话段切出另一半（「让大哥帮我查一下…」），ASR 只差一个字 →
        // Hermes 侧按文本哈希去重拦不住 → 用户听到「问一遍回两遍」。
        //
        // 两个 VAD 的分工本来就是：**日报 VAD 管语料、对话 VAD 管实时对话**。
        // 日报段还要进上传管线做双层 ASR，不该兼作对话触发源；而对话 VAD
        // （SherpaVad.forTalk：0.45s 静音 / 无合并）本来就是为短指令设计的，
        // 能完整覆盖这里能覆盖的一切。留着旧触发只会重复。
        //
        // 注意：对话触发**不是**被删掉了，只是收敛到唯一入口
        // ——`consumeTalkSegment`，见下方同名注释。
    }

    /**
     * 对话 VAD 切出一段 → 投递到 [talkQueue] 后立刻返回。
     *
     * 真正的处理（本地唤醒词判定 → 落盘 → 上传）在 [consumeTalkLoop] 里做。
     * 这里**必须**非阻塞：本方法由录音循环调用，只要它等一次磁盘或一次模型推理，
     * VAD 缓冲就会堆积、丢掉的正是用户真实说出口的话。
     *
     * 门槛比日报松得多：日报要 2.2s 才算一段，「大锅，开灯」整句约 1.2s 就足够
     * 唤醒；但也设了 0.4s 下界，太短的碎片送给服务端只会白跑一次 ASR
     * （本地 KWS 虽然也能筛，但没必要让它们占队列格子）。
     */
    private fun handleTalkSegment(samples: ShortArray) {
        val durationMs = samples.size * 1000L / SR
        if (durationMs < TALK_SEGMENT_MIN_MS) return
        if (talkQueue.trySend(samples).isFailure) {
            android.util.Log.w(TAG, "对话段队列已满，丢弃 ${durationMs}ms 片段")
        }
    }

    private fun sessionDir(): File {
        val day = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        return File(getExternalFilesDir(null), "audio/$day/session_$sessionId").apply { mkdirs() }
    }

    /**
     * 触发暂停（AUTO 双触发全离线）：结束当前会话但**服务保持前台存活**。
     * 麦克风 FGS 在 Android 14+ 只能前台启动，服务一旦 stop，进程被杀后就只能靠
     * 恢复通知人工拉起——暂停保活让「Mac/蓝牙回来」时零人工干预无缝续录。
     */
    private fun pauseCapture() {
        if (!CaptureState.captureRunning || CaptureState.triggerPaused) return
        CaptureState.triggerPaused = true
        scope.launch {
            val db = AppDatabase.get(applicationContext)
            closeSession(db, "TRIGGER_GONE")
            db.serviceEventDao().insert(
                com.symi.digitalhuman.data.ServiceEvent(type = "CAPTURE_PAUSED", detail = "no trigger online")
            )
        }
    }

    /** 触发恢复：开新会话并解除暂停（服务存活时无缝续录；服务不在则走完整启动） */
    private fun resumeCapture(triggerType: String, triggerDetail: String) {
        if (!CaptureState.captureRunning || !CaptureState.triggerPaused) return
        CaptureState.triggerPaused = false
        scope.launch {
            val db = AppDatabase.get(applicationContext)
            val sid = db.sessionDao().insert(
                Session(startedAt = System.currentTimeMillis(), triggerType = triggerType, triggerDetail = triggerDetail)
            )
            sessionId = sid
            CaptureState.sessionId = sid
            CaptureState.lastCaptureAliveAt = System.currentTimeMillis()
            db.serviceEventDao().insert(
                com.symi.digitalhuman.data.ServiceEvent(type = "CAPTURE_RESUMED", detail = "session=$sid")
            )
        }
    }

    /** 结束当前会话（endReason 落库），不动服务生命周期 */
    private suspend fun closeSession(db: AppDatabase, endReason: String) {
        val sid = CaptureState.sessionId
        if (sid > 0) {
            db.sessionDao().openSession()?.let { s ->
                if (s.id == sid) db.sessionDao().update(s.copy(endedAt = System.currentTimeMillis(), endReason = endReason))
            }
        }
        CaptureState.sessionId = -1
    }

    private fun stopCapture(endReason: String) {
        android.util.Log.i(TAG, "stopCapture reason=$endReason session=$sessionId")
        if (!CaptureState.captureRunning && endReason != "MANUAL") return
        scope.launch {
            val db = AppDatabase.get(applicationContext)
            closeSession(db, endReason)
            db.serviceEventDao().insert(
                com.symi.digitalhuman.data.ServiceEvent(type = "CAPTURE_STOPPED", detail = "reason=$endReason")
            )
            CaptureState.captureRunning = false
            CaptureState.triggerPaused = false
            releaseWakeLock()
            stopSelf()
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "digitalhuman:capture").apply {
            setReferenceCounted(false); acquire(12 * 60 * 60 * 1000L)
        }
    }

    private fun releaseWakeLock() {
        try { wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
    }

    override fun onDestroy() {
        CaptureState.captureRunning = false
        releaseWakeLock()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null

    companion object {
        const val EXTRA_TRIGGER_TYPE = "trigger_type"
        const val EXTRA_TRIGGER_DETAIL = "trigger_detail"
        const val EXTRA_SESSION_ID = "session_id"
        const val ACTION_STOP = "com.symi.digitalhuman.action.STOP_CAPTURE"
        const val ACTION_PAUSE = "com.symi.digitalhuman.action.PAUSE_CAPTURE"
        const val ACTION_RESUME = "com.symi.digitalhuman.action.RESUME_CAPTURE"
        private const val NOTIF_ID = 2001
        private const val TAG = "CaptureSvc"
        private const val SR = 16000

        /**
         * VAD 实例的最长存活时长（2026-09-27）。
         *
         * 真机实测：sherpa 原生 VAD 在进程连续运行 37h / 39h 时各崩一次
         * （SIGSEGV in memcpy ← acceptWaveform，线程 DefaultDispatch），
         * 09-16 ~ 09-24 共 5 次 APP CRASH(NATIVE) 全是它。既然崩溃是「跑太久」而非
         * 「某一帧数据」，最省事的根治办法就是定期在段边界重建实例、让原生状态归零。
         * 2 小时 = 对 37h 有 18 倍余量；代价是每天 12 次毫秒级重建。
         */
        private const val VAD_RECYCLE_MS = 2 * 60 * 60 * 1000L

        /** 重建失败后的重试间隔（建不出新实例时保持旧实例继续采，不中断录音）。 */
        private const val VAD_RECYCLE_RETRY_MS = 5 * 60 * 1000L

        /** 喂给 VAD 的 FloatArray 保留强引用的个数（原生侧可能跨调用持有指针）。 */
        private const val VAD_KEEPALIVE_ARRAYS = 8

        /** 最短有效段时长：正常讲话不会低于 2.2s（用户实测：1200ms 仍放行 1~4s 键盘/瞬态噪声短段）。
         *  引用 data 包的共享常量，避免与回放列表 / 统计聚合 / 上传清理的口径分叉。 */
        internal const val MIN_SEGMENT_MS = com.symi.digitalhuman.data.MIN_SEGMENT_MS
        /** 最短有效段平均电平：低于此值视为键盘/环境噪声（16bit 满幅 32768，≈ -43 dBFS） */
        internal const val MIN_SEGMENT_RMS = 200.0
        /** 电平柱 dBFS 映射下限：低于 -50 dBFS（环境噪声底以下）视为 0 */
        internal const val LEVEL_DB_FLOOR = -50.0
        /** 电平柱 dBFS 映射上限：-3 dBFS（几乎削波）即满格 */
        internal const val LEVEL_DB_CEIL = -3.0
        /** 电平回落系数（每 10ms chunk）：0.88 ≈ 80ms 时间常数，柱子快攻慢放不闪跳 */
        internal const val LEVEL_RELEASE = 0.88f
        /** 碎段合并窗口：相邻 VAD 段墙钟间隔 ≤5s 拼为同句（含 ~1s 静音判定延迟） */
        internal const val MERGE_GAP_MS = 5000L

        /**
         * 对话段最短时长（AUTO 模式专用）。
         *
         * 刻意远低于日报的 [MIN_SEGMENT_MS]（2.2s）：唤醒管家的是「大锅，开灯」
         * 这类短指令，整句约 1.2s，用日报门槛会被直接丢弃。0.4s 是「能容下两个
         * 音节 + 不吃进环境噪声」的下界，真正的语义判断交给服务端 ASR。
         */
        internal const val TALK_SEGMENT_MIN_MS = 400L

        /**
         * 判断一个既有段是否为无效段（供回放列表 / 统计聚合复用）。
         *
         * 只按时长判定，不读文件算 RMS：回放与统计面对的是已落盘的段，
         * 重新解码每个 WAV 算电平代价过高，且落盘时已过过一次能量关。
         */
        fun isInvalidSegment(durationMs: Long): Boolean = durationMs < MIN_SEGMENT_MS

        fun start(ctx: Context, triggerType: String, detail: String, sessionId: Long = -1L): Boolean =
            // 先真启动，失败再降级挂恢复通知（2026-09-27 变更）。
            //
            // 原实现在 Android 14+ 且界面不在前台时**连试都不试**，直接挂「点击恢复」通知，
            // 理由是「后台启动 microphone FGS 必定被系统拒绝」。这条假设过于绝对：
            // 应用进入**电池优化白名单**后系统会放行（白名单正确用法之一）。一律不试的
            // 代价很具体——进程一旦重启（崩溃 / 被清理 / 装机），就必须有人点一次通知
            // 才能恢复录音；长期驻留终端上等于「没人点就一直不录」（2026-09-24 那 3 天
            // 断采就是这么来的）。
            // 现在改成乐观尝试：能起就起（无人值守自愈），被拒时本服务的 onCreate 会
            // 捕获 SecurityException → onForegroundBlocked → 同样挂恢复通知 + stopSelf，
            // 不会出现「启动→崩溃→自动重启」死循环（那条路径有专门的兜底）。
            try {
                val i = Intent(ctx, CaptureService::class.java)
                    .putExtra(EXTRA_TRIGGER_TYPE, triggerType)
                    .putExtra(EXTRA_TRIGGER_DETAIL, detail)
                    .putExtra(EXTRA_SESSION_ID, sessionId)
                if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
                true
            } catch (e: Exception) {
                // 兜底：任何启动失败都转为恢复通知，不让异常外泄
                CaptureState.pendingRecoverNotify = true
                runCatching { RecoveryNotifier.notify(ctx, e.message ?: "start failed") }
                false
            }

        fun stop(ctx: Context) {
            val i = Intent(ctx, CaptureService::class.java).setAction(ACTION_STOP)
            try { ctx.startService(i) } catch (_: Exception) {}
        }

        /** 触发全离线：服务保持存活，仅暂停写入并结束会话（服务不在时等价于无操作） */
        fun pause(ctx: Context) {
            val i = Intent(ctx, CaptureService::class.java).setAction(ACTION_PAUSE)
            try { ctx.startService(i) } catch (_: Exception) {}
        }

        /** 触发恢复：服务存活时无缝续录；服务不在时走完整启动（含后台受限降级路径） */
        fun resume(ctx: Context, triggerType: String, detail: String) {
            if (!CaptureState.captureRunning) { start(ctx, triggerType, detail); return }
            val i = Intent(ctx, CaptureService::class.java)
                .setAction(ACTION_RESUME)
                .putExtra(EXTRA_TRIGGER_TYPE, triggerType)
                .putExtra(EXTRA_TRIGGER_DETAIL, detail)
            try { ctx.startService(i) } catch (_: Exception) {}
        }

        fun recoverIntent(ctx: Context): PendingIntent =
            PendingIntent.getForegroundService(
                ctx, 11,
                Intent(ctx, CaptureService::class.java).putExtra(EXTRA_TRIGGER_TYPE, "RECOVER"),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
    }
}

/** 恢复通知（高优先级，点击即拉起麦克风前台服务） */
object RecoveryNotifier {
    fun notify(ctx: Context, detail: String) {
        val n = NotificationCompat.Builder(ctx, App.CHANNEL_RECOVER)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("采集需要一键恢复")
            .setContentText("检测到您在附近，点击恢复语音采集")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(CaptureService.recoverIntent(ctx))
            .build()
        androidx.core.app.NotificationManagerCompat.from(ctx).notify(3001, n)
    }

    /**
     * 撤销「点击恢复」通知（2026-09-27）。
     *
     * 采集真正跑起来时必须撤掉它。改之前只在内存里清了 pendingRecoverNotify 标志，
     * 通知本身一直挂在通知栏——用户看到的是「采集需要一键恢复」+「语料采集中」两条
     * 并存的矛盾状态，会以为采集没恢复而反复去点（点一次又会重启一次会话）。
     */
    fun cancel(ctx: Context) {
        runCatching { androidx.core.app.NotificationManagerCompat.from(ctx).cancel(3001) }
    }
}
