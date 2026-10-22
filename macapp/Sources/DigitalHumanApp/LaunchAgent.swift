import Foundation

/// 开机自启管理（LaunchAgent）。
///
/// 站外分发不受 Mac App Store 2.4.5(iii) 的约束，所以这里可以直接写
/// `~/Library/LaunchAgents/*.plist`，不必走 SMAppService。
/// 但仍然**默认关闭** —— 后台常驻应该由用户决定，这是平台礼仪，不是合规要求。
enum LaunchAgent {

    static var fileURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(Paths.launchAgentLabel).plist")
    }

    static var isEnabled: Bool {
        FileManager.default.fileExists(atPath: fileURL.path)
    }

    static func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try plistXML().write(to: fileURL, atomically: true, encoding: .utf8)
        } else {
            // 先 unload 再删，否则 launchd 会一直重试加载一个已经不存在的 plist
            unload()
            try? FileManager.default.removeItem(at: fileURL)
        }
    }

    private static func unload() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["unload", fileURL.path]
        try? p.run()
        p.waitUntilExit()
    }

    private static func plistXML() -> String {
        // 必须用运行时的实际路径写死，launchd 不解析相对路径也不跟随别名
        let exe = Bundle.main.executablePath ?? ""
        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>\(Paths.launchAgentLabel)</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(exe)</string>
                <string>--autostart</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <false/>
        </dict>
        </plist>
        """
    }
}
