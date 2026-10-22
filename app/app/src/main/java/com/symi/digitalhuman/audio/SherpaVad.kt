package com.symi.digitalhuman.audio

import android.content.res.AssetManager
import android.util.Log
import com.k2fsa.sherpa.onnx.SileroVadModelConfig
import com.k2fsa.sherpa.onnx.SpeechSegment
import com.k2fsa.sherpa.onnx.TenVadModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig

/**
 * Silero VAD 封装（sherpa-onnx 1.13.6 Kotlin API）。
 * 内部按 VAD 缓冲切段（[minSilenceDuration] 分段 / [maxSpeechDuration] 强切），
 * 段完成时通过 [nextSegment] 取出，实现崩溃最多丢当前段（≤30s）的 WAL 策略。
 *
 * ## 参数默认值 = 日报那套（2026-09-10 参数化）
 *
 * 四个参数原先写死在构造里，现在提成带默认值的形参，**默认值与原写死值逐位相同**，
 * 所以日报链路的行为没有任何变化。需要另一种切段粒度的调用方用 [forTalk]，
 * 不要自己拼参数——两套参数方向相反，混用会把两边都毁掉（见 [forTalk] 注释）。
 */
class SherpaVad(
    assets: AssetManager,
    threshold: Float = 0.35f,
    minSilenceDuration: Float = 1.0f,
    minSpeechDuration: Float = 0.25f,
    maxSpeechDuration: Float = 30f,
) : AutoCloseable {

    private val vad = Vad(
        assets, VadModelConfig(
            sileroVadModelConfig = SileroVadModelConfig(
                model = "silero_vad.onnx",
                // 0.5 → 0.35：远场拾音灵敏度（2026-09-04 实测对比：0.25 反而显著少出段，
                // 0.35 + 碎段合并是最优组合）。VOICE_RECOGNITION 原始音源在 MIUI 返回
                // 纯静音，无法绕过系统处理。碎段由 CaptureService 的合并缓冲兜底，
                // 误采由三道闸兜底：段时长 ≥2.2s、段平均电平 ≥-43dBFS、Mac 端非人声过滤。
                threshold = threshold,
                // 0.5 → 1.0：远场语音概率在阈值附近抖动，0.5s 静音判定把连续说话
                // 切成 0.4~1.2s 碎段（实测 6 段全部 <2.2s 被时长门槛丢弃，日报零更新）。
                // 拉长到 1.0s 后概率抖动不再切断，段长恢复到正常说话粒度。
                minSilenceDuration = minSilenceDuration,
                minSpeechDuration = minSpeechDuration,
                windowSize = 512,
                maxSpeechDuration = maxSpeechDuration,
            ),
            tenVadModelConfig = TenVadModelConfig(),
            sampleRate = 16000,
            numThreads = 1,
            provider = "cpu",
            debug = false,
        )
    )

    /**
     * 喂入连续 PCM（任意长度，内部按 512 窗口处理）。
     * ⚠️ 每次必须传新数组：实测复用同一 FloatArray 会导致 Silero VAD 永远切不出段
     * （2026-09-04 回归：缓冲复用优化后全天 0 段，回退即恢复——JNI 层对传入数组
     * 的生命周期有假设，不能复用）。性能损耗可接受（512 float ≈ 2KB / 32ms）。
     */
    fun feed(samples: FloatArray) {
        vad.acceptWaveform(samples)
    }

    /** 会话结束/暂停时冲刷缓冲，产出尾段 */
    fun flush() = vad.flush()

    val hasSegment: Boolean get() = !vad.empty()

    fun nextSegment(): SpeechSegment? =
        if (vad.empty()) null else vad.front()

    fun pop() = vad.pop()

    override fun close() {
        try { vad.release() } catch (e: Exception) { Log.w(TAG, "vad release", e) }
    }

    companion object {
        private const val TAG = "SherpaVad"

        /** 对话静音判定：0.45s。短于日报的 1.0s，让「说完」尽快变成一段。 */
        const val TALK_MIN_SILENCE = 0.45f

        /** 对话最短起段：0.15s。低于日报的 0.25s，「大锅」这种两音节也能起段。 */
        const val TALK_MIN_SPEECH = 0.15f

        /** 对话最长段：8s。实时对话是问答，不该出现 30 秒的连续独白。 */
        const val TALK_MAX_SPEECH = 8f

        /**
         * 对话专用 VAD：**参数方向与日报那套相反**，两者绝不能互相复用。
         *
         * | 维度 | 日报 | 对话 |
         * |---|---|---|
         * | 静音判定 | 1.0s（远场概率抖动会切碎长句） | [TALK_MIN_SILENCE] 0.45s（说完立刻出段） |
         * | 最短起段 | 0.25s | [TALK_MIN_SPEECH] 0.15s（「大锅」两个音节也起段） |
         * | 最长段 | 30s | [TALK_MAX_SPEECH] 8s |
         *
         * 为什么必须分开：日报的段要过 `CaptureService.MIN_SEGMENT_MS=2200` 门槛
         * 和 5s 碎段合并，而「大锅，开灯」整句只有约 1.2s——走日报 VAD 会被时长
         * 门槛直接丢弃，表现为 AUTO 模式喊了管家完全没反应（2026-09-10 定位的根因）。
         *
         * ⚠️ 两个 VAD 实例必须**各自喂独立的 FloatArray**：sherpa JNI 对传入数组的
         * 生命周期有假设（复用会让 VAD 永远切不出段，2026-09-04 回归）。共用同一个
         * 数组的后果没有实测过，按保守处理，各喂一份拷贝。
         *
         * [maxSpeechSeconds] 可覆盖：手动录音要「说完自动停」，上限比 AUTO 宽松些。
         */
        fun forTalk(assets: AssetManager, maxSpeechSeconds: Float = TALK_MAX_SPEECH) = SherpaVad(
            assets,
            threshold = 0.35f,
            minSilenceDuration = TALK_MIN_SILENCE,
            minSpeechDuration = TALK_MIN_SPEECH,
            maxSpeechDuration = maxSpeechSeconds,
        )
    }
}

/** Short PCM → Float [-1,1] 转换 */
fun ShortArray.toFloatPcm(): FloatArray {
    val out = FloatArray(size)
    for (i in indices) out[i] = this[i] / 32768f
    return out
}

/** Float 段 → Short PCM */
fun FloatArray.toShortPcm(): ShortArray {
    val out = ShortArray(size)
    for (i in indices) out[i] = (this[i] * 32767f).toInt().coerceIn(-32768, 32767).toShort()
    return out
}
