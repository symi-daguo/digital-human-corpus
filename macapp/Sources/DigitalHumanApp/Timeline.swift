import SwiftUI

// MARK: - 日报时间轴（移植自手机 APK ReportPage.kt，解析规则与交互保持一致）
//
// 不重复造轮子的三层约定：
// 1. 服务端 /api/report/{date} 已返回 summary_cards（llm.parse_summary_cards
//    的产物）与 speakers 名单，这里只做消费，不再自己猜总结格式
// 2. 行解析正则与 APK 的 LINE_RE / SUMMARY_BLOCK_RE 逐字符一致
// 3. 展示顺序、换段时间标注规则（≥5 分钟）、本人高亮色与 APK 相同，
//    保证两端「看到的一样」

enum ReportTimeline {
    /// 本人标签：与 Mac 端 config.SELF_LABEL / APK SELF_LABEL 一致
    static let selfLabel = "本人"

    /// 时间轴卡标题与服务端卡片标题的映射（APK SUMMARY_TIMELINE_CARDS 同款）
    static let speakerOfCard: [String: String] = [
        "我的动态": selfLabel,
        "他人动态": "他人",
        "今日回顾": "",
    ]

    /// 逐字稿行：`[HH:MM] [说话人] 正文`（续行沿用上一行时间戳）
    private static let lineRe = try! NSRegularExpression(
        pattern: #"^\s*\[(\d{2}:\d{2})\]\s*(?:\[([^\]]*)\]\s*)?(.*)$"#
    )
    /// 总结卡正文行：「【HH:MM-HH:MM】要点」（Mac 端代码注入的时间头，非模型生成）
    private static let summaryBlockRe = try! NSRegularExpression(
        pattern: #"^【(\d{2}):(\d{2})-(\d{2}:\d{2})】\s*(.*)$"#
    )

    struct Row: Identifiable, Equatable {
        let idx: Int
        let minute: Int
        let speaker: String
        let text: String
        var id: Int { idx }
    }

    static func fmtMinute(_ m: Int) -> String {
        String(format: "%02d:%02d", m / 60, m % 60)
    }

    /// 把「每行带 [HH:MM] 时间戳」的逐字稿解析成时间线行（APK parseRows 同款）。
    /// 解析失败/没有时间戳的旧稿返回空列表，调用方回退纯文本，绝不吞内容。
    static func parseRows(_ text: String) -> [Row] {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return [] }
        var out: [Row] = []
        var lastMinute = -1
        var idx = 0
        for line in text.components(separatedBy: "\n") {
            let ns = line as NSString
            let range = NSRange(location: 0, length: ns.length)
            if let m = lineRe.firstMatch(in: line, range: range) {
                let hhmm = ns.substring(with: m.range(at: 1))
                lastMinute = (Int(hhmm.prefix(2)) ?? 0) * 60 + (Int(hhmm.suffix(2)) ?? 0)
                let speaker = m.range(at: 2).location != NSNotFound
                    ? ns.substring(with: m.range(at: 2)).trimmingCharacters(in: .whitespaces)
                    : ""
                let body = ns.substring(with: m.range(at: 3)).trimmingCharacters(in: .whitespaces)
                if !body.isEmpty {
                    out.append(Row(idx: idx, minute: lastMinute, speaker: speaker, text: body))
                    idx += 1
                }
            } else if !line.trimmingCharacters(in: .whitespaces).isEmpty, lastMinute >= 0 {
                out.append(Row(idx: idx, minute: lastMinute, speaker: "",
                               text: line.trimmingCharacters(in: .whitespaces)))
                idx += 1
            }
        }
        return out
    }

    /// 服务端总结卡的时间段要点 → 时间线行（APK parseSummaryRows 同款）。
    /// 解析不出时间行（旧版总结/格式变化）返回空列表，调用方回退卡片视图。
    static func parseSummaryRows(cards: [SummaryCard]) -> [Row] {
        var out: [Row] = []
        var matched = false
        for card in cards {
            guard let speaker = speakerOfCard[card.title] else { continue }
            matched = true
            for line in card.body.components(separatedBy: "\n") {
                let t = line.trimmingCharacters(in: .whitespaces)
                let ns = t as NSString
                let range = NSRange(location: 0, length: ns.length)
                guard let m = summaryBlockRe.firstMatch(in: t, range: range) else { continue }
                // 分组：1=HH 2=MM 3=HH:MM（结束，未用） 4=要点文本
                let hh = Int(ns.substring(with: m.range(at: 1))) ?? 0
                let mm = Int(ns.substring(with: m.range(at: 2))) ?? 0
                let text = ns.substring(with: m.range(at: 4)).trimmingCharacters(in: .whitespaces)
                    .replacingOccurrences(of: #"^\[[^\]]*\]\s*"#, with: "",
                                          options: .regularExpression)
                if !text.isEmpty {
                    out.append(Row(idx: out.count, minute: hh * 60 + mm,
                                   speaker: speaker, text: text))
                }
            }
        }
        guard matched else { return [] }
        // 与原始稿/干净稿同序：最新在上
        return out.sorted { $0.minute > $1.minute }
    }

    /// 说话人筛选选项：本人置顶，其余按发言数降序（APK speakerOptions 同款）
    static func speakerOptions(_ rows: [Row]) -> [(label: String, count: Int)] {
        var counts: [String: Int] = [:]
        for r in rows where !r.speaker.isEmpty { counts[r.speaker, default: 0] += 1 }
        return counts
            .sorted { a, b in
                if (a.key == selfLabel) != (b.key == selfLabel) { return a.key == selfLabel }
                return a.value > b.value
            }
            .map { ($0.key, $0.value) }
    }
}

// MARK: - 全天时间滑轨（APK DayTimeline 移植）

/// 每个语音段画成一根刻度，密度即「什么时候在说话」。点击/拖动定位，
/// 松手吸附到最近的有内容刻度（空白区段不可停留），本人发言 Teal 高亮。
struct DayTimelineCanvas: View {
    let rows: [ReportTimeline.Row]
    let focusMinute: Int?
    let onSeek: (Int) -> Void

    @State private var dragMinute: Int?

    var body: some View {
        let minutes = rows.map(\.minute)
        let minMin = minutes.min() ?? 0
        let maxMin = max(minutes.max() ?? 1, minMin + 1)
        let snapMinutes = Array(Set(minutes)).sorted()
        let shown = dragMinute ?? focusMinute ?? minMin
        return VStack(spacing: 6) {
            HStack {
                Text("时间线 · 点击或拖动定位")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textTertiary)
                Spacer()
                Text(ReportTimeline.fmtMinute(shown))
                    .font(.system(size: 12, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(dragMinute != nil ? Theme.amber : Theme.textSecondary)
            }
            GeometryReader { geo in
                Canvas { ctx, size in
                    let w = size.width, h = size.height
                    let railY = h - 10
                    // 基线
                    var rail = Path()
                    rail.move(to: CGPoint(x: 0, y: railY))
                    rail.addLine(to: CGPoint(x: w, y: railY))
                    ctx.stroke(rail, with: .color(Theme.stroke), lineWidth: 2)
                    // 每个语音段一根刻度；本人 Teal，其余 Indigo
                    for r in rows {
                        let x = CGFloat(r.minute - minMin) / CGFloat(maxMin - minMin) * w
                        var tick = Path()
                        tick.move(to: CGPoint(x: x, y: 6))
                        tick.addLine(to: CGPoint(x: x, y: railY))
                        let color = r.speaker == ReportTimeline.selfLabel ? Theme.teal : Theme.indigo.opacity(0.55)
                        ctx.stroke(tick, with: .color(color), lineWidth: 2)
                    }
                    // 当前位置指示线 + 节点
                    let fx = CGFloat(shown - minMin) / CGFloat(maxMin - minMin) * w
                    var focus = Path()
                    focus.move(to: CGPoint(x: fx, y: 0))
                    focus.addLine(to: CGPoint(x: fx, y: h))
                    ctx.stroke(focus, with: .color(Theme.amber), lineWidth: 2)
                    ctx.fill(Path(ellipseIn: CGRect(x: fx - 4, y: railY - 4, width: 8, height: 8)),
                             with: .color(Theme.amber))
                }
                .contentShape(Rectangle())
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { g in
                            let m = snap(minuteAt(g.location.x, geo.size.width, minMin, maxMin), snapMinutes)
                            dragMinute = m
                            onSeek(m)
                        }
                        .onEnded { _ in dragMinute = nil }
                )
            }
            .frame(height: 44)
        }
    }

    private func minuteAt(_ x: CGFloat, _ width: CGFloat, _ minMin: Int, _ maxMin: Int) -> Int {
        let ratio = min(max(x / width, 0), 1)
        return minMin + Int((ratio * CGFloat(maxMin - minMin)).rounded())
    }

    private func snap(_ minute: Int, _ snapMinutes: [Int]) -> Int {
        snapMinutes.min(by: { abs($0 - minute) < abs($1 - minute) }) ?? minute
    }
}

// MARK: - 时间线正文（APK TimelineBody 移植）

/// 行序最新在最上；时间只在换时段（与上一条间隔 ≥5 分钟）处标注；
/// 左侧时间槽 + 竖线节点 + 说话人标签。拖动滑轨 → 滚到第一条不晚于目标时刻的行。
struct TimelineListView<Footer: View>: View {
    let rows: [ReportTimeline.Row]
    let focusMinute: Int?
    @ViewBuilder var footer: Footer

    var body: some View {
        let visible = rows.sorted { $0.minute > $1.minute }
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if visible.isEmpty {
                        Text("该筛选下暂无内容，点上方「全部」恢复")
                            .font(.system(size: 11.5))
                            .foregroundStyle(Theme.textTertiary)
                            .padding(16)
                    } else {
                        ForEach(visible) { row in
                            rowView(row, prevMinute: previousMinute(of: row, in: visible))
                        }
                    }
                    if !(visible.isEmpty && footerIsEmpty) { footer }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
            }
            .onChange(of: focusMinute) { _, target in
                guard let target, !visible.isEmpty else { return }
                let idx = visible.firstIndex(where: { $0.minute <= target }) ?? 0
                withAnimation { proxy.scrollTo(visible[idx].id, anchor: .top) }
            }
        }
    }

    private var footerIsEmpty: Bool { footer is EmptyView }

    private func previousMinute(of row: ReportTimeline.Row, in visible: [ReportTimeline.Row]) -> Int? {
        guard let i = visible.firstIndex(where: { $0.id == row.id }), i > 0 else { return nil }
        return visible[i - 1].minute
    }

    @ViewBuilder
    private func rowView(_ row: ReportTimeline.Row, prevMinute: Int?) -> some View {
        // 降序列表中与上一条（更晚）间隔 ≥5 分钟才算换段
        let showTime = prevMinute == nil || prevMinute! - row.minute >= 5
        let isSelf = row.speaker == ReportTimeline.selfLabel
        HStack(alignment: .top, spacing: 10) {
            Text(showTime ? ReportTimeline.fmtMinute(row.minute) : "")
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(isSelf ? Theme.teal : Theme.textTertiary)
                .frame(width: 52, alignment: .trailing)
            ZStack {
                Rectangle()
                    .fill(Theme.stroke)
                    .frame(width: 2)
                Circle()
                    .fill(isSelf ? Theme.teal : Theme.indigo)
                    .frame(width: showTime ? 8 : 5, height: showTime ? 8 : 5)
            }
            .frame(width: 12, height: showTime ? 22 : 14, alignment: .top)
            VStack(alignment: .leading, spacing: 2) {
                if !row.speaker.isEmpty {
                    Text(row.speaker)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(isSelf ? Theme.teal : Theme.textSecondary)
                }
                Text(row.text)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            .padding(.vertical, 2)
        }
        .padding(.vertical, 3)
    }
}

// MARK: - 说话人筛选（APK SpeakerFilterRow 移植）

struct SpeakerChipsRow: View {
    let options: [(label: String, count: Int)]
    let total: Int
    @Binding var selected: String?

    var body: some View {
        if !options.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    chip("全部 \(total)", isSelected: selected == nil, accent: Theme.indigo) {
                        selected = nil
                    }
                    ForEach(options, id: \.label) { opt in
                        chip("\(opt.label) \(opt.count)", isSelected: selected == opt.label,
                             accent: opt.label == ReportTimeline.selfLabel ? Theme.teal : Theme.indigo) {
                            selected = selected == opt.label ? nil : opt.label
                        }
                    }
                }
            }
        }
    }

    private func chip(_ label: String, isSelected: Bool, accent: Color, onClick: @escaping () -> Void) -> some View {
        Text(label)
            .font(.system(size: 11.5, weight: isSelected ? .medium : .regular))
            .foregroundStyle(isSelected ? accent : Theme.textSecondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(
                Capsule().fill(isSelected ? accent.opacity(0.16) : Theme.surface1)
            )
            .overlay(Capsule().strokeBorder(isSelected ? accent.opacity(0.4) : Theme.stroke))
            .contentShape(Capsule())
            .onTapGesture(perform: onClick)
    }
}

// MARK: - 总结其余栏目卡（关键原话/待办/金句/状态/重要时刻）

struct SummaryCardsListView: View {
    let cards: [SummaryCard]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(cards.enumerated()), id: \.offset) { _, card in
                VStack(alignment: .leading, spacing: 6) {
                    Text(card.title)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Theme.teal)
                    Text(card.body)
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Theme.radiusM).fill(Theme.surface1))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusM).strokeBorder(Theme.stroke))
            }
        }
    }
}
