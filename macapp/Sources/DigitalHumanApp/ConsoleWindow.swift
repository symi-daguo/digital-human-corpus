import SwiftUI
import WebKit

// ============================================================================
// 主窗口 = 服务端自己渲染的工作台
//
// 为什么让窗口装一个网页、而不是把网页那套布局在 SwiftUI 里再画一遍：
// 两端各画一遍，就得靠人去维持「同步」——实际结果就是 mac 与 Windows 长得不一样、
// 改了一边忘了另一边（用户反馈「我希望你统一界面，mac 和 win 的界面和 logo 都是统一的，
// 功能也都是一致的」）。这里物理上只有一份 HTML：`server/app/console.py` 的 `_PAGE`，
// 浏览器直开、Windows 桌面壳、mac 主窗口用的都是它。要改界面只有一处可改。
//
// 原生那 7 页（MainWindowView）没有删，作为兜底保留：万一内嵌口起不来、
// 或用户就是想用原生控件，菜单栏「原生界面（备用）」随时切过去。宁可多留一条退路，
// 也不要出现「界面打不开就彻底没有界面」。
// ============================================================================

// MARK: - WKWebView 包装

struct ConsoleWebView: NSViewRepresentable {
    let url: URL
    /// 由外壳注入到 localStorage 的配对 Token（取自 env.local 的 DH_TOKEN）
    let token: String
    /// 变了就重新加载（失败页的「重试」）
    let reloadID: UUID
    let onFail: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFail: onFail) }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.addUserScript(
            WKUserScript(source: Self.bootstrapScript(token: token),
                         injectionTime: .atDocumentStart,
                         forMainFrameOnly: true))
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        // 工作台是「一页应用」，返回手势只会退到上一个 hash，没有意义还容易误触。
        view.allowsBackForwardNavigationGestures = false
        // 加载过程中 WebKit 默认铺白底，深色窗口里会先闪一下白再变黑。
        view.underPageBackgroundColor = NSColor(red: 0x0e / 255.0, green: 0x10 / 255.0,
                                                blue: 0x14 / 255.0, alpha: 1)
        context.coordinator.loadedID = reloadID
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        // SwiftUI 因为任何状态变化都会重跑 updateNSView（服务状态每 15 秒探一次）。
        // 无条件 load() 会把用户刚点开的页签、填了一半的表单整个冲掉 ——
        // 只在 reloadID 真的换了的时候重载。
        guard context.coordinator.loadedID != reloadID else { return }
        context.coordinator.loadedID = reloadID
        view.load(URLRequest(url: url))
    }

    /// 页面启动脚本。
    ///
    /// 必须走 `.atDocumentStart`：页面主体脚本一执行就把 localStorage 读进 TOKEN 变量了，
    /// 注入晚一步就得等下次刷新才生效（症状是「窗口里一直是 Token 不对」）。
    ///
    /// Token 每次都覆盖、空间只在首次给默认值，这两件事不一样：
    /// Token 的权威来源是 env.local（应用里可以轮换它，覆盖才跟得上）；
    /// 而空间是用户在窗口里选的，绝对不能每次启动都被打回 default ——
    /// 那等于「昨天在张三的空间里干活，今早打开变成看自己的数据」。
    static func bootstrapScript(token: String) -> String {
        var js = "(function () {\n  try {\n"
        if !token.isEmpty {
            js += "    localStorage.setItem('dh_token', \(jsLiteral(token)));\n"
        }
        js += "    if (!localStorage.getItem('dh_tenant')) localStorage.setItem('dh_tenant', 'default');\n"
        js += "  } catch (e) {}\n})();"
        return js
    }

    /// 拼成 JS 字符串字面量。Token 是用户能改的值（可能带引号或反斜杠），
    /// 直接拼进脚本源码会让整段注入语法出错，而症状只是「窗口里 Token 一直是空的」，
    /// 从现象完全看不出是这里的问题。走一遍 JSON 序列化最省事也最安全。
    static func jsLiteral(_ s: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [s]),
              let text = String(data: data, encoding: .utf8) else { return "\"\"" }
        return String(text.dropFirst().dropLast())   // 去掉外层数组的方括号
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate {
        var loadedID: UUID?
        private let onFail: (String) -> Void

        init(onFail: @escaping (String) -> Void) { self.onFail = onFail }

        /// 连不上、HTTP 出错、导航被取消之外的失败都走这两个回调。
        ///
        /// 特意区分「取消」（-999）：页面自己发起 load() 会把上一次未完成的导航取消掉，
        /// 那不是失败，报出来就是「明明加载好了却弹一个错误框」。
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            report(error)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            report(error)
        }

        private func report(_ error: Error) {
            let ns = error as NSError
            if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
            onFail(error.localizedDescription)
        }
    }
}

// MARK: - 主窗口内容

/// UserDefaults 的 key 集中在这里。
///
/// 单独抽出来是因为同一个 key 有两处读者（主窗口的 @AppStorage、菜单栏的开关），
/// 写成两个字符串字面量的话，改名字时漏掉一处不会报错，只会静默失效
/// ——开关点了没反应，最难查的那类问题。
enum AppKeys {
    /// 主窗口用原生界面还是内嵌工作台
    static let nativeConsoleUI = "useNativeConsoleUI"
}

/// 主窗口内容：服务就绪后就是内嵌工作台；未就绪给原生等待页，加载失败给能自救的失败页。
struct ConsoleHostView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var theme: ThemeManager

    /// 当前显示哪一套界面。默认就是工作台（用户要的「统一界面」）；
    /// 走 @AppStorage 而不是 @State：菜单栏里那个开关要能改同一份值，
    /// 而且用户明确切过一次原生界面之后，下次打开应该还是原生界面。
    @AppStorage(AppKeys.nativeConsoleUI) private var showingNative = false
    @State private var reloadID = UUID()
    @State private var failure: String?
    /// 服务**曾经**就绪过。
    ///
    /// 用它而不是直接看 `status == .running`：健康探测每 15 秒一次，机器忙（正在转写）时
    /// 偶发一次超时就会把状态打成「启动中」，那时窗口若跟着切回等待页，用户会看到
    /// 正在看的页面被整片替换掉、十几秒后又自己回来。就绪过一次以后就始终留在工作台上，
    /// 服务真挂了由页面自己的状态点和「刷新」按钮表达。
    @State private var everReady = false

    var body: some View {
        Group {
            if showingNative {
                MainWindowView()
            } else {
                console
            }
        }
        .onChange(of: service.status) { _, new in
            guard new == .running else { return }
            // 重启后回到运行态要重新载入：旧页面里那些 fetch 全部是失败态，
            // 不重载的话用户看到的是一个「连不上」的页面，而服务其实已经好了。
            if everReady { reloadID = UUID() }
            everReady = true
            failure = nil
        }
    }

    // MARK: 内嵌工作台

    @ViewBuilder
    private var console: some View {
        ZStack {
            if everReady || service.status == .running {
                if let url = service.consoleWindowURL {
                    ConsoleWebView(url: url,
                                   token: service.apiToken,
                                   reloadID: reloadID,
                                   onFail: { failure = $0 })
                        .ignoresSafeArea()
                } else {
                    Panel(title: "工作台地址无效",
                          detail: "端口配置读不出来，检查环境数据目录里的 env.local。",
                          symbol: "exclamationmark.triangle") {
                        Button("打开原生界面") { showingNative = true }
                    }
                }
            } else {
                waiting
            }

            if let failure {
                // 失败页盖在工作台上而不是替换它：底层那个 WebView 留着，
                // 点「重试」只是让它再 load 一次，不用重建整个视图。
                failPanel(failure)
            }
        }
        .frame(minWidth: 720, minHeight: 480)
    }

    private var waiting: some View {
        Panel(title: service.status.label,
              detail: service.detail.isEmpty ? startHint : service.detail,
              symbol: service.status.symbol,
              busy: service.status == .starting) {
            switch service.status {
            case .stopped, .failed:
                Button("启动服务") { service.start() }
                    .buttonStyle(.borderedProminent)
            default:
                EmptyView()
            }
            Button("打开原生界面") { showingNative = true }
        }
    }

    private var startHint: String {
        service.status == .starting
            ? "首次启动要加载语音模型，约十几秒。"
            : "服务没在跑，工作台要等服务起来才能打开。"
    }

    private func failPanel(_ message: String) -> some View {
        Panel(title: "工作台打不开", detail: message,
              symbol: "exclamationmark.triangle") {
            Button("重试") { failure = nil; reloadID = UUID() }
                .buttonStyle(.borderedProminent)
            Button("在浏览器打开") { service.openConsole() }
            Button("打开原生界面") { showingNative = true }
        }
        .background(.regularMaterial)
    }
}

// MARK: - 通用提示面板

/// 等待 / 失败两种状态共用的一块面板。
///
/// 内容居中、图标用应用自身的图标：这两个面板是「界面本身出不来」的时候唯一能看到的东西，
/// 上面有 logo 至少能让用户确认「打开的是哪个应用、有没有装错版本」。
private struct Panel<Actions: View>: View {
    let title: String
    let detail: String
    let symbol: String
    var busy = false
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 12) {
            Image(nsImage: NSApplication.shared.applicationIconImage)
                .resizable()
                .frame(width: 56, height: 56)
            HStack(spacing: 7) {
                if busy {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: symbol).foregroundStyle(.secondary)
                }
                Text(title).font(.headline)
            }
            Text(detail)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            HStack(spacing: 8) { actions }
                .padding(.top, 2)
        }
        .padding(36)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
