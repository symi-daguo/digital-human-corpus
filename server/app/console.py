"""浏览器工作台（/console）：给「配置完啥也看不到」的人一个可视界面。

设计约束：
- **零外部依赖**：单个 HTML 内嵌 CSS/JS，不引任何 CDN。Mac 端服务跑在局域网，
  不保证外网可达，引 CDN 会直接白屏。
- **服务端零额外内存**：页面只是一个字符串常量（~20KB），渲染全在浏览器做；
  服务端不引入模板引擎、不做 SSR。
- **一次请求拿全量**：`/api/console/state` 把状态、模型、行业、日期、角色聚合成
  一个响应，避免前端发 5 个请求（局域网 + 自签 TLS 下每多一次握手都有体感延迟）。
- **复用现有鉴权**：页面本身无数据不需要鉴权；数据接口沿用手机端 Bearer token
  （与 Agent token 分开，避免把更强的凭证暴露给浏览器）。

访问：https://<mac-ip>:8766/console （自签证书，浏览器需手动信任一次）
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import Response

from . import _logo, asr, config, llm, store, tenants, voiceprint
from ._version import VERSION

log = logging.getLogger(__name__)

router = APIRouter()


def _check_auth(authorization: str | None) -> None:
    """与手机端同一套凭证（routes._check_auth 的等价实现）。

    不直接 import routes 里的私有函数：那会让 console 依赖 routes 的导入副作用
    （routes 拉起了 pipeline / voiceprint 等重模块），而控制台应当尽量轻。
    """
    import secrets

    if config.TOKEN.strip():
        expected = f"Bearer {config.TOKEN}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="bad token")


def _resolve_console_tenant(tenant: str, x_tenant: str | None = None) -> str:
    """优先 header X-Tenant（工作台 JS 统一带 header），其次 query ?tenant=。"""
    tid = (x_tenant or "").strip() or (tenant or "").strip() or tenants.DEFAULT_TENANT
    try:
        return tenants.resolve(tid).id
    except KeyError:
        raise HTTPException(status_code=403, detail=f"unknown tenant: {tid[:24]}")


@router.get("/api/console/tenants")
def list_tenants(authorization: str | None = Header(default=None),
                 x_tenant: str = Header(default="")) -> dict:
    """租户列表 + 概览（工作台租户切换器与空间卡片用）。"""
    _check_auth(authorization)
    _resolve_console_tenant("", x_tenant)   # 校验租户（列表本身全量返回）
    return {"tenants": [tenants.status_of(t) for t in tenants.all_tenants()]}


@router.post("/api/console/tenants")
def add_tenant(body: dict = Body(default={}),
               authorization: str | None = Header(default=None)) -> dict:
    """注册新租户（成员手机接入前先在这里开空间）。body: {"id": "...", "name": "..."}"""
    _check_auth(authorization)
    try:
        t = tenants.add(str((body or {}).get("id") or ""), str((body or {}).get("name") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    store.connect(t.id)   # 立即建库，成员上传第一段就能落
    log.info("console: tenant created %s", t.id)
    return {"ok": True, "tenants": [tenants.status_of(x) for x in tenants.all_tenants()]}


@router.delete("/api/console/tenants/{tid}")
def remove_tenant(tid: str, authorization: str | None = Header(default=None)) -> dict:
    """注销租户（只移出注册表，数据目录保留——物理清除由人工确认后进行）。"""
    _check_auth(authorization)
    try:
        ok = tenants.remove(tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="tenant not found")
    log.info("console: tenant removed %s", tid)
    return {"ok": True, "tenants": [tenants.status_of(x) for x in tenants.all_tenants()]}


@router.get("/api/console/state")
def state(authorization: str | None = Header(default=None),
          x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """工作台首页需要的全部数据，一次拿完。X-Tenant（或 ?tenant=）选租户空间。"""
    _check_auth(authorization)
    tid = _resolve_console_tenant(tenant, x_tenant)
    days = []
    for r in store.report_dates(tid):
        days.append({
            "date": r["date"],
            "ready": bool(r["ready"]),
            "speech_segments": r["speech_segments"],
        })
    return {
        "service": {
            "ok": True,
            "version": VERSION,
            "port": config.PORT,
            "lan_ips": config._lan_ips(),
            "token_enabled": bool(config.TOKEN.strip()),
        },
        # 外网中转提示（env DH_RELAY_HINT）：手机端拉到后自动填进
        # 「外网中转地址」，自适应连接的第二跳不用用户手抄。
        "relay_hint": os.environ.get("DH_RELAY_HINT", "").strip(),
        "models": {
            "llm_available": llm.available(),
            "llm_model": config.LLM_MODEL,
            "asr_incremental": config.ASR_BACKEND_INCREMENTAL,
            "asr_final": config.ASR_BACKEND_FINAL,
            "asr_final_rewrite": config.ASR_FINAL_REWRITE,
            "hotwords": [w for w in config.ASR_HOTWORDS.split() if w],
            "hotword_max": config.HOTWORD_MAX,
        },
        "industry": {
            "current": config.INDUSTRY or "general",
            "label": config.industry_label(config.INDUSTRY or "general"),
            "available": config.available_industries(),
            "terms": len(config.INDUSTRY_TERMS),
            "fixes": config.INDUSTRY_FIXES,
            "pinned_by_env": bool(getattr(config, "_ENV_INDUSTRY", "")),
        },
        # 方言纠偏（2026-09-17）：与行业无关、始终生效的一张表。单列出来是为了
        # 让「为什么这个词被改了」有处可查 —— 它不在上面行业纠偏列表里，
        # 但确实会参与替换，混在一起会让人以为改错了地方。
        "dialect": {
            "fixes": config.load_dialect_fixes(),
            "path": config.dialect_fix_path(),
        },
        "days": days,
        "speakers": store.list_named_speakers(tid),
        # 注册样本的口径（时长 + 要朗读的固定文本）。由服务端下发而不是在
        # 每个客户端各写一份：Mac 壳与网页工作台要展示同一段话，客户端各存
        # 一份迟早会出现「手机提示读 A、电脑提示读 B」。
        # （Android 采集端例外，见 voiceprint.py 的注释：它必须离线可读。）
        "enroll": {
            "seconds": voiceprint.ENROLL_SECONDS,
            "script": voiceprint.ENROLL_SCRIPT,
        },
        "tenant": tid,
        "tenants": [tenants.status_of(t) for t in tenants.all_tenants()],
        # 硬盘占用（2026-09-15）：分项 + 各空间，30 秒缓存。长期驻留部署最怕
        # 某个目录悄悄涨满而服务照常跑，这里让占用直接可见。
        "storage": tenants.storage_overview(),
    }


@router.get("/api/console/report/{date}")
def report(date: str, authorization: str | None = Header(default=None),
           x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """某日三版日报（当前租户）。与手机端 /api/report 同源，只是不走 segment_flags 裁剪。

    summary_cards 必须与手机端 /api/report 一样返回（2026-09-10 审查补上）：
    Mac 壳日报页的总结卡片区与时间轴直接渲染这个字段。此前只有 date/ready/
    raw/clean/summary 五个字段，导致 Mac 端「今天总结」永远渲染不出卡片，
    而同一份数据在手机上是正常的——同一个服务端、两个端点字段不一致。
    解析统一走 llm.parse_summary_cards，与手机端同源，不各自实现一份。
    """
    _check_auth(authorization)
    tid = _resolve_console_tenant(tenant, x_tenant)
    if not config.is_valid_date(date):
        raise HTTPException(status_code=400, detail="bad date, expect yyyy-MM-dd")
    r = store.get_report(date, tid)
    if not r:
        return {"date": date, "ready": False, "raw": "", "clean": "",
                "summary": "", "summary_cards": []}
    return {
        "date": date,
        "ready": bool(r["ready"]),
        "raw": r["raw"] or "",
        "clean": r["clean"] or "",
        "summary": r["summary"] or "",
        # 卡片由服务端解析【栏目】得到，客户端直接渲染，不自己猜格式。
        # 解析为空说明模型没按格式输出，客户端回退成整段文本（内容不丢）。
        "summary_cards": llm.parse_summary_cards(r["summary"]),
    }


@router.get("/api/console/talk/{date}")
def talk_log(date: str, authorization: str | None = Header(default=None),
             x_tenant: str = Header(default=""), tenant: str = "",
             limit: int = 300) -> dict:
    """某日实时对话记录（当前租户）。给 Mac 壳「对话」页签用。

    ## 为什么单开一个端点，而不是让用户去翻服务日志

    服务日志是 tail 文本，`uvicorn.access` 的 HTTP 行、管线的 audio_tagging、
    LLM 的 clean_transcript 全混在一起。想看「管家今天到底说了什么」得在一堆
    噪声里翻，而且 rollover 之后旧的就没了。对话本身是**结构化**的（谁说的 /
    说了什么 / 走哪条链路 / 耗时多少），放进文本日志是降级使用。

    数据源是 `talk_message` 表（每轮一行，`/start` 时落占位、ASR 与回复就位后
    补全），不是解析日志——查表比抠字符串可靠得多，也不会被轮转吃掉。

    按时间**倒序**返回：用户打开这一页想看的是「刚刚那次管家回了啥」。
    """
    _check_auth(authorization)
    tid = _resolve_console_tenant(tenant, x_tenant)
    if not config.is_valid_date(date):
        raise HTTPException(status_code=400, detail="bad date, expect yyyy-MM-dd")

    rows = store.talk_messages_of_date(date, tid)
    cap = max(1, min(int(limit or 300), 1000))
    rows = rows[-cap:]           # 取当日最后 cap 条
    rows.reverse()               # 再倒序，最新的在最前

    items = [{
        "turn_id": r.get("turn_id", ""),
        "ts": float(r.get("ts") or 0),
        "device": r.get("device", ""),
        "session_id": r.get("session_id", ""),
        "asr_text": r.get("asr_text", ""),
        # is_user 三态：1 本人 / 0 他人 / null 未判定。**不要在这里折叠成 bool**，
        # 「未判定」和「是他人」在排查声纹问题时是完全不同的结论。
        "is_user": r.get("is_user"),
        "speaker": r.get("speaker", ""),
        "status": r.get("status", ""),
        "reply_text": r.get("reply_text", ""),
        "reply_speaker": r.get("reply_speaker", ""),
        "call_id": r.get("call_id", ""),
        "latency_ms": int(r.get("latency_ms") or 0),
        "via": r.get("via", ""),
        "played_ms": r.get("played_ms"),
    } for r in rows]

    return {
        "date": date,
        "count": len(items),
        "truncated": len(rows) >= cap,
        "items": items,
    }


@router.get("/api/console/voiceprint/audit")
def voiceprint_audit(authorization: str | None = Header(default=None),
                     x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """只读声纹体检（当前租户）。

    ## 为什么需要它

    `voiceprint.identify()` 第③步命中匿名簇后返回 `hit in SELF_ALIASES`：
    「说话人18」不在 `SELF_ALIASES` 里 → `is_user=False`，被当成**他人**。
    但实测（2026-09-10）说话人18 与本人锚点余弦 **0.592**、与「大锅」**0.578** ——
    它就是本人。这就是「同一个角色 8 秒内 is_user 0→1 跳变」的根因。

    修法不是降阈值（会把客人一起放进来），而是把**本人自己的历史大簇**并入具名角色。
    本端点只**算**不**改**：把每个持久簇与每个锚点的余弦都摆出来，
    由用户决定归并哪些 —— 把客人误并进「大锅」，门控就等于没开。

    ## 返回

    - `anchors`：当前具名锚点（含来源 `register` / `merge:<簇名>`）
    - `clusters`：全部持久簇，按与最相近锚点的余弦降序；`suggest=true` 表示过阈值
    - `suggestions`：`clusters` 里 `suggest=true` 的子集，即建议归并清单
    """
    _check_auth(authorization)
    tid = _resolve_console_tenant(tenant, x_tenant)
    from . import voiceprint          # 延迟导入：控制台保持轻量（见文件头注释）
    return voiceprint.audit(tid)


@router.post("/api/console/voiceprint/merge")
def voiceprint_merge(payload: dict = Body(...),
                     authorization: str | None = Header(default=None),
                     x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """把一个匿名簇归并进具名角色（**必须由用户确认后调用**）。

    body：`{"label": "说话人18", "name": "大锅"}`

    动作有三步且不可分割（见 `store.merge_speaker_into_named`）：
    簇的 centroid 变成该角色的一个**新锚点** → 删除该簇 → 改写历史段标签。

    「删除该簇」不能省：簇还在时 `identify()` 第③步依旧命中它，
    返回值仍是「不在 SELF_ALIASES」= 他人，等于白归并。

    `is_user` 由服务端的 `SELF_ALIASES` 判定，**不接受客户端传入** ——
    能否触发本人的管家是安全判定，不能让上层随手给 True。
    """
    _check_auth(authorization)
    tid = _resolve_console_tenant(tenant, x_tenant)
    label = str(payload.get("label") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not label or not name:
        raise HTTPException(status_code=400, detail="need label and name")
    from . import voiceprint
    res = voiceprint.merge_cluster(label, name, tid)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=str(res.get("reason") or "merge_failed"))
    return res


@router.put("/api/console/industry/{name}")
def set_industry(name: str, authorization: str | None = Header(default=None)) -> dict:
    """切换行业（与手机端同一逻辑：清识别器缓存，热词即时重建）。"""
    _check_auth(authorization)
    if name not in {i["key"] for i in config.available_industries()}:
        raise HTTPException(status_code=404, detail="industry not found")
    try:
        config.set_industry(name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    asr.invalidate()
    log.info("console: industry switched to %s", name)
    return {
        "ok": True,
        "industry": config.INDUSTRY,
        "label": config.industry_label(config.INDUSTRY),
        "terms": len(config.INDUSTRY_TERMS),
        "fixes": config.INDUSTRY_FIXES,
    }


@router.get("/api/console/industry/{name}/terms")
def get_industry_terms(name: str, authorization: str | None = Header(default=None)) -> dict:
    """词表原文（含 # 注释行），给 App 编辑器做起点。

    general 返回空文本是有意为之：通用行业没有词表文件，
    编辑器从空开始、保存后在数据目录新建一份即可。
    """
    _check_auth(authorization)
    if name != "general" and name not in {i["key"] for i in config.available_industries()}:
        raise HTTPException(status_code=404, detail="industry not found")
    p = config.industry_file(name)
    try:
        text = p.read_text(encoding="utf-8")
        user_editable = True
    except OSError:
        text = ""
        user_editable = True
    return {
        "ok": True,
        "industry": name,
        "label": config.industry_label(name),
        "text": text,
        # 内置词表本身只读，但保存会写到数据目录形成覆盖副本，所以永远可编辑
        "user_editable": user_editable,
        "current": name == (config.INDUSTRY or "general"),
    }


@router.put("/api/console/industry/{name}/terms")
def put_industry_terms(name: str, body: dict = Body(default={}),
                       authorization: str | None = Header(default=None)) -> dict:
    """保存编辑后的词表（写到数据目录，覆盖内置副本），并热生效。

    body: {"text": "# label: xxx\\n术语\\n错写=正写\\n"}
    校验必须在服务端做：一行超过 80 字符的「词」多半是把整句话粘进来了，
    会把 whisper prompt 与热词机制一起带偏。
    """
    _check_auth(authorization)
    text = str((body or {}).get("text") or "")
    lines = [s.strip() for s in text.splitlines() if s.strip() and not s.strip().startswith("#")]
    if len(lines) > 2000:
        raise HTTPException(status_code=400, detail=f"词表最多 2000 行，当前 {len(lines)}")
    for s in lines:
        if len(s) > 80:
            raise HTTPException(status_code=400,
                                detail=f"单行过长（{len(s)} 字符，上限 80）：{s[:40]}…")
        if "=" in s:
            wrong, _, right = s.partition("=")
            if not wrong.strip() or not right.strip():
                raise HTTPException(status_code=400, detail=f"映射行两侧都不能为空：{s}")
    try:
        p = config.write_industry_text(name, text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 当前行业被编辑：立刻重算词表/热词/prompt 并清识别器缓存
    if name == (config.INDUSTRY or "general"):
        config._apply_industry(config.INDUSTRY)
        asr.invalidate()
    log.info("console: industry terms saved: %s (%d lines)", name, len(lines))
    return {
        "ok": True,
        "industry": name,
        "terms": len(config.load_industry_terms(name)),
        "fixes": len(config.load_industry_fixes(name)),
        "path": str(p),
    }


# ---- 设置页后端 ----
# App 与 Web 工作台共用同一套接口与同一份 schema，服务端是唯一真相：
# 加一个可配项只改 config.CONFIG_SCHEMA，两端同时出现，不用各写一遍表单。


def _field(name: str) -> dict | None:
    for g in config.CONFIG_SCHEMA:
        for f in g["fields"]:
            if f["name"] == name:
                return f
    return None


def _config_payload() -> dict:
    return {
        "schema": config.CONFIG_SCHEMA,
        "values": config.config_values(),
        "secrets_set": config.secret_status(),
        "env_file": str(config.env_file_path()),
        "llm": config.llm_settings(),
        "presets": config.LLM_PRESETS,
        "industries": config.available_industries(),
        "industry": config.INDUSTRY or "general",
        "industry_locked": bool(getattr(config, "_ENV_INDUSTRY", "")),
    }


def _coerce(field: dict, value) -> str:
    """把前端传来的值规范化成 env.local 里的字符串，并做范围/取值校验。

    校验必须在服务端做：App 端可以不做，但 env.local 是能被手改的，
    而一个越界的阈值（比如聚类阈值 5.0）会让说话人全部合成一个人，
    且日志里不会有任何报错。
    """
    t = field.get("type")
    if t == "bool":
        return "1" if (value is True or str(value) in ("1", "true", "True")) else "0"
    s = str(value)
    if t == "int":
        try:
            n = int(float(s))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{field['name']} 不是整数") from e
        lo, hi = field.get("min"), field.get("max")
        if lo is not None:
            n = max(n, int(lo))
        if hi is not None:
            n = min(n, int(hi))
        return str(n)
    if t == "number":
        try:
            n = float(s)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"{field['name']} 不是数字") from e
        lo, hi = field.get("min"), field.get("max")
        if lo is not None:
            n = max(n, float(lo))
        if hi is not None:
            n = min(n, float(hi))
        return str(round(n, 4))
    # choice 与 select 都是「从固定枚举里挑一个」，取值必须落在 options 内。
    # select 早先没走这条校验（当时它的 options 是裸字符串、取不出 o["value"]），
    # options 规范化成 {value,label} 之后一并对齐（2026-09-16）。
    if t in ("choice", "select"):
        allowed = {o["value"] for o in field.get("options", [])}
        if s not in allowed:
            raise HTTPException(status_code=400,
                                detail=f"{field['name']} 取值非法：{s}")
        return s
    return s


@router.get("/api/console/config")
def get_config(authorization: str | None = Header(default=None)) -> dict:
    """设置页全量数据：schema + 当前值 + 厂商预设，一次拿完。"""
    _check_auth(authorization)
    return _config_payload()


@router.put("/api/console/config")
def put_config(patch: dict = Body(default={}),
               authorization: str | None = Header(default=None)) -> dict:
    """保存设置到 env.local，并让当前进程立刻生效。

    body: {"values": {"DH_LLM_MODEL": "...", "DH_LLM_API_KEY": null, ...}}
    - null 表示「未修改」，密钥字段尤其依赖它：设置页只显示「已设置」面具，
      用户没改就不回传，不能因此把已保存的 Key 清空。
    """
    _check_auth(authorization)
    raw = (patch or {}).get("values") or {}
    clean: dict[str, str] = {}
    for k, v in raw.items():
        f = _field(k)
        if f is None:
            continue          # 忽略未知项，别把 UI 的临时字段写进 env.local
        if v is None:
            continue
        clean[k] = _coerce(f, v)

    if not clean:
        return {"ok": True, "written": [], "restart_required": [],
                "config": _config_payload()}

    written = config.set_env_values(clean)
    # ASR 后端/热词改了必须清识别器缓存，否则「已保存」却仍在用旧实例跑。
    if any(k.startswith("DH_ASR") or k == "DH_HOTWORD_MAX" for k in clean):
        asr.invalidate()
    log.info("console: config saved %s", ",".join(written))
    restart = [k for k in clean if (_field(k) or {}).get("restart")]
    return {
        "ok": True,
        "written": written,
        "restart_required": restart,
        "config": _config_payload(),
    }


@router.post("/api/console/rotate-token")
def rotate_token(authorization: str | None = Header(default=None)) -> dict:
    """一键更换配对 Token：服务端生成强随机值，写 env.local 并热生效。

    更换后所有已配对手机要在设置里重填「配对 Token」；工作台前端拿到
    返回值后会自动切换自己的凭证，不需要手动再填。
    """
    import secrets

    _check_auth(authorization)
    token = "dhm_" + secrets.token_urlsafe(21)
    config.set_env_values({"DH_TOKEN": token})
    log.info("console: pairing token rotated")
    return {"ok": True, "token": token}


@router.post("/api/console/llm/test")
def llm_test(body: dict = Body(default={}),
             authorization: str | None = Header(default=None)) -> dict:
    """连通性测试。测的是**请求体里的参数**，不是已保存的配置。

    这样用户填一半也能测，不必先保存才能验证。未提供的字段沿用已保存值，
    所以「只改了地址、Key 没动」也能测对。
    """
    _check_auth(authorization)
    b = body or {}
    try:
        timeout = int(b.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60
    return llm.probe(
        base_url=b.get("base_url"),
        model=b.get("model"),
        api_key=b.get("api_key"),
        protocol=b.get("protocol"),
        proxy=b.get("proxy"),
        provider=b.get("provider"),
        served_id=b.get("served_id"),
        timeout=timeout,
    )


@router.post("/api/console/llm/models")
def llm_models(body: dict = Body(default={}),
               authorization: str | None = Header(default=None)) -> dict:
    """拉取厂商模型列表：模型名抄错一个字符就是 404，不如让用户直接选。"""
    _check_auth(authorization)
    b = body or {}
    return llm.list_models(
        base_url=b.get("base_url") or "",
        # 密钥空串按「未修改」处理，沿用已保存值（设置页掩码显示，不会回传明文）
        api_key=b.get("api_key") or config.LLM_API_KEY,
        protocol=b.get("protocol") or "openai",
        proxy=b.get("proxy") if b.get("proxy") is not None else config.LLM_PROXY,
    )


# ---- 日志页后端 ----
# 工作台与 Mac 应用读的是**同一份落盘日志**（run.py 的 RotatingFileHandler 写的
# api.log / llm.log）。刻意不做「进程内缓冲日志再吐给前端」：重启后缓冲就空了，
# 而排障最需要的恰恰是重启前那几十行。
#
# 文件名走白名单而不是拼路径：这个接口能读文件，接受任意入参等于开了一个
# 「读本机任意文件」的口子（DH_LOGS_DIR 之外的 %2e%2e%2f 之类）。
_LOG_NAMES = ("api.log", "llm.log")


@router.get("/api/console/logs")
def get_logs(name: str = "api.log", limit: int = 192 * 1024,
             authorization: str | None = Header(default=None)) -> dict:
    """读日志尾部。

    只读尾部：api.log 单文件上限 20 MB（run.py 轮转），整读既慢又会让前端
    反复对大字符串做 diff。从文件中途起读时首行多半是半条，丢掉。
    """
    _check_auth(authorization)
    if name not in _LOG_NAMES:
        raise HTTPException(status_code=400, detail=f"未知日志：{name[:32]}")
    try:
        take = int(limit)
    except (TypeError, ValueError):
        take = 192 * 1024
    take = max(8 * 1024, min(take, 1024 * 1024))

    files = []
    for n in _LOG_NAMES:
        f = config.LOGS_DIR / n
        if f.is_file():
            st = f.stat()
            # mtime 回 epoch 秒，格式化交给浏览器：服务端不必为「显示成什么样子」操心
            files.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime)})

    p = config.LOGS_DIR / name
    text, size = "", 0
    if p.is_file():
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - take))
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        if size > take:
            nl = text.find("\n")
            text = text[nl + 1:] if nl >= 0 else text
    return {"name": name, "text": text, "size": size,
            "truncated": size > take, "dir": str(config.LOGS_DIR), "files": files}


@router.get("/console")
def console_page() -> Response:
    """工作台页面。HTML 无数据，不需要鉴权。

    必须 no-store：页面常改（版式/功能迭代），浏览器对同 URL GET 的
    缓存会让用户看到旧版工作台还以为服务没更新（2026-09-09 实测踩坑）。
    页面本身只有 ~30KB，每次现发没有压力。
    """
    return Response(
        content=_PAGE,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数字人工作台</title>
<!-- 标签页图标与三端 app 图标同源（同一张母版的 data URI）。除了「看起来是同一个东西」，
     它还顺手消掉一个 404：没有 link 时浏览器会去要 /favicon.ico，而服务端没有这个路由，
     控制台里会长期挂着一条 404，排查真问题时是纯噪音。 -->
<link rel="icon" href="__LOGO_DATA_URI__">
<style>
/* ===== 设计系统 =====
   规则：间距只取 --s1..--s8，圆角只取 --r1..--r4，字号只取 --f1..--f6。
   改一处全局生效 —— 此前每张卡各写一套 padding/圆角/字号，是「每页风格不一致」的根因。 */
:root{
  --bg:#0e1014; --panel:#161a22; --panel2:#1d222d; --panel3:#232936;
  --line:#2a3040; --line2:#333c4e;
  --text:#e7eaf0; --dim:#98a1b3; --faint:#6b7484;
  --teal:#2dd4a7; --amber:#f0b429; --red:#ef5350; --blue:#5b9dff; --indigo:#8b8cf0;
  --teal-a:rgba(45,212,167,.14);  --teal-b:rgba(45,212,167,.42);
  --amber-a:rgba(240,180,41,.14); --amber-b:rgba(240,180,41,.42);
  --blue-a:rgba(91,157,255,.14);  --blue-b:rgba(91,157,255,.42);
  --red-b:rgba(239,83,80,.42);
  --indigo-a:rgba(139,140,240,.14); --indigo-b:rgba(139,140,240,.42);

  /* 顶栏底色 / 滚动条 / 悬停叠加 / 主按钮文字 / 背景那层品牌辉光。
     这五个此前是写死在各自规则里的，于是「换主题」只能换一半 ——
     浅色下最难看的恰好就是它们（深色滚动条、白字压在浅绿按钮上）。 */
  --bar:rgba(22,26,34,.9); --scroll:#2c3342;
  --hover:rgba(255,255,255,.015); --hover2:rgba(255,255,255,.03);
  --glow:rgba(45,212,167,.055); --on-accent:#06231b;

  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:20px; --s6:24px; --s7:32px; --s8:40px;
  --r1:6px; --r2:9px; --r3:12px; --r4:16px; --rp:999px;
  --f1:11px; --f2:12px; --f3:13px; --f4:14px; --f5:16px; --f6:20px;
  --ctl:34px; --t:.16s ease;

  --page:3200px;                  /* 版心上限：到这只是居中留白，不再「扩宽不动」 */
  --gutter:clamp(16px,2vw,40px);  /* 左右留白随视口缩放 */
  --read:118ch;                   /* 超宽屏下长文的可读行宽 */
  --edge:max(var(--gutter),calc((100% - var(--page)) / 2 + var(--gutter))); /* 顶栏/标签栏/正文左缘对齐 */
}

/* ===== 浅色主题 =====
   只覆盖颜色词元，间距 / 圆角 / 字号 / 版心一个都不动 —— 换主题不该改变布局，
   否则「浅色下卡片变窄了」这种问题会永远查不完。
   data-theme 由 <head> 里的内联脚本在首帧前定下，
   三种取值：light / dark 明确指定，缺省（跟随系统）也已被解析成具体一档。 */
html[data-theme=light]{
  --bg:#f5f7fa; --panel:#ffffff; --panel2:#f1f4f8; --panel3:#e6ebf2;
  --line:#dde3ec; --line2:#c6cedb;
  --text:#141a24; --dim:#5a6472; --faint:#8b94a3;
  /* 强调色压深一档：深色下 2dd4a7 在白底上对比度只有 1.6:1，字会糊掉 */
  --teal:#0f9d78; --amber:#9a6a00; --red:#cf3a37; --blue:#2c6bcb; --indigo:#5557cf;
  --teal-a:rgba(15,157,120,.10);  --teal-b:rgba(15,157,120,.32);
  --amber-a:rgba(154,106,0,.10);  --amber-b:rgba(154,106,0,.30);
  --blue-a:rgba(44,107,203,.10);  --blue-b:rgba(44,107,203,.30);
  --red-b:rgba(207,58,55,.32);
  --indigo-a:rgba(85,87,207,.10);  --indigo-b:rgba(85,87,207,.32);
  --bar:rgba(255,255,255,.86); --scroll:#c3cad6;
  --hover:rgba(20,26,36,.035); --hover2:rgba(20,26,36,.05);
  --glow:rgba(15,157,120,.06); --on-accent:#ffffff;
  color-scheme:light;
}
html[data-theme=dark]{color-scheme:dark}

*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased;text-size-adjust:100%}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;
  background:radial-gradient(1400px 600px at 78% -12%,var(--glow),transparent 62%),var(--bg);
  color:var(--text);font:var(--f4)/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--scroll);border-radius:var(--rp);border:2px solid var(--bg)}
::-webkit-scrollbar-track{background:transparent}

/* ===== 顶栏 / 标签栏：背景通栏，内容与正文版心左对齐 ===== */
header{display:flex;align-items:center;gap:var(--s3);flex-wrap:wrap;
  padding:var(--s3) var(--edge);background:var(--bar);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
h1{display:flex;align-items:center;gap:var(--s2);font-size:var(--f5);font-weight:600;
  margin:0;letter-spacing:.2px}
/* 顶栏 logo：与 mac / Windows 两端图标同源（由 tools/make-app-icons.py 从同一张母版派生，
   见 server/app/_logo.py）。内嵌 data URI 而不是外链图片 —— 页面是自包含的自签 HTTPS
   响应，多一个静态资源就多一条「局域网里拿不到」的失败路径。 */
h1 img{width:22px;height:22px;border-radius:5px;display:block;flex:0 0 auto}
#svc{display:inline-flex;align-items:center;gap:var(--s2);color:var(--dim);font-size:var(--f2)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;flex:0 0 auto}
.dot.on{background:var(--teal);box-shadow:0 0 6px var(--teal-b)}
.dot.off{background:var(--red)} .dot.wait{background:var(--amber)}
.spacer{flex:1}
#tenantSel{min-width:150px;max-width:min(260px,34vw);font-weight:500}
#token{width:min(180px,32vw)}

/* ===== 控件：统一高度 / 圆角 / 焦点环 ===== */
input,select,button{font:inherit;font-size:var(--f3);color:var(--text);background:var(--panel2);
  border:1px solid var(--line);border-radius:var(--r2);padding:0 var(--s3);min-height:var(--ctl);
  transition:border-color var(--t),background var(--t),color var(--t)}
input:focus,select:focus{outline:none;border-color:var(--teal)}
input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
input[type=file]{padding:var(--s1) var(--s2);color:var(--dim);font-size:var(--f2)}
button{cursor:pointer} button:hover{border-color:var(--teal)}
button.primary{background:var(--teal);color:var(--on-accent);border-color:transparent;font-weight:600}
button.primary:hover{filter:brightness(1.08)}
button.danger:hover{border-color:var(--red);color:var(--red)}

nav{display:flex;gap:var(--s1);padding:var(--s3) var(--edge) 0;flex-wrap:wrap}
nav button{background:transparent;border:1px solid transparent;color:var(--dim)}
nav button:hover{color:var(--text);background:var(--hover2)}
nav button.on{background:var(--panel2);border-color:var(--line);color:var(--text);font-weight:600}

/* ===== 正文骨架：内容短时页脚也贴底 ===== */
main{flex:1;width:100%;max-width:var(--page);margin-inline:auto;
  padding:var(--s5) var(--gutter) var(--s8);
  display:flex;flex-direction:column;gap:var(--s4)}
main > section{display:flex;flex-direction:column;gap:var(--s4)}

/* ===== 卡片 ===== */
.card{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:var(--r4);
  padding:var(--s4) var(--s5);transition:border-color var(--t)}
.card:hover{border-color:var(--line2)}
.card h2{font-size:var(--f2);font-weight:600;margin:0 0 var(--s3);color:var(--faint);letter-spacing:.4px}
.card > p{margin:var(--s3) 0 0}

/* ===== 网格：flex 自适应 =====
   卡片数少于列数时自动撑满整行（例：只有 1 台设备时不再只占四分之一宽留三份空白）；
   卡片宽度随视口增长，不再「窗口拉宽了、卡片不动」。 */
.grid,.grid2{display:flex;flex-wrap:wrap;gap:var(--s4);align-items:stretch}
/* 基准宽必须扣掉 gap，否则「4 张卡 × 25% + 3 个 gap」会超出一行，把第 4 张挤到下一行 */
.grid > *{flex:1 1 calc(25% - var(--s4));min-width:190px}
.grid2 > *{flex:1 1 calc(25% - var(--s4));min-width:280px}
.grid > .tm,.grid2 > .tm{flex:1 1 100%}
/* 两张卡并排：够宽（≈1700px 以上）才并排，否则上下堆叠 */
.pair{display:flex;flex-wrap:wrap;gap:var(--s4);align-items:stretch}
.pair > *{flex:1 1 800px;min-width:0}

/* ===== 行内明细 / 表格 / 代码块 ===== */
.kv{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s4);
  padding:5px 0;border-bottom:1px solid var(--line)}
.kv:last-child{border-bottom:0}
.kv span:first-child{color:var(--dim);flex:0 0 auto}
.kv span:last-child{min-width:0;text-align:right;word-break:break-word}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:var(--f2)}
pre{white-space:pre-wrap;word-break:break-word;margin:0;max-height:64vh;overflow:auto;
  font:var(--f3)/1.75 -apple-system,"PingFang SC",sans-serif}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:var(--s2) var(--s3);border-bottom:1px solid var(--line);font-size:var(--f3)}
th{color:var(--faint);font-weight:500;font-size:var(--f2)}
td:first-child,th:first-child{white-space:nowrap}
tr:hover td{background:var(--hover)}

.pill{display:inline-block;padding:2px var(--s2);border-radius:var(--rp);font-size:var(--f2);
  background:var(--panel2);border:1px solid var(--line);white-space:nowrap}
.pill.on{background:var(--teal-a);border-color:var(--teal-b);color:var(--teal)}
.pill.warn{background:var(--amber-a);border-color:var(--amber-b);color:var(--amber)}
.pill.info{background:var(--blue-a);border-color:var(--blue-b);color:var(--blue)}
.row{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}

/* 日报「原始稿 / 干净稿 / 总结」：此前 .on 只对 nav 生效，选中态没有任何样式 —— 补上 */
main button[data-ver]{min-width:84px}
main button[data-ver].on{background:var(--teal-a);border-color:var(--teal-b);color:var(--teal);font-weight:600}

.ind,.tcard{border:1px solid var(--line);border-radius:var(--r3);background:var(--panel2);
  transition:border-color var(--t),background var(--t)}
.ind{display:flex;justify-content:space-between;align-items:center;gap:var(--s3);
  padding:var(--s3) var(--s4);cursor:pointer}
.tcard{padding:var(--s3) var(--s4)}
.ind:hover{border-color:var(--teal)}
.ind.on{border-color:var(--teal);background:var(--teal-a)}
.tcard:hover{border-color:var(--teal)}
.tcard.on{border-color:var(--teal);background:var(--teal-a)}
.tcard .tn{font-weight:600}
.tcard .tm{color:var(--faint);font-size:var(--f2);margin-top:2px}

.msg{padding:var(--s2) var(--s3);border-radius:var(--r2);background:var(--panel2);font-size:var(--f3);
  border:1px solid var(--line)}
.err{color:var(--red);border-color:var(--red-b)}
.ok{color:var(--teal);border-color:var(--teal-b)}
/* 词表编辑弹窗（行业页）。::backdrop 是 <dialog> 自带的遮罩，不自己搭一层。 */
dialog#indDlg{width:min(680px,92vw);box-sizing:border-box;padding:var(--s4);
  border:1px solid var(--line);border-radius:var(--r4);background:var(--panel);
  color:var(--text);box-shadow:0 24px 60px rgba(0,0,0,.35)}
dialog#indDlg::backdrop{background:rgba(0,0,0,.45)}
dialog#indDlg h2{margin:0 0 var(--s2)}
dialog#indDlg textarea{width:100%;box-sizing:border-box;padding:var(--s3);
  border:1px solid var(--line);border-radius:var(--r2);background:var(--panel2);
  color:var(--text);resize:vertical;
  font:var(--f2)/1.7 ui-monospace,SFMono-Regular,Menlo,monospace}
.hide{display:none}
footer{color:var(--faint);font-size:var(--f2);text-align:center;
  padding:var(--s6) var(--edge) var(--s4)}

/* ===== 设置页：字段卡 =====
   每项一张小卡而不是「标签 + 控件」的密集表：这些项都有 2~3 行说明文字，
   密集排布会让说明贴到下一个控件上，读不清哪句属于谁。 */
.fld{padding:var(--s3) var(--s4);border:1px solid var(--line);border-radius:var(--r3);
  background:var(--panel2);min-width:0;transition:border-color var(--t)}
.fld:hover{border-color:var(--line2)}
.fld.off{display:none}
.fld .fl{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;
  font-weight:600;font-size:var(--f3)}
.fld .fh{color:var(--faint);font-size:var(--f2);margin:var(--s1) 0 var(--s2)}
.fld input,.fld select,.fld textarea{width:100%}
.fld input[type=checkbox]{width:auto;min-height:0;accent-color:var(--teal)}
textarea{font:inherit;font-size:var(--f3);color:var(--text);background:var(--panel2);
  border:1px solid var(--line);border-radius:var(--r2);padding:var(--s2) var(--s3);
  min-height:calc(var(--ctl) * 2);resize:vertical}
textarea:focus{outline:none;border-color:var(--teal)}
input[type=checkbox]{min-height:0;accent-color:var(--teal)}
.gridset{display:flex;flex-wrap:wrap;gap:var(--s3);align-items:flex-start}
.gridset > *{flex:1 1 calc(50% - var(--s3));min-width:300px}
#logBody{background:var(--panel2);border:1px solid var(--line);border-radius:var(--r2);
  padding:var(--s3) var(--s4)}

/* ===== 日报时间轴 =====
   与 Mac 端 Timeline.swift / 手机 APK ReportPage.kt 同款：解析规则、
   展示顺序（最新在上）、换段标注阈值（≥5 分钟）、本人高亮色三端一致，
   保证「同一个日期，三端看到的一样」。*/
.chips{display:flex;flex-wrap:wrap;gap:var(--s2);margin-bottom:var(--s3)}
.chip{padding:5px var(--s3);border-radius:var(--rp);font-size:var(--f2);line-height:1.5;
  cursor:pointer;user-select:none;white-space:nowrap;color:var(--dim);
  background:var(--panel2);border:1px solid var(--line);
  transition:border-color var(--t),background var(--t),color var(--t)}
.chip:hover{border-color:var(--line2)}
.chip.on{background:var(--indigo-a);border-color:var(--indigo-b);color:var(--indigo);font-weight:600}
.chip.on[data-self="1"]{background:var(--teal-a);border-color:var(--teal-b);color:var(--teal)}

/* 全天滑轨：每根刻度 = 一个语音段，密度即「什么时候在说话」。
   canvas 按 DPR 重画且宽度交给 CSS 100%，所以窗口拉宽拉窄刻度跟着重排。*/
.railhd{display:flex;align-items:baseline;justify-content:space-between;
  gap:var(--s3);font-size:var(--f2);color:var(--faint);margin-bottom:var(--s1)}
.railhd b{font-size:var(--f3);font-weight:500;color:var(--dim);
  font-variant-numeric:tabular-nums}
.railhd b.drag{color:var(--amber)}
canvas.railcv{display:block;width:100%;height:44px;touch-action:none;cursor:pointer}

/* 时间线正文：左侧时间槽 + 竖线节点 + 说话人标签。
   时间只在换时段（与上一条间隔 ≥5 分钟）处标注，否则整列重复同一分钟看不清。*/
.tl{position:relative;max-height:64vh;overflow:auto;
  border-top:1px solid var(--line);padding-top:var(--s2)}
.tlr{display:grid;grid-template-columns:52px 12px minmax(0,1fr);column-gap:10px;
  padding:3px 0;align-items:start}
.tlr:hover{background:var(--hover)}
.tlt{font-size:var(--f2);line-height:20px;text-align:right;color:var(--faint);
  font-variant-numeric:tabular-nums}
.tlt.self{color:var(--teal)}
.tln{position:relative;align-self:stretch;min-height:14px}
.tln::before{content:"";position:absolute;left:5px;top:0;bottom:0;width:2px;background:var(--line)}
.tln i{position:absolute;border-radius:50%;background:var(--indigo);
  left:3.5px;top:6px;width:5px;height:5px}
.tln.big i{left:2px;top:4px;width:8px;height:8px}
.tln.self i{background:var(--teal)}
.tlbx{min-width:0}
.tls{font-size:var(--f2);font-weight:600;color:var(--dim);margin-bottom:2px}
.tls.self{color:var(--teal)}
.tlb{font-size:var(--f3);line-height:1.6;color:var(--text);word-break:break-word;
  white-space:pre-wrap;max-width:var(--read)}
.tlempty{padding:var(--s4);color:var(--faint);font-size:var(--f2)}

/* 「更多栏目」殿后：时间轴卡（我的动态/他人动态/今日回顾）已渲染成行，
   关键原话/待办/金句/状态这些非时间序列的栏目留在这里，一个都不丢。*/
.tlf{border-top:1px solid var(--line);margin-top:var(--s4);padding-top:var(--s4)}
.tlf > h3{font-size:var(--f2);font-weight:600;color:var(--faint);
  letter-spacing:.4px;margin:0 0 var(--s3)}
.sumcard{padding:var(--s3) var(--s4);border:1px solid var(--line);border-radius:var(--r3);
  background:var(--panel2);min-width:0}
.sumcard b{display:block;font-size:var(--f2);font-weight:600;color:var(--teal);
  margin-bottom:var(--s2)}
.sumcard p{margin:0;font-size:var(--f3);line-height:1.6;color:var(--text);
  white-space:pre-wrap;word-break:break-word}

/* ===== 超宽屏：长文收行宽（避免一行 200 字），其余一律铺满 ===== */
@media (min-width:1900px){
  .card > p{max-width:var(--read)}
  pre{max-width:var(--read)}
}
@media (max-width:820px){
  h1{font-size:var(--f4)}
  .grid > *,.grid2 > *{flex:1 1 100%;min-width:0}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
}
/* 角色页固定朗读稿：宽屏下同样收行宽，否则一行两百字 */
@media (min-width:1900px){#spScript{max-width:var(--read)}}
</style>
<script>
/* 主题必须在首帧之前落定，否则「跟随系统 + 系统是浅色」会先闪一下深色再翻白，
   比没有主题还难受。所以这个小块宁可单独放在 <style> 后面 —— 放到正文末尾那份
   主脚本里就晚了（那时首帧已经画完）。 */
(function () {
  var mode = 'system';
  try { mode = localStorage.getItem('dh_theme') || 'system'; } catch (e) {}
  var light = mode === 'light' ||
    (mode === 'system' && window.matchMedia &&
     window.matchMedia('(prefers-color-scheme: light)').matches);
  document.documentElement.setAttribute('data-theme', light ? 'light' : 'dark');
})();
</script>
</head>
<body>
<header>
  <h1><img src="__LOGO_DATA_URI__" alt="">数字人工作台</h1>
  <span id="svc"><span class="dot wait"></span> <span id="svcText">连接中…</span></span>
  <div class="spacer"></div>
  <button id="themeBtn" title="界面主题：跟随系统 / 浅色 / 深色">跟随系统</button>
  <button id="reloadBtn" title="重新读取当前页的数据">刷新</button>
  <select id="tenantSel" title="租户空间：每部手机一个空间，数据完全隔离"></select>
  <input id="token" placeholder="配对 Token" type="password">
  <button id="save">保存</button>
</header>

<nav>
  <button data-tab="overview" class="on">概览</button>
  <button data-tab="report">日报</button>
  <button data-tab="industry">行业词表</button>
  <button data-tab="speakers">角色</button>
  <button data-tab="devices">设备与管家</button>
  <button data-tab="spaces">空间管理</button>
  <button data-tab="settings">设置</button>
  <button data-tab="logs">日志</button>
</nav>

<main>
  <div id="msg" class="msg hide"></div>

  <section id="tab-overview">
    <div class="grid2">
      <div class="card"><h2>服务</h2><div id="ovService"></div></div>
      <div class="card"><h2>模型</h2><div id="ovModels"></div></div>
      <div class="card"><h2>行业</h2><div id="ovIndustry"></div></div>
      <div class="card"><h2>安全</h2>
        <div class="row" style="margin-bottom:var(--s2)">
          <button id="rotateToken">生成新 Token</button>
          <code id="newToken" class="mono hide"></code>
          <button id="copyToken" class="hide">复制</button>
        </div>
        <p style="color:var(--dim);font-size:var(--f2);margin:0">
          更换后所有手机要在设置里重填「配对 Token」（外网中转同样用这一个）。
          工作台自动切换新 Token，无需手动改。
        </p>
      </div>
    </div>
    <div class="pair">
      <div class="card"><h2>当前空间 · 日报日期</h2><div id="ovDays"></div></div>
      <div class="card"><h2>硬盘占用（长期驻留看这里）</h2><div id="ovStorage"></div></div>
    </div>
  </section>

  <section id="tab-report" class="hide">
    <div class="card">
      <div class="row">
        <select id="dateSel"></select>
        <button id="loadReport" class="primary">加载</button>
        <!-- 与 Mac 原生日报页同款的两个动作。原生把它们分放头部与底部，
             这里并排在操作行：功能一致，少一次滚动。 -->
        <button id="regenReport">重新生成</button>
        <button id="copyReport">复制全文</button>
        <span id="repMeta" style="color:var(--dim)"></span>
      </div>
    </div>
    <div class="card">
      <div class="row" style="margin-bottom:var(--s3)">
        <button data-ver="raw" class="on">原始稿</button>
        <button data-ver="clean">干净稿</button>
        <button data-ver="summary">总结</button>
      </div>

      <!-- 说话人筛选：本人置顶，其余按发言数降序 -->
      <div id="repChips" class="chips hide"></div>

      <!-- 全天滑轨：点击或拖动定位，松手吸附到最近的有内容刻度 -->
      <div id="repRailBox" class="hide">
        <div class="railhd">
          <span>时间线 · 点击或拖动定位</span><b id="repRailAt">--:--</b>
        </div>
        <canvas id="repRail" class="railcv"></canvas>
      </div>

      <!-- 时间线正文：最新在最上；「更多栏目」卡殿后 -->
      <div id="repList" class="tl hide"></div>

      <!-- 回退：解析不出时间行（旧稿 / 模型没按格式输出）时整段纯文本，一个字都不丢 -->
      <pre id="repBody">选个日期，点「加载」。</pre>
    </div>
  </section>

  <section id="tab-industry" class="hide">
    <div class="pair">
      <div class="card">
        <h2>切换行业（只影响之后的转写）</h2>
        <div id="indList" class="grid"></div>
        <p style="color:var(--dim);font-size:var(--f2);margin:var(--s3) 0 0">
          已生成的历史日报不会自动重跑。要按新行业整体纠偏，在 Mac 上执行：<br>
          <code class="mono">python3 &lt;仓库&gt;/server/rebuild_report.py --tenant=&lt;空间ID&gt; 2026-09-03</code>
        </p>
      </div>
      <div class="card"><h2>当前词表</h2><div id="indDetail"></div></div>
    </div>
  </section>

  <section id="tab-speakers" class="hide">
    <div class="card"><h2>当前空间 · 已注册的具名角色</h2><div id="spList"></div></div>
    <div class="card">
      <h2>上传录音创建角色</h2>
      <p style="color:var(--dim);font-size:var(--f2);margin:0 0 var(--s3)">
        注册一次需要该角色<b>连续朗读 30 秒</b>。念下面这段固定文字即可 ——
        内容固定是为了让声纹稳定：同一段文字、同一个人的声音特征才可比，
        各读各的会让锚点偏掉。<b>不必再拿手机录一遍</b>，把这 30 秒的 WAV 传到这里就行。
      </p>
      <div id="spScript" style="padding:var(--s3) var(--s4);border:1px solid var(--line);
        border-left:3px solid var(--teal);border-radius:var(--r3);background:var(--panel2);
        white-space:pre-wrap;font-size:var(--f3);line-height:1.8"></div>
      <div class="row" style="margin-top:var(--s3)">
        <button id="spCopy">复制朗读文本</button>
        <input id="spName" placeholder="角色名（1-16 字，如 张三）" style="flex:0 1 220px;min-width:170px">
        <input id="spFile" type="file" accept=".wav,audio/wav,audio/x-wav">
        <button id="spUpload" class="primary">上传并注册</button>
      </div>
      <div id="spProg" style="margin-top:var(--s3)"></div>
      <p style="color:var(--dim);font-size:var(--f2);margin:var(--s3) 0 0">
        录音要求：单人、无背景音乐、无旁人插话，手机自带录音机或电脑录制都可以；
        采样率不限（服务端自动重采样）。太短的片段（不足 30 秒或只剩几秒有效人声）
        会提不出稳定声纹，接口会直接拒绝。<br>
        录进 <b>当前空间</b>（右上角「空间」切换）。角色名留空 = 注册为「本人」
        （与手机端「配对」等价）；名字 1-16 字，不能含方括号。
        同名再传一次会覆盖旧声纹，历史日报不受影响。
      </p>
    </div>
  </section>

  <section id="tab-devices" class="hide">
    <div class="card"><h2>设备与管家（服务端权威：每台设备用哪个空间、哪个管家、能不能用本机 Hermes）</h2>
      <div id="devList" class="grid2"></div>
      <p style="color:var(--dim);font-size:var(--f2);margin:var(--s3) 0 0">
        设备第一次上传时会按客户端自报的空间<b>自动登记</b>到这里。在这里改过之后
        <b>以服务端为准</b>——客户端那边即使还留着旧的空间 ID，数据也会落到这里指定的空间
        （服务端会记一条「自报与档案不一致」的告警）。<br>
        「本机管家」关掉后，这台设备的对话只走通用助手，<b>不读门店实时数据、也不能控制设备</b>；
        适合访客机、演示机这类不该碰门店东西的设备。旧版 APK 不带设备身份，一律按默认（允许）处理。
      </p>
    </div>
    <div class="card">
      <h2>管家角色</h2>
      <div id="botList" class="grid2"></div>
      <div class="row" style="margin-top:var(--s3)">
        <input id="newBotId" placeholder="角色 ID（小写字母开头，如 store）" style="flex:0 1 250px;min-width:170px">
        <input id="newBotName" placeholder="显示名（如 门店管家）" style="flex:0 1 180px;min-width:150px">
        <button id="addBot" class="primary">新建角色</button>
      </div>
      <p style="color:var(--dim);font-size:var(--f2);margin:var(--s3) 0 0">
        多台设备可以共用「默认管家」，也可以各自建专属角色。角色 ID 会随对话请求带给 Hermes；
        角色级关掉「本机管家」时，绑到它的所有设备一律不走本机 Hermes。
        角色的「专用地址」留空 = 用服务端全局的 Hermes 地址。
      </p>
    </div>
  </section>

  <section id="tab-spaces" class="hide">
    <div class="card"><h2>租户空间（每部手机一个空间，数据目录级物理隔离）</h2>
      <div id="spaceList" class="grid2"></div>
      <p style="color:var(--dim);font-size:var(--f2);margin:var(--s3) 0 0">
        新成员接入：① 在这里创建空间（如 zhangsan / 张三）→ ② 成员手机设置页填
        「空间 ID」→ ③ 成员上传的录音、日报、声纹、角色全部落进
        <span class="mono">data/tenants/&lt;空间ID&gt;/</span>，与其他人互不可见。
        备份时可选单个空间或全部。删除空间只移出注册表，数据目录保留待人工确认。
      </p>
    </div>
    <div class="card"><h2>新增空间</h2>
      <div class="row">
        <input id="newTid" placeholder="空间 ID（小写字母开头，如 zhangsan）" style="flex:0 1 280px;min-width:170px">
        <input id="newTname" placeholder="显示名（如 张三）" style="flex:0 1 180px;min-width:150px">
        <button id="addTenant" class="primary">创建</button>
      </div>
    </div>
  </section>

  <section id="tab-settings" class="hide">
    <div class="card">
      <h2>模型与接口 · 当前生效</h2>
      <div class="grid" id="setCurrent"></div>
      <div class="row" style="margin-top:var(--s4)">
        <button id="setTest">测试连通性</button>
        <button id="setModels">拉取模型列表</button>
        <span id="setOut" class="pill hide"></span>
      </div>
      <p style="color:var(--faint);font-size:var(--f2);margin:var(--s3) 0 0">
        测的是下面表单里当前填的内容，不必先保存；密钥留空表示不修改，会沿用已保存的 Key。
        「拉取模型列表」成功后，点「模型名」输入框可以直接从下拉里选，省得手抄出错。
      </p>
    </div>

    <div class="card">
      <h2>厂商预设（点一下自动填「接口地址 / 协议 / 模型名」）</h2>
      <div class="row" id="setPresets"></div>
      <p style="color:var(--faint);font-size:var(--f2);margin:var(--s3) 0 0">
        预设只填地址与模型名，API Key 要自己去厂商后台申请；点预设之后会提示申请地址。
      </p>
    </div>

    <div id="setGroups"></div>
    <datalist id="modelList"></datalist>

    <div class="card">
      <h2>保存与维护</h2>
      <div class="row">
        <button id="setSave" class="primary">保存设置</button>
        <span id="setDirty" class="pill warn hide">有未保存的修改</span>
        <button id="setBackup">立即备份当前空间</button>
        <button id="setBackupAll">备份全部空间</button>
      </div>
      <div class="gridset" style="margin-top:var(--s4)">
        <div class="card" style="background:var(--panel2)">
          <h2>最近备份（数据目录 backups/）</h2>
          <div id="setBackups">加载中…</div>
        </div>
        <div class="card" style="background:var(--panel2)">
          <h2>配置文件</h2>
          <div id="setEnvMeta"></div>
        </div>
      </div>
      <p style="color:var(--faint);font-size:var(--f2);margin:var(--s3) 0 0">
        设置统一写进 env.local —— Mac 应用、Windows 桌面壳、命令行启动共用同一份，
        不会出现「这里改了、那里还是旧的」。标「需重启」的项保存后要重启服务才生效：
        桌面壳在托盘菜单点「重启」，Mac 应用在菜单栏点「重启」。
        备份包会把归档库、转写文本与语料打包，可随时导出，不影响正在跑的服务。
      </p>
    </div>
  </section>

  <section id="tab-logs" class="hide">
    <div class="card">
      <h2>服务日志（与服务端落盘的同一份文件）</h2>
      <div class="row">
        <select id="logFile"></select>
        <button id="logReload">刷新</button>
        <label class="row" style="gap:var(--s2);color:var(--dim);font-size:var(--f2)">
          <input type="checkbox" id="logAuto">每 3 秒自动刷新
        </label>
        <button id="logCopy">复制全文</button>
        <span id="logMeta" class="pill"></span>
      </div>
      <pre id="logBody" style="margin-top:var(--s3)">切到这个页签就会加载。</pre>
      <p style="color:var(--faint);font-size:var(--f2);margin:var(--s3) 0 0">
        只显示末尾约 190&nbsp;KB：api.log 单文件上限 20&nbsp;MB，写满会自动轮转成
        <span class="mono">api.log.1</span>…，整读没有意义。完整日志目录：
        <span class="mono" id="logDir">—</span>
        &nbsp;·&nbsp;Web 工作台与 Mac 应用看的是同一份文件。
      </p>
    </div>
  </section>
</main>

<!-- 词表编辑器。刻意放在 <main> 之外，不塞进 #tab-industry：
     <dialog> 在 top layer 渲染，但祖先一旦带 display:none（页签的 .hide）就
     整个盒子树不生成 —— 表现是 showModal() 返回正常、屏幕上什么都没有。
     挡在 <main> 外面，这个前提条件就永远不成立。 -->
<dialog id="indDlg">
  <h2 id="indDlgT">编辑词表</h2>
  <p style="color:var(--dim);font-size:var(--f2);margin:0 0 var(--s3)">
    首行写 <span class="mono"># label: 中文名</span>；每行一个术语，或写
    <span class="mono">错写=正写</span> 做同音替换（一个字都不能差，别把整句话粘进来）。
    保存后立即生效，只影响之后的转写，已生成的日报不追溯。
  </p>
  <textarea id="indDlgText" rows="14" spellcheck="false"></textarea>
  <div id="indDlgMsg" style="color:var(--dim);font-size:var(--f2);margin-top:var(--s2)"></div>
  <div class="row" style="justify-content:flex-end;margin-top:var(--s3)">
    <button id="indDlgCancel">取消</button>
    <button id="indDlgSave" class="primary">保存</button>
  </div>
</dialog>
<footer>数字人语料服务 · 多租户工作台</footer>

<script>
const $ = id => document.getElementById(id);
let TOKEN = localStorage.getItem('dh_token') || '';
let TENANT = localStorage.getItem('dh_tenant') || 'default';
let STATE = null, REPORT = null, VER = 'raw';

$('token').value = TOKEN;
$('save').onclick = async () => {
  TOKEN = $('token').value.trim();
  localStorage.setItem('dh_token', TOKEN);
  // 先刷新再报结果：boot() 成功时会 say('')，反着写「Token 已保存」会被立刻清掉
  await boot();
  say('Token 已保存', 'ok');
};

// ---------- 主题（跟随系统 / 浅色 / 深色） ----------
// 三端共用这一页：Mac 内嵌窗口、Windows 桌面壳、浏览器直开都是它。
// 选择落 localStorage，各端互不干扰（同一台机器上浏览器选了浅色，
// 不影响壳窗口）。首帧的取值已经在 <head> 里解析过，这里只负责后续切换。
const THEME_MODES = [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']];
const THEME_LIGHT_Q = window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: light)') : null;

function themeSaved() {
  try { return localStorage.getItem('dh_theme') || 'system'; } catch (e) { return 'system'; }
}
function applyTheme(mode, remember) {
  // 「跟随系统」要在这里解析成具体一档：CSS 里只认 light / dark 两个明确值，
  // 免得同一套浅色变量在「显式浅色」与「跟随系统的浅色」两处各写一遍、然后漂移。
  const light = mode === 'light' || (mode === 'system' && !!THEME_LIGHT_Q && THEME_LIGHT_Q.matches);
  document.documentElement.setAttribute('data-theme', light ? 'light' : 'dark');
  $('themeBtn').textContent = (THEME_MODES.find(t => t[0] === mode) || THEME_MODES[0])[1];
  if (remember) { try { localStorage.setItem('dh_theme', mode); } catch (e) {} }
}
applyTheme(themeSaved(), false);
$('themeBtn').onclick = () => {
  const i = THEME_MODES.findIndex(t => t[0] === themeSaved());
  const next = THEME_MODES[(i + 1) % THEME_MODES.length][0];
  applyTheme(next, true);
};
// 停在「跟随系统」时，系统换主题要跟着变；否则页面会一直停在打开那一刻的配色，
// 用户在系统里切了浅色，只有这个窗口还是黑的。
if (THEME_LIGHT_Q) {
  const followSystem = () => { if (themeSaved() === 'system') applyTheme('system', false); };
  if (THEME_LIGHT_Q.addEventListener) THEME_LIGHT_Q.addEventListener('change', followSystem);
  else if (THEME_LIGHT_Q.addListener) THEME_LIGHT_Q.addListener(followSystem);
}

// ---------- 顶栏「刷新」 ----------
// 桌面窗口里没有菜单栏、也没有浏览器那个 ⌘R，这一页本身又是「数据不刷新就一直是旧的」，
// 所以给一个显式入口。各页签的数据来源不同，按当前页签分派，不要一律 boot()。
$('reloadBtn').onclick = () => {
  const on = document.querySelector('nav button.on');
  const name = on ? on.dataset.tab : 'overview';
  if (name === 'logs') return loadLogs();
  if (name === 'settings') return loadSettings();
  if (name === 'devices') return loadDevices().catch(e => say(e.message, 'err'));
  if (name === 'report') return $('loadReport').click();
  // 概览 / 行业词表 / 角色 / 空间管理 都出自 /api/console/state 一次请求
  return boot();
};

$('tenantSel').onchange = () => {
  TENANT = $('tenantSel').value;
  localStorage.setItem('dh_tenant', TENANT);
  REPORT = null; tlReset();
  boot();
};

$('rotateToken').onclick = async () => {
  if (!confirm('确定更换配对 Token？所有已配对手机都要重填新 Token。')) return;
  try {
    const r = await api('/api/console/rotate-token', {method: 'POST'});
    TOKEN = r.token;
    localStorage.setItem('dh_token', TOKEN);
    $('token').value = TOKEN;
    $('newToken').textContent = r.token;
    $('newToken').classList.remove('hide');
    $('copyToken').classList.remove('hide');
    say('Token 已更换并保存。把新 Token 填到每台手机的「配对 Token」。', 'ok');
  } catch (e) { say(e.message, 'err'); }
};
$('copyToken').onclick = async () => {
  try {
    await navigator.clipboard.writeText(TOKEN);
    say('新 Token 已复制到剪贴板', 'ok');
  } catch (e) { say('复制失败，请手动选中复制', 'err'); }
};

$('addBot').onclick = addBot;

$('addTenant').onclick = async () => {
  const id = $('newTid').value.trim(), name = $('newTname').value.trim();
  if (!id) { say('先填空间 ID', 'err'); return; }
  try {
    const r = await api('/api/console/tenants', {method:'POST', body: JSON.stringify({id, name})});
    renderSpaces(r.tenants);
    fillTenantSel(r.tenants);
    TENANT = id; localStorage.setItem('dh_tenant', id); $('tenantSel').value = id;
    await boot();
    // 同「保存设置」：必须在 boot() 之后报结果，否则确认信息会被 say('') 清掉
    say('空间「' + (name || id) + '」已创建，成员手机填空间 ID 为 ' + id, 'ok');
  } catch (e) { say(e.message, 'err'); }
};

async function api(path, opts, tenantOverride) {
  const o = opts || {};
  const headers = Object.assign({'Authorization': 'Bearer ' + TOKEN,
                                 'X-Tenant': tenantOverride || TENANT}, o.headers || {});
  // 有 JSON 请求体就必须显式声明 Content-Type。缺这个头时 FastAPI 不把请求体
  // 当 JSON 解析，而是把**原始字符串**塞给 `body: dict` 参数，一律回 422
  // （Input should be a valid dictionary）——症状是「点保存毫无反应、页面只显示
  // Object」，控制台里连一条业务错误都没有。实测：工作台里所有带 JSON 体的写操作
  // （保存设置 / 创建空间 / 测试连通性 / 拉取模型列表）此前全部是坏的。
  // FormData 不能加：multipart 的 boundary 必须由浏览器自己生成，写死了上传会解析失败。
  if (typeof o.body === 'string') headers['Content-Type'] = 'application/json';
  const r = await fetch(path, Object.assign({}, o, {headers}));
  if (r.status === 401) { throw new Error('Token 不对：到 Mac 应用「概览 → 手机端配对」点复制按钮，重新粘贴并保存（手机端「配对 Token」用的是同一个）'); }
  if (!r.ok) {
    // detail 未必是字符串：FastAPI 的参数校验错误是**数组**，
    // 直接塞进 Error 会变成没头没尾的「[object Object]」，排障时等于没提示。
    let detail = '';
    try {
      const d = (await r.json()).detail;
      detail = typeof d === 'string' ? d : JSON.stringify(d);
    } catch (e) {}
    throw new Error(detail || (path + ' 返回 ' + r.status));
  }
  return r.json();
}
function say(t, cls) {
  const m = $('msg');
  m.textContent = t; m.className = 'msg ' + (cls || '');
  if (!t) m.classList.add('hide');
  else m.classList.remove('hide');
}
function kv(k, v) {
  return '<div class="kv"><span>' + k + '</span><span>' + v + '</span></div>';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function fmtBytes(n) {
  if (!n) return '0 B';
  if (n > 1048576) return (n/1048576).toFixed(1) + ' MB';
  if (n > 1024) return (n/1024).toFixed(0) + ' KB';
  return n + ' B';
}

function fillTenantSel(list) {
  const sel = $('tenantSel');
  sel.innerHTML = (list || []).map(t =>
    '<option value="' + esc(t.id) + '"' + (t.id === TENANT ? ' selected' : '') + '>' +
    esc(t.name) + '（' + esc(t.id) + '）</option>').join('');
}

async function boot() {
  if (!TOKEN) { say('先填配对 Token：到 Mac 应用「概览 → 手机端配对」点 Token 行的复制按钮，粘贴到这里再点保存', 'err'); return; }
  try {
    STATE = await api('/api/console/state');
    $('svc').innerHTML = '<span class="dot on"></span> <span id="svcText">服务在线</span>';
    say('');
    fillTenantSel(STATE.tenants);
    renderOverview(); renderIndustry(); renderSpeakers(); renderSpaces(STATE.tenants); fillDates();
  maybeLoadReport();      // 深链直接开 #report 时，这里才有日期可选
  } catch (e) {
    $('svc').innerHTML = '<span class="dot off"></span> <span id="svcText">连不上</span>';
    say(e.message, 'err');
  }
}

// ---------- 设备与管家（服务端权威设置） ----------
// 数据来自 /api/profiles。每台设备一行：空间下拉 + 管家下拉 + 本机管家开关，
// 改动立刻 PUT 回去（这里是运维台，不做草稿态——所见即生效，避免「改了没保存」）。
async function loadDevices() {
  const d = await api('/api/profiles');
  STATE.profiles = d;
  renderBots(d.bots || []);
  renderDevices(d);
}

function renderDevices(d) {
  const list = d.profiles || [];
  const tids = (STATE && STATE.tenants || []).map(t => t.id);
  if (!list.length) {
    $('devList').innerHTML =
      '<div class="tm">还没有设备登记。设备<b>第一次上传</b>时会自动出现在这里' +
      '（用客户端自报的空间），之后就可以在这改归属。</div>';
    return;
  }
  $('devList').innerHTML = list.map(p => {
    const opts = tids.map(t => '<option value="' + esc(t) + '"' +
      (t === p.tenant ? ' selected' : '') + '>' + esc(t) + '</option>').join('');
    const bopts = (d.bots || []).map(b => '<option value="' + esc(b.id) + '"' +
      (b.id === p.bot ? ' selected' : '') + '>' + esc(b.name) + '</option>').join('');
    return '<div class="tcard" data-key="' + esc(p.install_key) + '">' +
      '<div class="row" style="justify-content:space-between">' +
        '<span class="tn">' + esc(p.label || '（未命名设备）') + '</span>' +
        (p.hermes_effective ? '<span class="pill on">本机管家</span>'
                            : '<span class="pill warn">仅通用助手</span>') +
      '</div>' +
      '<div class="tm mono">' + esc(p.install_key) + '</div>' +
      '<div class="row" style="margin-top:var(--s2);flex-wrap:wrap;gap:var(--s2)">' +
        '<span class="tm">数据空间</span><select data-f="tenant">' + opts + '</select>' +
        '<span class="tm">管家</span><select data-f="bot">' + bopts + '</select>' +
      '</div>' +
      '<label class="row" style="margin-top:var(--s2);gap:var(--s2);font-size:var(--f3)">' +
        '<input type="checkbox" data-f="hermes"' + (p.hermes ? ' checked' : '') + '> 允许用本机 Hermes' +
      '</label>' +
      '<div class="tm">首次 ' + esc(p.first_seen || '-') + ' · 最近改动 ' + esc(p.updated_at || '-') + '</div>' +
      '<div class="row" style="margin-top:var(--s2);gap:var(--s2)">' +
        '<button data-act="del" class="danger" style="padding:0 var(--s3);min-height:28px;font-size:var(--f2)">删除档案</button>' +
        '<span class="tm">（只删档案，数据保留在原空间）</span>' +
      '</div>' +
    '</div>';
  }).join('');

  document.querySelectorAll('#devList select, #devList input[data-f=hermes]').forEach(el => {
    el.onchange = async () => {
      const key = el.closest('.tcard').dataset.key;
      const card = el.closest('.tcard');
      const body = {
        tenant: card.querySelector('select[data-f=tenant]').value,
        bot: card.querySelector('select[data-f=bot]').value,
        hermes: card.querySelector('input[data-f=hermes]').checked,
      };
      try {
        await api('/api/profiles/' + encodeURIComponent(key), {method:'PUT', body: JSON.stringify(body)});
        say('已保存：' + key.slice(0, 16) + ' → 空间 ' + body.tenant +
            ' / 管家 ' + body.bot + ' / 本机管家 ' + (body.hermes ? '开' : '关'), 'ok');
        await loadDevices();
      } catch (e) { say(e.message, 'err'); await loadDevices(); }
    };
  });
  document.querySelectorAll('#devList [data-act=del]').forEach(btn => {
    btn.onclick = async () => {
      const key = btn.closest('.tcard').dataset.key;
      if (!confirm('删除设备档案 ' + key + ' ？\\n（只移出列表，已上传的数据仍留在原空间里）')) return;
      try {
        await api('/api/profiles/' + encodeURIComponent(key), {method:'DELETE'});
        say('设备档案已删除：' + key.slice(0, 16), 'ok');
        await loadDevices();
      } catch (e) { say(e.message, 'err'); }
    };
  });
}

function renderBots(list) {
  $('botList').innerHTML = (list || []).map(b =>
    '<div class="tcard">' +
      '<div class="row" style="justify-content:space-between">' +
        '<span class="tn">' + esc(b.name) + '</span>' +
        (b.hermes ? '<span class="pill on">本机管家</span>'
                  : '<span class="pill warn">不做对话</span>') +
      '</div>' +
      '<div class="tm mono">' + esc(b.id) + (b.base_url ? ' → ' + esc(b.base_url) : '') + '</div>' +
      (b.persona ? '<div class="tm">' + esc(b.persona) + '</div>' : '') +
      (b.id === 'default' ? '<div class="tm">所有未指定角色的设备都走这个</div>' :
        '<button data-bdel="' + esc(b.id) + '" class="danger" style="margin-top:var(--s2);padding:0 var(--s3);min-height:28px;font-size:var(--f2)">删除角色</button>') +
    '</div>').join('');
  document.querySelectorAll('#botList [data-bdel]').forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.bdel;
      if (!confirm('删除管家角色 ' + id + ' ？\\n绑到它的设备会改回「默认管家」。')) return;
      try {
        await api('/api/bots/' + encodeURIComponent(id), {method:'DELETE'});
        say('管家角色已删除：' + id + '（相关设备已回落默认管家）', 'ok');
        await loadDevices();
      } catch (e) { say(e.message, 'err'); }
    };
  });
}

async function addBot() {
  const id = $('newBotId').value.trim();
  const name = $('newBotName').value.trim();
  if (!id) { say('先填角色 ID（小写字母开头）', 'err'); return; }
  try {
    await api('/api/bots/' + encodeURIComponent(id), {method:'PUT', body: JSON.stringify({name: name || id})});
    $('newBotId').value = ''; $('newBotName').value = '';
    say('管家角色已创建：' + id, 'ok');
    await loadDevices();
  } catch (e) { say(e.message, 'err'); }
}

function renderSpaces(list) {
  $('spaceList').innerHTML = (list || []).map(t =>
    '<div class="tcard' + (t.id === TENANT ? ' on' : '') + '" data-tid="' + esc(t.id) + '">' +
      '<div class="row" style="justify-content:space-between">' +
        '<span class="tn">' + esc(t.name) + '</span>' +
        (t.id === 'default' ? '<span class="pill info">默认</span>' :
          '<button class="danger" data-del="' + esc(t.id) + '" style="padding:0 var(--s3);min-height:28px;font-size:var(--f2)">注销</button>') +
      '</div>' +
      '<div class="tm mono">' + esc(t.id) + '</div>' +
      '<div class="tm">日报文本 ' + t.text_files + ' · 语料 ' + t.corpus_files +
        ' · 标记 ' + t.markers + ' · 库 ' + fmtBytes(t.db_bytes) + '</div>' +
    '</div>').join('');
  document.querySelectorAll('#spaceList .tcard').forEach(el => {
    el.onclick = (ev) => {
      if (ev.target.dataset.del) return;   // 注销按钮不触发切换
      TENANT = el.dataset.tid;
      localStorage.setItem('dh_tenant', TENANT);
      $('tenantSel').value = TENANT;
      boot();
    };
  });
  document.querySelectorAll('#spaceList [data-del]').forEach(btn => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const tid = btn.dataset.del;
      if (!confirm('注销空间 ' + tid + '？（数据目录保留，可重新注册恢复）')) return;
      try {
        const r = await api('/api/console/tenants/' + encodeURIComponent(tid), {method:'DELETE'});
        renderSpaces(r.tenants); fillTenantSel(r.tenants);
        if (TENANT === tid) { TENANT = 'default'; localStorage.setItem('dh_tenant', TENANT); }
        say('空间 ' + tid + ' 已注销（数据保留在 data/tenants/' + tid + '/）', 'ok');
      } catch (e) { say(e.message, 'err'); }
    };
  });
}

function renderOverview() {
  const s = STATE.service, m = STATE.models, i = STATE.industry;
  $('ovService').innerHTML =
    kv('版本', esc(s.version)) +
    kv('端口', esc(s.port)) +
    kv('局域网地址', '<span class="mono">' + esc((s.lan_ips||[]).join(', ')) + '</span>') +
    // 外网中转：与 Mac 原生概览的「外网中转」同一字段（state 里的 relay_hint，
    // 来自 env.local 的 DH_RELAY_HINT）。为空表示没配，就不显示这一行 ——
    // 原生也是「非空才出」。生产上是配了的，少了这行等于手机端填什么地址
    // 在这一页看不出来。
    (STATE.relay_hint
      ? kv('外网中转', '<span class="mono">' + esc(STATE.relay_hint) + '</span>')
      : '') +
    kv('租户空间', (STATE.tenants||[]).length + ' 个') +
    kv('鉴权', s.token_enabled ? '<span class="pill on">已启用</span>' : '<span class="pill warn">未启用</span>');

  $('ovModels').innerHTML =
    kv('文字模型', m.llm_available
        ? '<span class="pill on">' + esc(m.llm_model) + ' 可用</span>'
        : '<span class="pill warn">' + esc(m.llm_model) + ' 不可用</span>') +
    kv('增量 ASR', '<span class="mono">' + esc(m.asr_incremental) + '</span>') +
    kv('日终 ASR', '<span class="mono">' + esc(m.asr_final) + '</span>' +
        (m.asr_final_rewrite ? '（全段重转）' : '')) +
    kv('热词', m.hotwords.length
        ? '<span class="mono">' + esc(m.hotwords.join(' ')) + '</span>'
        : '<span style="color:var(--dim)">无</span>');

  $('ovIndustry').innerHTML =
    kv('当前', '<span class="pill on">' + esc(i.label) + '</span>') +
    kv('术语数', esc(i.terms)) +
    kv('错字映射', Object.keys(i.fixes || {}).length + ' 条') +
    (i.pinned_by_env ? kv('', '<span class="pill warn">被环境变量锁定</span>') : '');

  // 最新的日期排在最前 —— 服务端 store.report_dates() 是 ORDER BY date 升序，
  // 直接铺出来最早那天在最上面。口径必须与日报页日期下拉（fillDates）、
  // Mac 原生 OverviewView/ReportView 的 days.reversed() 一致，否则同一个事实
  // 在三处显示成三种顺序。
  const d = (STATE.days || []).slice().reverse();
  $('ovDays').innerHTML = d.length
    ? '<table><tr><th>日期</th><th>人声段</th><th>状态</th></tr>' + d.map(x =>
        '<tr><td class="mono">' + esc(x.date) + '</td><td>' + esc(x.speech_segments) +
        '</td><td>' + (x.ready ? '<span class="pill on">就绪</span>'
                               : '<span class="pill warn">未生成</span>') + '</td></tr>'
      ).join('') + '</table>'
    : '<span style="color:var(--dim)">该空间还没有任何日报</span>';

  renderStorage(STATE.storage);
}

// 存储占用：分项 + 各空间。原始音频会由日终/final 轮清掉，语料与备份是长期保留的，
// 所以把「备份」「日志」单列出来 —— 长期驻留时它们是唯一会无限长的两处。
function renderStorage(s) {
  if (!s) { $('ovStorage').innerHTML = '<span style="color:var(--dim)">读取中…</span>'; return; }
  const rows = (s.tenants || []).map(t => {
    const db = t.dir_bytes || {};
    return '<tr><td class="mono">' + esc(t.id) + '</td><td>' + esc(t.name) + '</td>' +
      '<td>' + fmtBytes(t.bytes) + '</td>' +
      '<td>' + fmtBytes(db.audio || 0) + '</td>' +
      '<td>' + fmtBytes(db.corpus || 0) + '</td>' +
      '<td>' + fmtBytes(t.db_bytes) + '</td></tr>';
  }).join('');
  // 用 [.] 而不是反斜杠转义的点：这段 JS 嵌在 Python **非 raw** 字符串里，
  // 反斜杠加点会被 Python 当无效转义并抛 SyntaxWarning（实测）。
  const oldest = (s.backups_oldest || '').replace(/^backup_(all_)?/, '').replace(/[.]zip$/, '');
  $('ovStorage').innerHTML =
    kv('数据目录', '<span class="mono">' + esc(s.data_dir) + '</span>') +
    // 标签订正（2026-09-17）：原先把 tenants_bytes 写成「语料库」，但它其实是
    // **各空间的全部占用**（原始音频 + 语料 + 数据库 + 文本 + 标记），不是 corpus
    // 一个目录。实测量级差一倍以上（644 MB vs 真实 corpus 277 MB），
    // 用户据此判断「语料库怎么这么大」时会得出完全错误的结论。
    kv('合计', '<b>' + fmtBytes(s.total_bytes) + '</b>（各空间合计 ' + fmtBytes(s.tenants_bytes) +
       ' · 备份 ' + fmtBytes(s.backups_bytes) + ' · 日志 ' + fmtBytes(s.logs_bytes) + '）') +
    kv('备份包', esc(s.backups_count) + ' 份' + (oldest ? '，最早 ' + esc(oldest) : '')) +
    '<table style="margin-top:var(--s2)"><tr><th>空间</th><th>显示名</th><th>合计</th>' +
    '<th>原始音频</th><th>语料</th><th>数据库</th></tr>' + rows + '</table>' +
    '<p style="color:var(--dim);font-size:var(--f2);margin:var(--s3) 0 0">' +
    '「合计」= 原始音频 + 语料 + 数据库 + 文本，四列见上表，别把它当成语料一个目录。' +
    '音频回收（1.0.3，每天一轮）：日终（final）跑完后删该日全部 wav；' +
    '未终稿的音频与语料 wavs 副本超过 DH_RECLAIM_AFTER_DAYS（默认 7）天自动删；' +
    '低信息密度（无实体信号且有效字很少）的段 3 天后优先删。' +
    '只删 wav，转写文本与语料 jsonl 永久保留，日报与检索不受影响。' +
    '语料（corpus）与备份包（backups）是长期保留的 —— 若备份越堆越多，' +
    '可在 Mac 上删掉 <span class="mono">data/backups/</span> 里不要的旧包。</p>';
}

function renderIndustry() {
  const i = STATE.industry;
  // 「编辑」按钮必须 stopPropagation：.ind 整行是「切换行业」的点击区，
  // 不拦一下，点编辑会顺手把行业切了（切行业要清 ASR 缓存并重建热词）。
  $('indList').innerHTML = (i.available || []).map(x =>
    '<div class="ind' + (x.key === i.current ? ' on' : '') + '" data-key="' + esc(x.key) + '">' +
      '<div>' + esc(x.label) + '</div>' +
      '<div style="display:flex;align-items:center;gap:var(--s3)">' +
        '<span style="color:var(--dim);font-size:var(--f2)">' + esc(x.terms) + ' 词' +
        (x.key === i.current ? ' · 当前' : '') + '</span>' +
        '<button data-indedit="' + esc(x.key) + '" ' +
        'style="padding:0 var(--s3);min-height:28px;font-size:var(--f2)">编辑</button>' +
      '</div>' +
    '</div>').join('');
  document.querySelectorAll('[data-indedit]').forEach(b => {
    b.onclick = ev => { ev.stopPropagation(); openIndEditor(b.dataset.indedit); };
  });
  document.querySelectorAll('.ind').forEach(el => {
    el.onclick = async () => {
      if (el.classList.contains('on')) return;
      const key = el.dataset.key;
      try {
        const r = await api('/api/console/industry/' + encodeURIComponent(key), {method:'PUT'});
        STATE.industry = Object.assign(STATE.industry, {
          current: r.industry, label: r.label, terms: r.terms, fixes: r.fixes
        });
        say('已切换到「' + r.label + '」，' + r.terms + ' 个术语', 'ok');
        renderIndustry(); renderOverview();
      } catch (e) { say(e.message, 'err'); }
    };
  });
  const fx = i.fixes || {};
  const dx = (STATE.dialect && STATE.dialect.fixes) || {};
  // i.fixes 是「方言 + 行业」合并后的结果（服务端合并，前端不重复算一遍）。
  // 这里按来源分开显示：合并着列出来，用户会以为方言条目是行业词表里写的，
  // 去行业文件里找一个不存在的词条。
  const fkeys = Object.keys(fx).filter(k => !(k in dx) || dx[k] !== fx[k]);
  const dkeys = Object.keys(dx);
  const fxTable = (ks, src) => ks.length
    ? '<table style="margin-top:var(--s2)"><tr><th>错写</th><th>正写</th></tr>' + ks.map(k =>
        '<tr><td class="mono">' + esc(k) + '</td><td class="mono">' + esc(k in dx && src === 'd' ? dx[k] : fx[k]) + '</td></tr>').join('') + '</table>'
    : '<div style="color:var(--dim);font-size:var(--f3);margin-top:var(--s2)">（无）</div>';
  $('indDetail').innerHTML =
    '<div class="kv"><span>行业</span><span>' + esc(i.label) + '（' + esc(i.current) + '）</span></div>' +
    '<div style="margin-top:var(--s2);color:var(--dim);font-size:var(--f2)">方言纠偏（不分行业、始终生效，' + dkeys.length + ' 条）</div>' +
    fxTable(dkeys, 'd') +
    '<div style="margin-top:var(--s2);color:var(--dim);font-size:var(--f2)">行业同音错字映射（确定性替换，' + fkeys.length + ' 条）</div>' +
    fxTable(fkeys, 'i') +
    '<div style="margin-top:var(--s3);color:var(--dim);font-size:var(--f2)">' +
    '行业词表文件：<span class="mono">industries/' + esc(i.current) + '.txt</span>；' +
    '方言表文件：<span class="mono">' + esc((STATE.dialect || {}).path || 'data/dialect.fixes') +
    '</span>（不存在就用内置默认）。' +
    '两者都只需加一行 <span class="mono">错写=正写</span> 即可生效。</div>';
}

// ---------- 词表编辑器（行业页） ----------
// 与 Mac 原生行业页的「编辑」弹窗同一个端点对（GET / PUT /api/console/industry/<key>/terms）。
// 服务端做校验（单行 > 80 字符、映射行两侧为空、超过 2000 行都直接 400），
// 前端不重复实现一遍规则 —— 那种「两处判据」迟早会不一致。
let INDKEY = '';

async function openIndEditor(key) {
  INDKEY = key;
  $('indDlgT').textContent = '编辑词表 · ' + key;
  $('indDlgText').value = '';
  $('indDlgMsg').textContent = '读取中…';
  $('indDlg').showModal();
  try {
    const r = await api('/api/console/industry/' + encodeURIComponent(key) + '/terms');
    $('indDlgText').value = r.text || '';
    $('indDlgMsg').textContent = r.text ? ''
      : '这个行业还没有词表文件；保存后会在数据目录里新建一份（不动内置那份）。';
  } catch (e) { $('indDlgMsg').textContent = '读取失败：' + e.message; }
}

$('indDlgCancel').onclick = () => $('indDlg').close();

$('indDlgSave').onclick = async () => {
  const key = INDKEY;
  $('indDlgSave').disabled = true;
  $('indDlgMsg').textContent = '保存中…';
  try {
    const r = await api('/api/console/industry/' + encodeURIComponent(key) + '/terms', {
      method: 'PUT', body: JSON.stringify({text: $('indDlgText').value})
    });
    $('indDlg').close();
    // 词表热生效后，state 里的 terms/fixes 也变了，重新拉一次才对得上。
    await boot();
    // 回包字段是 terms（生效后的术语条数），不是行数 —— 注释行与空行不计入。
    say('词表已保存并生效（' + esc(key) + '，' + r.terms + ' 个术语）', 'ok');
  } catch (e) {
    $('indDlgMsg').textContent = '保存失败：' + e.message;
  } finally { $('indDlgSave').disabled = false; }
};

function renderSpeakers() {
  const s = STATE.speakers || [];
  const t = v => {
    if (!v) return '—';
    const d = new Date(v * 1000);
    return isNaN(d) ? String(v).slice(0, 19)
      : d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' +
        String(d.getDate()).padStart(2,'0') + ' ' +
        String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
  };
  $('spList').innerHTML = s.length
    ? '<table><tr><th>名字</th><th>注册时间</th><th>操作</th></tr>' + s.map(x =>
        '<tr><td>' + esc(x.name) + '</td><td class="mono">' + esc(t(x.updated_at)) + '</td>' +
        '<td><button class="danger" data-spdel="' + esc(x.name) + '" ' +
        'style="padding:0 var(--s3);min-height:28px;font-size:var(--f2)">删除</button></td></tr>').join('') + '</table>'
    : '<span style="color:var(--dim)">该空间还没注册具名角色。用下面的上传，或在手机端「设置 → 声纹角色」按提示朗读 30 秒。</span>';
  // 删除声纹角色（DELETE /api/speakers/{name}）—— 与 Mac 原生角色页同一个端点。
  // 二次确认的文案与原生确认框一字对齐：删的是声纹锚点，历史日报不追溯改动，
  // 但之后这个人说的话会退回匿名聚类。
  document.querySelectorAll('[data-spdel]').forEach(b => {
    b.onclick = async () => {
      const n = b.dataset.spdel;
      if (!confirm('删除角色「' + n + '」？已生成的日报不会追溯改动，之后这个人说的话会回到匿名聚类。')) return;
      try {
        await api('/api/speakers/' + encodeURIComponent(n), {method:'DELETE'});
        say('已删除「' + n + '」', 'ok');
        await boot();
      } catch (e) { say(e.message, 'err'); }
    };
  });

  // 朗读文本由服务端下发（STATE.enroll），不在这里硬编码：
  // Mac 壳的角色页要显示同一段话，各写一份迟早对不上。
  // 用 textContent 而不是 innerHTML —— 这段是纯文本（含换行），
  // 交给 white-space:pre-wrap 排版即可，也让 esc() 的转义顾虑消失。
  const en = STATE.enroll || {};
  const box = $('spScript');
  box.textContent = en.script || '';
  box.style.display = en.script ? '' : 'none';
}

// ---------- 上传录音创建角色 ----------
// 走 /api/pair —— 与手机端「配对」同一个端点：multipart 一个 file，角色名走 query。
// api() 只对**字符串体**补 Content-Type: application/json；这里传的是 FormData，
// 因此不会被补，boundary 仍由浏览器生成（手写 Content-Type 会让服务端解析失败）。
$('spUpload').onclick = async () => {
  const f = ($('spFile').files || [])[0];
  if (!f) { say('先选一段 WAV 录音', 'err'); return; }
  if (!/\\.wav$/i.test(f.name)) { say('只支持 WAV 格式（手机录音或电脑导出均可）', 'err'); return; }
  const name = $('spName').value.trim();
  const fd = new FormData();
  fd.append('file', f, f.name);
  $('spProg').innerHTML = '<span style="color:var(--dim)">上传中，正在提取声纹…</span>';
  say(name ? ('正在注册「' + name + '」…') : '未填角色名，将注册为「本人」…');
  $('spUpload').disabled = true;
  try {
    const r = await api('/api/pair?name=' + encodeURIComponent(name), {method:'POST', body: fd});
    $('spProg').innerHTML = '<span style="color:var(--dim)">已把「' + esc(r.name) + '」注册到当前空间。</span>';
    $('spName').value = ''; $('spFile').value = '';
    await boot();
    say('注册成功：' + r.name, 'ok');
  } catch (e) {
    $('spProg').innerHTML = '';
    say(e.message, 'err');
  } finally {
    $('spUpload').disabled = false;
  }
};

// 复制朗读文本：与 Mac 原生角色页的「复制文本」等价 —— 用户要拿这段去
// 手机录音、或发给别人照读时，纯文本比截图好使。
$('spCopy').onclick = async () => {
  const txt = (STATE.enroll || {}).script || '';
  if (!txt) { say('朗读文本为空', 'err'); return; }
  try {
    await navigator.clipboard.writeText(txt);
    say('已复制朗读文本', 'ok');
  } catch (e) { say('复制失败：' + e.message, 'err'); }
};

function fillDates() {
  // 最新的日期排在最前 —— 服务端 report_dates 是 ORDER BY date 升序，
  // 直接铺出来最早那天会被默认选中（与原生 days.reversed() 不一致，也不合直觉）。
  const d = (STATE.days || []).slice().reverse();
  const sel = $('dateSel');
  sel.innerHTML = d.length
    ? d.map(x => '<option value="' + esc(x.date) + '">' + esc(x.date) +
        '（' + esc(x.speech_segments) + ' 段）</option>').join('')
    : '<option value="">无数据</option>';
}

$('loadReport').onclick = async () => {
  const d = $('dateSel').value;
  if (!d) return;
  try {
    REPORT = await api('/api/console/report/' + encodeURIComponent(d));
    showVer();
  } catch (e) { say(e.message, 'err'); }
};

// 重新生成 = POST /api/done/{date}?final=1 —— 与 Mac 原生「重新生成」同一个端点。
// final=1 是「全段重转 + 日终清理」那一支；final=0 只补增量转写、不重算 LLM，
// 所以这里必须显式带 final=1，否则点下去等于什么都没生成。
// 二次确认不能省：它会重跑当天全部人声段的 ASR，代价是分钟级。
$('regenReport').onclick = async () => {
  const d = $('dateSel').value;
  if (!d) { say('先选一个日期', 'err'); return; }
  if (!confirm('重新生成 ' + d + ' 的日报？会重跑当天全部人声段的转写并重建三版日报，可能耗时数分钟。')) return;
  try {
    await api('/api/done/' + encodeURIComponent(d) + '?final=1', {method:'POST'});
    say('已触发 ' + d + ' 的重新生成，进度看「日志」页签', 'ok');
  } catch (e) { say(e.message, 'err'); }
};

// 复制全文：与 Mac 原生日报页「复制全文」等价，复制的是**当前显示的版本**
// （原始稿 / 干净稿 / 总结），不是永远复制原始稿。
$('copyReport').onclick = async () => {
  const txt = REPORT ? (REPORT[VER] || '') : '';
  if (!txt) { say('还没有可复制的内容，先点「加载」', 'err'); return; }
  const nm = {raw:'原始稿', clean:'干净稿', summary:'总结'}[VER] || '';
  try {
    await navigator.clipboard.writeText(txt);
    say('已复制' + nm + '（' + txt.length + ' 字）', 'ok');
  } catch (e) { say('复制失败：' + e.message, 'err'); }
};

// ---------- 日报时间轴 ----------
// 与 Mac 端 Timeline.swift / 手机 APK ReportPage.kt 同款：解析规则、展示顺序
// （最新在上）、换段标注阈值（≥5 分钟）、本人高亮色三端一致 —— 保证同一个
// 日期三端看到的一样。解析不出时间行时回退 <pre> 纯文本（宁可不好看，绝不吞内容），
// 这与原生 reportContent 的分支一一对应。
const TL_SELF = '本人';
const TL_CARD_SPK = {'我的动态': TL_SELF, '他人动态': '他人', '今日回顾': ''};
const TL_LINE_RE = /^\\s*\\[(\\d{2}):(\\d{2})\\]\\s*(?:\\[([^\\]]*)\\]\\s*)?(.*)$/;
const TL_SUM_RE = /^【(\\d{2}):(\\d{2})-(\\d{2}:\\d{2})】\\s*(.*)$/;
const TL_GAP = 5;          // 与上一条（更晚的那条）间隔 ≥5 分钟才算换段
let TLDATA = null;         // {rows, rest}
let TLSPK = null;          // 说话人筛选，null = 全部
let TLFOCUS = null;        // 滑轨定位到的分钟数
let TLRAILW = -1;          // 上次画滑轨时的宽度（宽度没变就不重画）

function tlFmt(m) {
  return String((m / 60) | 0).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
}
function tlBounds(rows) {
  let lo = 0, hi = 1;
  if (rows.length) {
    lo = hi = rows[0].minute;
    for (const r of rows) {
      if (r.minute < lo) lo = r.minute;
      if (r.minute > hi) hi = r.minute;
    }
    if (hi <= lo) hi = lo + 1;
  }
  return [lo, hi];
}

/** 逐字稿 → 时间线行。续行沿用上一行时间戳；没有时间戳的旧稿返回空（调用方回退纯文本）。 */
function tlParseRows(text) {
  if (!text || !text.trim()) return [];
  const out = [];
  let last = -1;
  for (const line of text.split('\\n')) {
    const m = TL_LINE_RE.exec(line);
    if (m) {
      last = (+m[1]) * 60 + (+m[2]);
      const body = (m[4] || '').trim();
      if (body) out.push({minute: last, speaker: (m[3] || '').trim(), text: body});
    } else if (line.trim() && last >= 0) {
      out.push({minute: last, speaker: '', text: line.trim()});
    }
  }
  return out;
}

/** 总结的时间轴卡（我的动态/他人动态/今日回顾）→ 时间线行；没有这类卡返回空。 */
function tlParseSummary(cards) {
  const out = [];
  let matched = false;
  for (const card of (cards || [])) {
    if (!(card.title in TL_CARD_SPK)) continue;
    matched = true;
    const spk = TL_CARD_SPK[card.title];
    for (const raw of String(card.body || '').split('\\n')) {
      const m = TL_SUM_RE.exec(raw.trim());
      if (!m) continue;
      // 代码注入的时间头里可能残留 [说话人]，与原生一样去掉
      const txt = (m[4] || '').trim().replace(/^\\[[^\\]]*\\]\\s*/, '');
      if (txt) out.push({minute: (+m[1]) * 60 + (+m[2]), speaker: spk, text: txt});
    }
  }
  if (!matched) return [];
  out.sort((a, b) => b.minute - a.minute);   // 与原始稿/干净稿同序：最新在上
  return out;
}

/** 筛选选项：本人置顶，其余按发言数降序。 */
function tlSpeakers(rows) {
  const cnt = new Map();
  for (const r of rows) if (r.speaker) cnt.set(r.speaker, (cnt.get(r.speaker) || 0) + 1);
  return [...cnt.entries()]
    .sort((a, b) => (a[0] === TL_SELF) !== (b[0] === TL_SELF)
      ? (a[0] === TL_SELF ? -1 : 1) : b[1] - a[1])
    .map(e => ({label: e[0], count: e[1]}));
}

/** 展示序的行：先按说话人筛选，再统一「最新在上」。 */
function tlRows() {
  if (!TLDATA) return [];
  const rows = TLSPK ? TLDATA.rows.filter(r => r.speaker === TLSPK) : TLDATA.rows.slice();
  rows.sort((a, b) => b.minute - a.minute);
  return rows;
}

function tlCardHtml(c) {
  return '<div class="sumcard"><b>' + esc(c.title) + '</b><p>' + esc(c.body) + '</p></div>';
}

function tlThemeVar(n) {
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
}

/** 全天滑轨：每根刻度一个语音段，本人 Teal、其余 Indigo 55%。 */
function tlDrawRail(force) {
  const cv = $('repRail');
  if (!cv) return;
  const w = Math.round(cv.clientWidth);
  if (!w) return;                       // 页签还藏着（display:none）时宽度为 0
  if (!force && w === TLRAILW) return;
  TLRAILW = w;

  const h = 44, dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const rows = tlRows();
  const [lo, hi] = tlBounds(rows);
  const X = m => (m - lo) / (hi - lo) * w;
  const railY = h - 10;

  // 基线
  g.strokeStyle = tlThemeVar('--line');
  g.lineWidth = 2;
  g.beginPath(); g.moveTo(0, railY); g.lineTo(w, railY); g.stroke();

  // 刻度：先画他人（半透明），本人叠在最上面，密度大的时段本人才不会被盖掉
  g.lineWidth = 2;
  g.strokeStyle = tlThemeVar('--indigo');
  g.globalAlpha = 0.55;
  for (const r of rows) {
    if (r.speaker === TL_SELF) continue;
    const x = X(r.minute);
    g.beginPath(); g.moveTo(x, 6); g.lineTo(x, railY); g.stroke();
  }
  g.strokeStyle = tlThemeVar('--teal');
  g.globalAlpha = 1;
  for (const r of rows) {
    if (r.speaker !== TL_SELF) continue;
    const x = X(r.minute);
    g.beginPath(); g.moveTo(x, 6); g.lineTo(x, railY); g.stroke();
  }

  // 当前位置指示线 + 节点（未定位过则停在最早一刻）
  const fx = X(TLFOCUS == null ? lo : TLFOCUS);
  g.strokeStyle = tlThemeVar('--amber');
  g.beginPath(); g.moveTo(fx, 0); g.lineTo(fx, h); g.stroke();
  g.fillStyle = tlThemeVar('--amber');
  g.beginPath(); g.arc(fx, railY, 4, 0, Math.PI * 2); g.fill();

  if (TLFOCUS == null) $('repRailAt').textContent = tlFmt(lo);
}

/** 松手吸附到最近的有内容刻度（空白区段不可停留，与原生 snap 同款）。 */
function tlSnap(minute) {
  let best = minute, dist = Infinity;
  for (const r of tlRows()) {
    const d = Math.abs(r.minute - minute);
    if (d < dist) { dist = d; best = r.minute; }
  }
  return best;
}

/** 拖动/点击滑轨 → 定位并滚到第一条不晚于目标时刻的行。 */
function tlSeek(clientX) {
  const cv = $('repRail');
  if (!cv) return;
  const rows = tlRows();
  if (!rows.length) return;
  const rc = cv.getBoundingClientRect();
  const [lo, hi] = tlBounds(rows);
  const ratio = Math.min(Math.max((clientX - rc.left) / (rc.width || 1), 0), 1);
  const m = tlSnap(Math.round(lo + ratio * (hi - lo)));
  TLFOCUS = m;
  $('repRailAt').textContent = tlFmt(m);
  tlDrawRail(true);
  tlScrollTo(m);
}

function tlScrollTo(target) {
  const box = $('repList');
  if (!box) return;
  for (const el of box.querySelectorAll('.tlr')) {
    if (+el.dataset.m <= target) { box.scrollTop = el.offsetTop; return; }
  }
}

/** 按当前版本重画整块：能解析出时间行就上时间轴，否则逐级回退，内容不丢。 */
function tlRender() {
  if (!REPORT) return;
  const text = REPORT[VER] || '';
  const cards = REPORT.summary_cards || [];
  const isSum = VER === 'summary';
  const rows = isSum ? tlParseSummary(cards) : tlParseRows(text);
  const rest = isSum ? cards.filter(c => !(c.title in TL_CARD_SPK)) : [];

  TLDATA = rows.length ? {rows: rows, rest: rest} : null;
  TLSPK = null;
  TLFOCUS = null;

  if (!rows.length && rest.length) {
    // 总结没有时间轴卡、但其它栏目有内容 → 只上卡片（内容全在卡里）
    $('repChips').classList.add('hide');
    $('repRailBox').classList.add('hide');
    $('repBody').classList.add('hide');
    $('repList').classList.remove('hide');
    $('repList').innerHTML = '<div class="grid2">' + rest.map(tlCardHtml).join('') + '</div>';
    return;
  }
  if (!rows.length) {
    $('repChips').classList.add('hide');
    $('repRailBox').classList.add('hide');
    $('repList').classList.add('hide');
    $('repBody').classList.remove('hide');
    $('repBody').textContent = text || '（空）';
    return;
  }
  $('repBody').classList.add('hide');
  $('repChips').classList.remove('hide');
  $('repRailBox').classList.remove('hide');
  $('repList').classList.remove('hide');
  tlPaint();
}

function tlPaint() {
  if (!TLDATA) return;
  const rows = tlRows();

  // 筛选胶囊：全部 + 各说话人（本人置顶）
  $('repChips').innerHTML =
    '<span class="chip' + (TLSPK == null ? ' on' : '') + '" data-spk="">全部 ' +
      TLDATA.rows.length + '</span>' +
    tlSpeakers(TLDATA.rows).map(o =>
      '<span class="chip' + (TLSPK === o.label ? ' on' : '') + '" data-spk="' +
        esc(o.label) + '"' + (o.label === TL_SELF ? ' data-self="1"' : '') + '>' +
        esc(o.label) + ' ' + o.count + '</span>').join('');
  // 没有任何具名说话人时收起胶囊（例如总结里只有一张「今日回顾」卡、说话人为空）：
  // 留一颗孤零零的「全部 1」没有意义 —— 原生 SpeakerChipsRow 也是 options 为空就不画。
  $('repChips').classList.toggle('hide', tlSpeakers(TLDATA.rows).length === 0);

  // 正文：最新在最上；只有换段（与上一条更晚的间隔 ≥5 分钟）才标时间
  let html = '';
  if (!rows.length) {
    html = '<div class="tlempty">该筛选下暂无内容，点上方「全部」恢复。</div>';
  } else {
    let prev = null;
    for (const r of rows) {
      const showTime = prev == null || prev - r.minute >= TL_GAP;
      prev = r.minute;
      const self = r.speaker === TL_SELF;
      html += '<div class="tlr" data-m="' + r.minute + '">' +
        '<div class="tlt' + (self ? ' self' : '') + '">' +
          (showTime ? tlFmt(r.minute) : '') + '</div>' +
        '<div class="tln' + (self ? ' self' : '') + (showTime ? ' big' : '') +
          '"><i></i></div>' +
        '<div class="tlbx">' +
          (r.speaker ? '<div class="tls' + (self ? ' self' : '') + '">' +
            esc(r.speaker) + '</div>' : '') +
          '<div class="tlb">' + esc(r.text) + '</div>' +
        '</div></div>';
    }
  }
  if (TLDATA.rest.length) {
    html += '<div class="tlf"><h3>更多栏目</h3><div class="grid2">' +
      TLDATA.rest.map(tlCardHtml).join('') + '</div></div>';
  }
  const box = $('repList');
  box.innerHTML = html;
  box.scrollTop = 0;

  tlDrawRail(true);
}

function tlReset() {
  TLDATA = null; TLSPK = null; TLFOCUS = null; TLRAILW = -1;
  $('repChips').innerHTML = '';
  $('repList').innerHTML = '';
  $('repChips').classList.add('hide');
  $('repRailBox').classList.add('hide');
  $('repList').classList.add('hide');
  $('repBody').classList.remove('hide');
  $('repBody').textContent = '选个日期，点「加载」。';
}

// 点胶囊筛选；再点一次同一个 = 取消（与原生 chip 的 toggle 同款）
$('repChips').onclick = e => {
  const c = e.target.closest('.chip');
  if (!c) return;
  const v = c.dataset.spk;
  TLSPK = (v === '' || v === TLSPK) ? null : v;
  TLFOCUS = null;
  tlPaint();
};

// 滑轨：按下即定位，按住拖动连续定位（空白处吸附到最近的有内容刻度）
(function () {
  const cv = $('repRail');
  const end = e => {
    if (cv.hasPointerCapture && cv.hasPointerCapture(e.pointerId)) {
      cv.releasePointerCapture(e.pointerId);
    }
    $('repRailAt').classList.remove('drag');
  };
  cv.addEventListener('pointerdown', e => {
    // setPointerCapture 对未知 pointerId 会抛 NotFoundError，抛出去整条拖动链就断了
    try { cv.setPointerCapture(e.pointerId); } catch (err) {}
    $('repRailAt').classList.add('drag');
    tlSeek(e.clientX);
  });
  cv.addEventListener('pointermove', e => {
    if (cv.hasPointerCapture && cv.hasPointerCapture(e.pointerId)) tlSeek(e.clientX);
  });
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
})();

// 窗口宽度变了刻度要跟着重排；换主题（data-theme）了颜色要跟着换
let tlResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(tlResizeTimer);
  tlResizeTimer = setTimeout(() => { if (TLDATA) tlDrawRail(true); }, 120);
});
new MutationObserver(() => { if (TLDATA) tlDrawRail(true); })
  .observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});

/* 进日报页就自动加载。
   为什么必须有：日期下拉已按「最新在前」排好、默认就是最新一天，用户切到日报页
   却只看到「选个日期，点加载」的空态 —— 观感上就是「时间轴没了」。
   三个条件都满足才拉（当前页签是日报 / 选了日期 / 这一天还没加载过），
   所以反复切页签不会重复请求。 */
function maybeLoadReport() {
  const onReport = !$('tab-report').classList.contains('hide');
  const d = $('dateSel') && $('dateSel').value;
  if (!onReport || !d || (REPORT && REPORT.date === d)) return;
  $('loadReport').click();
}

function showVer() {
  if (!REPORT) return;
  const d = REPORT;
  $('repMeta').textContent = d.ready
    ? '原始 ' + (d.raw || '').length + ' 字 · 干净 ' + (d.clean || '').length +
      ' 字 · 总结 ' + (d.summary || '').length + ' 字'
    : '这一天还没有日报';
  tlRender();
}
document.querySelectorAll('[data-ver]').forEach(b => {
  b.onclick = () => {
    VER = b.dataset.ver;
    document.querySelectorAll('[data-ver]').forEach(x => x.classList.toggle('on', x === b));
    showVer();
  };
});

// ---------- 设置页 ----------
// schema 由服务端给出（config.CONFIG_SCHEMA），前端只负责画 —— 加一个可配项
// 只改服务端一处，Mac 应用与这里同时出现，不用各写一遍表单。
let CFG = null;

function ctlId(n) { return 'f_' + n; }
function ctlEl(n) { return $(ctlId(n)); }

// 密钥字段的当前值**永远不回到浏览器**（config.config_values 默认掩码），
// 所以它只显示「已设置 / 未设置」，改就填新的、留空就是不改。
function fieldHtml(f) {
  const v = CFG.values[f.name];
  const tag = f.hot ? '<span class="pill info">即时生效</span>'
            : (f.restart ? '<span class="pill warn">需重启</span>' : '');
  let ctl;
  if (f.type === 'bool') {
    ctl = '<input type="checkbox" id="' + ctlId(f.name) + '"' + (v ? ' checked' : '') + '>';
  } else if (f.type === 'choice' || f.type === 'select') {
    ctl = '<select id="' + ctlId(f.name) + '">' + (f.options || []).map(o =>
      '<option value="' + esc(o.value) + '"' +
      (String(o.value) === String(v) ? ' selected' : '') + '>' + esc(o.label) +
      '</option>').join('') + '</select>';
  } else if (f.type === 'textarea') {
    ctl = '<textarea id="' + ctlId(f.name) + '" rows="3" spellcheck="false">' + esc(v) + '</textarea>';
  } else if (f.type === 'secret') {
    ctl = '<input type="password" id="' + ctlId(f.name) + '" spellcheck="false" placeholder="' +
      (CFG.secrets_set[f.name] ? '已设置（留空即不修改）' : '未设置') + '">';
  } else {
    const num = (f.type === 'int' || f.type === 'number');
    ctl = '<input type="' + (num ? 'number' : 'text') + '" id="' + ctlId(f.name) +
      '" value="' + esc(v) + '" spellcheck="false"' +
      (f.min != null ? ' min="' + f.min + '"' : '') +
      (f.max != null ? ' max="' + f.max + '"' : '') +
      (f.step != null ? ' step="' + f.step + '"' : '') + '>';
  }
  // local_only / cloud_only：选了「本地模型」就不该看到云端代理这一类项
  const scope = f.local_only ? 'local' : (f.cloud_only ? 'cloud' : '');
  return '<div class="fld" data-scope="' + scope + '"><div class="fl">' + esc(f.label) +
    tag + '</div>' + (f.help ? '<div class="fh">' + esc(f.help) + '</div>' : '') + ctl + '</div>';
}

function renderSettings(cfg) {
  CFG = cfg;
  const L = cfg.llm || {};
  $('setCurrent').innerHTML =
    kv('运行位置', L.is_cloud ? '云端 API' : '本地模型') +
    kv('接口地址', '<span class="mono">' + esc(L.base_url) + '</span>') +
    kv('协议', esc(L.protocol)) +
    kv('模型', '<span class="mono">' + esc(L.model_id) + '</span>') +
    kv('超时', esc(L.timeout) + ' 秒') +
    kv('API Key', L.has_key ? '已设置' : '未设置');
  $('setPresets').innerHTML = (cfg.presets || []).map((p, i) =>
    '<button data-preset="' + i + '" title="' + esc(p.base_url) + '">' +
    esc(p.vendor) + (p.region === 'intl' ? ' · 境外' : '') + '</button>').join('');
  $('setGroups').innerHTML = (cfg.schema || []).map(g =>
    '<div class="card"><h2>' + esc(g.label) + '</h2><div class="gridset">' +
    (g.fields || []).map(fieldHtml).join('') + '</div></div>').join('');
  $('setEnvMeta').innerHTML =
    kv('env.local', '<span class="mono">' + esc(cfg.env_file) + '</span>') +
    kv('当前空间', esc(TENANT)) +
    kv('行业词表', esc(cfg.industry) + ((cfg.industries || []).length ? '（共 ' +
       cfg.industries.length + ' 套可选）' : ''));
  $('setGroups').querySelectorAll('input,select,textarea').forEach(el => {
    el.addEventListener('input', markDirty);
    el.addEventListener('change', () => { markDirty(); scopeFields(); });
  });
  $('setPresets').querySelectorAll('button[data-preset]').forEach(b => {
    b.onclick = () => applyPreset(Number(b.dataset.preset));
  });
  scopeFields();
}

function markDirty() { $('setDirty').classList.remove('hide'); }

function scopeFields() {
  const p = ctlEl('DH_LLM_PROVIDER');
  const cloud = !!p && p.value === 'cloud';
  $('setGroups').querySelectorAll('[data-scope]').forEach(el => {
    const s = el.dataset.scope;
    el.classList.toggle('off', (s === 'local' && cloud) || (s === 'cloud' && !cloud));
  });
}

function applyPreset(i) {
  const p = (CFG.presets || [])[i];
  if (!p) return;
  const set = (n, v) => { const el = ctlEl(n); if (el) el.value = v; };
  set('DH_LLM_PROVIDER', 'cloud');
  set('DH_LLM_PROTOCOL', p.protocol);
  set('DH_LLM_BASE_URL', p.base_url);
  set('DH_LLM_MODEL', p.model);
  scopeFields();
  markDirty();
  say('已填入「' + p.vendor + '」的接口地址与模型名，接着填 API Key 再点「保存设置」。' +
      'Key 申请地址：' + p.key_url, 'ok');
}

// 只回传**改动过的**项。服务端约定 null / 缺省 = 未修改，密钥尤其依赖这一点：
// 把掩码后的空串当成新值回传，会把用户已保存的 Key 清掉。
function origStr(f) {
  const v = CFG.values[f.name];
  if (f.type === 'bool') return v ? '1' : '0';
  return String(v == null ? '' : v);
}

function cfgCollect() {
  const out = {};
  (CFG.schema || []).forEach(g => (g.fields || []).forEach(f => {
    const el = ctlEl(f.name);
    if (!el) return;
    if (f.type === 'bool') {
      const now = el.checked ? '1' : '0';
      if (now !== origStr(f)) out[f.name] = now;
      return;
    }
    if (f.type === 'secret') { if (el.value) out[f.name] = el.value; return; }
    if (el.value !== origStr(f)) out[f.name] = el.value;
  }));
  return out;
}

function formVal(n) { const el = ctlEl(n); return el ? el.value : ''; }

function showOut(text, ok) {
  const el = $('setOut');
  el.textContent = text;
  el.className = 'pill ' + (ok ? 'on' : 'warn');
}

async function loadSettings() {
  if (!TOKEN) return;   // 还没填 Token 时 boot() 已经在提示了，这里再报一次只会盖掉那条提示
  try {
    renderSettings(await api('/api/console/config'));
    $('setDirty').classList.add('hide');
    loadBackups();
  } catch (e) { say(e.message, 'err'); }
}

$('setTest').onclick = async () => {
  const b = $('setTest'), old = b.textContent;
  b.disabled = true; b.textContent = '测试中…'; showOut('正在请求…', true);
  try {
    const r = await api('/api/console/llm/test', {method: 'POST', body: JSON.stringify({
      provider: formVal('DH_LLM_PROVIDER'), protocol: formVal('DH_LLM_PROTOCOL'),
      base_url: formVal('DH_LLM_BASE_URL'), model: formVal('DH_LLM_MODEL'),
      api_key: formVal('DH_LLM_API_KEY') || null,
      proxy: formVal('DH_LLM_PROXY') || null,
      served_id: formVal('DH_LLM_SERVED_ID') || null
    })});
    showOut(r.message, r.ok);
  } catch (e) { showOut(e.message, false); }
  b.disabled = false; b.textContent = old;
};

$('setModels').onclick = async () => {
  const b = $('setModels'), old = b.textContent;
  b.disabled = true; b.textContent = '拉取中…'; showOut('正在请求…', true);
  try {
    const r = await api('/api/console/llm/models', {method: 'POST', body: JSON.stringify({
      base_url: formVal('DH_LLM_BASE_URL'), protocol: formVal('DH_LLM_PROTOCOL'),
      api_key: formVal('DH_LLM_API_KEY') || null,
      proxy: formVal('DH_LLM_PROXY') || null
    })});
    if (r.ok) {
      $('modelList').innerHTML = r.models.map(m =>
        '<option value="' + esc(m) + '"></option>').join('');
      const inp = ctlEl('DH_LLM_MODEL');
      if (inp) inp.setAttribute('list', 'modelList');
    }
    showOut(r.message, r.ok);
  } catch (e) { showOut(e.message, false); }
  b.disabled = false; b.textContent = old;
};

$('setSave').onclick = async () => {
  const values = cfgCollect();
  const keys = Object.keys(values);
  if (!keys.length) { say('没有需要保存的改动。', 'ok'); $('setDirty').classList.add('hide'); return; }
  try {
    const r = await api('/api/console/config', {method: 'PUT', body: JSON.stringify({values})});
    renderSettings(r.config);
    $('setDirty').classList.add('hide');
    const need = r.restart_required || [];
    // 必须先 await boot() 再报结果：boot() 成功时会 say('') 清掉消息条，
    // 顺序反了用户点完「保存」什么都看不到（实测踩到过）。
    await boot();
    say('已保存 ' + (r.written || []).length + ' 项到 env.local。' +
        (need.length ? '这些项要重启服务才生效：' + need.join('、') : '都是即时生效的项，已经生效。'), 'ok');
  } catch (e) { say(e.message, 'err'); }
};

async function loadBackups() {
  try {
    const r = await api('/api/backup/list');
    const list = r.backups || [];
    $('setBackups').innerHTML = list.length
      ? list.slice(0, 8).map(x => kv('<span class="mono">' + esc(x.file) + '</span>',
          esc(x.mtime) + ' · ' + fmtBytes(x.size_bytes))).join('')
      : '<div style="color:var(--faint);font-size:var(--f2)">还没有备份包。点上面「立即备份」生成一份。</div>';
  } catch (e) {
    $('setBackups').innerHTML = '<div style="color:var(--red);font-size:var(--f2)">' +
      esc(e.message) + '</div>';
  }
}

async function doBackup(scope) {
  const b = scope ? $('setBackupAll') : $('setBackup');
  b.disabled = true;
  say(scope === 'all' ? '正在打包全部空间，语料多时需要一会儿…' : '正在打包当前空间…', '');
  try {
    // scope=all 必须走 X-Tenant：服务端取 scope 时 header 优先于 query，
    // 只带 ?tenant=all 会被当前空间覆盖掉，用户以为备份了全部、其实只备了一个。
    const r = await api('/api/backup' + (scope ? '?tenant=' + encodeURIComponent(scope) : ''),
                        {method: 'POST'}, scope || null);
    say('已备份：' + r.file + '（' + fmtBytes(r.size_bytes) + '），在数据目录 backups/ 里。', 'ok');
    loadBackups();
  } catch (e) { say(e.message, 'err'); }
  b.disabled = false;
}

// ---------- 日志页 ----------
let LOG_TIMER = null;

async function loadLogs() {
  if (!TOKEN) return;
  const name = $('logFile').value || 'api.log';
  try {
    const r = await api('/api/console/logs?name=' + encodeURIComponent(name));
    $('logBody').textContent = r.text || '（这个日志文件还是空的）';
    $('logDir').textContent = r.dir || '—';
    const cur = (r.files || []).find(f => f.name === r.name);
    $('logMeta').textContent = fmtBytes(r.size) + (r.truncated ? ' · 只显示末尾' : ' · 全文') +
      (cur ? ' · 更新于 ' + new Date(cur.mtime * 1000).toLocaleString('zh-CN') : '');
    const sel = $('logFile');
    const keep = sel.value || name;
    sel.innerHTML = (r.files || []).length
      ? r.files.map(f => '<option value="' + esc(f.name) + '">' + esc(f.name) +
          '（' + fmtBytes(f.size) + '）</option>').join('')
      : '<option value="api.log">api.log</option>';
    if ([].slice.call(sel.options).some(o => o.value === keep)) sel.value = keep;
  } catch (e) { say(e.message, 'err'); }
}

$('logReload').onclick = loadLogs;
$('logFile').onchange = loadLogs;
$('logCopy').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('logBody').textContent);
    say('日志已复制到剪贴板', 'ok');
  } catch (e) { say('复制失败，请手动选中复制', 'err'); }
};
$('logAuto').onchange = () => {
  // 定时器必须显式清掉：只置 null 不停表，页面切走后仍在每 3 秒发一次请求
  if (LOG_TIMER) { clearInterval(LOG_TIMER); LOG_TIMER = null; }
  if ($('logAuto').checked) LOG_TIMER = setInterval(loadLogs, 3000);
};

// 切页签的唯一入口。onclick 与「地址栏 #页签」都走它，避免两处各写一遍。
function selectTab(name) {
  const btn = [].slice.call(document.querySelectorAll('nav button'))
    .find(b => b.dataset.tab === name);
  if (!btn) return false;      // 认不出来的 hash 就停在当前页，不抛错
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('on', x === btn));
  // 页面清单直接从 DOM 取，不再维护一份硬编码数组 —— 加了页签忘记同步，
  // 症状是「点了新页签什么都不显示」，而控制台里一点报错都没有。
  document.querySelectorAll('main > section').forEach(s => {
    s.classList.toggle('hide', s.id !== 'tab-' + name);
  });
  // 设备档案是「当前状态」而不是历史快照，每次切进来都重拉一遍，
  // 免得两台机器同时改的时候看着旧列表下判断。设置与日志同理。
  if (name === 'devices') loadDevices().catch(e => say(e.message, 'err'));
  if (name === 'settings') loadSettings();
  if (name === 'logs') loadLogs();
  if (name === 'report') maybeLoadReport();
  return true;
}

document.querySelectorAll('nav button').forEach(b => {
  b.onclick = () => {
    selectTab(b.dataset.tab);
    // 地址栏带上 #页签：既方便反复进同一页，也让「打开工作台」能直达某一页；
    // 刷新后停在原页而不是弹回概览。replaceState 不往浏览历史里塞垃圾。
    history.replaceState(null, '', '#' + b.dataset.tab);
  };
});

// 打开时按 hash 定位页签（#settings / #logs / #report …）。
if (location.hash.length > 1) selectTab(location.hash.slice(1));

// 同一页内改 hash 属于「没重新加载」的导航，只会触发 hashchange ——
// 只在加载时读一次 hash 是不够的：地址栏把 #overview 改成 #settings、
// 或壳里用 location.hash 切页，都会静默停在原页（实测踩到）。
window.addEventListener('hashchange', () => {
  const n = location.hash.slice(1);
  if (n) selectTab(n);
});

boot();
</script>
</body>
</html>
"""

# 顶栏 logo 的 data URI 由 tools/make-app-icons.py 从图标母版生成（server/app/_logo.py），
# 与 mac / Windows 两端 app 图标、托盘图标同源。这里做一次性替换而不是 f-string：
# 页面里全是 CSS 的花括号，套 f-string 等于每天在雷区上走路。
_PAGE = _PAGE.replace("__LOGO_DATA_URI__", _logo.DATA_URI)
