"""音频回收：按「信息价值 + 时间」分级删除 wav，释放硬盘。

为什么要有这一层（2026-10-23 用户实测反馈）：
工作台存储卡显示 3430MB，其中原始音频 1991MB、语料 1406MB。查下来两条泄漏：

  1. `audio/<date>/*.wav` 只在**日终 final 轮成功**后删（pipeline.py ⑤）。
     日终没跑到 / 失败 / 该日期从未 final，音频就永久留着——现场最早的
     2026-09-23 还占 477MB。
  2. `corpus/<date>/wavs/*.wav` 是语料导出时的**音频副本**（GPT-SoVITS 训练用），
     全仓没有任何清理逻辑，与 audio/ 双份占盘（09-21 一天 216MB）。

同时用户提出了价值判断的诉求：每天大量「口气类无效音频」，本地就有转写文本，
完全可以先用确定性规则判出低信息密度段，优先回收，不必等 7 天。

规则（安全优先，全部可配）：

  | 对象 | 条件 | 动作 |
  | --- | --- | --- |
  | audio/ 原音频 | 该日期已 final | 删（兜底清理漏网） |
  | audio/ 原音频 | 龄期 ≥ DH_RECLAIM_AFTER_DAYS（默认 7） | 删 |
  | audio/ 原音频 | 龄期 ≥ DH_RECLAIM_LOWVALUE_DAYS（默认 3）**且**该段低信息密度 | 删 |
  | corpus/ wavs 副本 | 同上三条 | 删 |
  | corpus.jsonl / 日报 / 数据库 / 备份 | — | **永不删** |

「低信息密度」复用 insights.is_low_value（无实体信号 + 有效字 <8 或纯应答），
与价值门同一套判定，避免两处标准打架。

硬约束：
- **只删 .wav**，且必须是 `uploaded_segment` 里**有转写文本**的段（没文本 = 删了就永久丢失）；
- 龄期按 segment 的 start_offset_ms 计算（不是文件 mtime，mtime 会被 rsync 改）；
- 任何单文件失败只记 warning，不中断整轮；
- 全程 dry-run 可预演（工具/接口都支持），先看数字再动手。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from . import config, insights, tenants

log = logging.getLogger(__name__)

# 龄期门槛（天）
AFTER_DAYS = int(os.environ.get("DH_RECLAIM_AFTER_DAYS", "7"))
LOWVALUE_DAYS = int(os.environ.get("DH_RECLAIM_LOWVALUE_DAYS", "3"))
# 总开关：0=完全停用（排查时用）
ENABLED = os.environ.get("DH_RECLAIM", "1") not in ("0", "false", "no")
# 每轮最多删多少个文件（防一次删爆 IO 影响采集）
MAX_FILES_PER_RUN = int(os.environ.get("DH_RECLAIM_MAX_FILES", "4000"))


def _tenant(tid: str) -> "tenants.Tenant":
    return tenants.resolve(tid)


def _db_path(tid: str) -> Path:
    return _tenant(tid).db_path


def _segments(tid: str, date: str) -> list[tuple[int, str, int]]:
    """(seg_id, text, start_offset_ms)。库缺失/表缺失返回空。"""
    p = _db_path(tid)
    if not p.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT seg_id, COALESCE(text,''), COALESCE(start_offset_ms,0) "
                "FROM uploaded_segment WHERE date=? ORDER BY seg_id",
                (date,),
            ).fetchall()
        finally:
            conn.close()
        return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]
    except Exception as e:  # 库损坏/锁住：本轮跳过该日期，绝不删无依据的文件
        log.warning("reclaim 读取 %s/%s 段失败，跳过：%s", tid, date, e)
        return []


def _final_dates(tid: str) -> set[str]:
    """已跑过日终终稿的日期集合（report.final_at>0 且 ready=1）：这些日期音频全部可删。

    注意表结构：report 只有 final_at / ready，没有 kind 字段（2026-10-23 实读 schema 确认）。
    """
    p = _db_path(tid)
    if not p.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT date FROM report "
                "WHERE COALESCE(final_at,0) > 0 AND COALESCE(ready,0) = 1"
            ).fetchall()
        finally:
            conn.close()
        return {str(r[0]) for r in rows}
    except Exception as e:
        log.warning("reclaim 读取 final 日期失败，按空集处理：%s", e)
        return set()


def _age_days(ms: int) -> float:
    if ms <= 0:
        return 1e9   # 没有时间戳的段不按龄期删（保守）
    return (time.time() - ms / 1000.0) / 86400.0


def plan(tenant: str = tenants.DEFAULT_TENANT) -> dict:
    """计算回收计划（不删任何东西）。

    返回 {files: [(path, reason, size)], by_reason: {...}, total_bytes, total_files}
    reason ∈ final / aged / low_value
    """
    t = _tenant(tenant)
    finals = _final_dates(tenant)
    audio_root = t.audio_dir
    corpus_root = t.corpus_dir
    files: list[tuple[Path, str, int]] = []
    reasons = {"final": 0, "aged": 0, "low_value": 0}

    if not audio_root.is_dir() and not corpus_root.is_dir():
        return {"files": [], "by_reason": reasons, "total_bytes": 0, "total_files": 0}

    dates = set()
    if audio_root.is_dir():
        dates |= {p.name for p in audio_root.iterdir() if p.is_dir()}
    if corpus_root.is_dir():
        dates |= {p.name for p in corpus_root.iterdir() if (p / "wavs").is_dir()}

    for date in sorted(dates):
        if not config.is_valid_date(date):
            continue
        segs = {sid: (text, ms) for sid, text, ms in _segments(tenant, date)}
        if not segs:
            # 该日期没有转写记录：**一个都不删**（可能是别的租户/异常目录）
            continue
        is_final = date in finals
        # 同一日期下 audio/ 与 corpus/wavs 两份，用同一套判定
        dirs = [audio_root / date, corpus_root / date / "wavs"]
        for d in dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or f.suffix.lower() != ".wav":
                    continue
                try:
                    sid = int(f.stem)
                except ValueError:
                    continue          # 文件名不是段号：不碰
                rec = segs.get(sid)
                if rec is None:
                    continue          # 库里没这段：不删（可能是手工放入的测试音频）
                text, ms = rec
                if not text.strip():
                    continue          # 没有转写文本：删了永久丢失，绝不删
                if is_final:
                    reason = "final"
                else:
                    age = _age_days(ms)
                    if age >= AFTER_DAYS:
                        reason = "aged"
                    elif age >= LOWVALUE_DAYS and insights.is_low_value(text):
                        reason = "low_value"
                    else:
                        continue
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                files.append((f, reason, size))
                reasons[reason] += 1

    files.sort(key=lambda x: x[2], reverse=True)   # 大文件优先，先释放空间
    return {
        "files": files,
        "by_reason": reasons,
        "total_bytes": sum(s for _, _, s in files),
        "total_files": len(files),
    }


def sweep(tenant: str = tenants.DEFAULT_TENANT, dry_run: bool = True) -> dict:
    """执行一轮回收。dry_run=True 只算不删（默认——调用方必须显式传 False）。"""
    if not ENABLED:
        return {"enabled": False, "deleted_files": 0, "freed_bytes": 0}
    p = plan(tenant)
    out = {
        "enabled": True,
        "dry_run": dry_run,
        "planned_files": p["total_files"],
        "planned_bytes": p["total_bytes"],
        "by_reason": p["by_reason"],
        "deleted_files": 0,
        "freed_bytes": 0,
        "failed": 0,
    }
    if dry_run:
        log.info("音频回收预演（%s）：可删 %d 个 wav / %.1f MB（final=%d aged=%d low_value=%d）",
                 tenant, p["total_files"], p["total_bytes"] / 1048576,
                 p["by_reason"]["final"], p["by_reason"]["aged"], p["by_reason"]["low_value"])
        return out

    deleted = 0
    freed = 0
    failed = 0
    for f, reason, size in p["files"][:MAX_FILES_PER_RUN]:
        try:
            f.unlink()
            deleted += 1
            freed += size
        except FileNotFoundError:
            pass                      # 已被别处删掉：不算失败
        except OSError as e:
            failed += 1
            log.warning("reclaim 删除失败 %s：%s", f, e)
    out.update(deleted_files=deleted, freed_bytes=freed, failed=failed)
    log.info("音频回收完成（%s）：删 %d 个 wav / %.1f MB（final=%d aged=%d low_value=%d，失败 %d）",
             tenant, deleted, freed / 1048576,
             p["by_reason"]["final"], p["by_reason"]["aged"], p["by_reason"]["low_value"], failed)
    return out


# ---- 自动回收节流（sweeper 每轮调用；每天最多实际删一次）--------------------
_LAST_RUN = config.DATA_DIR / "reclaim-last.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def maybe_sweep(tenant: str = tenants.DEFAULT_TENANT) -> dict | None:
    """每天至多执行一次真实回收（sweeper 每轮会调用，靠这里节流）。

    返回 None = 今天已跑过（跳过）；否则返回 sweep 结果。
    """
    import json
    today = _today()
    try:
        state = json.loads(_LAST_RUN.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if state.get(tenant) == today:
        return None
    result = sweep(tenant, dry_run=False)
    state[tenant] = today
    try:
        _LAST_RUN.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("reclaim 节流状态写入失败（下次可能重复跑）: %s", e)
    return result
