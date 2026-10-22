"""TTS：把 Hermes 回复合成为 16kHz 单声道 WAV，供手机端直接播放。

四级后端（`auto` 模式按顺序探测，取第一个可用的）：

0. **ming**     —— 蚂蚁 Ming-3.0-tts-0.5B（MLX 8bit，MIT，2026-10-22 接入）。
                   **发音鲁棒性第一**（Artificial Analysis 93.2% 并列第一），
                   专治「数字 / 英文专名念错」。实测 RTF 0.60、峰值内存 1.9GB。
1. **piper**    —— 音质尚可、可离线。需要 `DH_PIPER_BIN` + `DH_PIPER_MODEL` 都指到
                   真实文件（模型是 .onnx + .onnx.json 一对）。
2. **say**      —— macOS 原生 `say` + `afconvert`，零依赖。**没装 ming 时的默认后端**。
3. **edge**     —— 需要 `pip install edge-tts`（走微软在线接口，需联网）。

> ming 的取舍：进程内常驻（首次加载 3~4s），空闲 `DH_TTS_MING_IDLE_UNLOAD` 秒后
> 自动卸载释放 1.9GB；合成 RTF 0.60 意味着 24 秒音频约 14 秒出（含加载）。
> 对话场景若嫌慢，把 `DH_TTS_BACKEND=say` 即可退回零延迟的机器人音。

## 为什么默认落到 say，而不是规格里的 Piper

规格（docs/hermes-voice-conversation-2026-09-10.md §三.3）首选 Piper、兜底 Edge TTS。
但 2026-09-10 实测本机 **两者都没安装**（`piper not found`、`edge-tts not found`），
而实时对话链路的第一优先级是「先能出声」。`say` 随 macOS 自带、中文声音 Tingting
音质可接受，于是把它做成**探测链里的中间层**：装上 Piper 后只要填两个配置项就能
自动切过去，talk.py / 客户端代码一行都不用改。

## 声音名的坑（实测）

`say -v Eddy`（Flo / Grandma / Grandpa 同理）属于 Apple 的「新奇声音」，未下载时
`say` **退出码 0、不报错，但输出 0.02 秒的空音频**（256 帧）——手机上表现为「点了没反应」，
极难排查。所以默认声音写死 `Tingting`（经典中文女声，系统自带）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

# 目标格式：与手机端录音、ASR 输入、声纹提取完全一致，避免任何重采样环节。
_TARGET_RATE = 16000
_TARGET_CHANNELS = 1

# 两套缓存：普通（质量优先，可用 ming）与 fast（延迟优先，跳过 ming）
_backend_cache: dict[bool, str | None] = {}

# ---- Ming-3.0-tts 常驻状态（2026-10-22）---------------------------------
# 为什么要常驻：CLI 每次冷启要 3~4s（加载 1.5GB 权重），播报/对话都受不了。
# 为什么不一直占着：模型 1.9GB，空闲超过 _MING_IDLE_UNLOAD_SEC 就卸载。
_MING_IDLE_UNLOAD_SEC = 600.0   # 保留常量供文档引用；实际不再常驻
# 子进程合成要串行吗：不必。每次是独立进程，天然隔离；
# 但并发太多会把内存打爆，用信号量限制到 1 路。
_ming_lock = threading.Semaphore(1)


def _has(binary: str) -> bool:
    """可执行文件是否存在（绝对路径直接判存在，裸名字走 PATH 查找）。"""
    if not binary:
        return False
    if "/" in binary:
        return Path(binary).is_file()
    return shutil.which(binary) is not None


def _ming_dir() -> Path:
    return Path(config.TTS_MING_DIR)


def _ming_ready() -> bool:
    """模型文件齐 + mlx-audio 可导入，才算可用。"""
    d = _ming_dir()
    if not (d / "model.safetensors").is_file():
        return False
    try:
        import mlx_audio.tts.generate  # noqa: F401
    except Exception:
        return False
    return True


def _synth_ming(text: str, out_path: Path) -> bool:
    """Ming-3.0-tts：**每次起子进程**合成，进程退出即彻底归还内存。

    为什么不用「进程内常驻 + 空闲卸载」——实测走不通（2026-10-22）：
    模型 `del` 掉、`gc.collect()` + `mx.clear_cache()` 之后，
    `mx.get_active_memory()` 仍停在 1581MB、RSS 仍 1763MB，
    说明内部还有引用没放开，进程内根本还不掉这 1.9GB。
    子进程方案让 OS 在退出时回收全部内存，代价是每次多花 3~4s 加载。

    所以分工是：**日报播报用 ming（低频、可等），实时对话用 say（零延迟）**
    —— 由 synthesize(..., fast=True) 区分，见 backend()。

    两个实测坑：
    1. `--lang_code zh` 必须显式传：默认 en 时同一句中文读错且慢 2.5 倍。
    2. CLI 的 `--output` 是**目录**，产物是 `<prefix>_000.wav`。
    """
    tmpdir = out_path.parent / f".ming-{os.getpid()}-{abs(hash(text)) % 100000}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            sys.executable, "-m", "mlx_audio.tts.generate",
            "--model", str(_ming_dir()),
            "--text", text,
            "--lang_code", config.TTS_MING_LANG or "zh",
            "--output", str(tmpdir),
            "--file_prefix", "ming",
        ]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(60, int(len(text) * 2.5)))
        if r.returncode != 0:
            log.warning("Ming TTS 子进程失败（rc=%d）：%s",
                        r.returncode, (r.stderr or "")[-300:].strip())
            return False
        produced = sorted(tmpdir.glob("*.wav"))
        if not produced:
            log.warning("Ming TTS 没有产出文件（text=%r）", text[:40])
            return False
        ok = _to_wav(produced[0], out_path)
        log.info("Ming TTS 合成完成：%d 字 → %.1fs（含加载）", len(text), time.time() - t0)
        return ok
    except subprocess.TimeoutExpired:
        log.warning("Ming TTS 超时（%d 字）", len(text))
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def reap_idle() -> bool:
    """历史遗留接口（进程内常驻方案已废弃，恒返回 False）。

    保留函数名是为了不让 llm.backend_window_manager 的调用点报错；
    Ming 现在走子进程，用完即还，不需要空闲回收。
    """
    return False


def _piper_ready() -> bool:
    return (
        _has(config.PIPER_BIN or "piper")
        and bool(config.PIPER_MODEL)
        and Path(config.PIPER_MODEL).is_file()
    )


def _say_ready() -> bool:
    return _has("say") and _has("afconvert")


def _edge_ready() -> bool:
    """edge-tts 可能是 CLI，也可能是 Python 包（两者都认）。"""
    if _has("edge-tts"):
        return True
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def backend(fast: bool = False) -> str:
    """返回实际生效的后端名。结果进程内缓存（配置改了要重启）。

    `fast=True`（实时对话）**跳过 ming**：ming 每次要起子进程加载 1.5GB 权重
    （3~4s），而对话回复只有几十字，等 4 秒出声体验很差。对话走 say 零延迟，
    日报播报（低频、文本长、能等）才用 ming —— 这是刻意的分工，不是降级。
    """
    if fast in _backend_cache:
        return _backend_cache[fast] or ""

    want = (config.TTS_BACKEND or "auto").lower()
    if fast and want == "ming":
        # 显式配了 ming 但这是对话场景：照样走 say，并说明一次
        if _backend_cache.get(True) is None:
            log.info("TTS fast 路径跳过 ming（对话要低延迟），改用系统 say")
        want = "say"
    if want in ("ming", "piper", "say", "edge"):
        # 显式指定：可用就用，不可用则降级并在日志里说明原因，
        # 绝不静默返回一个「成功但没声音」的结果。
        ready = {"ming": _ming_ready, "piper": _piper_ready,
                 "say": _say_ready, "edge": _edge_ready}[want]
        if ready():
            _backend_cache[fast] = want
            return want
        log.warning("TTS 后端 %r 不可用（配置缺失或未安装），降级探测：%s",
                    want, _fallback_reason())
    elif want != "auto":
        log.warning("TTS 后端配置值 %r 无法识别，按 auto 处理", want)

    # ming 排最前：它是唯一能念对数字/英文专名的后端（见模块文档头）。
    # 模型不在或 mlx-audio 没装时 _ming_ready() 返回 False，自动落到后面的链。
    chain = (("piper", _piper_ready), ("say", _say_ready), ("edge", _edge_ready))
    if not fast:
        # ming 排最前：唯一能念对数字/英文专名的后端（见模块文档头）。
        # 模型不在或 mlx-audio 没装时 _ming_ready() 返回 False，自动落到后面的链。
        chain = (("ming", _ming_ready),) + chain
    for name, ready in chain:
        if ready():
            _backend_cache[fast] = name
            log.info("TTS 后端：%s（fast=%s）", name, fast)
            return name

    _backend_cache[fast] = ""
    log.warning("没有可用的 TTS 后端——对话会返回文字但无声（say 缺失说明不在 macOS 上）")
    return ""


def _fallback_reason() -> str:
    return (f"piper={_piper_ready()} say={_say_ready()} edge={_edge_ready()}")


def reset_cache() -> None:
    """配置热更新后重新探测后端（控制台保存 TTS 配置时调用）。

    顺带释放常驻的 Ming 模型：换了 `DH_TTS_MING_DIR` 还占着旧模型，
    下次合成会以为「已加载」而用错路径。
    """
    _backend_cache.clear()


def synthesize(text: str, out_path: Path, fast: bool = False) -> Path | None:
    """合成语音，写出 16kHz 单声道 16bit WAV。失败返回 None（不抛异常）。

    调用方（talk.py）拿到 None 时仍然返回文字回复——**有字无声**远好于整轮失败。

    `fast=True`：实时对话场景，跳过 ming 走 say（零延迟）。见 backend()。
    """
    text = (text or "").strip()
    if not text:
        return None

    name = backend(fast=fast)
    if not name:
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if name == "ming":
            ok = _synth_ming(text, out_path)
        elif name == "piper":
            ok = _synth_piper(text, out_path)
        elif name == "say":
            ok = _synth_say(text, out_path)
        else:
            ok = _synth_edge(text, out_path)
    except Exception as e:
        log.warning("TTS 合成异常（%s）：%s", name, e)
        return None

    if not ok or not out_path.is_file() or out_path.stat().st_size <= 44:
        # 44 字节 = 只有 WAV 头没有采样数据，正是「新奇声音」那种静默失败的特征
        log.warning("TTS 合成没有产出有效音频（后端=%s，文本=%r）", name, text[:40])
        return None
    return out_path


# --------------------------------------------------------------------------
# 各后端实现：统一产出 16k mono WAV，非 16k 的一律经 afconvert 归一
# --------------------------------------------------------------------------
def _synth_say(text: str, out_path: Path) -> bool:
    """macOS 原生。say 输出 AIFF，再用 afconvert 转 16k mono WAV。"""
    with tempfile.TemporaryDirectory(prefix="dh-tts-") as td:
        aiff = Path(td) / "raw.aiff"
        cmd = ["say", "-o", str(aiff)]
        if config.TTS_VOICE:
            cmd += ["-v", config.TTS_VOICE]
        if config.TTS_RATE:
            cmd += ["-r", str(config.TTS_RATE)]
        # `--` 分隔符：回复文本可能是 "-5度" 之类以减号开头的串，
        # 不加会被 say 当成命令行选项。
        cmd += ["--", text]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log.warning("say 失败：%s", (r.stderr or "").strip()[:200])
            return False
        if not aiff.is_file() or aiff.stat().st_size == 0:
            return False
        return _to_wav(aiff, out_path)


def _synth_piper(text: str, out_path: Path) -> bool:
    """piper 从 stdin 读文本，写 WAV（采样率随模型，多为 22050Hz）。"""
    binary = config.PIPER_BIN or "piper"
    with tempfile.TemporaryDirectory(prefix="dh-tts-") as td:
        raw = Path(td) / "raw.wav"
        cmd = [binary, "-m", config.PIPER_MODEL, "-f", str(raw)]
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log.warning("piper 失败：%s", (r.stderr or "").strip()[:200])
            return False
        if not raw.is_file() or raw.stat().st_size == 0:
            return False
        return _to_wav(raw, out_path)


def _synth_edge(text: str, out_path: Path) -> bool:
    """edge-tts 出 mp3，同样要转码。声音名是 zh-CN-XiaoxiaoNeural 这类，
    与 say 的声音名不同，所以这里不传 config.TTS_VOICE。"""
    with tempfile.TemporaryDirectory(prefix="dh-tts-") as td:
        mp3 = Path(td) / "raw.mp3"
        cmd = ["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
               "--text", text, "--write-media", str(mp3)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log.warning("edge-tts 失败：%s", (r.stderr or "").strip()[:200])
            return False
        if not mp3.is_file() or mp3.stat().st_size == 0:
            return False
        return _to_wav(mp3, out_path)


def _to_wav(src: Path, dst: Path) -> bool:
    """把任意音频转成 16kHz / 单声道 / 16bit PCM WAV。

    用 afconvert 而不是自己写重采样：它随 macOS 自带，且这里是整段离线转码，
    不涉及流式延迟，没必要引入 soundfile/librosa 之类的新依赖。
    """
    cmd = ["afconvert", "-f", "WAVE", "-d", f"LEI16@{_TARGET_RATE}",
           "-c", str(_TARGET_CHANNELS), str(src), str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log.warning("afconvert 失败：%s", (r.stderr or "").strip()[:200])
        return False
    return dst.is_file() and dst.stat().st_size > 44


def health() -> dict:
    """给 /api/talk/health 用的自检信息。"""
    return {
        "backend": backend() or None,
        "voice": config.TTS_VOICE if backend() == "say" else None,
        "ming_ready": _ming_ready(),
        "ming_loaded": False,   # 子进程模式：用完即还，不存在常驻实例
        "ming_dir": str(_ming_dir()),
        "piper_ready": _piper_ready(),
        "say_ready": _say_ready(),
        "edge_ready": _edge_ready(),
    }
