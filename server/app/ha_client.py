"""门店 HA 事实源（只读）。

## 定位

`talk.py` 原先调 Hermes 时写死 `ha_context=None`，于是每一轮对话
Hermes 都得自己去查一次 HA（6000+ 实体，约 1s），而且服务端自己
对"门店现在有什么设备"一无所知——手机端想在对话里回看设备状态也没依据。

本模块提供**同一份**只读事实，供两处消费：

1. `talk.py` → 注入 `ha_context`，让 Hermes 省掉一次全量查询；
2. `/api/ha/*` → Mac 壳 / Android / Tauri 三端读同一份设备视图，
   这是「多端信息同步」的地基：同一时刻三端看到的是同一个快照。

## 为什么不在服务端实现"控制"

门店设备控制已由 Hermes 侧的确定性执行层负责（`symi_devices.py`：
规则解析 + 直接调用 HA 服务 + 读回校验）。服务端再实现一份会导致
两处行为漂移（一处修了另一处没修），所以这里**只读**。

## 内存纪律

`/api/states` 单次返回 6000+ 实体，原始 JSON 反序列化后约 3MB。
这里只保留 4 元组（约 1MB），并靠 TTL 复用；检索按需求值，
不预建全量倒排表 —— 宁可每次多花几毫秒，也不要常驻几 MB。
"""
from __future__ import annotations

import json
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter

from . import config

_lock = threading.Lock()
_cache: dict = {"rows": [], "at": 0.0, "error": ""}

_UA = {"User-Agent": "digitalhuman-ha/1.0"}


def configured() -> bool:
    return bool(config.HA_TOKEN)


def _get(path: str, timeout: float | None = None):
    if not config.HA_TOKEN:
        raise RuntimeError("DH_HA_TOKEN 未配置")
    req = urllib.request.Request(
        f"{config.HA_URL}{path}",
        headers={"Authorization": f"Bearer {config.HA_TOKEN}", **_UA},
    )
    with urllib.request.urlopen(req, timeout=timeout or config.HA_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


# ---- 只读端点用不到控制域白名单：门店设备全量都要能展示 ----

def _norm(text: str) -> str:
    """归一化：全角→半角、小写、去标点空格。「继电器 1」「继电器１」→「继电器1」。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    return "".join(ch for ch in s if ch.isalnum())


_CN_NUM = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
           "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def _key(text: str) -> str:
    return "".join(_CN_NUM.get(ch, ch) for ch in _norm(text))


def _fetch() -> list:
    states = _get("/api/states")
    rows = []
    for e in states or []:
        eid = e.get("entity_id", "")
        if "." not in eid:
            continue
        rows.append((
            eid,
            eid.split(".", 1)[0],
            (e.get("attributes") or {}).get("friendly_name", "") or eid,
            e.get("state", ""),
        ))
    return rows


def _snapshot() -> list:
    """带 TTL 的快照。HA 不可用时回落上一次结果（宁可给旧数据也不给空）。"""
    now = time.time()
    if _cache["rows"] and (now - _cache["at"]) < config.HA_CACHE_TTL:
        return _cache["rows"]
    if not configured():
        return []
    try:
        rows = _fetch()
    except urllib.error.HTTPError as e:
        _cache["error"] = f"HTTP {e.code}"
        return _cache["rows"]
    except Exception as e:  # noqa: BLE001
        _cache["error"] = str(e)[:200]
        return _cache["rows"]
    with _lock:
        _cache["rows"] = rows
        _cache["at"] = time.time()
        _cache["error"] = ""
    return rows


def overview() -> dict:
    """设备概览。字段刻意与 Hermes 侧 `symi_devices.overview()` 同名同义，
    让两端读同一套语义（多端同步的前提是"同一个东西叫同一个名字"）。"""
    rows = _snapshot()
    if not rows:
        return {"configured": configured(), "total": 0, "online": 0,
                "offline": 0, "by_domain": {}, "error": _cache["error"] or "HA 不可达"}
    by_domain = Counter(r[1] for r in rows)
    online = sum(1 for r in rows if r[3] not in ("unavailable", "unknown", ""))
    return {
        "configured": True,
        "total": len(rows),
        "online": online,
        "offline": len(rows) - online,
        "by_domain": dict(by_domain.most_common(15)),
        "age_s": int(time.time() - _cache["at"]) if _cache["at"] else -1,
        "error": _cache["error"],
    }


def search(keyword: str, domain: str = "", limit: int = 40) -> list[dict]:
    """按关键词检索设备。

    打分规则与 Hermes 侧 `symi_devices.search_devices` 保持一致
    （完全相等 100 / 关键词被包含 80 / 名字被包含 60 / 顺序包含 40+），
    这样同一句话在两处检索出的候选顺序一致，不会出现"管家说 3 号、
    手机界面显示 1 号"这种对不上的情况。
    """
    rows = _snapshot()
    kw = _key(keyword)
    if not kw:
        return []
    out = []
    for eid, dom, name, state in rows:
        if domain and dom != domain:
            continue
        nk, ek = _key(name), _key(eid.split(".", 1)[-1])
        if not nk and not ek:
            continue
        if kw == nk or kw == ek:
            score = 100
        elif kw in nk or kw in ek:
            score = 80
        elif nk and nk in kw:
            score = 60
        else:
            it = iter(nk or ek)
            hit = sum(1 for ch in kw if ch in it)
            if hit >= max(2, len(kw) - 1):
                score = 40 + int(20 * hit / max(1, len(kw)))
            else:
                continue
        bonus = 10 if state not in ("unavailable", "unknown", "") else 0
        out.append((score + bonus, len(nk or ek), eid, dom, name, state))
    out.sort(key=lambda x: (-x[0], x[1]))
    return [{"entity_id": e, "domain": d, "name": n, "state": s}
            for _s, _l, e, d, n, s in out[:limit]]


def context_for(text: str, hits: int = 12) -> dict | None:
    """给 Hermes 注入的 ha_context：概览 + 与本轮文本相关的设备。

    刻意**只带相关设备**而不是全量清单 —— Hermes 自己也有检索，
    这里带的是"跨端一致的那一份"，体量必须小（注入的是每轮请求体）。
    """
    if not configured():
        return None
    ov = overview()
    if not ov.get("total"):
        return None
    ctx = {
        "source": "digitalhuman-server",
        "ha_url": config.HA_URL,
        "total_devices": ov["total"],
        "online": ov["online"],
        "offline_unknown": ov["offline"],
        "domains": ov["by_domain"],
        "snapshot_age_s": ov.get("age_s", -1),
    }
    rel = search(text, limit=hits)
    if rel:
        ctx["related"] = rel
    return ctx


def health() -> dict:
    return {
        "configured": configured(),
        "url": config.HA_URL,
        "cached": len(_cache["rows"]),
        "age_s": int(time.time() - _cache["at"]) if _cache["at"] else -1,
        "error": _cache["error"],
    }
