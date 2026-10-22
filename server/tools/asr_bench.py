"""ASR 后端 A/B 实测：同一批真实录音，多模型同条件转写对比。

为什么要有这个脚本：换模型不能靠"版本号更新了所以更好"来判断。SenseVoice
的 2024-07-17 与 2025-09-09 两版架构相同、体积几乎一样，光看文件信息没有任何
区别。只有拿真实录音跑一遍才有结论。

没有 ground truth（无人标注的正确答案），所以用三个代理指标综合判断：
  1. empty   —— 空输出率。转不出来 = 这段直接丢，最硬的负面指标。
  2. terms   —— 行业术语命中数。词表里的词在结果中出现多少次，直接反映
                 业务可用性（"网关"和"往关"对日报是两个世界）。
  3. agree   —— 与参照模型（默认 Fun-ASR-Nano，语义纠错最强）的字符重合率。
                两个独立模型都这么写，写错的概率就低；分歧越大越可疑。
  4. rtf     —— 实时倍率。快不快决定能不能放在增量轮。

用法（数据根用 --srv 指定，默认 App 数据目录；在能 import numpy 的解释器下跑）：
    python tools/asr_bench.py --date 2026-09-02 --limit 60
    # 只比 SenseVoice 两版，不做参照
    python tools/asr_bench.py --no-reference
"""
from __future__ import annotations

import argparse
import difflib
import sys
import time
import wave
from pathlib import Path

import numpy as np


def _load_terms(ind_dir: Path, industry: str) -> list[str]:
    f = ind_dir / f"{industry}.txt"
    if not f.is_file():
        return []
    return [s.strip() for s in f.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.startswith("#")]


def _read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as f:
        raw = f.readframes(f.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def make_recognizer(kind: str, model_dir: Path, hotwords: str = ""):
    import sherpa_onnx
    if kind == "sensevoice":
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=4, language="zh", use_itn=True,
        )
    if kind == "funasr_nano":
        return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=str(model_dir / "encoder_adaptor.int8.onnx"),
            llm=str(model_dir / "llm_int8_max1024.onnx"),
            embedding=str(model_dir / "embedding.int8.onnx"),
            tokenizer=str(model_dir / "Qwen3-0.6B"),
            num_threads=4, language="zh", hotwords=hotwords,
        )
    if kind == "fire_red_asr2":
        return sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=4,
        )
    raise SystemExit(f"unknown kind: {kind}")


def run_one(rec, wavs: list[Path]) -> tuple[list[str], float, float]:
    """返回 (文本列表, 音频总秒数, 转写总秒数)。"""
    texts: list[str] = []
    audio_s = 0.0
    t0 = time.time()
    for p in wavs:
        s = _read_wav(p)
        audio_s += len(s) / 16000
        st = rec.create_stream()
        st.accept_waveform(16000, s)
        rec.decode_stream(st)
        texts.append(st.result.text.strip())
    return texts, audio_s, time.time() - t0


def score(name: str, texts: list[str], terms: list[str],
          ref: list[str] | None, audio_s: float, wall_s: float) -> dict:
    non_empty = [t for t in texts if t]
    empty = len(texts) - len(non_empty)
    hit = sum(t.count(w) for t in texts for w in terms)
    agree = 0.0
    if ref:
        sims = [difflib.SequenceMatcher(None, a, b).ratio()
                for a, b in zip(texts, ref)]
        agree = sum(sims) / len(sims) if sims else 0.0
    chars = sum(len(t) for t in texts)
    return {
        "name": name,
        "n": len(texts),
        "empty": empty,
        "empty_pct": 100.0 * empty / len(texts) if texts else 0.0,
        "terms": hit,
        "chars": chars,
        "agree": agree * 100,
        "rtf": (audio_s / wall_s) if wall_s > 0 else 0.0,
        "wall_s": wall_s,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--srv",
                    default=str(Path.home() / "Library/Application Support/DigitalHuman"),
                    help="runtime 根目录（含 models/ data/ industries/）")
    ap.add_argument("--date", default="2026-09-02")
    ap.add_argument("--limit", type=int, default=60, help="最多取多少段（0=全部）")
    ap.add_argument("--industry", default="smart_home")
    ap.add_argument("--no-reference", action="store_true",
                    help="跳过参照模型（Fun-ASR-Nano），只比 SenseVoice 两版")
    ap.add_argument("--extra", nargs="*", default=[],
                    help="额外对比目录，格式 名字=路径（如 2025=.../xxx）")
    args = ap.parse_args()

    srv = Path(args.srv)
    audio_dir = srv / "data" / "audio" / args.date
    wavs = sorted(p for p in audio_dir.glob("*.wav") if p.is_file())
    if not wavs:
        print(f"no wav under {audio_dir}")
        return 1
    if args.limit:
        wavs = wavs[:args.limit]
    terms = _load_terms(srv / "industries", args.industry)
    print(f"音频：{audio_dir}  段数={len(wavs)}  行业词={len(terms)}（{args.industry}）")
    if not terms:
        print("  ! 词表为空，terms 指标不可用")

    models_dir = srv / "models"
    cands: list[tuple[str, str, Path]] = [
        ("SenseVoice-2024-07-17", "sensevoice",
         models_dir / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"),
        ("SenseVoice-2025-09-09", "sensevoice",
         models_dir / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"),
        ("FireRedASR2-AED", "fire_red_asr2",
         models_dir / "fire-red-asr2-zh_en-int8"),
    ]
    for spec in args.extra:
        nm, _, path = spec.partition("=")
        cands.append((nm, "sensevoice", Path(path)))

    hotwords = " ".join(terms)
    results: list[dict] = []
    ref: list[str] | None = None
    if not args.no_reference:
        nano = models_dir / "FunASR-nano-onnx"
        if nano.is_dir():
            print("参照模型：Fun-ASR-Nano（带行业热词）…", flush=True)
            rec = make_recognizer("funasr_nano", nano, hotwords)
            texts, a_s, w_s = run_one(rec, wavs)
            ref = texts
            results.append(score("FunASR-Nano(ref)", texts, terms, None, a_s, w_s))
        else:
            print("  ! 未找到 FunASR-nano-onnx，跳过参照")

    for name, kind, d in cands:
        if not d.is_dir():
            print(f"  ! 跳过 {name}：目录不存在 {d}")
            continue
        print(f"{name}…", flush=True)
        rec = make_recognizer(kind, d)
        texts, a_s, w_s = run_one(rec, wavs)
        results.append(score(name, texts, terms, ref, a_s, w_s))
        globals().setdefault("_T", {})[name] = texts

    print()
    hdr = f"{'模型':<24}{'空转':>8}{'术语命中':>10}{'字数':>9}{'与参照一致':>11}{'实时倍率':>10}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        agree = f"{r['agree']:.1f}%" if r["agree"] else "  -"
        print(f"{r['name']:<24}{r['empty']:>6}段{r['terms']:>10}"
              f"{r['chars']:>9}{agree:>11}{r['rtf']:>9.1f}x")

    # 逐段展示 SenseVoice 两版的分歧，人工可判
    a = globals().get("_T", {}).get("SenseVoice-2024-07-17")
    b = globals().get("_T", {}).get("SenseVoice-2025-09-09")
    if a and b:
        diff = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]
        print(f"\n两版 SenseVoice 有 {len(diff)}/{len(a)} 段结果不同，样例（最多 15 条）：")
        for i, x, y in diff[:15]:
            print(f"  #{i}")
            print(f"    2024: {x[:70] or '(空)'}")
            print(f"    2025: {y[:70] or '(空)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
