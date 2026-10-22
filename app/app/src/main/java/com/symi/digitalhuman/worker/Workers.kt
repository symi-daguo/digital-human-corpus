package com.symi.digitalhuman.worker

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.symi.digitalhuman.data.AppDatabase
import com.symi.digitalhuman.data.StatHour
import com.symi.digitalhuman.service.CaptureService
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/** 从 segment 聚合刷新某天的小时级统计 */
class AggregateWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val date = inputData.getString(KEY_DATE) ?: today()
        refreshDay(AppDatabase.get(applicationContext), date)
        return Result.success()
    }

    companion object {
        const val NAME = "aggregate"
        const val KEY_DATE = "date"

        fun today(): String = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

        suspend fun refreshDay(db: AppDatabase, date: String) {
            val fmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)
            val dayStart = (fmt.parse(date) ?: return).time
            // 用 Calendar 推算当日终点：直接 +86_400_000 在夏令时切换日会算错，
            // 那天会漏掉或多算一小时的数据。
            val dayEnd = Calendar.getInstance().apply {
                timeInMillis = dayStart
                add(Calendar.DAY_OF_YEAR, 1)
            }.timeInMillis
            val hourFmt = SimpleDateFormat("HH", Locale.US)

            val triggersByHour = IntArray(24)
            val secondsByHour = LongArray(24)

            // 触发次数：按 session 起始小时计数
            for (s in db.sessionDao().listBetween(dayStart, dayEnd)) {
                val h = hourFmt.format(Date(s.startedAt)).toIntOrNull() ?: continue
                triggersByHour[h]++
            }
            // 录音秒数：按段起始小时累加。
            // 过滤条件（filePath 非空、时长达 MIN_SEGMENT_MS）已在 SQL 里完成：
            // 原先是 listAll() 把整张 segment 表读进内存再在 Kotlin 里过滤，
            // 一年积累十几万段后是 O(n) 全表扫描 + 全量对象驻留。
            for (seg in db.segmentDao().listBetween(dayStart, dayEnd)) {
                val h = hourFmt.format(Date(seg.startOffsetMs)).toIntOrNull() ?: continue
                secondsByHour[h] += seg.durationMs / 1000
            }
            // 先清当天再写：否则某小时的数据从「有」变「无」（如删除音频）时，
            // 旧值会残留，统计永远只增不减。
            db.statDao().clearDate(date)
            for (h in 0 until 24) {
                if (triggersByHour[h] > 0 || secondsByHour[h] > 0)
                    db.statDao().upsert(StatHour(date, h, triggersByHour[h], secondsByHour[h]))
            }
        }
    }
}

/** 统一定时调度入口 */
object WorkScheduler {

    /**
     * 上传任务的重试退避（2026-09-10，修复「电脑开机后手机数据不自动同步」）。
     *
     * 修复前：所有上传请求都没配退避条件 → 走 WorkManager 默认的 EXPONENTIAL
     * （30s 起、每次翻倍、上限 5 小时）。Mac 关机一夜期间上传 worker 反复
     * [DailyUploadWorker] 的 Result.retry()，退避被拉到几十分钟～5 小时；而新段
     * 触发的 uploadAfterCapture 用的是 KEEP，见队列里已有任务（退避中）直接跳过，
     * 无法提前唤醒。Mac 开机后手机又无从感知（没有「服务端→手机」的推送通道），
     * 只能干等退避到期——实测当天 08:31 起录的段，直到 11:18（Mac 服务 10:55
     * 已就绪）才补传上去。
     *
     * 改为 LINEAR 15s：第 n 次重试等 15n 秒（15s/30s/45s…），有界且可预测，
     * 不再出现「退避到 5 小时」的悬崖。真正「Mac 一上线就立刻补传」由
     * GuardService 的 TCP 上升沿调用 [uploadOnReconnect] 完成，本退避只作兜底。
     */
    private const val UPLOAD_BACKOFF_SEC = 15L

    /**
     * 上传任务的网络约束（2026-09-21 修「人在外网 + 移动数据 = 语料一条都传不上去」）。
     *
     * allowMetered = true  → [NetworkType.CONNECTED]：WiFi 与蜂窝都传。
     * allowMetered = false → [NetworkType.UNMETERED]：只走 WiFi（省流量）。
     *
     * ⚠️ 为什么必须可配：修复前这里是**写死 UNMETERED**，于是手机一旦离开 WiFi
     * （车上/路上用移动数据），约束永不满足 → 上传任务永不执行；而唯一不带网络
     * 约束的自动通路 [uploadOnReconnect] 当时又挂在「局域网 TCP 上升沿」上，
     * 人在外网同样永不触发。两条路一起锁死，表现为「对话正常、语料全丢」。
     */
    private fun uploadConstraints(allowMetered: Boolean): Constraints =
        Constraints.Builder()
            .setRequiredNetworkType(
                if (allowMetered) NetworkType.CONNECTED else NetworkType.UNMETERED
            )
            .build()

    fun scheduleAll(
        ctx: Context, processMinuteOfDay: Int,
        realtimeUpload: Boolean = true, realtimeIntervalMin: Int = 30,
        uploadOnMetered: Boolean = true,
    ) {
        val wm = WorkManager.getInstance(ctx)

        // 每日上传：以 processMinuteOfDay 计算下次触发点，24h 周期
        val cal = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, processMinuteOfDay / 60)
            set(Calendar.MINUTE, processMinuteOfDay % 60)
            set(Calendar.SECOND, 0)
            if (before(Calendar.getInstance())) add(Calendar.DAY_OF_YEAR, 1)
        }
        val initialDelay = cal.timeInMillis - System.currentTimeMillis()
        wm.enqueueUniquePeriodicWork(
            // REPLACE 而不是 UPDATE：UPDATE 保留旧任务的周期锚点（period start），
            // initialDelay 只在首次生效，改过处理时间后 next 触发点会漂移到旧锚点+24h
            // （实测 17:30 的任务漂到 15:02）。REPLACE 每次按当前设置重算，
            // firstRun = 下一个 17:30，后续锚定在此，代价仅是保存设置会重置周期，可接受。
            DailyUploadWorker.NAME, ExistingPeriodicWorkPolicy.REPLACE,
            PeriodicWorkRequestBuilder<DailyUploadWorker>(24, TimeUnit.HOURS)
                .setInitialDelay(initialDelay, TimeUnit.MILLISECONDS)
                .setBackoffCriteria(BackoffPolicy.LINEAR, UPLOAD_BACKOFF_SEC, TimeUnit.SECONDS)
                .setInputData(workDataOf(DailyUploadWorker.KEY_FINAL to true))
                .build()
        )

        // 准实时增量上传：每 N 分钟把新录的段推给 Mac 并触发转写（不跑 LLM）。
        // 网络约束可配（2026-09-21）：默认 CONNECTED（WiFi + 蜂窝都传），
        // 关掉设置项才回到 UNMETERED（只走 WiFi）。
        // 修复前这里写死 UNMETERED —— 本意是「24h 录音每小时几十 MB，走蜂窝
        // 半个月吃掉一个月套餐」，但代价是手机一离开 WiFi 就**永不执行上传**，
        // 实测整上午 1127 轮对话 0 段入库（见 AppSettings.uploadOnMetered）。
        if (realtimeUpload) {
            val min = realtimeIntervalMin.coerceAtLeast(15)
            wm.enqueueUniquePeriodicWork(
                DailyUploadWorker.NAME_INCREMENTAL, ExistingPeriodicWorkPolicy.REPLACE,
                PeriodicWorkRequestBuilder<DailyUploadWorker>(
                    min.toLong(), TimeUnit.MINUTES,
                    // 弹性窗口 5 分钟：让系统把上传与系统其它后台任务合并到同一个
                    // 唤醒窗口，显著省电（Doze 下尤其明显），代价只是最多晚 5 分钟。
                    5, TimeUnit.MINUTES,
                )
                    .setConstraints(uploadConstraints(uploadOnMetered))
                    .setBackoffCriteria(BackoffPolicy.LINEAR, UPLOAD_BACKOFF_SEC, TimeUnit.SECONDS)
                    .setInputData(
                        workDataOf(
                            DailyUploadWorker.KEY_FINAL to false,
                            DailyUploadWorker.KEY_STRICT to false,
                        )
                    )
                    .build()
            )
        } else {
            wm.cancelUniqueWork(DailyUploadWorker.NAME_INCREMENTAL)
        }

        // 每日 03:00 保留策略
        val rcal = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, 3); set(Calendar.MINUTE, 0); set(Calendar.SECOND, 0)
            if (before(Calendar.getInstance())) add(Calendar.DAY_OF_YEAR, 1)
        }
        wm.enqueueUniquePeriodicWork(
            RetentionWorker.NAME, ExistingPeriodicWorkPolicy.REPLACE,
            PeriodicWorkRequestBuilder<RetentionWorker>(24, TimeUnit.HOURS)
                .setInitialDelay(rcal.timeInMillis - System.currentTimeMillis(), TimeUnit.MILLISECONDS)
                .build()
        )
    }

    /** 立即触发一次上传（手动按钮）。REPLACE：取消可能卡在退避重试中的旧任务，确保按钮立即生效。
     *  不带输入数据 → final 默认 true（手动上传按终稿处理）。 */
    fun uploadNow(ctx: Context) {
        WorkManager.getInstance(ctx).enqueueUniqueWork(
            "daily_upload_now", ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<DailyUploadWorker>()
                .setBackoffCriteria(BackoffPolicy.LINEAR, UPLOAD_BACKOFF_SEC, TimeUnit.SECONDS)
                .build()
        )
    }

    /**
     * Mac 从不可达恢复可达时的补传（2026-09-10）。
     *
     * 由 GuardService 的 TCP 探测上升沿调用：Mac 关机一夜后开机，服务一就绪
     * 就把手机上积压的段一次性推上去，不再等 WorkManager 退避到期。
     *
     * 用 REPLACE 而不是 KEEP：退避中的旧任务必须被取消重建，runAttemptCount
     * 才会归零（KEEP 会保留那条已退避到几十分钟的任务，白白继续等）。
     *
     * final=false / strict=false：只补 ASR 转写，不触发日终「全段重转 + 清理音频」
     * 的终稿流程（那是 17:30 或用户手动上传时才做的事），补传成本低、可随时重跑。
     * 不设网络约束：上升沿来自局域网 TCP 探测成功，此刻一定在同一个 WiFi 下。
     */
    fun uploadOnReconnect(ctx: Context) {
        WorkManager.getInstance(ctx).enqueueUniqueWork(
            "daily_upload_now", ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<DailyUploadWorker>()
                .setBackoffCriteria(BackoffPolicy.LINEAR, UPLOAD_BACKOFF_SEC, TimeUnit.SECONDS)
                .setInputData(
                    workDataOf(
                        DailyUploadWorker.KEY_FINAL to false,
                        DailyUploadWorker.KEY_STRICT to false,
                    )
                )
                .build()
        )
    }

    /**
     * 采集侧自动触发：新段落盘后立即安排一次增量上传。
     * KEEP 去重：已有同队列任务（排队中/运行中/退避重试中）就不再排——连续说话时
     * 每段都调用也不会刷任务，实际节奏 ≈ 每轮工作完成后再来一批。
     * 必须显式 final=false：否则走默认终稿（每段跑一次 LLM）。
     * 效果：日报从「最长等一个上传周期（15~30min）」变为「说完话约 1 分钟内更新」。
     */
    fun uploadAfterCapture(ctx: Context, uploadOnMetered: Boolean = true) {
        WorkManager.getInstance(ctx).enqueueUniqueWork(
            "daily_upload_now", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<DailyUploadWorker>()
                .setConstraints(uploadConstraints(uploadOnMetered))
                // 退避必须有界：Mac 离线时这条任务会一直 retry，默认指数退避会把
                // 下一次重试推到几十分钟后（详见 UPLOAD_BACKOFF_SEC 注释）。
                .setBackoffCriteria(BackoffPolicy.LINEAR, UPLOAD_BACKOFF_SEC, TimeUnit.SECONDS)
                .setInputData(
                    workDataOf(
                        DailyUploadWorker.KEY_FINAL to false,
                        DailyUploadWorker.KEY_STRICT to false,
                    )
                )
                .build()
        )
    }
}
