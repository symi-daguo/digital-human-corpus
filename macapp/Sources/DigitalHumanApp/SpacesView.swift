import SwiftUI

/// 空间管理（多租户，2026-09-09）：每部手机一个空间，数据目录级物理隔离。
///
/// 容器用 Theme 的 GlassCard、卡片用 Theme token，与概览页同一套设计语言；
/// 亮暗随 ThemeManager 切换。
///
/// 语义说明（页面里也写了）：创建 = 注册表新增 + 服务端立即建库；
/// 注销 = 只移出注册表，`data/tenants/<id>/` 数据目录保留（防误删，
/// 物理清除必须由用户到访达确认后手动进行）。
struct SpacesView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var api: APIClient

    @State private var newID = ""
    @State private var newName = ""
    @State private var busy = false
    @State private var message: String?
    @State private var messageIsError = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                intro
                spaceList
                createForm
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.pageH)
            .padding(.top, Theme.pageTop)
            .padding(.bottom, Theme.pageBottom)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await reload() }
    }

    // MARK: - 说明

    private var intro: some View {
        GlassCard(title: "每部手机一个空间，数据完全隔离", symbol: "square.stack.3d.up") {
            VStack(alignment: .leading, spacing: 6) {
                Text("成员手机的录音、日报、声纹、角色全部落在独立的 data/tenants/<空间ID>/ 目录里，相互不可见。接入流程：在这里创建空间 → 成员手机「设置」填空间 ID → 数据即隔离。备份可按单个空间或全部打包。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - 空间列表

    private var spaceList: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    SectionHeader(title: "已注册空间", symbol: "square.stack.3d.up")
                    Spacer()
                    if busy { ProgressView().controlSize(.small) }
                    Button {
                        Task { await reload() }
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                    .disabled(busy || service.status != .running)
                }
                if let tenants = api.state?.tenants, !tenants.isEmpty {
                    // 每张空间卡至少 360pt 宽，列数上限取「实际卡数」——
                    // 旧的 `adaptive(minimum: 360)` 会先把列数按宽度定死（1800pt 下是 4 列），
                    // 再让 2 张卡去分，于是右边整整空掉一半（用户反馈「拉宽了卡片没跟着变」）。
                    // 这里反过来：先按卡数定上限，再等分铺满。
                    ResponsiveGrid(minColumnWidth: 360,
                                   maxColumns: max(1, min(3, tenants.count)),
                                   spacing: Theme.space3) {
                        ForEach(tenants) { t in
                            card(t)
                        }
                    }
                } else if service.status == .running {
                    Text(busy ? "读取中…" : "还没有空间数据")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if let message {
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(messageIsError ? Color.red : Color.green)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func card(_ t: TenantInfo) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(t.name).font(.headline)
                if t.id == "default" {
                    Pill(text: "默认")
                }
                Spacer()
                if t.id == api.tenant {
                    Pill(text: "当前", tint: Theme.green)
                }
                if t.id != "default" {
                    Button(role: .destructive) {
                        Task { await remove(t.id) }
                    } label: {
                        Label("注销", systemImage: "trash")
                    }
                    .disabled(busy)
                    .help("只从注册表移除，数据目录保留")
                }
            }
            Text(t.id).font(.system(.callout, design: .monospaced)).foregroundStyle(.secondary)
            Text("日报文本 \(t.textFiles) · 语料 \(t.corpusFiles) · 标记 \(t.markers) · 库 \(Self.fmtBytes(t.dbBytes))")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                if t.id != api.tenant {
                    Button("切换到此空间") { api.tenant = t.id }
                        .controlSize(.small)
                }
                Spacer()
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusM)
                .fill(t.id == api.tenant ? Theme.teal.opacity(0.10) : Theme.surface1)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusM)
                .strokeBorder(t.id == api.tenant ? Theme.teal.opacity(0.5) : Theme.stroke)
        )
    }

    // MARK: - 新建

    private var createForm: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "新增空间", symbol: "plus.rectangle.on.rectangle")
                HStack(spacing: 10) {
                    TextField("空间 ID（小写字母开头，如 zhangsan）", text: $newID)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 320)
                    TextField("显示名（如 张三）", text: $newName)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 200)
                    Button("创建") {
                        Task { await add() }
                    }
                    .disabled(busy || newID.trimmingCharacters(in: .whitespaces).isEmpty)
                    .keyboardShortcut(.defaultAction)
                }
                Text("ID 规则：小写字母开头，1–24 位小写字母/数字/中划线/下划线。创建后把 ID 告诉成员，填进手机「设置 → 空间 ID」。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - 动作

    private func reload() async {
        guard service.status == .running else { return }
        busy = true
        defer { busy = false }
        do {
            let tenants = try await api.listTenants(token: service.apiToken)
            api.updateTenants(tenants)
            clearMessage()
        } catch {
            show(error.localizedDescription, isError: true)
        }
    }

    private func add() async {
        let id = newID.trimmingCharacters(in: .whitespaces)
        busy = true
        defer { busy = false }
        do {
            let tenants = try await api.addTenant(id: id, name: newName, token: service.apiToken)
            api.updateTenants(tenants)
            newID = ""; newName = ""
            show("空间「\(id)」已创建。让成员在手机设置里填这个空间 ID 即可接入。", isError: false)
        } catch {
            show(error.localizedDescription, isError: true)
        }
    }

    private func remove(_ id: String) async {
        guard id != "default" else { return }
        let alert = NSAlert()
        alert.messageText = "注销空间 \(id)？"
        alert.informativeText = "只从注册表移除，数据目录 data/tenants/\(id)/ 会保留，重新注册同名空间即可恢复。"
        alert.addButton(withTitle: "注销")
        alert.addButton(withTitle: "取消")
        alert.alertStyle = .warning
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        busy = true
        defer { busy = false }
        do {
            let tenants = try await api.removeTenant(id, token: service.apiToken)
            api.updateTenants(tenants)
            if api.tenant == id { api.tenant = "default" }
            show("空间 \(id) 已注销（数据保留在 data/tenants/\(id)/）。", isError: false)
        } catch {
            show(error.localizedDescription, isError: true)
        }
    }

    private func show(_ text: String, isError: Bool) {
        message = text
        messageIsError = isError
    }

    private func clearMessage() {
        message = nil
        messageIsError = false
    }

    private static func fmtBytes(_ n: Int) -> String {
        if n >= 1_048_576 { return String(format: "%.1f MB", Double(n) / 1_048_576) }
        if n >= 1024 { return String(format: "%.0f KB", Double(n) / 1024) }
        return "\(n) B"
    }
}
