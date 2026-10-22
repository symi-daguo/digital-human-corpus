package com.symi.digitalhuman.data

import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * 采集时段（2026-09-15 新增，用户要求）：
 *
 * > 「我们的 apk 要允许设置时间段进行采集，因为是长期驻留不一定要持续采集，
 * >   设置非采集时间段不处理即可」
 *
 * 长期驻留的终端不该 7×24 小时录音——夜间、闭店后、无人在场的时段录到的只有
 * 环境噪声，既费电费存储，又给服务端塞一堆无意义音频。这个模块只做一件事：
 * **给定一份时段描述，判断「此刻该不该采集」**。
 *
 * ## 存储格式
 *
 * 一段纯文本，逗号分隔的若干 `HH:MM-HH:MM`：
 *
 * ```
 * ""                            → 全天采集（默认，等同于不启用时段功能）
 * "09:00-12:00"                 → 仅上午
 * "09:00-12:00,14:00-18:00"     → 上午 + 下午
 * "08:30-12:00,13:30-18:00"     → 门店营业时段
 * "22:00-06:00"                 → 跨天（夜间采集）
 * ```
 *
 * 解析刻意宽松：`9:00`、`09:00`、`0900`、`24:00`、中文冒号与全角破折号都收，
 * 分隔符 `,`／`，`／`;`／`；`／空格／换行都收。
 *
 * ## fail-safe 原则
 *
 * **解析失败一律当作「全天」**，绝不当作「全天不采集」。
 * 理由：这个功能的存在是为了省资源，不是为了拦数据。用户手滑打错一个字，
 * 宁可多录一整天，也不能让设备静默地整天不录——后者在长期驻留场景里
 * 几天都不会被发现。
 *
 * ## 语义边界（与「用户暂停」严格区分）
 *
 * - [AppSettings.paused] / `CaptureState.userStopped` = **用户意图**，优先级最高，时段恢复不解开它。
 * - 本时段 = **计划**，只管「什么时候自动开录 / 什么时候自动收尾」。
 * - 两者是「与」关系：用户在时段内点了「停止」→ 仍然停止；时段关 → 无论用户是否点过开始都收尾。
 */
object CaptureWindow {

    /** 全天采集（不启用时段限制）。 */
    const val ALL_DAY = ""

    /** 一天的总分钟数。`24:00` 视作 1440，仅作为区间右端点有意义。 */
    private const val DAY_MIN = 24 * 60

    /** 单条时段解析结果：[startMinute, endMinute)，`start > end` 表示跨天。 */
    private data class Span(val start: Int, val end: Int)

    // ---------- 解析 ----------

    /**
     * 解析时段描述。返回空列表 = 全天（含「输入为空」与「全部片段非法」两种情况）。
     *
     * 不做缓存：调用方（守护服务的 15~30 秒巡检）频率极低，而用户改设置后
     * 必须立刻生效——加缓存反而要处理失效，得不偿失。
     */
    fun parse(spec: String?): List<Pair<Int, Int>> {
        val s = spec?.trim().orEmpty()
        if (s.isEmpty()) return emptyList()
        val out = ArrayList<Pair<Int, Int>>()
        // 逗号 / 分号 / 空白 / 换行都当分隔符，中文标点一并收
        s.split(',', '，', ';', '；', '\n', '\r', '\t', ' ').forEach { raw ->
            val piece = raw.trim()
            if (piece.isEmpty()) return@forEach
            // 破折号家族：ASCII '-'、全角 '－'、波浪 '~〜～'、长破折 '—–'
            val parts = piece.split('-', '－', '~', '〜', '～', '—', '–')
                .map { it.trim() }
                .filter { it.isNotEmpty() }
            if (parts.size != 2) return@forEach
            val start = parseHm(parts[0]) ?: return@forEach
            val end = parseHm(parts[1]) ?: return@forEach
            if (start == end) return@forEach   // 零长区间：忽略
            out += start to end
        }
        return out
    }

    /** 单个 `HH:MM` / `H:MM` / `HHMM` / `H` 解析为「当日第几分钟」。越界返回 null。 */
    private fun parseHm(raw: String): Int? {
        val t = raw.trim()
        if (t.isEmpty()) return null
        val h: Int
        val m: Int
        val colon = t.split(':', '：')
        if (colon.size == 2) {
            h = colon[0].trim().toIntOrNull() ?: return null
            m = colon[1].trim().toIntOrNull() ?: return null
        } else if (t.length == 4 && t.all { it.isDigit() }) {
            h = t.substring(0, 2).toInt()
            m = t.substring(2, 4).toInt()
        } else if (t.all { it.isDigit() }) {
            h = t.toInt()
            m = 0
        } else return null
        if (h !in 0..24 || m !in 0..59) return null
        return (h * 60 + m).coerceIn(0, DAY_MIN)
    }

    // ---------- 判定 ----------

    /**
     * 此刻是否处于采集时段。
     *
     * @param minuteOfDay 当日第几分钟（0~1439），由 [nowMinuteOfDay] 提供；
     *                    显式传参是为了让本函数可单测（不依赖系统时钟）。
     */
    fun isActive(spec: String?, minuteOfDay: Int): Boolean {
        val spans = parse(spec)
        if (spans.isEmpty()) return true          // 空 / 全非法 → 全天（fail-safe）
        val m = ((minuteOfDay % DAY_MIN) + DAY_MIN) % DAY_MIN
        return spans.any { (start, end) ->
            if (start < end) m >= start && m < end     // 同日区间：左闭右开
            else m >= start || m < end                 // 跨天区间（如 22:00-06:00）
        }
    }

    /** 当前时刻是否处于采集时段（读系统时钟）。 */
    fun isActiveNow(spec: String?): Boolean = isActive(spec, nowMinuteOfDay())

    /** 当日第几分钟（0~1439），按设备本地时区。 */
    fun nowMinuteOfDay(): Int {
        val c = Calendar.getInstance()
        return c.get(Calendar.HOUR_OF_DAY) * 60 + c.get(Calendar.MINUTE)
    }

    /**
     * 「到今天结束还有多久出时段」——只在日志与 UI 提示里用，说明下一次自动开录的时刻。
     *
     * @return 距离下一次进入时段的分钟数；全天或无法判断时返回 -1。
     */
    fun minutesUntilActive(spec: String?, minuteOfDay: Int): Int {
        val spans = parse(spec)
        if (spans.isEmpty()) return -1
        for (d in 0 until DAY_MIN) {
            if (isActive(spec, (minuteOfDay + d) % DAY_MIN)) return d
        }
        return -1
    }

    // ---------- 规范化与展示 ----------

    /**
     * 规范化为 `HH:MM-HH:MM,HH:MM-HH:MM`（左闭、逗号分隔、按起始时间升序）。
     * 非法输入返回 [ALL_DAY]（空串）。
     */
    fun normalize(spec: String?): String {
        val spans = parse(spec)
        if (spans.isEmpty()) return ALL_DAY
        return spans.map { Span(it.first, it.second) }
            .sortedBy { it.start }
            .joinToString(",") { "${fmt(it.start)}-${fmt(it.end)}" }
    }

    /**
     * 中文展示：
     * - 空 → 「全天（不限时段）」
     * - 单条全天 `00:00-24:00` → 「全天（不限时段）」
     * - 其余 → 「09:00–12:00、14:00–18:00」
     */
    fun label(spec: String?): String {
        val spans = parse(spec)
        if (spans.isEmpty()) return "全天（不限时段）"
        if (spans.size == 1) {
            val s = spans[0]
            if (s.first == 0 && s.second >= DAY_MIN) return "全天（不限时段）"
        }
        return spans.sortedBy { it.first }
            .joinToString("、") { "${fmt(it.first)}–${fmt(it.second)}" }
    }

    /** 校验用户输入：返回 null = 合法；否则返回中文错误说明（给 UI 直接展示）。 */
    fun validate(spec: String?): String? {
        val raw = spec?.trim().orEmpty()
        if (raw.isEmpty()) return null                      // 空 = 全天，合法
        val pieces = raw.split(',', '，', ';', '；', '\n', '\r', '\t', ' ')
            .map { it.trim() }.filter { it.isNotEmpty() }
        if (pieces.isEmpty()) return null
        for (p in pieces) {
            val parts = p.split('-', '－', '~', '〜', '～', '—', '–')
                .map { it.trim() }.filter { it.isNotEmpty() }
            if (parts.size != 2) return "「$p」格式不对，应形如 09:00-12:00"
            val a = parseHm(parts[0]) ?: return "「${parts[0]}」不是有效时间（应形如 09:00）"
            val b = parseHm(parts[1]) ?: return "「${parts[1]}」不是有效时间（应形如 18:00）"
            if (a == b) return "「$p」的开始与结束时间相同"
        }
        return null
    }

    private fun fmt(min: Int): String {
        val m = min.coerceIn(0, DAY_MIN)
        return "%02d:%02d".format(m / 60, m % 60)
    }

    /** 供日志：把当前时刻格式化为 `HH:mm`。 */
    fun nowHm(): String = SimpleDateFormat("HH:mm", Locale.US).format(Date())
}
