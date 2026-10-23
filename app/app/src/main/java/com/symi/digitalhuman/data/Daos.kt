package com.symi.digitalhuman.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import androidx.room.Update
import kotlinx.coroutines.flow.Flow

/** 供 Room @Query 字符串模板使用的编译期常量（SQL 里不能引用运行时对象）。 */
private const val DELETED_STATUS = "DELETED"

@Dao
interface SessionDao {
    @Insert
    suspend fun insert(s: Session): Long

    @Update
    suspend fun update(s: Session)

    @Query("SELECT * FROM session ORDER BY startedAt DESC LIMIT 1")
    suspend fun latest(): Session?

    @Query("SELECT * FROM session WHERE endedAt IS NULL ORDER BY startedAt DESC LIMIT 1")
    suspend fun openSession(): Session?

    @Query("SELECT COALESCE(COUNT(*),0) FROM session WHERE startedAt >= :dayStart AND startedAt < :dayEnd")
    suspend fun countBetween(dayStart: Long, dayEnd: Long): Int

    @Query("SELECT * FROM session WHERE startedAt >= :dayStart AND startedAt < :dayEnd ORDER BY startedAt DESC")
    fun observeBetween(dayStart: Long, dayEnd: Long): Flow<List<Session>>

    @Query("SELECT * FROM session WHERE startedAt >= :dayStart AND startedAt < :dayEnd ORDER BY startedAt")
    suspend fun listBetween(dayStart: Long, dayEnd: Long): List<Session>
}

@Dao
interface SegmentDao {
    @Insert
    suspend fun insert(s: Segment): Long

    @Update
    suspend fun update(s: Segment)

    @Query("SELECT * FROM segment WHERE id = :id")
    suspend fun byId(id: Long): Segment?

    @Query("SELECT * FROM segment WHERE sessionId = :sessionId ORDER BY startOffsetMs")
    suspend fun ofSession(sessionId: Long): List<Segment>

    @Query("SELECT * FROM segment WHERE status = 'PENDING' ORDER BY id")
    suspend fun pending(): List<Segment>

    @Query("UPDATE segment SET status = 'SYNCED', uploadedAt = :at WHERE id IN (:ids)")
    suspend fun markSynced(ids: List<Long>, at: Long)

    /** 软删除：只改状态不删文件，真实文件交给 RetentionWorker 延迟清理（可回滚）。 */
    @Query("UPDATE segment SET status = 'DELETED' WHERE id IN (:ids)")
    suspend fun markDeleted(ids: List<Long>)

    // ---- 1.0.4：按天重传 + 保留期清理 ----

    /** 某天（本地日界 [from, to)）的全部段，排除已删。重传入口用。 */
    @Query(
        "SELECT * FROM segment WHERE startOffsetMs >= :from AND startOffsetMs < :to " +
        "AND status != 'DELETED' ORDER BY startOffsetMs"
    )
    suspend fun ofDay(from: Long, to: Long): List<Segment>

    /**
     * 重传：把段改回 PENDING 并清掉 uploadedAt，让 DailyUploadWorker 重新上传。
     *
     * 服务端按 X-Start-Offset-Ms 落库（date = fromtimestamp(start_ms)），
     * 同一 install 的同一 seg_id 走 upsert + 覆盖同名 wav —— 重传幂等，不会串日期。
     */
    @Query("UPDATE segment SET status = 'PENDING', uploadedAt = NULL WHERE id IN (:ids)")
    suspend fun requeue(ids: List<Long>)

    /**
     * 已上传（SYNCED）且上传时间早于 cutoff 的段 —— 保留期清理用。
     *
     * 1.0.4 修：原 RetentionWorker 遍历 pending() 再要求 uploadedAt<cutoff，
     * 而 PENDING 的 uploadedAt 恒为 null → 按天数清理从来没生效过，手机录音无限堆积。
     * PENDING（还没传的）绝不能删——那是唯一副本。
     */
    @Query(
        "SELECT * FROM segment WHERE status = 'SYNCED' " +
        "AND uploadedAt IS NOT NULL AND uploadedAt < :cutoff ORDER BY uploadedAt"
    )
    suspend fun syncedBefore(cutoff: Long): List<Segment>

    /** 所有未删段的时间戳：设置页据此列出「哪些天有本地音频」可重传。 */
    @Query("SELECT startOffsetMs FROM segment WHERE status != 'DELETED' ORDER BY startOffsetMs")
    suspend fun activeOffsets(): List<Long>

    /**
     * 一次性写回 Mac 回流的说话人判定结果。
     *
     * isUser / isSpeech 刻意保留三态（可为 NULL）：null = Mac 尚未回流或无法判定，
     * 与「已判定为 false」语义不同。手机端 UI 依此区分「灰点（未知）」与「非本人」。
     */
    @Query(
        "UPDATE segment SET isUser = :isUser, speechRatio = :ratio,"
            + " speakerLabel = :speakerLabel, isSpeech = :isSpeech WHERE id = :id"
    )
    suspend fun markSpeaker(
        id: Long,
        isUser: Boolean?,
        ratio: Float,
        speakerLabel: String,
        isSpeech: Boolean?,
    )

    @Query("SELECT * FROM segment WHERE sessionId = :sessionId ORDER BY startOffsetMs")
    fun observeOfSession(sessionId: Long): Flow<List<Segment>>

    /** 全部可回放段（有音频文件、时长达标、未软删除），倒序，供状态页按天分组 + 分页。
     *  时长阈值引用共享常量 MIN_SEGMENT_MS：与 VAD 落盘、统计聚合、上传清理同源，
     *  避免改阈值时漏改导致「列表有、统计没有」的静默不一致。
     *  必须排除 DELETED：软删除后文件仍在（保留期后才真删），若这里不过滤，
     *  列表会显示已标记删除的段，点播放时文件可能已经被清掉。 */
    @Query(
        "SELECT * FROM segment WHERE filePath != '' AND durationMs >= $MIN_SEGMENT_MS"
            + " AND status != '$DELETED_STATUS'"
            + " ORDER BY startOffsetMs DESC"
    )
    fun observePlayable(): Flow<List<Segment>>

    /**
     * 按时间区间取段（SQL 层过滤）。
     * 统计聚合曾先 listAll() 把整张 segment 表读进内存再在 Kotlin 里过滤，
     * 一年十几万段后是 O(n) 全表扫描 + 全量对象驻留。改用区间查询只取需要的行。
     */
    @Query(
        "SELECT * FROM segment WHERE startOffsetMs >= :start AND startOffsetMs < :end"
            + " AND filePath != '' AND durationMs >= $MIN_SEGMENT_MS"
            + " AND status != '$DELETED_STATUS'"
            + " ORDER BY startOffsetMs"
    )
    suspend fun listBetween(start: Long, end: Long): List<Segment>

    /** 已软删除但文件仍在的段（供 RetentionWorker 在保留期后真正清理，留出回滚窗口） */
    @Query(
        "SELECT * FROM segment WHERE status = '$DELETED_STATUS' AND filePath != ''"
    )
    suspend fun deletedWithFile(): List<Segment>
}

@Dao
interface ReportDao {
    // REPLACE：同一天重复写入时覆盖（@Insert 遇主键冲突会抛异常 → Worker 无限 retry，实测踩坑）
    @Insert(onConflict = androidx.room.OnConflictStrategy.REPLACE)
    suspend fun upsert(r: DailyReport)

    @Query("SELECT * FROM daily_report WHERE date = :date")
    suspend fun of(date: String): DailyReport?

    @Query("SELECT * FROM daily_report ORDER BY date DESC")
    fun observeAll(): Flow<List<DailyReport>>
}

@Dao
interface StatDao {
    // REPLACE：复合主键 (date, hour) 重复聚合时覆盖（裸 @Insert 冲突抛异常 → Worker 崩溃，实测踩坑）
    @Insert(onConflict = androidx.room.OnConflictStrategy.REPLACE)
    suspend fun upsert(s: StatHour)

    @Query("SELECT * FROM stat_hour WHERE date = :date ORDER BY hour")
    suspend fun ofDate(date: String): List<StatHour>

    @Query("SELECT * FROM stat_hour WHERE date = :date ORDER BY hour")
    fun observeDate(date: String): Flow<List<StatHour>>

    @Query("SELECT COALESCE(SUM(recordSeconds),0) FROM stat_hour WHERE date = :date")
    fun observeDaySeconds(date: String): Flow<Long>

    /** 重算某天统计前先清空，避免「数据从有变无」时残留旧值。 */
    @Query("DELETE FROM stat_hour WHERE date = :date")
    suspend fun clearDate(date: String)
}

@Dao
interface ServiceEventDao {
    @Insert
    suspend fun insert(e: ServiceEvent)

    @Query("SELECT * FROM service_event ORDER BY at DESC LIMIT :limit")
    fun observeRecent(limit: Int = 50): Flow<List<ServiceEvent>>
}
