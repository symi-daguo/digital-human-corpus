package com.symi.digitalhuman.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import com.symi.digitalhuman.App
import com.symi.digitalhuman.R
import com.symi.digitalhuman.data.AppDatabase
import com.symi.digitalhuman.data.CaptureWindow
import com.symi.digitalhuman.data.ServiceEvent
import com.symi.digitalhuman.data.SettingsStore
import com.symi.digitalhuman.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 常驻守护（specialUse FGS）：LAN TCP probe + 蓝牙连接监听 → 触发/停止采集；
 * 60s 自检 CaptureService 死亡并恢复。
 */
class GuardService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val btEvents = Channel<String>(Channel.CONFLATED)
    private var btReceiver: BroadcastReceiver? = null
    // 必须延迟到 attach 之后再建：Service 构造器里 this 的 base context 还是 null，
    // 此时 SettingsStore(this) 调 getApplicationContext() 会直接 NPE（Service 起不来 → App 一开就闪退）。
    private val settings by lazy { SettingsStore(this) }

    /** AUTO 模式双触发的实时状态。任一在线 = 应采集；全部离线 = 暂停采集（服务保活） */
    @Volatile private var macUp = false
    @Volatile private var btUp = false

    /**
     * 双触发状态机（AUTO 模式核心）：
     * - 采集条件 = Mac 可达 **或** iPhone 蓝牙连接（用户需求：两者都离线才停）
     * - 上升沿：拉起/恢复采集；下降沿：暂停采集（CaptureService.pause，服务不 stop）
     * - 用户手动开始的会话（MANUAL）与常驻会话（ALWAYS）不受本状态机管辖
     * - userStopped 优先级最高：用户点过「停止」后状态机不再自动拉起，点「开始」或换模式才解除
     * - 上升沿（离开触发源后重新出现）重新武装 userStopped：重新在场 = 重新授权自动采集，
     *   否则一次停止就永远卡在「未触发」（实测故障）
     */
    @Volatile private var lastTriggerUp = false

    private suspend fun evaluate(reason: String) {
        // ── 采集时段闸门（2026-09-15）──
        // 非采集时段一律不触发新的采集；已经在录的走 triggerDown 正常收尾
        // （结束会话 + CaptureService.pause，服务保持前台存活），
        // 而不是 stop——长期驻留的红线是「服务不倒」，时段结束只是不录而已。
        // 注意这里**只**管采集，绝不碰 userStopped / paused：时段是计划，
        // 用户暂停是意图，计划恢复时替用户把暂停解开是不可接受的。
        val spec = settings.current().captureWindows
        if (!CaptureWindow.isActive(spec, CaptureWindow.nowMinuteOfDay())) {
            if (CaptureState.triggerUp) {
                android.util.Log.i(TAG, "非采集时段(${CaptureWindow.nowHm()})，收尾当前会话: $reason")
                triggerDown("out-of-window:$reason")
            } else {
                android.util.Log.i(TAG, "非采集时段(${CaptureWindow.nowHm()})，跳过触发: $reason")
            }
            lastTriggerUp = false
            return
        }
        val up = macUp || btUp
        val rose = up && !lastTriggerUp
        lastTriggerUp = up
        if (up) {
            if (rose && CaptureState.userStopped) {
                CaptureState.userStopped = false
            }
            val wasFallback = CaptureState.offlineFallback
            CaptureState.offlineFallback = false
            // 从降级状态恢复：triggerType 从 ALWAYS 改回 AUTO，采集中间不打断——
            // 用户设的是「自动」，Mac 回来后状态就该显示自动，而不是一直挂常驻。
            if (wasFallback && CaptureState.triggerType == "ALWAYS") {
                CaptureState.triggerType = "AUTO"
            }
            if (!CaptureState.triggerUp && !CaptureState.userStopped) {
                val type = if (macUp && btUp) "AUTO" else if (macUp) "LAN" else "BT"
                triggerUp(type, reason)
            }
            return
        }
        // 全部离线：默认降级为常驻采集（离线不该等于不录）。
        // MANUAL 是用户显式手动控制，不接管；ALWAYS 本就常驻，无需降级。
        if (CaptureState.triggerType == "MANUAL" || CaptureState.triggerType == "ALWAYS") return
        if (settings.current().offlineAlwaysFallback) {
            if (!CaptureState.offlineFallback) {
                CaptureState.offlineFallback = true
                scope.launch {
                    AppDatabase.get(applicationContext).serviceEventDao().insert(
                        ServiceEvent(type = "OFFLINE_FALLBACK", detail = reason)
                    )
                }
            }
            // 已经在录就不打断；还没录（例如开机即离线）则按常驻拉起
            if (!CaptureState.triggerUp && !CaptureState.userStopped) {
                triggerUp("ALWAYS", "offline-fallback:$reason")
            }
        } else if (CaptureState.triggerUp) {
            triggerDown(reason)
        }
    }


    override fun onCreate() {
        super.onCreate()
        startAsForeground()
        scope.launch {
            AppDatabase.get(applicationContext).serviceEventDao()
                .insert(ServiceEvent(type = "GUARD_STARTED"))
        }
        registerBt()
        scope.launch { probeLoop() }
        scope.launch { selfCheckLoop() }
        scope.launch { btLoop() }
        scope.launch { alwaysLoop() }
        // 采集时段巡检：进入时段自动开录、离开时段自动收尾（2026-09-15）
        scope.launch { windowLoop() }
        // 调度初始化（2026-09-15）：见 ensureWorkScheduled 的说明——
        // 这是「配置好参数后不再打开 App 也能持续上报」的关键一环。
        scope.launch { ensureWorkScheduled() }
        // 通知文案按当前时段刷新一次：长期驻留的设备用户几乎不开 App，
        // 下拉通知栏是唯一能一眼看出「现在到底在不在录」的地方。
        scope.launch { refreshGuardNotification() }
    }

    /**
     * 确保周期任务在位（日终处理 + 准实时增量上传）。
     *
     * 为什么放在守护服务里：`WorkScheduler.scheduleAll` 过去只在打开界面时被调用
     * （MainActivity / AppViewModel），而本项目的目标形态是**长期驻留、不再开界面**。
     * WorkManager 的队列虽然能跨重启保留，但一旦被系统清掉（Force Stop、
     * 「开发者选项 → 后台进程限制」、厂商省电策略），只有「下次有人开界面」才会重建。
     * 长期驻留的设备没有那个「下次」，结果是静默地再也不上传了。
     *
     * 放在服务冷启动 = 每次进程重建都把调度补回来，不依赖任何人工动作。
     * 失败只记日志：调度不上不该让守护服务起不来（采集本身不依赖 Worker）。
     */
    private suspend fun ensureWorkScheduled() {
        runCatching {
            val s = settings.current()
            com.symi.digitalhuman.worker.WorkScheduler.scheduleAll(
                applicationContext, s.processMinuteOfDay, s.realtimeUpload, s.realtimeIntervalMin,
                s.uploadOnMetered,
            )
            android.util.Log.i(
                TAG,
                "调度已确保: 日终=${s.processMinuteOfDay}分 增量=${s.realtimeUpload}/${s.realtimeIntervalMin}分"
            )
        }.onFailure { android.util.Log.w(TAG, "调度初始化失败: ${it.message}") }
    }

    private fun startAsForeground() {
        val n: Notification = buildGuardNotification("待命检测您是否在电脑/手机旁")
        startForeground(NOTIF_ID, n)
    }

    private fun buildGuardNotification(text: String): Notification =
        NotificationCompat.Builder(this, App.CHANNEL_GUARD)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("语料守护运行中")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openAppIntent())
            .build()

    private fun openAppIntent(): PendingIntent =
        PendingIntent.getActivity(
            this, 13, Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

    /**
     * 刷新守护通知文案。时刻反映「当前该不该录」——这是永久驻留终端上
     * 用户唯一不用开界面就能自检的地方，比任何日志都直接。
     */
    private suspend fun refreshGuardNotification() {
        val s = settings.current()
        val inside = CaptureWindow.isActive(s.captureWindows, CaptureWindow.nowMinuteOfDay())
        val txt = when {
            CaptureWindow.parse(s.captureWindows).isEmpty() -> "全天采集 · 待命检测触发源"
            inside -> "采集时段 ${CaptureWindow.label(s.captureWindows)} · 正在采集"
            else -> "非采集时段（${CaptureWindow.label(s.captureWindows)}）· 待命"
        }
        runCatching {
            androidx.core.app.NotificationManagerCompat.from(this)
                .notify(NOTIF_ID, buildGuardNotification(txt))
        }
    }

    // ---------- 常驻模式：不依赖 Mac / 蓝牙，App 存活期间持续采集 ----------
    private suspend fun alwaysLoop() {
        while (scope.isActive) {
            val s = settings.current()
            if (!s.paused && s.triggerMode == com.symi.digitalhuman.data.TriggerMode.ALWAYS
                // 采集时段是硬边界：非时段内常驻模式也不拉起（2026-09-15）
                && CaptureWindow.isActiveNow(s.captureWindows)
                && !CaptureState.triggerUp && !CaptureState.userStopped) {
                triggerUp("ALWAYS", "always-on")
            }
            delay(15_000)
        }
    }

    /**
     * 采集时段巡检（2026-09-15 新增）。
     *
     * 只在**状态翻转**时动作，不做每轮都评估——每轮评估会与 probeLoop /
     * alwaysLoop 的收敛逻辑打架（三处同时拉起/收尾，日志会难以解读）。
     *
     * - 进入时段：立刻评估一次，把该录的录起来，不等下个探测周期（最坏 5 秒内开录）。
     * - 离开时段：`triggerDown` 收尾 —— 结束当前会话（Session.endReason 可查），
     *   `CaptureService.pause` 保持服务前台存活，麦克风 FGS 不掉，
     *   下个时段到来时零人工干预续录。
     *
     * 为什么用 30 秒粒度：用户设的是分钟级时段，30 秒的边界误差对「闭店后别再录」
     * 这类诉求完全够用；再密就对长期驻留的功耗不友好了。
     */
    private suspend fun windowLoop() {
        val db = AppDatabase.get(applicationContext)
        var inWindow: Boolean? = null
        while (scope.isActive) {
            val s = settings.current()
            val spec = s.captureWindows
            val now = CaptureWindow.isActive(spec, CaptureWindow.nowMinuteOfDay())
            if (now != inWindow) {
                val first = inWindow == null
                inWindow = now
                if (!first) {
                    android.util.Log.i(TAG, "采集时段切换: now=${CaptureWindow.nowHm()} " +
                        "inWindow=$now spec=${CaptureWindow.normalize(spec)}")
                    db.serviceEventDao().insert(
                        ServiceEvent(
                            type = if (now) "WINDOW_OPEN" else "WINDOW_CLOSE",
                            detail = "${CaptureWindow.nowHm()} ${CaptureWindow.label(spec)}"
                        )
                    )
                }
                if (!now) {
                    // 离开时段：无论当前是哪个模式触发的采集，都收尾。
                    // 这是「非采集时段不处理」的字面语义——手动在窗口外点的开始也一并收尾，
                    // 避免出现「点了开始却在 30 秒后莫名停掉」的困惑（UI 侧同时做了拦截）。
                    if (CaptureState.triggerUp) triggerDown("out-of-window")
                } else if (!s.paused && !CaptureState.userStopped) {
                    evaluate("in-window")
                }
                refreshGuardNotification()
            }
            delay(WINDOW_CHECK_MS)
        }
    }

    // ---------- LAN 触发：TCP probe（连续 2 成功上线 / 3 失败下线） ----------
    private suspend fun probeLoop() {
        var okStreak = 0
        var failStreak = 0
        // 上传链路可用性（2026-09-10）。与采集触发的 macUp 刻意分开：
        // macUp 只服务 AUTO 模式的「该不该录音」，本变量服务「该不该补传数据」。
        // 初值 false → 服务启动后第一轮探测成功即视为上升沿，正好覆盖
        // 「电脑开机 + 手机守护服务刚拉起」这个场景。
        var uploadLinkUp = false
        // 上一轮生效端点是不是走中转（null = 上一轮两条都不通）。
        // 用来捕捉「链路没断、但换了条路」：人从家里走到外面，局域网变中转，
        // linkUp 一直是 true，光看上升沿抓不到，积压段就传不出去（2026-09-21）。
        var uploadLinkVia: Boolean? = null
        while (scope.isActive) {
            val s = settings.current()
            val canProbe = !s.paused && (s.macIp.isNotBlank() || s.relayAddress.isNotBlank())
            val autoMode = s.triggerMode == com.symi.digitalhuman.data.TriggerMode.AUTO
            // 这里有两个**不同**的可达性，别混（2026-09-21 修过一次）：
            //   ① lanReachable —— 局域网直连通不通。AUTO 模式「人在电脑旁才录」
            //      靠它，所以必须只探局域网：走中转不算"在家"。
            //   ② linkUp —— 生效端点（局域网优先、不通自动走外网中转）通不通，
            //      决定「积压音频该不该立刻补传」，与用户在不在家无关。
            //      以前只有 ①，还拿 ① 驱动补传：人一走外网 ① 恒 false，补传上升沿
            //      永不触发，积压只能干等 WorkManager 指数退避（最长 5 小时）。
            //      表现为「人在外面说的话，回来看不到」。
            val lanReachable = s.macIp.isNotBlank() && tcpProbe(s.macIp, s.macPort)
            val link = when {
                !canProbe -> null
                // 局域网已经通了就别再花一次探测；只有局域网不通时才去试中转，
                // 且强制重选（忽略 60s 缓存），出门后 1 个探测周期内必切过去。
                lanReachable -> com.symi.digitalhuman.sync.Endpoints.lan(s)
                else -> com.symi.digitalhuman.sync.Endpoints.probe(s, force = true)
            }
            val linkUp = link != null
            // 给 UI：当前上传链路走的是哪条路（null = 两条都不通）
            CaptureState.lastLinkViaRelay = link?.viaRelay

            // ── 上传补传：链路不可达 → 可达的上升沿，立刻推积压段 ──
            // 修复「电脑开机后手机数据不自动同步」：修复前上传任务失败走
            // WorkManager 默认指数退避（最长 5 小时），Mac 上线后手机无从感知，
            // 只能干等退避到期（实测 10:55 就绪、11:18 才补传）。这里用 REPLACE
            // 重建上传任务把退避归零，链路一回来就传，最迟 1 个探测周期。
            // 两种触发都要：① 不可达→可达；② 可达但**换了条路**（局域网↔中转）。
            // 少了 ②：人在家里时 linkUp 已经 true，走到外面切成中转不会触发补传，
            // 而旧版的中转上传又带 UNMETERED 约束、蜂窝下不执行 —— 于是人一出
            // 家门，之后录的语料就一直压在手机里（2026-09-21 实测）。
            val viaRelayNow = link?.viaRelay
            val pathChanged = linkUp && uploadLinkUp && uploadLinkVia != viaRelayNow
            if (canProbe && linkUp && (!uploadLinkUp || pathChanged)) {
                val why = when {
                    !uploadLinkUp -> "link back online"
                    viaRelayNow == true -> "switch to relay"
                    else -> "switch to lan"
                }
                scope.launch {
                    AppDatabase.get(applicationContext).serviceEventDao()
                        .insert(ServiceEvent(type = "UPLOAD_CATCHUP", detail = why))
                }
                com.symi.digitalhuman.worker.WorkScheduler.uploadOnReconnect(applicationContext)
            }
            uploadLinkUp = linkUp
            uploadLinkVia = viaRelayNow

            // ── 采集触发：只有 AUTO 模式才把探测结果用于「是否该录音」 ──
            // MANUAL / ALWAYS 不依赖 Mac 可达性，否则「Mac 没开 = 不能录音」——
            // 离线场景下面板显示正常但一段都录不到。
            if (canProbe && autoMode) {
                CaptureState.lastProbeAt = System.currentTimeMillis()
                CaptureState.lastProbeOk = lanReachable
                if (lanReachable) {
                    okStreak++; failStreak = 0
                    // 连续 2 次成功才算 Mac 上线（消抖）；btUp 已在线时不重复 evaluate
                    if (okStreak >= 2 && !macUp) { macUp = true; evaluate("tcp:${s.macIp}:${s.macPort}") }
                } else {
                    failStreak = (failStreak + 1).coerceAtMost(3); okStreak = 0
                    // 连续 3 次失败才算 Mac 离线；只要蓝牙还连着，采集继续（双触发语义）
                    if (failStreak >= 3 && macUp) { macUp = false; evaluate("tcp gone") }
                }
            } else {
                // 非 AUTO 模式：**局域网**探测结果不反映到 UI，避免把「Mac 不可达」
                // 红点一直亮着误导用户；同时清掉 macUp，防止切回 AUTO 时用陈旧状态误触发。
                // 注意只清 lan 那一路：上传链路真伪由 lastLinkViaRelay 单独表达，别覆盖它。
                CaptureState.lastProbeOk = false
                if (macUp) { macUp = false; }
            }
            // 蓝牙连接态巡检（AUTO）：补齐「服务启动时 iPhone 已连着，收不到 ACL 广播」的缺口
            if (!s.paused && s.triggerMode == com.symi.digitalhuman.data.TriggerMode.AUTO
                && s.btMac.isNotBlank()) {
                val connected = btProfileConnected(s.btMac)
                if (connected && !btUp) { btUp = true; evaluate("bt poll") }
                else if (!connected && btUp) { btUp = false; evaluate("bt poll gone") }
            }
            // 状态收敛（自愈，防卡死）：触发源在线但没在采集 → 补拉起。
            // 旧实现只在 macUp/btUp 翻转的「边沿」评估，任何中间态残留
            // （结束会话后、Guard 进程重启丢 macUp 状态、恢复启动失败）都会
            // 卡在「未触发」直到下一次边沿。每轮探测周期做一次收敛检查，
            // 任何原因导致的「应采未采」都在一个周期内自动修复。
            if (!s.paused && s.triggerMode == com.symi.digitalhuman.data.TriggerMode.AUTO
                && (macUp || btUp)
                && CaptureWindow.isActiveNow(s.captureWindows)
                && !CaptureState.triggerUp && !CaptureState.userStopped) {
                val type = if (macUp && btUp) "AUTO" else if (macUp) "LAN" else "BT"
                triggerUp(type, "reconcile")
            }
            val base = s.probeIntervalSec * 1000L
            delay(base + (0..5000).random())
        }
    }

    private suspend fun tcpProbe(host: String, port: Int): Boolean = try {
        Socket().use { it.connect(InetSocketAddress(host, port), 2000) }
        true
    } catch (_: Exception) { false }

    // ---------- 蓝牙触发：ACL 连接广播 + Profile 二次确认 ----------
    private fun registerBt() {
        if (checkSelfPermission(android.Manifest.permission.BLUETOOTH_CONNECT)
            != PackageManager.PERMISSION_GRANTED) return
        val r = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                val dev: BluetoothDevice? = if (Build.VERSION.SDK_INT >= 33)
                    intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
                val mac = dev?.address ?: return
                when (intent.action) {
                    BluetoothDevice.ACTION_ACL_CONNECTED -> btEvents.trySend("UP:$mac")
                    BluetoothDevice.ACTION_ACL_DISCONNECTED -> btEvents.trySend("DOWN:$mac")
                }
            }
        }
        val f = IntentFilter().apply {
            addAction(BluetoothDevice.ACTION_ACL_CONNECTED)
            addAction(BluetoothDevice.ACTION_ACL_DISCONNECTED)
        }
        registerReceiver(r, f)
        btReceiver = r
    }

    private suspend fun btLoop() {
        for (ev in btEvents) {
            val s = settings.current()
            // 蓝牙触发属于 AUTO 语义：MANUAL/ALWAYS 下由用户或常驻逻辑掌控，蓝牙不接管
            if (s.paused || s.btMac.isBlank() ||
                s.triggerMode != com.symi.digitalhuman.data.TriggerMode.AUTO) continue
            val (dir, mac) = ev.split(":", limit = 2).let { it[0] to it[1] }
            if (!mac.equals(s.btMac, ignoreCase = true)) continue
            when (dir) {
                "UP" -> {
                    delay(3000) // 消抖：3s 后仍连接才算上线
                    if (btProfileConnected(s.btMac) && !btUp) { btUp = true; evaluate("bt:$mac") }
                }
                "DOWN" -> {
                    delay(5000)
                    if (!btProfileConnected(s.btMac) && btUp) { btUp = false; evaluate("bt gone") }
                }
            }
        }
    }

    private fun btProfileConnected(mac: String): Boolean {
        if (checkSelfPermission(android.Manifest.permission.BLUETOOTH_CONNECT)
            != PackageManager.PERMISSION_GRANTED) return false
        val bm = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        // 必须按 MAC 精确匹配已连接设备列表。早期版本查的是适配器级 profile 连接态
        // （getProfileConnectionState），连个耳机都会被误判成「目标设备在线」而触发录音。
        // 注意 SDK 37 起 BluetoothAdapter 已无 getConnectedDevices，该 API 只在 BluetoothManager 上。
        val target = mac.trim().uppercase()
        return runCatching {
            bm.getConnectedDevices(BluetoothProfile.HEADSET).any { it.address?.uppercase() == target } ||
                    bm.getConnectedDevices(BluetoothProfile.A2DP).any { it.address?.uppercase() == target }
        }.getOrDefault(false)
    }

    // ---------- 触发/停止 ----------
    private fun triggerUp(type: String, detail: String) {
        if (CaptureState.triggerUp) return
        CaptureState.triggerUp = true
        CaptureState.triggerType = type
        scope.launch {
            AppDatabase.get(applicationContext).serviceEventDao()
                .insert(ServiceEvent(type = "PROBE_UP", detail = "$type $detail"))
            CaptureService.start(applicationContext, type, detail)
        }
    }

    private fun triggerDown(reason: String) {
        if (!CaptureState.triggerUp) return
        CaptureState.triggerUp = false
        scope.launch {
            AppDatabase.get(applicationContext).serviceEventDao()
                .insert(ServiceEvent(type = "PROBE_DOWN", detail = reason))
            // 暂停而非停止：服务保持前台存活，Mac/蓝牙回来时零人工干预无缝续录。
            // （Android 14+ 下麦克风 FGS 一旦 stop，后台就再也起不来了）
            CaptureService.pause(applicationContext)
        }
    }

    // ---------- 自检：60s 周期 ----------
    private suspend fun selfCheckLoop() {
        val db = AppDatabase.get(applicationContext)
        while (scope.isActive) {
            delay(60_000)
            if (!CaptureState.triggerUp) continue
            val alive = System.currentTimeMillis() - CaptureState.lastCaptureAliveAt < 180_000
            if (CaptureState.captureRunning) {
                // 心跳超 3 分钟视为僵死 → 拉起或发恢复通知
                if (!alive) {
                    db.serviceEventDao().insert(ServiceEvent(type = "CAPTURE_DIED"))
                    CaptureState.captureRunning = false
                    val ok = CaptureService.start(applicationContext, "RECOVER", "self-check")
                    if (ok) db.serviceEventDao().insert(ServiceEvent(type = "CAPTURE_RECOVERED"))
                }
            } else {
                // 已触发但服务不在：常驻/自动模式主动拉起（AUTO 下触发在线说明 Mac 或蓝牙至少一个在）。
                // MANUAL 模式不自动拉起——用户点了「停止」就该保持停止，
                // 自动重启会让「停止」按钮看起来失灵。
                // ALWAYS 模式同样尊重 userStopped，否则停止后 60s 又会被拉起。
                val s = settings.current()
                val shouldKeep = s.triggerMode == com.symi.digitalhuman.data.TriggerMode.ALWAYS ||
                    (s.triggerMode == com.symi.digitalhuman.data.TriggerMode.AUTO && CaptureState.triggerUp)
                // 采集时段是硬边界：非时段内自检也不许把采集拉起来，
                // 否则时段关闭后每 60 秒就会被自检复活一次（2026-09-15 加）。
                val inWindow = CaptureWindow.isActiveNow(s.captureWindows)
                if (!s.paused && shouldKeep && inWindow && !CaptureState.userStopped) {
                    CaptureService.start(applicationContext, "RECOVER", "self-check-restart")
                }
            }
            // 解锁兜底场景由 MainActivity 打开时顺带自检
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 结束会话后请求立即重估：触发源仍在线时马上续录，不等下个探测周期
        if (intent?.action == ACTION_REEVALUATE) {
            scope.launch { evaluate("reevaluate") }
        }
        // App 刚被打开：进程可能是被系统强停后重建的（装机 / Force Stop），
        // 此时 macUp/btUp 全是初值 false，reevaluate 会静默什么都不做，采集
        // 只能干等下一轮探测（2026-09-11 实测装机后等了 33 秒）。这里先做一次
        // 同步探测再评估，把「打开 App 到真正开录」压到 2 秒内。
        if (intent?.action == ACTION_RESTORE) {
            android.util.Log.i(TAG, "收到 ACTION_RESTORE，按当前设置恢复采集")
            scope.launch { restoreNow() }
        }
        return START_STICKY
    }

    /**
     * 按当前设置立刻把采集恢复起来，不等探测周期。
     *
     * 触发源要真实探测，不能凭猜：AUTO 的本意是「人在电脑/手机旁才录」，
     * 为了让恢复快一点就直接开录，会在 Mac 离线时凭空录一整天。
     * MANUAL 不主动拉起——那是用户显式掌控的模式。
     */
    private suspend fun restoreNow() {
        val s = settings.current()
        // 留痕：这条路径是「打开 App 立即开录」唯一的显式入口，出问题时若没有日志，
        // 就只能靠「服务在不在」反推，排查成本极高（2026-09-11 实测踩过）。
        android.util.Log.i(TAG, "restoreNow: mode=${s.triggerMode} paused=${s.paused} " +
            "userStopped=${CaptureState.userStopped} running=${CaptureState.captureRunning} " +
            "window=${CaptureWindow.label(s.captureWindows)}@${CaptureWindow.nowHm()}")
        if (s.paused || CaptureState.userStopped || CaptureState.captureRunning) return
        // 采集时段闸门（2026-09-15）：非时段内不启动，并**明确留痕**。
        // 不留痕的话「打开 App 却没开始录」会被误判成探测失败或服务没起来，
        // 排查成本极高（这条路径本来就是靠这一行日志才能自查的）。
        if (!CaptureWindow.isActiveNow(s.captureWindows)) {
            android.util.Log.i(TAG, "restoreNow → 当前不在采集时段（${CaptureWindow.label(s.captureWindows)}），不启动采集")
            return
        }
        when (s.triggerMode) {
            com.symi.digitalhuman.data.TriggerMode.ALWAYS -> {
                android.util.Log.i(TAG, "restoreNow → ALWAYS，直接开录")
                triggerUp("ALWAYS", "app-open")
            }
            com.symi.digitalhuman.data.TriggerMode.AUTO -> {
                val mac = s.macIp.isNotBlank() && tcpProbe(s.macIp, s.macPort)
                val bt = s.btMac.isNotBlank() && btProfileConnected(s.btMac)
                // 顺手刷新「上传链路走哪条路」给 UI：App 一打开就显示对，
                // 不必等下一个探测周期（人在外网时尤其重要，见 probeLoop 注释）
                CaptureState.lastLinkViaRelay = if (mac) {
                    false
                } else {
                    com.symi.digitalhuman.sync.Endpoints.probe(s, force = true)?.viaRelay
                }
                if (mac) macUp = true
                if (bt) btUp = true
                android.util.Log.i(TAG, "restoreNow 探测: mac=$mac(${s.macIp}:${s.macPort}) bt=$bt(${s.btMac})")
                if (macUp || btUp) {
                    val type = if (macUp && btUp) "AUTO" else if (macUp) "LAN" else "BT"
                    triggerUp(type, "app-open")
                } else {
                    // 都不在线：交给 evaluate 走离线降级（常驻采集）而不是什么都不做
                    android.util.Log.i(TAG, "restoreNow → 触发源均离线，走离线降级评估")
                    evaluate("app-open-offline")
                }
            }
            else -> android.util.Log.i(TAG, "restoreNow: MANUAL 模式，等用户点「开始录音」")
        }
    }

    override fun onDestroy() {
        scope.cancel()
        btReceiver?.let { try { unregisterReceiver(it) } catch (_: Exception) {} }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null

    companion object {
        private const val NOTIF_ID = 1001
        private const val TAG = "GuardSvc"
        const val ACTION_REEVALUATE = "com.symi.digitalhuman.action.REEVALUATE"
        const val ACTION_RESTORE = "com.symi.digitalhuman.action.RESTORE"

        /**
         * 采集时段巡检周期。30 秒对分钟级的时段边界足够精确，
         * 又不会给长期驻留的终端增加可感知的功耗。
         */
        private const val WINDOW_CHECK_MS = 30_000L

        fun start(ctx: Context) {
            val i = Intent(ctx, GuardService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        /** 请求守护服务立即重估触发状态（结束会话后快速续录用） */
        fun reevaluate(ctx: Context) {
            val i = Intent(ctx, GuardService::class.java).setAction(ACTION_REEVALUATE)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        /**
         * App 打开时调用：先真实探测一次触发源，再把采集恢复起来。
         *
         * 与 [reevaluate] 的区别是「不依赖进程内的 macUp/btUp」——进程被系统强停
         * 后重建时这两个标志都是初值 false，reevaluate 会静默什么都不做。
         */
        fun requestRestore(ctx: Context) {
            val i = Intent(ctx, GuardService::class.java).setAction(ACTION_RESTORE)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }
    }
}
