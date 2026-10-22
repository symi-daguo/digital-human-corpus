#!/usr/bin/env python3
"""声纹判定自检：回答「我提交的声纹到底匹配上了没有」。

为什么要有这个脚本（2026-09-10 的真实教训）：

`identify()` 只在命中**具名锚点**时才回 `is_user=True`，命中匿名簇则回
`hit in SELF_ALIASES` = False，也就是把本人判成**他人**。于是同一个人的声音
会被拆成「本人 / 说话人18 / 说话人27」三个身份，而单看某一轮的日志完全看不出
是「锚点不够」还是「引擎坏了」。

本脚本一次给三样东西，分开回答三个不同的问题：

  1. `--audit`   —— **锚点够不够**：每个持久簇与每个锚点的余弦。
     建议只信「可信锚点」（register + voiceprint）那一列，理由见 audit() 的注释。
  2. 逐段判定    —— **引擎通不通**：对某天的真实采集音频逐个跑 identify()，
     看整天分布。单个例子说明不了问题；成片失败才是引擎问题。
  3. 时间线      —— **注册生效了吗**：把当天的段按 `--since`（注册时刻）切成前/后，
     分别统计。若注册后本人语音 100% 通过，历史那些「说话人N」就只是陈年标签。

音频位置：`<data>/tenants/<tenant>/corpus/<date>/wavs/<seg_id>.wav`。
注意 `uploaded_segment.path` 是**空串**，别被它误导以为音频被清理了。

用法（用应用内嵌解释器，不要用系统 python）：

    R=/Applications/数字人服务.app/Contents/Resources
    DH_DATA_DIR="$HOME/Library/Application Support/DigitalHuman/data" \
      PYTHONPATH="$R/site-packages" "$R/python/bin/python3" \
      tools/voiceprint_check.py --date 2026-09-10

    # 只看锚点体检，不跑音频
    ... tools/voiceprint_check.py --audit --audit-only
    # 指定要切的注册时刻（判断注册是否立即生效）
    ... tools/voiceprint_check.py --date 2026-09-10 --since "2026-09-10 17:26:14"
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import struct
import sys
from pathlib import Path

# 独立运行时（不在 app 包内）先兜住数据目录，再导入 app —— config 在 import 时
# 就会 _load_env_file()，读 env.local 并把 $VAR 展开，所以必须在导入前设好。
#
# ⚠️ 必须同时设 DH_ENV_FILE，否则会读到**代码目录里那份过期的 server/env.local**：
#    _env_file_candidates() 的顺序是 ① DH_ENV_FILE ② 代码目录 ③ 数据目录上一级，
#    而从仓库跑这个脚本时 ② 恰好存在（是开发期留下的旧配置），于是它赢了 ——
#    表现为 DH_SELF_ALIASES 丢失、本人被误判成「他人」，看起来像引擎坏了。
#    打包部署不受影响：Swift 外壳会注入 DH_ENV_FILE（见 ServiceManager.swift），
#    且 bundle 内没有 env.local。
_DEFAULT_ROOT = Path.home() / "Library/Application Support/DigitalHuman"
if not os.environ.get("DH_DATA_DIR") and (_DEFAULT_ROOT / "data").is_dir():
    os.environ["DH_DATA_DIR"] = str(_DEFAULT_ROOT / "data")
if not os.environ.get("DH_ENV_FILE") and (_DEFAULT_ROOT / "env.local").is_file():
    os.environ["DH_ENV_FILE"] = str(_DEFAULT_ROOT / "env.local")

_REPO_SERVER = Path(__file__).resolve().parents[1]
if str(_REPO_SERVER) not in sys.path:
    sys.path.insert(0, str(_REPO_SERVER))

from app import config, store, voiceprint   # noqa: E402


def _vec(raw: bytes) -> list[float]:
    return list(struct.unpack(f"{len(raw) // 4}f", raw[: (len(raw) // 4) * 4]))


def cmd_audit(tenant: str, limit: int) -> None:
    rep = voiceprint.audit(tenant)
    thr, gray = rep["threshold"], rep["gray_band"]
    print("=" * 78)
    print(f"锚点体检  阈值={thr}  灰区=≥{thr - gray:.2f}  超大簇阈值={rep['huge_count']}")
    print(f"可信锚点={rep['trusted_anchors']}  锚点明细="
          f"{[(a['name'], a['source'], a['count']) for a in rep['anchors']]}")
    print(f"持久簇 {rep['cluster_total']} 个")
    print(f"SELF_ALIASES={rep['self_aliases']}")
    print("-" * 78)
    print(f"{'簇':<12}{'段数':>6}{'可信':>8}{'当前判':>8}  标记")
    for c in rep["clusters"][: max(1, limit)]:
        tags = []
        if c["suggest"]:
            tags.append("建议归并")
        if c["borderline"]:
            tags.append("灰区")
        if c["huge"]:
            tags.append("超大簇·疑多人")
        if c.get("is_role_self"):
            tags.append("角色自身")
        print(f"{c['label']:<12}{c['count']:>6}{c['trusted_sim']:>8.3f}"
              f"{c['now_sim']:>8.3f}  {c['trusted_name']} {' '.join(tags)}")
    print("-" * 78)
    print(f"建议归并（过阈值）：{[(s['label'], s['trusted_sim']) for s in rep['suggestions']]}")
    print(f"灰区（交人判断）　：{[(s['label'], s['trusted_sim']) for s in rep['borderline']]}")
    if not rep["suggestions"] and not rep["borderline"]:
        print("（没有簇接近任何锚点——锚点集与语料声学条件可能差得太远）")


def cmd_segments(tenant: str, date: str, limit: int, since: str) -> None:
    wavs = sorted((config.DATA_DIR / f"tenants/{tenant}/corpus/{date}/wavs").glob("*.wav"))
    if not wavs:
        print(f"\n{date} 没有可比对的音频（corpus/{date}/wavs 为空）")
        return
    ref = store.get_voiceprint(tenant)
    rv = _vec(ref) if ref else None
    anchors = voiceprint._load_named_anchors(tenant)
    thr = config.NAMED_VOICEPRINT_THRESHOLD
    cutoff = None
    if since:
        cutoff = dt.datetime.strptime(since, "%Y-%m-%d %H:%M:%S").timestamp()

    print("\n" + "=" * 78)
    print(f"逐段判定  {date}  共 {len(wavs)} 段  阈值 {thr}  "
          f"锚点 { {k: len(v) for k, v in anchors.items()} }")
    print("-" * 78)
    print(f"{'seg':>6}{'秒':>6}{'本人锚点':>10}{'大锅锚点':>10}  判定")
    hit = tot = 0
    s_ref_all: list[float] = []
    s_nam_all: list[float] = []
    for w in wavs[: max(1, limit)]:
        emb = voiceprint._embed(w)
        if emb is None:
            print(f"{w.stem:>6}   embedding 提取失败")
            continue
        tot += 1
        s_ref = voiceprint._cosine(emb, rv) if rv else None
        s_nam = max((voiceprint._cosine(emb, e) for e in anchors.get("大锅", [])),
                    default=None)
        if s_ref is not None:
            s_ref_all.append(s_ref)
        if s_nam is not None:
            s_nam_all.append(s_nam)
        is_user, label = voiceprint.identify(w, tenant)
        hit += int(is_user is True)
        print(f"{w.stem:>6}{w.stat().st_size / 32000:>6.1f}"
              f"{(f'{s_ref:.3f}' if s_ref is not None else '-'):>10}"
              f"{(f'{s_nam:.3f}' if s_nam is not None else '-'):>10}"
              f"  is_user={is_user} label={label!r}")
    print("-" * 78)
    print(f"判为本人 {hit}/{tot}")
    for tag, vals in (("本人锚点", s_ref_all), ("大锅锚点", s_nam_all)):
        if vals:
            low = sum(1 for v in vals if v < thr)
            print(f"  {tag}：min={min(vals):.3f} 均值={sum(vals) / len(vals):.3f} "
                  f"max={max(vals):.3f}  低于阈值 {low} 段")
    if tot:
        ratio = hit / tot
        print(f"结论：{'锚点可用（多数命中）' if ratio >= 0.8 else '命中率偏低，建议补锚点'}"
              f"（{ratio:.0%}）")

    if cutoff is not None:
        _timeline(tenant, date, cutoff)


def _timeline(tenant: str, date: str, cutoff: float) -> None:
    """按注册时刻切开当天的段标签，判断注册是否立即生效。"""
    db = config.DATA_DIR / f"tenants/{tenant}/digitalhuman.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print("\n" + "=" * 78)
    print(f"注册时刻线切分（cutoff = {dt.datetime.fromtimestamp(cutoff):%H:%M:%S}）")
    print("-" * 78)
    print(f"{'标签':<12}{'段数':>6}{'注册前':>8}{'注册后':>8}")
    rows = list(con.execute(
        "SELECT speaker_label lab, COUNT(*) c FROM uploaded_segment WHERE date=?"
        " GROUP BY speaker_label ORDER BY c DESC LIMIT 12", (date,)))
    for r in rows:
        before = con.execute(
            "SELECT COUNT(*) c FROM uploaded_segment WHERE date=? AND speaker_label=?"
            " AND received_at<?", (date, r["lab"], cutoff)).fetchone()["c"]
        print(f"{r['lab']!r:<12}{r['c']:>6}{before:>8}{r['c'] - before:>8}")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="声纹判定自检")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--limit", type=int, default=40, help="打印上限（0 = 不限）")
    ap.add_argument("--audit-only", action="store_true", help="只做锚点体检")
    ap.add_argument("--since", default="", help="注册时刻 yyyy-MM-dd HH:MM:SS，用于时间线切分")
    args = ap.parse_args()
    limit = args.limit if args.limit > 0 else 10 ** 6

    print(f"数据目录 {config.DATA_DIR}")
    print(f"声纹模型 {config.SPEAKER_MODEL}")
    if not config.SPEAKER_MODEL:
        print("⚠️ SPEAKER_MODEL 未配置 —— 引擎会静默关闭，所有判定都是「未判定」")
    cmd_audit(args.tenant, limit)
    if not args.audit_only:
        cmd_segments(args.tenant, args.date, limit, args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
