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

        // 1) 过期删除：**已上传（SYNCED）**且上传时间早于保留期。
        //
        // 1.0.4 修 bug：原实现遍历 pending()（status=PENDING）再要求 uploadedAt<cutoff，
        // 但 PENDING 的 uploadedAt 恒为 null（从未上传过）→ `?: continue` 全部跳过，
        // 而 SYNCED 段根本不在 pending() 里 → 「音频保留天数」实际只对软删段生效，
        // 正常录音会一直堆到存储上限（默认 0=不限）位置都不动。
        // PENDING 是唯一副本，绝不能按天数删；只清已确认上传到服务端的那部分。
        val expired = db.segmentDao().syncedBefore(cutoff)
        if (expired.isNotEmpty()) deleteSegments(db, expired.map { it.id })

        // 2) 存储阈值 LRU
        if (s.maxStorageMb > 0) {
            // 同上：LRU 只看已上传段（PENDING 是唯一副本，清了就丢）
            val all = db.segmentDao().syncedBefore(Long.MAX_VALUE)
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
