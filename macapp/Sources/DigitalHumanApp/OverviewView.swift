import SwiftUI

/// 上手引导是否已收起。
///
/// 概览页与 `GettingStartedCard` 共用同一个 key：引导卡收起后概览页只剩
/// 「配对」一张卡，这时它要独占整行 —— 列数上限得跟着这个状态走，
/// 否则宽屏下会留出半个空位（网格里只有 1 张卡、却按 2 列排）。
private let onboardingDismissedKey = "onboarding.dismissed"

/// 概览：一眼看清「服务在不在、用什么模型、产出多少、手机怎么连」。
///
/// 容器用 Theme 的 GlassCard、文字与强调色用 Theme token（DESIGN.md）；
/// 按钮、Grid、输入类控件保持系统原生，亮暗随 ThemeManager 切换。
struct OverviewView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient
    /// 只看不写：引导卡自己负责写这个 key，这里用来决定配对卡要不要与它并排
    @AppStorage(onboardingDismissedKey) private var onboardingDismissed = false

    private static let dayKey: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private var todayKey: String { Self.dayKey.string(from: Date()) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                hero
                if service.status != .running { offlineHint }
                metrics
                // 宽屏把「配对信息」和「快速开始」并排放，省掉一屏的滚动；
                // 窄屏竖排，硬塞两列会让每张卡都窄到读不了。
                // 引导卡收起后网格里只剩配对卡，列数上限降为 1 —— 让它独占整行，
                // 否则宽屏下会留下半列空白（「拉宽了卡片没跟着变」的同款观感）。
                // 分不分列交给 ResponsiveGrid 按实际宽度判断，不再依赖主窗口下发的
                // detailWidth（那个值实测一直是初始值，双栏分支从未走到过）。
                ResponsiveGrid(minColumnWidth: 420,
                               maxColumns: onboardingDismissed ? 1 : 2,
                               spacing: Theme.space4) {
                    pairing
                    GettingStartedCard()
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.pageH)
            .padding(.top, Theme.pageTop)
            .padding(.bottom, Theme.pageBottom)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .environmentObject(service)
        .environmentObject(api)
    }

    // MARK: - 状态区

    private var hero: some View {
        GlassCard {
            HStack(spacing: 15) {
                ZStack {
                    Circle()
                        .fill(statusColor.opacity(0.15))
                        .frame(width: 42, height: 42)
                    Image(systemName: service.status.symbol)
                        .font(.system(size: 19))
                        .foregroundStyle(statusColor)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(statusTitle)
                        .font(.system(size: 15, weight: .semibold))
                    Text(statusSubtitle)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 12)
                actions
            }
        }
    }

    @ViewBuilder
    private var actions: some View {
        switch service.status {
        case .running:
            Button {
                service.openConsole()
            } label: {
                Label("打开工作台", systemImage: "safari")
            }
            .buttonStyle(.borderedProminent)
            HStack(spacing: 8) {
                Button("重启") { service.restart() }
                Button("停止") { service.stop() }
            }
            .buttonStyle(.bordered)
        case .starting:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("正在加载语音模型，首次约需十几秒")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
            }
        case .stopped, .failed:
            Button {
                service.start()
            } label: {
                Label("启动服务", systemImage: "play.fill")
            }
            .buttonStyle(.borderedProminent)
        }
    }

    /// 服务没在跑时给一句可执行的提示，而不是让用户对着红色图标猜。
    @ViewBuilder
    private var offlineHint: some View {
        if let err = api.lastError {
            HStack(spacing: 8) {
                Image(systemName: "info.circle")
                    .foregroundStyle(.secondary)
                Text(err)
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                Spacer()
            }
        }
    }

    // MARK: - 指标

    private var metrics: some View {
        // 宽屏一行四张、窄屏自动降 3→2 列，每列等分铺满整行。
        // 断点不再手写 —— 交给 ResponsiveGrid 按这一层真正的可用宽度算，
        // 否则「宽度够放 5 列却只有 4 张卡」时右边会空掉近 200pt。
        ResponsiveGrid(minColumnWidth: 180, maxColumns: 4, spacing: Theme.space3) {
            MetricCard(
                title: "语音识别",
                value: asrValue,
                caption: "增量 \(incrementalBackend) · 日终 \(finalBackend)",
                symbol: "mic",
                tint: Theme.indigo
            )
            MetricCard(
                title: "文字模型",
                value: llmValue,
                caption: llmCaption,
                symbol: "cpu",
                tint: Theme.teal
            )
            MetricCard(
                title: "行业词表",
                value: industryValue,
                caption: industryCaption,
                symbol: "text.book.closed",
                tint: Theme.amber
            )
            MetricCard(
                title: "今日人声段",
                value: todayValue,
                caption: todayCaption,
                symbol: "waveform",
                tint: Theme.green
            )
        }
    }

    // MARK: - 配对信息

    private var pairing: some View {
        GlassCard(title: "手机端配对", symbol: "iphone") {
            VStack(alignment: .leading, spacing: 9) {
                Text("在手机 App 的「设置 → Mac 服务连接」里填写下面几项。"
                     + "外网中转选填：填了它，手机不在家也能自动改走云端同步。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 8) {
                    CopyableRow(label: "地址", value: lanAddress)
                    CopyableRow(label: "端口", value: "\(Paths.apiPort)")
                    CopyableRow(label: "Token", value: service.apiToken, secret: true)
                    if !relayHint.isEmpty {
                        CopyableRow(label: "外网中转", value: relayHint)
                    }
                }
            }
        }
    }

    // MARK: - 取值

    private var asrValue: String {
        guard let s = api.state else { return "—" }
        return s.models.asrFinalRewrite ? "两级" : "单级"
    }

    private var incrementalBackend: String { api.state?.models.asrIncremental ?? "—" }
    private var finalBackend: String { api.state?.models.asrFinal ?? "—" }

    private var llmValue: String {
        guard let s = api.state else { return "—" }
        return s.models.llmAvailable ? "已就绪" : "未就绪"
    }

    private var llmCaption: String {
        guard let s = api.state else { return "服务未运行" }
        let name = s.models.llmModel.isEmpty ? "未配置" : s.models.llmModel
        return name + (s.models.llmAvailable ? "" : " · 总结与干净稿会为空")
    }

    private var industryValue: String {
        guard let s = api.state else { return "—" }
        return s.industry.label
    }

    private var industryCaption: String {
        guard let s = api.state else { return "服务未运行" }
        return "\(s.industry.terms) 个术语 · \(s.industry.available.count) 个行业可选"
    }

    private var todayValue: String {
        guard let s = api.state else { return "—" }
        if let d = s.days.first(where: { $0.date == todayKey }) {
            return "\(d.speechSegments)"
        }
        return "0"
    }

    private var todayCaption: String {
        guard let s = api.state else { return "服务未运行" }
        let total = s.days.count
        if let d = s.days.first(where: { $0.date == todayKey }) {
            return d.ready ? "日报已生成 · 累计 \(total) 天" : "采集中 · 累计 \(total) 天"
        }
        return "今天还没有采集 · 累计 \(total) 天"
    }

    private var lanAddress: String {
        let ips = api.state?.service.lanIps ?? []
        // 手机连不了回环（127.*）与链路本地（169.254.*）地址；服务端把
        // 127.0.0.1 排在第一位，不过滤就会把没用的地址教给用户（实测踩坑）。
        let usable = ips.filter { !$0.hasPrefix("127.") && !$0.hasPrefix("169.254.") }
        // 192.168 是家用/办公最常见的网段，优先给
        return usable.first { $0.hasPrefix("192.168.") }
            ?? usable.first
            ?? "未检测到局域网地址"
    }

    /// 外网中转提示（env.local 的 DH_RELAY_HINT，选填）。手机自适应连接
    /// 的第二跳就填它；不配置则整行不显示。
    private var relayHint: String { service.relayHint }

    // MARK: - 外观

    private var statusTitle: String {
        switch service.status {
        case .stopped:  "服务未运行"
        case .starting: "正在启动"
        case .running:  "服务运行中"
        case .failed:   "服务异常"
        }
    }

    private var statusSubtitle: String {
        switch service.status {
        case .stopped:
            return "点击「启动服务」开始采集。手机端与局域网 agent 需要服务常驻才能调用。"
        case .starting:
            return "正在加载语音识别模型，请稍候…"
        case .running:
            let v = api.state?.service.version ?? "—"
            let llm = service.llmStatus.isEmpty ? "文字模型未知" : service.llmStatus
            return "版本 \(v) · 端口 \(Paths.apiPort) · \(llm)"
        case .failed(let reason):
            return reason
        }
    }

    private var statusColor: Color {
        switch service.status {
        case .stopped:  Theme.textTertiary
        case .starting: Theme.amber
        case .running:  Theme.green
        case .failed:   Theme.red
        }
    }
}

// MARK: - 子组件

private struct MetricCard: View {
    let title: String
    let value: String
    let caption: String
    let symbol: String
    let tint: Color

    var body: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 5) {
                Label(title, systemImage: symbol)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(tint)
                Text(value)
                    .font(.system(size: 16, weight: .semibold))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .monospacedDigit()
                Text(caption)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .lineLimit(2)
                Spacer(minLength: 0)
            }
        }
    }
}

private struct CopyableRow: View {
    let label: String
    let value: String
    var secret: Bool = false

    @State private var copied = false

    var body: some View {
        GridRow {
            Text(label)
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .gridColumnAlignment(.trailing)
            HStack(spacing: 8) {
                Text(secret ? masked : (value.isEmpty ? "未设置" : value))
                    .font(.system(size: 11.5).monospaced())
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if secret && !value.isEmpty {
                    Button {
                        Self.copy(value)
                        copied = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { copied = false }
                    } label: {
                        Image(systemName: copied ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 10))
                    }
                    .buttonStyle(.borderless)
                    .help("复制 Token")
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var masked: String {
        guard !value.isEmpty else { return "未设置" }
        return String(repeating: "•", count: min(value.count, 12))
    }

    private static func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}

/// 上手引导：三步走完，走完自动收起。
///
/// 用清单而不是模态向导 —— 模态向导会挡住窗口、必须一次点完，
/// 而这三件事本来就不需要按顺序做。状态用 @AppStorage 持久化。
private struct GettingStartedCard: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient
    // 与概览页共用同一个 key（见文件头的 onboardingDismissedKey）
    @AppStorage(onboardingDismissedKey) private var dismissed = false

    private var steps: [(String, Bool)] {
        let running = service.status == .running
        let hasDays = !(api.state?.days.isEmpty ?? true)
        let hasReady = api.state?.days.contains { $0.ready } ?? false
        return [
            ("启动本地服务", running),
            ("手机端完成配对并上传一段录音", hasDays),
            ("回到「日报」页查看转写与总结", hasReady),
        ]
    }

    private var allDone: Bool { steps.allSatisfy { $0.1 } }

    var body: some View {
        Group {
            if !dismissed { card }
        }
        // 三步都完成就自动收起：引导的意义就在于走完之后让它消失
        .onChange(of: allDone) { _, done in if done { dismissed = true } }
    }

    private var card: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 9) {
                HStack {
                    SectionHeader(title: "快速开始", symbol: "sparkles")
                    Spacer()
                    Button("隐藏") { dismissed = true }
                        .buttonStyle(.borderless)
                        .font(.system(size: 11))
                }
                ForEach(Array(steps.enumerated()), id: \.offset) { idx, step in
                    HStack(spacing: 9) {
                        Image(systemName: step.1 ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(step.1 ? .green : .secondary)
                            .font(.system(size: 12))
                        Text("\(idx + 1). \(step.0)")
                            .font(.system(size: 11.5))
                            .foregroundStyle(step.1 ? .secondary : .primary)
                        Spacer()
                    }
                }
            }
        }
    }
}
