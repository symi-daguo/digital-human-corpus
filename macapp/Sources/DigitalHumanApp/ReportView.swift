import SwiftUI

enum ReportVersion: String, CaseIterable, Identifiable {
    case raw     = "原始稿"
    case clean   = "干净稿"
    case summary = "总结"

    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .raw:     "waveform"
        case .clean:   "text.quote"
        case .summary: "sparkles.rectangle.stack"
        }
    }
}

/// 日报预览：选日期 → 切三版 → 读全文。
///
/// 三版同源于 `store.get_report`，与手机端、网页工作台看到的是同一份内容，
/// 这里不做任何再加工，避免出现「App 上看到的和实际产出的不一样」。
struct ReportView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient

    @State private var date: String = ""
    @State private var version: ReportVersion = .clean
    @State private var report: DayReport?
    @State private var loadError: String?
    @State private var isLoading = false
    @State private var showRegenConfirm = false
    @State private var regenerating = false
    @State private var regenMessage: String?
    @State private var speakerFilter: String?
    @State private var focusMinute: Int?

    private static let dayKey: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private var days: [ConsoleState.Day] { api.state?.days ?? [] }

    private var content: String {
        guard let r = report else { return "" }
        switch version {
        case .raw:     return r.raw
        case .clean:   return r.clean
        case .summary: return r.summary
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            body_
        }
        .task {
            if date.isEmpty { date = api.state?.latestReadyDay?.date ?? Self.dayKey.string(from: Date()) }
            await load()
        }
        .onChange(of: date) { _, _ in
            speakerFilter = nil
            focusMinute = nil
            Task { await load() }
        }
        // 服务从「未运行」恢复过来时 days 才有值，此时要把默认日期补上
        .onChange(of: api.state?.days.count) { _, _ in
            if date.isEmpty { date = api.state?.latestReadyDay?.date ?? Self.dayKey.string(from: Date()) }
        }
        .confirmationDialog("重新生成 \(date) 的终稿？", isPresented: $showRegenConfirm) {
            Button("重新生成（全段重转 + 日终清理）", role: .destructive) { regen() }
            Button("取消", role: .cancel) {}
        } message: {
            Text("日终终稿会对全天人声段重新转写并生成三版日报，"
                 + "过程中会清理已归档的音频文件。当天音频仍在手机端时，"
                 + "请确认已全部上传完成。")
        }
    }

    // MARK: - 头部

    /// `ViewThatFits` 是这里唯一正确的写法：
    /// 一行放得下就横排，放不下自动换成两行，不需要自己去量宽度、也不需要
    /// 为「某个宽度下到底走哪条分支」维护断点。写死宽度（原来的 190 / 220）
    /// 在窄窗口下会把右侧的版本切换挤出可视区。
    private var header: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                datePicker
                statusBadge
                regenButton
                Spacer(minLength: 12)
                versionPicker
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    datePicker
                    statusBadge
                    Spacer(minLength: 0)
                    regenButton
                }
                versionPicker
            }
        }
        // 顶部与其它页共用同一个基线（Theme.pageTop）：这一页是「固定头 + 滚动正文」，
        // 头部自带内边距，所以不再用页面容器的三向 padding，只对齐顶部这一个值。
        .padding(.horizontal, Theme.pageH)
        .padding(.top, Theme.pageTop)
        .padding(.bottom, 10)
    }

    /// 手动触发日终终稿：增量轮只补新段，漏掉的（比如中途停过服务）
    /// 靠这里补全。带确认——它会全段重转并清理音频。
    private var regenButton: some View {
        Button {
            showRegenConfirm = true
        } label: {
            if regenerating {
                ProgressView().controlSize(.small)
            } else {
                Label("重新生成", systemImage: "arrow.triangle.2.circlepath")
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .disabled(service.status != .running || date.isEmpty || regenerating)
        .help("重新跑日终终稿：全段重转 + 去口气 + 总结")
    }

    private var datePicker: some View {
        Picker("", selection: $date) {
            if days.isEmpty {
                Text("暂无数据").tag(date)
            } else {
                ForEach(days.reversed()) { d in
                    Text(Self.title(for: d)).tag(d.date)
                }
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(minWidth: 150, maxWidth: 280)
        .disabled(days.isEmpty)
    }

    @ViewBuilder
    private var statusBadge: some View {
        if let d = days.first(where: { $0.date == date }) {
            HStack(spacing: 5) {
                Image(systemName: d.ready ? "checkmark.circle.fill" : "clock")
                    .font(.system(size: 10))
                    .foregroundStyle(d.ready ? .green : .orange)
                Text(d.ready ? "已生成" : "待日终处理")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }
            .fixedSize()
        }
    }

    private var versionPicker: some View {
        Picker("", selection: $version) {
            ForEach(ReportVersion.allCases) { v in
                Text(v.rawValue).tag(v)
            }
        }
        .labelsHidden()
        .pickerStyle(.segmented)
        .frame(maxWidth: 260)
    }

    // MARK: - 内容

    @ViewBuilder
    private var body_: some View {
        if service.status != .running {
            placeholder(
                title: "服务未运行",
                symbol: "power",
                description: "启动服务后才能读取日报。"
            )
        } else if days.isEmpty {
            placeholder(
                title: "暂无日报",
                symbol: "doc.text.magnifyingglass",
                description: "还没有采集到人声片段。手机端配对成功并上传录音后，"
                    + "增量轮会即时转写，日终轮会生成完整三版。"
            )
        } else if let err = loadError {
            placeholder(title: "读取失败", symbol: "exclamationmark.triangle", description: err)
        } else if content.isEmpty {
            placeholder(
                title: "\(version.rawValue)为空",
                symbol: version.symbol,
                description: emptyHint
            )
        } else {
            reportContent
                .overlay(alignment: .topTrailing) {
                    if isLoading { ProgressView().controlSize(.small).padding(10) }
                }
                .safeAreaInset(edge: .bottom) { footer }
        }
    }

    /// 正文区：三版都按时间轴展示（与手机 APK 同一套解析与交互）；
    /// 解析不出时间行（旧稿/格式变化）时回退纯文本，内容不丢。
    @ViewBuilder
    private var reportContent: some View {
        switch version {
        case .raw, .clean:
            let rows = ReportTimeline.parseRows(content)
            if rows.isEmpty {
                AppTextView(text: content, monospaced: true)
            } else {
                timelineSection(rows: rows, restCards: [])
            }
        case .summary:
            let cards = report?.cards ?? []
            let rows = ReportTimeline.parseSummaryRows(cards: cards)
            // 时间轴卡（我的动态/他人动态/今日回顾）已渲染成行，其余栏目卡殿后
            let restCards = cards.filter { ReportTimeline.speakerOfCard[$0.title] == nil }
            if rows.isEmpty {
                if restCards.isEmpty {
                    AppTextView(text: content, monospaced: false)
                } else {
                    ScrollView {
                        SummaryCardsListView(cards: restCards)
                            .padding(16)
                    }
                }
            } else {
                timelineSection(rows: rows, restCards: restCards)
            }
        }
    }

    private func timelineSection(rows: [ReportTimeline.Row], restCards: [SummaryCard]) -> some View {
        let options = ReportTimeline.speakerOptions(rows)
        let visible = speakerFilter.flatMap { f in rows.filter { $0.speaker == f } } ?? rows
        return VStack(alignment: .leading, spacing: 12) {
            SpeakerChipsRow(options: options, total: rows.count, selected: $speakerFilter)
                .padding(.horizontal, 16)
            DayTimelineCanvas(rows: visible, focusMinute: focusMinute) { focusMinute = $0 }
                .padding(.horizontal, 16)
            TimelineListView(rows: visible, focusMinute: focusMinute) {
                if !restCards.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        SectionHeader(title: "更多栏目", symbol: "rectangle.stack")
                        SummaryCardsListView(cards: restCards)
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                }
            }
        }
    }

    private var emptyHint: String {
        switch version {
        case .raw:
            "该日期还没有原始转写。"
        case .clean:
            "干净稿由文字模型生成。若文字模型未就绪，这一版会是空的。"
        case .summary:
            "总结在日终轮生成。若当天没有足量人声片段，或文字模型未就绪，这一版会是空的。"
        }
    }

    private var footer: some View {
        HStack(spacing: 12) {
            if let m = regenMessage {
                Image(systemName: "info.circle")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                Text(m)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("\(content.count) 字")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            Spacer()
            Button {
                Self.copy(content)
            } label: {
                Label("复制全文", systemImage: "doc.on.doc")
                    .font(.system(size: 11))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(Material.bar)
    }

    // MARK: - 重算

    /// 触发日终终稿。管线在服务端后台跑，这里拿到「已启动」就回来：
    /// 重转一整天要几分钟到几十分钟，同步等会把请求挂死。
    private func regen() {
        regenerating = true
        regenMessage = nil
        Task {
            defer { regenerating = false }
            do {
                let r = try await api.triggerDone(date: date, final: true,
                                                   token: service.apiToken)
                regenMessage = r.pipelineStarted
                    ? "已开始重新生成，完成后此处会自动更新"
                    : "该日已有任务在处理中，请稍后刷新查看"
                // 起跑后轮询几次：增量阶段段数会变，日报也会翻新
                for _ in 0..<10 {
                    try? await Task.sleep(for: .seconds(15))
                    await api.refresh(token: service.apiToken)
                    await load()
                }
            } catch {
                regenMessage = "触发失败：\(error.localizedDescription)"
            }
        }
    }

    private func placeholder(title: String, symbol: String, description: String) -> some View {
        ContentUnavailableView {
            Label(title, systemImage: symbol)
        } description: {
            Text(description)
                .font(.system(size: 11.5))
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - 逻辑

    private func load() async {
        guard service.status == .running, !date.isEmpty else {
            report = nil
            loadError = nil
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            report = try await api.report(date: date, token: service.apiToken)
            loadError = nil
        } catch {
            report = nil
            loadError = error.localizedDescription
        }
    }

    private static func title(for d: ConsoleState.Day) -> String {
        let today = dayKey.string(from: Date())
        let suffix = d.date == today ? "（今天）" : ""
        return "\(d.date)  ·  \(d.speechSegments) 段\(suffix)"
    }

    private static func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}
