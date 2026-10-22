package com.symi.digitalhuman

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class App : Application() {

    companion object {
        const val CHANNEL_GUARD = "guard_service"
        const val CHANNEL_CAPTURE = "capture_service"
        const val CHANNEL_RECOVER = "recover_notice"

        fun context(): Context = AppHolder.ctx
    }

    /** 应用级协程作用域：只用于启动期的一次性初始化，随进程存亡，无需取消。 */
    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        AppHolder.ctx = this
        createChannels()
        // 安装实例标识（2026-09-15）：越早生成越好——它决定服务端的设备命名空间，
        // 第一次上传之前必须已存在，否则那一刻的段会落到「按设备名分空间」的退路里。
        // 失败不影响启动（服务端有退路），所以不阻塞、不抛。
        appScope.launch {
            runCatching { com.symi.digitalhuman.data.SettingsStore(this@App).ensureInstallId() }
        }
    }

    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_GUARD, "后台守护", NotificationManager.IMPORTANCE_MIN)
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_CAPTURE, "语音采集", NotificationManager.IMPORTANCE_LOW)
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_RECOVER, "采集恢复提醒", NotificationManager.IMPORTANCE_HIGH)
        )
    }
}

object AppHolder {
    lateinit var ctx: Context
}
