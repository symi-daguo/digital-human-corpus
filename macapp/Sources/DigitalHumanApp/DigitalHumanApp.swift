import SwiftUI

@main
struct DigitalHumanApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var service = ServiceManager.shared
    /// 首次访问即完成 UserDefaults 读取 + NSApp.appearance 应用，
    /// 保证「手动指定深色」在窗口创建前生效，不会闪一下浅色。
    @StateObject private var theme = ThemeManager.shared

    /// 单实例保护。**必须放在 init 里**，不能放到
    /// `applicationDidFinishLaunching`：那时 Scene 已经建好，
    /// `MenuBarExtra` 往菜单栏插的图标已经出现，再退出会闪一下 ——
    /// 而且更早退出才能保证第二个实例**根本不会**走到
    /// `ServiceManager.start()`（它开头会 pkill 清场，把第一个实例
    /// 正在跑的服务端子进程一起杀掉，详见 SingleInstance.swift）。
    init() {
        if !SingleInstance.claim() {
            SingleInstance.activateExistingAndExit()
        }
    }

    var body: some Scene {
        MenuBarExtra {
            MenuBarView()
                .environmentObject(service)
                .environmentObject(theme)
        } label: {
            menuBarStatusIcon
        }
        .menuBarExtraStyle(.window)
    }

    /// 菜单栏状态项只放一个小 logo，**不放任何文字**。
    ///
    /// 上一版是「waveform + 运行中」，三处不对：一是「运行中」三个字把图标挤到很右边，
    /// 与相邻系统图标不在一个视觉节奏上；二是菜单栏本就不该承担状态文字（弹窗顶部
    /// 已经有整行「数字人服务 / 端口 8766 已就绪」，那里空间够）；三是写死 `waveform`
    /// 表达不了「没在跑」。
    ///
    /// 现在颜色只表达一件事：
    /// - 运行中 → **模板图**（`isTemplate = true`）。系统只取 alpha、用菜单栏前景色
    ///   重绘，所以深色菜单栏上是白的、浅色菜单栏上是黑的，永远与旁边的 WiFi /
    ///   音量 / 控制中心同色。
    /// - 其余状态 → 另一张**固定 systemGray** 的图，并显式关掉模板。若也交给系统
    ///   重绘，「没在跑」就会显示成与运行中同色，灰不出来了。
    @ViewBuilder
    private var menuBarStatusIcon: some View {
        if let icon = Self.menuBarImage(running: service.status == .running) {
            Image(nsImage: icon)
                .resizable()
                .interpolation(.high)
                .aspectRatio(contentMode: .fit)
                .frame(width: Self.menuBarIconSide, height: Self.menuBarIconSide)
                .accessibilityLabel("数字人服务：\(service.status.label)")
        } else {
            // 资源缺失时退回系统符号，至少还能看出状态，不至于变成空白项
            Image(systemName: service.status.symbol)
        }
    }

    /// 菜单栏图标的显示边长。18pt 是 macOS 菜单栏图标的常规档位：16pt 跟系统图标
    /// 摆在一起偏弱，22pt（desktop 那份 tray.png 的档位）在 24pt 高的菜单栏里
    /// 几乎顶天立地、明显大一圈。图标资源本身出的是 36px（= 18pt @2x）。
    private static let menuBarIconSide: CGFloat = 18

    private static func menuBarImage(running: Bool) -> NSImage? {
        let name = running ? "MenuBarIcon" : "MenuBarIconIdle"
        guard let src = Bundle.main.image(forResource: name),
              let img = src.copy() as? NSImage else { return nil }
        // 先 copy 再改属性：`Bundle.image(forResource:)` 返回的是被缓存的对象，
        // 就地改 `isTemplate` 会污染之后所有取值。
        img.isTemplate = running
        img.size = NSSize(width: menuBarIconSide, height: menuBarIconSide)
        return img
    }
}

/// 标 @MainActor：窗口构造要读 `ServiceManager.shared`（MainActor 隔离），
/// 而 AppKit 的 delegate 回调与 SwiftUI 的按钮动作本来就在主线程上，
/// 只是编译器证明不了。整体标 MainActor 比在方法间来回 assumeIsolated 干净。
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    /// 供菜单栏入口唤出主窗口（MenuBarExtra 里的 SwiftUI 环境拿不到窗口控制器）
    ///
    /// AppDelegate 由 SwiftUI 的 @NSApplicationDelegateAdaptor 持有，生命周期与
    /// app 等长，不会提前释放。标 nonisolated(unsafe) 是因为它既被主线程（建窗口）
    /// 又被菜单栏面板访问，而 Swift 6 无法证明这种用法的并发安全性。
    nonisolated(unsafe) static weak var shared: AppDelegate?

    /// 是否恢复服务，默认 true。
    ///
    /// 语义是「延续上次的使用状态」，而不是「每次都拉起」：
    /// 用户在菜单栏点过「停止服务」就记为 false，下次启动保持停止；
    /// 否则（首次安装、上次是运行中被系统/用户退出）自动拉起。
    ///
    /// 为什么不让手动启动一律保持停止：这个 app 存在的唯一目的就是提供常驻服务，
    /// 手机端与局域网 agent 随时会来调用。要求用户每次开机都手动点一次，
    /// 等于让 agent 侧永远处于「可能连不上」的状态。
    /// 而用户真的不想跑，点一次「停止服务」就会被记住。
    private static let autoStartKey = "serviceAutoStart"

    /// 主窗口是否已向用户展示过。
    ///
    /// 菜单栏 app 天生没有窗口，安装后用户唯一能感知的就是状态栏多了一个图标；
    /// 图标还常常被 macOS 折叠起来，于是「装了等于没装」。
    /// 首次启动直接把主窗口推到前台，让「装上了、它在跑、产出在这里」一眼可见。
    private static let didShowMainWindowKey = "didShowMainWindow"

    /// 窗口位置记忆的 key（换过一次名，旧名下存过一个 1×32 的坏 frame）
    private static let mainWindowFrameName = "DigitalHumanMainWindow"

    /// 信号源要一直持有，释放了监听就失效
    private var signalSources: [DispatchSourceSignal] = []

    /// 主窗口。app 是 LSUIElement（不进 Dock、不进 Cmd+Tab），
    /// 窗口生命周期必须自己管：交给 SwiftUI 的 WindowGroup 在这种形态下
    /// 唤出时机不可控，所以这里直接持有 NSWindowController。
    private var mainWindowController: NSWindowController?

    /// 接管 SIGTERM / SIGINT。
    ///
    /// 这两个信号的默认行为是直接终止进程，**不会**走 NSApplication 的退出流程，
    /// 于是 `applicationWillTerminate` 根本不会被调用。实测后果：kill 掉 app 之后
    /// API 与 mlx-lm 两个进程照常活着，继续占着本地模型那几 GB 内存，
    /// 而菜单栏入口已经消失，用户没有任何办法再停掉它们。
    /// 这里自己接下信号，先杀子进程再退出。
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler {
                ServiceManager.killOrphan()
                // 给 uvicorn 一点时间走优雅关闭，别把正在写的 SQLite 留成脏页
                usleep(200_000)
                exit(0)
            }
            src.resume()
            signalSources.append(src)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppDelegate.shared = self
        installSignalHandlers()
        let ud = UserDefaults.standard
        // LaunchAgent 带 --autostart 时无条件拉起（用户勾选了「登录时自动启动」，
        // 意图就是让它常驻），不受上次状态影响。
        let force = CommandLine.arguments.contains("--autostart")
        guard force || ud.object(forKey: Self.autoStartKey) as? Bool ?? true else {
            showMainWindow()   // 服务不自动拉起时更要把窗口亮出来，否则用户无从下手
            return
        }
        ServiceManager.shared.start()

        if !ud.bool(forKey: Self.didShowMainWindowKey) {
            ud.set(true, forKey: Self.didShowMainWindowKey)
            // 等一拍再弹：服务正在启动，晚一秒窗口里的状态就是「启动中」而不是空白
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                self?.showMainWindow()
            }
        }
    }

    /// 唤出（或复用）主窗口。
    ///
    /// `isReleasedWhenClosed = false` 是关键：菜单栏 app 关窗口不等于退出，
    /// 窗口对象必须留着复用，否则第二次打开会访问已释放的 NSWindow 直接崩。
    func showMainWindow() {
        if let win = mainWindowController?.window {
            win.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        // delegate 回调与按钮动作都在主线程，这里同步构造即可
        makeMainWindow()
    }

    private func makeMainWindow() {
        // 根视图是「服务端工作台的宿主」，不是原生那 7 页 —— 用户要的是
        // mac / Windows / 浏览器三端同一套界面、同一个 logo、同一份功能。
        // 原生界面没删，菜单栏里有开关随时切过去（ConsoleHostView 按偏好分流）。
        let root = ConsoleHostView()
            .environmentObject(ServiceManager.shared)
            .environmentObject(ThemeManager.shared)
        // 默认尺寸按屏幕算而不是写死：14 寸笔记本上 1280 会顶满屏，
        // 27 寸外接屏上 1000 又显得局促。取可视区的比例再 clamp 到可用区间。
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1512, height: 900)
        let width = min(max(screen.width * 0.66, 960), 1280)
        let height = min(max(screen.height * 0.76, 620), 920)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        win.title = "数字人服务"
        // 工具栏样式（2026-09-16）。
        //
        // macOS 26 默认的浮动工具栏会**浮在内容之上**，并在自身下方拖一条同高的
        // 材质带（实测覆盖 52→104pt），把详情区首张卡片糊掉；上一版被迫把详情区
        // 内容整体下移 40pt 躲避，结果详情区比侧栏低一截、两边不对齐。
        // 换成传统 `.unified` 后工具栏占独立高度、不再覆盖内容区，
        // 详情区与侧栏从同一基线起排，遮挡与错位一起消失。
        //
        // 工作台那一支没有原生工具栏（页面自带顶栏），这一行对它是个空操作；
        // 留在原处是因为「切回原生界面」时它仍然必需 —— 那条路正是为了兜底才留的，
        // 兜底路径上的样式设置不能省。
        win.toolbarStyle = .unified
        win.contentViewController = NSHostingController(rootView: root)
        win.isReleasedWhenClosed = false
        // minSize 是必需的：SwiftUI 的 NavigationSplitView 算出来的 ideal size 极小，
        // hosting controller 会把窗口一路压缩到 1×32（实测），肉眼看上去就是「没反应」。
        // 下限给得比较小（720×480），是为了让用户在小屏 / 分屏下也能缩下去，
        // 而不是被一个写死的大最小尺寸卡住。
        win.minSize = NSSize(width: 720, height: 480)
        win.setContentSize(NSSize(width: width, height: height))
        win.setFrameAutosaveName(Self.mainWindowFrameName)
        // autosave 可能恢复出一个历史遗留的极小 frame，兜底拉回可用尺寸
        if win.frame.width < win.minSize.width || win.frame.height < win.minSize.height {
            win.setContentSize(win.minSize)
        }
        win.center()
        let wc = NSWindowController(window: win)
        mainWindowController = wc
        wc.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    static func setAutoStart(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: autoStartKey)
    }

    func applicationWillTerminate(_ notification: Notification) {
        ServiceManager.killOrphan()
    }

    /// 关闭最后一个窗口不退出：菜单栏 app 没有窗口，必须显式阻止默认行为，
    /// 否则打开过设置窗再关掉，app 会跟着退出、服务被一起杀掉。
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
