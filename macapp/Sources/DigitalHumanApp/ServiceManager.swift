import AppKit
import Foundation

@MainActor
final class ServiceManager: ObservableObject {

    enum Status: Equatable {
        case stopped
        case starting
        case running
        case failed(String)

        var label: String {
            switch self {
            case .stopped:  "已停止"
            case .starting: "启动中"
            case .running:  "运行中"
            case .failed:   "异常"
            }
        }

        var symbol: String {
            switch self {
            case .stopped:  "circle"
            case .starting: "circle.dotted"
            case .running:  "checkmark.circle.fill"
            case .failed:   "exclamationmark.triangle.fill"
            }
        }
    }

    static let shared = ServiceManager()

    /// 退出兜底用的 pid 副本。
    /// `applicationWillTerminate` 里不能再走 async/await 去拿 MainActor 上的 Process，
    /// 所以留一份标量在这里，由 AppDelegate 同步 kill，避免留下孤儿 uvicorn 进程。
    nonisolated(unsafe) static var runningPID: pid_t?

    nonisolated(unsafe) static var llmPID: pid_t?

    /// 同步终止服务进程，仅供 app 即将退出时调用。
    nonisolated static func killOrphan() {
        for pid in [runningPID, llmPID].compactMap({ $0 }) {
            kill(pid, SIGTERM)
        }
        runningPID = nil
        llmPID = nil
    }

    @Published private(set) var status: Status = .stopped
    @Published private(set) var detail: String = ""
    /// 本地文字模型状态。API 起来了但 LLM 没起来 = 干净稿与总结全空白，
    /// 这是历史上最难查的一类故障，所以单独暴露给界面。
    @Published private(set) var llmStatus: String = ""

    private var process: Process?
    private var llmProcess: Process?
    private var llmLogHandle: FileHandle?
    private var pollTask: Task<Void, Never>?
    private let fm = FileManager.default
    private lazy var session = URLSession(
        configuration: .ephemeral,
        delegate: LoopbackTrustDelegate(),
        delegateQueue: nil
    )

    private init() {}

    // MARK: - 路径

    /// .app 内的服务端代码，随 bundle 分发（只读）
    var serverRoot: URL {
        Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/server")
    }

    /// 启动器脚本。所有路径由脚本自己相对解析（PYTHONPATH / PYTHONHOME），
    /// 所以 .app 被拖到任何位置都能用，改路径逻辑也不用重编 Swift。
    var launcherURL: URL {
        serverRoot.appendingPathComponent("run-app.sh")
    }

    /// 用户数据目录。
    /// 放在 Application Support 而不是 bundle 内：bundle 会被更新整包替换，
    /// 数据放进去等于每次升级丢库。
    var dataRoot: URL {
        fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(Paths.appSupportFolder)
    }

    /// 日志目录。env.local 里的 DH_LOGS_DIR 优先——设置页（高级组）可以改它，
    /// 写死 dataRoot/logs 的话改完 Python 写新目录、App 读旧目录，
    /// 日志页会静默空白而没有任何报错。
    var logsRoot: URL {
        if let raw = envFileValues()["DH_LOGS_DIR"], !raw.isEmpty {
            return URL(fileURLWithPath: expandEnvVars(raw, env: ProcessInfo.processInfo.environment))
        }
        return dataRoot.appendingPathComponent("logs")
    }

    /// 配置文件。落在数据目录而不是 bundle 内：bundle 是签名过的只读区，
    /// 留在那儿用户既改不了，升级时还会被整包覆盖。
    var envFileURL: URL { dataRoot.appendingPathComponent("env.local") }

    /// env.local 的解析结果。
    ///
    /// 与 `config.py` 的 `_load_env_file` 行为对齐：跳过注释、去掉 `export ` 前缀、
    /// 去掉成对引号。不做 `$VAR` 展开——Swift 侧只用得到 DH_TOKEN 与 DH_LLM_*
    /// 这几个值，需要展开的只有模型路径，那里单独调 `expandEnvVars`。
    ///
    /// 解析只写一份：原来 `apiToken` 与 `resolveLLMServedID` 各写了一遍，
    /// 两处对引号的处理一旦漂移，表现就是「Token 读得到、模型路径读不到」。
    private func envFileValues() -> [String: String] {
        guard let text = try? String(contentsOf: envFileURL, encoding: .utf8) else {
            return [:]
        }
        var out: [String: String] = [:]
        for rawLine in text.split(separator: "\n") {
            var s = rawLine.trimmingCharacters(in: .whitespaces)
            if s.isEmpty || s.hasPrefix("#") { continue }
            if s.hasPrefix("export ") {
                s = String(s.dropFirst("export ".count)).trimmingCharacters(in: .whitespaces)
            }
            guard let eq = s.firstIndex(of: "=") else { continue }
            let key = String(s[s.startIndex..<eq]).trimmingCharacters(in: .whitespaces)
            guard !key.isEmpty else { continue }
            var v = String(s[s.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if v.count >= 2, v.first == v.last, v.first == "\"" || v.first == "'" {
                v = String(v.dropFirst().dropLast())
            }
            out[key] = v
        }
        return out
    }

    /// 手机端配对 Token，取自 env.local 的 DH_TOKEN。
    ///
    /// 这是主窗口读 `/api/console/*` 的凭证——与浏览器打开工作台用的是同一套，
    /// 不额外引入 Agent token：后者权限更强，没必要暴露给界面层。
    var apiToken: String { envFileValues()["DH_TOKEN"] ?? "" }

    /// 外网中转提示（手机自适应连接的第二跳），概览「手机端配对」卡展示用。
    var relayHint: String { envFileValues()["DH_RELAY_HINT"] ?? "" }

    /// 是否走云端 API。
    ///
    /// 判断逻辑必须与服务端 `config.llm_is_cloud()` **逐字一致**：
    /// provider 显式写死时以它为准，否则只有回环地址算本地。
    /// 两边一旦不一致，就会出现「App 拉起了 mlx-lm、服务端却去调云端」，
    /// 结果是白白占着几个 GB 内存，而用户完全看不出来。
    var isCloudLLM: Bool {
        let env = envFileValues()
        let provider = (env["DH_LLM_PROVIDER"] ?? "").lowercased()
        if ["cloud", "remote", "api", "online"].contains(provider) { return true }
        if ["local", "mlx", "offline"].contains(provider) { return false }
        let host = (env["DH_LLM_BASE_URL"] ?? "").lowercased()
        if host.isEmpty { return false }
        return !host.contains("127.0.0.1")
            && !host.contains("localhost")
            && !host.contains("0.0.0.0")
            && !host.contains("[::1]")
    }

    /// 语音模型目录
    var modelRoot: URL { dataRoot.appendingPathComponent("models") }

    /// 文字模型目录
    var llmModelRoot: URL { dataRoot.appendingPathComponent("llm-models") }

    /// 首次构建 env 时攒下的完整环境，供后续拉起 LLM 进程复用，
    /// 避免两个子进程拿到不一致的模型路径。
    nonisolated(unsafe) private static var processEnv: [String: String] = [:]

    // MARK: - 生命周期

    /// 上一个实例被 SIGKILL（强制退出、系统杀进程）时，applicationWillTerminate
    /// 不会跑，uvicorn / mlx-lm 会变成持有端口的孤儿：新实例拉起的进程绑不上端口，
    /// 表现为菜单「服务已退出」但健康检查却通着——应答的是孤儿。实测踩坑（2026-09-07）。
    /// start 前先清场：此时我们自己还没拉起任何子进程，匹配到的一定是孤儿。
    private func killOrphanChildren() {
        for pattern in ["Contents/Resources/server/run.py", "mlx_lm.server"] {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
            p.arguments = ["-f", pattern]
            try? p.run()
            // 必须等 pkill 扫描结束：它是异步的，若不等，下面的启动可能与
            // pkill 的进程扫描竞速——刚拉起的自家子进程被当成孤儿杀掉
            // （实测：uvicorn 没写一行日志就没了、mlx 起来几秒后暴毙）。
            p.waitUntilExit()
        }
    }

    /// 端口是否空闲（连不上 = 空闲）。SIGTERM 后 uvicorn 优雅退出要一两秒，
    /// 轮询等它把端口让出来，超时就照常继续——绑不上时日志里会留下明确错误。
    private func portFree(_ port: UInt16) -> Bool {
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result != 0
    }

    private func waitForPortsFree() {
        for _ in 0..<12 {
            if portFree(UInt16(Paths.apiPort)) && portFree(UInt16(Paths.llmPort)) { return }
            usleep(500_000)
        }
    }

    func start() {
        switch status {
        case .stopped, .failed: break
        case .starting, .running: return
        }
        guard fm.isExecutableFile(atPath: launcherURL.path) else {
            status = .failed("启动器缺失")
            detail = "找不到 \(launcherURL.path)，安装包可能已损坏。"
            return
        }

        status = .starting
        detail = ""
        killOrphanChildren()
        waitForPortsFree()
        bootstrapDataDirs()

        let proc = Process()
        proc.executableURL = launcherURL
        proc.arguments = []
        proc.currentDirectoryURL = serverRoot

        // run.py 内部已经清过一次代理，这里再清是因为 Process 会继承宿主环境：
        // 用户在公司网络下启动菜单栏 app 时，代理变量会透传给 uvicorn 与 mlx-lm。
        var env = ProcessInfo.processInfo.environment
        env["DH_DATA_DIR"] = dataRoot.appendingPathComponent("data").path
        // 配置文件必须落在可写的数据目录：代码在签名过的 bundle 内是只读的，
        // 把 env.local 留在那儿用户改不了、升级还会被整包覆盖。
        // config.py 默认只在代码目录找，靠这个变量指过去。
        env["DH_ENV_FILE"] = dataRoot.appendingPathComponent("env.local").path
        env["DH_INDUSTRY_DIR"] = serverRoot.appendingPathComponent("industries").path
        // 主窗口内嵌工作台用的明文回环口（config.DESKTOP_HTTP_ENABLED 默认关）。
        //
        // 为什么默认关、只能由壳显式打开：这个口唯一的用途就是给本机 WebView 用 ——
        // 浏览器、手机、局域网 agent 一律走 8766 的自签 HTTPS。多开一个明文监听口
        // 没有收益，所以命令行启动时不该带上它。
        // ⚠️ 也正因如此，这两个变量**不能**写进 bootstrapDataDirs 的 env.local 模板：
        // 用户从终端跑 run.py 时会跟着读到，等于把默认关的开关悄悄变成默认开。
        env["DH_DESKTOP_HTTP"] = "1"
        env["DH_DESKTOP_HTTP_PORT"] = "\(Paths.desktopHTTPPort)"
        // config.py 里模型路径默认相对代码目录，而代码在只读的 bundle 内、
        // 模型却在数据目录。这两个变量是 env.local 模板展开 $DH_MODEL_DIR 的
        // 前提，缺了它们模型会全部找不到。
        env["DH_MODEL_DIR"] = modelRoot.path
        env["DH_LLM_MODEL_DIR"] = llmModelRoot.path
        // mlx-lm 的 /v1/models 返回模型目录绝对路径，请求体 model 字段必须与之
        // 逐字一致，差一个字符就是 404，而表现是「干净稿与总结全空白」——日志里只有
        // 一行 404，极难定位。这里把「实际拿去启动 LLM 的那个目录」直接注入环境，
        // 从根上保证本进程拉起的 LLM 与 API 请求的 id 不可能对不上。
        if let servedID = resolveLLMServedID() {
            env["DH_LLM_SERVED_ID"] = servedID
        }
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"] {
            env.removeValue(forKey: k)
        }
        proc.environment = env
        Self.processEnv = env

        // API 进程的日志由 Python 端的 dictConfig + RotatingFileHandler 全权负责：
        // 单文件 20 MB 阈值，归档 5 份 = 100 MB 硬上限。
        // 不再由 Swift 把 stdout 重定向到 logs/api.log——那会与 Python logging
        // 双写到同一文件（或两个不同位置），且 Swift 持有 fd 后无法 rotate。
        // 这里把 stdout/stderr 接到 /dev/null，避免 print 调试泄漏到控制台。
        let devnull = FileHandle(forWritingAtPath: "/dev/null")
        proc.standardOutput = devnull
        proc.standardError = devnull

        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in self?.handleExit() }
        }

        do {
            try proc.run()
        } catch {
            status = .failed("启动失败")
            detail = error.localizedDescription
            return
        }

        process = proc
        Self.runningPID = proc.processIdentifier
        AppDelegate.setAutoStart(true)
        startPolling()
        // 不再无条件拉 mlx-lm：云端模式下它既用不上，又要白占约 3 GB 内存
        // （还得先下 2.9 GB 模型）。这正是「8GB 内存的 Mac 跑不动」的根因。
        reconcileLLM()
    }

    // MARK: - 本地文字模型（mlx-lm）

    /// 按当前配置决定本地文字模型进程该不该在。
    ///
    /// 用户在设置里切「本地 / 云端」之后调用：切到云端要立刻把已加载的模型进程
    /// 放掉，切回本地要补拉起来。少了这一步，切换就只是改了个配置，
    /// 内存一点没省，用户会以为功能没生效。
    func reconcileLLM() {
        if isCloudLLM {
            if llmProcess != nil { stopLLM() }
            llmStatus = "云端接口（未加载本地模型）"
            return
        }
        // 处理窗口（2026-09-27）：与 server/app/config.py 的 in_agg_window() 同语义。
        // 窗口外壳不再拉 mlx（3.6GB 常驻太重），由服务端 llm.backend_window_manager
        // 每 60s 对齐——窗口内它会自己拉起。这里只负责「启动期别白拉一次」。
        if !aggWindowActive() {
            if llmProcess?.isRunning != true {
                llmStatus = "处理窗口外，模型待窗口自动启动"
            }
            return
        }
        if llmProcess?.isRunning != true { startLLM() }
    }

    /// 是否处于音频处理窗口内。读 env.local 的 DH_AGG_WINDOWS / DH_AGG_SPAN_MIN，
    /// 与 config.py 保持同一套判定：空 = 实时模式（恒在窗口内）。
    /// 设置页热改这两个值会写回 env.local，本函数每次重读，无需重启。
    private func aggWindowActive() -> Bool {
        // 与 config.py 对齐：键**未配置**时用服务端默认 "13:00,17:45"；
        // **显式留空**才是实时模式（恒在窗口内）。两者必须区分——否则
        // 新装机器 env.local 里没有这个键，壳会在窗口外白拉 3.6GB 的 mlx。
        let envVals = envFileValues()
        let raw: String
        if let v = envVals["DH_AGG_WINDOWS"] {
            raw = v.trimmingCharacters(in: .whitespaces)
        } else {
            raw = "13:00,17:45"
        }
        if raw.isEmpty { return true }
        let span = Int(envVals["DH_AGG_SPAN_MIN"] ?? "") ?? 90
        let spanMin = max(1, span)
        let cal = Calendar.current
        let nowMin = cal.component(.hour, from: Date()) * 60 + cal.component(.minute, from: Date())
        for token in raw.replacingOccurrences(of: "，", with: ",").split(separator: ",") {
            let parts = token.trimmingCharacters(in: .whitespaces).split(separator: ":")
            guard parts.count >= 2, let hh = Int(parts[0]), let mm = Int(parts[1]),
                  hh >= 0, hh < 24, mm >= 0, mm < 60 else { continue }
            let w = hh * 60 + mm
            if w <= nowMin && nowMin < w + spanMin { return true }
        }
        return false
    }

    private func stopLLM() {
        if let llm = llmProcess, llm.isRunning { llm.terminate() }
        llmProcess = nil
        Self.llmPID = nil
        try? llmLogHandle?.synchronize()
        llmLogHandle = nil
    }

    /// 拉起 mlx-lm server。
    ///
    /// 不能省：干净稿去口气、分块、总结全靠它。历史上「服务明明在跑但总结空白」
    /// 就是 LLM 进程没起来，而 API 健康检查照样 200，极难定位。
    func startLLM() {
        guard llmProcess?.isRunning != true else { return }
        guard !isCloudLLM else {
            llmStatus = "云端接口（未加载本地模型）"
            return
        }

        guard let model = localLLMModelDir() else {
            llmStatus = "未找到本地文字模型"
            return
        }
        let launcher = serverRoot.appendingPathComponent("run-llm.sh")
        guard fm.isExecutableFile(atPath: launcher.path) else {
            llmStatus = "启动器缺失"
            return
        }

        let proc = Process()
        proc.executableURL = launcher
        proc.arguments = [model, "\(Paths.llmPort)"]
        proc.currentDirectoryURL = serverRoot
        proc.environment = Self.processEnv.isEmpty
            ? ProcessInfo.processInfo.environment
            : Self.processEnv

        let log = logsRoot.appendingPathComponent("llm.log")
        fm.createFile(atPath: log.path, contents: nil)
        llmLogHandle = FileHandle(forWritingAtPath: log.path)
        llmLogHandle?.seekToEndOfFile()
        proc.standardOutput = llmLogHandle
        proc.standardError = llmLogHandle

        // mlx-lm 偶发崩溃（显存压力、模型文件损坏）。自动重拉一次，
        // 避免用户发现总结空白才发现后端早挂了。
        proc.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.llmProcess = nil
                Self.llmPID = nil
                self?.llmStatus = "文字模型已停止"
            }
        }

        do {
            try proc.run()
        } catch {
            llmStatus = "文字模型启动失败：\(error.localizedDescription)"
            return
        }
        llmProcess = proc
        Self.llmPID = proc.processIdentifier
        llmStatus = "文字模型加载中"
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(8))
            await self.probeLLM()
        }
    }

    /// 解析本地文字模型目录。
    ///
    /// 优先级：env.local 里显式写的 DH_LLM_SERVED_ID（用户换模型时改这里）
    /// > 扫描 llm-models 下第一个含 config.json 的目录。
    /// 两条路都必须落到真实存在的目录上，否则返回 nil，让调用方显示
    /// 「未找到本地文字模型」——好过拿一个空路径去启动，然后静默失败。
    private func resolveLLMServedID() -> String? {
        var base = ProcessInfo.processInfo.environment
        base["DH_LLM_MODEL_DIR"] = llmModelRoot.path

        if let raw = envFileValues()["DH_LLM_SERVED_ID"] {
            let v = expandEnvVars(raw, env: base)
            if fm.fileExists(atPath: v) { return v }
        }
        return localLLMModelDir()
    }

    /// `$VAR` / `${VAR}` 展开，与 config.py 里 `os.path.expandvars` 的行为对齐。
    /// env.local 的路径写法依赖它（DH_LLM_MODEL_DIR/Qwen3.5-4B-4bit），
    /// 不展开就会把字面量当路径，模型必然找不到。
    private func expandEnvVars(_ s: String, env: [String: String]) -> String {
        var out = ""
        var i = s.startIndex
        while i < s.endIndex {
            if s[i] == "$" {
                let after = s.index(after: i)
                if after < s.endIndex, s[after] == "{",
                   let close = s[after...].firstIndex(of: "}") {
                    let name = String(s[s.index(after: after)..<close])
                    out += env[name] ?? ""
                    i = s.index(after: close)
                    continue
                }
                var j = after
                while j < s.endIndex, s[j].isLetter || s[j].isNumber || s[j] == "_" {
                    j = s.index(after: j)
                }
                if j > after {
                    out += env[String(s[after..<j])] ?? ""
                    i = j
                    continue
                }
            }
            out.append(s[i])
            i = s.index(after: i)
        }
        return out
    }

    /// 在数据目录里找本地文字模型：含 config.json 的子目录即为模型目录
    private func localLLMModelDir() -> String? {
        guard let items = try? fm.contentsOfDirectory(
            at: llmModelRoot, includingPropertiesForKeys: nil) else { return nil }
        for item in items {
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: item.path, isDirectory: &isDir),
                  isDir.boolValue,
                  fm.fileExists(atPath: item.appendingPathComponent("config.json").path)
            else { continue }
            return item.path
        }
        return nil
    }

    private func probeLLM() async {
        guard let url = URL(string: "http://127.0.0.1:\(Paths.llmPort)/v1/models") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        do {
            let (_, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                llmStatus = "文字模型已就绪"
            }
        } catch {
            if llmProcess?.isRunning != true { llmStatus = "文字模型未启动" }
        }
    }

    func stop() {
        // 显式停止要被记住：下次启动 app / 重新登录时保持停止，
        // 否则「用户刚停掉、系统重启又自己跑起来」会很困惑。
        AppDelegate.setAutoStart(false)
        pollTask?.cancel()
        pollTask = nil

        // stopLLM 里已经同步过日志句柄，这里只清状态文案
        stopLLM()
        llmStatus = ""

        guard let proc = process, proc.isRunning else {
            status = .stopped
            return
        }
        // SIGTERM 让 uvicorn 走优雅关闭；只有它没在 5 秒内退出才升级到 SIGKILL，
        // 否则正在写的 SQLite 可能留下脏页。
        proc.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) { [weak proc] in
            if proc?.isRunning == true { proc?.interrupt() }
        }
        process = nil
        Self.runningPID = nil
        status = .stopped
    }

    func restart() {
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.start() }
    }

    private func handleExit() {
        pollTask?.cancel()
        pollTask = nil
        process = nil
        Self.runningPID = nil
        switch status {
        case .running, .starting: break
        case .stopped, .failed: return
        }
        status = .failed("服务已退出")
        detail = "详情见 \(logsRoot.path)/api.log"
    }

    // MARK: - 健康检查

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            // 冷启动要加载 SenseVoice，给第一次探测留足时间
            try? await Task.sleep(for: .seconds(2))
            // 自适应轮询：3s 太过频繁——一天产生 28k 条「200」健康检查，占日志 80%+，
            // 把真问题淹没在噪音里；UI 对「服务刚挂」的感知也不需要秒级（前端启停动作用）
            // 拉到 15s：探测到挂掉 → 菜单栏红点最迟 15s 显示 → 用户察觉时间也只多了 12s，
            // 但日志量降到 1/5（约 6k/天），保留几天的诊断上下文。失败时再临时回退到 3s
            // 直到确认恢复（轮询退避见 probe()），既不丢响应性，也不打日志。
            var interval: TimeInterval = 15
            while !Task.isCancelled {
                let ok = (await self?.probe()) ?? false
                interval = ok ? 15 : 3
                try? await Task.sleep(for: .seconds(interval))
            }
        }
    }

    private func probe() async -> Bool {
        guard let url = URL(string: "https://127.0.0.1:\(Paths.apiPort)/api/agent/health") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
                if case .running = status { status = .starting }
                return false
            }
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ok = json["ok"] as? Bool, ok {
                status = .running
                return true
            }
            return false
        } catch {
            // 进程还在但接口没起来 = 启动中，不算失败
            switch status {
            case .stopped: return false
            case .starting, .running, .failed:
                if process?.isRunning == true { status = .starting }
            }
            return false
        }
    }

    // MARK: - 辅助

    /// 首次运行建好目录骨架；env.local 只在不存在时写一份最小默认配置，
    /// 绝不覆盖用户手改过的配置。
    private func bootstrapDataDirs() {
        for dir in ["data", "logs", "models", "llm-models"] {
            let url = dataRoot.appendingPathComponent(dir)
            try? fm.createDirectory(at: url, withIntermediateDirectories: true)
        }
        let envFile = envFileURL
        guard !fm.fileExists(atPath: envFile.path) else { return }
        let tmpl = """
        # 数字人服务配置
        # 修改后需要在菜单栏点「重启」生效。此文件不入 git、不随应用更新被覆盖。

        # ---- 目录（换位置改这里，其余路径都基于它们展开）----
        DH_DATA_DIR=\(dataRoot.path)/data
        DH_MODEL_DIR=\(dataRoot.path)/models
        DH_LLM_MODEL_DIR=\(dataRoot.path)/llm-models

        # ---- 模型（换模型改这里）----
        DH_SENSEVOICE_DIR=$DH_MODEL_DIR/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
        DH_FUNASR_NANO_DIR=$DH_MODEL_DIR/FunASR-nano-onnx
        DH_SPEAKER_MODEL=$DH_MODEL_DIR/speaker_ecapa.onnx
        DH_AUDIO_TAGGING_MODEL=$DH_MODEL_DIR/audio_tagging/model.onnx
        DH_AUDIO_TAGGING_LABELS=$DH_MODEL_DIR/audio_tagging/class_labels_indices.csv

        # ---- 接入 ----
        # 手机端配对的 Token，改了手机端也要改
        DH_TOKEN=digital-human
        DH_PORT=\(Paths.apiPort)
        # 本地文字模型（mlx-lm）端口
        DH_LLM_PORT=\(Paths.llmPort)
        DH_LLM_BASE_URL=http://127.0.0.1:\(Paths.llmPort)/v1
        DH_LLM_MODEL=qwen3.5-4b
        # mlx-lm 的 /v1/models 返回模型目录绝对路径，这里必须与之逐字一致，
        # 否则请求会 404。换模型后一定要同步改这一行。
        DH_LLM_SERVED_ID=$DH_LLM_MODEL_DIR/Qwen3.5-4B-4bit

        # ---- 转写 ----
        DH_ASR_PROMPT=以下是中文多人对话，口语化，有重复和语气词。
        # 热词只写真专名（人名 / 项目名 / 自家产品名）。
        # 2026-09-05 实测：塞 101 个行业通用词会让日终轮从 8.0x 掉到 2.9x 实时，
        # 还出现整段复读「可以了」。行业通用词的纠偏交给词表做，别放这里。
        DH_ASR_HOTWORDS=
        """
        try? tmpl.write(to: envFile, atomically: true, encoding: .utf8)
    }

    /// 主窗口内嵌的工作台地址：明文回环，只绑 127.0.0.1、只给本机 WebView 用。
    ///
    /// 为什么不内嵌 8766 那个自签 HTTPS：WKWebView 遇到不受信证书只会「导航失败」，
    /// 没有浏览器里那个「继续访问」可点，结果就是一整片白 —— 与 Windows 壳用
    /// WebView2 时是同一个原因（见 desktop/src-tauri 的双端口说明）。
    var consoleWindowURL: URL? {
        URL(string: "http://127.0.0.1:\(Paths.desktopHTTPPort)/console")
    }

    func openConsole() {
        guard let url = URL(string: "https://127.0.0.1:\(Paths.apiPort)/console") else { return }
        NSWorkspace.shared.open(url)
    }

    func revealDataFolder() {
        NSWorkspace.shared.activateFileViewerSelecting([dataRoot])
    }
}
