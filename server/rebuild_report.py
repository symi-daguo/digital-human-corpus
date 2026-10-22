#!/usr/bin/env python
"""一次性脚本：对指定日期用【当前代码逻辑】重算日报（raw/clean/summary）。

用途：管线改动（如时间戳分桶、分块去口气）后，把已处理完、音频已删的历史日期
按新逻辑重建，让手机端立即看到新格式，而不用等第二天自然产生。

安全：只读 segment 表里已保存的文本，不碰音频文件；跑失败会整体回滚吗？
不会——upsert_report 是最后一次写，失败就停在旧报告上，旧数据无损。
"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, llm, pipeline, store, tenants  # noqa: E402


def rebuild(date: str, tenant: str = tenants.DEFAULT_TENANT) -> None:
    segs = [s for s in store.segments_of_date(date, tenant)
            if s["is_speech"] and s["text"]]
    if not segs:
        print(f"{date}: 无可用人声段，跳过")
        return

    fixed_total: list[str] = []

    def line(s):
        ts = datetime.fromtimestamp(s["start_offset_ms"] / 1000).strftime("%H:%M")
        # 与在线管线一致：先做行业同音错字的确定性替换，再交给 LLM 整理。
        # 重算的价值正在于此——换行业/补了新映射后，历史日报能一起纠过来。
        txt, fixed = llm.apply_industry_fixes(s["text"])
        fixed_total.extend(fixed)
        body = f"[{s['speaker_label']}] {txt}" if s["speaker_label"] else txt
        return f"[{ts}] {body}"

    lines = [line(s) for s in segs]
    raw = "\n".join(lines)
    print(f"{date}: {len(segs)} 段 → raw {len(raw)} 字符")
    if fixed_total:
        print(f"  行业错字替换：{', '.join(fixed_total)}")

    if not llm.available():
        print("LLM 不可用，只重建 raw（clean/summary 沿用旧值）")
        prev_r = store.get_report(date, tenant)
        store.upsert_report(date, raw,
                            (prev_r["clean"] if prev_r else "") or "",
                            (prev_r["summary"] if prev_r else "") or "",
                            ready=True,
                            summary_generated_at=float((prev_r or {}).get("summary_generated_at") or 0),
                            tenant=tenant)
        return

    t = time.time()
    clean = llm.clean_transcript(raw)
    print(f"clean {len(clean)} 字符 / {time.time()-t:.1f}s")
    t = time.time()
    summary = llm.daily_summary(raw)
    print(f"summary {len(summary)} 字符 / {time.time()-t:.1f}s")

    store.upsert_report(date, raw, clean, summary, ready=True,
                        summary_generated_at=time.time(), tenant=tenant)
    pipeline._backup_text(date, raw, clean, summary, tenant)
    print("已写库 + 备份 text/")


if __name__ == "__main__":
    # 用法：rebuild_report.py [--tenant=<id>] <date> [<date>...]
    tenant = tenants.DEFAULT_TENANT
    dates = []
    for a in sys.argv[1:]:
        if a.startswith("--tenant="):
            tenant = a.split("=", 1)[1].strip() or tenants.DEFAULT_TENANT
        else:
            dates.append(a)
    tenants.resolve(tenant)   # 未注册租户直接报错退出，不静默
    for d in dates:
        rebuild(d, tenant)
