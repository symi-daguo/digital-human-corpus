import Foundation

enum Paths {
    /// ~/Library/Application Support/ 下的子目录名
    static let appSupportFolder = "DigitalHuman"

    /// LaunchAgent 的 Label，也是 plist 文件名
    static let launchAgentLabel = "com.symi.digitalhuman"

    /// env.local 的位置——与 ServiceManager.envFileURL 同一份。
    ///
    /// 为什么端口要从配置文件读而不是写死：设置页允许改 DH_PORT / DH_LLM_PORT，
    /// 写死的后果是用户改完端口后整个 App 连不上服务，而报错只会显示
    /// 「服务未运行」，完全看不出是端口对不上（2026-09-07 审查发现的缺口）。
    private static var envFile: URL {
        FileManager.default.urls(for: .applicationSupportDirectory,
                                 in: .userDomainMask)[0]
            .appendingPathComponent(appSupportFolder)
            .appendingPathComponent("env.local")
    }

    /// 读一个 env.local 里的数值型配置，缺省回落。
    ///
    /// ponytail: 每次调用都读文件。env.local 只有几 KB，且只在拼 URL 时读，
    /// 不是热路径；真成为瓶颈再加缓存 + 保存时失效，现在缓存是多余的复杂度。
    private static func envInt(_ key: String, fallback: Int) -> Int {
        guard let text = try? String(contentsOf: envFile, encoding: .utf8)
        else { return fallback }
        for line in text.split(separator: "\n") {
            var s = line.trimmingCharacters(in: .whitespaces)
            if s.hasPrefix("export ") { s = String(s.dropFirst(7)) }
            guard let eq = s.firstIndex(of: "=") else { continue }
            if String(s[s.startIndex..<eq]).trimmingCharacters(in: .whitespaces) != key {
                continue
            }
            var v = String(s[s.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if v.count >= 2, v.first == v.last, v.first == "\"" || v.first == "'" {
                v = String(v.dropFirst().dropLast())
            }
            return Int(v) ?? fallback
        }
        return fallback
    }

    /// 服务端监听端口，取 env.local 的 DH_PORT（缺省 8766）
    static var apiPort: Int { envInt("DH_PORT", fallback: 8766) }

    /// 主窗口内嵌工作台用的明文回环端口，取 env.local 的 DH_DESKTOP_HTTP_PORT（缺省 8767）。
    ///
    /// 与 8766 分开的理由是证书而不是端口占用：WKWebView 遇到不受信的自签证书
    /// 只会「导航失败」，没有浏览器那个「继续访问」可点，所以内嵌窗口只能走明文回环。
    /// 让用户能在 env.local 里改它，是因为 8767 也可能被别的进程先占上。
    static var desktopHTTPPort: Int { envInt("DH_DESKTOP_HTTP_PORT", fallback: 8767) }

    /// 本地文字模型（mlx-lm）端口，取 env.local 的 DH_LLM_PORT（缺省 1235）
    static var llmPort: Int { envInt("DH_LLM_PORT", fallback: 1235) }
}
