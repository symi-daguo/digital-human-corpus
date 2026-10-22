"""非人声检测：sherpa-onnx AudioTagging（Zipformer + AudioSet 527 类）。

用途：区分「人声」与「非人声」（音乐/键盘/环境噪声/静默）。
电视/音箱里的人声本质仍是「人声」，会被判为 speech 保留——这正是产品诉求
（要忽略的是音乐/噪声，而非「非本人的话」）。

引擎未配置或异常时返回 True（当作人声），不误杀。
"""
from __future__ import annotations

import logging
import struct
import wave
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

_tagger = None

# AudioSet 中与「人类语音」直接相关的标签（display_name）
SPEECH_LABELS = {
    "Speech",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Child speech, kid speaking",
    "Conversation",
    "Narration, monologue",
    "Chatter",
    "Babbling",
    "Speech synthesizer",
    "Whispering",
    "Hubbub, speech noise, speech babble",
    "Shout",
    "Bellow",
    "Yell",
    "Whoop",
}

# 明确的非人声标签（仅用于日志归类，不参与判定）
NON_SPEECH_LABELS = {
    "Music", "Singing", "Male singing", "Female singing", "Child singing",
    "Synthetic singing", "Singing bowl", "Musical instrument",
    "Background noise", "Static", "Rain", "Traffic noise, roadway noise",
    "Engine", "Wind noise (microphone)",
}


def _get_tagger():
    global _tagger
    if _tagger is not None:
        return _tagger
    model = config.AUDIO_TAGGING_MODEL
    labels = config.AUDIO_TAGGING_LABELS
    if not model or not Path(model).exists():
        log.warning("audio tagging model not configured; non-speech detection disabled")
        return None
    if not labels or not Path(labels).exists():
        log.warning("audio tagging labels not configured; non-speech detection disabled")
        return None
    from sherpa_onnx import (
        AudioTagging,
        AudioTaggingConfig,
        AudioTaggingModelConfig,
        OfflineZipformerAudioTaggingModelConfig,
    )

    cfg = AudioTaggingConfig(
        model=AudioTaggingModelConfig(
            zipformer=OfflineZipformerAudioTaggingModelConfig(model=model),
            num_threads=1,
            debug=False,
            provider="cpu",
        ),
        labels=labels,
        top_k=10,
    )
    if not cfg.validate():
        log.error("audio tagging config invalid: %s", model)
        return None
    _tagger = AudioTagging(cfg)
    return _tagger


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), sr


# CED 单次推理的音频上限（秒）。
#
# 2026-09-14 实测故障：一段 42.9s 的音频让 CED 在 self_attn 的 Reshape 节点上抛
#   RuntimeError: ... Input shape:{1,140,16}, requested shape:{-1,4279,4,4}
# 4279 帧对应约 42.8s —— 正好是超出上限的那一段。引擎内部按 4x4 patch 切分时
# 441 个 token 除不尽，于是 reshape 失败。同一次运行里 30.6s 的段（2522.wav）
# 是能过的，所以上限落在 30~43s 之间，取 30s 留足余量。
_MAX_TAG_SEC = 30.0


def _label_events(events) -> tuple[bool, str]:
    """把 events 归组成 (是否人声, 日志摘要)。"""
    speech_prob = 0.0
    non_speech_prob = 0.0
    top_name, top_prob = "", 0.0
    for e in events:
        if e.prob > top_prob:
            top_name, top_prob = e.name, e.prob
        if e.name in SPEECH_LABELS:
            speech_prob += e.prob
        elif e.name in NON_SPEECH_LABELS:
            non_speech_prob += e.prob
    is_s = speech_prob >= config.AUDIO_TAGGING_SPEECH_THRESHOLD
    detail = (f"speech={speech_prob:.3f} non_speech={non_speech_prob:.3f} "
              f"top={top_name}({top_prob:.3f})")
    return is_s, detail


def is_speech(wav_path: Path) -> bool:
    """判断一段音频是否为「人声」。异常/引擎不可用返回 True（不误杀）。"""
    tagger = _get_tagger()
    if tagger is None:
        return True
    try:
        samples, sr = _read_wav_mono(wav_path)
    except (OSError, ValueError) as e:
        log.warning("audio tagging read failed %s: %s", wav_path, e)
        return True
    if sr != 16000:
        log.warning("audio tagging expects 16k, got %d in %s; treat as speech", sr, wav_path)
        return True
    if samples.size == 0:
        return False

    name = Path(wav_path).name
    dur = samples.size / sr
    # 超长音频必须切块送——见 _MAX_TAG_SEC 的说明。**不切会打断整条管线**：
    # 2026-09-14 那次崩溃让当天 131 段全部停在待判定，sweeper 每 10 分钟重试
    # 仍连崩 3 次，用户侧表现就是「Mac 开机了但手机一直显示待判定」。
    chunks = int(np.ceil(dur / _MAX_TAG_SEC)) if dur > _MAX_TAG_SEC else 1
    step = int(np.ceil(samples.size / chunks))

    results: list[tuple[bool, str]] = []
    for i in range(chunks):
        seg = samples[i * step:(i + 1) * step]
        if seg.size == 0:
            continue
        try:
            stream = tagger.create_stream()
            stream.accept_waveform(sample_rate=sr,
                                   waveform=np.ascontiguousarray(seg, dtype=np.float32))
            events = tagger.compute(stream)
        except Exception as e:
            # 引擎内部异常一律当人声（不误杀），**并且绝不能让它冒泡** ——
            # 单段坏音频不该毁掉当天全部数据的处理。
            log.warning("audio tagging failed %s chunk %d/%d: %s; treat as speech",
                        name, i + 1, chunks, e)
            return True
        results.append(_label_events(events))

    if not results:
        return True
    # 任一块是人声即为整段人声：切块后只要有一段在说话，这段就有内容，
    # 丢掉它会漏掉真实发言（宁可多转写，不可漏）。
    is_s = any(ok for ok, _ in results)
    log.info("audio tagging %s (%.1fs, %d chunk) -> %s => %s",
             name, dur, chunks, " | ".join(d for _, d in results),
             "speech" if is_s else "NON-SPEECH")
    return is_s
