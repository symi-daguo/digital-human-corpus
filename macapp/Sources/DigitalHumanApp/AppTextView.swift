import SwiftUI

/// 只读的大文本视图，底层是 `NSTextView`。
///
/// 为什么不用 SwiftUI 的 `Text`：日报原始稿一天几万字，`Text` 会一次性布局全部
/// 字形，滚动与窗口缩放都明显掉帧；`NSTextView` 自带排版虚拟化，十万字也流畅。
/// 选中复制、查找、无障碍朗读这些能力同样是系统自带的，不必自己实现。
///
/// 日志与日报共用这一个组件，保证两边阅读体验一致。
struct AppTextView: NSViewRepresentable {

    let text: String
    var fontSize: CGFloat = 12.5
    var monospaced: Bool = true
    /// 日志场景需要始终跟着最新一行；日报则应该从头读起。
    var stickToBottom = false

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.drawsBackground = false
        scroll.autohidesScrollers = true

        let tv = NSTextView()
        tv.isEditable = false
        tv.isSelectable = true
        tv.isRichText = false
        tv.drawsBackground = false
        tv.textColor = .labelColor
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.textContainerInset = NSSize(width: 10, height: 10)
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        // 按容器宽度换行，而不是拉出一条超长的单行
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)

        scroll.documentView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = scroll.documentView as? NSTextView else { return }
        let font: NSFont = monospaced
            ? NSFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
            : NSFont.systemFont(ofSize: fontSize)
        if tv.font != font { tv.font = font }

        guard tv.string != text else { return }
        tv.string = text
        if stickToBottom {
            tv.scrollToEndOfDocument(nil)
        } else {
            tv.scroll(.zero)
        }
    }
}
