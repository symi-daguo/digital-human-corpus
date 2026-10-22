package com.symi.digitalhuman.ui

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import com.symi.digitalhuman.service.CaptureService
import com.symi.digitalhuman.service.CaptureState
import com.symi.digitalhuman.service.GuardService
import com.symi.digitalhuman.ui.root.AppRoot
import com.symi.digitalhuman.ui.theme.DigitalHumanTheme
import com.symi.digitalhuman.worker.WorkScheduler

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        // 必须在 super.onCreate 之前调用：接管系统启动屏并应用主题。
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            DigitalHumanTheme { AppRootWithPermissions() }
        }
    }

    // Android 14+ 是否允许启动 microphone 前台服务，取决于 App 是否处于前台
    override fun onResume() {
        super.onResume()
        CaptureState.activityResumed = true
        // 进程被系统强停后重建（装机 / Force Stop）时 CaptureState 全部复位为初值，
        // 下面那条依赖 triggerUp 的恢复路径会静默跳过 —— 用户的直观感受就是
        // 「App 打开了却还在待机」。2026-09-11 实测：装机后打开 App，采集拖了
        // 33 秒才靠 Guard 探测循环兜底恢复。这里补一条不依赖 triggerUp 的路径，
        // 让 Guard 先真实探测一次触发源再决定是否开录（已在录则直接返回，零开销）。
        if (!CaptureState.userStopped) GuardService.requestRestore(this)
        // 后台续录被系统拦截后的自愈：GuardService 只在探测「边沿」才触发 evaluate，
        // Mac 回线时若 App 在后台，resume 被拒后 triggerPaused 永远清不掉（状态卡死）。
        // 回到前台后这里立即补一次恢复：
        // ① 服务存活但触发暂停 → 无缝续录（resume 对已运行 FGS 只是投递 intent，无后台启动限制）
        // ② 触发在线但服务已死 → 完整启动（前台时 start 必然成功）
        if (CaptureState.triggerUp && !CaptureState.userStopped) {
            if (CaptureState.captureRunning && CaptureState.triggerPaused) {
                CaptureService.resume(this, CaptureState.triggerType.ifBlank { "RECOVER" }, "activity-resume")
            } else if (!CaptureState.captureRunning) {
                CaptureService.start(this, "RECOVER", "activity-resume")
            }
        }
    }

    override fun onPause() {
        CaptureState.activityResumed = false
        super.onPause()
    }
}

/** 首次打开：申请权限 → 启动守护服务与定时任务 */
@androidx.compose.runtime.Composable
fun AppRootWithPermissions() {
    val ctx = LocalContext.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) ==
                    android.content.pm.PackageManager.PERMISSION_GRANTED
        )
    }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r ->
        granted = r[Manifest.permission.RECORD_AUDIO] == true
    }

    LaunchedEffect(Unit) {
        val perms = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
            if (Build.VERSION.SDK_INT >= 31) add(Manifest.permission.BLUETOOTH_CONNECT)
        }
        val missing = perms.filter {
            ContextCompat.checkSelfPermission(ctx, it) !=
                    android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) launcher.launch(missing.toTypedArray())
    }

    LaunchedEffect(granted) {
        if (granted) {
            GuardService.start(ctx)
            val s = com.symi.digitalhuman.data.SettingsStore(ctx).current()
            WorkScheduler.scheduleAll(
                ctx, s.processMinuteOfDay, s.realtimeUpload, s.realtimeIntervalMin,
                s.uploadOnMetered,
            )
            // 解锁兜底：若应采集但服务死亡且可前台启动，直接恢复
            if (com.symi.digitalhuman.service.CaptureState.triggerUp &&
                !com.symi.digitalhuman.service.CaptureState.captureRunning) {
                CaptureService.start(ctx, "RECOVER", "user_open")
            }
        }
    }

    AppRoot()
}

