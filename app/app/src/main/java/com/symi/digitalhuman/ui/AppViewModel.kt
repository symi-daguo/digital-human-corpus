package com.symi.digitalhuman.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.symi.digitalhuman.BuildConfig
import com.symi.digitalhuman.data.AppDatabase
import com.symi.digitalhuman.data.AppSettings
import com.symi.digitalhuman.data.CaptureWindow
import com.symi.digitalhuman.data.ENROLL_SECONDS
import com.symi.digitalhuman.data.DailyReport
import com.symi.digitalhuman.data.Segment
import com.symi.digitalhuman.data.Session
import com.symi.digitalhuman.data.SettingsStore
import com.symi.digitalhuman.data.StatHour
import com.symi.digitalhuman.data.TriggerMode
import com.symi.digitalhuman.data.TalkMode
import com.symi.digitalhuman.service.CaptureService
import com.symi.digitalhuman.service.GuardService
import com.symi.digitalhuman.sync.EnrollQueue
import com.symi.digitalhuman.sync.MacClient
import com.symi.digitalhuman.service.CaptureState
import com.symi.digitalhuman.worker.WorkScheduler
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** UI 共享 ViewModel：DB 观察流 + 设置 + 服务状态快照刷新 */
class AppViewModel(app: Application) : AndroidViewModel(app) {
    private val db = AppDatabase.get(app)
    private val store = SettingsStore(app)

    private val dayFmt = SimpleDateFormat("yyyy-MM-dd", Locale.US)

    init {
        // 启动后延迟拉一次外网中转提示：服务可能还没就绪，失败静默，
        // 用户保存设置时会再触发。中转地址从此不用手抄。
        viewModelScope.launch {
            delay(20_000)
            syncRelayHint()
        }
    }

    /** 从服务端拉外网中转提示自动填写（自适应连接第二跳）。用户手填优先，绝不覆盖。 */
    fun syncRelayHint() {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val s = settings.value
            if (s.relayAddress.isNotBlank()) return@launch
            val client = MacClient(s)
            val (reachable, _) = client.checkReachable()
            if (!reachable) return@launch
            val hint = client.fetchRelayHint()?.trim().orEmpty()
            if (hint.isNotEmpty()) {
                store.update(relayAddress = hint)
                android.util.Log.i("AppViewModel", "外网中转已自动填写: $hint")
            }
        }
    }

    // ---------- 设置 ----------
    val settings: StateFlow<AppSettings> = store.settings
        .stateIn(viewModelScope, SharingStarted.Eagerly, defaultSettings())

    private fun defaultSettings() = AppSettings(
        // 出厂默认同 SettingsStore：来自不入库的 app/dh.properties
        macIp = BuildConfig.DEFAULT_MAC_HOST, macPort = 8766, token = AppSettings.DEFAULT_TOKEN, btMac = "",
        processMinuteOfDay = 1050, retentionDays = 7, maxStorageMb = 0,
        probeIntervalSec = 30, paused = false,
        triggerMode = TriggerMode.MANUAL, pinnedCertSha256 = "",
        realtimeUpload = true,
        realtimeIntervalMin = AppSettings.DEFAULT_REALTIME_INTERVAL_MIN,
        offlineAlwaysFallback = true,
        uploadOnMetered = true,
        relayAddress = "",
        ownerName = "",
        tenantId = "",
        talkMode = TalkMode.MANUAL,
        talkRecordSec = 6,
        talkKwsFilter = true,
        captureWindows = CaptureWindow.ALL_DAY,
    )
    fun saveSettings(s: AppSettings) = viewModelScope.launch {
        val modeChanged = settings.value.triggerMode != s.triggerMode
        store.update(
            macIp = s.macIp, macPort = s.macPort, token = s.token, btMac = s.btMac,
            processMinuteOfDay = s.processMinuteOfDay, retentionDays = s.retentionDays,
            maxStorageMb = s.maxStorageMb, probeIntervalSec = s.probeIntervalSec,
            triggerMode = s.triggerMode, pinnedCertSha256 = s.pinnedCertSha256,
            realtimeUpload = s.realtimeUpload, realtimeIntervalMin = s.realtimeIntervalMin,
            offlineAlwaysFallback = s.offlineAlwaysFallback, relayAddress = s.relayAddress,
            uploadOnMetered = s.uploadOnMetered,
            ownerName = s.ownerName, tenantId = s.tenantId,
            talkMode = s.talkMode, talkRecordSec = s.talkRecordSec,
            talkKwsFilter = s.talkKwsFilter,
            talkScope = s.talkScope,
            captureWindows = s.captureWindows,
        )
        // 处理时间 / 准实时开关变更后重排调度
        WorkScheduler.scheduleAll(
            getApplication(), s.processMinuteOfDay,
            s.realtimeUpload, s.realtimeIntervalMin, s.uploadOnMetered,
        )
        // 切换触发模式视为用户重新授权采集：清除上一次「主动停止」的抑制，
        // 否则从 ALWAYS 停掉后切到别的模式再切回来，常驻逻辑永远不会再启动。
        CaptureState.userStopped = false
        // 换模式时清掉旧模式的触发残留，避免 AUTO 状态机 / ALWAYS 常驻循环
        // 拿着上一个模式的 triggerUp/triggerType 做错误判断。
        // 采集中不断录：ALWAYS 会自然接管，MANUAL 由用户按钮掌控。
        if (modeChanged) {
            CaptureState.triggerUp = false
            CaptureState.triggerType = ""
        }
        // 采集时段改完立刻重估：否则要等下一次 30 秒巡检才生效，
        // 用户改完盯着状态页看到「还在录 / 还没录」会以为设置没保存上。
        GuardService.reevaluate(getApplication())
        // 保存后顺手补拉中转提示：用户刚填好 token/地址时服务可达概率最高
        syncRelayHint()
    }

    fun setPaused(v: Boolean) = viewModelScope.launch { store.setPaused(v) }

    /**
     * 按需把 Mac 端指定日期的处理结果同步进本地 Room（**不触发上传**）。
     *
     * 场景（2026-09-21）：日报页原本只读 Room，而 Room 只由 DailyUploadWorker
     * 的周期任务写入（准实时 15-20 分钟一次）。用户讲完一段、打开日报页发现
     * 没有新内容，就会判断成「上传了但没处理」。改成打开页面主动拉一次，
     * 服务端已处理完的内容立刻可见。失败静默（下次周期任务仍会兜底）。
     */
    fun syncReportNow(date: String) = viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
        runCatching {
            com.symi.digitalhuman.worker.DailyUploadWorker.syncReportNow(getApplication(), date)
        }.onFailure {
            android.util.Log.w("AppViewModel", "同步日报失败: ${it.message}")
        }
    }

    // ---------- 设置页：按天重传（1.0.4） ----------

    /**
     * 有本地音频的日期（yyyy-MM-dd，倒序）。
     *
     * 服务端按回收策略删了 wav 之后，手机本地这段（受「音频保留天数」约束）
     * 就是唯一的备份来源；设置页据此列出可重传的日期。
     */
    private val _daysWithAudio = kotlinx.coroutines.flow.MutableStateFlow<List<String>>(emptyList())
    val daysWithAudio: kotlinx.coroutines.flow.StateFlow<List<String>> = _daysWithAudio

    fun refreshDaysWithAudio() = viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
        runCatching {
            _daysWithAudio.value = db.segmentDao().activeOffsets()
                .map { dayFmt.format(Date(it)) }
                .distinct()
                .sortedDescending()
        }
    }

    /**
     * 重传某天的全部本地段。
     *
     * 为什么安全：服务端落库日期 = `datetime.fromtimestamp(X-Start-Offset-Ms)`，
     * 每个段带自己的绝对时间戳，重传只会落回它原本那一天，**不会串到别的日期**；
     * 同一 install 的同一 seg_id 走 upsert + 覆盖同名 wav，幂等。
     *
     * 重传完成后服务端把该日期拉进补跑队列重算（清稿/总结/关键词都会刷新）。
     */
    fun reuploadDay(date: String, onDone: (Int) -> Unit) =
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val n = runCatching {
                val start = dayFmt.parse(date)?.time ?: return@runCatching 0
                val ids = db.segmentDao().ofDay(start, start + 86_400_000L).map { it.id }
                if (ids.isNotEmpty()) {
                    db.segmentDao().requeue(ids)
                    com.symi.digitalhuman.worker.DailyUploadWorker.runNow(getApplication(), date)
                }
                ids.size
            }.getOrDefault(0)
            withContext(kotlinx.coroutines.Dispatchers.Main) { onDone(n) }
        }

    // ---------- 设置页：一键抓取 Mac 证书指纹 ----------
    sealed interface CertProbeState {
        data object Idle : CertProbeState
        data object Loading : CertProbeState
        /** 抓到的指纹（base64 SPKI SHA-256），由设置页填进草稿 */
        data class Ok(val fingerprint: String) : CertProbeState
        data class Fail(val message: String) : CertProbeState
    }

    private val _certProbe = MutableStateFlow<CertProbeState>(CertProbeState.Idle)
    val certProbe: StateFlow<CertProbeState> = _certProbe

    /**
     * 按草稿里的 IP/端口去抓 Mac 证书指纹。
     *
     * 传草稿而不是已保存设置：用户往往是先改了 IP 再来点这个按钮，
     * 用已保存值会去连旧机器，抓回来一个对不上的指纹。
     */
    fun fetchCertFingerprint(draft: AppSettings) {
        if (_certProbe.value is CertProbeState.Loading) return
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            _certProbe.value = CertProbeState.Loading
            val (ok, result) = MacClient(draft).fetchCertFingerprint()
            _certProbe.value = if (ok) CertProbeState.Ok(result) else CertProbeState.Fail(result)
        }
    }

    fun resetCertProbe() { _certProbe.value = CertProbeState.Idle }

    // ---------- 手动开始 / 停止录音（不依赖 Mac 是否在线） ----------
    /**
     * 手动开始被采集时段拦截时的一次性中文提示。
     * 用 StateFlow 而不是 Toast：状态页要能持续显示到用户下一次操作。
     */
    private val _windowBlocked = MutableStateFlow<String?>(null)
    val windowBlocked: StateFlow<String?> = _windowBlocked
    fun clearWindowBlocked() { _windowBlocked.value = null }

    /**
     * 用户主动开始录音。仅用于 MANUAL / ALWAYS 模式下的显式控制。
     *
     * 注意必须同步置 triggerUp：GuardService 的自检循环以它为开关，
     * 不置位的话服务被系统回收后不会自愈。
     */
    fun startCapture() {
        // 采集时段是硬边界：窗口外不启动。UI 侧已经把按钮拦住了，这里再兜一道——
        // 万一状态页在窗口边界上没刷新过来，用户点了「开始」却在 30 秒后被
        // windowLoop 收尾，会以为是 bug。直接不开，行为才是可预期的。
        val spec = settings.value.captureWindows
        if (!CaptureWindow.isActiveNow(spec)) {
            android.util.Log.i(
                "AppViewModel",
                "手动开始被采集时段拦截：${CaptureWindow.label(spec)} @ ${CaptureWindow.nowHm()}"
            )
            _windowBlocked.value = "当前不在采集时段（${CaptureWindow.label(spec)}），未开始采集"
            return
        }
        CaptureState.triggerUp = true
        CaptureState.triggerType = "MANUAL"
        CaptureState.capturePaused = false
        // 解除「用户主动停止」：ALWAYS 的常驻循环重新获得拉起许可
        CaptureState.userStopped = false
        CaptureService.start(getApplication(), "MANUAL", "user-start")
    }

    /**
     * 用户主动停止采集。
     * 除了清 triggerUp，还要置 userStopped —— 否则 ALWAYS 模式的 alwaysLoop
     * 会在 15 秒内重新拉起，用户会以为「停止」按钮坏了。
     * userStopped 的解除：点「开始录音」、换触发模式，或离开后重新回到触发源旁
     * （GuardService 在触发上升沿重新武装）。
     */
    fun stopCapture() {
        CaptureState.triggerUp = false
        CaptureState.triggerType = ""
        CaptureState.capturePaused = false
        CaptureState.userStopped = true
        CaptureService.stop(getApplication())
    }

    /**
     * 结束会话（拾音控制卡片）：归档当前采集，与「停止采集」是两回事。
     * MANUAL 是用户显式掌控，停止后保持停止；AUTO/ALWAYS 的采集由触发源/常驻语义决定，
     * 不置 userStopped —— 否则一次归档就把自动采集永久按停（实测 Mac 在线也一直「未触发」），
     * Guard 的状态收敛会在下个探测周期自动续上新会话。
     */
    fun endSession() {
        val manual = settings.value.triggerMode == TriggerMode.MANUAL
        CaptureState.triggerUp = false
        CaptureState.triggerType = ""
        CaptureState.capturePaused = false
        CaptureState.userStopped = manual
        CaptureService.stop(getApplication())
        // AUTO：请 Guard 立即重估（Mac 还在线就马上续录，不等下个探测周期）
        if (!manual) GuardService.reevaluate(getApplication())
    }

    // ---------- 服务状态快照（CaptureState 是全局 volatile 变量，用 tick 定时刷新 UI） ----------
    val tick: StateFlow<Int> = flow {
        var i = 0
        while (true) { emit(i++); delay(2000) }
    }.stateIn(viewModelScope, SharingStarted.Eagerly, 0)

    // ---------- 今日数据 ----------
    val todaySessions: StateFlow<List<Session>> = db.sessionDao()
        .observeBetween(dayStart(), dayStart() + 86_400_000L)
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    val todayStats: StateFlow<List<StatHour>> = db.statDao()
        .observeDate(dayFmt.format(Date()))
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    val pendingCount: StateFlow<Int> = kotlinx.coroutines.flow.flow {
        while (true) { emit(db.segmentDao().pending().size); delay(5000) }
    }.stateIn(viewModelScope, SharingStarted.Eagerly, 0)

    val recentEvents: StateFlow<List<com.symi.digitalhuman.data.ServiceEvent>> = db.serviceEventDao()
        .observeRecent(8)
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    // ---------- 日报 ----------
    val reports: StateFlow<List<DailyReport>> = db.reportDao()
        .observeAll()
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    // ---------- 统计页：近 7 天会话（聚合展示） ----------
    val weekSessions: StateFlow<List<Session>> = db.sessionDao()
        .observeBetween(dayStart() - 6 * 86_400_000L, dayStart() + 86_400_000L)
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    val weekSegments: StateFlow<List<Segment>> = kotlinx.coroutines.flow.flow {
        while (true) {
            val end = System.currentTimeMillis()
            val start = end - 7 * 86_400_000L
            emit(db.sessionDao().listBetween(start, end).flatMap { s ->
                db.segmentDao().ofSession(s.id).filter {
                    it.filePath.isNotBlank() && !CaptureService.isInvalidSegment(it.durationMs)
                }
            })
            delay(10_000)
        }
    }.stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    // ---------- 操作 ----------
    fun uploadNow() = WorkScheduler.uploadNow(getApplication())

    // ---------- 拾音暂停（保持会话与服务存活，仅停止单写入） ----------
    fun toggleMicPause() { CaptureState.capturePaused = !CaptureState.capturePaused }

    // ---------- 声纹注册：弹窗流程（填名字 → 录样本 → 试听 → 提交/入队） ----------
    sealed interface EnrollState {
        data object Idle : EnrollState
        /** 录音中，secondsLeft 为剩余秒数 */
        data class Recording(val secondsLeft: Int, val name: String) : EnrollState
        /** 录完待确认：可试听、可重录、可提交 */
        data class Recorded(val name: String, val file: java.io.File) : EnrollState
        data object Uploading : EnrollState
        data object Success : EnrollState
        /** 已存入本地队列，等 Mac 上线后自动补交 */
        data class Pending(val name: String) : EnrollState
        data class Error(val message: String) : EnrollState
    }

    private val _enroll = kotlinx.coroutines.flow.MutableStateFlow<EnrollState>(EnrollState.Idle)
    val enroll: StateFlow<EnrollState> = _enroll

    private var enrollJob: kotlinx.coroutines.Job? = null

    /**
     * 只录一段样本，完全不碰网络，录完停在 [EnrollState.Recorded] 等用户确认。
     *
     * 录音与提交必须分开：旧实现要求「先探通 Mac 再开麦克风」，于是不在 Mac 旁边时
     * 根本没法录，回到 Mac 旁还要再花 30 秒重录一遍。现在录音只依赖麦克风，
     * 录下来的样本即使 Mac 不在线也留在本地，连上后自动补交（见 [submitEnrollSample]）。
     */
    fun recordEnrollSample(name: String) {
        val st = _enroll.value
        if (st is EnrollState.Recording || st is EnrollState.Uploading) return
        enrollJob?.cancel()
        enrollJob = viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val file = java.io.File(
                getApplication<Application>().cacheDir,
                "enroll_${System.currentTimeMillis()}.wav",
            )
            // 采集服务正占着麦克风，两个 AudioRecord 无法同时打开——必须先让出。
            val wasCapturing = CaptureState.captureRunning
            if (wasCapturing) {
                CaptureState.triggerUp = false
                CaptureService.stop(getApplication())
                delay(800)
            }
            try {
                recordWav(file, ENROLL_SECONDS) { left ->
                    _enroll.value = EnrollState.Recording(left, name)
                }
                // 被取消（用户点「取消」）：半成品不能留，否则会被当成有效样本提交
                if (!currentCoroutineContext().isActive) {
                    file.delete()
                    return@launch
                }
                _enroll.value = EnrollState.Recorded(name, file)
            } catch (e: Exception) {
                file.delete()
                _enroll.value = EnrollState.Error(e.message ?: "录音失败")
            } finally {
                if (wasCapturing) {
                    delay(500)
                    CaptureState.triggerUp = true
                    CaptureService.start(getApplication(), "RECOVER", "after-enroll")
                }
            }
        }
    }

    /**
     * 提交样本：Mac 在线直接注册；不在线就进本地队列，之后自动补交。
     *
     * 在线但注册失败（名字非法、服务端报错）也先入队——样本是用户花 30 秒录的，
     * 比失败原因值钱，留着让下一轮自动重试，好过让人再录一遍。
     */
    fun submitEnrollSample(name: String, file: java.io.File) {
        enrollJob?.cancel()
        enrollJob = viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            _enroll.value = EnrollState.Uploading
            val client = MacClient(settings.value)
            val (reachable, why) = client.checkReachable()
            if (!reachable) {
                EnrollQueue.enqueue(getApplication(), name, file)
                refreshPendingEnrolls()
                _enroll.value = EnrollState.Pending(name)
                return@launch
            }
            val (ok, msg) = client.registerVoiceprint(file, name)
            if (ok) {
                refreshSpeakers()
                _enroll.value = EnrollState.Success
            } else {
                EnrollQueue.enqueue(getApplication(), name, file)
                refreshPendingEnrolls()
                _enroll.value = EnrollState.Error("${msg.ifBlank { "注册失败" }} · 已暂存，联网后自动重试")
            }
        }
    }

    /** 放弃已录样本（重录 / 关闭弹窗） */
    fun cancelEnroll() {
        enrollJob?.cancel()
        enrollJob = null
        (_enroll.value as? EnrollState.Recorded)?.file?.delete()
        _enroll.value = EnrollState.Idle
    }

    fun resetEnroll() { _enroll.value = EnrollState.Idle }

    // ---------- 待补交的声纹样本 ----------
    /** 本地队列里还没提交成功的角色名（设置页显示「待提交」标记） */
    private val _pendingEnrolls = MutableStateFlow<List<String>>(emptyList())
    val pendingEnrolls: StateFlow<List<String>> = _pendingEnrolls

    fun refreshPendingEnrolls() {
        _pendingEnrolls.value = EnrollQueue.names(getApplication())
    }

    // 注意位置：这个 init 必须写在 _pendingEnrolls 之后。Kotlin 按声明顺序初始化，
    // 把 init 放在类顶部会在 _pendingEnrolls 还没赋值时就去写它的 value，
    // 构造直接 NPE —— App 一打开就闪退（实测踩过）。
    init {
        // 待补交声纹样本自动提交：App 进程活着时每 20 秒试一次。
        // 队列为空时 flushPendingEnrolls 一条网络请求都不发，常态开销为零。
        // delay 放循环开头：构造期间 launch 的协程可能立刻跑，先等一个周期
        // 保证类属性全部初始化完成，也避免刚进 App 就发探测请求。
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            while (isActive) {
                delay(20_000)
                runCatching { flushPendingEnrolls() }
            }
        }
    }

    /**
     * 有队列就试着补交。队列为空时直接返回，一次网络请求都不发——
     * 这是常驻 20 秒一轮的轮询，绝大多数轮次应该是零开销的。
     */
    private suspend fun flushPendingEnrolls(): Int {
        val ctx = getApplication<Application>()
        if (EnrollQueue.list(ctx).isEmpty()) return 0
        val client = MacClient(settings.value)
        if (!client.checkReachable().first) return 0
        val n = EnrollQueue.flush(ctx, client)
        if (n > 0) {
            refreshPendingEnrolls()
            refreshSpeakers()
        }
        return n
    }

    // ---------- 样本试听 ----------
    private var samplePlayer: android.media.MediaPlayer? = null
    private val _samplePlaying = MutableStateFlow(false)
    val samplePlaying: StateFlow<Boolean> = _samplePlaying

    fun playSample(file: java.io.File) {
        stopSample()
        try {
            val p = android.media.MediaPlayer().apply {
                setDataSource(file.absolutePath)
                setOnCompletionListener { _samplePlaying.value = false }
                setOnErrorListener { _, _, _ -> _samplePlaying.value = false; true }
                prepare()
                start()
            }
            samplePlayer = p
            _samplePlaying.value = true
        } catch (e: Exception) {
            _samplePlaying.value = false
        }
    }

    fun stopSample() {
        runCatching { samplePlayer?.stop() }
        runCatching { samplePlayer?.release() }
        samplePlayer = null
        _samplePlaying.value = false
    }

    // ---------- 具名角色管理（本人 / 同事…） ----------
    /**
     * Mac 端已注册的具名角色名字列表。
     *
     * 注册过的同事在日报里直接显示姓名，不再出现甲乙丙丁——这是工作环境里
     * 让日报可读的关键：匿名标签对熟人是无效信息。
     */
    private val _speakers = MutableStateFlow<List<String>>(emptyList())
    val speakers: StateFlow<List<String>> = _speakers

    /** 角色操作结果提示（删除失败原因等），null = 无待展示提示 */
    private val _speakerMessage = MutableStateFlow<String?>(null)
    val speakerMessage: StateFlow<String?> = _speakerMessage

    fun refreshSpeakers() {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            // 拉不到就保留上一次结果：网络抖动时不该把已注册列表清成空白，
            // 那会让用户以为角色丢了而重复注册。
            MacClient(settings.value).listSpeakers()?.let { _speakers.value = it }
        }
    }

    fun deleteSpeaker(name: String) {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val (ok, msg) = MacClient(settings.value).deleteSpeaker(name)
            _speakerMessage.value = if (ok) "已删除角色「$name」" else "删除失败：$msg"
            if (ok) refreshSpeakers()
        }
    }

    fun clearSpeakerMessage() { _speakerMessage.value = null }

    // ---------- 行业词表：决定转写热词与文本纠偏用的术语表 ----------

    /** Mac 端当前行业与可选列表；null = 还没拉到（未连过 Mac 或拉取失败） */
    private val _industry = MutableStateFlow<MacClient.IndustryInfo?>(null)
    val industry: StateFlow<MacClient.IndustryInfo?> = _industry

    private val _industryMessage = MutableStateFlow<String?>(null)
    val industryMessage: StateFlow<String?> = _industryMessage

    fun refreshIndustries() {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            // 同 speakers：拉不到就保留上次结果，网络抖动不该把选项清成空白
            MacClient(settings.value).listIndustries()?.let { _industry.value = it }
        }
    }

    fun setIndustry(key: String) {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val (ok, msg) = MacClient(settings.value).setIndustry(key)
            _industryMessage.value = msg
            if (ok) refreshIndustries()
        }
    }

    fun clearIndustryMessage() { _industryMessage.value = null }

    // ---------- 数据备份：触发 Mac 端全量归档（/api/backup） ----------
    sealed interface BackupState {
        data object Idle : BackupState
        data object Running : BackupState
        /** 备份成功，message 含归档文件名与大小 */
        data class Ok(val message: String) : BackupState
        data class Fail(val message: String) : BackupState
    }

    private val _backup = MutableStateFlow<BackupState>(BackupState.Idle)
    val backup: StateFlow<BackupState> = _backup

    /**
     * 触发一次 Mac 端数据备份。
     * 与声纹注册同一顺序：先探 Mac 在线（失败原因更精准），再触发备份，
     * 避免网络不通时用户对着一排原始异常发懵。
     */
    fun backupNow() {
        if (_backup.value is BackupState.Running) return
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            _backup.value = BackupState.Running
            val client = MacClient(settings.value)
            val (reachable, why) = client.checkReachable()
            if (!reachable) {
                _backup.value = BackupState.Fail(why)
                return@launch
            }
            val (ok, msg) = client.triggerBackup()
            _backup.value = if (ok) BackupState.Ok(msg) else BackupState.Fail(msg)
        }
    }

    fun resetBackup() { _backup.value = BackupState.Idle }

    // ---------- 导入 / 导出（2026-09-15） ----------
    /**
     * 导出/恢复的进行态与结果。
     *
     * 与 [BackupState] 分开：备份卡片上「打包中」与「导出中」是两件事，
     * 共用一个状态会让「点了备份，卡片却显示导出结果」。
     */
    sealed interface TransferState {
        data object Idle : TransferState
        /** what = 中文动作名（如「导出」「恢复」），用于文案「正在导出……」 */
        data class Busy(val what: String) : TransferState
        data class Ok(val message: String, val file: java.io.File? = null) : TransferState
        data class Fail(val message: String) : TransferState
    }

    private val _transfer = MutableStateFlow<TransferState>(TransferState.Idle)
    val transfer: StateFlow<TransferState> = _transfer

    /** 服务端已有备份归档列表；元素含 file / size / mtime */
    private val _backups = MutableStateFlow<List<Map<String, String>>>(emptyList())
    val backups: StateFlow<List<Map<String, String>>> = _backups

    /** 备份列表的一次性提示（如「共 3 份备份」/「连不上 Mac」） */
    private val _backupListMsg = MutableStateFlow<String?>(null)
    val backupListMsg: StateFlow<String?> = _backupListMsg

    fun resetTransfer() { _transfer.value = TransferState.Idle }

    /** 导出目录：应用外部文件目录下的 backups/（用户可用文件管理器取走） */
    private fun exportDir(): java.io.File =
        java.io.File(getApplication<Application>().getExternalFilesDir(null), "backups")

    /** 拉取服务端备份列表。失败只更新提示，不清空已有列表（避免网络抖动把列表刷没）。 */
    fun refreshBackups() {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val client = MacClient(settings.value)
            val (ok, msg, list) = client.listBackups()
            _backupListMsg.value = msg
            if (ok) _backups.value = list
        }
    }

    // ---------- TTS 测试播报（设置页按钮，2026-09-15） ----------
    /**
     * 用户在设置页点「测试播报」后的状态文案。
     * null = 还没点过（不占位）；其余都是可直接展示的中文。
     */
    private val _ttsTest = MutableStateFlow<String?>(null)
    val ttsTest: StateFlow<String?> = _ttsTest

    private val _ttsTesting = MutableStateFlow(false)
    val ttsTesting: StateFlow<Boolean> = _ttsTesting

    /**
     * 拉服务端合成的「今天数据」播报并放出来。
     *
     * 这个动作同时验证三件事，所以放在设置页而不是单独做界面：
     * ① 网络与 Token 通（拿不到音频说明这条路断了）
     * ② 服务端 TTS 后端可用（503 会带出当前后端名）
     * ③ 本机扬声器与播放链路正常（复用真实回话的播放实现）
     */
    fun testTts() {
        if (_ttsTesting.value) return
        _ttsTesting.value = true
        _ttsTest.value = "正在合成播报语音…"
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val client = com.symi.digitalhuman.sync.MacClient(settings.value)
            val r = client.fetchTtsPreview()
            if (r.ok) {
                _ttsTest.value = if (r.script.isBlank()) "播报中…" else "播报中，内容：${r.script}"
                com.symi.digitalhuman.sync.TalkController.speakTest(getApplication(), r.wav)
            } else {
                _ttsTest.value = "测试失败：${r.message}"
            }
            _ttsTesting.value = false
        }
    }

    /** 清掉上次的测试结果（用户下次点按钮时不该还挂着上一次的文案） */
    fun resetTtsTest() { _ttsTest.value = null }

    /**
     * 把服务端某份备份导出到本机。
     *
     * 导出成功后把文件留在 [exportDir]，UI 提供「分享」按钮把它发出去——
     * 手机端这才是「导出」的完整闭环：拿得到、也发得走。
     */
    fun exportBackup(name: String) {
        if (_transfer.value is TransferState.Busy) return
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            _transfer.value = TransferState.Busy("导出")
            val client = MacClient(settings.value)
            val dest = java.io.File(exportDir(), name)
            val (ok, msg) = client.downloadBackup(name, dest)
            _transfer.value = if (ok) TransferState.Ok(msg, dest) else TransferState.Fail(msg)
        }
    }

    /**
     * 从本机文件恢复服务端数据（破坏性）。
     *
     * 先把选中的内容流式拷进 cache/import/ —— 内容 Uri 在进程切换后可能失效，
     * 而恢复上传是异步的；拷成本地文件后整个流程都建立在真实文件上，更稳。
     */
    fun importBackup(uri: android.net.Uri) {
        if (_transfer.value is TransferState.Busy) return
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            _transfer.value = TransferState.Busy("恢复")
            val ctx = getApplication<Application>()
            val dir = java.io.File(ctx.cacheDir, "import").apply { mkdirs() }
            val tmp = java.io.File(dir, "restore_${System.currentTimeMillis()}.zip")
            try {
                ctx.contentResolver.openInputStream(uri)?.use { ins ->
                    tmp.outputStream().use { outs -> ins.copyTo(outs, 64 * 1024) }
                } ?: run {
                    _transfer.value = TransferState.Fail("读不到所选文件（可能已被删除或没有授权）")
                    return@launch
                }
                if (tmp.length() <= 0) {
                    _transfer.value = TransferState.Fail("所选文件是空的")
                    return@launch
                }
                val client = MacClient(settings.value)
                val (ok, msg) = client.restoreBackup(tmp)
                _transfer.value = if (ok) TransferState.Ok(msg) else TransferState.Fail(msg)
                if (ok) {
                    // 恢复后数据整体换了一批：刷新列表与今日统计，避免界面停留在旧快照
                    refreshBackups()
                }
            } catch (e: Exception) {
                _transfer.value = TransferState.Fail("恢复失败：${e.javaClass.simpleName} ${e.message.orEmpty()}")
            } finally {
                // 待恢复包是临时产物，无论成败都不留在手机里（可能含全部语料）
                tmp.delete()
            }
        }
    }

    /**
     * 分享已导出的备份包。
     *
     * 必须走 FileProvider：Android 7 起直接把 file:// 交给别的应用会抛
     * FileUriExposedException，而且这是系统级拦截，捕获不到就只能崩溃。
     */
    fun shareExported(file: java.io.File) {
        runCatching {
            val ctx = getApplication<Application>()
            val uri = androidx.core.content.FileProvider.getUriForFile(
                ctx, "${ctx.packageName}.fileprovider", file
            )
            val i = android.content.Intent(android.content.Intent.ACTION_SEND).apply {
                type = "application/zip"
                putExtra(android.content.Intent.EXTRA_STREAM, uri)
                addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            val chooser = android.content.Intent.createChooser(i, "分享备份包")
                .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(chooser)
        }.onFailure {
            _transfer.value = TransferState.Fail("无法分享：${it.message.orEmpty()}")
        }
    }

    // ---------- 重要时刻标记（DayoTag 式随手标记） ----------
    private val _markMessage = MutableStateFlow<String?>(null)
    val markMessage: StateFlow<String?> = _markMessage

    /** 把当前时刻记为重要时刻：服务端落 markers.json，日报多一张「重要时刻」卡片 */
    fun markNow() {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val t = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
            val date = dayFmt.format(Date())
            val (_, msg) = MacClient(settings.value).markNow(date, t)
            _markMessage.value = msg
        }
    }

    fun clearMarkMessage() { _markMessage.value = null }

    // ------------------------------------------------------------------
    // 实时对话（Hermes 对接，2026-09-10）
    // ------------------------------------------------------------------
    private val _talkMessage = MutableStateFlow<String?>(null)
    val talkMessage: StateFlow<String?> = _talkMessage

    private val _talkBusy = MutableStateFlow(false)
    val talkBusy: StateFlow<Boolean> = _talkBusy

    /**
     * 打断正在播报的管家回复（2026-09-10）。
     *
     * 状态页播报期间会多出一个「停止播报」按钮，点它走这里。非挂起、不抛异常
     * ——它只置一个标志位，真正的收尾由持播放器的那条协程做
     * （见 `TalkController.stopPlayback`）。
     */
    fun stopTalkPlayback() {
        com.symi.digitalhuman.sync.TalkController.stopPlayback("用户点击停止")
        _talkMessage.value = "已停止播报"
    }

    /**
     * 手动对话：录一段 → 上传 → 播放管家回复。
     *
     * 忙碌期间直接忽略重复点击——录制中再点一次会开出第二个 AudioRecord，
     * 在 MIUI 上会互相抢麦导致两次都录成静音。
     */
    fun talkNow() {
        if (_talkBusy.value) return
        // 必须在 IO 线程：manualTalk 内部一路走到 OkHttp 的阻塞 execute()，
        // 在主线程上会被 NetworkOnMainThreadException 拦下，客户端只看到
        // 「连不上 Mac 服务」而服务端一条日志都没有（2026-09-10 真机实测踩到）。
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            _talkBusy.value = true
            _talkMessage.value = "正在录音，请说「大锅…」"
            val outcome = try {
                com.symi.digitalhuman.sync.TalkController.manualTalk(getApplication())
            } catch (e: Exception) {
                com.symi.digitalhuman.sync.TalkController.Outcome.Failed("对话失败：${e.message}")
            }
            _talkMessage.value = when (outcome) {
                is com.symi.digitalhuman.sync.TalkController.Outcome.Ok ->
                    "你说：${outcome.asr}\n管家（${outcome.via}）：${outcome.reply}"
                is com.symi.digitalhuman.sync.TalkController.Outcome.Skipped ->
                    if (outcome.reason == "no_alias")
                        "听到「${outcome.asr}」，但没有唤醒词——先说「大锅」再讲指令"
                    else "这一段没有有效语音"
                is com.symi.digitalhuman.sync.TalkController.Outcome.Failed -> outcome.msg
            }
            _talkBusy.value = false
        }
    }

    fun clearTalkMessage() { _talkMessage.value = null }

    /** 录一段固定时长 WAV（16kHz 单声道，与服务端声纹模型要求一致）。
     *  必须是 suspend：需要在循环里检查协程是否已取消（用户点「取消」）。 */
    private suspend fun recordWav(out: java.io.File, seconds: Int, onTick: (Int) -> Unit) {
        val sr = 16000
        val minBuf = android.media.AudioRecord.getMinBufferSize(
            sr, android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT)
        val rec = android.media.AudioRecord(
            android.media.MediaRecorder.AudioSource.MIC, sr,
            android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf, sr * 2))
        if (rec.state != android.media.AudioRecord.STATE_INITIALIZED) {
            rec.release()
            throw IllegalStateException("麦克风不可用（可能被其他应用占用）")
        }
        val writer = com.symi.digitalhuman.audio.WavWriter(out, sr)
        val chunk = ShortArray(1024)
        val total = sr * seconds
        var written = 0
        var lastLeft = -1
        try {
            writer.start()
            rec.startRecording()
            while (written < total && currentCoroutineContext().isActive) {
                val n = rec.read(chunk, 0, chunk.size)
                if (n <= 0) continue
                val remain = total - written
                if (n >= remain) writer.write(chunk, 0, remain) else writer.write(chunk, 0, n)
                written += n
                val left = ((total - written) + sr - 1) / sr
                if (left != lastLeft) { lastLeft = left; onTick(left) }
            }
        } finally {
            runCatching { rec.stop() }
            rec.release()
            writer.close()
        }
    }


    // ---------- 录音回放：按天分组 + 分页 ----------
    /** 全部可回放段（有音频文件），倒序 */
    val playableSegments: StateFlow<List<Segment>> = db.segmentDao()
        .observePlayable()
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    /** 有录音的日期列表（yyyy-MM-dd，倒序），供日期选择器 */
    val availableDays: StateFlow<List<String>> = playableSegments
        .map { segs -> segs.map { dayFmt.format(Date(it.startOffsetMs)) }.distinct() }
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    // 用户手动选中的日期；null = 自动（今天优先，否则最近有录音的一天）
    private val _selectedDay = MutableStateFlow<String?>(null)

    /** 当前选中的日期 */
    val selectedDay: StateFlow<String> = combine(_selectedDay, availableDays) { sel, days ->
        val today = dayFmt.format(Date())
        sel ?: (days.firstOrNull { it == today } ?: days.firstOrNull() ?: today)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, dayFmt.format(Date()))

    /** 选中日期下的可回放段（倒序） */
    val segmentsOfSelectedDay: StateFlow<List<Segment>> =
        combine(playableSegments, selectedDay) { segs, day ->
            segs.filter { dayFmt.format(Date(it.startOffsetMs)) == day }
        }.stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    fun selectDay(day: String) { _selectedDay.value = day }

    private val _playback = kotlinx.coroutines.flow.MutableStateFlow<Segment?>(null)
    val playback: StateFlow<Segment?> = _playback

    private var player: android.media.MediaPlayer? = null

    /** 点击段：播放中则停止；否则开始播放。播放期间采集自动暂停，播完自动恢复 */
    fun togglePlayback(seg: Segment) {
        if (_playback.value?.id == seg.id) { stopPlayback(); return }
        stopPlayback()
        try {
            val p = android.media.MediaPlayer()
            p.setAudioAttributes(
                android.media.AudioAttributes.Builder()
                    .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SPEECH)
                    .setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                    .build()
            )
            p.setDataSource(seg.filePath)
            p.setOnCompletionListener { stopPlayback() }
            p.setOnErrorListener { _, _, _ -> stopPlayback(); true }
            CaptureState.playingBack = true   // 服务侧暂停喂数据，外放声不会录进语料
            p.prepare()
            p.start()
            player = p
            _playback.value = seg
        } catch (e: Exception) {
            stopPlayback()
        }
    }

    fun stopPlayback() {
        player?.let { runCatching { it.stop(); it.release() } }
        player = null
        _playback.value = null
        CaptureState.playingBack = false   // 自动恢复拾音
    }

    override fun onCleared() {
        stopPlayback()
        stopSample()
        super.onCleared()
    }

    private fun dayStart(): Long {
        val cal = java.util.Calendar.getInstance().apply {
            set(java.util.Calendar.HOUR_OF_DAY, 0)
            set(java.util.Calendar.MINUTE, 0)
            set(java.util.Calendar.SECOND, 0)
            set(java.util.Calendar.MILLISECOND, 0)
        }
        return cal.timeInMillis
    }
}
