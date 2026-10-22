package com.symi.digitalhuman.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(tableName = "session")
data class Session(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val startedAt: Long,
    val endedAt: Long? = null,
    val triggerType: String,        // LAN / BT / MANUAL / ALWAYS / RECOVER
    val triggerDetail: String = "",
    val endReason: String? = null,  // TRIGGER_GONE / SERVICE_DIED / MANUAL
    // 保留字段：当前无读取方。会话的同步状态实际由 segment.status 表达，
    // 删它需要对 session 表做 DROP COLUMN（需 SQLite 3.35+/Android 12，而 minSdk=29），
    // 只能整表重建，风险远大于收益，故保留。
    val synced: Boolean = false,
)

@Entity(
    tableName = "segment",
    // sessionId 用于按会话取段；status 用于待上传/保留清理；startOffsetMs 用于按天区间查询。
    // 三者都是高频过滤列，缺索引时一年十几万段会退化成全表扫描。
    indices = [
        Index("sessionId"),
        Index("status"),
        Index("startOffsetMs"),
    ],
)
data class Segment(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val sessionId: Long,
    val filePath: String,
    val startOffsetMs: Long,        // 段起始时间（epoch ms）
    val durationMs: Long,
    val hasSpeech: Boolean = true,  // VAD 判定；false 只记行不写文件
    val status: String = SegmentStatus.RECORDING, // RECORDING/PENDING/SYNCED/DELETED
    val isUser: Boolean? = null,    // Mac 声纹回流；null=未判定/非本人
    val speechRatio: Float = 0f,
    val uploadedAt: Long? = null,
    /** Mac 说话人聚类结果：本人 / 甲 / 乙 / 丙…；空串=尚未回流。 */
    val speakerLabel: String = "",
    /**
     * 是否人声（AudioTagging 判定）：true=人声，false=音乐/噪声/静默，null=尚未回流。
     * 三态不可省——null 表示「Mac 还没处理过这段」，与「已判定为非人声」语义不同。
     */
    val isSpeech: Boolean? = null,
)

object SegmentStatus {
    const val RECORDING = "RECORDING"
    const val PENDING = "PENDING"
    const val SYNCED = "SYNCED"
    const val DELETED = "DELETED"
}

@Entity(tableName = "daily_report")
data class DailyReport(
    @PrimaryKey val date: String,   // yyyy-MM-dd
    val rawTranscript: String = "",
    val cleanTranscript: String = "",
    val summary: String = "",
    val generatedAt: Long? = null,
    val ready: Boolean = false,
    /**
     * 总结卡片（JSON 字符串：[{"title":"今日回顾","body":"…"}]）。
     * 由 Mac 端解析【栏目】后下发；为空表示模型未按格式输出，UI 回退成整段文本。
     */
    val summaryCards: String = "",
)

/** 总结卡片：标题 + 正文（正文保留内部换行，UI 原样展示） */
data class SummaryCard(val title: String, val body: String)

object SummaryCards {
    private val json = kotlinx.serialization.json.Json

    fun encode(cards: List<SummaryCard>): String = runCatching {
        json.encodeToString(
            kotlinx.serialization.json.JsonArray.serializer(),
            kotlinx.serialization.json.buildJsonArray {
                cards.forEach { c ->
                    add(kotlinx.serialization.json.buildJsonObject {
                        put("title", kotlinx.serialization.json.JsonPrimitive(c.title))
                        put("body", kotlinx.serialization.json.JsonPrimitive(c.body))
                    })
                }
            },
        )
    }.getOrDefault("")

    fun decode(raw: String): List<SummaryCard> = runCatching {
        val arr = json.parseToJsonElement(raw) as? kotlinx.serialization.json.JsonArray ?: return emptyList()
        arr.mapNotNull { e ->
            val o = e as? kotlinx.serialization.json.JsonObject ?: return@mapNotNull null
            val t = o["title"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content } ?: ""
            val b = o["body"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content } ?: ""
            if (t.isBlank() && b.isBlank()) null else SummaryCard(t, b)
        }
    }.getOrDefault(emptyList())
}

@Entity(tableName = "stat_hour", primaryKeys = ["date", "hour"])
data class StatHour(
    val date: String,               // yyyy-MM-dd
    val hour: Int,                  // 0-23
    val triggerCount: Int = 0,
    val recordSeconds: Long = 0,
)

@Entity(tableName = "service_event")
data class ServiceEvent(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val at: Long = System.currentTimeMillis(),
    val type: String,               // GUARD_STARTED / CAPTURE_DIED / CAPTURE_RECOVERED / PROBE_UP / PROBE_DOWN / ...
    val detail: String = "",
)
