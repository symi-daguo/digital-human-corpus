package com.symi.digitalhuman.worker

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.symi.digitalhuman.data.AppDatabase
import com.symi.digitalhuman.data.SegmentStatus
import java.io.File
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/** 每日 03:00：按保留天数 + 存储阈值 LRU 删除手机端音频，并刷新统计 */
class RetentionWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val db = AppDatabase.get(applicationContext)
        val s = com.symi.digitalhuman.data.SettingsStore(applicationContext).current()

        val cutoff = System.currentTimeMillis() - s.retentionDays * 86_400_000L

        // 0) 软删除段的文件清理：DailyUploadWorker 判定无效段时只改状态不删文件，
        //    真正的删除推迟到这里。录音时间超过保留期才清，这段时间内发现阈值设错
        //    还能把状态改回来找回音频（硬删除是不可逆的）。
        val purgeIds = db.segmentDao().deletedWithFile()
            .filter { it.startOffsetMs < cutoff }
            .map { it.id }
        if (purgeIds.isNotEmpty()) deleteSegments(db, purgeIds)

        // 1) 过期删除：PENDING 中已上传且上传时间早于保留期
        val expired = db.segmentDao().pending() // 先取全部再过滤，MVP 数据量小
        val expiredIds = mutableListOf<Long>()
        for (seg in expired) {
            val uploadedAt = seg.uploadedAt ?: continue
            if (uploadedAt < cutoff) expiredIds.add(seg.id)
        }
        deleteSegments(db, expiredIds)

        // 2) 存储阈值 LRU
        if (s.maxStorageMb > 0) {
            val all = db.segmentDao().pending()
            var totalBytes = all.sumOf { File(it.filePath).takeIf { f -> f.exists() }?.length() ?: 0L }
            val limitBytes = s.maxStorageMb * 1024L * 1024L
            if (totalBytes > limitBytes) {
                val sorted = all.sortedBy { it.uploadedAt ?: 0L }
                val toDelete = mutableListOf<Long>()
                for (seg in sorted) {
                    if (totalBytes <= limitBytes) break
                    val f = File(seg.filePath)
                    val size = f.length()
                    if (seg.status == SegmentStatus.SYNCED) {
                        toDelete.add(seg.id)
                        totalBytes -= size
                    }
                }
                deleteSegments(db, toDelete)
            }
        }

        // 3) 刷新近 3 天统计
        val fmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        repeat(3) { i ->
            val cal = Calendar.getInstance().apply { add(Calendar.DAY_OF_YEAR, -i) }
            AggregateWorker.refreshDay(db, fmt.format(cal.time))
        }
        return Result.success()
    }

    private suspend fun deleteSegments(db: AppDatabase, ids: List<Long>) {
        for (id in ids) {
            db.segmentDao().byId(id)?.let { seg ->
                File(seg.filePath).takeIf { it.exists() }?.delete()
            }
        }
        db.segmentDao().markDeleted(ids)
    }

    companion object { const val NAME = "retention" }
}
