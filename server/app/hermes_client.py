"""调用 Hermes 的实时语音端点（`/hermes/voice/invoke` 与 `/hermes/voice/ack`）。

## 定位

数字人服务是**语音入口**（录音 → ASR → 声纹），Hermes 是**大脑**（意图理解 +
HA 实时数据 + MiniMax 生成回复）。本模块只负责把转写文本送过去、把回复拿回来。

## 失败处理原则

**任何失败都返回 None，绝不抛异常、绝不让调用方 500。**
talk.py 拿到 None 会降级为「纯 AI 对话」（直接用 config.LLM_* 回答），
用户听到的仍然是有效回复，只是少了 HA 实时数据与 Hermes 的意图链路。
这对应规格第九节测试场景 7 与 11。

## 为什么用标准库 urllib 而不是 httpx / requests

与本项目 llm.py 保持一致：依赖表里没有 HTTP 客户端库，而这里是「一次请求拿一个
JSON」的简单场景。若将来要做 SSE 流式（规格 §5.3 提到过），再引 httpx 不迟。

## 代理

本机回环调用必须显式禁用代理：run.py 虽然清了 HTTP(S)_PROXY，但如果用户在
「设置 → 云端代理」里填了 DH_HTTPS_PROXY，某些库会把它当通用代理用，导致
127.0.0.1 的请求被发到外网代理上去（表现为莫名超时）。ProxyHandler({}) 一次解决。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)

# 显式禁代理的 opener（见模块 docstring）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_health_cache: dict[str, tuple[float, bool]] = {}   # key = base_url，见 available()
_HEALTH_TTL = 10.0     # 秒：避免每个 turn 都探一次 health


def _base(base_url: str = "") -> str:
    """解析实际要调的 Hermes 地址。

    默认用全局 `DH_HERMES_BASE_URL`；管家角色可以覆盖成自己的地址
    （`profiles.Bot.base_url`），这样「新建一个 bot 角色」既可以只是同一
    实例里的另一个人格，也可以是一个完全独立的 Hermes 进程。
    """
    return (base_url or config.HERMES_BASE_URL).rstrip("/")


def _post(path: str, payload: dict, timeout: float, base_url: str = "") -> dict | None:
    url = f"{_base(base_url)}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log.warning("Hermes %s 返回 HTTP %s：%s", path, e.code, body)
    except Exception as e:
        log.warning("Hermes %s 调用失败：%s", path, e)
    return None


def available(force: bool = False, base_url: str = "") -> bool:
    """Hermes 是否可达。带 10 秒缓存——health 会被 /api/talk/health 频繁调用，
    每次都真探一次会把日志刷满（而且 Hermes 不在时每次都要等超时）。

    缓存按**地址**分开：不同管家角色可能指向不同 Hermes 实例，
    共用一份缓存会让「A 通 B 不通」时两边结论串味。
    """
    key = _base(base_url)
    now = time.time()
    ts, val = _health_cache.get(key, (0.0, False))
    if not force and now - ts < _HEALTH_TTL:
        return val
    ok = False
    try:
        req = urllib.request.Request(f"{key}/health")
        with _opener.open(req, timeout=2.0) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _health_cache[key] = (now, ok)
    return ok


def invoke(text: str, *, user_aliases: list[str], tenant: str = "default",
           session_id: str = "", device: str = "", call_id: str = "",
           ha_context: dict | None = None, has_alias: bool = False,
           bot: str = "", persona: str = "", base_url: str = "") -> dict | None:
    """调 Hermes 拿回复。成功返回 dict，任何失败返回 None。

    返回字段（与 Hermes 侧 `_handle_voice_invoke` 对齐）：
        reply_text / reply_short / intent_type / ha_used / duration_ms / model

    Hermes 判为「不需要应答」时回 `skip=True, reason=not_conversation,
    intent_type=note_only`：调用方应落库记录（status=noted）但**不播报**。
    这与 `intent_type=voice_echo`（回声去重）是两回事，别合并处理。

    `has_alias` 是我方算出的唤醒词命中结果，传给 Hermes 当**线索**：
    两边的变体表不完全一致，只要一边认为在叫它就算在叫它（漏听比多答严重）。

    `bot` / `persona`（2026-09-15）是**管家角色**：同一台 Hermes 里可能有多个
    角色（门店管家、演示区管家、只做记录的助手），角色决定它能看哪些数据、
    用什么口吻。Hermes 侧不认识这两个字段时会忽略——不会因此报错。

    重试策略：config.HERMES_RETRY 次，指数退避（0.3s → 0.6s → 1.2s…）。
    只对**网络类**失败重试；Hermes 明确回了 skip 就直接采纳，重试没有意义。
    """
    payload = {
        "text": text,
        "user_aliases": list(user_aliases or []),
        "tenant": tenant,
        "session_id": session_id,
        "device": device,
        "call_id": call_id,
        "has_alias": bool(has_alias),
        "ts": int(time.time() * 1000),
    }
    if bot:
        payload["bot"] = bot
    if persona:
        payload["persona"] = persona
    if ha_context:
        payload["ha_context"] = ha_context

    attempts = max(1, config.HERMES_RETRY)
    for i in range(attempts):
        res = _post("/hermes/voice/invoke", payload, config.HERMES_TIMEOUT, base_url)
        if res is not None:
            return res
        if i < attempts - 1:
            time.sleep(0.3 * (2 ** i))
    log.warning("Hermes 调用 %d 次均失败，本轮降级为纯 AI 回答", attempts)
    return None


def ack(call_id: str, turn_id: str, played_ms: int = 0,
        user_correction: str = "", base_url: str = "") -> bool:
    """TTS 播报完毕回调。失败只记日志——这是统计用途，不该影响用户听到的内容。"""
    res = _post("/hermes/voice/ack", {
        "call_id": call_id,
        "turn_id": turn_id,
        "played_ms": int(played_ms),
        "user_correction": user_correction or "",
    }, timeout=3.0, base_url=base_url)
    return res is not None


def health() -> dict:
    return {
        "base_url": config.HERMES_BASE_URL,
        "reachable": available(force=True),
    }
