package com.symi.digitalhuman.sync

import android.content.Context
import java.io.File
import java.net.URLDecoder
import java.net.URLEncoder

/**
 * 声纹样本的离线待提交队列。
 *
 * 场景：人在外面、Mac 没开机，但正好有空给同事录声纹。旧流程要求「先探通 Mac 再开
 * 麦克风」，探不通就直接失败——录了 30 秒什么都没留下，回到 Mac 旁还得重录一遍。
 * 现在录音与提交解耦：录完先进本地队列（filesDir/enroll_pending），
 * 之后由 [flush] 在 Mac 可达时自动补交，用户不用守着点提交。
 *
 * 用文件目录而不是 Room 表：样本本身就是 wav，目录即索引。为「待提交」再建一张表
 * 意味着 Room 又多一次显式 Migration，而这里的数据结构只有一个「名字 + 文件」，
 * 文件名编码足以承载，不值得付迁移成本。
 */
object EnrollQueue {

    data class Pending(val name: String, val file: File, val createdAt: Long)

    /** 5 秒以下（16kHz/16bit ≈ 160KB）的样本不足以提取稳定声纹，补交也只会失败 */
    private const val MIN_BYTES = 160_000L

    private fun dir(ctx: Context): File =
        File(ctx.filesDir, "enroll_pending").apply { mkdirs() }

    fun list(ctx: Context): List<Pending> =
        dir(ctx).listFiles()?.mapNotNull { f ->
            if (f.extension != "wav") return@mapNotNull null
            val base = f.nameWithoutExtension
            val ts = base.substringBefore('_').toLongOrNull() ?: return@mapNotNull null
            val raw = base.substringAfter('_', "")
            if (raw.isBlank()) return@mapNotNull null
            val name = runCatching { URLDecoder.decode(raw, "UTF-8") }.getOrDefault(raw)
            Pending(name, f, ts)
        }?.sortedBy { it.createdAt } ?: emptyList()

    fun names(ctx: Context): List<String> = list(ctx).map { it.name }

    /** 入队：同名只保留最新一份，否则重复注册同一人会被补交两次 */
    fun enqueue(ctx: Context, name: String, src: File): File {
        remove(ctx, name)
        val out = File(
            dir(ctx),
            "${System.currentTimeMillis()}_${URLEncoder.encode(name, "UTF-8")}.wav",
        )
        src.copyTo(out, overwrite = true)
        return out
    }

    fun remove(ctx: Context, name: String) {
        list(ctx).filter { it.name == name }.forEach { it.file.delete() }
    }

    /**
     * 把队列里的样本逐个注册到 Mac；成功的删文件，失败的留下等下一轮。
     * 返回成功条数。**调用方必须先确认 Mac 可达**，否则每条都要等满连接超时，
     * 队列一长就把后台任务拖成几分钟。
     */
    suspend fun flush(ctx: Context, client: MacClient): Int {
        var ok = 0
        for (p in list(ctx)) {
            if (!p.file.exists() || p.file.length() < MIN_BYTES) {
                p.file.delete()
                continue
            }
            val (success, _) = client.registerVoiceprint(p.file, p.name)
            if (success) {
                p.file.delete()
                ok++
            }
        }
        return ok
    }
}
