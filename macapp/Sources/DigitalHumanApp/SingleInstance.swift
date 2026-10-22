import AppKit
import Darwin
import Foundation

/// 单实例保护 —— **同一台机器上「服务端」只能有一个**。
///
/// 为什么必须自己做：macOS 上至少有**三条互相不知道对方**的拉起路径
///   ① 用户双击 / `open -a`（走 LaunchServices，本来会复用已有实例）
///   ② 本 app 自己写的 LaunchAgent（`com.symi.digitalhuman`，带 `--autostart`）
///   ③ 系统「登录项」（系统设置 → 通用 → 登录项与扩展）
/// ②③ 都在登录时触发，彼此无感知。实测 2026-09-18 09:33:47 / 09:33:48 ——
/// 两个实例相差 1 秒被同时拉起（`launchctl list` 里能同时看到
/// `application.com.symi.digitalhuman.*` 与 `com.symi.digitalhuman` 两个 job），
/// 菜单栏于是出现**两个图标**：一个「运行中」、一个「未启动」。
///
/// 更糟的是 `ServiceManager.start()` 会先 `pkill` 清场（本意是清上一次 SIGKILL
/// 留下的孤儿）。后启动的那个实例会把先启动那个**正在正常运行的**服务端子进程
/// 一起杀掉，然后自己拉起一套 —— 于是「一个壳在干活、另一个空转」，
/// 用户看到两个图标却只有一个能用，点错那个还会把正在跑的服务停掉。
///
/// 权威判据用**文件锁**（`flock`）而不是只比 bundle id：
///   - 进程被 SIGKILL 时内核自动释放锁，**不存在过期锁**（这是不用「写 PID 文件 +
///     启动时检查 PID 是否存活」那套的原因：PID 会被复用，判断不可靠）；
///   - 不依赖 bundle identifier，直接在 bundle 外跑二进制调试时同样有保护。
enum SingleInstance {

    /// 锁文件句柄。必须活到进程结束 —— 一旦 close 就等于把锁放了。
    nonisolated(unsafe) private static var lockFD: Int32 = -1

    /// 尝试成为唯一实例。返回 `false` 表示已经有实例在跑。
    ///
    /// 取不到锁文件时**放行**（返回 true）：磁盘只读之类的异常不该把整个 app 挡死，
    /// 那种情况下多开一个的代价远小于完全用不了。
    static func claim() -> Bool {
        let dir = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(Paths.appSupportFolder)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appendingPathComponent("app.lock").path

        let fd = open(path, O_RDWR | O_CREAT, 0o644)
        guard fd >= 0 else { return true }
        // LOCK_NB：拿不到立刻返回，不要挂起等待（挂起会让「双击没反应」更难查）
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            return false
        }
        lockFD = fd

        // 把 PID 写进锁文件纯粹为了排障：出问题能一眼看出锁在谁手里
        ftruncate(fd, 0)
        let line = "\(getpid())\n"
        _ = line.withCString { write(fd, $0, strlen($0)) }
        return true
    }

    /// 把已经在跑的那个实例唤到前台，然后自己退出。
    ///
    /// 菜单栏 app 没有 Dock 图标（LSUIElement），用户双击时若只是静默退出，
    /// 观感就是「点了没反应」。所以先激活已有实例的主窗口再退。
    static func activateExistingAndExit() {
        let me = getpid()
        if let bid = Bundle.main.bundleIdentifier {
            for app in NSRunningApplication.runningApplications(withBundleIdentifier: bid)
            where app.processIdentifier != me {
                // `activate()`（无参版，macOS 14+）取代了
                // `activate(options: .activateIgnoringOtherApps)` —— 后者在
                // macOS 14 起已被弃用且不再生效。本包 platforms 就是 .macOS(.v14)，
                // 直接用新 API，不留弃用告警。
                app.activate()
                break
            }
        }
        // 用 exit 而不是让流程继续：此刻 SwiftUI 的 Scene 还没建，
        // 直接终止最干净，不会在菜单栏闪出一个空图标。
        exit(0)
    }
}
