package com.symi.digitalhuman.sync

import com.symi.digitalhuman.data.AppSettings

/**
 * 生效端点：host/port + 这条是不是外网中转。
 *
 * [viaRelay] 必须显式带出来，不能用「不等于 macIp」去猜——UI 与日志都要如实
 * 说明"当前走的是哪条路"，否则用户在外网看到"连不上 Mac"却查不出原因。
 */
data class LinkEndpoint(val host: String, val port: Int, val viaRelay: Boolean)

/**
 * 端点选择：**局域网优先，局域网探不通且配了外网中转才走中转**。
 *
 * 为什么单独抽成一份：MacClient 的所有请求走这里，GuardService 的链路探测也
 * 必须走这里。以前两边各写一套——请求认中转、探测只看局域网——后果是人在外网
 * 时「Mac 不可达」一直亮着，而且**积压音频的补传上升沿永远不触发**
 * （uploadLinkUp 恒 false），只能干等 WorkManager 指数退避（最长 5 小时）。
 * 2026-09-21 排查"手机远程连不上 Mac"时定位到此处，别再拆开。
 *
 * 探测用 1.5s TCP 连接（端口开着就算在线，不必等 TLS 握手）；选择结果缓存 60s，
 * 免得每个请求都付一次探测成本。
 */
object Endpoints {

    private const val CACHE_MS = 60_000L

    @Volatile
    private var cached: Pair<LinkEndpoint, Long>? = null

    /** 局域网端点（Mac 地址可能为空，调用方自行判空） */
    fun lan(s: AppSettings): LinkEndpoint =
        LinkEndpoint(s.macIp.trim(), s.macPort, viaRelay = false)

    /** 中转端点：relayAddress 必须是 host:port；解析失败或留空 → null（禁用） */
    fun relay(s: AppSettings): LinkEndpoint? =
        s.relayAddress.trim().takeIf { it.isNotBlank() }?.let { raw ->
            val i = raw.lastIndexOf(':')
            val port = if (i > 0) raw.substring(i + 1).toIntOrNull() else null
            if (port != null && port in 1..65535) {
                LinkEndpoint(raw.substring(0, i), port, viaRelay = true)
            } else {
                null
            }
        }

    /** 纯 TCP 连通性探测（不开 TLS，只要端口通就算在线） */
    fun tcpAlive(ep: LinkEndpoint): Boolean = try {
        java.net.Socket().use { it.connect(java.net.InetSocketAddress(ep.host, ep.port), 1500) }
        true
    } catch (e: Exception) {
        false
    }

    /**
     * 选一次生效端点（60s 缓存）。
     *
     * [force] = true 时忽略缓存、现场重探。给 GuardService 的周期探测用：人刚走出门
     * 时局域网立刻不通，若被 60s 缓存挡住，会有最多一分钟的请求全打在不可达的
     * 局域网上（表现为"刚出门这一分钟怎么都传不上去"）。周期探测强制重选一次，
     * 随后缓存即翻到中转，请求路径也跟着切过去。
     */
    @Synchronized
    fun pick(s: AppSettings, force: Boolean = false): LinkEndpoint {
        val l = lan(s)
        val r = relay(s) ?: return l         // 没配中转 → 永远直连（原行为）
        val now = System.currentTimeMillis()
        if (!force) {
            cached?.let { (ep, at) -> if (now - at < CACHE_MS) return ep }
        }
        return if (tcpAlive(l)) {
            cached = l to now; l             // 在家：局域网（延迟低、不吃流量）
        } else {
            cached = r to now; r             // 出门：走中转，上传/报告照常
        }
    }

    /**
     * 探一次当前生效端点是否可达；两条路都不通返回 null。
     *
     * 与 [pick] 的区别：pick 只"选"，不看结果；本方法把选中的那条**真连一次**，
     * 供 GuardService 判断"上传链路现在通不通"。
     */
    fun probe(s: AppSettings, force: Boolean = false): LinkEndpoint? =
        pick(s, force).takeIf { tcpAlive(it) }

    /** 端点可读标签，直接进用户可见的失败提示 */
    fun label(ep: LinkEndpoint): String =
        "${ep.host}:${ep.port}" + if (ep.viaRelay) "（外网中转）" else ""
}
