import SwiftUI

enum LogFile: String, CaseIterable, Identifiable {
    case api = "api.log"
    case llm = "llm.log"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .api: "服务日志"
        case .llm: "文字模型日志"
        }
    }
}

/// 日志查看。
///
/// 只做尾部读取（见 `LogStore.tail`），不加载整个文件 —— api.log 的单文件上限是
/// 20 MB，全量读进内存既慢又会让界面卡住。
///
/// 这里展示的是**真实落盘**的日志：Python 侧的 `RotatingFileHandler` 写到
/// `~/Library/Application Support/DigitalHuman/logs/`，与 Swift 侧 `logsRoot`
/// 必须是同一个路径，否则这里会一直空。
struct LogView: View {
    @EnvironmentObject var service: ServiceManager

    @State private var file: LogFile = .api
    @State private var text = ""
    @State private var autoRefresh = false
    @State private var exists = true

    private var url: URL { service.logsRoot.appendingPathComponent(file.rawValue) }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
        }
        .task(id: "\(file.rawValue)-\(autoRefresh)") {
            reload()
            guard autoRefresh else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                guard !Task.isCancelled else { break }
                reload()
            }
        }
    }

    /// 同 ReportView：一行放得下横排，放不下自动竖排。
    /// 「自动刷新」开关与按钮是固定宽度的，窄窗口下挤在一起会互相截断标题。
    private var header: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                filePicker
                Spacer(minLength: 12)
                refreshToggle
                refreshButton
            }
            VStack(alignment: .leading, spacing: 8) {
                filePicker
                HStack(spacing: 12) {
                    refreshToggle
                    Spacer(minLength: 0)
                    refreshButton
                }
            }
        }
        // 顶部与其它页共用同一个基线（Theme.pageTop），见 ReportView 里的同款说明
        .padding(.horizontal, Theme.pageH)
        .padding(.top, Theme.pageTop)
        .padding(.bottom, 10)
    }

    private var filePicker: some View {
        Picker("", selection: $file) {
            ForEach(LogFile.allCases) { f in
                Text("\(f.title)  \(f.rawValue)").tag(f)
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(minWidth: 150, maxWidth: 280)
    }

    private var refreshToggle: some View {
        Toggle("自动刷新", isOn: $autoRefresh)
            .toggleStyle(.switch)
            .controlSize(.small)
            .font(.system(size: 11))
            .fixedSize()
    }

    private var refreshButton: some View {
        Button {
            reload()
        } label: {
            Label("刷新", systemImage: "arrow.clockwise")
                .font(.system(size: 11))
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .fixedSize()
    }

    @ViewBuilder
    private var content: some View {
        if !exists || text.isEmpty {
            ContentUnavailableView {
                Label("暂无日志", systemImage: "doc.text.magnifyingglass")
            } description: {
                Text("尚未生成 \(file.rawValue)。启动服务后再回来看。")
                    .font(.system(size: 11.5))
            }
        } else {
            AppTextView(text: text, fontSize: 11.5, monospaced: true, stickToBottom: autoRefresh)
                .safeAreaInset(edge: .bottom) { footer }
        }
    }

    private var footer: some View {
        HStack(spacing: 12) {
            Text("\(LogStore.sizeDescription(url)) · 仅显示末尾约 190 KB")
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                Self.copy(text)
            } label: {
                Label("复制", systemImage: "doc.on.doc")
                    .font(.system(size: 11))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            Button {
                NSWorkspace.shared.activateFileViewerSelecting([url])
            } label: {
                Label("在访达中显示", systemImage: "folder")
                    .font(.system(size: 11))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(Material.bar)
    }

    private func reload() {
        exists = FileManager.default.fileExists(atPath: url.path)
        guard exists else { text = ""; return }
        text = LogStore.tail(url)
    }

    private static func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}
