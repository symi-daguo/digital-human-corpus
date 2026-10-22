package com.symi.digitalhuman.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.symi.digitalhuman.BuildConfig
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "settings")

/** 采集触发模式。存字符串以便 DataStore 直接读写。 */
object TriggerMode {
    /**
     * 自动：双触发状态机。Mac 在线（TCP 可达）或指定蓝牙设备连接，任一满足即自动采集；
     * 两者都离线才暂停（服务保活，恢复在线自动续录）。用户点「停止」优先级最高。
     */
    const val AUTO = "AUTO"
    /** 手动：完全不依赖 Mac/蓝牙，用户点「开始录音」才录，点「停止」才停。离线可用。 */
    const val MANUAL = "MANUAL"
    /** 常驻：App 运行期间持续采集，不依赖 Mac/蓝牙。离线可用。 */
    const val ALWAYS = "ALWAYS"

    val ALL = listOf(MANUAL, AUTO, ALWAYS)

    fun label(mode: String): String = when (mode) {
        AUTO -> "自动（Mac 在线 / 蓝牙连接）"
        MANUAL -> "手动（点按钮开始，离线可用）"
        ALWAYS -> "常驻（持续采集，离线可用）"
        else -> mode
    }

    /** 该模式是否需要联网探测 Mac */
    fun needsProbe(mode: String): Boolean = mode == AUTO
}

/**
 * 实时对话模式（Hermes 对接，2026-09-10）。
 *
 * OFF    关闭——完全不碰 /api/talk，退回纯语音日记。
 * MANUAL 手动——用户点状态页的「对话」按钮录一段，说完自动播放管家回复。
 *         像对讲机，触发时机完全可控，也不会被误唤醒打断。
 * AUTO   自动——每段采集落盘后立即送上服务端，服务端转写发现唤醒词才回话，
 *         手机收到回复就播放。真正「喊一声就答应」，代价是每段都要多跑一次
 *         网络往返（服务端对无唤醒词的段会秒回 skipped，不调大模型）。
 *
 * 为什么默认 MANUAL 而不是 AUTO：AUTO 会在用户正常说话时突然从扬声器冒出一句
 * 回复，而扬声器的声音又会被本机麦克风录进去（形成回声自激）。门店演示场景下
 * 这种意外比「多点一下按钮」糟得多。要自动的用户自己开。
 */
object TalkMode {
    const val OFF = "off"
    const val MANUAL = "manual"
    const val AUTO = "auto"
}

/**
 * 送给智能体的范围（2026-09-12 用户要求，APK 上可选）。
 *
 * 这道开关决定的是**服务端还要不要按唤醒词先筛一遍**，与「要不要回话」是两件事
 * —— 后者的闸门在智能体侧（`hermes_wake_receiver.should_reply`，零 token）。
 *
 * ⚠️ 2026-09-14 起，**回话的硬门是「识别出的文字里有没有唤醒词」**，两种范围都成立
 * （`DH_TALK_REPLY_REQUIRES_ALIAS=1`）。所以两者的差别退化成「要不要送出去」：
 *
 * KEYWORD 仅唤醒词（默认）。不命中唤醒词的轮次在服务端落 `no_alias` 直接丢，
 *         智能体根本看不到它。绝对不打扰，代价是漏听。
 * ALL     全部语音。所有段都送给智能体**留档**（语料不丢、方便事后回看），
 *         但文字里没有唤醒词的同样不会回话，只落库记录（status=noted）。
 *
 * 历史坑：09-11~09-14 期间 ALL 曾是默认，本机 KWS 的「命中」声明又与文本取或，
 * 于是本地误触发就能把不含唤醒词的轮次送进智能体并**真的回话**——实测 163 条
 * 回复里有 49 条（30%）文本里没有唤醒词。现已改成文本优先 + 硬门。
 */
object TalkScope {
    const val ALL = "all"
    const val KEYWORD = "keyword"

    /** 设置页二选一的展示顺序：默认项在前 */
    val OPTIONS = listOf(KEYWORD, ALL)

    fun label(v: String): String = when (v) {
        ALL -> "全部语音"
        KEYWORD -> "仅唤醒词（推荐）"
        else -> v
    }
}

data class AppSettings(
    val macIp: String,           // Mac 固定 IP（触发探测 + 上传目标）
    val macPort: Int,            // 服务端口（probe 与 REST 同端口）
    val token: String,           // 配对 token（默认 DEFAULT_TOKEN，与服务端 config.DEFAULT_TOKEN 一致）
    val btMac: String,           // 备用触发手机蓝牙 MAC（空=禁用）
    val processMinuteOfDay: Int, // 每日处理时间（默认 17:30 = 1050）
    val retentionDays: Int,      // 手机音频保留天数
    val maxStorageMb: Int,       // 存储阈值 LRU（0=不启用）
    val probeIntervalSec: Int,   // 探测间隔（默认 30）
    val paused: Boolean,         // 一键暂停采集
    val triggerMode: String,     // 见 TriggerMode
    val pinnedCertSha256: String, // Mac 自签证书指纹（base64 SHA-256），非空则启用 pinning
    val realtimeUpload: Boolean,  // 准实时上传：每 realtimeIntervalMin 分钟增量上传并触发转写
    val realtimeIntervalMin: Int, // 准实时上传间隔（分钟），可选 15/30/60
    /**
     * 外网中转地址（host:port，空=禁用）。
     *
     * 自适应连接：局域网内直连 macIp:macPort（延迟低）；探测不到 Mac 时自动
     * 切到中转（frp 公网隧道指向同一台 Mac，同一张自签证书与 Token）。
     * 手机离家仍可上传/拉报告，回家自动切回局域网——用户无感。
     */
    val relayAddress: String,
    /** 设备名称（空=用手机型号）。随每段上传 ?device= 传给服务端落 device_label。 */
    val ownerName: String,
    /**
     * 租户空间 ID（多租户，2026-09-09）：每部手机一个空间，服务端按
     * data/tenants/<id>/ 物理隔离各自的数据（录音/日报/声纹/角色）。
     * 空 = default 空间（旧版行为）；填错未注册的 ID 会被服务端 403 拒绝，
     * 绝不静默混进别人的空间。由管理员在工作台「空间管理」创建后分发。
     */
    val tenantId: String,
    /**
     * AUTO 模式下 Mac 与蓝牙都离线时，自动降级为常驻采集（等效 ALWAYS）。
     *
     * 为什么需要：AUTO 的本意是「人在电脑旁才录」，但它同时意味着
     * 「Mac 没开 = 一整天不录」。现场跑动、Mac 休眠、网络抖动都会触发后者，
     * 结果是白丢一整天语料。降级后离线照样录，Mac 回来自动恢复 AUTO 语义。
     */
    val offlineAlwaysFallback: Boolean = true,
    /** 实时对话模式，见 [TalkMode]。默认手动（不自动播放）。 */
    val talkMode: String = TalkMode.MANUAL,
    /**
     * 对话录音时长（秒）。**0 = 自适应**（默认），正数 = 固定秒数。
     *
     * 原来的默认值是 6 秒，问题在于说「大锅，开灯」只用约 1.2 秒，剩下 4.8 秒
     * 全是静音：用户干等、语音占比被稀释、ASR 更容易出空结果。改成自适应后由
     * 对话专用 VAD 判断「说完」（静音 ≥0.85s 收尾），15 秒硬上限兜底。
     *
     * 保留固定秒数选项是为了排查问题：VAD 断句异常时，固定时长能立刻区分
     * 「是录音的问题」还是「是断句的问题」。
     */
    val talkRecordSec: Int = 0,
    /**
     * AUTO 对话模式下是否跑**本地唤醒词检测**（默认开）。
     *
     * ⚠️ 2026-09-14 起这条线索**已不参与任何回话判定**：本地 KWS 会误触发
     * （相似音、远场、环境声），而它一旦报「命中」，就会把一条**识别文本里根本
     * 没有唤醒词**的轮次推进回话通路 —— 用户的原话是「我都没说大锅/四米，
     * 怎么能错误识别并跟我反馈」。现在服务端与智能体两侧都改成「文本优先」，
     * 这条声明只留着对比、把本地误触发率打进日志。
     *
     * 开：手机本地跑 sherpa-onnx KeywordSpotter；
     * 关：不跑本地模型（省电、少一次推理）。
     * 两者都**不影响**「会不会回话」——回话只看识别文本里有没有「大锅」。
     */
    val talkKwsFilter: Boolean = true,
    /**
     * 送给智能体的范围，见 [TalkScope]。
     *
     * 2026-09-14 起默认 [TalkScope.KEYWORD]：用户在真实使用中发现 ALL 会把大量
     * 「不是在跟管家说话」的轮次（打电话、跟客人聊天）送进智能体并**真的回话**。
     * 默认要安静；切到 ALL 则变成「全部留档」——能回话的仍然只有带唤醒词的那些。
     */
    val talkScope: String = TalkScope.KEYWORD,
    /**
     * 采集时段（2026-09-15 新增）。空 = 全天不限（默认，保持旧行为）。
     *
     * 长期驻留的终端不必 7×24 录音：夜间/闭店后录到的只有噪声，白费电与存储，
     * 还给服务端塞一堆无意义音频。格式与语义见 [CaptureWindow]。
     *
     * ⚠️ 这条是**计划**，与「用户暂停」([paused] / `CaptureState.userStopped`)
     * 是「与」关系，两者都要满足才采集。时段结束只负责收尾会话，绝不改写
     * 用户意图；时段恢复也不会替用户把暂停解开。
     */
    val captureWindows: String = CaptureWindow.ALL_DAY,
    /**
     * 安装实例标识（2026-09-15）。首次启动生成、此后永不变更的随机串。
     *
     * 用途：作为服务端的**设备命名空间键**。服务端按它给每台设备分配互不重叠的
     * 段号区间，从而根治「两台设备都从 1 号段开始上传，后上传的把先上传的原始
     * 音频整段覆盖」这个数据丢失问题（实测已发生）。
     *
     * 为什么不用 [ownerName]：那是用户可改的中文名，两台设备完全可能被起成同一个
     * 名字。把唯一性建立在「用户不会起重名」之上是不可靠的。
     *
     * 空 = 尚未生成（旧版本升级上来的短暂窗口）→ 服务端退回按设备名分空间，
     * 不丢数据，只是这一小段时间的空间划分粒度粗一级。
     */
    val installId: String = "",
    /**
     * 移动网络（计费网络）下是否也上传语料，默认**开**。
     *
     * ⚠️ 这是个**真实数据丢失事故**换来的开关（2026-09-21 实测）。
     *
     * 修复前：上传任务一律带 `NetworkType.UNMETERED` 约束（只允许 WiFi），意图是
     * 「24h 录音走蜂窝半个月能吃掉一个月套餐，且用户毫无感知」。但手机一旦离开
     * WiFi（在车上/在路上，用移动数据），这条约束**永不满足** → 增量上传任务
     * 永不执行；而唯一不带网络约束的自动通路 `WorkScheduler.uploadOnReconnect()`
     * 当时挂在「局域网 TCP 上升沿」上，人在外网同样永不触发。
     *
     * 两条路一起锁死 ⇒ 现象：**人在外网 + 移动数据 = 对话正常（talk 路径无约束），
     * 但语料一条都传不上去**。实测 2026-09-21 上午 1127 轮对话、0 段音频入库，
     * 而 09-18（家里 WiFi）503 段、09-20（别的 WiFi）556 段 —— 差别只在
     * 那个 WiFi 是不是计费网络。
     *
     * 开：上传走 `NetworkType.CONNECTED`，蜂窝也传 —— 外网/移动数据下语料不丢。
     * 关：维持旧的 `NetworkType.UNMETERED`，只走 WiFi，省流量，代价是离家后
     *     语料一直压在手机里（不会丢，但要等回到 WiFi 才补传）。
     *
     * 默认开：语料采集是这套系统的**唯一目的**，静默不采集比多花流量严重得多。
     * 想省流量的人在设置页关掉即可。
     */
    val uploadOnMetered: Boolean = true,
) {
    companion object {
        /**
         * 默认配对 Token。必须与服务端 config.DEFAULT_TOKEN 逐字一致，
         * 否则开箱即用第一次上传就是 401，而报错信息只会说「上传失败」，极难定位。
         * 改动此处请同步改 server/app/config.py。
         */
        const val DEFAULT_TOKEN = "digital-human"

        /** 准实时上传默认间隔（分钟）与可选值 */
        const val DEFAULT_REALTIME_INTERVAL_MIN = 30
        val REALTIME_INTERVAL_OPTIONS = listOf(15, 30, 60)
    }
}

class SettingsStore(private val ctx: Context) {
    private object K {
        val macIp = stringPreferencesKey("mac_ip")
        val macPort = intPreferencesKey("mac_port")
        val token = stringPreferencesKey("token")
        val btMac = stringPreferencesKey("bt_mac")
        val processMinute = intPreferencesKey("process_minute")
        val retentionDays = intPreferencesKey("retention_days")
        val maxStorageMb = intPreferencesKey("max_storage_mb")
        val probeInterval = intPreferencesKey("probe_interval")
        val paused = booleanPreferencesKey("paused")
        val triggerMode = stringPreferencesKey("trigger_mode")
        val pinnedCert = stringPreferencesKey("pinned_cert_sha256")
        val realtimeUpload = booleanPreferencesKey("realtime_upload")
        val realtimeInterval = intPreferencesKey("realtime_interval_min")
        val offlineAlwaysFallback = booleanPreferencesKey("offline_always_fallback")
        val uploadOnMetered = booleanPreferencesKey("upload_on_metered")
        val relayAddress = stringPreferencesKey("relay_address")
        val ownerName = stringPreferencesKey("owner_name")
        val tenantId = stringPreferencesKey("tenant_id")
        val talkMode = stringPreferencesKey("talk_mode")
        val talkRecordSec = intPreferencesKey("talk_record_sec")
        val talkKwsFilter = booleanPreferencesKey("talk_kws_filter")
        val talkScope = stringPreferencesKey("talk_scope")
        val captureWindows = stringPreferencesKey("capture_windows")
        val installId = stringPreferencesKey("install_id")
    }

    val settings: Flow<AppSettings> = ctx.dataStore.data.map { p ->
        AppSettings(
            // 出厂默认地址来自 BuildConfig.DEFAULT_MAC_HOST（app/dh.properties，
            // 不入库），缺失时为空串 —— 用户在设置页填自己的服务端地址即可。
            macIp = p[K.macIp] ?: BuildConfig.DEFAULT_MAC_HOST,
            macPort = p[K.macPort] ?: 8766,
            // 空串也要兜底：历史上保存过「留空」的用户升级后应当拿到新默认值，
            // 而不是退化成无鉴权的 HTTP。想关鉴权得服务端也把 DH_TOKEN 置空。
            token = p[K.token]?.takeIf { it.isNotBlank() } ?: AppSettings.DEFAULT_TOKEN,
            btMac = p[K.btMac] ?: "",
            processMinuteOfDay = p[K.processMinute] ?: 1050,
            retentionDays = p[K.retentionDays] ?: 7,
            maxStorageMb = p[K.maxStorageMb] ?: 0,
            probeIntervalSec = p[K.probeInterval] ?: 30,
            paused = p[K.paused] ?: false,
            // 默认手动：App 不应在 Mac 不可达时变成「什么也录不了」。
            // 需要自动触发的用户在设置页切到 AUTO 即可。
            triggerMode = p[K.triggerMode] ?: TriggerMode.MANUAL,
            pinnedCertSha256 = p[K.pinnedCert] ?: "",
            // 默认开启：Mac 全天在线时，用户期望随时能看到今天的转写，
            // 而不是等到 17:30 才一次性出结果。
            realtimeUpload = p[K.realtimeUpload] ?: true,
            realtimeIntervalMin = p[K.realtimeInterval] ?: AppSettings.DEFAULT_REALTIME_INTERVAL_MIN,
            offlineAlwaysFallback = p[K.offlineAlwaysFallback] ?: true,
            uploadOnMetered = p[K.uploadOnMetered] ?: true,
            relayAddress = p[K.relayAddress] ?: "",
            ownerName = p[K.ownerName] ?: "",
            tenantId = p[K.tenantId] ?: "",
            talkMode = p[K.talkMode] ?: TalkMode.MANUAL,
            talkRecordSec = p[K.talkRecordSec] ?: 0,
            talkKwsFilter = p[K.talkKwsFilter] ?: true,
            talkScope = p[K.talkScope] ?: TalkScope.KEYWORD,
            // 空串也走默认「全天」。历史上没存过这个键的设备升级后第一件事
            // 就是拿到「不限时段」——绝不能因为缺键而变成「整天不采集」。
            captureWindows = p[K.captureWindows] ?: CaptureWindow.ALL_DAY,
            installId = p[K.installId] ?: "",
        )
    }

    suspend fun current(): AppSettings = settings.first()

    /**
     * 取安装实例标识，没有就生成一个并落盘（幂等，可在任意启动路径重复调用）。
     *
     * 刻意不做在 [settings] 的 flow 映射里：flow 每次收集都会重跑，
     * 在里面写库是典型的副作用泄漏（改个设置就多写一次 UUID，甚至可能与
     * 生成时的读构成竞态）。这里用 suspend + 进程内缓存，只在真正缺时才落盘。
     */
    @Volatile private var cachedInstallId: String? = null

    suspend fun ensureInstallId(): String {
        cachedInstallId?.let { return it }
        val existing = ctx.dataStore.data.first()[K.installId].orEmpty()
        if (existing.isNotBlank()) {
            cachedInstallId = existing
            return existing
        }
        // 32 位十六进制（128 bit 随机），足够避免任何现实中的碰撞。
        val fresh = buildString {
            while (length < 32) append("%08x".format(java.security.SecureRandom().nextInt()))
        }.take(32)
        ctx.dataStore.edit { it[K.installId] = fresh }
        cachedInstallId = fresh
        return fresh
    }

    suspend fun setPaused(v: Boolean) = ctx.dataStore.edit { it[K.paused] = v }

    suspend fun update(
        macIp: String? = null, macPort: Int? = null, token: String? = null,
        btMac: String? = null, processMinuteOfDay: Int? = null,
        retentionDays: Int? = null, maxStorageMb: Int? = null,
        probeIntervalSec: Int? = null, triggerMode: String? = null,
        pinnedCertSha256: String? = null,
        realtimeUpload: Boolean? = null, realtimeIntervalMin: Int? = null,
        offlineAlwaysFallback: Boolean? = null,
        uploadOnMetered: Boolean? = null,
        relayAddress: String? = null,
        ownerName: String? = null,
        tenantId: String? = null,
        talkMode: String? = null,
        talkRecordSec: Int? = null,
        talkKwsFilter: Boolean? = null,
        talkScope: String? = null,
        captureWindows: String? = null,
        installId: String? = null,
    ) {
        ctx.dataStore.edit { p ->
            macIp?.let { p[K.macIp] = it }
            macPort?.let { p[K.macPort] = it }
            token?.let { p[K.token] = it }
            btMac?.let { p[K.btMac] = it }
            processMinuteOfDay?.let { p[K.processMinute] = it }
            retentionDays?.let { p[K.retentionDays] = it }
            maxStorageMb?.let { p[K.maxStorageMb] = it }
            probeIntervalSec?.let { p[K.probeInterval] = it }
            triggerMode?.let { p[K.triggerMode] = it }
            pinnedCertSha256?.let { p[K.pinnedCert] = it }
            realtimeUpload?.let { p[K.realtimeUpload] = it }
            realtimeIntervalMin?.let { p[K.realtimeInterval] = it }
            offlineAlwaysFallback?.let { p[K.offlineAlwaysFallback] = it }
            uploadOnMetered?.let { p[K.uploadOnMetered] = it }
            relayAddress?.let { p[K.relayAddress] = it }
            ownerName?.let { p[K.ownerName] = it }
            tenantId?.let { p[K.tenantId] = it }
            talkMode?.let { p[K.talkMode] = it }
            talkRecordSec?.let { p[K.talkRecordSec] = it }
            talkKwsFilter?.let { p[K.talkKwsFilter] = it }
            talkScope?.let { p[K.talkScope] = it }
            // 落盘前规范化：用户输入 "9:0-12:0" 也存成 "09:00-12:00"，
            // 免得非法/异体写法长期留在库里，日后排查时看不出实际生效的是什么。
            captureWindows?.let { p[K.captureWindows] = CaptureWindow.normalize(it) }
            installId?.let { p[K.installId] = it }
        }
    }
}
