import SwiftUI

// MARK: - 主题模式

/// 用户可选的三档主题：跟随系统 / 浅色 / 深色。
///
/// 手动指定通过 NSApp.appearance 覆盖（只影响本 app，不动系统设置），
/// system 时置 nil 交还给系统。选择持久化在 UserDefaults，
/// 启动时在 applicationDidFinishLaunching 之前由 ThemeManager.shared 的
/// 构造器应用（App struct 的 StateObject 初始化先于窗口创建）。
enum ThemeMode: String, CaseIterable, Identifiable {
    case system
    case light
    case dark

    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: "跟随系统"
        case .light:  "浅色"
        case .dark:   "深色"
        }
    }

    var symbol: String {
        switch self {
        case .system: "circle.lefthalf.filled"
        case .light:  "sun.max"
        case .dark:   "moon"
        }
    }

    var appearance: NSAppearance? {
        switch self {
        case .system: nil
        case .light:  NSAppearance(named: .aqua)
        case .dark:   NSAppearance(named: .darkAqua)
        }
    }
}

/// 主题状态与应用入口。
///
/// 为什么是单例而不是环境注入一路传：NSApp.appearance 是全局副作用，
/// 工具条的选择器需要 binding 写回，页面内组件需要读 token——
/// 三处消费者共享同一份状态，@ObservedObject 持 shared 最直接。
@MainActor
final class ThemeManager: ObservableObject {
    static let shared = ThemeManager()

    private static let key = "themeMode"

    @Published private(set) var mode: ThemeMode {
        didSet { UserDefaults.standard.set(mode.rawValue, forKey: Self.key) }
    }

    private init() {
        mode = ThemeMode(rawValue: UserDefaults.standard.string(forKey: Self.key) ?? "") ?? .system
        NSApp.appearance = mode.appearance
    }

    /// 切换主题（工具条选择器与设置页共用）。
    func set(_ newMode: ThemeMode) {
        mode = newMode
        NSApp.appearance = newMode.appearance
    }
}

// MARK: - 颜色工具

private extension NSColor {
    /// hex → NSColor（0xRRGGBB）。
    convenience init(hex: UInt32) {
        self.init(
            red: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }
}

private extension Color {
    /// 亮暗双值注册：由系统（或 NSApp.appearance 覆盖值）决定解析到哪一支。
    ///
    /// 为什么不用两套 Color 手动 switch：dynamicProvider 让卡片底、文字色
    /// 在主题切换的瞬间自动失效重绘，不需要每个视图额外监听 ThemeManager。
    static func dh(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(NSColor(name: nil, dynamicProvider: { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .vibrantDark]) != nil
            return NSColor(hex: isDark ? dark : light)
        }))
    }
}

// MARK: - 设计 Token

/// 与手机 APK `Theme.kt`（Glass object）同一套色板的 Mac 端映射。
/// 语义与取值依据见 macapp/DESIGN.md——改色请两边同步改，本文档是中间契约。
enum Theme {
    // 背景 / 卡片（tonal elevation：Bg → Surface0 → Surface1）
    static let bg = Color.dh(0xF5F7FB, 0x0B0F1A)
    static let surface0 = Color.dh(0xFFFFFF, 0x0F1424)
    static let surface1 = Color.dh(0xFFFFFF, 0x161D33)

    // 描边与高光（Raycast 双环手法）
    static let stroke = Color.dh(0xE3E8F2, 0x1F2A47)
    static let topHighlight = Color.dh(0xFFFFFF, 0xFFFFFF)

    // 文字三级
    static let textPrimary = Color.dh(0x1A2233, 0xECF1FA)
    static let textSecondary = Color.dh(0x5A6B85, 0x95A2BD)
    static let textTertiary = Color.dh(0x8C99B0, 0x5C6A87)

    // 强调色（亮色端加深保对比度，深色端与 APK 一致）
    static let teal = Color.dh(0x0D9488, 0x5EEAD4)
    static let indigo = Color.dh(0x6366F1, 0x818CF8)
    static let amber = Color.dh(0xD97706, 0xFBBF24)
    static let red = Color.dh(0xDC2626, 0xF87171)
    static let green = Color.dh(0x16A34A, 0x4ADE80)
    static let onAccent = Color.dh(0xFFFFFF, 0x06211C)

    // 圆角阶梯（与 APK 一致）
    static let radiusS: CGFloat = 6
    static let radiusM: CGFloat = 10
    static let radiusL: CGFloat = 14
    static let radiusXL: CGFloat = 20

    // 间距网格（8dp 基）
    static let space1: CGFloat = 4
    static let space2: CGFloat = 8
    static let space3: CGFloat = 12
    static let space4: CGFloat = 16
    static let space5: CGFloat = 20
    static let space6: CGFloat = 24

    // 页面容器（v1.5.2 统一规范，7 个页面共用）
    //
    // 顶部为 0：实测内容区顶端与侧栏首项顶边重合（都在工具栏下沿），
    // 详情区再叠任何内边距都会「右边整块往下掉」。左右 20 保留。
    static let pageH: CGFloat = 20
    static let pageTop: CGFloat = 0
    static let pageBottom: CGFloat = 20

    /// 设置页「标签列」的固定宽度。
    ///
    /// 所有设置行的控件左缘由它对齐 —— 这是让一屏设置项看起来成体系的关键，
    /// 也是原先 `Spacer` + `maxWidth: 340` 做法最欠缺的东西。
    ///
    /// 220 是量出来的：168 时「mlx-lm 的 /v1/models 返回目录绝对路径，请求体必须
    /// 原样带回」这类说明会被折成 3 行，行高忽高忽低。220 下最多 2 行。
    static let formLabel: CGFloat = 220
}

// MARK: - 组件

/// 标准卡片：Surface0 底 + 1px Stroke 弱描边 + 内侧顶部高光 + 圆角 14。
///
/// 替代系统 GroupBox：GroupBox 在深色下是一块和背景几乎没差的灰，
/// 层次出不来（用户反馈「没有层次感」的直接原因）。这里的层次来自
/// tonal elevation（Bg → Surface0）+ 双环描边，见 DESIGN.md 第 4 节。
struct GlassCard<Content: View>: View {
    var title: String?
    var symbol: String?
    private let content: Content

    /// 必须显式声明 @ViewBuilder 的 init：memberwise init 的 content 参数
    /// 没有 builder 标注，调用端的 trailing closure 匹配不上，
    /// 会被编译器当成「未使用的独立表达式」直接丢弃（实测踩坑）。
    init(title: String? = nil, symbol: String? = nil,
         @ViewBuilder content: () -> Content) {
        self.title = title
        self.symbol = symbol
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let title {
                SectionHeader(title: title, symbol: symbol)
            }
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusL)
                .fill(Theme.surface0)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusL)
                .strokeBorder(Theme.stroke, lineWidth: 1)
        )
        .overlay(
            // 内侧顶部高光：1px 白线只画上边缘，模拟顶光（Raycast 手法）
            RoundedRectangle(cornerRadius: Theme.radiusL)
                .strokeBorder(Theme.topHighlight.opacity(0.5), lineWidth: 1)
                .mask(
                    LinearGradient(
                        colors: [.white, .clear],
                        startPoint: .top,
                        endPoint: .init(x: 0.5, y: 0.28)
                    )
                )
                .padding(1)
        )
        // 亮色下白卡贴浅灰背景需要投影才能浮起来；深色下投影不可见，无害
        .shadow(color: .black.opacity(0.06), radius: 3, y: 1)
    }
}

/// 响应式等宽栅格：按**自己的实际可用宽度**决定列数（1～`maxColumns`），
/// 列宽用 `flexible()` 等分 —— 有几张卡就铺几张，每张一样宽。
///
/// 为什么不用 `GridItem(.adaptive(minimum:))`：adaptive 先按 minimum 把列数定死，
/// 再让每列去分宽度。于是「宽度够放 4 列、但只有 2 张卡」时右边会整整空掉一半
/// —— 1800pt 窗口下的空间管理页实测就是这样，用户反馈「界面拉宽了卡片却没跟着变」。
/// 这里反过来：先按可用宽度算列数，再用等分列铺满。
///
/// 为什么不读 `detailWidth` 环境值：那个值来自主窗口在 `.background` 里量宽后
/// 上抛的 Preference —— **background 是独立子树，preference 传不出去**，
/// 实测它永远停在初始值 900。自带量宽（在本视图 `.background` 里 onChange）
/// 不跨子树，拿到的就是这一层真正的可用宽度，嵌在卡片里也不用减内边距去猜。
struct ResponsiveGrid<Content: View>: View {
    /// 单列至少要有多宽才值得分列。决定「几列」只靠它和实际宽度。
    let minColumnWidth: CGFloat
    /// 列数上限。列表类内容传 `min(上限, 卡片数)`，避免几张卡占着一排空位。
    var maxColumns: Int = 3
    var spacing: CGFloat = Theme.space4
    var alignment: HorizontalAlignment = .leading
    private let content: Content

    /// 显式 @ViewBuilder init：memberwise init 的参数没有 builder 标注，
    /// 调用端的 trailing closure 会被编译器当成「未使用的独立表达式」丢掉
    /// （GlassCard 上踩过同一个坑，见那里的注释）。
    init(minColumnWidth: CGFloat,
         maxColumns: Int = 3,
         spacing: CGFloat = Theme.space4,
         alignment: HorizontalAlignment = .leading,
         @ViewBuilder content: () -> Content) {
        self.minColumnWidth = minColumnWidth
        self.maxColumns = maxColumns
        self.spacing = spacing
        self.alignment = alignment
        self.content = content()
    }

    @State private var available: CGFloat = 0

    private var columnCount: Int {
        // 首帧还没量到宽度时按单列渲染，量到后自动进位（一帧的过渡，无震荡：
        // 本视图的宽度由外层决定，不受自己列数影响）
        guard available > 0, minColumnWidth > 0 else { return 1 }
        let n = Int((available + spacing) / (minColumnWidth + spacing))
        return max(1, min(n, maxColumns))
    }

    var body: some View {
        let cols = columnCount
        Group {
            if cols <= 1 {
                VStack(alignment: alignment, spacing: spacing) { content }
            } else {
                LazyVGrid(
                    columns: Array(repeating: GridItem(.flexible(), spacing: spacing),
                                   count: cols),
                    alignment: alignment,
                    spacing: spacing
                ) { content }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            GeometryReader { geo in
                Color.clear
                    .onAppear { available = geo.size.width }
                    .onChange(of: geo.size.width) { _, w in available = w }
            }
        }
    }
}

/// 卡片标题行：12pt semibold + 可选符号。
///
/// 对齐 APK 的 titleMedium 层级；符号用强调色点亮，让一列卡片扫过去
/// 有视觉锚点（用户反馈「缺乏美感」的针对性处理）。
struct SectionHeader: View {
    let title: String
    var symbol: String?

    var body: some View {
        HStack(spacing: 7) {
            if let symbol {
                Image(systemName: symbol)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Theme.teal)
            }
            Text(title)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
        }
    }
}

/// 小胶囊标签（「默认」「当前」这类状态）。
struct Pill: View {
    let text: String
    var tint: Color = Theme.teal

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .medium))
            .foregroundStyle(tint)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(tint.opacity(0.14), in: Capsule())
    }
}
