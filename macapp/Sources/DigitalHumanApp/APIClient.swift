import Foundation

/// 只信任回环地址上的自签证书。
///
/// TLS 证书是服务端首次启动时自己生成的（`config.ensure_tls`），URLSession 默认会拒绝。
/// 这里放松校验的范围被严格限定为 127.0.0.1 —— 对任何其他 host 依旧走系统默认校验，
/// 避免「为了本地能连上」而把整个 app 的 TLS 校验关掉。
///
/// 主窗口与菜单栏共用这一个实现，避免两处各写一份导致校验范围悄悄漂移。
final class LoopbackTrustDelegate: NSObject, URLSessionDelegate {
    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.host == "127.0.0.1",
              let trust = challenge.protectionSpace.serverTrust
        else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

// MARK: - 响应模型

/// `/api/console/state` 的响应。字段全部按服务端实际命名做 snake_case → camelCase 转换。
///
/// 只声明界面真正要用的字段：Codable 天然忽略未知键，服务端后续加字段不会让解码失败，
/// 也就不需要为此同步发版。
struct ConsoleState: Decodable, Equatable {

    struct ServiceInfo: Decodable, Equatable {
        let version: String
        let port: Int
        let lanIps: [String]
        let tokenEnabled: Bool
    }

    struct ModelInfo: Decodable, Equatable {
        let llmAvailable: Bool
        let llmModel: String
        let asrIncremental: String
        let asrFinal: String
        let asrFinalRewrite: Bool
        let hotwords: [String]
        let hotwordMax: Int
    }

    struct IndustryInfo: Decodable, Equatable {
        struct Item: Decodable, Equatable, Identifiable {
            let key: String
            let label: String
            let terms: Int
            var id: String { key }
        }
        let current: String
        let label: String
        let terms: Int
        let pinnedByEnv: Bool
        let available: [Item]
        /// 同音错字映射（错写 → 正写）。切换行业后它跟着变，
        /// 行业页的映射表直接渲染这一份，与服务端 `llm.apply_industry_fixes` 同源。
        let fixes: [String: String]
    }

    /// 具名声纹角色（用户在手机端注册，Mac 端这里只能看与删）。
    struct Speaker: Decodable, Equatable, Identifiable {
        let name: String
        /// unix 秒（SQLite REAL）
        let createdAt: Double
        let updatedAt: Double
        var id: String { name }
    }

    /// 声纹注册样本的口径：要录多久（秒）+ 要朗读的固定文本。
    ///
    /// 由服务端下发（`voiceprint.ENROLL_SECONDS/ENROLL_SCRIPT`），不在客户端
    /// 各写一份 —— 手机端提示读 A、电脑端提示读 B 会让两边的锚点不可比。
    struct Enroll: Decodable, Equatable {
        let seconds: Int
        let script: String
    }

    struct Day: Decodable, Equatable, Identifiable {
        let date: String
        let ready: Bool
        let speechSegments: Int
        var id: String { date }
    }

    let service: ServiceInfo
    let models: ModelInfo
    let industry: IndustryInfo
    let days: [Day]
    let speakers: [Speaker]
    /// 注册样本口径。可选：老版本服务端没有这个字段，
    /// 此时角色页退回一句概述，不显示朗读正文。
    let enroll: Enroll?
    /// 当前请求的租户空间 id（服务端回显，用于确认切换生效）
    let tenant: String
    /// 全部租户空间概览（工作台空间切换与管理用，2026-09-09 多租户）。
    /// var：空间管理页创建/注销后局部刷新列表，不必整页 refresh。
    var tenants: [TenantInfo]

    /// 最近一个已完成（ready）的日期。侧栏与日报页默认落在这里，
    /// 避免用户每次打开都要先选日期。
    var latestReadyDay: Day? { days.last { $0.ready } ?? days.last }
}

/// 租户空间概览（`GET /api/console/tenants`、`/api/console/state` 的 tenants 字段）。
struct TenantInfo: Decodable, Equatable, Identifiable {
    let id: String
    let name: String
    let dbBytes: Int
    let textFiles: Int
    let corpusFiles: Int
    let markers: Int
}

/// `/api/console/report/{date}` 的响应：同一天的三版日报。
struct SummaryCard: Decodable, Equatable {
    let title: String
    let body: String
}

struct DayReport: Decodable, Equatable {
    let date: String
    let ready: Bool
    let raw: String
    let clean: String
    let summary: String
    /// 服务端 llm.parse_summary_cards 的产物（App 直接渲染，不自己猜格式）
    var summaryCards: [SummaryCard]?

    var cards: [SummaryCard] { summaryCards ?? [] }
}

/// `POST /api/done/{date}` 的响应。`pipeline_started=false` 表示
/// 同一天的管线已经在跑（服务端做了去重），不是错误。
struct DoneResult: Decodable {
    let ok: Bool
    let pipelineStarted: Bool
    let final: Bool

    enum CodingKeys: String, CodingKey {
        case ok, final, pipelineStarted
    }
}

/// `GET /api/console/industry/{name}/terms` 的响应。
struct IndustryTerms: Decodable, Equatable {
    let industry: String
    let label: String
    /// 词表原文：首行 `# label: 中文名`，之后每行一个术语或 `错写=正写`
    let text: String
    let userEditable: Bool
    let current: Bool

    enum CodingKeys: String, CodingKey {
        case industry, label, text, current, userEditable
    }
}

/// `PUT /api/console/industry/{name}/terms` 的响应。
struct IndustryTermsSaveResult: Decodable {
    let ok: Bool
    let industry: String
    let terms: Int
    let fixes: Int
    let path: String
}

// MARK: - 客户端

enum APIError: LocalizedError {
    case offline
    case unauthorized
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .offline:
            "服务未运行，无法读取数据"
        case .unauthorized:
            "Token 校验失败，请检查 env.local 里的 DH_TOKEN"
        case .http(let code):
            "服务返回异常状态码 \(code)"
        }
    }
}

/// 工作台数据接口的客户端。
///
/// 只走回环地址（127.0.0.1），与菜单栏进程同一台机器；鉴权沿用手机端的
/// Bearer token，和浏览器打开 `/console` 用的是同一套凭证，不额外引入 Agent token
/// ——后者权限更强，没必要暴露给界面层。
@MainActor
final class APIClient: ObservableObject {

    static let shared = APIClient()

    @Published private(set) var state: ConsoleState?
    @Published private(set) var lastError: String?
    @Published private(set) var isLoading = false

    /// 当前租户空间（多租户，2026-09-09）。切换后由界面触发一次 refresh；
    /// 所有请求统一带 X-Tenant header，服务端按注册表白名单校验后路由。
    @Published var tenant: String = "default"

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 8
        return URLSession(configuration: cfg,
                          delegate: LoopbackTrustDelegate(),
                          delegateQueue: nil)
    }()

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private init() {}

    /// 拉取一次全量状态。失败时保留上一次的数据，只更新错误提示：
    /// 界面上「数据暂时拿不到」比「整页清空」体感好得多。
    func refresh(token: String) async {
        isLoading = true
        defer { isLoading = false }
        do {
            state = try await get("/api/console/state", token: token)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// 空间管理页局部更新租户列表（避免为列表变动整页 refresh）。
    func updateTenants(_ tenants: [TenantInfo]) {
        state?.tenants = tenants
    }

    func report(date: String, token: String) async throws -> DayReport {
        try await get("/api/console/report/\(date)", token: token)
    }

    // MARK: - 设置

    func config(token: String) async throws -> ServerConfig {
        try await get("/api/console/config", token: token)
    }

    func saveConfig(_ values: [String: String], token: String) async throws -> SaveConfigResult {
        try await send("/api/console/config", method: "PUT",
                       body: SaveBody(values: values), token: token)
    }

    func testLLM(_ body: LLMTestBody, token: String) async throws -> LLMTestResult {
        try await send("/api/console/llm/test", method: "POST", body: body, token: token)
    }

    func llmModels(_ body: LLMModelsBody, token: String) async throws -> LLMModelsResult {
        try await send("/api/console/llm/models", method: "POST", body: body, token: token)
    }

    // MARK: - 行业 / 角色 / 日报重算

    /// 切换行业。路径参数必须百分号编码：行业 key 是 ASCII 没问题，
    /// 但这里与删除角色共用同一条规则，免得将来哪个 key 带了非 ASCII 再踩一遍。
    func setIndustry(_ name: String, token: String) async throws {
        let path = "/api/console/industry/\(Self.encodePath(name))"
        let _: EmptyResult = try await sendEmpty(path, method: "PUT", token: token)
    }

    /// 词表原文（含注释行），编辑器的起点。
    func industryTerms(_ name: String, token: String) async throws -> IndustryTerms {
        try await get("/api/console/industry/\(Self.encodePath(name))/terms", token: token)
    }

    /// 保存编辑后的词表。服务端写数据目录（不动只读的 bundle）并热生效。
    func saveIndustryTerms(_ name: String, text: String,
                           token: String) async throws -> IndustryTermsSaveResult {
        try await send("/api/console/industry/\(Self.encodePath(name))/terms",
                       method: "PUT", body: TermsBody(text: text), token: token)
    }

    private struct TermsBody: Encodable {
        let text: String
    }

    /// 删除具名角色。**名字含中文必须 URL 编码**（实测踩过：裸中文路径
    /// 会被服务端按字面量解析，404 却查不出原因）。
    func deleteSpeaker(_ name: String, token: String) async throws {
        let _: EmptyResult = try await sendEmpty(
            "/api/speakers/\(Self.encodePath(name))", method: "DELETE", token: token)
    }

    struct SpeakerRegistration: Decodable {
        let ok: Bool
        let name: String
    }

    /// 上传一段录音注册具名角色（走 `/api/pair`，与手机端「配对」同一端点）。
    ///
    /// 为什么 Mac 端也要留一条：手机端那条路必须现场录满 30 秒，而电脑上往往
    /// 已经存着现成录音（会议记录、录音笔导出、工作台上传）。服务端只认
    /// 「一段清晰人声」，从哪来都一样 —— 落库按**当前空间**（`X-Tenant`），
    /// 与设备档案无关，所以在 Mac 上切空间即可给任意空间注册角色。
    func registerSpeaker(name: String, fileURL: URL,
                         token: String) async throws -> SpeakerRegistration {
        // 角色名要进 query。urlQueryAllowed 默认保留 `&` `=` `+` `#`，
        // 名字里一旦有它们就会把 query 拆坏（服务端只看到半个名字）。
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "&+=?#")
        let q = name.addingPercentEncoding(withAllowedCharacters: allowed) ?? ""
        guard let url = URL(string: "https://127.0.0.1:\(Paths.apiPort)/api/pair?name=\(q)") else {
            throw APIError.offline
        }
        let wav: Data
        do {
            wav = try Data(contentsOf: fileURL)
        } catch {
            throw APIError.http(400)
        }
        guard !wav.isEmpty else { throw APIError.http(400) }

        // multipart 手工拼：只有 file 一个字段，为它引第三方库不值得
        let boundary = "----dh\(UUID().uuidString)"
        var body = Data()
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data(("Content-Disposition: form-data; name=\"file\"; "
                          + "filename=\"\(fileURL.lastPathComponent)\"\r\n").utf8))
        body.append(Data("Content-Type: audio/wav\r\n\r\n".utf8))
        body.append(wav)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        // 30 秒样本要做重采样 + 声纹提取，60 秒在加载模型的机器上会擦边
        req.timeoutInterval = 180
        req.setValue("multipart/form-data; boundary=\(boundary)",
                     forHTTPHeaderField: "Content-Type")
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        req.setValue(tenant, forHTTPHeaderField: "X-Tenant")
        req.httpBody = body

        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError.offline
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        switch http.statusCode {
        case 200:
            return try decoder.decode(SpeakerRegistration.self, from: data)
        case 401:
            throw APIError.unauthorized
        default:
            throw APIError.http(http.statusCode)
        }
    }

    /// 手动触发某日的处理管线。
    /// `final: true` 是日终终稿（全段重转 + 去口气 + 总结 + 清理音频），
    /// 代价大且有删音频的副作用，调用方必须先弹确认。
    func triggerDone(date: String, final: Bool, token: String) async throws -> DoneResult {
        try await sendEmpty(
            "/api/done/\(Self.encodePath(date))?final=\(final ? 1 : 0)",
            method: "POST", token: token)
    }

    private static func encodePath(_ s: String) -> String {
        // 必须额外去掉 `/`：默认 urlPathAllowed 保留它，名字里一旦有斜杠就会
        // 把 /api/speakers/{name} 切断成两级路径，服务端只能回 404。
        // 角色名是用户在手机端自由输入的（夹斜杠很常见），这里不做猜测。
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/")
        return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
    }

    /// 手动触发数据备份（服务端归档 SQLite + 转写文本 + 语料）。
    /// 服务端有这个能力但界面一直没有入口，数据只能靠 .app 升级前碰运气。
    func triggerBackup(token: String) async throws -> DoneResult {
        try await sendEmpty("/api/backup", method: "POST", token: token)
    }

    struct RotateTokenResult: Decodable {
        let ok: Bool
        let token: String
    }

    /// 一键更换配对 Token：服务端生成强随机值、写 env.local 并热生效。
    /// 返回的新 token 由调用方展示；所有已配对手机要重填。
    func rotateToken(token: String) async throws -> RotateTokenResult {
        try await sendEmpty("/api/console/rotate-token", method: "POST", token: token)
    }

    // MARK: - 租户空间管理（2026-09-09 多租户）

    struct TenantListResult: Decodable {
        let tenants: [TenantInfo]
    }

    /// 全部租户空间概览。
    func listTenants(token: String) async throws -> [TenantInfo] {
        let r: TenantListResult = try await get("/api/console/tenants", token: token)
        return r.tenants
    }

    struct AddTenantBody: Encodable {
        let id: String
        let name: String
    }

    /// 注册新租户（成员手机接入前先开空间；服务端立即建库）。
    func addTenant(id: String, name: String, token: String) async throws -> [TenantInfo] {
        let r: TenantListResult = try await send("/api/console/tenants", method: "POST",
                                                 body: AddTenantBody(id: id, name: name),
                                                 token: token)
        return r.tenants
    }

    /// 注销租户（只移出注册表，数据目录保留，防误删）。
    func removeTenant(_ id: String, token: String) async throws -> [TenantInfo] {
        let _: EmptyResult = try await sendEmpty(
            "/api/console/tenants/\(Self.encodePath(id))", method: "DELETE", token: token)
        return try await listTenants(token: token)
    }

    /// 按租户触发备份（空 = 当前租户；"all" = 全部空间打进一个包）。
    func triggerBackup(scope: String? = nil, token: String) async throws -> DoneResult {
        let s = scope ?? tenant
        let path = s == "default" ? "/api/backup" : "/api/backup?tenant=\(Self.encodePath(s))"
        return try await sendEmpty(path, method: "POST", token: token)
    }

    private struct EmptyResult: Decodable {}

    private struct SaveBody: Encodable {
        let values: [String: String]
    }

    /// 无请求体的请求（PUT/DELETE/POST 空体）。复用 send 的响应处理，
    /// 单独写一份而不是给 send 加可选 body：可选参数会让调用处类型推断变复杂。
    private func sendEmpty<T: Decodable>(_ path: String, method: String,
                                         token: String) async throws -> T {
        guard let url = URL(string: "https://127.0.0.1:\(Paths.apiPort)\(path)") else {
            throw APIError.offline
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        // done 触发要等 pipeline 启动确认，8 秒不够
        req.timeoutInterval = 60
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        req.setValue(tenant, forHTTPHeaderField: "X-Tenant")
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError.offline
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        switch http.statusCode {
        case 200:
            return try decoder.decode(T.self, from: data)
        case 401:
            throw APIError.unauthorized
        case 404:
            throw APIError.http(404)
        default:
            throw APIError.http(http.statusCode)
        }
    }

    private func get<T: Decodable>(_ path: String, token: String) async throws -> T {
        guard let url = URL(string: "https://127.0.0.1:\(Paths.apiPort)\(path)") else {
            throw APIError.offline
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        req.setValue(tenant, forHTTPHeaderField: "X-Tenant")
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError.offline
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        switch http.statusCode {
        case 200:
            return try decoder.decode(T.self, from: data)
        case 401:
            throw APIError.unauthorized
        default:
            throw APIError.http(http.statusCode)
        }
    }

    private func send<T: Decodable, B: Encodable>(
        _ path: String, method: String, body: B, token: String
    ) async throws -> T {
        guard let url = URL(string: "https://127.0.0.1:\(Paths.apiPort)\(path)") else {
            throw APIError.offline
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        // 连通性测试要等厂商往返，8 秒的请求超时不够用（本地 4B 模型光思考就几十秒）
        req.timeoutInterval = 60
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        req.setValue(tenant, forHTTPHeaderField: "X-Tenant")
        req.httpBody = try JSONEncoder().encode(body)
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError.offline
        }
        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        switch http.statusCode {
        case 200:
            return try decoder.decode(T.self, from: data)
        case 401:
            throw APIError.unauthorized
        default:
            throw APIError.http(http.statusCode)
        }
    }
}
