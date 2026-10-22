import SwiftUI

/// 设置页的状态容器。
///
/// 为什么要单独一层：设置项来自服务端 schema（`config.CONFIG_SCHEMA`），
/// 有 30 个字段、多种类型，直接塞进 View 会让 body 里出现一大堆
/// 「从 draft 取字符串 → 转成 Double → 再写回去」的胶水代码。
/// 这里集中管三件事：拉 schema、维护编辑缓冲、只回传真正改动的键。
@MainActor
final class SettingsStore: ObservableObject {

    @Published private(set) var groups: [ConfigGroup] = []
    @Published private(set) var presets: [LLMPreset] = []
    @Published private(set) var llm: LLMSettings?
    @Published private(set) var envFile = ""
    @Published private(set) var secretsSet: [String: Bool] = [:]

    @Published private(set) var isLoading = false
    @Published private(set) var isSaving = false
    @Published private(set) var loadError: String?
    @Published private(set) var saveMessage: String?
    @Published private(set) var restartRequired: [String] = []

    @Published private(set) var testResult: LLMTestResult?
    @Published private(set) var isTesting = false
    @Published private(set) var modelList: [String] = []
    @Published var isLoadingModels = false
    @Published var modelListError: String?

    @Published private(set) var isRotatingToken = false
    @Published private(set) var rotateMessage: String?

    /// 编辑缓冲。key 是 env 变量名，值统一存字符串（布尔存 "1"/"0"）。
    @Published var draft: [String: String] = [:]
    /// 用户显式输入过的密钥。
    ///
    /// 独立于 draft 是因为服务端**不会回传密钥明文**，draft 里永远是空串；
    /// 若把「没回传」当成「清空」，用户每保存一次别的设置就会把 Key 抹掉。
    /// 只有这里出现过的键才会参与提交，值为 "" 表示用户要清除它。
    @Published var secrets: [String: String] = [:]

    private var baseline: [String: String] = [:]

    // MARK: - 键名

    enum Key {
        static let provider  = "DH_LLM_PROVIDER"
        static let proto     = "DH_LLM_PROTOCOL"
        static let baseURL   = "DH_LLM_BASE_URL"
        static let model     = "DH_LLM_MODEL"
        static let apiKey    = "DH_LLM_API_KEY"
        static let servedID  = "DH_LLM_SERVED_ID"
        static let timeout   = "DH_LLM_TIMEOUT"
        static let proxy     = "DH_HTTPS_PROXY"
        static let pairingToken = "DH_TOKEN"
    }

    /// 括号不能省：`??` 优先级低于 `==`，不加括号会把 `"local" == "cloud"` 先算掉。
    var isCloud: Bool { (draft[Key.provider] ?? "local") == "cloud" }

    /// 真正要提交的变化。
    ///
    /// 只回传改动过的键：全量提交会把「用户没动、服务端基于别的字段推算出来的值」
    /// 一起固化进 env.local，之后再改别的就不生效了。
    var pendingChanges: [String: String] {
        var out: [String: String] = [:]
        for (k, v) in draft where baseline[k] != v {
            out[k] = v
        }
        for (k, v) in secrets {
            if v.isEmpty {
                // 清空只在原本有值时才有意义
                if secretsSet[k] == true { out[k] = "" }
            } else {
                out[k] = v
            }
        }
        return out
    }

    var hasChanges: Bool { !pendingChanges.isEmpty }

    // MARK: - 读写

    func load(token: String) async {
        isLoading = true
        defer { isLoading = false }
        do {
            apply(try await APIClient.shared.config(token: token))
            loadError = nil
        } catch {
            loadError = error.localizedDescription
        }
    }

    private func apply(_ cfg: ServerConfig) {
        groups = cfg.schema
        presets = cfg.presets
        llm = cfg.llm
        envFile = cfg.envFile
        secretsSet = cfg.secretsSet
        var d: [String: String] = [:]
        for g in cfg.schema {
            for f in g.fields {
                // env.local 里没写这一项时，服务端 values 给的是**空字符串**而不是
                // null，`?? f.defaultValue` 于是不会被触发，draft 拿到 ""。
                // 后果不是「显示空值」那么轻：下拉类控件（choice/select）的
                // selection 是 ""，落不到任何选项上，Picker 渲染成一个**空控件**
                // ——用户看到「增量轮毂后端」右边只有一个空圈（2026-09-16 实测）。
                // 空串一律回退到 schema 的 default，与服务端实际生效值一致。
                let raw = cfg.values[f.name]?.asString ?? ""
                d[f.name] = raw.isEmpty ? (f.defaultValue ?? "") : raw
            }
        }
        draft = d
        baseline = d
        secrets = [:]
    }

    func revert() {
        draft = baseline
        secrets = [:]
        saveMessage = nil
        restartRequired = []
    }

    func save(token: String) async {
        let changes = pendingChanges
        guard !changes.isEmpty else { return }
        isSaving = true
        defer { isSaving = false }
        do {
            let res = try await APIClient.shared.saveConfig(changes, token: token)
            apply(res.config)
            restartRequired = res.restartRequired
            saveMessage = res.restartRequired.isEmpty
                ? "已保存 \(res.written.count) 项，立即生效"
                : "已保存 \(res.written.count) 项，其中 \(res.restartRequired.count) 项需重启服务"
        } catch {
            saveMessage = "保存失败：\(error.localizedDescription)"
        }
    }

    /// 应用厂商预设：填地址 / 协议 / 模型，并把运行位置切到云端。
    func applyPreset(_ p: LLMPreset) {
        draft[Key.provider] = "cloud"
        draft[Key.proto] = p.protocolName
        draft[Key.baseURL] = p.baseUrl
        draft[Key.model] = p.model
    }

    func test(token: String) async {
        isTesting = true
        testResult = nil
        defer { isTesting = false }
        let body = LLMTestBody(
            baseUrl: draft[Key.baseURL],
            model: draft[Key.model],
            apiKey: secrets[Key.apiKey],
            protocolName: draft[Key.proto],
            proxy: draft[Key.proxy],
            provider: draft[Key.provider],
            servedId: isCloud ? nil : draft[Key.servedID],
            timeout: Int(draft[Key.timeout] ?? "")
        )
        do {
            testResult = try await APIClient.shared.testLLM(body, token: token)
        } catch {
            testResult = LLMTestResult(ok: false, message: error.localizedDescription,
                                       latencyMs: 0, url: "", model: "", protocolName: "")
        }
    }

    /// 一键更换配对 Token：服务端生成强随机值并已写 env.local 热生效，
    /// 这里把 draft/baseline 同步成新值（不算「未保存的修改」）。
    /// 手机端不会自动收到——要用户重填，所以消息里必须说清楚。
    func rotatePairingToken(apiToken: String) async {
        guard !isRotatingToken else { return }
        isRotatingToken = true
        defer { isRotatingToken = false }
        do {
            let r = try await APIClient.shared.rotateToken(token: apiToken)
            draft[Key.pairingToken] = r.token
            baseline[Key.pairingToken] = r.token
            rotateMessage = "已生成并保存，复制后填到每台手机的「配对 Token」"
        } catch {
            rotateMessage = "更换失败：\(error.localizedDescription)"
        }
    }

    /// 换厂商后旧模型列表就作废了，必须清掉，否则用户会拿 A 厂商的模型名
    /// 去填 B 厂商，测出来 404 还以为是 Key 的问题。
    func resetModels() {
        modelList = []
        modelListError = nil
        testResult = nil
    }

    func fetchModels(token: String) async {
        isLoadingModels = true
        modelListError = nil
        defer { isLoadingModels = false }
        let body = LLMModelsBody(
            baseUrl: draft[Key.baseURL] ?? "",
            apiKey: secrets[Key.apiKey] ?? "",
            protocolName: draft[Key.proto] ?? "openai",
            proxy: draft[Key.proxy] ?? ""
        )
        do {
            let res = try await APIClient.shared.llmModels(body, token: token)
            modelList = res.models
            modelListError = res.ok ? nil : res.message
        } catch {
            modelList = []
            modelListError = error.localizedDescription
        }
    }
}
