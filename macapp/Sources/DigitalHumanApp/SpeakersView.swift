import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// 角色页：具名声纹的查看、注册与管理。
///
/// 注册有两条路：手机端「设置 → 声纹角色」（现场录 30 秒）与本页的
/// **上传 WAV**（电脑上已有现成录音时更快）。两条路走的是服务端同一个
/// `POST /api/pair`，落库都按**当前空间**（工具栏可切），与设备档案无关。
/// 本页负责「看谁注册了、补注册、删掉不要的」——删除只影响后续识别，
/// 不历史追溯改标签，这点要向用户讲清楚。
struct SpeakersView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient

    @State private var deleting: String?
    @State private var pendingDelete: ConsoleState.Speaker?
    @State private var message: String?
    @State private var isError = false

    // 上传注册
    @State private var newName = ""
    @State private var pickedFile: URL?
    @State private var picking = false
    @State private var uploading = false

    private var speakers: [ConsoleState.Speaker] { api.state?.speakers ?? [] }

    private static var dateTime: DateFormatter {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return f
    }

    var body: some View {
        Group {
            if service.status != .running {
                ContentUnavailableView {
                    Label("服务未运行", systemImage: "power")
                } description: {
                    Text("角色列表由服务端读取，需要先启动服务。")
                        .font(.system(size: 11.5))
                } actions: {
                    Button("启动服务") { service.start() }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                content
            }
        }
        .navigationSubtitle("具名声纹优先于匿名聚类匹配")
        .confirmationDialog("删除角色「\(pendingDelete?.name ?? "")」？",
                            isPresented: Binding(
                                get: { pendingDelete != nil },
                                set: { if !$0 { pendingDelete = nil } })) {
            Button("删除（仅影响后续识别）", role: .destructive) {
                if let s = pendingDelete { delete(s) }
                pendingDelete = nil
            }
            Button("取消", role: .cancel) { pendingDelete = nil }
        } message: {
            Text("已生成的日报不会追溯改动。之后这个人说的话会回到匿名聚类。")
        }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.space4) {
                if let m = message { banner(m) }
                // 角色列表是主内容，独占一行（角色多时会很长）
                listCard
                // 上传区与说明卡宽屏并排（左操作、右依据），窄屏堆叠
                ResponsiveGrid(minColumnWidth: 460, maxColumns: 2) {
                    uploadCard
                    howtoCard
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.pageH)
            .padding(.top, Theme.pageTop)
            .padding(.bottom, Theme.pageBottom)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: - 列表

    private var listCard: some View {
        GlassCard(title: "已注册角色", symbol: "person.2") {
            VStack(alignment: .leading, spacing: 2) {
                if speakers.isEmpty {
                    HStack(spacing: Theme.space2) {
                        Image(systemName: "person.crop.circle.badge.questionmark")
                            .foregroundStyle(.secondary)
                            .font(.system(size: 22))
                        Text("这个空间还没注册具名角色。用下面的上传，或在手机端「设置 → 声纹角色」录 30 秒。")
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.vertical, 10)
                } else {
                    ForEach(speakers) { s in
                        speakerRow(s)
                        if s.id != speakers.last?.id { Divider() }
                    }
                }
            }
        }
    }

    private func speakerRow(_ s: ConsoleState.Speaker) -> some View {
        HStack(spacing: 10) {
            ZStack {
                Circle()
                    .fill(Color.accentColor.opacity(0.12))
                    .frame(width: 30, height: 30)
                Text(String(s.name.prefix(1)))
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Color.accentColor)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(s.name)
                    .font(.system(size: 12.5, weight: .medium))
                Text("注册于 \(Self.dateTime.string(from: Date(timeIntervalSince1970: s.createdAt)))")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            if deleting == s.name {
                ProgressView().controlSize(.small)
            } else {
                Button {
                    pendingDelete = s
                } label: {
                    Label("删除", systemImage: "trash")
                        .font(.system(size: 11))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
    }

    // MARK: - 上传注册

    /// 上传卡：说明「录多长、念什么」+ 一行搞定「选文件 + 起名 + 提交」。
    ///
    /// 不做成向导：这里只做一件事，多一步弹窗就多一次打断。
    /// 角色名为空 = 服务端按「本人」注册（与手机端配对等价），文案里写明。
    ///
    /// v1.5.2 补：原先只说「建议 ≥30 秒」，没说**念什么**。声纹锚点要求
    /// 样本可比，必须照读同一段固定文本（文本由服务端下发，见
    /// `ConsoleState.Enroll`），所以这里把正文原样摆出来，并给一个复制按钮
    /// —— 大多数场景是「把这段话发给要注册的同事」或「贴到手机上照着念」。
    private var uploadCard: some View {
        GlassCard(title: "上传录音创建角色", symbol: "waveform.badge.plus") {
            VStack(alignment: .leading, spacing: Theme.space3) {
                // 用 Text 拼接做局部加粗，不写 `**…**`：SwiftUI 的 Markdown
                // 只在**字面量**里解析，字符串拼接之后星号会原样显示出来。
                (Text("注册一次需要该角色")
                 + Text("连续朗读 30 秒").bold()
                 + Text("，念下面这段固定文字。内容固定是为了让声纹稳定 —— "
                        + "同一段文字、同一个人的声音特征才可比。录好的 WAV 传到"
                        + "这里就行，")
                 + Text("不必再拿手机录一遍").bold()
                 + Text("。"))
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                if let e = api.state?.enroll {
                    scriptBox(e)
                }

                HStack(spacing: Theme.space2) {
                    TextField("角色名（1-16 字，如 张三）", text: $newName)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 220)

                    Button {
                        picking = true
                    } label: {
                        Label(pickedFile == nil ? "选择 WAV…" : "重新选择…",
                              systemImage: "doc.badge.plus")
                    }

                    if let url = pickedFile {
                        Text(url.lastPathComponent)
                            .font(.system(size: 11).monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    Spacer(minLength: Theme.space2)

                    if uploading {
                        HStack(spacing: Theme.space2) {
                            ProgressView().controlSize(.small)
                            Text("正在提取声纹…")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        Button("上传并注册") { upload() }
                            .buttonStyle(.borderedProminent)
                            .disabled(pickedFile == nil)
                    }
                }

                (Text("录音要求：单人、无背景音乐、无旁人插话；手机录音机或电脑录都行，"
                        + "采样率不限（服务端自动重采样）。")
                 + Text("太短会提不出稳定声纹").bold()
                 + Text("，接口会直接拒绝。"))
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .fileImporter(isPresented: $picking,
                      allowedContentTypes: [.wav],
                      allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls):
                pickedFile = urls.first
            case .failure(let err):
                isError = true
                message = "选择文件失败：\(err.localizedDescription)"
            }
        }
    }

    // MARK: - 说明

    /// 朗读正文：从服务端下发的文本原样渲染。
    ///
    /// 为什么不做成「可编辑的输入框」：这段文本是声纹可比性的前提，
    /// 用户改了内容（哪怕只是改个日期）都会让新样本与已有锚点错位。
    /// 允许选中复制，但不给编辑入口。
    private func scriptBox(_ e: ConsoleState.Enroll) -> some View {
        VStack(alignment: .leading, spacing: Theme.space2) {
            HStack(spacing: 6) {
                Image(systemName: "text.quote")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Theme.teal)
                Text("照读这段（约 \(e.seconds) 秒）")
                    .font(.system(size: 11, weight: .semibold))
                Spacer(minLength: Theme.space2)
                Button {
                    copy(e.script)
                } label: {
                    Label("复制文本", systemImage: "doc.on.doc")
                        .font(.system(size: 10.5))
                }
                .buttonStyle(.borderless)
                .help("复制朗读文本：贴到手机上照着念，或发给要注册的同事")
            }
            Text(e.script)
                .font(.system(size: 11.5))
                .lineSpacing(3)
                .foregroundStyle(Theme.textPrimary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Theme.space3)
        .padding(.vertical, Theme.space3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusM).fill(Theme.surface1)
        )
        .overlay(alignment: .leading) {
            // 左侧 3pt 强调竖线：提示这是一段「要照读的引用」，
            // 而不是普通说明文字（卡片里已经有大量说明文字了）
            RoundedRectangle(cornerRadius: 1.5)
                .fill(Theme.teal.opacity(0.85))
                .frame(width: 3)
                .padding(.vertical, 8)
        }
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusM)
                .strokeBorder(Theme.stroke, lineWidth: 1)
        )
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        isError = false
        message = "已复制朗读文本"
    }

    private var howtoCard: some View {
        GlassCard(title: "关于声纹识别", symbol: "info.circle") {
            VStack(alignment: .leading, spacing: 7) {
                bullet("两级匹配：具名锚点优先（阈值 0.6），匿名聚类兜底")
                bullet("注册方式：手机端「设置 → 声纹角色」照读同一段文字 30 秒，"
                       + "或在本页上传那 30 秒的 WAV（走同一个接口）")
                bullet("注册落进「当前空间」：切了空间再传，就注册到那个空间，两边互不干扰")
                bullet("同一个人换名重注册会覆盖旧声纹，保留累计段数")
                bullet("删除具名角色不影响已生成的历史日报")
            }
        }
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.space2) {
            Text("·").font(.system(size: 12, weight: .bold)).foregroundStyle(.secondary)
            Text(text)
                .font(.system(size: 11.5))
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - 交互

    private func upload() {
        guard let url = pickedFile, !uploading else { return }
        let name = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        uploading = true
        isError = false
        message = nil
        Task {
            defer { uploading = false }
            // fileImporter 给的是安全作用域 URL，不 start 读出来是空的
            let scoped = url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            do {
                let r = try await api.registerSpeaker(name: name, fileURL: url,
                                                      token: service.apiToken)
                await api.refresh(token: service.apiToken)
                message = "已把「\(r.name)」注册到当前空间"
                newName = ""
                pickedFile = nil
            } catch let APIError.http(code) where code == 400 {
                isError = true
                message = "注册失败：这段音频没提出有效声纹。换一段 ≥30 秒的清晰人声再试。"
            } catch {
                isError = true
                message = "注册失败：\(error.localizedDescription)"
            }
        }
    }

    private func delete(_ s: ConsoleState.Speaker) {
        deleting = s.name
        isError = false
        message = nil
        Task {
            defer { deleting = nil }
            do {
                try await api.deleteSpeaker(s.name, token: service.apiToken)
                await api.refresh(token: service.apiToken)
                message = "已删除「\(s.name)」"
            } catch {
                isError = true
                message = "删除失败：\(error.localizedDescription)"
            }
        }
    }

    private func banner(_ text: String) -> some View {
        HStack(spacing: Theme.space2) {
            Image(systemName: isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                .foregroundStyle(isError ? Color.red : Color.green)
                .font(.system(size: 12))
            Text(text)
                .font(.system(size: 11.5))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button {
                message = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10))
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, Theme.space3)
        .padding(.vertical, Theme.space2)
        .background(RoundedRectangle(cornerRadius: Theme.radiusS)
            .fill((isError ? Color.red : Color.green).opacity(0.08)))
    }
}
