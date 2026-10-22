import AppKit
import SwiftUI

/// 设置。
///
/// 版式与其余 6 个页面完全同一套：`ScrollView` + `Theme.GlassCard` + `Theme` 的
/// 间距/文字/描边 token。
///
/// ⚠️ 这里**不再**用 `.formStyle(.grouped)`。原先用系统 grouped Form 有两个后果：
/// 1. 它是全 app 唯一一个系统版式的页面 —— 卡片圆角、内边距、字号、分区头都跟
///    其他页不一样，用户看到的「每一页风格都不一致」就是它；
/// 2. grouped Form 会把 `TextField` 的 **placeholder 当成行标签**再渲染一次，
///    而每行本来就有一个标签，于是每行出现两个标签。
///
/// 行结构统一走 `SettingRow`（固定宽度标签列 + 填满剩余宽度的控件列），
/// 行容器走 `SettingRows`（宽屏自动分 1/2/3 列，让卡片填满窗口宽度）。
///
/// 分成两半：
/// - 「模型与接口」手工排（它是这个 app 最重要的一个选择，值得占一屏的位置，
///   并且要带厂商预设与连通性测试）；
/// - 其余分组由服务端 schema 驱动渲染（`row(_:)`），服务端加一个可配项，
///   这里不用改代码就多一行。
struct SettingsView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient
    @StateObject private var store = SettingsStore()

    /// 与 AppDelegate.autoStartKey 同一个 key：菜单栏里点过「停止服务」会写这里，
    /// 这一栏要如实反映那个状态，否则用户会困惑「为什么开机又自己跑了」。
    @AppStorage("serviceAutoStart") private var autoStart = true

    @State private var launchAtLogin = LaunchAgent.isEnabled
    @State private var launchAgentError: String?
    @State private var preset: LLMPreset?
    @State private var showModelPicker = false
    @State private var showAdvanced = false
    @State private var backingUp = false
    @State private var backupMessage: String?

    var body: some View {
        Group {
            if service.status != .running {
                unavailable
            } else if let err = store.loadError {
                // 加载失败必须显示错误与重试：否则解码或网络一旦失败，
                // 页面就永远停在「正在读取配置…」，用户无从下手
                ContentUnavailableView {
                    Label("配置读取失败", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(err).font(.system(size: 11.5))
                } actions: {
                    Button("重试") { Task { await store.load(token: service.apiToken) } }
                        .buttonStyle(.borderedProminent)
                }
            } else if store.llm == nil {
                ProgressView("正在读取配置…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                form
            }
        }
        .task { await store.load(token: service.apiToken) }
        // 服务重启后会回到 running，此时要重新拉一次：env.local 可能刚被改写，
        // 而界面上还留着旧值，用户会以为「保存没生效」。
        .onChange(of: service.status) { _, s in
            if s == .running { Task { await store.load(token: service.apiToken) } }
        }
        .sheet(isPresented: $showModelPicker) {
            ModelPickerSheet(models: store.modelList) { store.draft[SettingsStore.Key.model] = $0 }
        }
    }

    // MARK: - 骨架

    private var form: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.space4) {
                locationSection
                interfaceSection
                if let g = group("asr")     { schemaSection(g) }
                if let g = group("speaker") { schemaSection(g) }
                if let g = group("general") { schemaSection(g) }
                if let g = group("advanced") {
                    GlassCard(title: g.label, symbol: "slider.horizontal.3") {
                        VStack(alignment: .leading, spacing: Theme.space4) {
                            DisclosureGroup("高级（模型与目录路径）", isExpanded: $showAdvanced) {
                                SettingRows {
                                    ForEach(visibleFields(g)) { f in row(f) }
                                }
                            }
                            Text("改这些路径需要重启服务。默认由应用自动维护，一般不用动。")
                                .font(.system(size: 10.5))
                                .foregroundStyle(Theme.textTertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                startupSection
                dataSection
                aboutSection
                actionsSection
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.pageH)
            .padding(.top, Theme.pageTop)
            .padding(.bottom, Theme.pageBottom)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // 保存栏用 safeAreaInset 而不是 VStack 里另起一段：
        // 这样滚动内容会自动空出保存栏的高度，最后一行不会再被它压住
        // （改造前「热词」输入框就是被保存栏压掉一截的）。
        .safeAreaInset(edge: .bottom, spacing: 0) { saveBar }
    }

    // MARK: - 运行位置

    private var locationSection: some View {
        GlassCard(title: "模型与接口", symbol: "cpu") {
            SettingRows {
                SettingRow(label: "运行位置",
                           help: store.isCloud
                                 ? "云端：文本会发送给所选厂商。省掉约 2.9 GB 模型下载"
                                   + "与约 3 GB 常驻内存，8 GB 内存的 Mac 也能跑。"
                                 : "本地：语料完全不出网。需要已下载的文字模型，"
                                   + "并常驻约 3 GB 内存。") {
                    Picker("", selection: binding(SettingsStore.Key.provider)) {
                        Text("本地模型").tag("local")
                        Text("云端 API").tag("cloud")
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .fixedSize()
                }
            }
        }
    }

    // MARK: - 接口参数

    private var interfaceSection: some View {
        GlassCard(title: "接口参数", symbol: "network") {
            VStack(alignment: .leading, spacing: Theme.space4) {
                // 厂商预设：模型名各家都在变，预设只负责填域名，模型名可以「拉取」得到
                HStack(spacing: Theme.space3) {
                    Menu {
                        Section("国内") {
                            ForEach(store.presets.filter { $0.isDomestic }) { p in
                                Button(p.vendor) { applyPreset(p) }
                            }
                        }
                        Section("境外") {
                            ForEach(store.presets.filter { !$0.isDomestic }) { p in
                                Button(p.vendor) { applyPreset(p) }
                            }
                        }
                    } label: {
                        Label("服务商预设", systemImage: "building.2")
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    if let p = preset {
                        Button {
                            if let u = URL(string: p.keyUrl) { NSWorkspace.shared.open(u) }
                        } label: {
                            Text("去 \(p.vendor) 申请 Key")
                        }
                        .buttonStyle(.link)
                        .font(.system(size: 11))
                    }
                    Spacer(minLength: 0)
                }

                SettingRows {
                    if store.isCloud {
                        SettingRow(label: "接口协议",
                                   help: "国内厂商与 OpenAI / Gemini / xAI 都是 OpenAI 兼容；"
                                       + "只有直连 Claude 官方要选 Anthropic。") {
                            Picker("", selection: binding(SettingsStore.Key.proto)) {
                                Text("OpenAI 兼容").tag("openai")
                                Text("Anthropic 原生").tag("anthropic")
                            }
                            .labelsHidden()
                            .pickerStyle(.segmented)
                            .fixedSize()
                        }
                    }

                    SettingRow(label: "接口地址",
                               help: store.isCloud
                                     ? nil
                                     : "本机 mlx-lm 一般是 http://127.0.0.1:\(Paths.llmPort)/v1") {
                        TextField("https://api.deepseek.com/v1",
                                  text: binding(SettingsStore.Key.baseURL))
                            .textFieldStyle(.roundedBorder)
                            .font(.system(size: 11.5).monospaced())
                    }

                    SettingRow(label: "模型名", help: "不确定就点「拉取」从厂商那里选") {
                        HStack(spacing: Theme.space2) {
                            TextField("deepseek-v4-flash", text: binding(SettingsStore.Key.model))
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 11.5).monospaced())
                            Button("拉取") { Task { await store.fetchModels(token: service.apiToken) } }
                                .disabled(store.isLoadingModels || !store.isCloud)
                                .help("从厂商的 /models 接口读取可选模型")
                        }
                    }

                    SettingRow(label: "API Key",
                               help: keySet ? "已保存（留空表示不修改）" : "只存在本机 env.local") {
                        HStack(spacing: Theme.space2) {
                            SecureField(keySet ? "留空则不修改" : "sk-…",
                                        text: secretBinding(SettingsStore.Key.apiKey))
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 11.5).monospaced())
                            if keySet {
                                Button("清除") { store.secrets[SettingsStore.Key.apiKey] = "" }
                                    .font(.system(size: 11))
                            }
                        }
                    }

                    if store.isCloud {
                        SettingRow(label: "云端代理",
                                   help: "只有访问境外厂商且本机需要代理时才填，形如 http://127.0.0.1:7890") {
                            TextField("留空直连", text: binding(SettingsStore.Key.proxy))
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 11.5).monospaced())
                        }
                    }

                    SettingRow(label: "超时（秒）", help: "本地 4B 模型出长正文需要时间") {
                        TextField("300", text: binding(SettingsStore.Key.timeout))
                            .textFieldStyle(.roundedBorder)
                            .monospacedDigit()
                            .frame(width: 90)
                    }
                }

                // 本地模型路径**不放进上面的分栏网格**：它的值是目录绝对路径
                // （/Users/…/llm-models/Qwen3.5-4B-4bit），两列布局下控件列只剩约 500pt，
                // 会被截到看不出是哪个模型 —— 宽屏反倒比窄屏更难读。
                // 独占整行才放得下，也才对得上它「这是哪一版模型」的信息量。
                if !store.isCloud {
                    SettingRow(label: "本地模型路径",
                               help: "mlx-lm 的 /v1/models 返回目录绝对路径，请求体必须原样带回") {
                        TextField("留空则与模型名相同", text: binding(SettingsStore.Key.servedID))
                            .textFieldStyle(.roundedBorder)
                            .font(.system(size: 11.5).monospaced())
                    }
                }

                if let e = store.modelListError {
                    Text(e).font(.system(size: 10.5)).foregroundStyle(Theme.red)
                        .padding(.leading, Theme.formLabel + Theme.space4)
                } else if !store.modelList.isEmpty {
                    Button("已拉取 \(store.modelList.count) 个模型，点此选择") { showModelPicker = true }
                        .buttonStyle(.link)
                        .font(.system(size: 11))
                        .padding(.leading, Theme.formLabel + Theme.space4)
                }

                // 连通性测试：这一栏必须有。云端接不上的表现是「总结空白」，
                // 没有任何报错，靠猜是猜不出来的。
                HStack(spacing: Theme.space3) {
                    Button {
                        Task { await store.test(token: service.apiToken) }
                    } label: {
                        Label("测试连接", systemImage: "bolt.horizontal")
                    }
                    .disabled(store.isTesting)
                    if store.isTesting { ProgressView().controlSize(.small) }
                    if let r = store.testResult {
                        Image(systemName: r.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(r.ok ? Theme.green : Theme.red)
                            .font(.system(size: 12))
                        Text(r.message)
                            .font(.system(size: 11))
                            .foregroundStyle(r.ok ? Theme.textPrimary : Theme.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                Text("测的是当前填写的内容，不必先保存。未填的字段沿用已保存值。")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textTertiary)
            }
        }
    }

    // MARK: - schema 驱动的分组

    private func schemaSection(_ g: ConfigGroup) -> some View {
        GlassCard(title: g.label, symbol: "slider.horizontal.3") {
            SettingRows {
                ForEach(visibleFields(g)) { f in row(f) }
            }
        }
    }

    private func group(_ key: String) -> ConfigGroup? {
        store.groups.first { $0.key == key }
    }

    /// 过滤掉不适用于当前模式的字段（比如云端下不显示本地模型路径）。
    private func visibleFields(_ g: ConfigGroup) -> [ConfigField] {
        g.fields.filter { f in
            if f.localOnly == true && store.isCloud { return false }
            if f.cloudOnly == true && !store.isCloud { return false }
            return true
        }
    }

    @ViewBuilder
    private func row(_ f: ConfigField) -> some View {
        switch f.type {
        case "bool":
            SettingRow(label: f.label, help: f.help) {
                Toggle("", isOn: boolBinding(f.name))
                    .labelsHidden()
                    .toggleStyle(.switch)
            }
        // choice 与 select 都是「从固定枚举里挑一个」，渲染完全相同 ——
        // 早先只写了 choice，select（服务端的 DH_TALK_FORCE_HERMES /
        // DH_TALK_OWNER_GATE）掉进 default 变成一个空输入框。
        //
        // ⚠️ 用 `Menu` 而不是 `Picker(.menu)`：本机 macOS 27 上，
        // `Picker("") + .labelsHidden() + .pickerStyle(.menu)` 渲染出来
        // **只有一对升降箭头、没有选中项文字**（放大截图确认），
        // 用户看到的就是「右边一个空圈」。`Menu` 的 label 是纯 `Text`，
        // 文字正常显示，当前项用 checkmark 标记 —— 与工具栏主题菜单同一套方案。
        case "choice", "select":
            SettingRow(label: f.label, help: f.help) {
                Menu {
                    ForEach(f.options ?? []) { o in
                        Button {
                            store.draft[f.name] = o.value
                        } label: {
                            if (store.draft[f.name] ?? "") == o.value {
                                Label(o.label, systemImage: "checkmark")
                            } else {
                                Text(o.label)
                            }
                        }
                    }
                } label: {
                    Text(optionLabel(f))
                }
                .fixedSize()
            }
        case "number":
            SettingRow(label: f.label, help: f.help) {
                HStack(spacing: Theme.space3) {
                    Slider(value: numberBinding(f),
                           in: (f.min ?? 0)...(f.max ?? 1),
                           step: f.step ?? 0.01) {
                        EmptyView()
                    }
                    .frame(maxWidth: 320)
                    Text(store.draft[f.name] ?? "")
                        .font(.system(size: 10.5).monospacedDigit())
                        .foregroundStyle(Theme.textTertiary)
                        .frame(width: 48, alignment: .trailing)
                }
            }
        case "int":
            SettingRow(label: f.label, help: f.help) {
                TextField("", text: binding(f.name))
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .monospacedDigit()
                    .frame(width: 90)
            }
        case "textarea":
            VStack(alignment: .leading, spacing: Theme.space1) {
                Text(f.label).font(.system(size: 12))
                if let h = f.help {
                    Text(h)
                        .font(.system(size: 10.5))
                        .foregroundStyle(Theme.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                TextEditor(text: binding(f.name))
                    .font(.system(size: 11.5))
                    .frame(minHeight: 64)
                    .overlay(RoundedRectangle(cornerRadius: Theme.radiusS)
                        .strokeBorder(Theme.stroke, lineWidth: 1))
            }
        case "secret":
            SettingRow(label: f.label, help: f.help) {
                SecureField("", text: secretBinding(f.name))
                    .textFieldStyle(.roundedBorder)
            }
        case "text" where f.name == SettingsStore.Key.pairingToken:
            // 配对 Token 专用行：除手改外提供一键生成与拷贝。
            // 生成的值服务端已保存热生效，draft/baseline 已同步，不需要再点保存。
            VStack(alignment: .leading, spacing: Theme.space2) {
                SettingRow(label: f.label, help: f.help) {
                    TextField("", text: binding(f.name))
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 11.5).monospaced())
                }
                HStack(spacing: Theme.space3) {
                    Button {
                        Task { await store.rotatePairingToken(apiToken: service.apiToken) }
                    } label: {
                        if store.isRotatingToken {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("生成新 Token", systemImage: "arrow.triangle.2.circlepath")
                        }
                    }
                    .disabled(store.isRotatingToken)
                    Button("拷贝") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(
                            store.draft[SettingsStore.Key.pairingToken] ?? "", forType: .string)
                    }
                    .disabled((store.draft[SettingsStore.Key.pairingToken] ?? "").isEmpty)
                    if let m = store.rotateMessage {
                        Text(m).font(.system(size: 10.5)).foregroundStyle(Theme.textTertiary)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.leading, Theme.formLabel + Theme.space4)
            }
        default:
            SettingRow(label: f.label, help: f.help) {
                TextField("", text: binding(f.name))
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11.5).monospaced())
            }
        }
    }

    /// 下拉按钮上显示的文字：按当前值找选项的中文 label。
    ///
    /// 兜底两档：值不在选项里时显示值本身（多半是 env.local 被手改成非法值，
    /// 让用户看见比显示空白有用）；值为空时显示「未设置」，提示这一项还没配。
    private func optionLabel(_ f: ConfigField) -> String {
        let v = store.draft[f.name] ?? ""
        if let o = (f.options ?? []).first(where: { $0.value == v }) { return o.label }
        return v.isEmpty ? "未设置" : v
    }

    // MARK: - 本地段落（与服务端 schema 无关）

    private var startupSection: some View {
        GlassCard(title: "启动", symbol: "power") {
            VStack(alignment: .leading, spacing: Theme.space4) {
                Toggle("登录时自动启动应用", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, on in applyLaunchAgent(on) }
                Toggle("启动应用时自动拉起服务", isOn: $autoStart)
                if let e = launchAgentError {
                    Text(e).font(.system(size: 11)).foregroundStyle(Theme.red)
                }
                Text("「登录时自动启动」写入 ~/Library/LaunchAgents，"
                     + "「自动拉起服务」只影响本应用启动后是否立刻跑服务。两个互不依赖。")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var dataSection: some View {
        GlassCard(title: "数据", symbol: "internaldrive") {
            VStack(alignment: .leading, spacing: Theme.space4) {
                PathRow(title: "数据目录", path: service.dataRoot.path)
                PathRow(title: "日志文件", path: service.logsRoot.path)
                PathRow(title: "配置文件", path: store.envFile.isEmpty ? service.envFileURL.path : store.envFile)
                HStack(spacing: Theme.space3) {
                    Button {
                        Task { await backup() }
                    } label: {
                        if backingUp {
                            ProgressView().controlSize(.small)
                        } else {
                            Label("立即备份", systemImage: "externaldrive.badge.timemachine")
                        }
                    }
                    .disabled(backingUp)
                    if let m = backupMessage {
                        Text(m).font(.system(size: 11)).foregroundStyle(Theme.textTertiary)
                    }
                    Spacer(minLength: 0)
                }
                Text("备份会把归档库、转写文本与语料打包进 backups/；"
                     + "上面的设置就写在这个 env.local 里，它不入 git、不随应用更新被覆盖。")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var aboutSection: some View {
        GlassCard(title: "关于", symbol: "info.circle") {
            SettingRows {
                SettingRow(label: "应用版本") { Text(appVersion).monospacedDigit() }
                SettingRow(label: "服务版本") { Text(api.state?.service.version ?? "—").monospacedDigit() }
                SettingRow(label: "服务端口") { Text("\(Paths.apiPort)").monospacedDigit() }
                SettingRow(label: "文字模型端口") { Text("\(Paths.llmPort)").monospacedDigit() }
            }
        }
    }

    private var actionsSection: some View {
        GlassCard(title: "操作", symbol: "wrench.and.screwdriver") {
            HStack(spacing: Theme.space3) {
                Button("打开工作台") { service.openConsole() }
                Button("重启服务") { service.restart() }
                Spacer(minLength: 0)
                Button("停止服务") { service.stop() }
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
        }
    }

    // MARK: - 保存栏

    private var saveBar: some View {
        HStack(spacing: Theme.space3) {
            if let m = store.saveMessage {
                Text(m).font(.system(size: 11)).foregroundStyle(Theme.textTertiary)
            }
            if !store.restartRequired.isEmpty {
                Button("重启服务") { service.restart() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .help("有 \(store.restartRequired.count) 项需要重启后才生效")
            }
            Spacer(minLength: 0)
            if store.hasChanges {
                Text("有未保存的修改")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.amber)
            }
            Button("放弃") { store.revert() }
                .disabled(!store.hasChanges)
            Button {
                Task {
                    await store.save(token: service.apiToken)
                    // 切了「本地 / 云端」要立刻让本地模型进程跟着起落，
                    // 否则改完配置内存一点没变，用户会以为没生效。
                    service.reconcileLLM()
                }
            } label: {
                if store.isSaving { ProgressView().controlSize(.small) } else { Text("保存") }
            }
            .buttonStyle(.borderedProminent)
            .disabled(!store.hasChanges || store.isSaving)
        }
        .controlSize(.small)
        .padding(.horizontal, Theme.pageH)
        .padding(.vertical, 9)
        .background(Material.bar)
        .overlay(alignment: .top) { Divider() }
    }

    private var unavailable: some View {
        ContentUnavailableView {
            Label("服务未运行", systemImage: "power")
        } description: {
            Text("设置由服务端读写，需要先启动服务。")
                .font(.system(size: 11.5))
        } actions: {
            Button("启动服务") { service.start() }
                .buttonStyle(.borderedProminent)
        }
    }

    // MARK: - 绑定与辅助

    private var keySet: Bool { store.secretsSet[SettingsStore.Key.apiKey] ?? false }

    private func applyPreset(_ p: LLMPreset) {
        preset = p
        store.applyPreset(p)
        store.resetModels()
    }

    private func binding(_ key: String) -> Binding<String> {
        Binding(get: { store.draft[key] ?? "" },
                set: { store.draft[key] = $0 })
    }

    private func secretBinding(_ key: String) -> Binding<String> {
        Binding(get: { store.secrets[key] ?? "" },
                set: { store.secrets[key] = $0 })
    }

    private func boolBinding(_ key: String) -> Binding<Bool> {
        Binding(get: { (store.draft[key] ?? "0") == "1" },
                set: { store.draft[key] = $0 ? "1" : "0" })
    }

    private func numberBinding(_ f: ConfigField) -> Binding<Double> {
        Binding(
            get: {
                if let v = Double(store.draft[f.name] ?? "") { return v }
                if let d = f.defaultValue, let v = Double(d) { return v }
                return f.min ?? 0
            },
            set: { store.draft[f.name] = String(format: "%g", $0) }
        )
    }

    private var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "—"
        return "\(v) (\(b))"
    }

    /// 手动备份。服务端有 POST /api/backup 但界面一直没有入口，
    /// 升级前想留个底只能手工翻数据目录。
    private func backup() async {
        backingUp = true
        backupMessage = nil
        defer { backingUp = false }
        do {
            _ = try await api.triggerBackup(token: service.apiToken)
            backupMessage = "已备份到数据目录 backups/"
        } catch {
            backupMessage = "备份失败：\(error.localizedDescription)"
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
}

// MARK: - 子组件

/// 一行「标签 + 说明 + 右侧控件」。
///
/// 手写而不是用 `LabeledContent`：label 下面要挂一行 10.5 号的说明文字，
/// `LabeledContent` 的 label 只接受单个 View，套 VStack 会破坏它的基线对齐。
/// 一行「标签 + 说明 + 控件」。
///
/// 布局 = 固定宽度的标签列 + 填满剩余宽度的控件列，所有行的控件左缘因此对齐。
///
/// 早先这里是 `Spacer` + `control().frame(maxWidth: 340)`，在 `Form(.grouped)` 里
/// 又叠加了「系统把 TextField 的 placeholder 当行标签」的行为，于是每行出现
/// **两个标签**、标签与控件之间一大段空白、长路径被 340pt 截断 ——
/// 用户反馈「设置界面太差」的三条直接原因。
///
/// 不用 `LabeledContent`：label 下面要挂一行 10.5 号的说明文字，
/// `LabeledContent` 的 label 只接受单个 View，套 VStack 会破坏它的基线对齐。
private struct SettingRow<Control: View>: View {
    let label: String
    let help: String?
    private let control: Control

    /// 显式声明带 @ViewBuilder 的 init：memberwise init 的参数没有 builder 标注，
    /// 调用端的 trailing closure 会被编译器当成「未使用的独立表达式」直接丢掉
    /// （Theme.GlassCard 上踩过同一个坑，见那里的注释）。
    init(label: String, help: String? = nil, @ViewBuilder control: () -> Control) {
        self.label = label
        self.help = help
        self.control = control()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.space4) {
            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(.system(size: 12))
                if let help {
                    Text(help)
                        .font(.system(size: 10.5))
                        .foregroundStyle(Theme.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(width: Theme.formLabel, alignment: .leading)
            control
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// 设置行的容器：窄屏一行一条，宽屏按可用宽度自动分 2 / 3 列。
///
/// 设置项本身都很短，宽屏若仍竖排，单个输入框会被拉到 2000pt、中间一大段空白。
/// 分列既让卡片**填满窗口宽度**（用户要的「拉宽就跟着变」），
/// 又把每个控件保持在可读长度内 —— 这是设置页在宽屏下的正确形态。
///
/// 实现委托给 `Theme.ResponsiveGrid`：本文件原先自己写断点并读 `detailWidth`，
/// 而那个环境值实测一直是初始值（下发它的 preference 在 background 子树里，
/// 传不出来），于是「宽屏分列」从未生效过。现在统一由 ResponsiveGrid 自己量宽。
private struct SettingRows<Content: View>: View {
    private let content: Content

    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        // 一列 = 220pt 标签列 + 至少 340pt 控件列，再加 20pt 呼吸
        ResponsiveGrid(minColumnWidth: Theme.formLabel + 340, spacing: Theme.space5) {
            content
        }
    }
}

private struct PathRow: View {
    let title: String
    let path: String

    var body: some View {
        SettingRow(label: title, help: "点路径可在访达中显示") {
            HStack(spacing: Theme.space2) {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                } label: {
                    Text(path)
                        .font(.system(size: 11).monospaced())
                        .foregroundStyle(Theme.teal)
                        .underline()
                        .lineLimit(1)
                        // 中间截断：路径的头（/Users/…）与尾（模型目录名）都是有效信息，
                        // 留头去尾就看不出是哪个目录了
                        .truncationMode(.middle)
                        // 把可用宽度全部吃掉，长路径在按钮内部截断而不是溢出到外面
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                // ⚠️ 不能用 `.buttonStyle(.link)`：它给 label 提议的是内容的 ideal 宽度、
                // 不接受压缩，长路径会溢出被父容器硬裁（连省略号都没有），
                // 同一行的复制按钮还会被整个挤出可视区。实测 1800pt 窗口两列布局下就是这个
                // 现象。`.plain` 不额外加内边距，label 按可用宽度布局，截断才真正生效。
                .buttonStyle(.plain)
                .help(path)
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(path, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc").font(.system(size: 10))
                }
                .buttonStyle(.borderless)
                // 复制按钮不参与压缩，否则长路径下它会被压没
                .fixedSize()
                .help("复制路径")
            }
        }
    }
}

/// 厂商模型列表。厂商动辄几百个模型，用 Menu 会点不出来，
/// 所以给一个可搜索的列表（系统 `.searchable`）。
private struct ModelPickerSheet: View {
    let models: [String]
    let onPick: (String) -> Void

    @State private var query = ""
    @Environment(\.dismiss) private var dismiss

    private var filtered: [String] {
        guard !query.isEmpty else { return models }
        return models.filter { $0.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        NavigationStack {
            List(filtered, id: \.self) { m in
                Button {
                    onPick(m)
                    dismiss()
                } label: {
                    Text(m)
                        .font(.system(size: 12).monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.plain)
            }
            .searchable(text: $query, prompt: "搜索模型")
            .navigationTitle("选择模型（\(models.count)）")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
            }
        }
        .frame(minWidth: 460, minHeight: 520)
    }
}
