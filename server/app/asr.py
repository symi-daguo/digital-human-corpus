"""ASR 适配层。只动这里，不动管线。

两级后端（2026-09-04 起，按调用 stage 分发）：
- incremental：SenseVoice-Small int8 —— 58x 实时，准实时增量轮秒级出稿。
- final：Fun-ASR-Nano int8（max_token_1024）+ 热词 —— LLM 架构，语义纠错
  （「生死位」→「生死未卜」）与专名热词命中都优于 whisper。
- whisper_cpp：回退后端。任一新后端加载失败自动落回，管线不断。

历史备注：曾预留 qwen3-asr 分支，但依赖（qwen_asr）从未声明、实现从未验证，
按 YAGNI 移除。2026-09-04 用 sherpa-onnx 1.13.7（与 VAD/声纹同栈）实装两级后端。
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_TS_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}\]\s*(.*)$")

# 语言守卫（2026-09-04 用户要求：只处理中文和英文，其他语言暂不处理）。
# 检测日语假名（U+3040-30FF）与韩文谚文（U+AC00-D7A3）。ASR 强制 language="zh"
# 仍可能偶发误判成日语（实测：Fun-ASR-Nano 日终重转把中文说成
# 「もうすぐ ここを…」混进原始稿），这里从结果层兜住。
_KANA_HANGUL_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7a3]")

# ---- ASR 机器标记清理（2026-10-20）--------------------------------------
# sherpa-onnx 的 SenseVoice / Fun-ASR-Nano 会把非语音事件吐成 [noise]、
# <|nospeech|>、<|emo_xxx|> 这类机器标记，原样进稿子很碍眼——实测 10-20
# 有段转写正文全是「[noise] 部项」，读者完全看不懂。
# 在共享解码出口统一清掉，三个后端都受益。
# 刻意只匹配这两种形态：[HH:MM] 时间戳与 [说话人] 标签由 pipeline 后加，
# 不会出现在 ASR 输出里，但正则绝不去碰它们，避免误伤。
_ASR_MARKER_RE = re.compile(r"\[noise\]|<\|[^|]{0,32}\|>")


def strip_asr_markers(text: str) -> str:
    """清掉 ASR 机器标记，并把清理后留下的多余空格规整掉。"""
    if not text:
        return text
    out = _ASR_MARKER_RE.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def looks_non_zh_en(text: str) -> bool:
    """转写结果里出现假名/谚文 = 后端把语音误判成了其他语言。"""
    return bool(_KANA_HANGUL_RE.search(text or ""))


# ---- 重复幻觉抑制（2026-09-17）------------------------------------------
#
# 症状：同一句在一段音频里被吐很多遍，下游照单全收，日报出现大段重复。
# 实测（2026-09-16）：
#   * 一条 31 秒的段，180 字里「对角线固定，这不挺好吗？这。」出现 5 次；
#   * 另一条把「ETS－」吐了 200 多遍（几百字全是幻觉）；
#   * 「这个这个这个…」「本体 本体 本体…」各几百字。
# 三个后端（SenseVoice / Fun-ASR-Nano / whisper）都会，只是频率不同。
#
# 判据分两档，刻意保守——只打「重复」，不打「口吃」：
#   * 长单元（≥4 字）：连续 ≥4 次即判幻觉。自然口语几乎不会把一整句连说 4 遍。
#   * 短单元（1-3 字）：要连续 ≥12 次才判。因为「对对对」「好好好」「这个这个」
#     是人真的会说的，门槛低了会把正常语气词删掉。
# 取**最短满足条件的周期**：这样「对角线固定，这不挺好吗？这。」整体作为周期
# 被识别，而不是被拆成两半。
REPEAT_LONG_UNIT_MIN = 4      # 「长单元」的起点长度
REPEAT_LONG_UNIT_TIMES = 4    # 长单元连续几次算幻觉
REPEAT_SHORT_UNIT_TIMES = 12  # 短单元连续几次算幻觉
REPEAT_MAX_UNIT = 80          # 周期搜索上限（再长就不像幻觉，像正常重复引用）


def collapse_repeats(text: str) -> str:
    """把「同一段连续重复 N 遍」压成 1 遍。

    纯函数、幂等（压缩后的文本再跑一遍不会继续变短）。

    性能：先按「首字符是否相同」剪枝（周期 p 的必要条件是 text[i]==text[i+p]），
    绝大多数 p 在 O(1) 内被排除；再按「剩余长度够不够重复 need 次」二次剪枝。
    实测单条 180 字文本 < 1 ms，全天几百段无感。
    """
    if not text:
        return text
    n = len(text)
    if n < REPEAT_LONG_UNIT_MIN * REPEAT_LONG_UNIT_TIMES:
        return text

    out: list[str] = []
    i = 0
    while i < n:
        hit: tuple[int, int] | None = None
        limit = min(REPEAT_MAX_UNIT, n - i)
        for p in range(1, limit + 1):
            need = (REPEAT_LONG_UNIT_TIMES if p >= REPEAT_LONG_UNIT_MIN
                    else REPEAT_SHORT_UNIT_TIMES)
            if (n - i) // p < need:
                continue
            if i + p >= n or text[i] != text[i + p]:
                continue
            unit = text[i:i + p]
            if not unit.strip():
                continue
            k = 1
            j = i + p
            while j + p <= n and text[j:j + p] == unit:
                k += 1
                j += p
            if k >= need:
                hit = (p, k)
                break
        if hit is None:
            out.append(text[i])
            i += 1
        else:
            p, k = hit
            out.append(text[i:i + p])   # 只留一份
            i += p * k
    return "".join(out)


# 懒加载单例：模型加载要数秒且占内存数百 MB，进程内只加载一次。
# 锁保护首次并发调用（uvicorn 线程池可能同时转写多段）。
_lock = threading.Lock()
_recognizers: dict[str, object] = {}
_failed: set[str] = set()   # 加载失败过的后端：不再反复尝试，直接走 whisper


def transcribe(wav_path: Path, stage: str = "incremental") -> str:
    """对外入口：转写 + 重复幻觉压缩。

    压缩放在最外层而不是塞进三个后端里：它们都会吐重复幻觉、只是频率不同，
    在出口统一掐一次最省事，也保证「这次是哪个后端生效」不影响这一层的行为。
    """
    return collapse_repeats(_transcribe_raw(wav_path, stage))


def _transcribe_raw(wav_path: Path, stage: str = "incremental") -> str:
    """按 stage 选后端转写。stage ∈ {incremental, final}。

    语言守卫：主后端结果含假名/谚文（误判成日/韩语）时，先用 whisper.cpp
    （-l zh 强制中文词表）重转一次；whisper 也拿不出结果才保留原结果，
    由调用方（pipeline）决定丢弃。
    """
    backend = (config.ASR_BACKEND_FINAL if stage == "final"
               else config.ASR_BACKEND_INCREMENTAL)
    fn = {
        "sensevoice": _transcribe_sensevoice,
        "funasr_nano": _transcribe_funasr_nano,
        "whisper": _transcribe_whisper_cpp,
    }.get(backend)
    if fn is None:
        log.error("unknown ASR backend %r, falling back to whisper", backend)
        return _transcribe_whisper_cpp(wav_path)
    if backend in _failed:
        return _transcribe_whisper_cpp(wav_path)
    text = fn(wav_path)
    if text is None:
        return _transcribe_whisper_cpp(wav_path)
    if text and looks_non_zh_en(text) and backend != "whisper":
        # 回退后端未配置时这次重试必然返回空。逐段 WARNING 会随实时采集刷屏
        # （实测 8 行/分钟，且下游 pipeline 的丢弃告警与它一比一成对，纯重复）。
        # 此时降为 debug，由调用方（pipeline）按批汇总上报丢弃段数。
        if not config.WHISPER_MODEL:
            log.debug("ASR backend %s produced non zh/en text (%r); "
                      "whisper fallback unconfigured, keep original",
                      backend, text[:60])
            return text
        log.warning("ASR backend %s produced non zh/en text (%r), retry with whisper",
                    backend, text[:60])
        alt = _transcribe_whisper_cpp(wav_path)
        if alt.strip():
            return alt
    return text


def invalidate() -> None:
    """丢弃已加载的识别器缓存。

    行业切换后 Fun-ASR-Nano 的热词变了，而热词是构建时传进去的（不是每
    次解码的参数）——不重建识别器，热词会一直是旧行业的，表现为「手机上
    显示切到装修了，转写还是按智能家居的词纠错」。
    同时清掉 _failed，让之前加载失败的后端（比如下完新模型后）能重试一次。
    """
    with _lock:
        _recognizers.clear()
        _failed.clear()


def _recognizer(name: str, build):
    """懒加载共享逻辑：首次构建失败会把后端记入 _failed 并返回 None。"""
    if name in _recognizers:
        return _recognizers[name]
    with _lock:
        if name in _recognizers:
            return _recognizers[name]
        try:
            rec = build()
        except Exception as e:
            log.error("ASR backend %s failed to load, will fall back to whisper: %s", name, e)
            _failed.add(name)
            return None
        _recognizers[name] = rec
        log.info("ASR backend %s loaded", name)
        return rec


def _decode(rec, wav_path: Path) -> str:
    """共享解码：16kHz 单声道 WAV（手机端即 16k）→ 纯文本。"""
    import wave

    import numpy as np

    with wave.open(str(wav_path)) as f:
        if f.getnchannels() != 1 or f.getframerate() != 16000:
            log.warning("unexpected wav format %s (channels=%d rate=%d)",
                        wav_path, f.getnchannels(), f.getframerate())
        raw = f.readframes(f.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    s = rec.create_stream()
    s.accept_waveform(16000, samples)
    rec.decode_stream(s)
    return strip_asr_markers(s.result.text)


def _transcribe_sensevoice(wav_path: Path) -> str | None:
    """SenseVoice-Small int8。内部 time-stamp 不输出——原始稿的 [HH:MM]
    时间行由 pipeline 按段 start_offset 注入，与 ASR 无关。"""
    d = config.SENSEVOICE_DIR
    if not (d / "model.int8.onnx").exists():
        log.error("sensevoice model missing at %s", d)
        _failed.add("sensevoice")
        return None

    def build():
        import sherpa_onnx
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=4, language=config.ASR_LANGUAGE, use_itn=True,
        )
    rec = _recognizer("sensevoice", build)
    return _decode(rec, wav_path) if rec is not None else None


def _transcribe_funasr_nano(wav_path: Path) -> str | None:
    """Fun-ASR-Nano int8（max_token_1024）+ 热词。
    llm 必须用 max1024 版：默认 512 上下文下 >20s 音频直接空输出（实测）。"""
    d = config.FUNASR_NANO_DIR
    need = [
        d / "encoder_adaptor.int8.onnx",
        d / config.FUNASR_NANO_LLM,
        d / "embedding.int8.onnx",
        d / "Qwen3-0.6B",
    ]
    if not all(p.exists() for p in need):
        log.error("funasr_nano model files missing under %s", d)
        _failed.add("funasr_nano")
        return None

    def build():
        import sherpa_onnx
        return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=str(d / "encoder_adaptor.int8.onnx"),
            llm=str(d / config.FUNASR_NANO_LLM),
            embedding=str(d / "embedding.int8.onnx"),
            tokenizer=str(d / "Qwen3-0.6B"),
            num_threads=4, language=config.ASR_LANGUAGE,
            hotwords=config.ASR_HOTWORDS,
        )
    rec = _recognizer("funasr_nano", build)
    return _decode(rec, wav_path) if rec is not None else None


_whisper_missing_warned = False


def _transcribe_whisper_cpp(wav_path: Path) -> str:
    """whisper.cpp CLI（回退后端）：stdout 去时间戳前缀。"""
    global _whisper_missing_warned
    if not config.WHISPER_MODEL:
        # 日终全段重转时每个非中英段都会走到这里，不节流就是 ERROR 刷屏
        # （生产未配置 whisper 是常态：失败段保留原后端结果，属预期路径）。
        # 每次进程只提示一次，降为 WARNING。
        if not _whisper_missing_warned:
            _whisper_missing_warned = True
            log.warning("whisper 回退后端未配置（DH_WHISPER_MODEL 为空），"
                        "非中英段将保留原后端结果；本提示每次进程只打一次")
        return ""
    cmd = [
        config.WHISPER_CPP_BIN,
        "-m", config.WHISPER_MODEL,
        "-l", config.ASR_LANGUAGE,
        "-np",                # 不打进度日志
    ]
    # 术语引导：把行业高频词喂给 initial prompt，压低同音词错字（二维码/浏览器/卸载…）。
    # 每段 WAV 是一次独立调用，所以每段都带上；段与段之间没有上下文依赖。
    if config.ASR_PROMPT:
        cmd += ["--prompt", config.ASR_PROMPT]
    cmd.append(str(wav_path))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log.error("whisper.cpp failed: %s", r.stderr[-500:])
            return ""
        # 行格式：[00:00:00.000 --> 00:00:02.040]  文本
        lines = []
        for line in r.stdout.splitlines():
            m = _TS_RE.match(line.strip())
            text = (m.group(1) if m else line).strip()
            if text:
                lines.append(text)
        # 换行拼接：whisper 的每段时间戳行是一条独立语句，用 "" 拼接会让中文
        # 句子粘连（中文无空格分词，下游 LLM 去口气无法断句）。
        return "\n".join(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("whisper.cpp error: %s", e)
        return ""
