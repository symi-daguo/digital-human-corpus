"""Agent 知识端点（只读，移植自 DeskCap 的 agent API 契约）。

设计原则（与手机端 /api/* 严格隔离）：
- 只有 GET：agent 物理上无法触发管线/上传/声纹变更
- 独立凭证：AGENT_TOKEN（data/agent.token，0600，启动自动生成），
  与手机端配对 Token 分离——泄露互不影响，可独立轮换
- 增量水位线：?since=<seg_id>（排他下界）+ 响应 next_since，
  agent 在自己库里存游标 + 去重键 sha256(date|seg_id|text)，禁止重复入库
- 硬上限：单请求 ≤200 行；agent 单轮建议 30-80 段（SKILL.md 详述）
- 出口脱敏：所有文本过 redact.redact()，秘密 → [REDACTED]
- 数据边界：绝不返回音频路径/声纹向量/语料；只有转写文本与总结
"""
from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException

from . import asr, config, llm, redact, store, tenants
from ._version import VERSION

router = APIRouter(prefix="/api/agent")

MAX_ROWS = 200
DEFAULT_ROWS = 100


def _check_agent_auth(authorization: str | None) -> None:
    expected = f"Bearer {config.AGENT_TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="bad agent token")


def _require_date(date: str) -> str:
    if not config.is_valid_date(date):
        raise HTTPException(status_code=400, detail="bad date, expect yyyy-MM-dd")
    return date


def _resolve_tenant(tenant: str) -> str:
    """agent 侧租户解析：?tenant= 参数，默认 default；未注册 403（与手机端同基线）。"""
    tid = (tenant or "").strip() or tenants.DEFAULT_TENANT
    try:
        return tenants.resolve(tid).id
    except KeyError:
        raise HTTPException(status_code=403, detail=f"unknown tenant: {tid[:24]}")


@router.get("/health")
def health() -> dict:
    """公开端点：只返能力标志与上限，不含任何数据（对齐 DeskCap）。

    行业名算元信息（不含用户内容），公开返回：agent 未鉴权时也能据此判断
    这批语料属于哪个领域，决定要不要先加载对应术语表。
    """
    return {
        "ok": True,
        "version": VERSION,
        "limits": {
            "rows_per_request": MAX_ROWS,
            "agent_turn_rows": "30-80",
        },
        "industry": config.INDUSTRY or "general",
        # 处理模式（2026-10-22 加）：`tools/agg-mode.sh status` 靠这几个字段报真实生效值。
        # 配置值 ≠ 生效值——空字符串会被误读成默认值，这里给的是**进程内判定结果**。
        # 不含任何语料内容，公开可见不敏感。
        "agg": {
            "windows": config.AGG_WINDOWS_RAW,
            "batch_min": config.AGG_BATCH_MIN,
            "in_window": config.in_agg_window(),
            "active": config.agg_active(),
        },
        "llm": {
            "backend": "local" if not config.llm_is_cloud() else "cloud",
            "model": config.LLM_MODEL,
        },
    }


@router.get("/days")
def days(authorization: str | None = Header(default=None),
         tenant: str = "") -> dict:
    """可学习日期列表（ready=1 才有完整三版）。?tenant= 选择租户（默认 default）。

    同时返回当前行业与术语表：服务端已在 ASR 热词与 LLM 提示词里用过一遍，
    但小模型纠不干净的地方，agent（通常是更强的大模型）可以拿这份表做二次
    纠偏 —— 服务端兜底、agent 精修，各做一层，不互相依赖。
    """
    _check_agent_auth(authorization)
    tid = _resolve_tenant(tenant)
    out = []
    for r in store.report_dates(tid):
        out.append({
            "date": r["date"],
            "ready": bool(r["ready"]),
            "speech_segments": r["speech_segments"],
        })
    return {
        "tenant": tid,
        "days": out,
        "industry": config.INDUSTRY or "general",
        "industry_terms": list(config.INDUSTRY_TERMS),
        "industry_note": "服务端已用该词表做过 ASR 热词偏置与 LLM 同音纠偏；"
                        "残留的疑似同音错字可用此表二次校正。",
    }


@router.get("/report/{date}")
def report(date: str, since: int = 0, limit: int = DEFAULT_ROWS,
           authorization: str | None = Header(default=None),
           tenant: str = "") -> dict:
    """当日人声转写行，按 seg_id 增量（since=上一页 next_since，排他）。?tenant= 选租户。

    文本已出口脱敏；is_user 仅为布尔（本人/他人），不含声纹向量。
    """
    _check_agent_auth(authorization)
    tid = _resolve_tenant(tenant)
    _require_date(date)
    if not 1 <= limit <= MAX_ROWS:
        raise HTTPException(status_code=400, detail=f"bad limit: 1-{MAX_ROWS}")
    rows = store.segments_for_agent(date, since, limit + 1, tid)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        # 重复幻觉兜底（2026-09-17）：本端点逐行发的是库里**原始**转写，
        # 早于建稿修复入库的旧段带着 ASR 循环幻觉（如「T A T A T A…」×8148）。
        # 这是喂给智能体的上下文，带垃圾循环既烧 token 又会把智能体带偏，
        # 所以在出口再压一道（与 pipeline._line() 同一函数，幂等）。
        # 不影响 /summary：那条路径读的是 report 表里的总结稿，本就干净。
        text, _hits = redact.redact(asr.collapse_repeats(r["text"] or ""))
        items.append({
            "seg_id": r["seg_id"],
            "at": datetime.fromtimestamp(r["start_offset_ms"] / 1000).isoformat(timespec="seconds"),
            "speaker": r["speaker_label"],
            "is_user": bool(r["is_user"]),
            "text": text,
        })
    next_since = items[-1]["seg_id"] if items else since
    return {
        "date": date,
        "count": len(items),
        "has_more": has_more,
        "next_since": next_since,
        "items": items,
        "note": "Dedupe by sha256(date|seg_id|text) in agent DB; never re-ingest. "
                "Advance since=next_since only after commit.",
    }


@router.get("/summary/{date}")
def summary(date: str, authorization: str | None = Header(default=None),
            tenant: str = "") -> dict:
    """当日总结：回顾时间轴行 + 其余栏目卡。文本已出口脱敏。?tenant= 选租户。"""
    _check_agent_auth(authorization)
    tid = _resolve_tenant(tenant)
    _require_date(date)
    r = store.get_report(date, tid)
    if not r or not r["ready"]:
        return {"tenant": tid, "date": date, "ready": False, "cards": []}
    cards = []
    for c in llm.parse_summary_cards(r["summary"]):
        body, _ = redact.redact(c["body"])
        cards.append({"title": c["title"], "body": body})
    return {
        "tenant": tid,
        "date": date,
        "ready": True,
        "cards": cards,
        "note": "【今日回顾】body lines are code-injected 【HH:MM-HH:MM】 timeline rows.",
    }
