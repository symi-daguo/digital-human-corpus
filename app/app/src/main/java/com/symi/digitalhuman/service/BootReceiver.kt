package com.symi.digitalhuman.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** 开机/升级自启：启动守护服务（系统豁免清单内允许从后台启动） */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED -> GuardService.start(context)
        }
    }
}
