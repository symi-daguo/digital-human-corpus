package com.symi.digitalhuman.ui.pages

import androidx.compose.runtime.setValue
import androidx.compose.runtime.getValue
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AllInclusive
import androidx.compose.material.icons.filled.Bluetooth
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.RecordVoiceOver
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.BroadcastReceiver
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.delay
import com.symi.digitalhuman.data.AppSettings
import com.symi.digitalhuman.data.CaptureWindow
import com.symi.digitalhuman.data.ENROLL_SCRIPT
import com.symi.digitalhuman.data.ENROLL_SECONDS
import com.symi.digitalhuman.data.TriggerMode
import com.symi.digitalhuman.sync.MacClient
import com.symi.digitalhuman.ui.AppViewModel
import com.symi.digitalhuman.ui.root.AdaptiveCards
import com.symi.digitalhuman.ui.theme.GhostButton
import com.symi.digitalhuman.ui.theme.Glass
import com.symi.digitalhuman.ui.theme.GlassCard

import com.symi.digitalhuman.ui.theme.InsetCard
import com.symi.digitalhuman.ui.theme.PageHeader
import com.symi.digitalhuman.ui.theme.Pill
import com.symi.digitalhuman.ui.theme.PillTone
import com.symi.digitalhuman.ui.theme.PrimaryButton
import com.symi.digitalhuman.BuildConfig
import com.symi.digitalhuman.ui.theme.SectionTitle

/** 本人标签（与 Mac 端 config.SELF_LABEL 一致） */
private const val SELF_LABEL = "本人"

/**
 * 空间 ID 合法性判据，**与服务端 tenants._ID_RE 逐字一致**。
 *
 * 两端必须同源：服务端不合法就 403，而 403 在用户眼里和「没连上」长得一样。
 * 实测踩过——填了 `1001`（数字开头），保存时毫无提示，之后每次对话都失败，
 * 表现为「管家突然不理我了」，排查了很久才发现是这一栏。
 * 客户端能拦的错就别留到网络上。
 */
private val TENANT_ID_RE = Regex("^[a-z][a-z0-9_-]{0,23}$")

/** 返回 null 表示合法；否则返回给用户看的原因。留空合法 = 用默认空间。 */
private fun tenantIdError(raw: String): String? {
    val v = raw.trim()
    if (v.isEmpty()) return null
    if (!TENANT_ID_RE.matches(v)) {
        return "必须以小写字母开头，只能含小写字母、数字、中划线、下划线，最多 24 位"
    }
    return null
}

@Composable
fun SettingsPage(wsc: WindowSizeClass, vm: AppViewModel = viewModel()) {
    val saved by vm.settings.collectAsStateWithLifecycle()
    var draft by remember(saved) { mutableStateOf(saved) }
    val dirty = draft != saved

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        PageHeader(
            title = "设置",
            // 版本号放副标题：客户反馈问题第一句必问「装的哪个版本」，
            // 藏在「关于」二级页里等于没有。
            subtitle = "语料采集服务配置 · v${BuildConfig.VERSION_NAME}",
        ) {
            if (dirty) {
                Pill(label = "未保存", tone = PillTone.Pending)
            } else {
                Pill(label = "已保存", tone = PillTone.Positive)
            }
        }
        Spacer(Modifier.height(16.dp))
        AdaptiveCards(
            windowSizeClass = wsc,
            cards = listOf(
                { TriggerCard(draft) { draft = it } },
                { KeepAliveCard() },
                { MacConnectionCard(vm, draft) { draft = it } },
                { VoiceprintCard(vm) },
                { IndustryCard(vm) },
                { TalkCard(vm, draft) { draft = it } },
                { CaptureWindowCard(draft) { draft = it } },
                { DailyFlowCard(draft) { draft = it } },
                { StorageCard(draft) { draft = it } },
                { BackupCard(vm) },
                { InsightsCard(vm) },
                {
                    GlassCard {
                        SectionTitle("保存", accent = Glass.Indigo)
                        Spacer(Modifier.height(12.dp))
                        // 有非法值时直接禁用保存，并在按钮上方说明原因。
                        // 让用户「改不了」比让他「改了但用不了」好得多：
                        // 空间 ID 填错的后果是之后**所有**请求 403，而报错文案
                        // 只会说「连不上 Mac 服务」，根本指不回这一栏。
                        val tidErr = tenantIdError(draft.tenantId)
                        if (tidErr != null) {
                            Text(
                                "空间 ID 不合法：$tidErr",
                                style = MaterialTheme.typography.bodySmall,
                                color = Glass.Red,
                            )
                            Spacer(Modifier.height(8.dp))
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            PrimaryButton(
                                "保存设置",
                                onClick = { vm.saveSettings(draft) },
                                enabled = dirty && tidErr == null,
                                accent = Glass.Teal,
                                modifier = Modifier.weight(1f),
                            )
                            GhostButton(
                                "还原",
                                onClick = { draft = saved },
                                enabled = dirty,
                                accent = Glass.Red,
                                modifier = Modifier.weight(1f),
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                        Pill(
                            if (saved.paused) "当前状态：已暂停" else "当前状态：运行中",
                            tone = if (saved.paused) PillTone.Pending else PillTone.Positive,
                            showDot = true,
                        )
                    }
                },
            ),
        )
        Spacer(Modifier.height(8.dp))
    }
}

@Composable
private fun MacConnectionCard(vm: AppViewModel, draft: AppSettings, onChange: (AppSettings) -> Unit) {
    GlassCard {
        SectionTitle("Mac 服务连接")
        Spacer(Modifier.height(12.dp))
        Field("Mac 地址（局域网 IP 或域名）", draft.macIp) { onChange(draft.copy(macIp = it)) }
        Spacer(Modifier.height(10.dp))
        Field("端口", draft.macPort.toString(), number = true) {
            onChange(draft.copy(macPort = it.toIntOrNull() ?: draft.macPort))
        }
        Spacer(Modifier.height(10.dp))
        Field(
            "配对 Token",
            draft.token,
            placeholder = "默认 ${AppSettings.DEFAULT_TOKEN}",
        ) { onChange(draft.copy(token = it)) }
        Spacer(Modifier.height(10.dp))
        // 外网中转（frp 隧道指向同一台 Mac）：连不上局域网 Mac 时自动切换，
        // 两条路同一证书同一 Token。留空则保持旧行为（只走局域网）。
        Field(
            "外网中转地址（可选）",
            draft.relayAddress,
            placeholder = "host:port，留空 = 只用局域网",
        ) { onChange(draft.copy(relayAddress = it.trim())) }
        Spacer(Modifier.height(6.dp))
        Text(
            "自适应连接：在家自动走局域网；Mac 局域网不可达且填了中转地址时，自动改走外网中转，回家自动切回。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextFaint,
        )

        Spacer(Modifier.height(10.dp))
        // 设备名称：多人共用一台 Mac 时按手机区分来源。
        // 每段上传以 ?device= 带给服务端落 device_label，/api/devices 可查各设备活跃度。
        Field(
            "设备名称（团队多人区分，可选）",
            draft.ownerName,
            placeholder = "建议填自己名字，留空 = 用手机型号",
        ) { onChange(draft.copy(ownerName = it.trim())) }
        Spacer(Modifier.height(6.dp))
        Text(
            "填了设备名后，每段录音上传都会带上它；在 Mac 端日报与设备列表里按此名字区分是哪部手机传的。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextFaint,
        )

        Spacer(Modifier.height(10.dp))
        // 租户空间 ID（多租户）：管理员在工作台「空间管理」创建后分发；
        // 服务端按 data/tenants/<id>/ 物理隔离，填错未注册 ID 会被 403 拒绝。
        Field(
            "空间 ID（租户隔离，可选）",
            draft.tenantId,
            placeholder = "留空 = 默认空间，多人请填各自分配的 ID",
        ) { onChange(draft.copy(tenantId = it.trim())) }
        Spacer(Modifier.height(6.dp))
        // 实时回显格式错误：只在**非空且非法**时变红，留空是合法状态不打扰。
        val tidErr = tenantIdError(draft.tenantId)
        Text(
            if (tidErr != null) "空间 ID 不合法：$tidErr"
            else "每部手机对应一个空间：录音、日报、声纹、角色在服务端完全隔离、互不可见。" +
                "ID 需管理员在 Mac 工作台创建（小写字母开头，如 zhangsan）。填错会连不上，留空即默认空间。",
            style = MaterialTheme.typography.bodySmall,
            color = if (tidErr != null) Glass.Red else Glass.TextFaint,
        )

        Spacer(Modifier.height(10.dp))
        Field(
            "证书指纹 SHA-256（点下方按钮自动填入）",
            draft.pinnedCertSha256,
            placeholder = "留空 = 信任任意自签证书",
        ) { onChange(draft.copy(pinnedCertSha256 = it.trim())) }
        Spacer(Modifier.height(10.dp))

        // 抓回来就自动填进草稿：用户不用手动复制粘贴 44 位 base64。
        // key 用指纹本身，填完 draft 变化引发重组时不会重复触发。
        val probe by vm.certProbe.collectAsStateWithLifecycle()
        LaunchedEffect(probe) {
            if (probe is AppViewModel.CertProbeState.Ok) {
                onChange(draft.copy(pinnedCertSha256 = (probe as AppViewModel.CertProbeState.Ok).fingerprint))
                vm.resetCertProbe()
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            GhostButton(
                if (probe is AppViewModel.CertProbeState.Loading) "获取中……" else "获取证书指纹",
                onClick = { vm.fetchCertFingerprint(draft) },
                enabled = probe !is AppViewModel.CertProbeState.Loading,
                accent = Glass.Indigo,
                modifier = Modifier.weight(1f),
            )
            if (draft.pinnedCertSha256.isNotBlank()) {
                GhostButton(
                    "清除",
                    onClick = {
                        onChange(draft.copy(pinnedCertSha256 = ""))
                        vm.resetCertProbe()
                    },
                    accent = Glass.Red,
                    modifier = Modifier.weight(1f),
                )
            }
        }

        Spacer(Modifier.height(10.dp))
        when (val p = probe) {
            is AppViewModel.CertProbeState.Fail -> InsetCard {
                Text(
                    "获取失败：${p.message}",
                    style = MaterialTheme.typography.bodySmall,
                    color = Glass.Red,
                )
            }
            is AppViewModel.CertProbeState.Ok -> InsetCard {
                Text(
                    "已获取并填入，记得点「保存设置」",
                    style = MaterialTheme.typography.bodySmall,
                    color = Glass.Green,
                )
            }
            else -> if (draft.pinnedCertSha256.isBlank()) {
                InsetCard {
                    Text(
                        "未锁定证书：局域网内任何自签证书都能通过校验，存在中间人截获录音与 Token 的风险。" +
                            "先填好上面的 IP 与端口，再点「获取证书指纹」。",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.Amber,
                    )
                }
            } else {
                InsetCard {
                    Text(
                        "已锁定证书指纹：只信任该证书，中间人无法伪造。" +
                            "Mac 端换新证书后需重新获取一次。",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.Green,
                    )
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(
            "也可以在 Mac 上手工取：openssl s_client -connect IP:${draft.macPort} </dev/null 2>/dev/null " +
                "| openssl x509 -pubkey -noout | openssl pkey -pubin -outform der " +
                "| openssl dgst -sha256 -binary | openssl enc -base64",
            style = MaterialTheme.typography.labelSmall,
            color = Glass.TextFaint,
        )
    }
}

@Composable
private fun TriggerCard(draft: AppSettings, onChange: (AppSettings) -> Unit) {
    GlassCard {
        SectionTitle("触发方式", accent = Glass.Indigo)
        Spacer(Modifier.height(6.dp))
        Text(
            "决定什么时候开始录音。手动 / 常驻模式不需要连接 Mac 即可录制。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )
        Spacer(Modifier.height(12.dp))
        TriggerMode.ALL.forEach { mode ->
            val (label, desc, icon) = triggerOptionMeta(mode)
            ModeOption(
                icon = icon,
                label = label,
                description = desc,
                selected = draft.triggerMode == mode,
            ) { onChange(draft.copy(triggerMode = mode)) }
            Spacer(Modifier.height(8.dp))
        }
        if (draft.triggerMode == TriggerMode.AUTO) {
            Spacer(Modifier.height(6.dp))
            InsetCard {
                Field("备用触发手机蓝牙 MAC", draft.btMac, placeholder = "留空 = 禁用蓝牙触发") {
                    onChange(draft.copy(btMac = it))
                }
                Spacer(Modifier.height(10.dp))
                var showPicker by remember { mutableStateOf(false) }
                GhostButton(
                    "扫描附近设备并选择",
                    onClick = { showPicker = true },
                    accent = Glass.Indigo,
                    modifier = Modifier.fillMaxWidth(),
                )
                if (showPicker) {
                    BluetoothPickerDialog(
                        current = draft.btMac,
                        onPick = { mac ->
                            onChange(draft.copy(btMac = mac))
                            showPicker = false
                        },
                        onDismiss = { showPicker = false },
                    )
                }
                if (draft.btMac.isNotBlank()) {
                    val context = LocalContext.current
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(
                            Icons.Filled.Bluetooth,
                            contentDescription = null,
                            tint = Glass.Teal,
                            modifier = Modifier.size(16.dp),
                        )
                        Spacer(Modifier.width(8.dp))
                        Text(
                            "已选：${bluetoothDeviceName(context, draft.btMac)}",
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.Teal,
                        )
                    }
                }
                Spacer(Modifier.height(10.dp))
                Text(
                    "点上面的按钮扫描附近蓝牙设备并直接点选，也可手工填 MAC。白名单语义：只有这台设备" +
                        "连上手机才触发录音，其它蓝牙（耳机、手环等）一律忽略。设备需先在系统蓝牙设置里" +
                        "配对——未配对的设备连不上，不会触发。更省事的做法是让 Mac 连上手机热点：" +
                        "同一网络后自动走局域网探测触发，无需填蓝牙。",
                    style = MaterialTheme.typography.bodySmall,
                    color = Glass.TextDim,
                )
                Spacer(Modifier.height(10.dp))
                Field("探测间隔（秒）", draft.probeIntervalSec.toString(), number = true) {
                    onChange(draft.copy(probeIntervalSec = it.toIntOrNull() ?: draft.probeIntervalSec))
                }
                Spacer(Modifier.height(10.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            "Mac 离线时自动转常驻",
                            style = MaterialTheme.typography.bodyMedium,
                            color = Glass.Text,
                        )
                        Text(
                            "Mac 与蓝牙都连不上时继续采集，Mac 回来自动恢复自动模式。"
                                + "关掉的话离线期间将完全不录音。",
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.TextDim,
                        )
                    }
                    Spacer(Modifier.width(10.dp))
                    Switch(
                        checked = draft.offlineAlwaysFallback,
                        onCheckedChange = { onChange(draft.copy(offlineAlwaysFallback = it)) },
                        colors = SwitchDefaults.colors(
                            checkedThumbColor = Glass.Teal,
                            checkedTrackColor = Glass.Teal.copy(alpha = 0.35f),
                            uncheckedThumbColor = Glass.TextDim,
                            uncheckedTrackColor = Glass.Surface2,
                        ),
                    )
                }
            }
        }
    }
}

private data class TriggerOptionMeta(val label: String, val description: String, val icon: ImageVector)

private fun triggerOptionMeta(mode: String): TriggerOptionMeta = when (mode) {
    TriggerMode.MANUAL -> TriggerOptionMeta(
        label = "手动",
        description = "点按钮开始，离线可用",
        icon = Icons.Filled.PlayArrow,
    )
    TriggerMode.AUTO -> TriggerOptionMeta(
        label = "自动",
        description = "Mac 在线 / 蓝牙连接",
        icon = Icons.Filled.Bluetooth,
    )
    TriggerMode.ALWAYS -> TriggerOptionMeta(
        label = "常驻",
        description = "持续采集，离线可用",
        icon = Icons.Filled.AllInclusive,
    )
    else -> TriggerOptionMeta(
        label = mode,
        description = "",
        icon = Icons.Filled.PlayArrow,
    )
}

/** 单选行：图标 + 标题 + 描述；选中时左侧描边 + accent 高亮。 */
@Composable
private fun ModeOption(
    icon: ImageVector,
    label: String,
    description: String,
    selected: Boolean,
    onClick: () -> Unit,
) {
    val borderColor = if (selected) Glass.Teal.copy(alpha = 0.5f) else Glass.Stroke
    val container = if (selected) Glass.Teal.copy(alpha = 0.10f) else Glass.Surface0
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(container)
            .border(1.dp, borderColor, RoundedCornerShape(12.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(36.dp)
                .clip(CircleShape)
                .background(if (selected) Glass.Teal.copy(alpha = 0.18f) else Glass.Surface2),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                icon,
                contentDescription = null,
                tint = if (selected) Glass.Teal else Glass.TextDim,
                modifier = Modifier.size(20.dp),
            )
        }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                label,
                style = MaterialTheme.typography.titleSmall,
                color = if (selected) Glass.Teal else Glass.Text,
            )
            Text(
                description,
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        }
        // 选中指示点
        Box(
            Modifier
                .size(if (selected) 14.dp else 8.dp)
                .clip(CircleShape)
                .background(if (selected) Glass.Teal else Glass.Surface2),
        )
    }
}

/**
 * 具名声纹角色管理：注册本人 + 同事角色。
 *
 * 为什么要多角色：工作环境里的对话对象基本是固定的一批人，甲乙丙丁这类匿名
 * 标签把「谁说的」这个关键信息丢掉了。注册后聚类会优先按具名锚点匹配，
 * 识别到就直接写名字，日报里不再出现匿名标签。
 */
/**
 * 声纹角色管理卡：已注册角色列表 + 「添加角色」弹窗。
 *
 * 注册是弹窗内的四步流程（填名字 → 朗读固定文本 → 录音 30 秒 → 试听后提交），
 * 而不是旧版「一个输入框 + 一个按钮」。原因是声纹样本录错了没法事后修正——
 * 一旦注册进去，后面一整天的说话人识别都会按错的样本走。分步 + 可试听 + 可重录，
 * 是为了让这一步一次做对。
 */
@Composable
private fun VoiceprintCard(vm: AppViewModel) {
    val speakers by vm.speakers.collectAsStateWithLifecycle()
    val pending by vm.pendingEnrolls.collectAsStateWithLifecycle()
    val speakerMsg by vm.speakerMessage.collectAsStateWithLifecycle()
    var showAdd by remember { mutableStateOf(false) }

    // 进页面拉一次：角色列表 + 本地待补交队列
    LaunchedEffect(Unit) { vm.refreshSpeakers(); vm.refreshPendingEnrolls() }
    LaunchedEffect(speakerMsg) {
        if (speakerMsg != null) {
            kotlinx.coroutines.delay(2500)
            vm.clearSpeakerMessage()
        }
    }

    GlassCard {
        SectionTitle("声纹角色", accent = Glass.Teal)
        Spacer(Modifier.height(6.dp))
        Text(
            // 必须先讲清「要录多久、录什么」：用户点进这一栏如果没有这句，
            // 只会看到「添加角色」四个字，不知道要准备 30 秒的朗读
            // （2026-09-16 用户反馈补上）。文本本身由添加角色弹窗给出
            // （Constants.ENROLL_SCRIPT），与服务端 voiceprint.py 那份逐字一致。
            "注册一次需要该角色连续朗读 30 秒：点「添加角色」后会给出这段固定文字，"
                + "照着念完即可（内容固定是为了让声纹稳定，各读各的会让锚点偏掉）。"
                + "注册后 Mac 才能在多人对话中区分出每个人。本人语料只进数字人训练集；"
                + "同事注册后日报直接显示姓名，不再出现甲乙丙丁。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        if (speakers.isNotEmpty()) {
            Spacer(Modifier.height(12.dp))
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                speakers.forEach { n ->
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(Glass.Surface2)
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            Icons.Filled.Mic,
                            contentDescription = null,
                            tint = if (n == SELF_LABEL) Glass.Teal else Glass.Indigo,
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(Modifier.width(8.dp))
                        Text(
                            n,
                            style = MaterialTheme.typography.bodyMedium,
                            color = Glass.Text,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            "删除",
                            style = MaterialTheme.typography.labelLarge,
                            color = Glass.Red,
                            modifier = Modifier
                                .clip(RoundedCornerShape(8.dp))
                                .clickable { vm.deleteSpeaker(n) }
                                .padding(horizontal = 8.dp, vertical = 4.dp),
                        )
                    }
                }
            }
        }

        // 离线录下、还没提交成功的样本：让用户知道它不会被丢
        if (pending.isNotEmpty()) {
            Spacer(Modifier.height(10.dp))
            InsetCard {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.CloudUpload,
                        contentDescription = null,
                        tint = Glass.Amber,
                        modifier = Modifier.size(18.dp),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "待提交：${pending.joinToString("、")}\n连上 Mac 后自动补交，无需重录",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.Amber,
                    )
                }
            }
        }

        speakerMsg?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, style = MaterialTheme.typography.bodySmall, color = Glass.Amber)
        }

        Spacer(Modifier.height(14.dp))
        PrimaryButton(
            "添加角色",
            onClick = { showAdd = true },
            accent = Glass.Teal,
            modifier = Modifier.fillMaxWidth(),
        )
    }

    if (showAdd) {
        AddSpeakerDialog(vm) { showAdd = false }
    }
}

/**
 * 行业词表选择。
 *
 * 说清楚它到底改了什么、没改什么，避免用户以为选了就能把昨天的日报改对：
 * - 改：之后的转写热词偏置、文本纠偏用的术语表、Agent 可见的术语表
 * - 不改：已生成的历史日报（要重算得在 Mac 上跑 rebuild_report.py）
 */
@Composable
private fun IndustryCard(vm: AppViewModel) {
    val info by vm.industry.collectAsStateWithLifecycle()
    val msg by vm.industryMessage.collectAsStateWithLifecycle()

    LaunchedEffect(Unit) { vm.refreshIndustries() }
    LaunchedEffect(msg) {
        if (msg != null) {
            kotlinx.coroutines.delay(2500)
            vm.clearIndustryMessage()
        }
    }

    GlassCard {
        SectionTitle("行业词表", accent = Glass.Teal)
        Spacer(Modifier.height(6.dp))
        Text(
            "告诉 Mac 你在哪个行业说话，转写和整理时会按这个行业的术语纠同音字"
                + "（比如装修场景的「吊顶」容易被写成「杯盖」）。只影响之后的录音。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        Spacer(Modifier.height(12.dp))
        if (info == null) {
            Text(
                "还没连上 Mac，连上后可在这里切换行业",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        } else {
            val data = info!!
            // 竖排一行一个：行业名是中文字号较大的标签，横排 chip 会挤
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                data.available.forEach { ind ->
                    val selected = ind.key == data.current
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(
                                if (selected) Glass.Teal.copy(alpha = 0.12f) else Glass.Surface2
                            )
                            .border(
                                1.dp,
                                if (selected) Glass.Teal.copy(alpha = 0.5f) else Glass.Stroke,
                                RoundedCornerShape(10.dp),
                            )
                            .clickable(enabled = !selected && !data.pinnedByEnv) {
                                vm.setIndustry(ind.key)
                            }
                            .padding(horizontal = 12.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            ind.label,
                            style = MaterialTheme.typography.bodyMedium,
                            color = if (selected) Glass.Teal else Glass.Text,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            if (selected) "当前" else "${ind.terms} 词",
                            style = MaterialTheme.typography.labelSmall,
                            color = if (selected) Glass.Teal else Glass.TextDim,
                        )
                    }
                }
            }
            if (data.pinnedByEnv) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Mac 端用环境变量锁定了行业，手机端切换无效",
                    style = MaterialTheme.typography.bodySmall,
                    color = Glass.Amber,
                )
            }
        }

        msg?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, style = MaterialTheme.typography.bodySmall, color = Glass.Amber)
        }
    }
}

/** 弹窗内的步骤。状态真相在 ViewModel（enroll），这里只是把展示步骤对齐过去。 */
private enum class EnrollStep { NAME, READY, RECORDING, REVIEW, DONE }

@Composable
private fun AddSpeakerDialog(vm: AppViewModel, onDismiss: () -> Unit) {
    val state by vm.enroll.collectAsStateWithLifecycle()
    val playing by vm.samplePlaying.collectAsStateWithLifecycle()
    var name by remember { mutableStateOf("") }
    var step by remember { mutableStateOf(EnrollStep.NAME) }
    var recordedFile by remember { mutableStateOf<java.io.File?>(null) }

    // ViewModel 状态 → 展示步骤。录音/提交是异步的，弹窗不能自己瞎猜进度。
    LaunchedEffect(state) {
        step = when (val s = state) {
            is AppViewModel.EnrollState.Recording -> EnrollStep.RECORDING
            is AppViewModel.EnrollState.Recorded -> {
                recordedFile = s.file
                EnrollStep.REVIEW
            }
            is AppViewModel.EnrollState.Uploading -> EnrollStep.REVIEW
            is AppViewModel.EnrollState.Success,
            is AppViewModel.EnrollState.Pending,
            -> EnrollStep.DONE
            else -> step
        }
    }
    DisposableEffect(Unit) {
        // 中途关闭：已经录好但没提交的样本要清掉，否则 cache 里堆一堆孤儿 wav
        onDispose {
            vm.stopSample()
            if (step != EnrollStep.DONE) vm.cancelEnroll()
        }
    }

    val label = name.trim().ifBlank { SELF_LABEL }
    AlertDialog(
        onDismissRequest = { vm.resetEnroll(); onDismiss() },
        containerColor = Glass.Surface1,
        title = {
            Text(
                when (step) {
                    EnrollStep.NAME -> "添加角色"
                    EnrollStep.READY -> "准备录音"
                    EnrollStep.RECORDING -> "正在录音"
                    EnrollStep.REVIEW -> "确认样本"
                    EnrollStep.DONE -> "完成"
                },
                color = Glass.Text,
            )
        },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                when (step) {
                    EnrollStep.NAME -> {
                        Text(
                            "留空表示注册本人；填同事姓名则日报里直接显示该姓名。",
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.TextDim,
                        )
                        Spacer(Modifier.height(12.dp))
                        OutlinedTextField(
                            value = name,
                            onValueChange = { name = it.take(16) },
                            label = { Text("角色名（留空 = 本人）") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                            colors = OutlinedTextFieldDefaults.colors(
                                focusedTextColor = Glass.Text,
                                unfocusedTextColor = Glass.Text,
                                focusedBorderColor = Glass.Teal,
                                unfocusedBorderColor = Glass.Surface2,
                                focusedLabelColor = Glass.Teal,
                                unfocusedLabelColor = Glass.TextDim,
                                cursorColor = Glass.Teal,
                            ),
                        )
                    }

                    EnrollStep.READY -> {
                        Text(
                            "请朗读下面这段文字，正常语速，约 30 秒。内容固定是为了让声纹稳定——"
                                + "同一段文字、同一个人的声音特征才可比较。",
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.TextDim,
                        )
                        Spacer(Modifier.height(12.dp))
                        InsetCard {
                            Text(
                                ENROLL_SCRIPT,
                                style = MaterialTheme.typography.bodyMedium,
                                color = Glass.Text,
                            )
                        }
                        (state as? AppViewModel.EnrollState.Error)?.let {
                            Spacer(Modifier.height(10.dp))
                            Text(it.message, style = MaterialTheme.typography.bodySmall, color = Glass.Red)
                        }
                    }

                    EnrollStep.RECORDING -> {
                        val left = (state as? AppViewModel.EnrollState.Recording)?.secondsLeft ?: 0
                        val progress = (ENROLL_SECONDS - left).toFloat() / ENROLL_SECONDS
                        LinearProgressIndicator(
                            progress = { progress.coerceIn(0f, 1f) },
                            modifier = Modifier.fillMaxWidth(),
                            color = Glass.Teal,
                            trackColor = Glass.Surface2,
                        )
                        Spacer(Modifier.height(12.dp))
                        Text(
                            "正在录制「$label」的声纹…… 剩余 $left 秒",
                            style = MaterialTheme.typography.bodyMedium,
                            color = Glass.Teal,
                        )
                        Spacer(Modifier.height(10.dp))
                        Text(
                            ENROLL_SCRIPT,
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.TextDim,
                        )
                    }

                    EnrollStep.REVIEW -> {
                        val f = recordedFile
                        Text(
                            "已录好 30 秒样本。建议先试听一遍，确认没有杂音、不是别人的声音，再提交。",
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.TextDim,
                        )
                        Spacer(Modifier.height(12.dp))
                        if (f != null) {
                            GhostButton(
                                if (playing) "停止试听" else "试听样本",
                                onClick = { if (playing) vm.stopSample() else vm.playSample(f) },
                                accent = Glass.Indigo,
                                height = 44.dp,
                                modifier = Modifier.fillMaxWidth(),
                            )
                        }
                        (state as? AppViewModel.EnrollState.Error)?.let {
                            Spacer(Modifier.height(10.dp))
                            Text(it.message, style = MaterialTheme.typography.bodySmall, color = Glass.Red)
                        }
                        if (state is AppViewModel.EnrollState.Uploading) {
                            Spacer(Modifier.height(10.dp))
                            Text(
                                "正在提交到 Mac……",
                                style = MaterialTheme.typography.bodySmall,
                                color = Glass.Indigo,
                            )
                        }
                    }

                    EnrollStep.DONE -> {
                        val pending = state is AppViewModel.EnrollState.Pending
                        Text(
                            if (pending) {
                                "「$label」的样本已保存在手机上。现在连不上 Mac，" +
                                    "等回到 Mac 旁（或 Mac 开机后）会自动提交，不用重录。"
                            } else {
                                "「$label」注册成功。之后的采集里，Mac 识别出这个声音" +
                                    "就会直接记为「$label」。"
                            },
                            style = MaterialTheme.typography.bodyMedium,
                            color = if (pending) Glass.Amber else Glass.Green,
                        )
                    }
                }
            }
        },
        confirmButton = {
            when (step) {
                EnrollStep.NAME -> PrimaryButton(
                    "下一步", height = 44.dp,
                    onClick = { vm.resetEnroll(); step = EnrollStep.READY },
                )
                EnrollStep.READY -> PrimaryButton(
                    "开始录音（30 秒）", height = 44.dp,
                    onClick = { vm.recordEnrollSample(label) },
                )
                EnrollStep.RECORDING -> GhostButton(
                    "取消录音", height = 44.dp, accent = Glass.Red,
                    onClick = { vm.cancelEnroll(); step = EnrollStep.READY },
                )
                EnrollStep.REVIEW -> {
                    val f = recordedFile
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        GhostButton(
                            "重录", height = 44.dp, accent = Glass.Amber,
                            onClick = { vm.cancelEnroll(); step = EnrollStep.READY },
                        )
                        PrimaryButton(
                            "提交", height = 44.dp,
                            enabled = f != null && state !is AppViewModel.EnrollState.Uploading,
                            onClick = { if (f != null) vm.submitEnrollSample(label, f) },
                        )
                    }
                }
                EnrollStep.DONE -> PrimaryButton(
                    "完成", height = 44.dp,
                    onClick = { vm.resetEnroll(); onDismiss() },
                )
            }
        },
        dismissButton = {
            if (step != EnrollStep.DONE) {
                TextButton(onClick = { vm.cancelEnroll(); onDismiss() }) {
                    Text("取消", color = Glass.TextDim)
                }
            }
        },
    )
}

/**
 * 数据备份 / 导出 / 恢复（2026-09-15 补全「导入导出」）。
 *
 * 三个动作，语义要分清：
 * - **备份到服务端**：在 Mac 上打一份归档（不动现有数据）
 * - **导出到本机**：把服务端某份归档下载到手机，并可分享出去
 * - **从备份文件恢复**：把本机的一份归档传回服务端并**整体覆盖**（破坏性）
 *
 * 恢复必须先过二次确认弹窗：它是唯一会覆盖线上数据的入口，而误触的代价是
 * 整天语料被替换。服务端收到后还会自动留一份「恢复前快照」，两者一起构成退路。
 */
@Composable
private fun BackupCard(vm: AppViewModel) {
    val state by vm.backup.collectAsStateWithLifecycle()
    val transfer by vm.transfer.collectAsStateWithLifecycle()
    val backups by vm.backups.collectAsStateWithLifecycle()
    val listMsg by vm.backupListMsg.collectAsStateWithLifecycle()
    var pendingRestore by remember { mutableStateOf<android.net.Uri?>(null) }

    // 进入设置页时顺手拉一次服务端备份列表：用户走到这一栏多半就是想导出或恢复，
    // 让他先点一下「刷新」才知道有什么，是多余的一步。
    LaunchedEffect(Unit) { vm.refreshBackups() }

    // 选择本机备份包。用 OpenDocument（不是 GetContent）：拿到的是持久的、
    // 可再读的内容 Uri，且由系统文件选择器给授权，不需要任何存储权限。
    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> if (uri != null) pendingRestore = uri }

    GlassCard {
        SectionTitle("数据备份与导出", accent = Glass.Indigo)
        Spacer(Modifier.height(6.dp))
        Text(
            "把服务端已生成的文字备份、语料导出与数据库打包归档，随时可恢复，不怕误删。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )
        Spacer(Modifier.height(14.dp))
        when (val s = state) {
            is AppViewModel.BackupState.Idle -> {
                PrimaryButton(
                    "备份到服务端",
                    onClick = { vm.backupNow() },
                    accent = Glass.Indigo,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            is AppViewModel.BackupState.Running -> {
                InsetCard {
                    Text(
                        "正在打包备份…… 数据量大时可能需要几十秒",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.Indigo,
                    )
                }
            }
            is AppViewModel.BackupState.Ok -> {
                InsetCard {
                    Text(
                        s.message,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.Green,
                    )
                }
                Spacer(Modifier.height(10.dp))
                PrimaryButton(
                    "再备一次",
                    onClick = { vm.backupNow() },
                    accent = Glass.Indigo,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            is AppViewModel.BackupState.Fail -> {
                InsetCard {
                    Text(
                        "备份失败：${s.message}",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.Red,
                    )
                }
                Spacer(Modifier.height(10.dp))
                GhostButton(
                    "重试",
                    onClick = { vm.backupNow() },
                    accent = Glass.Amber,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }

        // ---------- 服务端已有归档：导出到本机 ----------
        Spacer(Modifier.height(16.dp))
        HorizontalDivider(color = Glass.Stroke)
        Spacer(Modifier.height(12.dp))
        GhostButton(
            "刷新备份列表",
            onClick = { vm.refreshBackups() },
            accent = Glass.Indigo,
            modifier = Modifier.fillMaxWidth(),
        )
        listMsg?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, style = MaterialTheme.typography.bodySmall, color = Glass.TextDim)
        }
        if (backups.isEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(
                "还没有备份。点上面的「备份到服务端」生成第一份。",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextFaint,
            )
        } else {
            backups.forEach { b ->
                val name = b["file"].orEmpty()
                Spacer(Modifier.height(10.dp))
                InsetCard {
                    Text(name, style = MaterialTheme.typography.bodySmall, color = Glass.Text)
                    Spacer(Modifier.height(2.dp))
                    Text(
                        "${humanSize(b["size"]?.toLongOrNull() ?: 0L)} · ${b["mtime"].orEmpty()}",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.TextDim,
                    )
                    Spacer(Modifier.height(8.dp))
                    GhostButton(
                        "导出到本机",
                        onClick = { vm.exportBackup(name) },
                        accent = Glass.Teal,
                        enabled = transfer !is AppViewModel.TransferState.Busy,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        }

        // ---------- 从本机文件恢复（破坏性，二次确认） ----------
        Spacer(Modifier.height(16.dp))
        HorizontalDivider(color = Glass.Stroke)
        Spacer(Modifier.height(12.dp))
        Text("从备份文件恢复", style = MaterialTheme.typography.bodyMedium, color = Glass.Text)
        Spacer(Modifier.height(4.dp))
        Text(
            "选手机里的一份备份包传回服务端。注意：这会整体替换服务端该空间的数据"
                + "（文字、语料、数据库）。服务端在动手前会自动再存一份快照，可退回。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )
        Spacer(Modifier.height(10.dp))
        GhostButton(
            "选择备份文件…",
            onClick = {
                // 各机型对 zip 的 MIME 标注不一致（有的是 application/zip，有的是
                // application/octet-stream，还有的压根没有），只给 zip 会让选择器
                // 里一份文件都显示不出来。放宽到 */*，格式由服务端校验。
                picker.launch(arrayOf("application/zip", "application/octet-stream", "*/*"))
            },
            accent = Glass.Amber,
            enabled = transfer !is AppViewModel.TransferState.Busy,
            modifier = Modifier.fillMaxWidth(),
        )

        when (val t = transfer) {
            is AppViewModel.TransferState.Idle -> Unit
            is AppViewModel.TransferState.Busy -> {
                Spacer(Modifier.height(10.dp))
                InsetCard {
                    Text(
                        "正在${t.what}……大文件可能需要一会儿，请勿离开此页面",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Glass.Indigo,
                    )
                }
            }
            is AppViewModel.TransferState.Ok -> {
                Spacer(Modifier.height(10.dp))
                InsetCard {
                    Text(t.message, style = MaterialTheme.typography.bodyMedium, color = Glass.Green)
                }
                t.file?.let { f ->
                    Spacer(Modifier.height(8.dp))
                    GhostButton(
                        "分享这份备份",
                        onClick = { vm.shareExported(f) },
                        accent = Glass.Teal,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                Spacer(Modifier.height(8.dp))
                GhostButton(
                    "知道了",
                    onClick = { vm.resetTransfer() },
                    accent = Glass.TextDim,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            is AppViewModel.TransferState.Fail -> {
                Spacer(Modifier.height(10.dp))
                InsetCard {
                    Text(t.message, style = MaterialTheme.typography.bodyMedium, color = Glass.Red)
                }
                Spacer(Modifier.height(8.dp))
                GhostButton(
                    "知道了",
                    onClick = { vm.resetTransfer() },
                    accent = Glass.Amber,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }
    }

    // 恢复二次确认。放在卡片外：AlertDialog 是独立窗口，塞进 GlassCard 的
    // 布局树里在窄屏/大字号下会被裁掉。
    pendingRestore?.let {
        AlertDialog(
            onDismissRequest = { pendingRestore = null },
            title = { Text("确认恢复？") },
            text = {
                Text(
                    "这会用所选备份整体替换服务端该空间的数据（文字、语料、数据库），"
                        + "当前数据将被覆盖。服务端会在动手前自动留一份快照，"
                        + "但仍请确认选对了文件。"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    val u = pendingRestore
                    pendingRestore = null
                    if (u != null) vm.importBackup(u)
                }) { Text("确认恢复", color = Glass.Red) }
            },
            dismissButton = {
                TextButton(onClick = { pendingRestore = null }) { Text("取消") }
            },
        )
    }
}

/** 字节数转可读文案（与 MacClient 里那份同规则，各自包内私有，避免跨层依赖）。 */
private fun humanSize(bytes: Long): String = when {
    bytes >= 1024L * 1024 * 1024 -> "%.2f GB".format(bytes / 1024.0 / 1024 / 1024)
    bytes >= 1024L * 1024 -> "%.1f MB".format(bytes / 1024.0 / 1024)
    bytes >= 1024L -> "%.0f KB".format(bytes / 1024.0)
    else -> "$bytes B"
}

/** 已配对蓝牙设备选择弹窗：把地址填进备用触发 MAC 字段 */
private data class BtDevice(val name: String, val mac: String, val bonded: Boolean)

/** 扫描/读取蓝牙设备所需的权限。Android 12（SDK 31）前后权限模型完全不同。 */
private fun btPermissions(): Array<String> = if (Build.VERSION.SDK_INT >= 31) {
    arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
} else {
    // SDK 30 及以下：经典蓝牙发现走 BLUETOOTH_ADMIN，且必须配合位置权限系统才允许扫描
    arrayOf(
        Manifest.permission.BLUETOOTH,
        Manifest.permission.BLUETOOTH_ADMIN,
        Manifest.permission.ACCESS_FINE_LOCATION,
    )
}

private fun btPermissionsGranted(context: android.content.Context): Boolean =
    btPermissions().all { context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }

private fun bluetoothDeviceName(context: android.content.Context, mac: String): String {
    if (mac.isBlank()) return ""
    if (!btPermissionsGranted(context)) return mac
    return runCatching {
        val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
        adapter?.bondedDevices?.firstOrNull { it.address.equals(mac, ignoreCase = true) }?.name ?: mac
    }.getOrDefault(mac)
}

private fun loadBondedDevices(context: android.content.Context): List<BtDevice> {
    val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter ?: return emptyList()
    return runCatching {
        adapter.bondedDevices.orEmpty()
            .mapNotNull { d -> d.address?.let { BtDevice(d.name ?: "未命名设备", it, true) } }
            .sortedBy { it.name }
    }.getOrDefault(emptyList())
}

/**
 * 蓝牙设备选择器：已配对设备（可直接用于触发）+ 扫描附近设备（发现后仍需配对）。
 *
 * 扫描用经典蓝牙 startDiscovery + ACTION_FOUND 广播：目标设备（手机/耳机/车机）都是经典
 * 蓝牙，BLE 扫描扫不到它们，且 BLE 结果里拿不到可直接用于 ACL 触发的配对态。
 */
@Composable
private fun BluetoothPickerDialog(
    current: String,
    onPick: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val context = LocalContext.current
    var granted by remember { mutableStateOf(btPermissionsGranted(context)) }
    var scanning by remember { mutableStateOf(false) }
    var found by remember { mutableStateOf(listOf<BtDevice>()) }
    var notice by remember { mutableStateOf("") }

    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        granted = btPermissions().all { result[it] == true }
        if (!granted) notice = "未授予蓝牙权限，无法扫描设备。请在系统设置里允许「附近设备」。"
    }

    val bonded = remember(granted) {
        if (!granted) emptyList() else loadBondedDevices(context)
    }

    DisposableEffect(granted, scanning) {
        if (!granted || !scanning) return@DisposableEffect onDispose {}
        val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
        if (adapter == null) {
            notice = "本机不支持蓝牙。"
            scanning = false
            return@DisposableEffect onDispose {}
        }
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: android.content.Context, intent: Intent) {
                when (intent.action) {
                    BluetoothDevice.ACTION_FOUND -> {
                        val dev: BluetoothDevice? = if (Build.VERSION.SDK_INT >= 33) {
                            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
                        } else {
                            @Suppress("DEPRECATION")
                            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
                        }
                        val mac = dev?.address ?: return
                        val name = runCatching { dev.name }.getOrNull()
                            .takeUnless { it.isNullOrBlank() } ?: "未命名设备"
                        val bondedFlag = runCatching { dev.bondState == BluetoothDevice.BOND_BONDED }
                            .getOrDefault(false)
                        val entry = BtDevice(name, mac, bondedFlag)
                        found = (found + entry)
                            .distinctBy { it.mac }
                            .sortedWith(compareBy({ !it.bonded }, { it.name }))
                    }
                    BluetoothAdapter.ACTION_DISCOVERY_FINISHED -> scanning = false
                }
            }
        }
        val filter = IntentFilter().apply {
            addAction(BluetoothDevice.ACTION_FOUND)
            addAction(BluetoothAdapter.ACTION_DISCOVERY_FINISHED)
        }
        context.registerReceiver(receiver, filter)
        found = emptyList()
        notice = "正在扫描附近设备…"
        if (runCatching { adapter.isDiscovering }.getOrDefault(false)) {
            runCatching { adapter.cancelDiscovery() }
        }
        val started = runCatching { adapter.startDiscovery() }.getOrDefault(false)
        if (!started) {
            notice = "扫描启动失败，请先打开系统蓝牙。"
            scanning = false
        }
        onDispose {
            runCatching { context.unregisterReceiver(receiver) }
            runCatching { adapter.cancelDiscovery() }
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("选择蓝牙设备", color = Glass.Text) },
        text = {
            Column {
                if (notice.isNotBlank()) {
                    Text(
                        notice,
                        style = MaterialTheme.typography.bodySmall,
                        color = if (scanning) Glass.Teal else Glass.Amber,
                    )
                    Spacer(Modifier.height(6.dp))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    GhostButton(
                        if (scanning) "扫描中…" else "扫描附近设备",
                        onClick = {
                            if (granted) {
                                found = emptyList()
                                notice = ""
                                scanning = true
                            } else {
                                launcher.launch(btPermissions())
                            }
                        },
                        enabled = !scanning,
                        accent = Glass.Teal,
                        modifier = Modifier.weight(1f),
                    )
                    if (scanning) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(20.dp),
                            strokeWidth = 2.dp,
                            color = Glass.Teal,
                        )
                    }
                }
                Spacer(Modifier.height(8.dp))

                val shown = (bonded + found).distinctBy { it.mac }
                    .sortedWith(compareBy({ !it.bonded }, { it.name }))
                when {
                    !granted -> Text(
                        "需要「附近设备」权限才能读取蓝牙设备列表。",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.Amber,
                    )
                    shown.isEmpty() -> Text(
                        "暂无设备。已配对设备会直接列出；也可点「扫描附近设备」查找新设备。",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.TextDim,
                    )
                    else -> {
                        LazyColumn(Modifier.height(300.dp)) {
                            items(shown, key = { it.mac }) { dev ->
                                val selected = dev.mac.equals(current, ignoreCase = true)
                                Row(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .clip(RoundedCornerShape(8.dp))
                                        .background(
                                            if (selected) Glass.Teal.copy(alpha = 0.14f)
                                            else androidx.compose.ui.graphics.Color.Transparent
                                        )
                                        .clickable { onPick(dev.mac) }
                                        .padding(horizontal = 8.dp, vertical = 10.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Icon(
                                        Icons.Filled.Bluetooth,
                                        contentDescription = null,
                                        tint = if (dev.bonded) Glass.Teal else Glass.TextFaint,
                                        modifier = Modifier.size(18.dp),
                                    )
                                    Spacer(Modifier.width(10.dp))
                                    Column(Modifier.weight(1f)) {
                                        Text(
                                            dev.name,
                                            style = MaterialTheme.typography.bodyMedium,
                                            color = Glass.Text,
                                        )
                                        Text(
                                            dev.mac,
                                            style = MaterialTheme.typography.labelSmall,
                                            color = Glass.TextFaint,
                                        )
                                    }
                                    Text(
                                        when {
                                            selected -> "已选"
                                            dev.bonded -> "已配对"
                                            else -> "未配对"
                                        },
                                        style = MaterialTheme.typography.labelSmall,
                                        color = when {
                                            selected -> Glass.Teal
                                            dev.bonded -> Glass.TextDim
                                            else -> Glass.Amber
                                        },
                                    )
                                }
                            }
                        }
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "标「未配对」的设备需先在系统蓝牙设置里配对，否则连不上、不会触发录音。",
                            style = MaterialTheme.typography.labelSmall,
                            color = Glass.TextFaint,
                        )
                    }
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    "选中后仅该设备连入时触发录音，其它蓝牙一律忽略。",
                    style = MaterialTheme.typography.labelSmall,
                    color = Glass.TextFaint,
                )
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("关闭", color = Glass.Teal) }
        },
        containerColor = Glass.Surface3,
    )
}

/**
 * 后台保活（2026-09-27 新增）。
 *
 * 起因是一次真实的 3 天断采：手机连续开机 36 天没重启，App 在 09-24 22:24
 * 被划掉清后台（系统退出记录：`reason=13 OTHER KILLS BY SYSTEM / SwipeUpClean`），
 * 之后 3 天没有任何进程重启——采集彻底停摆，服务端一条数据都没有。
 *
 * 三个闸门缺一不可，且**都不是 App 能自己打开的**：
 * ① 电池优化「无限制」：不进白名单，Doze/App Standby 会持续压制（可一键跳转，
 *    状态可读 `PowerManager.isIgnoringBatteryOptimizations`）；
 * ② 系统「自启动」：小米/红米（MIUI/HyperOS）有独立的自启动开关，不在里面
 *    开机不拉起、被清理后也不拉起。系统没给第三方读接口，只能跳过去让用户自己开；
 * ③ 锁定后台任务：Android 9+ 把「最近任务上划」当作用户明确要停 → 系统**不再**
 *    重启 START_STICKY 服务，任何 WorkManager/闹钟也一并失去意义。多任务界面
 *    下拉卡片点锁图标可避免误清。
 *
 * 为什么放在设置页而不是弹窗：本项目的形态是「配好就不再打开界面」，弹窗只在
 * 装机那天能看到；设置页是之后唯一会回来的地方，且用户遇到「几天没数据」时
 * 第一反应就是来这里看。
 */
@Composable
private fun KeepAliveCard() {
    val ctx = LocalContext.current
    var ignoring by remember { mutableStateOf(isIgnoringBatteryOptimizations(ctx)) }
    // 从系统设置页返回时立刻刷新状态：用户点「去设置」改完回来，这一栏必须马上变绿，
    // 否则他会以为「点了没用」而反复点（实测这类反馈最多）。
    val owner = LocalLifecycleOwner.current
    DisposableEffect(owner) {
        val obs = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) ignoring = isIgnoringBatteryOptimizations(ctx)
        }
        owner.lifecycle.addObserver(obs)
        onDispose { owner.lifecycle.removeObserver(obs) }
    }

    GlassCard {
        SectionTitle("后台保活（重要）", accent = Glass.Amber)
        Text(
            "本机是长期驻留设备，App 必须在后台一直活着。下面三项是 Android / 小米的"
                + "省电闸门，任何一项没开都可能出现「好几天没有数据」。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        Spacer(Modifier.height(12.dp))
        // ① 电池优化
        Row(verticalAlignment = Alignment.CenterVertically) {
            Pill(
                if (ignoring) "① 电池优化已放行" else "① 电池优化未放行",
                tone = if (ignoring) PillTone.Positive else PillTone.Pending,
                showDot = true,
            )
            Spacer(Modifier.weight(1f))
            GhostButton(
                if (ignoring) "已设置" else "去设置",
                onClick = { openBatteryWhitelist(ctx) },
                enabled = !ignoring,
                accent = if (ignoring) Glass.Teal else Glass.Amber,
            )
        }
        Spacer(Modifier.height(4.dp))
        Text(
            "选「允许」后系统不再在待机时压制本 App（点按钮会直接弹出系统授权框）。",
            style = MaterialTheme.typography.labelSmall,
            color = Glass.TextFaint,
        )

        Spacer(Modifier.height(14.dp))
        // ② 自启动（读不到状态，只能引导）
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "② 允许自启动",
                style = MaterialTheme.typography.bodyMedium,
                color = Glass.Text,
            )
            Spacer(Modifier.weight(1f))
            GhostButton(
                "去设置",
                onClick = { openAutoStartSettings(ctx) },
                accent = Glass.Indigo,
            )
        }
        Spacer(Modifier.height(4.dp))
        Text(
            "在「自启动管理」里把本应用打开。关着的话：开机不拉起、被清理后也不拉起。"
                + "系统不提供读取接口，请自行确认一次。",
            style = MaterialTheme.typography.labelSmall,
            color = Glass.TextFaint,
        )

        Spacer(Modifier.height(14.dp))
        // ③ 任务锁定
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "③ 锁定后台任务",
                style = MaterialTheme.typography.bodyMedium,
                color = Glass.Text,
            )
        }
        Spacer(Modifier.height(4.dp))
        Text(
            "在「最近任务」界面把本应用的卡片**下拉**找到锁图标点一下。"
                + "Android 9 起，上划清掉应用 = 用户明确要停，系统不会自动重启它，"
                + "之后采集就一直是停的（这正是 09-24 那次 3 天断采的原因）。",
            style = MaterialTheme.typography.labelSmall,
            color = Glass.TextFaint,
        )
    }
}

/** 是否已被系统放行电池优化（Doze / App Standby 不再压制）。 */
private fun isIgnoringBatteryOptimizations(ctx: android.content.Context): Boolean = runCatching {
    val pm = ctx.getSystemService(android.content.Context.POWER_SERVICE) as? android.os.PowerManager
    pm?.isIgnoringBatteryOptimizations(ctx.packageName) == true
}.getOrDefault(false)

/** 一键跳系统「电池优化」授权框；个别 ROM 没有该页时退回优化列表。 */
private fun openBatteryWhitelist(ctx: android.content.Context) {
    val pkg = Uri.parse("package:${ctx.packageName}")
    val direct = Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, pkg)
    if (!launch(ctx, direct)) {
        launch(ctx, Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
    }
}

/**
 * 打开「自启动管理」。小米/红米走 MIUI 安全中心的私有页，其它 ROM 退回
 * 应用详情页（用户仍可在「电池 / 后台运行」里找到对应开关）。
 */
private fun openAutoStartSettings(ctx: android.content.Context) {
    val miui = Intent().setComponent(
        android.content.ComponentName(
            "com.miui.securitycenter",
            "com.miui.permcenter.autostart.AutoStartManagementActivity",
        )
    )
    if (launch(ctx, miui)) return
    launch(
        ctx,
        Intent(
            android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.parse("package:${ctx.packageName}"),
        )
    )
}

/** 统一 startActivity（补 NEW_TASK，失败返回 false 以便退回上一级方案）。 */
private fun launch(ctx: android.content.Context, intent: Intent): Boolean = runCatching {
    ctx.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    true
}.getOrDefault(false)

/**
 * 采集时段（2026-09-15 新增）。
 *
 * 长期驻留的终端不必 7×24 录音——夜里、闭店后录到的只有环境噪声，
 * 白费电与存储，还给服务端塞一堆无意义音频。这里给三档预设 + 手填，
 * 解析与判定规则集中在 [CaptureWindow]，服务端不参与（纯客户端闸门）。
 *
 * 校验就地做而不是留到保存时：格式写错会静默退化成「全天」，
 * 用户以为自己关掉了夜间采集，实际还在录——这种无声失效最难发现。
 * 所以当场红字提示，但**不阻止保存**（宁可多录，不可漏录）。
 */
@Composable
private fun CaptureWindowCard(draft: AppSettings, onChange: (AppSettings) -> Unit) {
    val nowMin = CaptureWindow.nowMinuteOfDay()
    val activeNow = CaptureWindow.isActive(draft.captureWindows, nowMin)
    val err = CaptureWindow.validate(draft.captureWindows)
    val presets = listOf(
        CaptureWindow.ALL_DAY to "全天",
        "08:30-12:00,13:30-18:00" to "营业时段",
        "09:00-12:00,14:00-18:00" to "上下午",
    )
    GlassCard {
        SectionTitle("采集时段", accent = Glass.Teal)
        Text(
            "只在设定的时段采集。非采集时段不录音、不上报，但服务保持常驻，"
                + "进入时段后自动开始，全程不需要打开这个 App。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            presets.forEach { (spec, label) ->
                IntervalChip(
                    text = label,
                    selected = CaptureWindow.normalize(draft.captureWindows) ==
                        CaptureWindow.normalize(spec),
                    modifier = Modifier.weight(1f),
                ) { onChange(draft.copy(captureWindows = CaptureWindow.normalize(spec))) }
            }
        }

        Spacer(Modifier.height(12.dp))
        Field(
            "自定义时段",
            draft.captureWindows,
            placeholder = "留空 = 全天，如 09:00-12:00,14:00-18:00",
        ) { onChange(draft.copy(captureWindows = it)) }

        if (err != null) {
            Spacer(Modifier.height(6.dp))
            Text(err, style = MaterialTheme.typography.bodySmall, color = Glass.Red)
            Text(
                "格式不对时按「全天」处理（宁可多录，不可漏录）",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        }

        Spacer(Modifier.height(10.dp))
        Text(
            "当前生效：" + CaptureWindow.label(draft.captureWindows),
            style = MaterialTheme.typography.bodySmall,
            color = Glass.Text,
        )
        Spacer(Modifier.height(4.dp))
        val until = CaptureWindow.minutesUntilActive(draft.captureWindows, nowMin)
        Text(
            if (activeNow) "现在处于采集时段，正在采集"
            else "现在不在采集时段，服务待命" +
                (if (until > 0) "，约 $until 分钟后开始采集" else ""),
            style = MaterialTheme.typography.bodySmall,
            color = if (activeNow) Glass.Teal else Glass.TextDim,
        )
    }
}

@Composable
private fun DailyFlowCard(draft: AppSettings, onChange: (AppSettings) -> Unit) {
    GlassCard {
        SectionTitle("每日流程", accent = Glass.Amber)
        Spacer(Modifier.height(12.dp))
        Field(
            "每日批量处理时间",
            formatMinuteOfDay(draft.processMinuteOfDay),
            placeholder = "格式 HH:mm，默认 17:30",
        ) { text ->
            val parts = text.split(":")
            val h = parts.getOrNull(0)?.toIntOrNull()
            val m = parts.getOrNull(1)?.toIntOrNull()
            if (h != null && m != null && h in 0..23 && m in 0..59)
                onChange(draft.copy(processMinuteOfDay = h * 60 + m))
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "到点后 Mac 跑完整管线（转写 + 去口气 + 总结），生成当日终稿日报",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        Spacer(Modifier.height(14.dp))
        HorizontalDivider(color = Glass.Stroke)
        Spacer(Modifier.height(12.dp))

        ToggleRow(
            icon = Icons.Filled.CloudUpload,
            label = "准实时上传",
            description = "边录边传，Mac 收到即转写，日报随时刷新",
            checked = draft.realtimeUpload,
        ) { onChange(draft.copy(realtimeUpload = it)) }

        if (draft.realtimeUpload) {
            Spacer(Modifier.height(12.dp))
            Text(
                "上传间隔",
                style = MaterialTheme.typography.labelMedium,
                color = Glass.TextDim,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                AppSettings.REALTIME_INTERVAL_OPTIONS.forEach { min ->
                    IntervalChip(
                        text = "$min 分钟",
                        selected = draft.realtimeIntervalMin == min,
                        modifier = Modifier.weight(1f),
                    ) { onChange(draft.copy(realtimeIntervalMin = min)) }
                }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                // 注意：字符串拼接的 `+` 必须写在**上一行末尾**。放在行首虽然
                // 在参数列表里合法，但这里是 if/else 的**代码块**——Kotlin 会把
                // 行首 `+` 当成新语句的一元加号，报 unaryPlus on String。
                if (draft.uploadOnMetered) {
                    "WiFi 与移动数据都上传，走出家门也能同步语料。" +
                            "此轮只补转写，去口气与总结仍在每日处理时间生成。"
                } else {
                    "仅 WiFi 下上传，走蜂窝不传（避免偷跑流量）。" +
                            "离家期间语料会压在手机里（不会丢），回到 WiFi 自动补传。"
                },
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        }

        Spacer(Modifier.height(14.dp))
        HorizontalDivider(color = Glass.Stroke)
        Spacer(Modifier.height(12.dp))

        ToggleRow(
            icon = Icons.Filled.Sync,
            label = "移动网络下也上传",
            description = "关掉后只走 WiFi 省流量；离家期间语料会压着不传（不丢，回 WiFi 补传）",
            checked = draft.uploadOnMetered,
        ) { onChange(draft.copy(uploadOnMetered = it)) }
    }
}

/**
 * 实时对话（Hermes 对接，2026-09-10）。
 *
 * 三档模式（关闭 / 手动 / 自动）。默认手动：自动模式会在用户正常说话时
 * 从扬声器突然冒出回复，门店演示场景下这种意外比「多点一下按钮」糟得多，
 * 所以让用户显式选择。
 *
 * 回声自激（2026-09-10 修）：外放声被麦克风录回会让自动模式无限自激。
 * 现在客户端在播报期挂起拾音、播后留 800ms 静默期，服务端再按「连续多轮
 * 转写高度相似」熔断兜底，双保险到位后文案不再劝退用户。
 */
@Composable
private fun TalkCard(vm: AppViewModel, draft: AppSettings, onChange: (AppSettings) -> Unit) {
    val ttsTesting by vm.ttsTesting.collectAsStateWithLifecycle()
    val ttsMsg by vm.ttsTest.collectAsStateWithLifecycle()
    GlassCard {
        SectionTitle("实时对话（管家）", accent = Glass.Indigo)
        Text(
            "说「大锅…」唤醒管家，服务端转写后由管家回答，并合成语音在手机上播报。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf(
                com.symi.digitalhuman.data.TalkMode.OFF to "关闭",
                com.symi.digitalhuman.data.TalkMode.MANUAL to "手动",
                com.symi.digitalhuman.data.TalkMode.AUTO to "自动",
            ).forEach { (mode, label) ->
                IntervalChip(
                    text = label,
                    selected = draft.talkMode == mode,
                    modifier = Modifier.weight(1f),
                ) { onChange(draft.copy(talkMode = mode)) }
            }
        }

        Spacer(Modifier.height(8.dp))
        Text(
            when (draft.talkMode) {
                com.symi.digitalhuman.data.TalkMode.OFF ->
                    "已关闭：不调用管家，只做日常语音日记。"
                com.symi.digitalhuman.data.TalkMode.MANUAL ->
                    "手动：在状态页点「对话」按钮，" +
                        (if (draft.talkRecordSec == 0) "说完自动停"
                         else "录 ${draft.talkRecordSec} 秒") +
                        "，随后自动播报回复。"
                else ->
                    // 送出范围由下面的 scope 决定，文案必须跟着变，
                    // 否则「每段都送」的旧说法会与「仅唤醒词」的默认自相矛盾。
                    //
                    // 回话规则 2026-09-14 又收紧了一档：**识别出的文字里没有「大锅」
                    // 就一律不回话**（硬门在 Hermes 侧，`DH_TALK_REPLY_REQUIRES_ALIAS`），
                    // 所以这里不能再说「由它按内容判断要不要回话」。
                    "自动：录到一段就交给管家留档。" +
                        (if (draft.talkScope == com.symi.digitalhuman.data.TalkScope.KEYWORD)
                            "当前是「仅唤醒词」，只有带「大锅」的段会送出去。"
                         else "当前是「全部语音」，所有段都会送出去留档。") +
                        "文字里没有「大锅」的一律不回话，只留一条记录。" +
                        "播报期间会自动挂起拾音，避免外放声被录回去形成回声自激。"
            },
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )

        if (draft.talkMode != com.symi.digitalhuman.data.TalkMode.OFF) {
            Spacer(Modifier.height(12.dp))
            Text(
                "录音时长",
                style = MaterialTheme.typography.labelMedium,
                color = Glass.TextDim,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(0, 4, 6, 8).forEach { sec ->
                    IntervalChip(
                        text = if (sec == 0) "自适应" else "$sec 秒",
                        selected = draft.talkRecordSec == sec,
                        modifier = Modifier.weight(1f),
                    ) { onChange(draft.copy(talkRecordSec = sec)) }
                }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                if (draft.talkRecordSec == 0)
                    "自适应：说到停为止（静音约 0.85 秒判定说完，15 秒硬上限）。" +
                        "「大锅，开灯」约 1 秒就结束，不必干等。"
                else
                    "固定 ${draft.talkRecordSec} 秒：说短指令时剩下的都是纯静音，" +
                        "整体延迟更高。排查断句故障时才需要固定时长。",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        }

        // 本地唤醒词过滤只有 AUTO 用得上：手动模式是用户主动按按钮，段一定是
        // 有话要说，没有任何「筛掉」的理由。
        if (draft.talkMode == com.symi.digitalhuman.data.TalkMode.AUTO) {
            Spacer(Modifier.height(12.dp))
            Text(
                "传给智能体的范围",
                style = MaterialTheme.typography.labelMedium,
                color = Glass.TextDim,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                // 顺序与 TalkScope.OPTIONS 一致：推荐项（默认项）在前
                listOf(
                    com.symi.digitalhuman.data.TalkScope.KEYWORD to "仅唤醒词（推荐）",
                    com.symi.digitalhuman.data.TalkScope.ALL to "全部语音",
                ).forEach { (scope, label) ->
                    IntervalChip(
                        text = label,
                        selected = draft.talkScope == scope,
                        modifier = Modifier.weight(1f),
                    ) { onChange(draft.copy(talkScope = scope)) }
                }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                if (draft.talkScope == com.symi.digitalhuman.data.TalkScope.KEYWORD)
                    "仅唤醒词：没说「大锅」的语音不送给管家，别人聊天、打电话都不会被打断。" +
                        "足够安静，代价是没带唤醒词的整句可能被漏掉。"
                else
                    "全部语音：每段都送给管家留档，语料不丢、方便事后回看；" +
                        "但文字里没有「大锅」的同样不会回话。" +
                        "两种范围的差别只是「要不要送出去」。",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )

            Spacer(Modifier.height(6.dp))
            ToggleRow(
                icon = Icons.Filled.RecordVoiceOver,
                label = "本地唤醒词检测",
                description = "在本机跑唤醒词模型，只作本地预判；" +
                    "是否回话只看识别出的文字里有没有「大锅」——" +
                    "本机判断会误触发（相似音、远处说话），不作回话依据",
                checked = draft.talkKwsFilter,
            ) { onChange(draft.copy(talkKwsFilter = it)) }
        }

        // ---------- 语音播报测试（2026-09-15） ----------
        //
        // 刻意做成设置页里的一个按钮，不单独开界面：这个动作只是「确认服务端 TTS
        // 与本机播报链路都通」，独立一页没有额外信息，反而多一层跳转。
        // 点击后服务端会用今天的数据合成一段中文播报，手机直接放出来；
        // 播报稿同时显示在下面，用户能对着听到的内容判断数据对不对。
        Spacer(Modifier.height(16.dp))
        HorizontalDivider(color = Glass.Stroke)
        Spacer(Modifier.height(12.dp))
        Text("语音播报测试", style = MaterialTheme.typography.bodyMedium, color = Glass.Text)
        Spacer(Modifier.height(4.dp))
        Text(
            "让服务端把今天的数据合成一段语音，从这台设备的扬声器播报出来。"
                + "点一次即可验证：网络与 Token 通、服务端 TTS 可用、本机扬声器正常。",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )
        Spacer(Modifier.height(10.dp))
        PrimaryButton(
            if (ttsTesting) "正在合成…" else "测试播报",
            onClick = { vm.testTts() },
            enabled = !ttsTesting,
            accent = Glass.Indigo,
            modifier = Modifier.fillMaxWidth(),
        )
        ttsMsg?.let { msg ->
            Spacer(Modifier.height(10.dp))
            val bad = msg.startsWith("测试失败")
            InsetCard {
                Text(
                    msg,
                    style = MaterialTheme.typography.bodySmall,
                    color = if (bad) Glass.Red else Glass.Green,
                )
            }
        }
    }
}

/** 带开关的设置行：左侧图标 + 文案，右侧 Switch。 */
@Composable
private fun ToggleRow(
    icon: ImageVector,
    label: String,
    description: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(if (checked) Glass.Teal.copy(alpha = 0.08f) else Color.Transparent)
            .clickable { onCheckedChange(!checked) }
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(36.dp)
                .clip(CircleShape)
                .background(if (checked) Glass.Teal.copy(alpha = 0.18f) else Glass.Surface2),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                icon, contentDescription = null,
                tint = if (checked) Glass.Teal else Glass.TextDim,
                modifier = Modifier.size(20.dp),
            )
        }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                label,
                style = MaterialTheme.typography.titleSmall,
                color = if (checked) Glass.Teal else Glass.Text,
            )
            Text(
                description,
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextDim,
            )
        }
        Spacer(Modifier.width(8.dp))
        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            colors = SwitchDefaults.colors(
                checkedThumbColor = Glass.Bg,
                checkedTrackColor = Glass.Teal,
                checkedBorderColor = Color.Transparent,
                uncheckedThumbColor = Glass.TextDim,
                uncheckedTrackColor = Glass.Surface3,
                uncheckedBorderColor = Color.Transparent,
            ),
        )
    }
}

/** 间隔选择 chip：选中态用 Teal 描边 + 半透明底，与 ModeOption 的视觉语言一致。 */
@Composable
private fun IntervalChip(
    text: String,
    selected: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(10.dp))
            .background(if (selected) Glass.Teal.copy(alpha = 0.12f) else Glass.Surface0)
            .border(
                1.dp,
                if (selected) Glass.Teal.copy(alpha = 0.5f) else Glass.Stroke,
                RoundedCornerShape(10.dp),
            )
            .clickable(onClick = onClick)
            .padding(vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text,
            style = MaterialTheme.typography.labelLarge,
            color = if (selected) Glass.Teal else Glass.TextDim,
        )
    }
}

@Composable
private fun StorageCard(draft: AppSettings, onChange: (AppSettings) -> Unit) {
    GlassCard {
        SectionTitle("手机存储")
        Spacer(Modifier.height(12.dp))
        Field("音频保留天数", draft.retentionDays.toString(), number = true) {
            onChange(draft.copy(retentionDays = it.toIntOrNull() ?: draft.retentionDays))
        }
        Spacer(Modifier.height(10.dp))
        Field("存储上限 MB（0=不限）", draft.maxStorageMb.toString(), number = true) {
            onChange(draft.copy(maxStorageMb = it.toIntOrNull() ?: draft.maxStorageMb))
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "超出上限后按最旧优先自动清理已上传音频",
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )
    }
}

@Composable
private fun Field(
    label: String,
    value: String,
    number: Boolean = false,
    placeholder: String = "",
    onChange: (String) -> Unit,
) {
    Column(Modifier.fillMaxWidth()) {
        Text(
            label,
            style = MaterialTheme.typography.bodySmall,
            color = Glass.TextDim,
        )
        Spacer(Modifier.height(6.dp))
        OutlinedTextField(
            value = value,
            onValueChange = onChange,
            singleLine = true,
            placeholder = if (placeholder.isBlank()) null else {
                { Text(placeholder, style = MaterialTheme.typography.bodyMedium, color = Glass.TextFaint) }
            },
            keyboardOptions = KeyboardOptions(
                keyboardType = if (number) KeyboardType.Number else KeyboardType.Text,
            ),
            shape = RoundedCornerShape(10.dp),
            textStyle = MaterialTheme.typography.bodyLarge,
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = Glass.Teal,
                unfocusedBorderColor = Glass.Stroke,
                focusedTextColor = Glass.Text,
                unfocusedTextColor = Glass.Text,
                cursorColor = Glass.Teal,
                focusedContainerColor = Glass.Surface0,
                unfocusedContainerColor = Glass.Surface0,
            ),
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

/**
 * 今日洞察卡（1.0.2）：关键词 Top-N chips + 隐私分类高亮。
 *
 * 数据来自 Mac 端 /api/agent/summary 的 insights 层（服务端零 LLM 成本产出）。
 * 设计取舍：
 * - 放设置页而非日报页——洞察是「处理时的参照」，不是内容本身；
 * - 加载失败（Mac 不在线/当天无数据）只显示一行提示，不阻塞其他卡片；
 * - 隐私样本只显示服务端已掩码的片段——这个卡片会被人截屏外发，
 *   不做敏感值展示（与出口脱敏同一标准）。
 */
@Composable
private fun InsightsCard(vm: AppViewModel) {
    var state by remember { mutableStateOf<MacClient.DayInsights?>(null) }
    var failed by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        // DataStore 冷读比 Compose 首帧慢：立即取 settings.value 会拿到内置默认
        // （endpoint=出厂 IP），连的就是错地方。重试到拿到真实配置，网络抖动顺带免疫。
        repeat(4) { attempt ->
            val s = vm.settings.value
            val r = MacClient(s).fetchSummary(java.time.LocalDate.now().toString())
            if (r != null) {
                state = r
                return@LaunchedEffect
            }
            if (attempt < 3) delay(1500)
        }
        failed = true
    }
    GlassCard {
        SectionTitle("今日洞察", accent = Glass.Teal)
        Spacer(Modifier.height(12.dp))
        val s = state
        when {
            s == null -> Text(
                if (failed) "暂不可用（Mac 端未连接或今日尚无数据）" else "加载中……",
                style = MaterialTheme.typography.bodySmall,
                color = Glass.TextFaint,
            )
            else -> {
                if (s.keywords.isNotEmpty()) {
                    Text(
                        "关键词",
                        style = MaterialTheme.typography.labelSmall,
                        color = Glass.TextFaint,
                    )
                    Spacer(Modifier.height(6.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        modifier = Modifier.horizontalScroll(rememberScrollState()),
                    ) {
                        s.keywords.forEach { k ->
                            Text(
                                "${k.kw} ×${k.count}",
                                style = MaterialTheme.typography.labelSmall,
                                color = Glass.Text,
                                modifier = Modifier
                                    .background(Glass.Surface0, RoundedCornerShape(8.dp))
                                    .border(1.dp, Glass.Stroke, RoundedCornerShape(8.dp))
                                    .padding(horizontal = 8.dp, vertical = 4.dp),
                            )
                        }
                    }
                    Spacer(Modifier.height(10.dp))
                }
                if (s.privacy.isNotEmpty()) {
                    Text(
                        "隐私（已脱敏，仅提示类别）",
                        style = MaterialTheme.typography.labelSmall,
                        color = Glass.TextFaint,
                    )
                    Spacer(Modifier.height(6.dp))
                    s.privacy.forEach { p ->
                        Text(
                            "${p.label} ×${p.count}" +
                                if (p.samples.isNotEmpty()) "　${p.samples.joinToString("、")}" else "",
                            style = MaterialTheme.typography.bodySmall,
                            color = Glass.Red,
                        )
                        Spacer(Modifier.height(4.dp))
                    }
                } else {
                    Text(
                        "今日未检出隐私内容",
                        style = MaterialTheme.typography.bodySmall,
                        color = Glass.TextFaint,
                    )
                }
            }
        }
    }
}
