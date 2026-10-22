package com.symi.digitalhuman.audio

import android.content.res.AssetManager
import android.util.Log
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/**
 * 本地离线唤醒词检测（sherpa-onnx `KeywordSpotter`，2026-09-10）。
 *
 * ## 它解决什么
 *
 * AUTO 模式下每一段对话音频都要「上传 Mac → 服务端 ASR → 判唤醒词」，一天下来
 * 上传与转写次数是**全部语音段**的量级，而真正喊了「大锅」的可能只有几十次。
 * 在手机上先跑一遍专用的唤醒词模型，不含唤醒词的段直接丢掉，省掉绝大部分
 * 上传流量与服务端转写。模型只有 4.6MB（int8 量化），常驻内存无压力。
 *
 * ## 为什么是「粗筛」而不是「终判」
 *
 * 2026-09-10 用 19 个含唤醒词的样本 + 199 段真实语料做过阈值标定：
 *
 * | 阈值 | 正样本命中 | 真实语料误报 |
 * |---|---|---|
 * | 0.25 | 2/2 | 2/199 |
 * | 0.30 | 2/2 | 2/199 |
 * | 0.35 | 2/2 | 2/199 |
 * | 0.40 | 1/2 ❌ | 2/199 |
 * | 0.60 | 0/2 ❌ | 0/199 |
 *
 * **不存在同时零漏报、零误报的阈值**（误报源之一是 31 秒长段里的「大部分」）。
 * 所以这里取 0.30（宁可多送一次也不能漏掉唤醒），而把最终判定留给服务端的
 * `config.has_talk_alias`——它面对的是全文转写，判得准得多。误报的代价只是
 * 一段音频白跑一次 ASR（约 130ms），漏报的代价是「喊了管家不理人」。
 *
 * ## 线程
 *
 * 单实例非线程安全（内部 `OnlineStream` 有状态）。调用方
 * （`CaptureService` 的对话段处理协程）保证串行调用。一次 `detect` 对 1.2 秒
 * 音频约 18ms、对 8 秒音频约 120ms，**不要放在录音循环里同步调用**。
 */
class KeywordGate(assets: AssetManager) : AutoCloseable {

    private val spotter: KeywordSpotter = run {
        // 先做 Java 侧格式自检，再交给 native —— 顺序不能反，原因见 validateKeywords
        validateKeywords(assets)?.let {
            throw IllegalArgumentException("唤醒词表格式不合法：$it")
        }
        KeywordSpotter(
            assets,
            KeywordSpotterConfig(
                featConfig = FeatureConfig(sampleRate = SR, featureDim = 80),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "$DIR/encoder.onnx",
                        decoder = "$DIR/decoder.onnx",
                        joiner = "$DIR/joiner.onnx",
                    ),
                    tokens = "$DIR/tokens.txt",
                    numThreads = 1,
                    provider = "cpu",
                ),
                maxActivePaths = 4,
                keywordsFile = "$DIR/keywords.txt",
                keywordsScore = SCORE,
                keywordsThreshold = THRESHOLD,
                numTrailingBlanks = 1,
            )
        )
    }

    /**
     * 判定一段 16kHz 单声道 PCM 里是否含唤醒词。
     *
     * @return 命中的显示名（如「大锅」）；未命中返回 null。
     *   非唤醒词导致的关键词为空串也算未命中。
     */
    fun detect(samples: FloatArray): String? {
        if (samples.isEmpty()) return null
        val stream = spotter.createStream("")
        return try {
            stream.acceptWaveform(samples, SR)
            stream.inputFinished()
            var hit: String? = null
            // isReady 在 inputFinished 后仍要一直解码到缓冲耗尽，否则尾音里的
            // 唤醒词会被丢掉——「大锅」正好容易落在段尾。
            while (spotter.isReady(stream)) {
                spotter.decode(stream)
                val r = spotter.getResult(stream)
                if (r.keyword.isNotBlank()) {
                    hit = r.keyword
                    break
                }
                spotter.reset(stream)
            }
            hit
        } catch (e: Exception) {
            Log.w(TAG, "唤醒词检测失败", e)
            null
        } finally {
            runCatching { stream.release() }
        }
    }

    override fun close() {
        runCatching { spotter.release() }
    }

    companion object {
        private const val TAG = "KeywordGate"

        /** assets 下的模型目录。 */
        private const val DIR = "kws"
        private const val SR = 16000

        /**
         * 关键词阈值。0.30 是标定出来的保守点（见类注释的表格）：
         * 再高就开始漏掉正样本，再低只会增加误报（误报只浪费一次 ASR）。
         */
        private const val THRESHOLD = 0.30f

        /** 关键词加权分。1.0 与标定实验一致，调大会压制非关键词路径。 */
        private const val SCORE = 1.0f

        /**
         * 唤醒词表预校验，**必须在把文件交给 native 之前跑**。
         *
         * sherpa-onnx 的 C++ 解析器对格式错误零容忍：`keywords.txt` 里出现任何它不
         * 认识的 token（**包括 `#` 开头的注释行——该格式不支持注释**），`std::stof`
         * 就抛 `std::invalid_argument`，而这个异常跨不出 JNI 边界，会直接变成
         * SIGABRT 杀掉进程。2026-09-10 就是这么崩的（见 pitfalls 第十二章）：
         * Kotlin 侧 try/catch 完全拦不住，App 反复崩溃到被系统判定
         * 「crashed too many times」，采集服务被延后 90 分钟才重启。
         *
         * 所以这里在 Java 侧把同样的规则先查一遍，把「进程崩溃」降级为
         * 「构造抛异常 → 调用方捕获 → 本次会话退化为直接上传」。
         *
         * @return 错误描述；合法时返回 null。
         */
        private fun validateKeywords(assets: AssetManager): String? = try {
            // tokens.txt 是「发音 token + 数字 id」两列（如 `uō 145`），只取第一列。
            // 若误把整行当 token，校验会把所有关键词判成非法 → KWS 被静默关掉。
            val tokens = assets.open("$DIR/tokens.txt").bufferedReader().use { r ->
                r.readLines().mapNotNull { it.trim().substringBefore(' ').takeIf { t -> t.isNotEmpty() } }
                    .toHashSet()
            }
            if (tokens.isEmpty()) {
                "tokens.txt 为空或读不到内容"
            } else {
                val lines = assets.open("$DIR/keywords.txt").bufferedReader().use { it.readLines() }
                var bad: String? = null
                var ok = 0
                for (i in lines.indices) {
                    val line = lines[i].trim()
                    if (line.isEmpty()) continue          // 空行 native 会跳过，无风险
                    if (line.startsWith("#")) {
                        bad = "第 ${i + 1} 行是注释，该格式不支持注释：$line"
                        break
                    }
                    // 显示名在最后一个 @ 之后（发音 token 里不会出现 @）
                    val at = line.lastIndexOf('@')
                    if (at <= 0 || at == line.length - 1) {
                        bad = "第 ${i + 1} 行缺少「发音 @显示名」结构：$line"
                        break
                    }
                    val missing = line.substring(0, at).trim()
                        .split(' ', '\t').filter { it.isNotEmpty() && it !in tokens }
                    if (missing.isNotEmpty()) {
                        bad = "第 ${i + 1} 行含 tokens.txt 里没有的发音：" +
                            "${missing.joinToString(" ")} —— 需改用 tokens.txt 中的等价写法"
                        break
                    }
                    ok++
                }
                when {
                    bad != null -> bad
                    ok == 0 -> "keywords.txt 里没有有效关键词行"
                    else -> null
                }
            }
        } catch (e: Exception) {
            "读取唤醒词模型失败：${e.message}"
        }
    }
}
