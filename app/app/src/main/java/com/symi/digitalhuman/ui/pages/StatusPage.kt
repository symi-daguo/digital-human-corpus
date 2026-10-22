package com.symi.digitalhuman.ui.pages

import androidx.compose.runtime.setValue
import androidx.compose.runtime.getValue
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.symi.digitalhuman.data.Segment
import com.symi.digitalhuman.data.ServiceEvent
import com.symi.digitalhuman.data.TriggerMode
import com.symi.digitalhuman.service.CaptureState
import com.symi.digitalhuman.ui.AppViewModel
import com.symi.digitalhuman.ui.root.AdaptiveCards
import com.symi.digitalhuman.ui.theme.DateField
import com.symi.digitalhuman.ui.theme.Glass
import com.symi.digitalhuman.ui.theme.GlassCard
import com.symi.digitalhuman.ui.theme.GhostButton
import com.symi.digitalhuman.ui.theme.InsetCard
import com.symi.digitalhuman.ui.theme.PageHeader
import com.symi.digitalhuman.ui.theme.Pill
import com.symi.digitalhuman.ui.theme.PillTone
import com.symi.digitalhuman.ui.theme.PrimaryButton
import com.symi.digitalhuman.ui.theme.SectionTitle
import com.symi.digitalhuman.ui.theme.StatRow
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)
private val dayFmt  = SimpleDateFormat("yyyy-MM-dd", Locale.US)
private const val PAGE_SIZE = 10
private const val BAR_COUNT = 72

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun StatusPage(wsc: WindowSizeClass, vm: AppViewModel = viewModel()) {
    val tick by vm.tick.collectAsStateWithLifecycle()
    val settings by vm.settings.collectAsStateWithLifecycle()
    val sessions by vm.todaySessions.collectAsStateWithLifecycle()
    val stats by vm.todayStats.collectAsStateWithLifecycle()
    val pending by vm.pendingCount.collectAsStateWithLifecycle()
    val events by vm.recentEvents.collectAsStateWithLifecycle()
    val days by vm.availableDays.collectAsStateWithLifecycle()
    val selectedDay by vm.selectedDay.collectAsStateWithLifecycle()
    val daySegments by vm.segmentsOfSelectedDay.collectAsStateWithLifecycle()
    val playableAll by vm.playableSegments.collectAsStateWithLifecycle()
    val playing by vm.playback.collectAsStateWithLifecycle()
    val markMessage by vm.markMessage.collectAsStateWithLifecycle()
    val talkMessage by vm.talkMessage.collectAsStateWithLifecycle()
    val talkBusy by vm.talkBusy.collectAsStateWithLifecycle()

    // 状态快照：从 volatile 读一次
    val captureRunning = tick >= 0 && CaptureState.captureRunning
    val triggerUp = tick >= 0 && CaptureState.triggerUp
    val probeOk = tick >= 0 && CaptureState.lastProbeOk
    // 上传链路是否走外网中转（与 probeOk 语义不同，见 CaptureState.lastLinkViaRelay）。
    // 人走外网时 probeOk=false 而本值为 true —— UI 必须分开说，否则一律显示
    // 「Mac 不可达」，用户会以为整套都断了（2026-09-21 排查时的直接成因）。
    val linkViaRelay = tick >= 0 && CaptureState.lastLinkViaRelay == true
    val micPaused = tick >= 0 && CaptureState.capturePaused
    val playingBack = tick >= 0 && CaptureState.playingBack
    // 播报管家回复（实时对话）也走 playingBack 挂起拾音，但提示语必须分开：
    // 用户没点过「播放」，显示「回放中」会让人以为 App 自己动了。
    val talkPlaying = tick >= 0 && CaptureState.talkPlaying
    val lastAlive = CaptureState.lastCaptureAliveAt
    val now = System.currentTimeMillis()
    val captureStale = captureRunning && lastAlive > 0 && now - lastAlive > 180_000L
    // 落盘活性：心跳只证明「AudioRecord 还在读数」，不证明「真录到了话」。
    // 用户 2026-09-11 反馈「讲了 20 分钟没有任何数据」，当时心跳一直新鲜，
    // 真正该看的是「多久没落盘」——所以健康判据必须挂在段上，不是挂在心跳上。
    val lastSegAt = CaptureState.lastSegmentAt
    val segGapMin = if (lastSegAt > 0) (now - lastSegAt) / 60_000L else -1L
    val segStale = captureRunning && !micPaused && !CaptureState.triggerPaused && segGapMin >= 20

    // 波形活性：是否点亮波形 + 主色
    val waveActive = captureRunning && !micPaused && !CaptureState.triggerPaused && !playingBack && !settings.paused
    val waveAccent = when {
        settings.paused || micPaused -> Glass.Amber
        CaptureState.triggerPaused -> Glass.Amber
        playingBack -> Glass.Indigo
        captureRunning -> Glass.Teal
        else -> Glass.TextDim
    }

    val statusText: String
    val statusTone: PillTone
    val statusSub: String
    val statusTag: String
    when {
        settings.paused -> { statusText = "已暂停"; statusTone = PillTone.Pending;  statusTag = "PAUSED";   statusSub = "采集已一键暂停，点击下方恢复" }
        talkPlaying     -> { statusText = "管家说话中"; statusTone = PillTone.Info; statusTag = "REPLY";    statusSub = "播报期间挂起拾音（防回声自激），播完自动恢复" }
        playingBack     -> { statusText = "回放中"; statusTone = PillTone.Info;     statusTag = "PLAYBACK"; statusSub = "回放期间自动挂起拾音，播完自动恢复" }
        micPaused       -> { statusText = "拾音已暂停"; statusTone = PillTone.Pending; statusTag = "MIC MUTED"; statusSub = "会话保持但暂停写入，点击「继续拾音」恢复" }
        captureRunning && CaptureState.triggerPaused -> {
            statusText = "等待触发"
            statusTone = PillTone.Pending
            statusTag = "STANDBY"
            // 副标题必须反映实时探测而非写死：后台自动续录被系统拦截时会出现
            // triggerUp=true + triggerPaused=true 的中间态，写死「均离线」会与
            // 下方「Mac 可达」Pill 同屏矛盾，用户误以为功能坏了。
            statusSub = if (triggerUp)
                "触发源已在线，此前后台自动续录被系统拦截；回到本页即自动续录，如仍停止请点「开始录音」"
            else if (CaptureState.offlineFallback)
                "Mac 与蓝牙均离线，已自动转为常驻采集中（Mac 回来自动恢复自动模式）"
            else
                "Mac 与蓝牙均离线，服务保持待命；任一恢复在线即自动续录"
        }
        captureRunning  -> {
            statusText = "采集中"
            statusTone = PillTone.Positive
            statusTag = "LIVE"
            statusSub = if (CaptureState.offlineFallback)
                "Mac 离线兜底：已转常驻采集 · 会话 #${CaptureState.sessionId}"
            else
                "触发源：${triggerLabel(CaptureState.triggerType)} · 会话 #${CaptureState.sessionId}"
        }
        else -> {
            statusText = "待机"
            statusTone = PillTone.Neutral
            statusTag = "IDLE"
            statusSub = when (settings.triggerMode) {
                TriggerMode.AUTO   -> "等待 Mac 上线或蓝牙连接"
                TriggerMode.ALWAYS -> "常驻模式待机"
                else               -> "手动模式：点击「开始录音」即可录制（无需 Mac）"
            }
        }
    }

    // 每天段数（供日期胶囊角标）
    val dayCounts = remember(playableAll) {
        playableAll.groupBy { dayFmt.format(Date(it.startOffsetMs)) }.mapValues { (_, v) -> v.size }
    }

    // 分页状态：切换日期时自动回到第一页
    var page by remember(selectedDay) { mutableStateOf(0) }
    val totalPages = ((daySegments.size + PAGE_SIZE - 1) / PAGE_SIZE).coerceAtLeast(1)
    val pageItems = daySegments.drop(page * PAGE_SIZE).take(PAGE_SIZE)

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        PageHeader(
            title = "语料采集",
            subtitle = "${dayFmt.format(Date())} · 数字人语料录制",
        ) {
            Pill(
                label = if (pending > 0) "$pending 待上传" else "已同步",
                tone = if (pending > 0) PillTone.Pending else PillTone.Positive,
            )
        }

        Spacer(Modifier.height(16.dp))

        AdaptiveCards(
            windowSizeClass = wsc,
            cards = buildList {
                // 1) 健康红条（仅在 stale 时显示）
                if (captureStale) {
                    add {
                        InsetCard {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Box(
                                    Modifier.size(8.dp).clip(CircleShape)
                                        .background(Glass.Red)
                                )
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    "服务心跳异常（>3 分钟无响应），录音可能已中断。尝试点击恢复或重启 App。",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Glass.Red,
                                )
                            }
                        }
                    }
                }

                // 1b) 落盘活性提示（中性措辞：安静场合长时间没段是正常的，
                //     但若用户刚说过话却没数据，这条就是唯一能提示他的地方）
                if (segStale) {
                    add {
                        InsetCard {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Box(
                                    Modifier.size(8.dp).clip(CircleShape)
                                        .background(Glass.Amber)
                                )
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    "已 ${segGapMin} 分钟没有新的录音段落盘。若您刚才确实在说话却没有数据，请下拉通知栏确认「语料采集中」是否还在。",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Glass.Amber,
                                )
                            }
                        }
                    }
                }

                // 2) 主状态 + 实时波形（紧凑 hero）
                add {
                    GlassCard(elevated = true) {
                        StatusHero(
                            statusText = statusText,
                            statusTone = statusTone,
                            statusTag = statusTag,
                            statusSub = statusSub,
                            waveAccent = waveAccent,
                            waveActive = waveActive,
                            captureRunning = captureRunning,
                            probeOk = probeOk,
                            linkViaRelay = linkViaRelay,
                            triggerUp = triggerUp,
                            pending = pending,
                            triggerMode = settings.triggerMode,
                            micPaused = micPaused,
                            playingBack = playingBack,
                        )
                    }
                }

                // 3) 采集控制
                add {
                    GlassCard {
                        SectionTitle("采集控制", accent = Glass.Teal)
                        Spacer(Modifier.height(12.dp))
                        // 手动开始/停止：仅在 MANUAL/ALWAYS 下显示（AUTO 由触发源决定）
                        if (settings.triggerMode != TriggerMode.AUTO) {
                            PrimaryButton(
                                if (captureRunning) "停止录音" else "开始录音",
                                onClick = { if (captureRunning) vm.stopCapture() else vm.startCapture() },
                                accent = if (captureRunning) Glass.Red else Glass.Teal,
                                modifier = Modifier.fillMaxWidth(),
                            )
                            Spacer(Modifier.height(10.dp))
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            PrimaryButton(
                                if (settings.paused) "恢复采集" else "一键暂停",
                                onClick = { vm.setPaused(!settings.paused) },
                                accent = if (settings.paused) Glass.Teal else Glass.Amber,
                                modifier = Modifier.weight(1f),
                            )
                            GhostButton(
                                "立即上传",
                                onClick = { vm.uploadNow() },
                                accent = Glass.Indigo,
                                modifier = Modifier.weight(1f),
                            )
                        }
                        Spacer(Modifier.height(10.dp))
                        // 重要时刻标记（DayoTag 式随手标记）：按一下在日报里留一个
                        // 时间锚点，回看时直接按图索骥；不依赖 LLM，确定性呈现。
                        GhostButton(
                            "标记此刻",
                            onClick = { vm.markNow() },
                            accent = Glass.Teal,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Spacer(Modifier.height(10.dp))
                        // 实时对话（Hermes 对接，2026-09-10）：录一段 → 服务端 ASR →
                        // 管家出回复 → 手机扬声器播报。整宽按钮，与「标记此刻」同规格，
                        // 录制期间按钮文案变为「对话中…」防止重复点击抢麦。
                        //
                        // 播报期间**不把「对话」换成「停止播报」，而是禁用「对话」、
                        // 在下方**多出一个**「停止播报」**（2026-09-10 真机踩到）：
                        // 两个按钮在同一组合槽位互换时，上一帧按下、下一帧抬起的手势会
                        // 「迁移」到新节点上，新节点的 onClick 被误触发——实测出现过
                        // 无人操作就打出「打断播报：用户点击停止」的情况。让新按钮
                        // 出现在一个新的槽位，这条路径就不存在了。
                        GhostButton(
                            if (talkBusy) "对话中…" else "对话（说「大锅…」）",
                            onClick = { vm.talkNow() },
                            enabled = !talkPlaying,
                            accent = Glass.Indigo,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        if (talkPlaying) {
                            Spacer(Modifier.height(10.dp))
                            PrimaryButton(
                                "停止播报",
                                onClick = { vm.stopTalkPlayback() },
                                accent = Glass.Red,
                                modifier = Modifier.fillMaxWidth(),
                            )
                        }
                        if (talkMessage != null) {
                            Spacer(Modifier.height(6.dp))
                            Text(
                                talkMessage.orEmpty(),
                                style = MaterialTheme.typography.bodySmall,
                                color = Glass.TextFaint,
                            )
                            LaunchedEffect(talkMessage) {
                                kotlinx.coroutines.delay(9000)
                                vm.clearTalkMessage()
                            }
                        }
                        if (markMessage != null) {
                            Spacer(Modifier.height(6.dp))
                            Text(
                                markMessage.orEmpty(),
                                style = MaterialTheme.typography.bodySmall,
                                color = Glass.TextFaint,
                            )
                            LaunchedEffect(markMessage) {
                                kotlinx.coroutines.delay(4000)
                                vm.clearMarkMessage()
                            }
                        }
                    }
                }

                // 4) 拾音控制（仅在采集中且未一键暂停时显示：
                //    全局暂停时「暂停拾音/结束会话」与上方「恢复采集」语义冲突，先隐藏，
                //    恢复采集后再回来操作）
                if (captureRunning && !settings.paused) {
                    add {
                        GlassCard {
                            SectionTitle("拾音控制", accent = Glass.Indigo)
                            Spacer(Modifier.height(12.dp))
                            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                                GhostButton(
                                    if (micPaused) "继续拾音" else "暂停拾音",
                                    onClick = { vm.toggleMicPause() },
                                    accent = if (micPaused) Glass.Teal else Glass.Amber,
                                    modifier = Modifier.weight(1f),
                                )
                                GhostButton(
                                    "结束会话",
                                    onClick = { vm.endSession() },
                                    accent = Glass.Red,
                                    modifier = Modifier.weight(1f),
                                )
                            }
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "暂停拾音：会话保持，停止写入；结束会话：本次采集立即归档（自动模式在触发源在线时会自动续录）。",
                                style = MaterialTheme.typography.bodySmall,
                                color = Glass.TextFaint,
                            )
                        }
                    }
                }

                // 5) 今日统计
                add {
                    GlassCard {
                        SectionTitle("今日统计", accent = Glass.Indigo)
                        Spacer(Modifier.height(8.dp))
                        val totalSec = stats.sumOf { it.recordSeconds }
                        // 「触发次数」和「会话数」曾同时显示 sessions.size —— 每次 startCapture
                        // 都必然同时新建一条 Session 和一条 CAPTURE_STARTED 事件，两行永远相等，
                        // 看着像两个指标其实是同一个。换成今日语音段数才是独立信息。
                        val todayStr = dayFmt.format(Date())
                        val todaySegs = playableAll.count { dayFmt.format(Date(it.startOffsetMs)) == todayStr }
                        StatRow("今日语音段", "$todaySegs 段", Glass.Teal)
                        StatRow("录音时长", formatDuration(totalSec), Glass.Teal)
                        StatRow("会话数", "${sessions.size} 次")
                        StatRow(
                            "待上传音频",
                            "$pending 段",
                            if (pending > 0) Glass.Amber else Glass.Green,
                        )
                        StatRow(
                            "每日处理时间",
                            formatMinuteOfDay(settings.processMinuteOfDay),
                            helper = "到点后 Mac 自动批量转写并生成日报",
                        )
                    }
                }

                // 6) 录音回放
                add {
                    PlaybackCard(
                        days = days,
                        dayCounts = dayCounts,
                        selectedDay = selectedDay,
                        segments = pageItems,
                        totalSegments = daySegments.size,
                        page = page,
                        totalPages = totalPages,
                        playingId = playing?.id,
                        onSelectDay = { d -> vm.selectDay(d); page = 0 },
                        onToggle = { seg -> vm.togglePlayback(seg) },
                        onStop = { vm.stopPlayback() },
                        onPrev = { if (page > 0) page-- },
                        onNext = { if (page < totalPages - 1) page++ },
                    )
                }

                // 7) 最近事件
                if (events.isNotEmpty()) {
                    add {
                        GlassCard {
                            SectionTitle("最近事件", accent = Glass.Amber)
                            Spacer(Modifier.height(8.dp))
                            events.forEach { e ->
                                val (title, detail, tone) = eventInfo(e.type, e.detail)
                                Row(
                                    Modifier.fillMaxWidth().padding(vertical = 6.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    // 语义色点：一眼区分 正常(Green/Indigo) / 注意(Amber) / 异常(Red) / 中性
                                    Box(
                                        Modifier
                                            .size(8.dp)
                                            .clip(CircleShape)
                                            .background(tone)
                                    )
                                    Spacer(Modifier.width(10.dp))
                                    Column(Modifier.weight(1f)) {
                                        Text(
                                            title,
                                            style = MaterialTheme.typography.bodyMedium,
                                            color = Glass.Text,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis,
                                        )
                                        if (!detail.isNullOrBlank()) {
                                            Text(
                                                detail,
                                                style = MaterialTheme.typography.bodySmall,
                                                color = Glass.TextFaint,
                                                maxLines = 1,
                                                overflow = TextOverflow.Ellipsis,
                                            )
                                        }
                                    }
                                    Spacer(Modifier.width(8.dp))
                                    Text(
                                        timeFmt.format(Date(e.at)),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = Glass.TextFaint,
                                    )
                                }
                            }
                        }
                    }
                }
            },
        )
        Spacer(Modifier.height(8.dp))
    }
}

/**
 * Hero 区：紧凑信息密度（替代旧版 34sp 待机大字）。
 *   - 第 1 行：状态点 + 状态文字 + tone Pill
 *   - 第 2 行：触发源 / 会话号（dim）
 *   - 第 3 行：实时波形（独立子组件，仅自身重组）
 *   - 第 4 行：连接/触发/待上传 三段 Pill
 */
@Composable
private fun StatusHero(
    statusText: String,
    statusTone: PillTone,
    statusTag: String,
    statusSub: String,
    waveAccent: Color,
    waveActive: Boolean,
    captureRunning: Boolean,
    probeOk: Boolean,
    /** 上传链路是否走外网中转（与 probeOk 语义不同，见 CaptureState.lastLinkViaRelay） */
    linkViaRelay: Boolean,
    triggerUp: Boolean,
    pending: Int,
    triggerMode: String,
    micPaused: Boolean,
    playingBack: Boolean,
) {
    Column(Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            // 状态指示点（运行中=Green，暂停=Amber，错误=Red，回放=Indigo）
            val dotColor = when (statusTone) {
                PillTone.Positive -> Glass.Green
                PillTone.Pending  -> Glass.Amber
                PillTone.Negative -> Glass.Red
                PillTone.Info     -> Glass.Indigo
                PillTone.Neutral  -> Glass.TextDim
            }
            Box(
                Modifier
                    .size(10.dp)
                    .clip(CircleShape)
                    .background(dotColor),
            )
            Spacer(Modifier.width(10.dp))
            Text(
                statusText,
                style = MaterialTheme.typography.headlineSmall,
                color = Glass.Text,
            )
            Spacer(Modifier.width(10.dp))
            Pill(label = statusTag, tone = statusTone, showDot = false)
        }
        Spacer(Modifier.height(6.dp))
        Text(
            statusSub,
            style = MaterialTheme.typography.bodyMedium,
            color = Glass.TextDim,
        )
        Spacer(Modifier.height(16.dp))

        // 波形（独立子组件：内部读 level，仅自身重组）
        Waveform(accent = waveAccent, active = waveActive)
        Spacer(Modifier.height(8.dp))
        Text(
            when {
                waveActive   -> "实时拾音 —— 对手机说话，波形随之跳动"
                playingBack  -> "回放中，拾音挂起（此时说话不会被录制）"
                micPaused    -> "拾音已暂停，波形静默"
                else         -> "未在采集 —— 波形将随拾音实时显示"
            },
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextFaint,
        )

        Spacer(Modifier.height(14.dp))
        // 三段状态：连接 / 触发 / 待上传（AUTO 模式下 Mac 必须可达，其它模式仅展示）
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            // 两种「可达」分开报：probeOk=局域网直连（AUTO 的触发依据），
            // linkViaRelay=上传链路走外网中转。人在外面时以前只会显示「Mac 不可达」，
            // 明明中转是通的——用户因此判断"手机远程连不上 Mac"。
            when {
                triggerMode == TriggerMode.AUTO && probeOk ->
                    Pill("Mac 可达（局域网）", tone = PillTone.Positive)
                // 中转可达 ≠ 在电脑旁：能上传、但 AUTO 不会自动开录，如实写出来
                triggerMode == TriggerMode.AUTO && linkViaRelay ->
                    Pill("Mac 可达（外网中转，不自动触发）", tone = PillTone.Neutral)
                triggerMode == TriggerMode.AUTO ->
                    Pill("Mac 不可达", tone = PillTone.Negative)
                probeOk ->
                    Pill("Mac 可达（局域网）", tone = PillTone.Neutral)
                linkViaRelay ->
                    Pill("Mac 可达（外网中转）", tone = PillTone.Neutral)
                else ->
                    Pill("未连接 Mac（不影响录音）", tone = PillTone.Neutral)
            }
            Pill(
                if (triggerUp) "触发在线" else "未触发",
                tone = if (triggerUp) PillTone.Positive else PillTone.Neutral,
            )
            Pill(
                "待上传 $pending 段",
                tone = if (pending > 0) PillTone.Pending else PillTone.Positive,
            )
        }
    }
}

/** 录音回放卡：日期胶囊 + 分页列表 */
@Composable
private fun PlaybackCard(
    days: List<String>,
    dayCounts: Map<String, Int>,
    selectedDay: String,
    segments: List<Segment>,
    totalSegments: Int,
    page: Int,
    totalPages: Int,
    playingId: Long?,
    onSelectDay: (String) -> Unit,
    onToggle: (Segment) -> Unit,
    onStop: () -> Unit,
    onPrev: () -> Unit,
    onNext: () -> Unit,
) {
    GlassCard {
        // 头部：标题 + 分页控件（右侧，替代旧版底部居中的圆形翻页按钮，省一行高度）
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            SectionTitle("录音回放", modifier = Modifier.weight(1f))
            if (totalPages > 1) {
                Text(
                    "${page + 1}/${totalPages}",
                    style = MaterialTheme.typography.labelLarge,
                    color = Glass.TextDim,
                )
                Spacer(Modifier.width(8.dp))
                PagerTextButton("上一页", enabled = page > 0, onClick = onPrev)
                Spacer(Modifier.width(6.dp))
                PagerTextButton("下一页", enabled = page < totalPages - 1, onClick = onNext)
            }
            if (playingId != null) {
                if (totalPages > 1) Spacer(Modifier.width(8.dp))
                GhostButton("停止回放", onClick = onStop, accent = Glass.Red, height = 36.dp)
            }
        }
        Spacer(Modifier.height(12.dp))

        if (days.isEmpty()) {
            Text(
                "暂无录音片段，采集后会自动出现在这里",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
            return@GlassCard
        }

        // 日期选择：与日报页同一个 Material3 日历组件（跨月可跳、只有有录音的日期可选）
        DateField(
            currentDate = selectedDay,
            availableDates = remember(days) { days.toSet() },
            onSelect = onSelectDay,
            hint = "共 ${days.size} 天",
        )
        Spacer(Modifier.height(12.dp))
        // 最近几天的快捷胶囊：日历是完整选择器，这排是高频日期的一步直达
        Row(
            Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            days.forEach { day ->
                DayChip(
                    label = dayLabel(day),
                    count = dayCounts[day] ?: 0,
                    selected = day == selectedDay,
                ) { onSelectDay(day) }
            }
        }
        Spacer(Modifier.height(14.dp))

        // 分页列表
        if (segments.isEmpty()) {
            Text(
                "当天暂无录音片段",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        } else {
            segments.forEach { seg ->
                PlaybackRow(seg = seg, isPlaying = playingId == seg.id, onToggle = { onToggle(seg) })
            }
        }

        if (totalSegments > PAGE_SIZE) {
            Spacer(Modifier.height(6.dp))
            Text(
                "共 $totalSegments 段 · 每页 ${PAGE_SIZE} 条",
                style = MaterialTheme.typography.labelSmall,
                color = Glass.TextFaint,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

/** 紧凑文本分页按钮（替代旧版 48dp 圆形图标按钮） */
@Composable
private fun PagerTextButton(label: String, enabled: Boolean, onClick: () -> Unit) {
    Text(
        label,
        style = MaterialTheme.typography.labelLarge,
        color = if (enabled) Glass.Teal else Glass.TextFaint,
        modifier = Modifier
            .clip(RoundedCornerShape(8.dp))
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = 8.dp, vertical = 6.dp),
    )
}

/** 日期胶囊：今天/昨天/MM-dd + 段数角标 */
@Composable
private fun DayChip(label: String, count: Int, selected: Boolean, onClick: () -> Unit) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .clip(RoundedCornerShape(10.dp))
            .background(if (selected) Glass.Teal.copy(alpha = 0.16f) else Glass.Surface2)
            .border(1.dp, if (selected) Glass.Teal.copy(alpha = 0.5f) else Glass.Stroke, RoundedCornerShape(10.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelLarge,
            color = if (selected) Glass.Teal else Glass.Text,
        )
        Text(
            "$count 段",
            style = MaterialTheme.typography.labelSmall,
            color = Glass.TextFaint,
        )
    }
}

/** 单个回放行：说话人标记 + 时间 + 时长 + 标签 + 文本播放按钮。整行可点（旧版仅小圆钮可点，点按区域太小） */
@Composable
private fun PlaybackRow(seg: Segment, isPlaying: Boolean, onToggle: () -> Unit) {
    // 说话人语义：先看 Mac 回流的 speakerLabel；未回流时退到 is_user / is_speech 三态
    val label = when {
        seg.speakerLabel.isNotBlank() -> seg.speakerLabel
        seg.isUser == true            -> "本人"
        seg.isSpeech == false         -> "非人声"
        else                          -> "待判定"
    }
    val dot = when {
        seg.isSpeech == false                     -> Glass.TextDim
        seg.speakerLabel == "本人" || seg.isUser == true -> Glass.Green
        seg.speakerLabel.isNotBlank()             -> Glass.Indigo
        else                                      -> Glass.Stroke
    }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(if (isPlaying) Glass.Indigo.copy(alpha = 0.08f) else Color.Transparent)
            .clickable(onClick = onToggle)
            .padding(horizontal = 8.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(dot)
                .border(1.dp, Glass.Stroke, CircleShape)
        )
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                timeFmt.format(Date(seg.startOffsetMs)),
                style = MaterialTheme.typography.titleSmall,
                color = if (isPlaying) Glass.Indigo else Glass.Text,
            )
            Text(
                "${formatDuration(seg.durationMs / 1000)} · $label",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        }
        GhostButton(
            if (isPlaying) "停止" else "播放",
            onClick = onToggle,
            accent = if (isPlaying) Glass.Red else Glass.Teal,
            height = 32.dp,
        )
    }
}

/** 触发源展示名 */
private fun triggerLabel(type: String): String = when (type) {
    "BT"      -> "蓝牙连接"
    "LAN"     -> "局域网在线"
    "AUTO"    -> "自动（Mac/蓝牙）"
    "MANUAL"  -> "手动开始"
    "ALWAYS"  -> "常驻采集"
    "RECOVER" -> "自动恢复"
    else      -> type.ifBlank { "未知" }
}

/**
 * 事件 → (中文标题, 人性化补充说明, 语义色)。
 * 旧版直接展示原始 type/detail（如 "CAPTURE_BLOCKED 后台启动麦克风前台服务被系统拒绝"），不可读。
 */
private fun eventInfo(type: String, detail: String): Triple<String, String?, Color> = when (type) {
    "CAPTURE_STARTED" -> Triple(
        "开始采集",
        detail.substringAfter("session=").takeIf { it != detail }?.let { "会话 #$it" } ?: detail.ifBlank { null },
        Glass.Green,
    )
    "CAPTURE_STOPPED" -> Triple(
        "停止采集",
        when {
            detail.contains("TRIGGER_GONE")          -> "触发源离线，自动挂起"
            detail.contains("USER")                  -> "手动停止"
            detail.contains("PAUSE")                 -> "一键暂停"
            detail.isBlank()                         -> null
            else                                     -> detail
        },
        Glass.TextDim,
    )
    "GUARD_STARTED"   -> Triple("守护服务已启动", detail.ifBlank { null }, Glass.Indigo)
    "GUARD_STOPPED"   -> Triple("守护服务已停止", detail.ifBlank { null }, Glass.TextDim)
    "PROBE_UP"        -> Triple("Mac 已上线", detail.ifBlank { null }, Glass.Green)
    "PROBE_DOWN"      -> Triple("Mac 已离线", detail.ifBlank { null }, Glass.Amber)
    "UPLOAD_OK"       -> Triple("上传完成", detail.ifBlank { null }, Glass.Green)
    "UPLOAD_FAIL"     -> Triple("上传失败", detail.ifBlank { null }, Glass.Red)
    "CAPTURE_BLOCKED" -> Triple(
        "后台录音被系统拦截",
        "系统限制后台启动麦克风，回到 App 点击「开始录音」即可恢复",
        Glass.Red,
    )
    else              -> Triple(type, detail.ifBlank { null }, Glass.TextDim)
}

/** 日期显示：今天/昨天优先，否则 MM-dd */
private fun dayLabel(day: String): String {
    val today = dayFmt.format(Date())
    val yesterday = dayFmt.format(Date(System.currentTimeMillis() - 86_400_000L))
    return when (day) {
        today     -> "今天"
        yesterday -> "昨天"
        else      -> day.substring(5)
    }
}

/**
 * 实时拾音波形：滚动柱状电平条。
 *
 * 性能说明：仅当 `active` 变化时进入 while 循环 + 自身重组。
 * 父层 List 重组不会触发本组件（除非 `active` 变化），避免 tick 每秒触发整页重组。
 */
@Composable
private fun Waveform(accent: Color, active: Boolean, modifier: Modifier = Modifier) {
    val bars = remember { mutableStateListOf<Float>() }
    LaunchedEffect(active) {
        if (!active) {
            bars.clear()
            return@LaunchedEffect
        }
        while (true) {
            bars.add(CaptureState.level)
            while (bars.size > BAR_COUNT) bars.removeAt(0)
            delay(60)
        }
    }
    Canvas(modifier.fillMaxWidth().height(56.dp)) {
        val bw = size.width / BAR_COUNT
        // 静默中线
        drawLine(
            Glass.Stroke,
            Offset(0f, size.height / 2),
            Offset(size.width, size.height / 2),
            strokeWidth = 1.dp.toPx(),
        )
        bars.forEachIndexed { i, lv ->
            val h = size.height * 0.92f * lv.coerceIn(0.03f, 1f)
            drawRoundRect(
                color = accent.copy(alpha = 0.85f),
                topLeft = Offset(i * bw + 1.dp.toPx(), (size.height - h) / 2),
                size = Size(bw - 2.dp.toPx(), h),
                cornerRadius = CornerRadius(2.dp.toPx()),
            )
        }
    }
}

internal fun formatDuration(totalSec: Long): String {
    val h = totalSec / 3600
    val m = (totalSec % 3600) / 60
    val s = totalSec % 60
    return if (h > 0) "%d时%02d分%02d秒".format(h, m, s) else "%d分%02d秒".format(m, s)
}

internal fun formatMinuteOfDay(min: Int): String = "%02d:%02d".format(min / 60, min % 60)
