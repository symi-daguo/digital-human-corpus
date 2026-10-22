import SwiftUI

/// 菜单栏下拉面板。
///
/// 定位是「快速操作」，不是主界面：状态一句话说清，主操作一个按钮直达主窗口，
/// 其余（日报、日志、设置）一律交给主窗口，避免菜单栏被撑成第二个应用。
struct MenuBarView: View {
    @EnvironmentObject var service: ServiceManager
    @State private var launchAtLogin = LaunchAgent.isEnabled
    @State private var launchAgentError: String?
    /// 界面选择的兜底开关，与主窗口 ConsoleHostView 读的是同一个 key。
    @AppStorage(AppKeys.nativeConsoleUI) private var useNativeUI = false

    // MARK: - 图标列
    //
    // 面板里有两行「图标 + 文字」（顶部状态行、下方文字模型行），两行的图标必须
    // 落在同一条竖线上。上一版顶部是 18pt 框的 SF Symbol、下面那行是 Label 自带的
    // 图标间距，实测两个图标中心差 2.0pt、两行文字左缘差 4.0pt —— 肉眼就是「没对齐」。
    // 统一成一个常量，谁也别自己量。
    private static let iconColumn: CGFloat = 20
    private static let iconSpacing: CGFloat = 8
    /// logo 的视觉重心补偿（上移量，pt）。
    ///
    /// 量测过：logo 的几何中心与右侧两行文字块的中心只差 0.25pt
    /// （logo y 95..135，文字块 y 88..141，同一条水平线上），严格说已经居中。
    /// 但 logo 图案的**视觉重量偏下** —— 上方是几乎透明的光弧、下方是实心的
    /// 肩膀，所以看起来「往下一沉」。这里上移 1pt，让视觉重心落回中线。
    private static let logoOpticalLift: CGFloat = 1

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            statusHeader
            if !service.detail.isEmpty {
                Text(service.detail)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // 文字模型单独显示：API 活着但 LLM 挂了 = 总结全空白，
            // 这种状态不暴露出来的话用户完全无从判断。
            // 用 HStack 而不是 Label：Label 的图标列宽由字形自己决定（同一段代码
            // 换一个字重就变），和顶部 logo 的 20pt 框对不齐。
            if !service.llmStatus.isEmpty {
                HStack(spacing: Self.iconSpacing) {
                    Image(systemName: "cpu")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .frame(width: Self.iconColumn)
                    Text(service.llmStatus)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }

            Divider()
            actions

            Divider()
            Toggle("登录时自动启动", isOn: $launchAtLogin)
                .font(.system(size: 12))
                .onChange(of: launchAtLogin) { _, on in applyLaunchAgent(on) }
            if let e = launchAgentError {
                Text(e).font(.system(size: 11)).foregroundStyle(.red)
            }

            Divider()
            // 界面兜底开关。默认关 = 主窗口就是网页工作台（与 Windows 端同一套 HTML）；
            // 勾上改用原生那 7 页。留这个开关的目的不是「两种界面并存」，
            // 而是内嵌口万一被占/起不来时，用户手上还有一个能用的界面。
            Toggle("主窗口用原生界面", isOn: $useNativeUI)
                .font(.system(size: 12))
                .help("默认是网页工作台（与 Windows 端完全一致）。勾选后换成 Mac 原生界面。")

            Divider()
            HStack {
                Button("数据目录") { service.revealDataFolder() }
                    .buttonStyle(.borderless)
                Spacer()
                Button("退出") { quit() }
                    .buttonStyle(.borderless)
            }
            .font(.system(size: 12))
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .frame(width: 300)
    }

    // MARK: - 子视图

    private var statusHeader: some View {
        HStack(spacing: Self.iconSpacing) {
            brandLogo
                .offset(y: -Self.logoOpticalLift)
            VStack(alignment: .leading, spacing: 1) {
                Text("数字人服务").font(.system(size: 13, weight: .medium))
                // 状态色从图标挪到副标题：换成 logo 之后，这一行是唯一还能表达
                // 「运行中 / 启动中 / 失败」的地方。丢掉它就等于把这个面板最该
                // 一眼看到的信息（服务到底起没起）删了。
                Text(statusSubtitle).font(.system(size: 11)).foregroundStyle(statusColor)
            }
            Spacer()
        }
    }

    /// 数字人 logo。
    ///
    /// 取的是 bundle 里的 `MenuBarLogo.png`（由 tools/make-app-icons.py 从图标
    /// 母版派生，与 mac/Windows 两端 app 图标、网页工作台顶栏同源），不是 SF Symbol
    /// —— 用户认的就是这个图标，菜单栏面板顶部必须和它一致。
    /// `swift run` 起的开发态没有 bundle 资源，回退到状态符号，免得开天窗。
    @ViewBuilder
    private var brandLogo: some View {
        if let img = Bundle.main.image(forResource: "MenuBarLogo") {
            Image(nsImage: img)
                .resizable()
                .interpolation(.high)
                .aspectRatio(contentMode: .fit)
                .frame(width: Self.iconColumn, height: Self.iconColumn)
                .accessibilityLabel("数字人服务")
        } else {
            Image(systemName: service.status.symbol)
                .font(.system(size: 15))
                .foregroundStyle(statusColor)
                .frame(width: Self.iconColumn, height: Self.iconColumn)
        }
    }

    @ViewBuilder
    private var actions: some View {
        Button {
            AppDelegate.shared?.showMainWindow()
        } label: {
            Label("打开主窗口", systemImage: "macwindow")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .keyboardShortcut("o")

        switch service.status {
        case .running:
            HStack(spacing: 6) {
                Button("重启") { service.restart() }
                    .buttonStyle(.bordered)
                Button("停止") { service.stop() }
                    .buttonStyle(.bordered)
                Button("工作台") { service.openConsole() }
                    .buttonStyle(.bordered)
            }
        case .starting:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("正在加载模型，首次启动约需十几秒")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        case .stopped, .failed:
            Button {
                service.start()
            } label: {
                Label("启动服务", systemImage: "play.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
    }

    // MARK: - 逻辑

    private var statusSubtitle: String {
        switch service.status {
        case .stopped:  "服务未运行"
        case .starting: "正在启动…"
        case .running:  "端口 \(Paths.apiPort) 已就绪"
        case .failed(let reason): reason
        }
    }

    private var statusColor: Color {
        switch service.status {
        case .stopped:  .secondary
        case .starting: .orange
        case .running:  .green
        case .failed:   .red
        }
    }

    private func applyLaunchAgent(_ on: Bool) {
        do {
            try LaunchAgent.setEnabled(on)
            launchAgentError = nil
        } catch {
            launchAtLogin = LaunchAgent.isEnabled
            launchAgentError = "写入失败：\(error.localizedDescription)"
        }
    }

    private func quit() {
        service.stop()
        NSApp.terminate(nil)
    }
}
