import SwiftUI

// v1.5.2 删掉了 `DetailWidthKey` / `detailWidth` 环境值与 `Breakpoint` 断点枚举。
//
// 它们服务的是「主窗口量一次宽度、下发给各页决定单栏还是双栏」。这套机制实际上
// 从未生效：量宽用的是 `.background { Color.clear.preference(...) }`，而 background
// 是独立子树，preference 传不到外层，`detailWidth` 永远停在初始值 900 ——
// 于是「宽屏固定四列」「宽屏双栏」两条分支一直是死的，1800pt 窗口下四张指标卡
// 仍按 adaptive 挤在左边一半（用户反馈「界面拉宽了卡片没有响应」的根因）。
//
// 与其修好一套「全局量一次、再往下发」的间接机制，不如让每个需要分栏的网格
// 自己量自己的可用宽度 —— 于是有了 `Theme.ResponsiveGrid`：它不需要知道
// 「详情区多宽」，也不需要减去各层内边距去猜，嵌在卡片里同样正确。
// 概念从两套（环境值 + 断点）收敛到一处。

/// 主窗口的四个分区。
///
/// 用 enum 而不是字符串 tag：`List(selection:)` 需要 Hashable，
/// 编译期就能保证不会出现「选中了一个不存在的页」。
enum SidebarItem: String, CaseIterable, Identifiable {
    case overview
    case report
    case industry
    case speakers
    case spaces
    case logs
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: "概览"
        case .report:   "日报"
        case .industry: "行业词表"
        case .speakers: "角色"
        case .spaces:   "空间管理"
        case .logs:     "日志"
        case .settings: "设置"
        }
    }

    var symbol: String {
        switch self {
        case .overview: "gauge.with.dots.needle.33percent"
        case .report:   "doc.text"
        case .industry: "text.book.closed"
        case .speakers: "person.2"
        case .spaces:   "square.stack.3d.up"
        case .logs:     "terminal"
        case .settings: "gearshape"
        }
    }

    /// 侧栏图标配色。
    ///
    /// 依据 macOS 27 Golden Gate 的界面调整：**边栏延伸到窗口边缘，图标恢复各自的
    /// 颜色**（macOS 26 的浮动边栏把图标压成了单色）。系统 App 的边栏就是这个做法 ——
    /// 有明确语义的功能入口上色，纯粹的系统工具（日志、设置）保持中性，
    /// 一列彩色图标里留两处安静的位置，扫视时反而更快定位。
    ///
    /// 彩色取自本项目的 Glass 色板（不是新造的颜色），亮暗两套值自动跟随主题。
    var tint: Color {
        switch self {
        case .overview: Theme.teal      // 服务运行态的主色
        case .report:   Theme.indigo
        case .industry: Theme.amber
        case .speakers: Theme.green
        case .spaces:   Theme.indigo
        case .logs:     Theme.textSecondary
        case .settings: Theme.textSecondary
        }
    }
}

/// 主窗口：菜单栏 app 的「可见形态」。
///
/// 为什么需要它：`LSUIElement=1` 的菜单栏 app 没有任何窗口，服务哪怕一直在跑，
/// 用户也没有一个地方能确认「它在跑、跑得怎么样、产出是什么」。
/// 这里用 macOS 标准的 `NavigationSplitView`（系统设置 / 邮件同款布局），
/// 全部控件都是系统原生，不自定义配色，亮暗模式与动态字体自动跟随。
///
/// 响应式三件事：侧栏宽度给了区间而不是定值、侧栏可折叠、
/// 详情区各页的卡片网格自己量宽分栏（`Theme.ResponsiveGrid`）。
struct MainWindowView: View {
    @EnvironmentObject var service: ServiceManager
    @EnvironmentObject var theme: ThemeManager
    @StateObject private var api = APIClient.shared
    @State private var selection: SidebarItem = .overview
    @State private var columnVisibility: NavigationSplitViewVisibility = .automatic

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            sidebar
        } detail: {
            detail
        }
        // 只钉一个很小的下限：它是防止 NavigationSplitView 把窗口压成 1×32 的
        // 保险（实测过），不是设计尺寸。真正的尺寸由 AppDelegate 按屏幕算。
        .frame(minWidth: 640, minHeight: 420)
        // 打开窗口即拉一次；服务没起来时 refresh 会把错误写进 lastError，
        // 各页面据此显示「服务未运行」而不是空白。
        .task { await api.refresh(token: service.apiToken) }
    }

    // MARK: - 侧栏

    private var sidebar: some View {
        List(SidebarItem.allCases, selection: $selection) { item in
            // 图标单独给色（Golden Gate 的「边栏图标恢复彩色」），文字仍走系统
            // 默认色。不能整个 Label 加 `.foregroundStyle`：那会把标题一起染色，
            // 选中行的白字会变成彩字，在蓝色高亮底上直接糊掉。
            // 选中行交给系统惯例 —— 高亮底上图标转白，与标题同色。
            Label {
                Text(item.title)
            } icon: {
                Image(systemName: item.symbol)
                    .foregroundStyle(item == selection ? Color.white : item.tint)
            }
            .tag(item)
        }
        .listStyle(.sidebar)
        // 区间而不是定值：用户拖分隔条时侧栏可以在 176~280 之间连续变化，
        // 系统还会记住用户拖出来的宽度（automatic 模式下按内容自适应）。
        .navigationSplitViewColumnWidth(min: 176, ideal: 208, max: 280)
        .safeAreaInset(edge: .bottom) {
            SidebarStatusFooter()
                .environmentObject(service)
        }
    }

    // MARK: - 详情

    private var detail: some View {
        Group {
            switch selection {
            case .overview: OverviewView()
            case .report:   ReportView()
            case .industry: IndustryView()
            case .speakers: SpeakersView()
            case .spaces:   SpacesView()
            case .logs:     LogView()
            case .settings: SettingsView()
            }
        }
        // 详情区**不再需要额外的顶部安全带**（v1.5.2）。
        //
        // 1.5.1 时这里挂过 `.padding(.top, 40)`：那会儿窗口用 macOS 26 默认的
        // 浮动工具栏，工具栏下面还拖着一条同高材质带盖到 104pt，而详情区滚动内容
        // 从 52pt 起排，首张卡片正中招。把内容整体下移确实躲开了，但代价是
        // 详情区比侧栏**低了 40pt**，两眼一看就是没对齐（用户反馈「下面空了一个
        // 高度、没有对齐左边的菜单栏」）。
        //
        // 正解是让工具栏不再压在内容上：窗口改成 `toolbarStyle = .unified`
        // （见 AppDelegate.makeMainWindow），工具栏占独立高度、材质带随之消失，
        // 详情区与侧栏从同一个基线起排 —— 既对齐又不会被糊。
        // 详情区背景交给系统 Material，而不是铺一块不透明的品牌底色。
        //
        // 依据 macOS 27 Golden Gate：Liquid Glass 增加了**系统级透明度滑块**
        // （超透明 ←→ 不透明），深色模式还会自动降低透明度。滑块作用的对象就是
        // 窗口底材 —— 我们原先在这里铺 Theme.bg 实色，等于把滑块整个吃掉：
        // 用户把系统调成「更透明」，本窗口纹丝不动，在一排会变的原生窗口里很突兀。
        //
        // 品牌冷调靠一层很淡的叠加保留（45%），其余透明度交还系统，
        // 这样滑块、深色自动降透明度、以及「当前窗口材质提亮」都能正常体现。
        .background {
            Rectangle()
                .fill(.regularMaterial)
                .overlay(Theme.bg.opacity(0.45))
        }
        .environmentObject(service)
        .environmentObject(api)
        // 详情区不能无限窄：窄到一定程度侧栏会反过来挤压内容。
        // 480 是「日报一行的字还能读」的下限。
        .frame(minWidth: 480)
        .navigationTitle(selection.title)
        .toolbar { toolbarContent }
    }

    /// 工具栏。
    ///
    /// 排布遵循 macOS 惯例：左侧导航、右侧操作，**同类控件聚在一起**。
    /// 顺序 = 导航｜刷新 · 空间 · 主题｜工作台，中间用分隔线分组。
    ///
    /// ⚠️ 两个 `Picker` 的菜单项**必须用纯 `Text`，不能包 `Label`**：工具栏会把
    /// 带图标的 label 压成「只显示图标」，于是空间选择器只剩一个空胶囊、主题选择器
    /// 只剩一个月亮 —— 用户反馈「切换空间的文字和当前主题的文字都没有显示」。
    /// 纯 Text 没有图标可压，文字就正常显示（AX 侧读到的一直是 value，与显示无关）。
    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            Button {
                NSApp.sendAction(#selector(NSSplitViewController.toggleSidebar(_:)),
                                 to: nil, from: nil)
            } label: {
                Label("切换侧栏", systemImage: "sidebar.leading")
            }
            .help("显示 / 隐藏侧栏")
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                Task { await api.refresh(token: service.apiToken) }
            } label: {
                Label("刷新", systemImage: "arrow.clockwise")
            }
            .disabled(api.isLoading || service.status != .running)
            .help("重新读取服务状态与日报索引")
        }
        // 租户空间切换器（多租户，2026-09-09）：概览/日报/角色/日志的数据
        // 全部跟随当前空间；服务端按目录物理隔离，切过去就是别人的数据看不到。
        ToolbarItem(placement: .primaryAction) {
            Picker("空间", selection: $api.tenant) {
                if let tenants = api.state?.tenants, !tenants.isEmpty {
                    ForEach(tenants) { t in
                        Text(t.name).tag(t.id)
                    }
                } else {
                    // 状态没拉回来之前给一句占位，而不是一个空胶囊：
                    // 空控件会让用户以为功能坏了（实际只是还没加载）。
                    Text(scopedTenantLabel).tag(api.tenant)
                }
            }
            .pickerStyle(.menu)
            .fixedSize()
            .disabled(service.status != .running || api.isLoading)
            .help("切换空间：每台设备一个空间，数据完全隔离")
            .onChange(of: api.tenant) { _, _ in
                Task { await api.refresh(token: service.apiToken) }
            }
        }
        // 主题三档切换：跟随系统 / 浅色 / 深色（DESIGN.md §1 原则 4）。
        // 与手机 APK 的深色玻璃风格对齐后，深色是默认观感，
        // 浅色用户也能一键切过去，不必去系统设置里找。
        //
        // ⚠️ 这里用 `Menu` 而不是 `Picker`：同样一份选项，`Picker(.menu)` 在工具栏
        // 里会被压成一对升降箭头（只剩指示器、标题不见了，实测截图就是那样），
        // `Menu` 的 label 是纯 `Text`，文字正常显示。
        ToolbarItem(placement: .primaryAction) {
            Menu {
                ForEach(ThemeMode.allCases) { m in
                    Button {
                        theme.set(m)
                    } label: {
                        // 当前项打勾：menu 里没有原生单选标记，靠图标表达
                        if theme.mode == m {
                            Label(m.label, systemImage: "checkmark")
                        } else {
                            Text(m.label)
                        }
                    }
                }
            } label: {
                Text(theme.mode.label)
            }
            .help("设置界面主题：跟随系统、浅色或深色")
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                service.openConsole()
            } label: {
                Label("工作台", systemImage: "safari")
            }
            .disabled(service.status != .running)
            .help("在浏览器中打开网页版工作台（局域网内其他设备也能访问）")
        }
    }

    /// 空间选择器的占位文案：有 id 就显示 id（比空着强），完全没有就说明还没就绪。
    private var scopedTenantLabel: String {
        let t = api.tenant.trimmingCharacters(in: .whitespaces)
        return t.isEmpty ? "空间未就绪" : t
    }
}

/// 侧栏底部的常驻状态条。
///
/// 放在侧栏而不是详情区：无论切到哪一页，服务是否在跑都始终可见。
/// 这是 Docker Desktop / Tailscale 这类常驻工具的通行做法。
private struct SidebarStatusFooter: View {
    @EnvironmentObject var service: ServiceManager

    var body: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: 8) {
                Image(systemName: service.status.symbol)
                    .font(.system(size: 12))
                    .foregroundStyle(statusColor)
                    .frame(width: 14)
                VStack(alignment: .leading, spacing: 0) {
                    Text(service.status.label)
                        .font(.system(size: 11, weight: .medium))
                    // 用 verbatim：`Text("… \(Int)")` 走的是 LocalizedStringKey，
                    // 会把 8766 格式化成「8,766」，与概览页的「端口 8766」不一致
                    Text(verbatim: "端口 \(Paths.apiPort)")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
        }
        .background(Material.bar)
    }

    private var statusColor: Color {
        switch service.status {
        case .stopped:  .secondary
        case .starting: .orange
        case .running:  .green
        case .failed:   .red
        }
    }
}
