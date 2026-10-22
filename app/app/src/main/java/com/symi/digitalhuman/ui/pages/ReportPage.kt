package com.symi.digitalhuman.ui.pages

import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.getValue
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SecondaryTabRow
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.symi.digitalhuman.data.DailyReport
import com.symi.digitalhuman.data.SummaryCard
import com.symi.digitalhuman.data.SummaryCards
import com.symi.digitalhuman.ui.AppViewModel
import com.symi.digitalhuman.ui.root.AdaptiveCards
import com.symi.digitalhuman.ui.theme.DateField
import com.symi.digitalhuman.ui.theme.Glass
import com.symi.digitalhuman.ui.theme.GlassCard
import com.symi.digitalhuman.ui.theme.PageHeader
import com.symi.digitalhuman.ui.theme.Pill
import com.symi.digitalhuman.ui.theme.PillTone
import com.symi.digitalhuman.ui.theme.SectionTitle
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt

private val dateFmt = SimpleDateFormat("yyyy-MM-dd (EEEE)", Locale.CHINA)
private val timeFmt = SimpleDateFormat("MM-dd HH:mm", Locale.US)

/** 本人标签：与 Mac 端 config.SELF_LABEL 一致，用于时间线高亮与「只看本人」筛选 */
private const val SELF_LABEL = "本人"

@Composable
fun ReportPage(wsc: WindowSizeClass, vm: AppViewModel = viewModel()) {
    // 打开日报页就主动同步一次服务端的处理结果（2026-09-21）。
    // Room 平时只由 DailyUploadWorker 的周期任务写入（准实时 15-20 分钟一次），
    // 用户讲完一段、打开页面看不到新内容，就会判断成「上传了但没处理」。
    // 这里按今天拉一次，服务端已处理完的部分立刻可见；跨天时 key 变化会再触发。
    val today = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US)
        .format(java.util.Date())
    LaunchedEffect(today) { vm.syncReportNow(today) }
    val reports by vm.reports.collectAsStateWithLifecycle()
    val settings by vm.settings.collectAsStateWithLifecycle()
    var selectedDate by rememberSaveable { mutableStateOf<String?>(null) }
    var tab by rememberSaveable { mutableIntStateOf(0) }

    if (reports.isEmpty()) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            // 与其它三页保持同一头部结构，空态仅替换正文区
            PageHeader("日报", "Mac 批量处理后自动生成")
            Spacer(Modifier.height(16.dp))
            EmptyReportState(processMinute = settings.processMinuteOfDay)
        }
        return
    }

    val current = reports.firstOrNull { it.date == selectedDate } ?: reports.first()
    val headerSubtitle = "Mac 批量处理后自动生成 · 共 ${reports.size} 天"

    // 展开屏：左右双栏（左=日期选择，右=正文）；其余：纵向堆叠
    if (wsc.widthSizeClass == WindowWidthSizeClass.Expanded) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            PageHeader("日报", headerSubtitle)
            Spacer(Modifier.height(16.dp))
            Row(
                modifier = Modifier.fillMaxWidth().weight(1f),
                horizontalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                Column(modifier = Modifier.weight(0.32f).fillMaxHeight()) {
                    DatePickerCard(reports, current.date) { selectedDate = it }
                }
                Column(modifier = Modifier.weight(0.68f).fillMaxHeight()) {
                    BodyCard(current, tab, Modifier.fillMaxHeight(), contentScrollable = true) { tab = it }
                }
            }
        }
    } else {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            PageHeader("日报", headerSubtitle)
            Spacer(Modifier.height(16.dp))
            AdaptiveCards(
                windowSizeClass = wsc,
                cards = listOf(
                    { DatePickerCard(reports, current.date) { selectedDate = it } },
                    { BodyCard(current, tab) { tab = it } },
                ),
            )
            Spacer(Modifier.height(8.dp))
        }
    }
}

/** 空态：等生成中提示（占满头部以下的剩余空间，居中展示） */
@Composable
private fun EmptyReportState(processMinute: Int) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(vertical = 32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            "还没有日报",
            style = MaterialTheme.typography.headlineSmall,
            color = Glass.Text,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "每天 ${formatMinuteOfDay(processMinute)} Mac 批量处理完成后，\n原始转写 / 去口气干净稿 / 总结会同步到这里。",
            style = MaterialTheme.typography.bodyMedium,
            color = Glass.TextDim,
        )
    }
}

/**
 * 日期选择卡：Material3 日历（跨月跳转 + 只可选有数据的日期）为主入口，
 * 下面一行横向胶囊保留作最近几天的快捷切换——日历是标准组件，快捷条是效率补充。
 */
@Composable
private fun DatePickerCard(reports: List<DailyReport>, currentDate: String, onSelect: (String) -> Unit) {
    GlassCard(modifier = Modifier.fillMaxWidth()) {
        SectionTitle("选择日期", accent = Glass.Indigo)
        Spacer(Modifier.height(10.dp))

        DateField(
            currentDate = currentDate,
            availableDates = remember(reports) { reports.map { it.date }.toSet() },
            onSelect = onSelect,
            hint = "共 ${reports.size} 天",
        )

        Spacer(Modifier.height(12.dp))
        // 横向滚动日期胶囊（不再限制 6 天）；当天的 chip 用「今天」字样更易辨识
        val today = remember { SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date()) }
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            reports.forEach { r ->
                val sel = r.date == currentDate
                DateChip(
                    label = if (r.date == today) "今天" else r.date.substring(5),
                    ready = r.ready,
                    selected = sel,
                ) { onSelect(r.date) }
            }
        }

        if (reports.size > 5) {
            Spacer(Modifier.height(8.dp))
            Text(
                "共 ${reports.size} 天 · 左右滑动查看",
                style = MaterialTheme.typography.labelSmall,
                color = Glass.TextFaint,
            )
        }
    }
}

@Composable
private fun DateChip(label: String, ready: Boolean, selected: Boolean, onClick: () -> Unit) {
    val container = if (selected) Glass.Teal.copy(alpha = 0.16f) else Glass.Surface2
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(10.dp))
            .background(container)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelLarge,
            color = when {
                selected -> Glass.Teal
                !ready   -> Glass.TextFaint
                else     -> Glass.Text
            },
        )
    }
}

/** 逐字稿行：[HH:MM] [说话人] 正文 → 结构化行，供时间线渲染 */
private data class TimelineRow(val minute: Int, val speaker: String, val text: String)

private val LINE_RE = Regex("""^\s*\[(\d{2}:\d{2})\]\s*(?:\[([^\]]*)\]\s*)?(.*)$""")

// 总结【回顾】卡正文行：「【HH:MM-HH:MM】要点」（Mac 端代码注入的时间头，非模型生成）
private val SUMMARY_BLOCK_RE = Regex("""^【(\d{2}):(\d{2})-(\d{2}:\d{2})】\s*(.*)$""")

/**
 * 总结时间轴数据源（2026-09-08 本人/他人分离版）：
 * 【我的动态】= 机主本人发言（speaker=SELF_LABEL，时间轴上青色高亮），
 * 【他人动态】= 其他人的发言（多人合并，speaker=「他人」）。
 * 旧版总结只有【今日回顾】卡：speaker 留空，不高亮但内容不丢。
 */
private val SUMMARY_TIMELINE_CARDS = linkedMapOf(
    "我的动态" to SELF_LABEL,
    "他人动态" to "他人",
    "今日回顾" to "",
)

/**
 * 把【我的动态】【他人动态】卡（旧版为【今日回顾】）的时间段要点解析成时间线行
 * （与原始稿/干净稿同一套 TimelineRow/DayTimeline/TimelineBody，三版时间轴与顺序一致）。
 * 解析不出时间行（旧版总结/格式变化）时返回空列表，调用方回退卡片视图。
 */
private fun parseSummaryRows(cards: List<SummaryCard>): List<TimelineRow> {
    val out = ArrayList<TimelineRow>()
    var matched = false
    for ((title, speaker) in SUMMARY_TIMELINE_CARDS) {
        val body = cards.firstOrNull { it.title == title }?.body ?: continue
        matched = true
        for (line in body.lineSequence()) {
            val m = SUMMARY_BLOCK_RE.find(line.trim()) ?: continue
            val hh = m.groupValues[1].toInt()
            val mm = m.groupValues[2].toInt()
            val text = m.groupValues[4].trim()
            if (text.isNotEmpty()) out.add(TimelineRow(hh * 60 + mm, speaker, text))
        }
    }
    if (!matched) return emptyList()
    // 与原始稿/干净稿同序：最新在上
    return out.sortedByDescending { it.minute }
}

/**
 * 把「每行带 [HH:MM] 时间戳」的逐字稿解析成时间线行。
 *
 * 解析失败/没有时间戳的旧稿一律降级：整段按纯文本展示（见 BodyCard 的 rows 为空分支），
 * 绝不因为格式问题吞掉内容。续行（同一时间块内的多行）沿用上一行的时间戳。
 */
private fun parseRows(text: String): List<TimelineRow> {
    if (text.isBlank()) return emptyList()
    val out = ArrayList<TimelineRow>()
    var lastMinute = -1
    for (line in text.lineSequence()) {
        val m = LINE_RE.find(line)
        if (m != null) {
            val (hh, mm) = m.groupValues[1].split(":")
            lastMinute = hh.toInt() * 60 + mm.toInt()
            val body = m.groupValues[3].trim()
            if (body.isNotEmpty()) out.add(TimelineRow(lastMinute, m.groupValues[2].trim(), body))
        } else if (line.isNotBlank()) {
            if (lastMinute >= 0) out.add(TimelineRow(lastMinute, "", line.trim()))
        }
    }
    return out
}

private fun fmtMinute(m: Int) = "%02d:%02d".format(m / 60, m % 60)

/**
 * 全天时间滑轨：把每个语音段画成一根刻度，密度即「什么时候在说话」。
 *
 * 为什么不用文字前缀时间戳：文字里的时间只能读不能操作，几十分钟的一段话
 * 无法定位。滑轨可直接拖动/点击跳到任意时刻，本人发言高亮，一眼看出
 * 「今天什么时候说过话、跟谁说的」。
 *
 * 吸附：拖动/点击的目标时刻自动吸附到最近的有内容刻度——滑轨上空白区段
 * （没说过话的时段）不可停留，松手必落在有内容的位置。
 */
@Composable
private fun DayTimeline(
    rows: List<TimelineRow>,
    focusMinute: Int?,
    showSelfOnly: Boolean,
    onSeek: (Int) -> Unit,
) {
    if (rows.isEmpty()) return
    val minMin = rows.minOf { it.minute }
    val maxMin = maxOf(rows.maxOf { it.minute }, minMin + 1)
    var dragMinute by remember { mutableStateOf<Int?>(null) }

    // 吸附目标集：与正文列表同口径（筛选后）的去重时刻，拖动结束吸附到最近一条
    val snapMinutes = remember(rows, showSelfOnly) {
        (if (showSelfOnly) rows.filter { it.speaker == SELF_LABEL } else rows)
            .map { it.minute }.distinct().sorted()
    }
    fun snap(minute: Int): Int =
        snapMinutes.minByOrNull { kotlin.math.abs(it - minute) } ?: minute

    val railColor = Glass.Surface2
    val selfColor = Glass.Teal
    val otherColor = Glass.Indigo.copy(alpha = 0.55f)
    val focusColor = Glass.Amber

    Column {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "时间线 · 拖动或点击定位",
                style = MaterialTheme.typography.labelSmall,
                color = Glass.TextFaint,
                modifier = Modifier.weight(1f),
            )
            Text(
                fmtMinute(dragMinute ?: focusMinute ?: minMin),
                style = MaterialTheme.typography.labelLarge,
                color = if (dragMinute != null) focusColor else Glass.TextDim,
            )
        }
        Spacer(Modifier.height(6.dp))
        Canvas(
            modifier = Modifier
                .fillMaxWidth()
                .height(44.dp)
                .pointerInput(rows, minMin, maxMin, snapMinutes) {
                    detectTapGestures { offset -> onSeek(snap(minuteAt(offset.x, size.width, minMin, maxMin))) }
                }
                .pointerInput(rows, minMin, maxMin, snapMinutes) {
                    detectDragGestures(
                        onDragEnd = { dragMinute = null },
                        onDragCancel = { dragMinute = null },
                    ) { change, _ ->
                        val m = snap(minuteAt(change.position.x, size.width, minMin, maxMin))
                        dragMinute = m
                        onSeek(m)
                    }
                },
        ) {
            val w = size.width
            val h = size.height
            val railY = h - 10.dp.toPx()
            drawLine(railColor, Offset(0f, railY), Offset(w, railY), strokeWidth = 2.dp.toPx())
            // 每个语音段一根刻度；本人用 Teal，其余用 Indigo
            for (r in rows) {
                val x = ((r.minute - minMin).toFloat() / (maxMin - minMin) * w).coerceIn(0f, w)
                val isSelf = r.speaker == SELF_LABEL
                if (showSelfOnly && !isSelf) continue
                drawLine(
                    color = if (isSelf) selfColor else otherColor,
                    start = Offset(x, 6.dp.toPx()),
                    end = Offset(x, railY),
                    strokeWidth = 2.dp.toPx(),
                )
            }
            // 当前位置指示线
            val focus = dragMinute ?: focusMinute ?: minMin
            val fx = ((focus - minMin).toFloat() / (maxMin - minMin) * w).coerceIn(0f, w)
            drawLine(focusColor, Offset(fx, 0f), Offset(fx, h), strokeWidth = 2.dp.toPx())
            drawCircle(focusColor, radius = 4.dp.toPx(), center = Offset(fx, railY))
        }
    }
}

private fun minuteAt(x: Float, width: Int, minMin: Int, maxMin: Int): Int {
    val ratio = (x / width).coerceIn(0f, 1f)
    return minMin + (ratio * (maxMin - minMin)).roundToInt()
}

/** 时间线正文：左侧时间槽 + 说话人标签 + 正文；时间只在时段变化处标注。
 *  行序为**最新在最上**（日报打开即见最新内容，无需滚到底）；
 *  定位目标从「第一个不早于目标」改为「第一条不晚于目标」的行。 */
@Composable
private fun TimelineBody(rows: List<TimelineRow>, showSelfOnly: Boolean, focusMinute: Int?) {
    val visible = remember(rows, showSelfOnly) {
        val base = if (showSelfOnly) rows.filter { it.speaker == SELF_LABEL } else rows
        base.sortedByDescending { it.minute }
    }
    if (visible.isEmpty()) {
        Text(
            if (showSelfOnly) "今天没有识别到本人发言" else "暂无内容",
            style = MaterialTheme.typography.bodyMedium,
            color = Glass.TextDim,
        )
        return
    }
    val listState = rememberLazyListState()
    // 定位：拖动/点击时间线 → 降序列表中滚到第一条不晚于目标时刻的行（即该时段的内容）
    LaunchedEffect(focusMinute, showSelfOnly) {
        val target = focusMinute ?: return@LaunchedEffect
        val idx = visible.indexOfFirst { it.minute <= target }.takeIf { it >= 0 } ?: 0
        listState.animateScrollToItem(idx)
    }
    LazyColumn(
        state = listState,
        modifier = Modifier.fillMaxWidth().heightIn(min = 200.dp, max = 380.dp),
    ) {
        itemsIndexed(visible, key = { i, _ -> i }) { i, row ->
            val prev = visible.getOrNull(i - 1)
            // 时间只在「换时段」时标：降序列表中与上一条（更晚）间隔 ≥5 分钟
            val showTime = prev == null || prev.minute - row.minute >= 5
            Row(modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
                Column(
                    modifier = Modifier.width(52.dp),
                    horizontalAlignment = Alignment.End,
                ) {
                    if (showTime) {
                        Text(
                            fmtMinute(row.minute),
                            style = MaterialTheme.typography.labelMedium,
                            color = if (row.speaker == SELF_LABEL) Glass.Teal else Glass.TextFaint,
                        )
                    }
                }
                Spacer(Modifier.width(10.dp))
                // 时间轴竖线 + 节点
                Box(
                    modifier = Modifier
                        .width(12.dp)
                        .height(if (showTime) 22.dp else 14.dp),
                    contentAlignment = Alignment.TopCenter,
                ) {
                    Box(
                        Modifier
                            .width(2.dp)
                            .fillMaxHeight()
                            .background(Glass.Surface2),
                    )
                    Box(
                        Modifier
                            .size(if (showTime) 8.dp else 5.dp)
                            .clip(CircleShape)
                            .background(if (row.speaker == SELF_LABEL) Glass.Teal else Glass.Indigo),
                    )
                }
                Spacer(Modifier.width(10.dp))
                Column(modifier = Modifier.weight(1f)) {
                    if (row.speaker.isNotBlank()) {
                        Text(
                            row.speaker,
                            style = MaterialTheme.typography.labelMedium,
                            color = if (row.speaker == SELF_LABEL) Glass.Teal else Glass.TextDim,
                        )
                    }
                    Text(
                        row.text,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.Text,
                    )
                }
            }
        }
    }
}

/**
 * 总结卡片视图：卡片为空（模型未按栏目输出）时回退为整段文本，内容不丢。
 *
 * 卡片列不再限制 380dp 内滚（2026-09-04）：【回顾】卡是全天时间轴叙事，
 * 单卡就能占满 380dp 的小盒子，后面四张卡全被压进内滚区看不见，用户以为界面坏了。
 * 纵向布局下外层页面本身可滚动，让卡片自然撑开；Expanded 双栏由调用方包一层滚动。
 */
@Composable
private fun SummaryCardsView(cards: List<SummaryCard>, fallback: String) {
    if (cards.isEmpty()) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 200.dp, max = 380.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Text(
                fallback.ifBlank { "暂无内容" },
                style = MaterialTheme.typography.bodyLarge,
                color = Glass.Text,
            )
        }
        return
    }
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        cards.forEach { c ->
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(Glass.Surface2)
                    .padding(horizontal = 12.dp, vertical = 10.dp),
            ) {
                Text(
                    c.title,
                    style = MaterialTheme.typography.titleSmall,
                    color = Glass.Teal,
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    c.body,
                    style = MaterialTheme.typography.bodyMedium,
                    color = Glass.Text,
                )
            }
        }
    }
}

/** 总结 Tab 正文：时间轴 + 降序时间段行（与原始稿/干净稿同交互），其余栏目卡殿后 */
@Composable
private fun SummaryTabContent(
    summaryRows: List<TimelineRow>,
    cards: List<SummaryCard>,
    restCards: List<SummaryCard>,
    fallback: String,
) {
    if (summaryRows.isEmpty()) {
        // 旧版总结（回顾卡没有时间行）：整卡回退，内容不丢
        SummaryCardsView(cards = cards, fallback = fallback)
        return
    }
    // (目标时刻, 序号)：带序号才能在「再次点同一时刻」时也触发一次滚动
    var focus by remember { mutableStateOf<Pair<Int, Int>?>(null) }
    DayTimeline(
        rows = summaryRows,
        focusMinute = focus?.first,
        showSelfOnly = false,
        onSeek = { m -> focus = m to ((focus?.second ?: 0) + 1) },
    )
    Spacer(Modifier.height(8.dp))
    TimelineBody(rows = summaryRows, showSelfOnly = false, focusMinute = focus?.first)
    if (restCards.isNotEmpty()) {
        Spacer(Modifier.height(14.dp))
        SummaryCardsView(cards = restCards, fallback = "")
    }
}

/** 正文卡片：Tab 切换 + 时间线正文（解决嵌套滚动：外层 Expanded 不滚，内层独立滚）
 *  contentScrollable：Expanded 双栏时右栏高度固定，总结卡片需要内滚兜底；
 *  纵向布局传 false（默认），卡片撑开由外层页面滚动。 */
/**
 * 从时间轴行提取说话人筛选选项（2026-09-08）：本人置顶，其余按发言数降序。
 * 只要当天有该角色的录音行就会出现在筛选里（与 Mac 端 /api/report speakers 名单同口径）。
 */
private fun speakerOptions(rows: List<TimelineRow>): List<Pair<String, Int>> =
    rows.filter { it.speaker.isNotBlank() }
        .groupingBy { it.speaker }
        .eachCount()
        .entries
        .sortedWith(
            compareByDescending<Map.Entry<String, Int>> { it.key == SELF_LABEL }
                .thenByDescending { it.value }
        )
        .map { it.key to it.value }

/** 说话人筛选 chips：横滑一行，「全部」+ 每个角色（含段数）；再点已选角色取消筛选。 */
@Composable
private fun SpeakerFilterRow(
    options: List<Pair<String, Int>>,
    total: Int,
    selected: String?,
    onSelect: (String?) -> Unit,
) {
    if (options.isEmpty()) return
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        FilterChip(
            label = "全部 $total",
            selected = selected == null,
            onClick = { onSelect(null) },
        )
        options.forEach { (label, count) ->
            FilterChip(
                label = "$label $count",
                selected = selected == label,
                onClick = { onSelect(if (selected == label) null else label) },
                accent = if (label == SELF_LABEL) Glass.Teal else Glass.Indigo,
            )
        }
    }
}

@Composable
private fun BodyCard(
    report: DailyReport,
    tab: Int,
    modifier: Modifier = Modifier,
    contentScrollable: Boolean = false,
    onTabChange: (Int) -> Unit,
) {
    val tabs = listOf("原始转写", "干净稿", "总结")
    // 说话人筛选（2026-09-08）：跨 Tab 记忆；切 Tab 后若无该角色则显示空态，再点一次取消即可
    var speakerFilter by remember(report.date) { mutableStateOf<String?>(null) }
    GlassCard(modifier = modifier) {
        // M3 1.4 SecondaryTabRow 默认 indicator 自适应 selectedTabIndex，
        // 默认 Teal 主题色与 App 强调色一致；containerColor 透明化后融入卡片。
        SecondaryTabRow(
            selectedTabIndex = tab,
            containerColor = Color.Transparent,
            contentColor = Glass.Teal,
        ) {
            tabs.forEachIndexed { i, t ->
                Tab(
                    selected = tab == i,
                    onClick = { onTabChange(i) },
                    text = {
                        Text(
                            t,
                            style = MaterialTheme.typography.titleSmall,
                            color = if (tab == i) Glass.Teal else Glass.TextDim,
                        )
                    },
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                dateFmt.format(safeParse(report.date)),
                style = MaterialTheme.typography.bodyMedium,
                color = Glass.TextDim,
            )
            if (!report.ready) {
                Spacer(Modifier.width(8.dp))
                Pill(label = "处理中", tone = PillTone.Pending, showDot = false)
            }
        }
        report.generatedAt?.let {
            Text(
                "生成于 ${timeFmt.format(Date(it))}",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextFaint,
            )
        }
        Spacer(Modifier.height(10.dp))
        if (tab == 2) {
            val cards = remember(report.summaryCards) { SummaryCards.decode(report.summaryCards) }
            // 总结与原始稿/干净稿同形态：同一套可滑动时间轴 + 按时间降序的行；
            // 其余栏目卡（关键原话/待办/金句/状态）跟在正文后面。
            val summaryRows = remember(cards) { parseSummaryRows(cards) }
            // 时间轴卡已渲染成行，只把其余栏目卡（关键原话/待办/金句/状态）殿后展示
            val restCards = remember(cards) {
                cards.filter { it.title !in SUMMARY_TIMELINE_CARDS.keys }
            }
            // 总结 Tab 同样支持按说话人筛选（本人/他人）
            val sumOptions = remember(summaryRows) { speakerOptions(summaryRows) }
            val visibleSummary = remember(summaryRows, speakerFilter) {
                if (speakerFilter == null) summaryRows
                else summaryRows.filter { it.speaker == speakerFilter }
            }
            SpeakerFilterRow(sumOptions, summaryRows.size, speakerFilter) { speakerFilter = it }
            Spacer(Modifier.height(8.dp))
            if (contentScrollable) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 200.dp)
                        .verticalScroll(rememberScrollState()),
                ) {
                    SummaryTabContent(visibleSummary, cards, restCards, report.summary)
                }
            } else {
                SummaryTabContent(visibleSummary, cards, restCards, report.summary)
            }
        } else {
            val text = if (tab == 0) report.rawTranscript else report.cleanTranscript
            val rows = remember(text) { parseRows(text) }
            if (rows.isEmpty()) {
                // 空态要给得出原因。准实时上传开启后，「今天的干净稿是空的」是常态
                // （LLM 只在每日处理时间跑），若仍显示「暂无内容」，用户会以为链路坏了
                // ——这正是最初报障时的第一印象。
                val emptyHint = when {
                    tab == 0 -> "暂无内容"
                    report.rawTranscript.isBlank() -> "暂无内容"
                    else -> "原始转写已就绪，去口气与总结将在每日处理时间"
                }
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 200.dp, max = 380.dp)
                        .verticalScroll(rememberScrollState()),
                ) {
                    Text(
                        emptyHint,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.TextDim,
                    )
                }
            } else {
                // (目标时刻, 序号)：带序号才能在「再次点同一时刻」时也触发一次滚动
                var focus by remember(report.date) { mutableStateOf<Pair<Int, Int>?>(null) }
                // 说话人筛选：原始/干净稿逐行带 [说话人] 标签，选中后只看该角色的全部发言
                val options = remember(rows) { speakerOptions(rows) }
                val visible = remember(rows, speakerFilter) {
                    if (speakerFilter == null) rows
                    else rows.filter { it.speaker == speakerFilter }
                }
                SpeakerFilterRow(options, rows.size, speakerFilter) { speakerFilter = it }
                Spacer(Modifier.height(8.dp))
                DayTimeline(
                    rows = visible,
                    focusMinute = focus?.first,
                    showSelfOnly = false,
                    onSeek = { m -> focus = m to ((focus?.second ?: 0) + 1) },
                )
                Spacer(Modifier.height(8.dp))
                TimelineBody(rows = visible, showSelfOnly = false, focusMinute = focus?.first)
            }
        }
    }
}

@Composable
private fun FilterChip(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
    accent: Color = Glass.Indigo,
) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(10.dp))
            .background(if (selected) accent.copy(alpha = 0.16f) else Glass.Surface2)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelLarge,
            color = if (selected) accent else Glass.TextDim,
        )
    }
}

private fun safeParse(date: String): Date =
    SimpleDateFormat("yyyy-MM-dd", Locale.US).parse(date) ?: Date()
