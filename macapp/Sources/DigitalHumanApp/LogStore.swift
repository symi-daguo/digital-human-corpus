import Foundation

/// 日志文件读取。
///
/// 只做「从尾部往前读一段」这一件事：api.log 上限 20 MB，整个读进内存既慢
/// 又会在 SwiftUI 里反复触发大字符串 diff。界面只需要最后几百行。
enum LogStore {

    /// 读文件末尾最多 `maxBytes` 字节，丢掉可能因截断产生的半行。
    static func tail(_ url: URL, maxBytes: Int = 192 * 1024) -> String {
        guard let fh = try? FileHandle(forReadingFrom: url) else { return "" }
        defer { try? fh.close() }
        // `seekToEndOfFile()` 不抛错（throwing 的版本叫 `seekToEnd()`），
        // 所以这里不能写 `try?` —— 编译器会报「没有可抛错调用」
        let end = fh.seekToEndOfFile()
        guard end > 0 else { return "" }
        let take = min(UInt64(maxBytes), end)
        fh.seek(toFileOffset: end - take)
        let data = fh.readDataToEndOfFile()
        guard var text = String(data: data, encoding: .utf8) else { return "" }
        // 从文件中途开始读时首行多半是残的，直接丢弃，避免第一行显示半条日志
        if take < end, let nl = text.firstIndex(of: "\n") {
            text = String(text[text.index(after: nl)...])
        }
        return text
    }

    /// 文件字节数的可读形式
    static func sizeDescription(_ url: URL) -> String {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let n = attrs[.size] as? NSNumber
        else { return "—" }
        return ByteCountFormatter.string(fromByteCount: n.int64Value, countStyle: .file)
    }
}
