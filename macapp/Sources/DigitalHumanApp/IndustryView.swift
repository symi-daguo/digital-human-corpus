import SwiftUI

/// 行业词表页：与网页工作台的「行业词表」同一份数据与同一套接口。
///
/// 三块内容：
/// 1. 行业列表——点选即切换（服务端会清识别器缓存，热词即时重建）；
/// 2. 当前行业的同音错字映射表——让用户知道「哪些错字会被自动纠正」；
/// 3. 词表文件的维护指引——加词加映射改 txt 文件即可，不用改代码。
struct IndustryView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient

    @State private var switching: String?
    @State private var message: String?
    @State private var isError = false
    @State private var editing: EditingIndustry?
    @State private var editorText: String = ""
    @State private var isSavingTerms = false
    @State private var termsError: String?

    private var industry: ConsoleState.IndustryInfo? { api.state?.industry }

    /// sheet(item:) 需要 Identifiable，String 不满足，包一层。
    private struct EditingIndustry: Identifiable {
        let key: String
        let label: String
        var id: String { key }
    }

    var body: some View {
        Group {
            if service.status != .running {
                ContentUnavailableView {
                    Label("服务未运行", systemImage: "power")
                } description: {
                    Text("行业词表由服务端读取，需要先启动服务。")
                        .font(.system(size: 11.5))
                } actions: {
                    Button("启动服务") { service.start() }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if let m = message { banner(m) }
                        // 行业列表是主内容，独占一行（条目多时会很长）
                        listSection
                        // 两块纯说明卡片在宽屏并排、窄屏堆叠：一路竖排到底的话，
                        // 1800pt 窗口下每张卡都被拉到 1500pt 宽、一行字读起来很累
                        ResponsiveGrid(minColumnWidth: 460, maxColumns: 2) {
                            if let ind = industry { fixesSection(ind) }
                            guideSection
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, Theme.pageH)
                    .padding(.top, Theme.pageTop)
                    .padding(.bottom, Theme.pageBottom)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .navigationSubtitle("切换后热词与错字映射即时生效")
        .sheet(item: $editing) { target in
            IndustryEditorSheet(
                title: target.label,
                initialText: editorText,
                isSaving: isSavingTerms,
                error: termsError,
                onSave: { text in saveTerms(target.key, text: text) },
                onDismiss: { editing = nil }
            )
            .frame(minWidth: 560, minHeight: 520)
        }
    }

    // MARK: - 行业列表

    private var listSection: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 4) {
                if pinnedByEnv {
                    HStack(spacing: 8) {
                        Image(systemName: "lock.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                        Text("行业已被 env.local 里的 DH_INDUSTRY 锁定，无法在此切换。删除该行后即可。")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.bottom, 6)
                }
                ForEach(industry?.available ?? []) { item in
                    row(item)
                }
            }
        }
    }

    private var pinnedByEnv: Bool { api.state?.industry.pinnedByEnv ?? false }

    private func row(_ item: ConsoleState.IndustryInfo.Item) -> some View {
        let current = item.key == industry?.current
        return HStack(spacing: 10) {
            Image(systemName: current ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(current ? Color.accentColor : Color.secondary)
                .font(.system(size: 13))
            VStack(alignment: .leading, spacing: 1) {
                Text(item.label)
                    .font(.system(size: 12.5, weight: current ? .semibold : .regular))
                Text("\(item.terms) 个术语 · \(item.key)")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            if switching == item.key {
                ProgressView().controlSize(.small)
            }
            // 词表内容编辑：切换解决「用哪套词」，编辑解决「词对不对」——
            // 错字映射只能从真实转写里积累，必须能在 App 里改，不能只靠改文件
            Button {
                openEditor(item)
            } label: {
                Label("编辑", systemImage: "square.and.pencil")
                    .font(.system(size: 11))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            if !current && !pinnedByEnv {
                Button("切换") { switchTo(item) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
        }
        .padding(.vertical, 5)
        .padding(.horizontal, 6)
        .background(
            // 选中项给一层极淡的系统色底，而不是自定义配色
            RoundedRectangle(cornerRadius: 6)
                .fill(current ? Color.accentColor.opacity(0.08) : Color.clear)
        )
        .contentShape(Rectangle())
        .onTapGesture { if !current && !pinnedByEnv { switchTo(item) } }
    }

    // MARK: - 错字映射

    private func fixesSection(_ ind: ConsoleState.IndustryInfo) -> some View {
        GlassCard(title: "同音错字映射（确定性替换）", symbol: "arrow.left.arrow.right") {
            VStack(alignment: .leading, spacing: 8) {
                Text("转写入库前，下方左侧的错写会被逐字替换成右侧的正写。"
                     + "映射只收真实转写里见过的错字，误改比不改更糟。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if ind.fixes.isEmpty {
                    Text("（当前行业没有错字映射，通用行业不做替换）")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 4)
                } else {
                    // 双列表格用 Grid 而不是 Table：Table 的类型推断在
                    // key-path 上过不去，而这里也不需要排序与选择能力
                    Grid(alignment: .leadingFirstTextBaseline,
                         horizontalSpacing: 16, verticalSpacing: 5) {
                        GridRow {
                            Text("错写")
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(.secondary)
                            Text("正写")
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(.secondary)
                        }
                        ForEach(Array(ind.fixes.keys.sorted()), id: \.self) { k in
                            GridRow {
                                Text(k)
                                    .font(.system(size: 11.5).monospaced())
                                Text(ind.fixes[k] ?? "")
                                    .font(.system(size: 11.5).monospaced())
                            }
                        }
                    }
                    .padding(.top, 2)
                }
            }
        }
    }

    // MARK: - 维护指引

    private var guideSection: some View {
        GlassCard(title: "维护词表", symbol: "wrench.and.screwdriver") {
            VStack(alignment: .leading, spacing: 7) {
                Text("行业词表是服务端的 txt 文件，一行一词，不用改代码：")
                    .font(.system(size: 11.5))
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 5) {
                    GridRow {
                        Text("新增行业")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                        Text("在 industries/ 目录丢一个 txt，首行写 # label: 中文名")
                            .font(.system(size: 11.5))
                    }
                    GridRow {
                        Text("加错字映射")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                        Text("在对应行业文件里加一行「错写=正写」")
                            .font(.system(size: 11.5))
                    }
                    GridRow {
                        Text("热词上限")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                        Text("真专名（人名/项目名）才可以进热词，通用词会导致复读与降速")
                            .font(.system(size: 11.5))
                    }
                }
            }
        }
    }

    // MARK: - 交互

    private func switchTo(_ item: ConsoleState.IndustryInfo.Item) {
        switching = item.key
        isError = false
        message = nil
        Task {
            defer { switching = nil }
            do {
                try await api.setIndustry(item.key, token: service.apiToken)
                await api.refresh(token: service.apiToken)
                message = "已切换到「\(item.label)」，热词与错字映射即时生效"
            } catch {
                isError = true
                message = "切换失败：\(error.localizedDescription)"
            }
        }
    }

    private func openEditor(_ item: ConsoleState.IndustryInfo.Item) {
        termsError = nil
        editorText = ""
        editing = EditingIndustry(key: item.key, label: item.label)
        Task {
            do {
                let t = try await api.industryTerms(item.key, token: service.apiToken)
                editorText = t.text
            } catch {
                termsError = "词表读取失败：\(error.localizedDescription)"
            }
        }
    }

    private func saveTerms(_ key: String, text: String) {
        isSavingTerms = true
        termsError = nil
        Task {
            defer { isSavingTerms = false }
            do {
                let r = try await api.saveIndustryTerms(key, text: text,
                                                        token: service.apiToken)
                await api.refresh(token: service.apiToken)
                isError = false
                message = "已保存「\(r.industry)」词表：\(r.terms) 个术语、\(r.fixes) 条映射，即时生效"
                editing = nil
            } catch {
                termsError = "保存失败：\(error.localizedDescription)"
            }
        }
    }

    private func banner(_ text: String) -> some View {
        HStack(spacing: 8) {
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
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill((isError ? Color.red : Color.green).opacity(0.08)))
    }
}

// MARK: - 词表编辑器

/// 编辑一个行业的词表原文。
///
/// 直接编辑文本而不是结构化表单：词表格式就是「一行一词 / 错写=正写」，
/// 文本编辑对懂格式的用户最快；格式校验（行长、映射两侧非空）在服务端做，
/// 错误消息原样透传到这里。
private struct IndustryEditorSheet: View {
    let title: String
    let initialText: String
    let isSaving: Bool
    let error: String?
    let onSave: (String) -> Void
    let onDismiss: () -> Void

    @State private var text: String = ""
    @State private var loaded = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            TextEditor(text: $text)
                .font(.system(size: 12).monospaced())
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            Divider()
            footer
        }
        .onAppear {
            if !loaded {
                text = initialText
                loaded = true
            }
        }
        // 拉取完成后同步一次：sheet 先开、后到的词表原文要填进去
        .onChange(of: initialText) { _, newValue in
            if !loaded || text.isEmpty {
                text = newValue
                loaded = true
            }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Label("编辑词表 · \(title)", systemImage: "text.book.closed")
                .font(.system(size: 12.5, weight: .semibold))
            Spacer()
            Button("取消") { onDismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var footer: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text("首行 `# label: 中文名`；每行一个术语，或 `错写=正写` 做同音替换。")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                if let e = error {
                    Text(e)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
            if isSaving { ProgressView().controlSize(.small) }
            Button {
                onSave(text)
            } label: {
                Text("保存")
            }
            .buttonStyle(.borderedProminent)
            .disabled(isSaving || text.isEmpty)
            .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(Material.bar)
    }
}
