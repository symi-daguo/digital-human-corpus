#!/usr/bin/env python3
"""Agent 知识同步客户端——数字人日报学习流程的最小可用实现。

严格按 skills/digital-human/SKILL.md 契约：
  health → days → report?since=（增量）→ 去重 → 二次隐私扫描 → 入 agent 自库 → 推进水位线

用法：
    python3 agent_sync.py [date]        # 同步指定日期（默认今天）
    python3 agent_sync.py --status      # 查看水位线与已学条数

自库位置：~/.dh-agent-knowledge/
  knowledge.jsonl   已学条目（append-only）
  cursor.json       水位线 {date: next_since}
  digest.md         最近一次学习的可读摘要

这就是「智能体怎么学日报」的答案：任何 agent（Hermes/Cursor/WorkBuddy）
拿这个脚本或按契约自己实现，都能把日报变成自己的长期知识。
"""
from __future__ import annotations

import hashlib
import json
import ssl
import sys
import time
import urllib.request
from datetime import date as _date
from pathlib import Path

BASE = "https://127.0.0.1:8766"          # 本机调用；跨机请改 Mac 的局域网 IP
TOKEN_FILE = Path.home() / ".dh-agent-knowledge" / "agent.token"  # 或直接指 App 数据目录 data/agent.token
STORE = Path.home() / ".dh-agent-knowledge"
PAGE = 100                                # 每页行数（上限 200）
TURN_BUDGET_CHARS = 40_000                # 单轮字符预算（SKILL.md：20-40k）

# agent 自库的二次隐私扫描（服务端已扫一遍，契约要求 agent 再扫）
import re
_SECRET = re.compile(
    r"(?i)\bbearer\s+\S+|\bsk-\S+|\bghp_\S+|\bAKIA[0-9A-Z]{16}\b"
    r"|(?:密码|口令|密钥|令牌)\s*[是为:：=]\s*\S{4,}|\b[a-f0-9]{32,}\b")


def _token() -> str:
    for p in (
        TOKEN_FILE,
        Path.home() / "Library/Application Support/DigitalHuman/data/agent.token",
    ):
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    raise SystemExit(f"找不到 agent token（{TOKEN_FILE}）")


def _get(path: str) -> dict:
    ctx = ssl._create_unverified_context()   # 自签证书；生产可固定指纹
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {_token()}"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),     # 绝不走系统代理（本机直连）
        urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _load_jsonl(path: Path) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                seen[rec["id"]] = rec
    return seen


def sync(target: str) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    kj = STORE / "knowledge.jsonl"
    cursor_f = STORE / "cursor.json"
    cursor = json.loads(cursor_f.read_text()) if cursor_f.is_file() else {}

    health = _get("/api/agent/health")
    days = {d["date"]: d for d in _get("/api/agent/days")["days"]}
    if target not in days:
        raise SystemExit(f"{target} 无日报（可用：{sorted(days)}）")

    seen = _load_jsonl(kj)
    since = cursor.get(target, 0)
    new_items: list[dict] = []
    budget = TURN_BUDGET_CHARS

    while True:
        page = _get(f"/api/agent/report/{target}?since={since}&limit={PAGE}")
        for it in page["items"]:
            key = hashlib.sha256(
                f"{target}|{it['seg_id']}|{it['text']}".encode()).hexdigest()
            if key in seen:                       # 去重：绝不重复入库
                continue
            if _SECRET.search(it["text"]):        # agent 侧二次隐私扫描
                it["text"] = "[REDACTED]"
            seen[key] = it
            new_items.append(it)
            budget -= len(it["text"])
        since = page["next_since"]
        if not page["has_more"] or budget <= 0:   # 预算耗尽：下轮续
            break

    with kj.open("a", encoding="utf-8") as f:
        for it in new_items:
            f.write(json.dumps({
                "id": hashlib.sha256(f"{target}|{it['seg_id']}|{it['text']}".encode()).hexdigest(),
                "date": target, "seg_id": it["seg_id"], "at": it["at"],
                "speaker": it["speaker"], "is_user": it["is_user"], "text": it["text"],
            }, ensure_ascii=False) + "\n")
    cursor[target] = since
    cursor_f.write_text(json.dumps(cursor, ensure_ascii=False, indent=2))

    # 可读摘要
    n_user = sum(1 for i in new_items if i["is_user"])
    chars = sum(len(i["text"]) for i in new_items)
    lines = [
        f"# 学习摘要 {target}（{time.strftime('%H:%M:%S')}）",
        f"- 服务版本：{health['version']}；该日人声段总数：{days[target]['speech_segments']}",
        f"- 本轮新学：{len(new_items)} 段（本人 {n_user} / 他人 {len(new_items)-n_user}），约 {chars} 字",
        f"- 知识库总量：{len(seen)} 条；水位线 next_since={since}",
        "",
        "## 学到的内容（按时间）",
    ]
    for it in sorted(new_items, key=lambda x: x["at"]):
        who = "本人" if it["is_user"] else it["speaker"]
        lines.append(f"- [{it['at'][11:16]}] [{who}] {it['text']}")
    (STORE / "digest.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"[{target}] 新学 {len(new_items)} 段（本人 {n_user}），"
          f"知识库共 {len(seen)} 条，水位线 → {since}")
    for it in sorted(new_items, key=lambda x: x["at"])[:5]:
        who = "本人" if it["is_user"] else it["speaker"]
        print(f"  [{it['at'][11:16]}] [{who}] {it['text'][:40]}")


def status() -> None:
    cursor_f = STORE / "cursor.json"
    seen = _load_jsonl(STORE / "knowledge.jsonl")
    print(f"知识库共 {len(seen)} 条")
    if cursor_f.is_file():
        print("水位线：", json.dumps(json.loads(cursor_f.read_text()), ensure_ascii=False))
    md = STORE / "digest.md"
    if md.is_file():
        print("\n最近一次摘要：\n" + md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--status":
        status()
    else:
        sync(arg or _date.today().isoformat())
