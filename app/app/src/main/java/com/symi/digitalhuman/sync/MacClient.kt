package com.symi.digitalhuman.sync

import com.symi.digitalhuman.data.AppSettings
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

/**
 * Mac 服务端 REST 客户端。
 *
 * TLS 策略：
 * - 已配置证书指纹（设置页「配对」时抓取）→ 在 TrustManager 里校验 SPKI 指纹，
 *   只信任该证书。这是局域网场景的正确做法：自签证书无法走公共 CA 校验，但可以
 *   用指纹锁定把「信任任何自签证书」收敛成「只信任我那一台 Mac 的证书」。
 *   （不用 OkHttp 的 CertificatePinner，原因见 buildClient 内注释）
 * - 未配置指纹 → 退化为信任全部自签证书（仅靠 Bearer token 兜底）。此时中间人
 *   可以伪造证书截获语音数据与 token，设置页会对此给出提示。
 *
 * 关于 hostnameVerifier：Mac 以局域网 IP 提供自签证书，证书 CN 通常不是该 IP，
 * 严格的 hostname 校验必然失败，因此放宽。安全性由证书指纹锁定承担——
 * pinning 校验的是证书本身，伪造的证书即使 CN 正确也过不了。
 */
class MacClient(private val s: AppSettings) {

    private val http: OkHttpClient = buildClient()

    /**
     * 对话专用客户端：只把**读超时**放宽到 120s，其余（连接/写入超时、证书锁定、
     * endpoint 选择）与 [http] 完全一致。
     *
     * 为什么单开一个：`/api/talk/audio` 是**同步**跑完 ASR → Hermes → TTS 才返回的，
     * 而 Hermes 侧最坏情况是 `DH_HERMES_TIMEOUT(30s) × DH_HERMES_RETRY(3)` ≈ 90s，
     * 已经超过通用客户端的 60s 读超时。超时后客户端会误判成「上传失败」，
     * 而服务端其实可能已经出稿——所以既要放宽这里，又要有 [talkReply] 兜底补拉。
     * 不改 [http] 本身：上传/日报那些接口不该跟着一起变慢。
     */
    private val talkHttp: OkHttpClient = http.newBuilder()
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    private fun buildClient(): OkHttpClient {
        val builder = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)

        val pin = s.pinnedCertSha256.trim().ifBlank { null }
        val tm = trustAllManager(pin)
        val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf<TrustManager>(tm), SecureRandom()) }
        builder.sslSocketFactory(ssl.socketFactory, tm)
        // 见类注释：自签 + IP 访问下必须放宽，安全性由 pinning 承担
        builder.hostnameVerifier { _, _ -> true }

        // 注意：不用 OkHttp 的 CertificatePinner——
        // ① OkHttp 5 的 add() 构建期解析 pattern，写 host:port 直接抛 Invalid pattern；
        // ② 实测 OkHttp 5.5.0 下 pin 校验时 handshake.peerCertificates 为空列表
        //   （"Certificate pinning failure! Peer certificate chain:（空）"），
        //   禁用 TLS 会话缓存也无法避免。指纹校验已移入 TrustManager.checkServerTrusted：
        //   该回调的 chain 参数始终是完整对端链，不依赖 OkHttp 内部行为。
        return builder.build()
    }

    /**
     * 信任全部签发者 + 可选 SPKI 指纹锁定。
     *
     * 指纹校验在 checkServerTrusted 里做：对证书链上每张证书算
     * SPKI（SubjectPublicKeyInfo DER）的 SHA-256，任一命中即放行；
     * 已配置指纹但链上无命中 → 抛 CertificateException（握手失败）。
     * TLS 会话恢复的连接不再回调 checkServerTrusted，但恢复的会话证书
     * 与首次验证过的完全一致，安全性等价。
     */
    private fun trustAllManager(pinnedSha256: String?): X509TrustManager = object : X509TrustManager {
        override fun checkClientTrusted(c: Array<X509Certificate>, a: String) {}
        override fun checkServerTrusted(c: Array<X509Certificate>, a: String) {
            if (pinnedSha256 == null) return
            val md = MessageDigest.getInstance("SHA-256")
            val matched = c.any { cert ->
                Base64.getEncoder().encodeToString(md.digest(cert.publicKey.encoded)) == pinnedSha256
            }
            if (!matched) throw CertificateException("服务端证书 SPKI 与已锁定指纹不符")
        }
        override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
    }

    /** 已启用证书锁定时为 true（设置页据此提示安全等级） */
    val pinningEnabled: Boolean get() = s.pinnedCertSha256.trim().isNotBlank()

    // ---- 自适应端点：局域网优先，探测失败自动切外网中转 ----
    // 中转（frp 隧道）与局域网指向同一台 Mac：同一张自签证书、同一个 Token，
    // 证书 pinning 与鉴权在两条路上同样生效；探测 = 1.5s TCP 连接，结果缓存 60s。
    // 选择逻辑统一放在 [Endpoints]（本类与 GuardService **共用同一份**，
    // 别再各写一套——2026-09-21 修过一次两边不一致的坑）。

    private fun pickEndpoint(): LinkEndpoint = Endpoints.pick(s)

    /**
     * 生效端点的可读标签，用于失败提示。
     *
     * 以前失败提示写死 `${s.macIp}:${s.macPort}` 再附一句"确认⋯⋯在同一局域网"，
     * 人走外网时明明走的是中转，却被告知"要在同一局域网"——把用户往错方向引。
     */
    private fun endpointLabel(): String = Endpoints.label(Endpoints.pick(s))

    private fun url(path: String): String {
        val ep = pickEndpoint()
        return "${if (s.token.isNotBlank()) "https" else "http"}://${ep.host}:${ep.port}$path"
    }

    private fun Request.Builder.auth(): Request.Builder =
        header("Authorization", "Bearer ${s.token}")
            // 多租户（2026-09-09）：所有请求带 X-Tenant，服务端按注册表白名单
            // 校验后路由到 data/tenants/<id>/。header 只允许 ASCII，无需编码；
            // 空 = default 空间（旧版服务端会忽略这个 header，双向兼容）。
            .header("X-Tenant", s.tenantId.trim().ifBlank { "default" })
            // 设备身份（2026-09-15）：带上安装实例 ID，服务端据此查到**设备档案**，
            // 由档案决定数据落哪个空间、用哪个管家角色、允不允许走本机 Hermes。
            // 档案的优先级高于 X-Tenant——管理员在服务端改过归属后，客户端这边
            // 即使还留着旧的空间设置也不会把数据写错地方（服务端会记一条告警）。
            // 旧版服务端不认识这个 header，忽略即可，双向兼容。
            .apply { s.installId.trim().takeIf { it.isNotEmpty() }?.let { header("X-Install-Id", it) } }

    /** 上传一个 WAV 段（整文件 PUT，Mac 端按 segId 幂等落盘） */
    fun uploadSegment(
        segId: Long, sessionId: Long, startOffsetMs: Long, durationMs: Long,
        speechRatio: Float, file: File,
    ): Boolean {
        val body = file.readBytes().toRequestBody("audio/wav".toMediaType())
        // 设备名随段上传：URL query 用（header 不允许非 ASCII），空则回退手机型号，
        // 服务端 device_label 落库 → /api/devices 可按手机/成员区分来源。
        val deviceName = s.ownerName.trim().ifBlank { android.os.Build.MODEL }
        val deviceQ = java.net.URLEncoder.encode(deviceName, "UTF-8")
        // install：安装实例标识 → 服务端据此给本机分配独立的段号命名空间，
        // 多台设备不再互相覆盖（2026-09-15，见 store.device_registry）。
        // 空则不带该参数，服务端退回按设备名分空间（旧版行为，不丢数据）。
        val installQ = s.installId.trim()
        val url = buildString {
            append("/api/upload/$segId?device=$deviceQ")
            if (installQ.isNotEmpty()) append("&install=$installQ")
        }
        val req = Request.Builder()
            .url(url(url))
            .auth()
            .header("X-Session-Id", sessionId.toString())
            .header("X-Start-Offset-Ms", startOffsetMs.toString())
            .header("X-Duration-Ms", durationMs.toString())
            .header("X-Speech-Ratio", speechRatio.toString())
            .put(body)
            .build()
        http.newCall(req).execute().use { r -> return r.isSuccessful }
    }

    /**
     * 通知 Mac 当日上传完毕并触发处理管线。
     *
     * final=true  → 日终终稿：跑去口气 + 总结（成本高，只在每日处理时间点跑）
     * final=false → 增量上传：只补 ASR 转写，沿用上一版干净稿/总结（成本低，可高频）
     *
     * 返回值带**服务端处理窗口**（2026-09-27 起）：服务端为省资源改成只在
     * `DH_AGG_WINDOWS`（默认 13:00 / 17:45）内处理，窗口外只落库不跑管线。
     * 调用方据此决定要不要等结果——窗口外等是白等（旧版固定轮询 150 秒）。
     * 老服务端不返回这两个字段 → 视为「一直在窗口内」，行为与旧版完全一致。
     */
    fun notifyUploadDone(date: String, final: Boolean = false,
                         force: Boolean = false): DoneAck =
        postJson("/api/done/$date?final=${if (final) 1 else 0}${if (force) "&force=1" else ""}")?.let { obj ->
            DoneAck(
                ok = true,
                pipelineStarted = obj["pipeline_started"]?.jsonPrimitive?.booleanOrNull ?: false,
                inWindow = obj["in_window"]?.jsonPrimitive?.booleanOrNull ?: true,
                aggWindows = obj["agg_windows"]?.jsonPrimitive?.contentOrNull ?: "",
            )
        } ?: DoneAck(ok = false, pipelineStarted = false, inWindow = true, aggWindows = "")

    /** `/api/done` 的回执：是否受理、是否已启动管线、当前是否在服务端处理窗口内。 */
    data class DoneAck(
        val ok: Boolean,
        val pipelineStarted: Boolean,
        val inWindow: Boolean,
        /** 服务端当前的处理窗口原文（如 `13:00,17:45`），空 = 实时模式 */
        val aggWindows: String,
    )

    /** 拉取日报；Mac 未就绪返回 null */
    fun fetchReport(date: String): String? {
        val req = Request.Builder().url(url("/api/report/$date")).auth().get().build()
        http.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            // OkHttp 5 起 Response.body 是非空类型，无需安全调用
            return r.body.string()
        }
    }

    /**
     * 注册一个具名角色声纹（建议 ≥30s）。
     *
     * name 为空 → 注册为「本人」；非空 → 注册为该名字的角色（如「张三」）。
     * 注册后服务端把该声纹作为**具名锚点**，聚类时优先级最高：
     * 后续识别到就直接按这个名字记录，工作环境里的固定同事不再被写成甲乙丙丁。
     */
    fun registerVoiceprint(file: File, name: String = ""): Pair<Boolean, String> {
        val label = name.trim()
        val path = if (label.isEmpty()) "/api/pair"
        else "/api/pair?name=${java.net.URLEncoder.encode(label, "UTF-8")}"
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", file.name,
                file.readBytes().toRequestBody("audio/wav".toMediaType()),
            )
            .build()
        return try {
            val req = Request.Builder().url(url(path)).auth().post(body).build()
            http.newCall(req).execute().use { r ->
                // body.string() 在流被中断时会抛，故保留 runCatching 兜底
                val detail = runCatching { r.body.string().take(160) }.getOrNull() ?: ""
                r.isSuccessful to if (r.isSuccessful) detail else "${r.code} $detail"
            }
        } catch (e: Exception) {
            false to "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /**
     * 外网中转提示（服务端 env DH_RELAY_HINT，经 /api/console/state 下发）。
     * 拿不到返回 null——调用方保持现状，绝不因它清空已填地址。
     */
    fun fetchRelayHint(): String? {
        return try {
            val req = Request.Builder().url(url("/api/console/state")).auth().get().build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return null
                runCatching {
                    kotlinx.serialization.json.Json.parseToJsonElement(r.body.string())
                        .jsonObject["relay_hint"]?.let {
                            (it as? kotlinx.serialization.json.JsonPrimitive)?.content
                        }
                }.getOrNull()
            }
        } catch (e: Exception) {
            android.util.Log.w("MacClient", "fetchRelayHint 失败: ${e.javaClass.simpleName}")
            null
        }
    }

    /** 已注册的具名角色列表；失败返回 null（调用方保留上一次结果，不清空 UI） */    fun listSpeakers(): List<String>? {
        return try {
            val req = Request.Builder().url(url("/api/speakers")).auth().get().build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return null
                val arr = runCatching {
                    kotlinx.serialization.json.Json.parseToJsonElement(r.body.string())
                        .jsonObject["speakers"] as? kotlinx.serialization.json.JsonArray
                }.getOrNull() ?: return emptyList()
                arr.mapNotNull { e ->
                    (e as? kotlinx.serialization.json.JsonObject)
                        ?.get("name")
                        ?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content }
                }
            }
        } catch (e: Exception) {
            android.util.Log.w("MacClient", "listSpeakers 失败: ${e.javaClass.simpleName}")
            null
        }
    }

    /**
     * 行业词表：当前行业 + 可选行业列表。
     *
     * 行业决定三件事（都在 Mac 端生效）：
     * ① 转写热词：把「KNX」「乳胶漆」这类词喂给 ASR，压低同音错字
     *   （实测过「吊顶」被写成「杯盖」、「软膜款」被写成「预买款」）
     * ② 文本纠偏：去口气/总结时让模型按行业术语表改写可疑同音词
     * ③ Agent 可见：/api/agent/days 会把词表一起返回，供智能体二次校正
     */
    data class Industry(val key: String, val label: String, val terms: Int)

    data class IndustryInfo(
        val current: String,
        val currentLabel: String,
        val available: List<Industry>,
        val terms: Int,
        val pinnedByEnv: Boolean,
    )

    fun listIndustries(): IndustryInfo? {
        return try {
            val req = Request.Builder().url(url("/api/industries")).auth().get().build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return null
                val obj = runCatching {
                    kotlinx.serialization.json.Json.parseToJsonElement(r.body.string()).jsonObject
                }.getOrNull() ?: return null
                val el = { k: String -> obj[k] as? kotlinx.serialization.json.JsonPrimitive }
                val arr = obj["available"] as? kotlinx.serialization.json.JsonArray ?: return null
                val list = arr.mapNotNull { e ->
                    val o = e as? kotlinx.serialization.json.JsonObject ?: return@mapNotNull null
                    val g = { k: String ->
                        (o[k] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: ""
                    }
                    Industry(g("key"), g("label").ifBlank { g("key") },
                        g("terms").toIntOrNull() ?: 0)
                }
                IndustryInfo(
                    current = el("current")?.content ?: "general",
                    currentLabel = el("current_label")?.content
                        ?: el("current")?.content ?: "general",
                    available = list,
                    terms = el("terms")?.content?.toIntOrNull() ?: 0,
                    pinnedByEnv = el("pinned_by_env")?.content == "true",
                )
            }
        } catch (e: Exception) {
            android.util.Log.w("MacClient", "listIndustries 失败: ${e.javaClass.simpleName}")
            null
        }
    }

    /**
     * 切换行业（即时生效，只影响之后的转写）。
     * 已生成的历史日报不会重跑——要整体重算请在 Mac 上跑 rebuild_report.py。
     */
    fun setIndustry(name: String): Pair<Boolean, String> {
        val path = "/api/industry/${java.net.URLEncoder.encode(name.trim(), "UTF-8")}"
        return try {
            val req = Request.Builder().url(url(path)).auth().put("{}".toRequestBody()).build()
            http.newCall(req).execute().use { r ->
                val bodyText = runCatching { r.body.string() }.getOrNull() ?: ""
                if (!r.isSuccessful) return false to when (r.code) {
                    404 -> "Mac 上没有这个行业词表"
                    409 -> "Mac 端行业被环境变量锁定，无法从手机切换"
                    else -> "Mac 返回 ${r.code}"
                }
                val label = runCatching {
                    kotlinx.serialization.json.Json.parseToJsonElement(bodyText)
                        .jsonObject["label"]?.let {
                            (it as? kotlinx.serialization.json.JsonPrimitive)?.content
                        }
                }.getOrNull()
                true to "已切换到「${label?.takeIf { it.isNotBlank() } ?: name}」"
            }
        } catch (e: java.net.ConnectException) {
            false to "连不上 ${endpointLabel()}"
        } catch (e: Exception) {
            android.util.Log.w("MacClient", "setIndustry 失败: ${e.javaClass.simpleName}: ${e.message}")
            false to "切换失败，请重试"
        }
    }

    /** 删除一个具名角色（不追溯改历史标签，只影响后续识别） */
    fun deleteSpeaker(name: String): Pair<Boolean, String> {
        val path = "/api/speakers/${java.net.URLEncoder.encode(name.trim(), "UTF-8")}"
        return try {
            val req = Request.Builder().url(url(path)).auth().delete().build()
            http.newCall(req).execute().use { r ->
                if (r.isSuccessful) true to "已删除"
                else false to when (r.code) {
                    404 -> "Mac 上没有这个角色"
                    else -> "Mac 返回 ${r.code}"
                }
            }
        } catch (e: Exception) {
            false to "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /**
     * 连通性自检，返回 (是否可用, 原因)。
     *
     * 刻意不返回裸 Boolean：失败原因差别很大（IP 不通 / 端口没开 / token 不对 /
     * 证书不匹配），全揉成一句「失败」会让用户和日志都无从下手。
     * 声纹注册这类「要先确认 Mac 在线再干活」的场景尤其依赖它——
     * 以前是闷头录 30 秒才发现上传不了，白等。
     */
    fun checkReachable(): Pair<Boolean, String> {
        if (s.macIp.isBlank() && s.relayAddress.isBlank()) return false to "还没填 Mac 地址"
        return try {
            http.newCall(Request.Builder().url(url("/api/health")).auth().get().build())
                .execute().use { r ->
                    when {
                        r.isSuccessful -> true to "已连接"
                        r.code == 401 -> false to "Token 不对，请与 Mac 端 DH_TOKEN 核对"
                        else -> false to "Mac 返回 ${r.code}"
                    }
                }
        } catch (e: java.net.ConnectException) {
            false to "连不上 ${endpointLabel()}，确认 Mac 服务已启动（在家走局域网，在外走外网中转）"
        } catch (e: java.net.SocketTimeoutException) {
            false to "连接超时，确认 Mac 服务已启动（在家走局域网，在外走外网中转）"
        } catch (e: javax.net.ssl.SSLException) {
            // 带上异常类型与细节：pin 不匹配、协议协商失败、连接中断在 SSLException
            // 家族里表现完全不同，只给一句「证书校验失败」会多绕一轮排查。
            false to "证书校验失败（${e.javaClass.simpleName}: ${e.message}），请重新获取证书指纹"
        } catch (e: Exception) {
            false to "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /**
     * 通过 TLS 握手取回 Mac 证书的公钥指纹（SPKI SHA-256，base64）。
     *
     * 这就是设置页那串 openssl 命令的等价实现，只是不用用户去 Mac 上敲：
     *   openssl x509 -pubkey -noout | openssl pkey -pubin -outform der
     *     | openssl dgst -sha256 -binary | openssl enc -base64
     *
     * 为什么不用 OkHttp 的 Response.handshake：实测它会返回非空的 handshake 但
     * peerCertificates 为空列表（连接复用/会话恢复时 OkHttp 不回填证书链），
     * 直接 .first() 就抛 NoSuchElementException。这里改用原生 SSLSocket 握手，
     * 拿到的必然是本次连接的真实对端证书，不依赖框架行为。
     */
    fun fetchCertFingerprint(): Pair<Boolean, String> {
        if (s.macIp.isBlank() && s.relayAddress.isBlank()) return false to "还没填 Mac 地址"
        // 走**生效端点**：局域网与中转是同一张自签证书，人在外网也能取到指纹
        val ep = pickEndpoint()
        val host = ep.host
        return try {
            // 与 http client 同一套信任策略：自签证书必须先放开（这里不锁指纹——
            // 我们要的恰恰是「先看清楚这张证书长什么样」，再用指纹把它锁死）。
            val tm = trustAllManager(null)
            val ssl = SSLContext.getInstance("TLS").apply {
                init(null, arrayOf<TrustManager>(tm), SecureRandom())
            }
            val socket = ssl.socketFactory.createSocket() as javax.net.ssl.SSLSocket
            socket.use { sock ->
                sock.connect(java.net.InetSocketAddress(host, ep.port), 5000)
                sock.soTimeout = 5000
                sock.startHandshake()
                val leaf = sock.session.peerCertificates.firstOrNull()
                    ?: return@use false to "没拿到对方证书，确认 Mac 端服务已启用 HTTPS"
                val spki = leaf.publicKey.encoded   // SubjectPublicKeyInfo DER
                if (spki == null || spki.isEmpty()) {
                    return@use false to "证书内容异常，请重试"
                }
                val digest = java.security.MessageDigest.getInstance("SHA-256").digest(spki)
                true to android.util.Base64.encodeToString(digest, android.util.Base64.NO_WRAP)
            }
        } catch (e: java.net.ConnectException) {
            false to "连不上 ${Endpoints.label(ep)}，确认 Mac 服务已启动"
        } catch (e: java.net.SocketTimeoutException) {
            false to "连接超时，确认 Mac 服务已启动"
        } catch (e: java.net.UnknownHostException) {
            false to "找不到主机 $host，检查地址填得对不对（在家填局域网 IP，在外填外网中转地址）"
        } catch (e: javax.net.ssl.SSLException) {
            false to "TLS 握手失败，确认 Mac 端服务已启用 HTTPS"
        } catch (e: Exception) {
            // 不把原始异常类名/报错暴露给用户，只提示重试；细节留给 logcat 排查
            android.util.Log.w("MacClient", "fetchCertFingerprint 失败: ${e.javaClass.simpleName}: ${e.message}")
            false to "获取失败，请重试（网络或服务异常）"
        }
    }

    /**
     * 触发 Mac 端数据备份（POST /api/backup）。
     * 服务端把 text/ + corpus/ + 数据库归档为 backups/ 下的 zip，返回文件名与大小。
     * 返回 (是否成功, 人性化消息)：成功消息含归档文件名与大小，失败消息可直接展示。
     */
    fun triggerBackup(): Pair<Boolean, String> {
        val req = Request.Builder().url(url("/api/backup")).auth()
            .post("{}".toRequestBody("application/json".toMediaType())).build()
        return try {
            http.newCall(req).execute().use { r ->
                // 备份涉及整目录打包，Mac 端可能在响应前耗时较久，读超时已放宽到 60s
                val bodyText = runCatching { r.body.string() }.getOrNull() ?: ""
                if (!r.isSuccessful) {
                    return false to when (r.code) {
                        401 -> "Token 不对，请与 Mac 端 DH_TOKEN 核对"
                        else -> "Mac 返回 ${r.code}，备份未执行"
                    }
                }
                val obj = runCatching {
                    kotlinx.serialization.json.Json.parseToJsonElement(bodyText).jsonObject
                }.getOrNull() ?: return false to "Mac 响应格式异常，备份结果未知"
                val file = (obj["file"] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: ""
                val size = (obj["size_bytes"] as? kotlinx.serialization.json.JsonPrimitive)?.content?.toLongOrNull() ?: 0L
                true to "备份完成：${file.ifBlank { "backup.zip" }}（${formatSize(size)}）"
            }
        } catch (e: java.net.ConnectException) {
            false to "连不上 ${endpointLabel()}，确认 Mac 服务已启动（在家走局域网，在外走外网中转）"
        } catch (e: java.net.SocketTimeoutException) {
            false to "备份超时（数据量大或 Mac 忙碌），可稍后重试"
        } catch (e: javax.net.ssl.SSLException) {
            // 带上异常类型与细节：pin 不匹配、协议协商失败、连接中断在 SSLException
            // 家族里表现完全不同，只给一句「证书校验失败」会多绕一轮排查。
            false to "证书校验失败（${e.javaClass.simpleName}: ${e.message}），请重新获取证书指纹"
        } catch (e: Exception) {
            android.util.Log.w("MacClient", "triggerBackup 失败: ${e.javaClass.simpleName}: ${e.message}")
            false to "备份请求失败，请重试"
        }
    }

    /**
     * 列出 Mac 端已有的备份归档（GET /api/backup/list）。
     *
     * @return (是否成功, 人性化消息, 列表)。列表元素为 {file, size, mtime}。
     *         失败时列表为空，调用方保留上一次结果、不要把 UI 清空。
     */
    fun listBackups(): Triple<Boolean, String, List<Map<String, String>>> {
        return try {
            http.newCall(Request.Builder().url(url("/api/backup/list")).auth().get().build())
                .execute().use { r ->
                    val bodyText = runCatching { r.body.string() }.getOrNull().orEmpty()
                    if (!r.isSuccessful) {
                        val why = if (r.code == 401) "Token 不对，请与 Mac 端 DH_TOKEN 核对"
                        else "Mac 返回 ${r.code}"
                        return Triple(false, why, emptyList())
                    }
                    val arr = runCatching {
                        kotlinx.serialization.json.Json.parseToJsonElement(bodyText)
                            .jsonObject["backups"] as? kotlinx.serialization.json.JsonArray
                    }.getOrNull() ?: return Triple(false, "Mac 响应格式异常", emptyList())
                    val out = arr.mapNotNull { e ->
                        val o = e.jsonObject
                        fun s(k: String) = o[k]?.jsonPrimitive?.contentOrNull.orEmpty()
                        val f = s("file")
                        if (f.isBlank()) null else mapOf(
                            "file" to f,
                            "size" to s("size_bytes"),
                            "mtime" to s("mtime"),
                        )
                    }
                    Triple(true, "共 ${out.size} 份备份", out)
                }
        } catch (e: Exception) {
            Triple(false, "读取备份列表失败：${e.javaClass.simpleName}", emptyList())
        }
    }

    /**
     * 把服务端的某份备份归档下载到本机（GET /api/backup/download/{name}）。
     *
     * 「导出」在本机侧就是一个真实文件：下载到应用外部目录，用户可用文件管理器
     * 取走或通过分享发送出去。下载用流式拷贝，不把整个包读进内存
     * （语料库压缩后也可能上百兆，读进内存在大包上会 OOM）。
     */
    fun downloadBackup(name: String, dest: File): Pair<Boolean, String> {
        val safe = java.net.URLEncoder.encode(name, "UTF-8").replace("+", "%20")
        val req = Request.Builder().url(url("/api/backup/download/$safe")).auth().get().build()
        return try {
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) {
                    return false to if (r.code == 404) "服务端已找不到这份备份" else "Mac 返回 ${r.code}"
                }
                dest.parentFile?.mkdirs()
                r.body.byteStream().use { ins ->
                    dest.outputStream().use { outs -> ins.copyTo(outs, 64 * 1024) }
                }
                val n = dest.length()
                if (n <= 0) {
                    dest.delete()
                    return false to "下载到的文件为空"
                }
                true to "已导出：${dest.name}（${formatSize(n)}）"
            }
        } catch (e: Exception) {
            runCatching { dest.delete() }
            false to "导出失败：${e.javaClass.simpleName} ${e.message.orEmpty()}"
        }
    }

    /**
     * 从一份备份包恢复服务端数据（POST /api/backup/restore?confirm=1）。
     *
     * 这是**破坏性**操作：服务端会整体替换该租户的数据库与 text/corpus/markers。
     * 服务端自身在动手前会先留一份「恢复前快照」，成功的响应里带着它的文件名，
     * 这里把文件名带回给用户，让「恢复错了还能退回来」这件事是**可见**的。
     */
    fun restoreBackup(file: File): Pair<Boolean, String> {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", file.name,
                file.readBytes().toRequestBody("application/zip".toMediaType()),
            )
            .build()
        val req = Request.Builder()
            .url(url("/api/backup/restore?confirm=1"))
            .auth()
            .post(body)
            .build()
        return try {
            // 恢复要解包 + 覆盖整库，比普通请求慢得多；通用读超时 60s 可能不够。
            http.newBuilder().readTimeout(180, TimeUnit.SECONDS).build()
                .newCall(req).execute().use { r ->
                    val bodyText = runCatching { r.body.string() }.getOrNull().orEmpty()
                    if (!r.isSuccessful) {
                        val detail = runCatching {
                            kotlinx.serialization.json.Json.parseToJsonElement(bodyText)
                                .jsonObject["detail"]?.jsonPrimitive?.contentOrNull
                        }.getOrNull().orEmpty()
                        return false to (detail.ifBlank { "Mac 返回 ${r.code}，恢复未执行" })
                    }
                    val obj = runCatching {
                        kotlinx.serialization.json.Json.parseToJsonElement(bodyText).jsonObject
                    }.getOrNull() ?: return false to "Mac 响应格式异常，恢复结果未知"
                    fun s(k: String) = obj[k]?.jsonPrimitive?.contentOrNull.orEmpty()
                    val files = s("files")
                    val safety = s("safety_backup")
                    true to buildString {
                        append("恢复完成：写入 $files 个文件")
                        if (safety.isNotBlank()) append("；恢复前快照 ${safety}（可退回来）")
                    }
                }
        } catch (e: java.net.SocketTimeoutException) {
            false to "恢复超时（包较大或 Mac 忙碌），请在服务端确认结果后再重试"
        } catch (e: Exception) {
            false to "恢复失败：${e.javaClass.simpleName} ${e.message.orEmpty()}"
        }
    }

    /**
     * TTS 测试播报的结果。ok=false 时 message 是可直接展示的中文错误。
     *
     * 用普通 class 而不是 data class：内含 ByteArray，自动生成的 equals/hashCode
     * 比的是引用，容易让人误以为「内容相同就是相等」，是个埋雷。
     */
    class TtsPreview(
        val ok: Boolean,
        val wav: ByteArray = ByteArray(0),
        /** 服务端实际念出来的文字（URL 解码后），显示给用户核对 */
        val script: String = "",
        val message: String = "",
    )

    /**
     * 拉一段服务端合成的「今天数据」播报语音（GET /api/tts/preview）。
     *
     * 直接拿 WAV 字节而不是「让服务端播放」：声音必须从**手机/盒子自己的扬声器**
     * 出来——服务端在另一台机器上，它在那边放没人听得到。
     */
    fun fetchTtsPreview(date: String = ""): TtsPreview {
        val q = if (date.isBlank()) "" else "?date=${java.net.URLEncoder.encode(date, "UTF-8")}"
        val req = Request.Builder().url(url("/api/tts/preview$q")).auth().get().build()
        return try {
            // 合成要等 TTS 后端跑完（本地 say 通常 1~3 秒，edge-tts 走网络更久）
            http.newBuilder().readTimeout(120, TimeUnit.SECONDS).build()
                .newCall(req).execute().use { r ->
                    val script = java.net.URLDecoder.decode(
                        r.header("X-TTS-Text").orEmpty(), "UTF-8"
                    )
                    if (!r.isSuccessful) {
                        val detail = runCatching {
                            kotlinx.serialization.json.Json.parseToJsonElement(r.body.string())
                                .jsonObject["detail"]?.jsonPrimitive?.contentOrNull
                        }.getOrNull().orEmpty()
                        return TtsPreview(false, script = script, message = detail.ifBlank {
                            if (r.code == 401) "Token 不对，请与 Mac 端 DH_TOKEN 核对"
                            else "Mac 返回 ${r.code}"
                        })
                    }
                    val bytes = r.body.bytes()
                    if (bytes.size <= 44) {
                        return TtsPreview(false, script = script, message = "服务端返回的音频是空的")
                    }
                    TtsPreview(true, bytes, script)
                }
        } catch (e: java.net.SocketTimeoutException) {
            TtsPreview(false, message = "合成超时（Mac 端 TTS 可能未配置），请稍后重试")
        } catch (e: Exception) {
            TtsPreview(false, message = "${e.javaClass.simpleName}: ${e.message.orEmpty()}")
        }
    }

    /**
     * 标记此刻（重要时刻，DayoTag 式随手标记）。
     * t 为手机本地时间 HH:MM(:SS)，服务端存进当天 markers.json，
     * 日报以「重要时刻」卡片呈现（确定性追加，不依赖 LLM）。
     */
    fun markNow(date: String, t: String): Pair<Boolean, String> {
        val body = """{"t":"$t"}"""
        val req = Request.Builder().url(url("/api/mark/$date")).auth()
            .post(body.toRequestBody("application/json".toMediaType())).build()
        return try {
            http.newCall(req).execute().use { r ->
                if (r.isSuccessful) true to "已标记 $t"
                else false to when (r.code) {
                    401 -> "Token 不对，请与 Mac 端 DH_TOKEN 核对"
                    else -> "Mac 返回 ${r.code}，未标记"
                }
            }
        } catch (e: java.net.ConnectException) {
            false to "连不上 ${endpointLabel()}"
        } catch (e: Exception) {
            android.util.Log.w("MacClient", "markNow 失败: ${e.javaClass.simpleName}: ${e.message}")
            false to "标记失败，请重试"
        }
    }

    // ------------------------------------------------------------------
    // 实时对话（Hermes 对接，2026-09-10）
    //
    // 与服务端 /api/talk/* 对应。设计上刻意复用本类已有的 endpoint 选择
    // （局域网直连 / 中转自动切换）与 TLS pinning——对话和上传是同一台
    // Mac、同一张证书、同一个 token，没有任何理由再建一套连接栈。
    // ------------------------------------------------------------------

    /** 一轮对话的结果。status ∈ done / skipped / failed（服务端 hermes 字段） */
    data class TalkResult(
        val turnId: String,
        val asrText: String,
        /** 管家回复文本；status=skipped 时为空 */
        val replyText: String,
        /** 回复音频的相对路径（服务端未合成成功时为 null） */
        val audioUrl: String?,
        /** hermes=走管家 / ai_only=降级纯AI / none=未命中唤醒词 */
        val via: String,
        val status: String,
        /** status=skipped 的原因：no_alias / empty_transcript */
        val reason: String,
        val totalMs: Long,
    ) {
        /** 值得播放：拿到了文本且有音频 */
        val playable: Boolean get() = replyText.isNotBlank() && audioUrl != null

        /**
         * 终态：这一轮的结果不会再变。
         *
         * **不能只把 `pending` 当未完成**：服务端 `talk_start` 建 turn 时
         * `status="open"`，`/reply` 直接回这个值（`talk.py` 的
         * `turn.get("status") or "pending"`），只有真正走完才会变成
         * done / failed / no_alias / empty / loop_guard。用白名单判终态最稳。
         */
        val terminal: Boolean get() = status in TERMINAL_STATUS

        private companion object {
            /** 服务端 hermes.status 的终态取值（见 server/app/talk.py）。 */
            val TERMINAL_STATUS = setOf(
                "done", "failed", "skipped", "no_alias", "empty",
                // 回声自激熔断：连续多轮转写高度相似，服务端主动静默
                "loop_guard",
                // 声纹角色门控：识别出的说话人不是已注册角色，服务端不应答。
                // 必须算终态——否则补拉通道会一直等到 6 秒超时才给结论。
                "not_owner",
                // 近重复 / Hermes 回声去重生效（2026-09-10）：同一句话被两条
                // 路径各切一份送来时，服务端只答第一轮、把后续的判成 dup。
                // 这是**防护生效**，不是故障；必须算终态，否则补拉通道会白等。
                "dup",
                // 「判为仅记录」（2026-09-11）：Hermes 按内容判断这句不是在跟它
                // 说话，于是只落库、不回复。同属**分流生效**，不是故障。
                "noted",
                // 服务端明确拒绝（400/413，音频超限）。这不是「没等到结果」，
                // 必须立刻算终态并把原因带回去，否则用户会看到误导性的「超时」。
                "rejected",
            )
        }
    }

    private val talkJson = Json { ignoreUnknownKeys = true; isLenient = true }

    private fun deviceId(): String = s.ownerName.trim().ifBlank { android.os.Build.MODEL }

    /**
     * 开一轮对话的结果。
     *
     * 为什么要区分成四种而不是笼统返回 null：**403（空间 ID 未注册）和
     * 连不上在用户眼里长得一模一样，处理方式却完全相反**——前者要改设置，
     * 后者要查网络。之前统一报「连不上 Mac 服务」，实测把一个填错的
     * 空间 ID（`1001`，服务端要求小写字母开头）排查了很久才定位。
     */
    sealed class StartResult {
        data class Ok(val turnId: String) : StartResult()
        /** 403：空间 ID 在服务端未注册（tenants.resolve 未命中） */
        object TenantRejected : StartResult()
        /** 401：配对 Token 不对 */
        object AuthRejected : StartResult()
        /** 其余失败：不可达、证书不匹配、超时、5xx */
        data class Failed(val code: Int, val reason: String) : StartResult()
    }

    /**
     * 开一轮对话。`sessionId` 必须是**会话内稳定**的字符串——
     * 服务端按它维护多轮上下文（见 TalkController.conversationSessionId）。
     */
    fun talkStart(sessionId: String): StartResult = try {
        val body = buildJsonObject {
            put("tenant", JsonPrimitive(s.tenantId.trim().ifBlank { "default" }))
            put("device_id", JsonPrimitive(deviceId()))
            put("session_id", JsonPrimitive(sessionId))
            // 设备身份（2026-09-15）：服务端据此定位设备档案，决定这一轮用哪个
            // 管家角色、允不允许调用本机 Hermes。**在 /start 时定下来**并挂在这一轮
            // 上，中途改设置不会让同一轮对话换人格。
            s.installId.trim().takeIf { it.isNotEmpty() }
                ?.let { put("install_id", JsonPrimitive(it)) }
        }.toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(url("/api/talk/start")).auth().post(body).build()
        http.newCall(req).execute().use { r ->
            when {
                r.isSuccessful -> {
                    val turnId = talkJson.parseToJsonElement(r.body.string())
                        .jsonObject["turn_id"]?.jsonPrimitive?.contentOrNull
                    if (turnId.isNullOrBlank()) StartResult.Failed(r.code, "响应缺少 turn_id")
                    else StartResult.Ok(turnId)
                }
                r.code == 403 -> StartResult.TenantRejected
                r.code == 401 -> StartResult.AuthRejected
                else -> StartResult.Failed(r.code, "HTTP ${r.code}")
            }
        }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "talkStart 失败: ${e.javaClass.simpleName}: ${e.message}")
        StartResult.Failed(0, e.javaClass.simpleName)
    }

    /**
     * 上传一段对话音频。服务端同步走完 ASR → Hermes → TTS 后返回结果。
     *
     * @param hasAlias 本地唤醒词是否命中。**只是线索**，不决定服务端要不要处理
     *   （见 `CaptureService.consumeTalkSegment` 的说明）。手动模式传 true——
     *   用户主动点「对话」本身就是明确的对话意图。
     *
     * @param scope 送给智能体的范围（`data/TalkScope`）。`keyword` 时服务端把
     *   不命中唤醒词的轮次落 `no_alias` 丢掉；`all`（默认）全部交给智能体按内容
     *   判定要不要应答。**空串 = 交由服务端全局配置决定** —— 老客户端不带这个
     *   header 时，行为与加入本参数之前完全一致。
     *
     * ## 非 2xx 不再吞成 null（2026-09-11）
     *
     * 旧实现是 `if (!r.isSuccessful) null`。400/413 意味着**服务端明确拒绝了这段
     * 音频**（超时长/超体积），与「网络没回来」是两件事；合成一个 null 之后调用方
     * 只能盲等补拉 6 秒、最后报一句「对话超时」——用户既不知道原因、内容也被丢弃。
     * 日志实测这样丢过 24 轮（`audio too long (12.3s > 10s)`）。
     * 现在把服务端的 detail 带回去，让调用方能给出可执行的提示。
     */
    fun talkAudio(turnId: String, file: File, speechRatio: Float = 0.9f,
                  hasAlias: Boolean = true,
                  scope: String = ""): TalkResult? = try {
        val body = file.readBytes().toRequestBody("audio/wav".toMediaType())
        val req = Request.Builder()
            .url(url("/api/talk/audio/$turnId"))
            .auth()
            // header 只允许 ASCII，设备名可能含中文，必须 URL 编码
            .header("X-Device-Name", java.net.URLEncoder.encode(deviceId(), "UTF-8"))
            .header("X-Speech-Ratio", speechRatio.toString())
            .header("X-Has-Alias", if (hasAlias) "1" else "0")
            // 传输范围（keyword / all），由用户在设置页决定。
            // 空串 = 沿用服务端全局配置 —— 老客户端不带这个 header 时行为与改造前一致。
            .header("X-Talk-Scope", scope)
            .put(body)
            .build()
        talkHttp.newCall(req).execute().use { r ->
            if (r.isSuccessful) return@use parseTalk(r.body.string(), turnId)
            val detail = runCatching {
                talkJson.parseToJsonElement(r.body.string()).jsonObject["detail"]
                    ?.jsonPrimitive?.contentOrNull
            }.getOrNull().orEmpty()
            android.util.Log.w("MacClient", "talkAudio 被服务端拒绝 HTTP ${r.code}: $detail")
            TalkResult(
                turnId = turnId, asrText = "", replyText = "", audioUrl = null,
                via = "server", status = "rejected",
                reason = if (detail.contains("too long")) "audio_too_long" else "http_${r.code}",
                totalMs = 0L,
            )
        }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "talkAudio 失败: ${e.javaClass.simpleName}: ${e.message}")
        null
    }

    /**
     * 补拉一轮对话的结果（规格 §5.3）。
     *
     * 只在 [talkAudio] 没拿到结果时调用：客户端超时/连接中断，不代表服务端没算完——
     * 同一轮 turn 的回复已经写进服务端内存态，再问一次就能拿到。
     * 返回 null 表示 turn 不存在（已过期/被 GC）或网络仍不通。
     *
     * 返回结果可能是未完成态（`status="open"`，音频还没处理完），
     * 调用方必须用 [TalkResult.terminal] 判断，别直接当作答复。
     */
    fun talkReply(turnId: String): TalkResult? = try {
        val req = Request.Builder().url(url("/api/talk/reply/$turnId")).auth().get().build()
        // 补拉是同步函数的最后一次机会，用宽超时；失败必须快速返回，别把 UI 卡住
        talkHttp.newBuilder().readTimeout(10, TimeUnit.SECONDS).build()
            .newCall(req).execute().use { r ->
                if (!r.isSuccessful) null else parseTalk(r.body.string(), turnId)
            }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "talkReply 失败: ${e.javaClass.simpleName}: ${e.message}")
        null
    }

    private fun parseTalk(raw: String, turnId: String): TalkResult {
        val o = talkJson.parseToJsonElement(raw).jsonObject
        val h = o["hermes"]?.jsonObject
        return TalkResult(
            turnId = turnId,
            asrText = o["asr_text"]?.jsonPrimitive?.contentOrNull ?: "",
            replyText = h?.get("reply_text")?.jsonPrimitive?.contentOrNull ?: "",
            audioUrl = h?.get("reply_audio_url")?.jsonPrimitive?.contentOrNull,
            via = h?.get("via")?.jsonPrimitive?.contentOrNull ?: "",
            status = h?.get("status")?.jsonPrimitive?.contentOrNull ?: "",
            reason = h?.get("reason")?.jsonPrimitive?.contentOrNull ?: "",
            totalMs = o["total_latency_ms"]?.jsonPrimitive?.contentOrNull?.toLongOrNull() ?: 0L,
        )
    }

    /** 下载服务端合成的回复音频（16k mono wav）。 */
    fun downloadTalkAudio(path: String): ByteArray? = try {
        val req = Request.Builder().url(url(path)).auth().get().build()
        http.newCall(req).execute().use { r ->
            if (r.isSuccessful) r.body.bytes() else null
        }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "downloadTalkAudio 失败: ${e.javaClass.simpleName}: ${e.message}")
        null
    }

    /**
     * 结束一轮：服务端删音频、保留文字记录，并把播放时长回传 Hermes
     * （管家那边据此统计播报成功率）。
     */
    fun talkEnd(turnId: String, playedMs: Long): Boolean = try {
        val body = buildJsonObject {
            put("played_ms", JsonPrimitive(playedMs.toString()))
        }.toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(url("/api/talk/end/$turnId")).auth().post(body).build()
        http.newCall(req).execute().use { it.isSuccessful }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "talkEnd 失败: ${e.javaClass.simpleName}: ${e.message}")
        false
    }

    private fun formatSize(bytes: Long): String = when {
        bytes >= 1 shl 20 -> "%.1f MB".format(bytes / 1048576.0)
        bytes >= 1 shl 10 -> "%.0f KB".format(bytes / 1024.0)
        else -> "$bytes B"
    }

    private fun post(path: String, jsonBody: String = "{}"): Boolean = try {
        val req = Request.Builder().url(url(path)).auth()
            .post(jsonBody.toRequestBody("application/json".toMediaType())).build()
        http.newCall(req).execute().use { it.isSuccessful }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "post 失败 url=$path: ${e.javaClass.simpleName}: ${e.message}")
        false
    }

    /**
     * POST 并解析 JSON 响应体；网络失败或响应非 2xx 返回 null。
     *
     * 与 [post] 的区别：需要读回执字段时用这个（如 /api/done 的 in_window）。
     * 解析失败按 null 处理，调用方走「与旧服务端一致」的保守分支。
     */
    /**
     * 今日洞察：关键词 Top-N + 隐私分类汇总（1.0.2 新增）。
     *
     * 服务端 /api/agent/summary 的 keywords 与 privacy 字段：
     * - keywords：行业词/实体/高频段，智能体与 UI 优先围绕它们处理，
     *   避免重读全天原文烧 token；
     * - privacy：口述密码/手机号/身份证等按类计数，samples 已由服务端掩码
     *   （不留原值——UI 只是高亮提示，不是敏感信息展示页）。
     *
     * ready!=true、非 200、解析失败一律返回 null，调用方显示「暂不可用」。
     */
    fun fetchSummary(date: String): DayInsights? {
        val req = Request.Builder().url(url("/api/agent/summary/$date")).auth().get().build()
        return try {
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return null
                val obj = runCatching { Json.parseToJsonElement(r.body.string()).jsonObject }
                    .getOrNull() ?: return null
                if (obj["ready"]?.jsonPrimitive?.booleanOrNull != true) return null
                val keywords = obj["keywords"]?.jsonArray?.mapNotNull { k ->
                    val o = k.jsonObject
                    InsightKeyword(
                        kw = o["kw"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null,
                        count = o["count"]?.jsonPrimitive?.intOrNull ?: 0,
                        kind = o["kind"]?.jsonPrimitive?.contentOrNull ?: "freq",
                    )
                } ?: emptyList()
                val privacy = obj["privacy"]?.jsonArray?.mapNotNull { p ->
                    val o = p.jsonObject
                    InsightPrivacy(
                        label = o["label"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null,
                        count = o["count"]?.jsonPrimitive?.intOrNull ?: 0,
                        samples = (o["samples"] as? JsonArray)
                            ?.mapNotNull { (it as? JsonPrimitive)?.content }
                            ?: emptyList(),
                    )
                } ?: emptyList()
                DayInsights(keywords = keywords, privacy = privacy)
            }
        } catch (e: Exception) {
            // 带上端点与 pin 状态：洞察是低频请求，失败时必须能一眼定位
            // 「连的哪个候选、是否证书锁定误伤、token 是否为空」。
            android.util.Log.w(
                "MacClient",
                "fetchSummary 失败: ${e.javaClass.simpleName}: ${e.message} " +
                    "endpoint=${endpointLabel()} pin=${pinningEnabled} " +
                    "tokenBlank=${s.token.isBlank()} date=$date",
            )
            null
        }
    }

    /** /summary 的 keywords 单项（kind: term=行业词 / entity=数字英文实体 / freq=高频段） */
    data class InsightKeyword(val kw: String, val count: Int, val kind: String)

    /** /summary 的 privacy 单项（样本已由服务端掩码，UI 只做高亮提示） */
    data class InsightPrivacy(val label: String, val count: Int, val samples: List<String>)

    /** 今日洞察汇总：任一列表可为空（当天没说到就是没有，不是错误） */
    data class DayInsights(val keywords: List<InsightKeyword>, val privacy: List<InsightPrivacy>)

    private fun postJson(path: String, jsonBody: String = "{}"): JsonObject? = try {
        val req = Request.Builder().url(url(path)).auth()
            .post(jsonBody.toRequestBody("application/json".toMediaType())).build()
        http.newCall(req).execute().use { r ->
            if (!r.isSuccessful) null
            else runCatching { Json.parseToJsonElement(r.body.string()).jsonObject }.getOrNull()
        }
    } catch (e: Exception) {
        android.util.Log.w("MacClient", "postJson 失败 url=$path: ${e.javaClass.simpleName}: ${e.message}")
        null
    }
}
