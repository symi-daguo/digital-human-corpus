package com.symi.digitalhuman.service

/** 跨进程共享的采集状态（GuardService 自检 / UI 读取） */
object CaptureState {
    /** MainActivity 是否处于前台（Android 14+ 决定能否启动 microphone 前台服务） */
    @Volatile var activityResumed: Boolean = false
    @Volatile var captureRunning: Boolean = false
    @Volatile var sessionId: Long = -1L
    @Volatile var lastCaptureAliveAt: Long = 0L
    @Volatile var pendingRecoverNotify: Boolean = false

    /**
     * 最近一次有效段落盘时刻（0 = 本次进程启动后尚无落盘）。
     *
     * 与 [lastCaptureAliveAt]（录音循环心跳，只要在录就一直在跳）刻意分开：
     * 心跳只证明「AudioRecord 还在读」，不证明「真录到了有意义的话」。用户
     * 2026-09-11 反馈「讲了 20 分钟没有任何数据」时，心跳一直是新鲜的，真正
     * 该看的是「多久没落盘」。前台通知与状态页都用它，用户一眼能判断采集是
     * 不是真在工作。
     */
    @Volatile var lastSegmentAt: Long = 0L

    /** 实时拾音音量（归一化 0~1，recordLoop 每个 chunk 更新，UI 波形用） */
    @Volatile var level: Float = 0f

    /** 用户暂停拾音：会话与服务保持存活，仅停止单写入与 VAD（恢复后无缝续录） */
    @Volatile var capturePaused: Boolean = false

    /**
     * 触发暂停（AUTO 模式）：Mac 与蓝牙全部离线，服务保持前台存活但停止写入并结束当前会话。
     * 任一触发重新在线时由 Guard 发 RESUME，开新会话无缝续录。
     * 与 capturePaused（用户拾音暂停）分离：两个标志互相独立，任一为真都不落盘。
     */
    @Volatile var triggerPaused: Boolean = false

    /** 回放中：服务自动暂停喂数据，避免把外放声音录进语料；播完自动恢复 */
    @Volatile var playingBack: Boolean = false

    /**
     * 正在播报管家回复（实时对话）。与 [playingBack] 分开记：两者都让 recordLoop
     * 挂起拾音，但语义不同——[playingBack] 是用户点「播放」听录音，本标志是
     * AUTO 模式下自动外放回复。UI 要分开提示，否则用户会以为「我没点播放啊」。
     */
    @Volatile var talkPlaying: Boolean = false

    // 探测状态
    @Volatile var triggerUp: Boolean = false
    @Volatile var triggerType: String = ""
    @Volatile var lastProbeAt: Long = 0L
    @Volatile var lastProbeOk: Boolean = false

    /**
     * 生效上传链路是否走**外网中转**（null = 局域网与中转都不通）。
     *
     * 与 [lastProbeOk] 刻意分开，两者语义不同、外网下必然一真一假：
     *   [lastProbeOk]     = 局域网直连通不通（AUTO 的「人在电脑旁」靠它）
     *   [lastLinkViaRelay] = 上传链路通不通、走的哪条路（与用户在不在家无关）
     * UI 必须分开说，否则人一走外网就看到「Mac 不可达」，误以为整套都断了
     * ——2026-09-21 排查「手机远程连不上 Mac」时定位到的直接成因。
     */
    @Volatile var lastLinkViaRelay: Boolean? = null

    /**
     * 用户主动点过「停止录音」。
     * ALWAYS 模式的 alwaysLoop / selfCheckLoop 会周期性把采集拉起来，
     * 没有这个标志的话「停止」按钮会在 15 秒内被自动撤销，看起来像失灵。
     * 只有用户再点「开始录音」或切换触发模式才清除。
     */
    @Volatile var userStopped: Boolean = false

    /**
     * AUTO 模式下「Mac + 蓝牙全部离线 → 已降级为常驻采集」。
     *
     * 与 triggerType 分开存：降级期间 triggerType 仍是 "ALWAYS"，但 UI 需要
     * 区分「用户主动选的常驻」和「因为 Mac 掉线临时兜底的常驻」——
     * 后者在 Mac 回来后会自动回到 AUTO，副标题必须如实反映，否则用户会困惑
     * 「我明明设的是自动，怎么显示常驻」。
     */
    @Volatile var offlineFallback: Boolean = false
}
