package com.symi.digitalhuman.worker

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.symi.digitalhuman.data.AppDatabase
import com.symi.digitalhuman.data.DailyReport
import com.symi.digitalhuman.data.MIN_SEGMENT_MS
import com.symi.digitalhuman.data.SettingsStore
import com.symi.digitalhuman.data.SummaryCard
import com.symi.digitalhuman.data.SummaryCards
import com.symi.digitalhuman.sync.EnrollQueue
import com.symi.digitalhuman.sync.MacClient
import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 上传 + 拉回报。三种用法，靠 inputData 区分：
 *
 * 1) 日终终稿（17:30，[KEY_FINAL]=true、[KEY_STRICT]=true）
 *    上传 → 通知 Mac 跑完整管线（ASR + 去口气 + 总结）→ 拉日报，未就绪退避重试。
 *
 * 2) 准实时增量（每 N 分钟，KEY_FINAL=false、KEY_STRICT=false）
 *    上传 → 通知 Mac 只跑 ASR（沿用上一版干净稿/总结）→ 尝试拉日报，
 *    未就绪直接成功，不留退避任务。这样用户在 17:30 之前也能看到今天到目前为止
 *    的原始转写，而 LLM 不会被高频重算拖垮（去口气是全天逐字稿级的 O(n) 任务，
 *    每轮都重算的话一天十几轮是 O(n²)，4B 本地模型会累积到几十分钟）。
 *
 * 3) 手动「立即上传」：final=true、strict=true，期望拿到完整结果。
 *
 * 上传本身是增量的（只取 PENDING 段，成功后 markSynced），任意频次重复触发都安全。
 */
class DailyUploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    private val final: Boolean get() = inputData.getBoolean(KEY_FINAL, true)
    private val strict: Boolean get() = inputData.getBoolean(KEY_STRICT, true)

    /** 重传某天后要额外通知的日期（1.0.4）：让服务端去重算那一天，而不是今天。 */
    private val doneDate: String? get() = inputData.getString(KEY_DONE_DATE)

    override suspend fun doWork(): Result {
        val db = AppDatabase.get(applicationContext)
        val s = SettingsStore(applicationContext).current()
        if (s.paused) return Result.success()
        val client = MacClient(s)
        // 用带原因的自检而不是裸 ping：上传失败是最常报的问题，
        // 日志里有「连不上 IP:端口」还是「Token 不对」能省掉一轮来回排查。
        val (reachable, why) = client.checkReachable()
        if (!reachable) {
            android.util.Log.w("DailyUploadWorker", "上传跳过：$why")
            return Result.retry()
        }

        // 待补交的声纹样本：App 进程没在前台时，借这一轮上传把它补交上去。
        // （App 前台时 ViewModel 每 20 秒自己试一次，这里是 App 被杀后的兜底路径。）
        runCatching { EnrollQueue.flush(applicationContext, client) }
            .onFailure { android.util.Log.w(TAG, "补交声纹样本失败: ${it.message}") }

        val pending = db.segmentDao().pending()
        var uploaded = 0
        for (seg in pending) {
            // 历史无效段清理：只改状态，不删文件。
            // 曾经这里直接 File.delete() 硬删除——一旦 MIN_SEGMENT_MS 阈值调错，
            // 真实语音就被永久删掉且无法找回。现在由 RetentionWorker 在保留期后清理，
            // 期间发现误判还能把状态改回来。
            if (seg.durationMs < MIN_SEGMENT_MS) {
                db.segmentDao().markDeleted(listOf(seg.id))
                continue
            }
            val f = java.io.File(seg.filePath)
            if (!f.exists()) {
                db.segmentDao().markDeleted(listOf(seg.id))
                continue
            }
            // uploadSegment 在网络抖动时抛 IOException。不捕获的话异常会冒泡到
            // CoroutineWorker，被记成 failure 而不是 retry——与这里「失败就退避重试」
            // 的意图完全相反，上传会直接放弃。
            val ok = try {
                client.uploadSegment(
                    seg.id, seg.sessionId, seg.startOffsetMs,
                    seg.durationMs, seg.speechRatio, f,
                )
            } catch (e: Exception) {
                android.util.Log.w(TAG, "upload seg ${seg.id} threw: ${e.javaClass.simpleName}")
                false
            }
            when {
                ok -> {
                    db.segmentDao().markSynced(listOf(seg.id), System.currentTimeMillis())
                    uploaded++
                }
                else -> return Result.retry()
            }
        }

        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        // 回执带服务端处理窗口（2026-09-27）：服务端只在 13:00 / 17:45 两个窗口内跑管线，
        // 窗口外**只落库不处理**。旧版这里无视回执、固定轮询报告 150 秒等结果，
        // 窗口外那 150 秒必然白等（服务端根本不会开始）——白耗电、白占内存、
        // 还把「日报不实时」的锅甩给了网络。现在窗口外直接跳过等待。
        val ack = client.notifyUploadDone(today, final = final)
        // 重传场景（1.0.4）：本次上传的是**历史某天**的段，done 通知的日期必须是那一天，
        // 否则服务端只会重算今天，历史日期的清稿/总结/关键词不会刷新。
        doneDate?.takeIf { it.isNotBlank() && it != today }?.let { d ->
            runCatching { client.notifyUploadDone(d, final = false, force = true) }
                .onFailure { android.util.Log.w(TAG, "补发 done($d) 失败: ${it.message}") }
        }
        val serverInWindow = ack.inWindow

        // 历史回填：必须在 today 拉取之前，否则 today 未就绪时 return retry 就跳过了。
        // 这样即使今天没有数据，也能把 9/2 等历史报告从 Mac 拉回 Room。
        runCatching { backfillHistoricalReports(db, client, today, days = 7) }
            .onFailure { android.util.Log.w(TAG, "backfill 历史日报失败: ${it.message}") }

        // 增量轮且本轮有新段：Mac 端管线是异步的，立刻拉拿到的是上一轮旧稿，
        // 旧稿写进 Room 后页面看起来就是「日报不实时」。短轮询等本轮结果落库
        // （服务端增量轮修复后 30~120s，冷启动缓存时最慢约 2min），拿到比本地
        // 新的原始稿再继续；超时就按旧逻辑走，下一轮自然补上。无新段不等待。
        // 服务端在窗口外时跳过整段等待（见上），只拉一次现有稿。
        var reportJson = client.fetchReport(today)
        if (!strict && uploaded > 0 && reportJson != null && serverInWindow) {
            val prevRaw = db.reportDao().of(today)?.rawTranscript
            var waitedMs = 0
            while (waitedMs < 150_000) {
                val probe = runCatching {
                    reportJson?.let { Json.parseToJsonElement(it).jsonObject }
                }.getOrNull() ?: break
                val probeReady = probe["ready"]?.jsonPrimitive?.booleanOrNull == true
                val probeRaw = (probe["raw_transcript"] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: ""
                if (probeReady && probeRaw != prevRaw) break
                delay(15_000)
                waitedMs += 15_000
                reportJson = client.fetchReport(today) ?: break
            }
        }
        // 拉今天日报。增量轮次不重试：管线是异步跑的，下一轮自然能拿到；
        // 若这里 retry，WorkManager 会排一个退避任务，高频增量下会堆积成任务风暴。
        reportJson ?: return if (strict) Result.retry() else Result.success()
        try {
            val obj = Json.parseToJsonElement(reportJson).jsonObject
            // 角色标记**与日报正文解耦**（2026-09-14）。管线里说话人聚类（写角色）排在
            // 转写之后、日报正文（干净稿/总结，要跑 LLM）之前就已完成。旧实现把回流
            // 放在 ready 判断之后，于是只要 LLM 还没跑完、或管线崩过，状态页就一直显示
            // 「待判定」——用户看到的就是「Mac 明明开着，手机上还停在待判定」。
            // 现在只要服务端带回了 flags 就先落库，正文照旧等 ready。
            applySegmentFlags(db, obj["segment_flags"], s.installId)
            val ready = obj["ready"]?.jsonPrimitive?.booleanOrNull == true
            if (!ready) return if (strict) Result.retry() else Result.success()
            saveReportFromJson(db, today, obj)
        } catch (_: Exception) {
            return if (strict) Result.retry() else Result.success()
        }
        AggregateWorker.refreshDay(db, today)
        return Result.success(workDataOf("uploaded" to uploaded))
    }

    /**
     * 解析 Mac 回流的 segment_flags：[{id, is_user, is_speech, speaker, speech_ratio}]
     *
     * is_user / is_speech 是**三态**：true / false / null（null = 未判定）。
     * 服务端对非人声段就返回 null。若用 `toString() == "true"` 解析，
     * JSON null 会变成字符串 "null"，进而被当成 false——「未判定」被静默
     * 写成「确定不是本人」，且无法恢复。这里必须显式识别 JsonNull。
     */
    private suspend fun applySegmentFlags(
        db: AppDatabase,
        flags: kotlinx.serialization.json.JsonElement?,
        myInstall: String,
    ) {
        val arr = flags as? kotlinx.serialization.json.JsonArray ?: return
        for (e in arr) {
            val o = e.jsonObject
            // 设备归属过滤（2026-09-15 多设备互不干扰）：同一租户下两台设备都
            // 可能有本机 3 号段，服务端会把两台的标记一起回传；不过滤就会互相
            // 涂改状态页的角色/本人判定。
            // 空 install_key = 服务端的历史行（归属未知）→ 一律接受，
            // 否则升级前的数据会永远停在「待判定」。
            val flagKey = o["install_key"]?.jsonPrimitive?.contentOrNull.orEmpty()
            if (flagKey.isNotEmpty() && myInstall.isNotBlank() && flagKey != myInstall) continue
            val id = o["id"]?.jsonPrimitive?.longOrNull ?: continue
            val isUser = o["is_user"].triStateBool()
            val isSpeech = o["is_speech"].triStateBool()
            val speaker = o["speaker"]?.jsonPrimitive?.contentOrNull ?: ""
            val ratio = o["speech_ratio"]?.jsonPrimitive?.contentOrNull?.toFloatOrNull() ?: 0f
            db.segmentDao().markSpeaker(id, isUser, ratio, speaker, isSpeech)
        }
    }

    /** 三态布尔解析：null / JsonNull → null；其余按 JSON boolean 解析。 */
    private fun kotlinx.serialization.json.JsonElement?.triStateBool(): Boolean? =
        when (this) {
            null, JsonNull -> null
            else -> runCatching { this.jsonPrimitive.booleanOrNull }.getOrNull()
        }

    // saveReportFromJson 已移至 companion object（2026-09-21）：界面需要「打开日报页
    // 立即同步」，放在实例方法里外部拿不到。移动后本类内的调用点无需改动。

    /**
     * 历史日报回填：拉近 N 天的报告并写入 Room，用于补齐「Mac 端已就绪、App 端 Room 缺数据」的差距
     * （典型：LLM 修好后已重算历史报告，或用户重装/清空 App 后再装）。
     * 单日失败不影响其它日；任一日期 ready=false（Mac 端尚未处理）也直接跳过。
     */
    private suspend fun backfillHistoricalReports(
        db: AppDatabase, client: MacClient, today: String, days: Int,
    ) {
        val fmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        val cal = java.util.Calendar.getInstance().apply {
            time = fmt.parse(today) ?: return
        }
        for (i in 1..days) {
            cal.add(java.util.Calendar.DAY_OF_MONTH, -1)
            val d = fmt.format(cal.time)
            try {
                val json = client.fetchReport(d) ?: continue
                val obj = Json.parseToJsonElement(json).jsonObject
                val ready = obj["ready"]?.jsonPrimitive?.booleanOrNull == true
                if (!ready) continue
                saveReportFromJson(db, d, obj)
            } catch (e: Exception) {
                android.util.Log.w(TAG, "backfill $d 失败: ${e.javaClass.simpleName}: ${e.message}")
            }
        }
    }

    companion object {
        const val NAME = "daily_upload"
        const val NAME_INCREMENTAL = "incremental_upload"

        /**
         * 立刻跑一次增量上传（1.0.4「按天重传」用）。
         *
         * 重传已经把段改回 PENDING，这里只是不等下一次周期任务、马上触发一轮；
         * 内嵌一次 /api/done 通知，服务端会据此把该日期拉进补跑队列重算。
         */
        fun runNow(ctx: Context, doneDate: String? = null) {
            val req = OneTimeWorkRequestBuilder<DailyUploadWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .apply {
                    if (!doneDate.isNullOrBlank()) {
                        setInputData(workDataOf(KEY_DONE_DATE to doneDate))
                    }
                }
                .build()
            WorkManager.getInstance(ctx).enqueue(req)
        }
        const val KEY_FINAL = "final"
        const val KEY_DONE_DATE = "done_date"
        const val KEY_STRICT = "strict"
        private const val TAG = "DailyUploadWorker"

        /**
         * 将 Mac /api/report/{date} 的 JSON 写入 Room。三个文本字段的 \n 转义
         * 由 jsonPrimitive.content 正确解码（用 toString() 会保留字面 \n）。
         *
         * 2026-09-21 由类内实例方法移到这里：界面也要用它（见 [syncReportNow]），
         * 放在实例方法里外部拿不到。移动后类内的调用点无需改动。
         */
        private suspend fun saveReportFromJson(
            db: AppDatabase, date: String, obj: kotlinx.serialization.json.JsonObject,
        ) {
            fun str(k: String) = obj[k]?.let {
                (it as? kotlinx.serialization.json.JsonPrimitive)?.content
            } ?: ""
            // 总结卡片：Mac 端已按【回顾】【关键原话】…栏目解析好，这里只需原样转存。
            // 卡片为空（模型没按格式输出）时留空串，UI 回退成整段文本，内容不丢。
            val cards = (obj["summary_cards"] as? kotlinx.serialization.json.JsonArray)
                ?.mapNotNull { e ->
                    val o = e as? kotlinx.serialization.json.JsonObject ?: return@mapNotNull null
                    val t = o["title"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content } ?: ""
                    val b = o["body"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content } ?: ""
                    if (t.isBlank() && b.isBlank()) null else SummaryCard(t, b)
                } ?: emptyList()
            db.reportDao().upsert(
                DailyReport(
                    date = date,
                    rawTranscript = str("raw_transcript"),
                    cleanTranscript = str("clean_transcript"),
                    summary = str("summary"),
                    generatedAt = System.currentTimeMillis(),
                    ready = true,
                    summaryCards = SummaryCards.encode(cards),
                )
            )
        }

        /**
         * 按需同步：只把指定日期的报告拉回来写进 Room，**不触发上传**。
         * 返回是否真的写入了内容。
         *
         * 为什么需要（2026-09-21）：手机端原本只有本 Worker 的周期任务（准实时
         * 15-20 分钟一次）会把 Mac 的处理结果搬进 Room，用户讲完一段、打开日报页
         * 刷新却看不到新的说话人/正文，只会判断成「上传了但没处理」。
         * 给界面一个按需同步入口，打开页面就能拿到最新结果。
         *
         * 判据与 doWork 一致：`ready != true`（Mac 还没处理完）时**不写库**，
         * 避免用空内容把 Room 里已有的正文覆盖掉。
         */
        suspend fun syncReportNow(ctx: Context, date: String): Boolean = runCatching {
            val client = MacClient(SettingsStore(ctx).current())
            val json = client.fetchReport(date) ?: return@runCatching false
            val obj = Json.parseToJsonElement(json).jsonObject
            val ready = (obj["ready"] as? kotlinx.serialization.json.JsonPrimitive)
                ?.content?.toBoolean() ?: false
            if (!ready) return@runCatching false
            saveReportFromJson(AppDatabase.get(ctx), date, obj)
            true
        }.getOrDefault(false)
    }
}
