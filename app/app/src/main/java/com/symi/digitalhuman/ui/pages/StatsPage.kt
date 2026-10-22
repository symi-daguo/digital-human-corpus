package com.symi.digitalhuman.ui.pages

import androidx.compose.runtime.getValue
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.symi.digitalhuman.ui.AppViewModel
import com.symi.digitalhuman.ui.root.AdaptiveCards
import com.symi.digitalhuman.ui.theme.Glass
import com.symi.digitalhuman.ui.theme.GlassCard
import com.symi.digitalhuman.ui.theme.InsetCard
import com.symi.digitalhuman.ui.theme.PageHeader
import com.symi.digitalhuman.ui.theme.Pill
import com.symi.digitalhuman.ui.theme.PillTone
import com.symi.digitalhuman.ui.theme.SectionTitle
import com.symi.digitalhuman.ui.theme.StatRow
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

private val dayKeyFmt  = SimpleDateFormat("yyyy-MM-dd", Locale.US)
private val dayLabelFmt = SimpleDateFormat("MM-dd", Locale.US)
private val timeFmt    = SimpleDateFormat("HH:mm", Locale.US)

@Composable
fun StatsPage(wsc: WindowSizeClass, vm: AppViewModel = viewModel()) {
    val stats by vm.todayStats.collectAsStateWithLifecycle()
    val weekSessions by vm.weekSessions.collectAsStateWithLifecycle()
    val weekSegments by vm.weekSegments.collectAsStateWithLifecycle()

    val totalSec = weekSegments.sumOf { it.durationMs / 1000 }
    val totalSessions = weekSessions.size
    val avgSec = if (totalSessions > 0) totalSec / totalSessions else 0

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        PageHeader(
            title = "统计",
            subtitle = "${dayKeyFmt.format(Date())} · 近 7 天语料采集",
        ) {
            Pill(label = "${totalSessions} 次会话", tone = PillTone.Info)
        }
        Spacer(Modifier.height(16.dp))

        AdaptiveCards(
            windowSizeClass = wsc,
            cards = listOf(
                { HourlyChart(stats.map { it.hour to it.recordSeconds }) },
                { WeekSummary(weekSessions, weekSegments) },
                { AverageCard(totalSec, totalSessions, avgSec) },
                { SessionList(weekSessions) },
            ),
        )
        Spacer(Modifier.height(8.dp))
    }
}

/** 今日 24 小时录音分布柱状图（24 根全画，0 也画淡色，加基线 + 当前小时高亮） */
@Composable
private fun HourlyChart(hourSeconds: List<Pair<Int, Long>>) {
    val seconds = IntArray(24) { h -> hourSeconds.firstOrNull { it.first == h }?.second?.toInt() ?: 0 }
    val max = seconds.max().coerceAtLeast(1)
    val currentHour = remember { Calendar.getInstance().get(Calendar.HOUR_OF_DAY) }
    val totalSec = seconds.sum()

    GlassCard {
        SectionTitle(
            title = "今日录音分布",
            subtitle = "共 ${formatDuration(totalSec.toLong())}",
        )
        Spacer(Modifier.height(12.dp))

        Canvas(Modifier.fillMaxWidth().height(140.dp)) {
            val gap = 3.dp.toPx()
            val barW = (size.width - gap * 23) / 24f
            val baselineY = size.height * 0.92f
            // 顶部虚线基线（目标 1h）
            drawLine(
                color = Glass.Stroke,
                start = Offset(0f, baselineY),
                end = Offset(size.width, baselineY),
                strokeWidth = 1.dp.toPx(),
            )
            for (h in 0 until 24) {
                val v = seconds[h] / max.toFloat()
                val isCurrent = h == currentHour
                val color = when {
                    seconds[h] <= 0 -> Glass.Surface2
                    isCurrent       -> Glass.Teal
                    else            -> Glass.Teal.copy(alpha = 0.55f)
                }
                val x = h * (barW + gap)
                val barH = size.height * v * 0.88f
                if (seconds[h] > 0) {
                    drawRoundRect(
                        color = color,
                        topLeft = Offset(x, baselineY - barH),
                        size = Size(barW, barH),
                        cornerRadius = CornerRadius(3.dp.toPx()),
                    )
                } else {
                    // 空小时画细底线表示占位
                    drawRoundRect(
                        color = color,
                        topLeft = Offset(x, baselineY - 2.dp.toPx()),
                        size = Size(barW, 2.dp.toPx()),
                        cornerRadius = CornerRadius(1.dp.toPx()),
                    )
                }
                // 当前小时描边
                if (isCurrent) {
                    drawRoundRect(
                        color = Glass.Teal,
                        topLeft = Offset(x, baselineY - barH),
                        size = Size(barW, barH),
                        cornerRadius = CornerRadius(3.dp.toPx()),
                        style = Stroke(width = 1.5.dp.toPx()),
                    )
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("0 时",  style = MaterialTheme.typography.labelSmall, color = Glass.TextFaint)
            Text("6 时",  style = MaterialTheme.typography.labelSmall, color = Glass.TextFaint)
            Text("12 时", style = MaterialTheme.typography.labelSmall, color = Glass.TextFaint)
            Text("18 时", style = MaterialTheme.typography.labelSmall, color = Glass.TextFaint)
            Text("23 时", style = MaterialTheme.typography.labelSmall, color = Glass.TextFaint)
        }
    }
}

/** 7 天汇总（正向时间序列，今天在最右） */
@Composable
private fun WeekSummary(
    weekSessions: List<com.symi.digitalhuman.data.Session>,
    weekSegments: List<com.symi.digitalhuman.data.Segment>,
) {
    val byDay = weekSessions.groupBy { dayKeyFmt.format(Date(it.startedAt)) }
    val secByDay = weekSegments.groupBy { dayKeyFmt.format(Date(it.startOffsetMs)) }
        .mapValues { (_, segs) -> segs.sumOf { it.durationMs / 1000 } }
    val today = dayKeyFmt.format(Date())
    // 正向排序：6 天前 → 今天
    val keys = (6 downTo 0).map { off ->
        Calendar.getInstance().apply { add(Calendar.DAY_OF_YEAR, -off) }.time
    }.map { dayKeyFmt.format(it) }

    GlassCard {
        SectionTitle("近 7 天汇总", accent = Glass.Indigo)
        Spacer(Modifier.height(8.dp))
        InsetCard {
            keys.forEachIndexed { idx, key ->
                val sessions = byDay[key]?.size ?: 0
                val secs = secByDay[key] ?: 0
                val isToday = key == today
                Row(
                    modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Box(
                        Modifier
                            .size(6.dp)
                            .clip(CircleShape)
                            .background(if (isToday) Glass.Teal else Glass.TextFaint),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        if (isToday) "今天" else dayLabelFmt.format(dayKeyFmt.parse(key) ?: Date()),
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.Text,
                        modifier = Modifier.weight(1f),
                    )
                    Text(
                        "$sessions 次",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.TextDim,
                    )
                    Spacer(Modifier.width(12.dp))
                    Text(
                        formatDuration(secs),
                        style = MaterialTheme.typography.titleSmall,
                        color = if (secs > 0) Glass.Teal else Glass.TextFaint,
                    )
                }
                if (idx < keys.size - 1) {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .height(1.dp)
                            .background(Glass.Stroke.copy(alpha = 0.5f)),
                    )
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        StatRow(
            "合计",
            "${weekSessions.size} 次会话",
            Glass.Teal,
            helper = "近 7 天总录音时长 " + formatDuration(weekSegments.sumOf { it.durationMs / 1000 }),
        )
    }
}

/** 平均值卡：只讲「平均」，不再重复上面已展示的合计数据（避免与 WeekSummary 的「合计」重复） */
@Composable
private fun AverageCard(totalSec: Long, totalSessions: Int, avgSec: Long) {
    val days = 7
    // 不足 7 次会话时整除会显示「0 次」，有误导性；非整数改显一位小数（如 0.7 次）
    val avgDay = totalSessions / days.toDouble()
    val avgDayStr = if (avgDay == Math.floor(avgDay)) "${avgDay.toInt()}"
        else String.format(Locale.US, "%.1f", avgDay)
    GlassCard {
        SectionTitle("平均水平", accent = Glass.Indigo)
        Spacer(Modifier.height(8.dp))
        StatRow("每天会话", "$avgDayStr 次")
        StatRow("每天录音", formatDuration(totalSec / days), Glass.Teal)
        StatRow("单次会话", if (avgSec > 0) formatDuration(avgSec) else "—", Glass.Indigo)
    }
}

/** 会话记录（近 7 天） */
@Composable
private fun SessionList(weekSessions: List<com.symi.digitalhuman.data.Session>) {
    GlassCard {
        SectionTitle("会话记录（近 7 天）", accent = Glass.Amber)
        Spacer(Modifier.height(8.dp))
        if (weekSessions.isEmpty()) {
            Text(
                "暂无会话",
                style = MaterialTheme.typography.bodyMedium,
                color = Glass.TextDim,
            )
        } else {
            InsetCard {
                // 先 take 再遍历：原来在循环体里又写了一次 weekSessions.take(30).lastIndex，
                // 等于每行都重建一遍列表，列表一长就白扔几十个临时对象。
                val shown = weekSessions.take(30)
                shown.forEachIndexed { idx, s ->
                    val dur = s.endedAt?.let { formatDuration((it - s.startedAt) / 1000) } ?: "进行中"
                    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text(
                                "${timeFmt.format(Date(s.startedAt))} · ${triggerLabel(s.triggerType)}",
                                style = MaterialTheme.typography.titleSmall,
                                color = Glass.Text,
                            )
                            Text(
                                dur,
                                style = MaterialTheme.typography.titleSmall,
                                color = if (s.endedAt == null) Glass.Amber else Glass.Teal,
                            )
                        }
                        if (s.triggerDetail.isNotBlank()) {
                            Text(
                                s.triggerDetail,
                                style = MaterialTheme.typography.bodySmall,
                                color = Glass.TextFaint,
                            )
                        }
                    }
                    if (idx < shown.lastIndex) {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(1.dp)
                                .background(Glass.Stroke.copy(alpha = 0.5f)),
                        )
                    }
                }
            }
        }
    }
}

private fun triggerLabel(t: String) = when (t) {
    "LAN"    -> "局域网"
    "BT"     -> "蓝牙"
    "AUTO"   -> "自动"
    "MANUAL" -> "手动"
    "RECOVER"-> "恢复"
    "ALWAYS" -> "常驻"
    else     -> t
}
