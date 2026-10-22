import Foundation

// MARK: - 服务端返回的 schema

/// 一个可配项的声明。字段名与服务端 `config.CONFIG_SCHEMA` 逐字对应，
/// 服务端加一个可配项，App 不用改代码就会多出一行控件。
struct ConfigField: Decodable, Identifiable {
    /// 一个下拉选项。
    ///
    /// ⚠️ 服务端按字段类型给两种形态：
    /// - `choice`：`{"value":"local","label":"本地模型"}`
    /// - `select`：`["1","0"]` —— **只有值，没有显示名**
    ///
    /// 早先只按对象形态声明，服务端一加 `select` 类型（DH_TALK_FORCE_HERMES /
    /// DH_TALK_OWNER_GATE），**整个设置页就变成「配置读取失败」**：接口是 200、
    /// 响应体完全正常，光看服务端日志什么都查不出来，只有拿真实响应逐字段
    /// 比对才能发现（2026-09-16 实测）。
    ///
    /// 字符串形态没有显示名，直接拿值当 label —— 这些值本身就是 "1"/"0"/"off"
    /// 这类短枚举，可读性够。
    struct Option: Decodable, Identifiable {
        let value: String
        let label: String
        var id: String { value }

        enum CodingKeys: String, CodingKey { case value, label }

        init(from decoder: Decoder) throws {
            let single = try decoder.singleValueContainer()
            if let s = try? single.decode(String.self) {
                value = s
                label = s
                return
            }
            let c = try decoder.container(keyedBy: CodingKeys.self)
            value = try c.decode(String.self, forKey: .value)
            label = try c.decode(String.self, forKey: .label)
        }
    }

    /// text / secret / int / number / bool / choice / textarea
    let type: String
    let name: String
    let label: String
    let help: String?
    let defaultValue: String?
    let min: Double?
    let max: Double?
    let step: Double?
    let options: [Option]?
    /// 改了要重启服务
    let restart: Bool?
    /// 可热更新
    let hot: Bool?
    /// 只在本地模式下出现
    let localOnly: Bool?
    /// 只在云端模式下出现
    let cloudOnly: Bool?

    var id: String { name }

    /// CodingKeys 的值必须是 **convertFromSnakeCase 转换后**的形式：
    /// APIClient 的 decoder 先把 `local_only` 转成 `localOnly`，再拿
    /// CodingKeys 的字符串去匹配。写成 "local_only" 会全部失配、解码抛错，
    /// 设置页就永远停在「正在读取配置…」（2026-09-05 实际踩过）。
    /// 只有与属性名不一致的 key 才需要显式声明。
    enum CodingKeys: String, CodingKey {
        case type, name, label, help, min, max, step, options, hot, restart
        case localOnly, cloudOnly
        case defaultValue = "default"
    }
}

struct ConfigGroup: Decodable, Identifiable {
    let key: String
    let label: String
    let fields: [ConfigField]
    var id: String { key }
}

/// 云端厂商预设。模型名随时在变，所以列表以厂商的 /models 为准，
/// 这里只免得用户去翻文档找域名。
struct LLMPreset: Decodable, Identifiable {
    let vendor: String
    let region: String
    let baseUrl: String
    let protocolName: String
    let model: String
    let keyUrl: String

    var id: String { vendor }

    // 值为转换后的 camelCase（见 ConfigField 的说明），"protocol" 无下划线不转换
    enum CodingKeys: String, CodingKey {
        case vendor, region, model, baseUrl, keyUrl
        case protocolName = "protocol"
    }

    var isDomestic: Bool { region == "cn" }
}

/// 模型与接口的当前生效状态。
struct LLMSettings: Decodable, Equatable {
    let isCloud: Bool
    let provider: String
    let protocolName: String
    let baseUrl: String
    let model: String
    let modelId: String
    let servedId: String
    let timeout: Int
    let proxy: String
    let hasKey: Bool

    enum CodingKeys: String, CodingKey {
        case provider, model, timeout, proxy
        case isCloud, protocolName = "protocol", baseUrl, modelId, servedId, hasKey
    }
}

/// `GET /api/console/config` 的响应。
struct ServerConfig: Decodable {
    let schema: [ConfigGroup]
    let values: [String: JSONValue]
    let secretsSet: [String: Bool]
    let envFile: String
    let llm: LLMSettings
    let presets: [LLMPreset]

    /// 键名走 APIClient 全局的 `.convertFromSnakeCase`：服务端给 `secrets_set`
    /// 会被转成 `secretsSet` 再与属性名匹配，所以这里**不能**再写
    /// `case secretsSet = "secrets_set"` —— 转换后的键是驼峰，显式写下划线
    /// 反而会失配（与 ConfigField 顶部那段注释同一条规则）。
    enum CodingKeys: String, CodingKey {
        case schema, values, presets, llm, secretsSet, envFile
    }
}

/// `PUT /api/console/config` 的响应。
struct SaveConfigResult: Decodable {
    let ok: Bool
    let written: [String]
    let restartRequired: [String]
    let config: ServerConfig

    enum CodingKeys: String, CodingKey {
        case ok, written, config, restartRequired
    }
}

/// `POST /api/console/llm/test` 的响应。
struct LLMTestResult: Decodable, Equatable {
    let ok: Bool
    let message: String
    let latencyMs: Int
    let url: String
    let model: String
    let protocolName: String

    enum CodingKeys: String, CodingKey {
        case ok, message, url, model, latencyMs
        case protocolName = "protocol"
    }
}

/// `POST /api/console/llm/models` 的响应。
struct LLMModelsResult: Decodable {
    let ok: Bool
    let message: String
    let models: [String]
}

// MARK: - 请求体

/// 连通性测试的入参。
///
/// 全部可选：合成的 Encodable 对 Optional 用 encodeIfPresent，
/// 没填的字段不会出现在 JSON 里，服务端 `body.get(...)` 拿到 None 就沿用已保存值。
/// 这一点很关键——设置页里 API Key 是掩码显示的，用户没改就不会回传，
/// 服务端必须拿已保存的 Key 去测，否则永远报「缺少 API Key」。
struct LLMTestBody: Encodable {
    var baseUrl: String?
    var model: String?
    var apiKey: String?
    var protocolName: String?
    var proxy: String?
    var provider: String?
    var servedId: String?
    var timeout: Int?

    enum CodingKeys: String, CodingKey {
        case model, proxy, provider, timeout
        case baseUrl = "base_url"
        case apiKey = "api_key"
        case protocolName = "protocol"
        case servedId = "served_id"
    }
}

struct LLMModelsBody: Encodable {
    var baseUrl: String
    var apiKey: String
    var protocolName: String
    var proxy: String

    enum CodingKeys: String, CodingKey {
        case proxy
        case baseUrl = "base_url"
        case apiKey = "api_key"
        case protocolName = "protocol"
    }
}

// MARK: - 混合类型的配置值

/// env.local 里同一份 values 字典既有字符串也有布尔（bool 字段直接给 true/false），
/// 用枚举而不是 `Any`，解码时按类型分支。
///
/// 顺序要紧：Bool 必须在 Double 之前判，否则 `true` 会被当成数字。
enum JSONValue: Decodable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else {
            self = .null
        }
    }

    /// 统一成字符串，方便在 TextField 里编辑。
    /// 布尔转 "1"/"0"，与 env.local 的写法一致，回传时服务端也认。
    var asString: String {
        switch self {
        case .string(let s): return s
        case .bool(let b):   return b ? "1" : "0"
        case .number(let n): return Self.fmt(n)
        case .null:          return ""
        }
    }

    private static func fmt(_ n: Double) -> String {
        if n == n.rounded(), abs(n) < 1e15 { return String(Int(n)) }
        return String(format: "%g", n)
    }
}
