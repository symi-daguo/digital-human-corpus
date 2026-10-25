"""FastAPI 路由：与 Android 端 MacClient 协议严格对齐。

- GET  /api/health           健康检查（App 探测 ping 用）
- PUT  /api/upload/{segId}   上传一个 WAV 段（幂等，头携带元数据）
- POST /api/done/{date}      通知当日上传完毕 → 触发处理管线
- GET  /api/report/{date}    拉取日报（ready=false 表示未就绪，App 退避重试）
- POST /api/pair             配对：注册本人声纹（multipart 上传 ≥30s 录音）
- POST /api/backup           触发数据备份：归档 text/ + corpus/ + DB 到 backups/
- GET  /api/backup/list      列出已有备份包（导出/恢复用）
- GET  /api/backup/download/{name}  下载一份备份包（导出）
- POST /api/backup/restore   从备份包恢复（破坏性，需 confirm=1，先自动留快照）
- GET  /api/tts/preview      合成一段「今天数据」的播报语音（设置页 TTS 测试按钮）
- POST /api/tts/synthesize    任意文本 → 语音（返回 audio/wav）
- POST /api/push/workbench    把一段物料描述推进「智能家居 AI 工作台」做结构化抽取

日期参数一律经 config.is_valid_date() 校验后才允许拼进路径或 SQL。
"""
from __future__ import annotations

import logging
import re
import secrets
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from . import (asr, config, ha_client, llm, pipeline, profiles, store, tenants, tts,
               voiceprint, workbench_push)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# 合理时间戳区间：低于下限通常是客户端传了 0（会算出 1970-01-01 分区目录）
_MIN_START_MS = int(datetime(2000, 1, 1).timestamp() * 1000)
_MAX_START_MS = int(datetime(2100, 1, 1).timestamp() * 1000)


def _check_auth(authorization: str | None) -> None:
    """恒定时间比较，避免通过响应耗时逐字节爆破 token。"""
    if not config.TOKEN:
        return
    expected = f"Bearer {config.TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="bad token")


def _resolve_tenant(tenant: str, x_tenant: str | None, install: str = "") -> str:
    """解析并校验请求的租户。

    优先级：header X-Tenant（手机端用，ASCII 安全）> query ?tenant=。
    不带 → 默认租户 default（旧版 App 完全兼容）。
    OWASP 多租户基线：客户端自报的租户 ID 必须在服务端注册表校验，
    未注册一律 403——绝不静默落到 default，否则拼错 ID 的数据会混进别人的空间。

    `install` 非空时，**以设备档案为权威**（2026-09-15）：设备已被管理员
    绑定到某个租户，就以档案为准，客户端自报的租户只当首次登记的建议值。
    这样一台设备即使 header 写错（或被人为改过）也不会把数据写进别人空间。
    """
    tid_raw = (x_tenant or "").strip() or (tenant or "").strip()
    try:
        tid = tenants.resolve(tid_raw).id
    except KeyError:
        raise HTTPException(status_code=403, detail=f"unknown tenant: {tid_raw[:24]}")
    if not install:
        return tid
    p = profiles.get_profile(install)
    if p is None:
        # 未登记：以客户端自报为准（安全的那次），档案由上传路径 ensure_profile 建立
        return tid
    if p.tenant != tid:
        log.warning(
            "设备 %s 自报租户 %s 与档案 %s 不一致，按档案路由（客户端设置可能过期）",
            install[:16], tid, p.tenant,
        )
    return p.tenant


def _require_date(date: str) -> str:
    if not config.is_valid_date(date):
        raise HTTPException(status_code=400, detail="bad date, expect yyyy-MM-dd")
    return date


def _int_header(name: str, raw: str, lo: int, hi: int) -> int:
    """解析并校验数值型请求头，非法值统一返回 400 而不是 500。"""
    try:
        v = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad header {name}: not an integer")
    if not lo <= v <= hi:
        raise HTTPException(status_code=400, detail=f"bad header {name}: out of range")
    return v


@router.get("/tenants")
def list_tenants(authorization: str | None = Header(default=None)) -> dict:
    """租户列表（id + 显示名 + 概览）。手机端登录页/工作台租户切换器用。"""
    _check_auth(authorization)
    return {"tenants": [tenants.status_of(t) for t in tenants.all_tenants()]}


@router.get("/health")
def health(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 设备档案与管家角色（2026-09-15）
#
# 服务端权威设置：每台采集端设备属于哪个租户、用哪个管家角色、
# 允不允许调用本机 Hermes。设备未登记时上传会按客户端自报租户自动登记，
# 这里改过之后就以这里的设置为准（上传请求不会再把它改回去）。
# ---------------------------------------------------------------------------


@router.get("/profiles")
def list_profiles(authorization: str | None = Header(default=None)) -> dict:
    """设备档案 + 可选管家角色 + 上限。工作台「设备与管家」卡片用。"""
    _check_auth(authorization)
    return profiles.summary()


@router.get("/profiles/self")
def profile_self(authorization: str | None = Header(default=None),
                 x_tenant: str = Header(default=""),
                 x_install_id: str = Header(default="")) -> dict:
    """客户端自查：我这台设备在服务端被绑定到了哪个空间 / 哪个管家。

    存在的意义是「让设备看到真相」：管理员在服务端改了归属之后，手机上原来的
    空间设置就过期了，与其让用户对着「设置显示 A、数据却落在 B」猜，不如直接把
    服务端的判定回给客户端显示。未登记的设备返回 `registered=False`。
    """
    _check_auth(authorization)
    install = urllib.parse.unquote((x_install_id or "").strip())[:64]
    tid = _resolve_tenant("", x_tenant, install)
    p = profiles.get_profile(install) if install else None
    bot = profiles.resolve_bot(p.bot if p else profiles.BOT_DEFAULT)
    return {
        "registered": p is not None,
        "install_key": install,
        "tenant": tid,
        "tenant_from_profile": bool(p and p.tenant == tid),
        "bot": bot.id,
        "bot_name": bot.name,
        "hermes_allowed": profiles.hermes_allowed(p),
    }


@router.put("/profiles/{install_key}")
def put_profile(install_key: str, payload: dict = Body(default_factory=dict),
                authorization: str | None = Header(default=None)) -> dict:
    """新建或更新一台设备的档案。

    请求体（字段都可选，只改传入的）：
        tenant  目标租户 id（必须已注册，否则 400）
        bot     管家角色 id（必须已存在，否则 400）
        hermes  是否允许调用本机 Hermes（true/false）
        label   显示名
        note    备注（≤120 字）
    """
    _check_auth(authorization)
    try:
        p = profiles.set_profile(
            urllib.parse.unquote(install_key),
            tenant=payload.get("tenant") if "tenant" in payload else None,
            bot=payload.get("bot") if "bot" in payload else None,
            hermes=bool(payload["hermes"]) if "hermes" in payload else None,
            label=payload.get("label") if "label" in payload else None,
            note=payload.get("note") if "note" in payload else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "profile": p.public()}


@router.delete("/profiles/{install_key}")
def delete_profile(install_key: str,
                   authorization: str | None = Header(default=None)) -> dict:
    """删除设备档案。**不删数据**——数据仍留在原租户里，由人工处置。"""
    _check_auth(authorization)
    ok = profiles.remove_profile(urllib.parse.unquote(install_key))
    if not ok:
        raise HTTPException(status_code=404, detail="device profile not found")
    return {"ok": True}


@router.get("/bots")
def list_bots(authorization: str | None = Header(default=None)) -> dict:
    """管家角色列表。多台设备可共用一个角色，也可以各建各的。"""
    _check_auth(authorization)
    return {"bots": [b.public() for b in profiles.all_bots()]}


@router.put("/bots/{bot_id}")
def put_bot(bot_id: str, payload: dict = Body(default_factory=dict),
            authorization: str | None = Header(default=None)) -> dict:
    """新建或更新一个管家角色。

    请求体（字段都可选）：
        name      显示名
        persona   角色说明（≤500 字，随请求带给 Hermes 作角色线索）
        base_url  可选：该角色专用的 Hermes 地址（留空 = 用全局 DH_HERMES_BASE_URL）
        hermes    该角色默认是否允许走本机 Hermes
    """
    _check_auth(authorization)
    try:
        b = profiles.upsert_bot(
            urllib.parse.unquote(bot_id),
            name=str(payload.get("name") or ""),
            persona=str(payload.get("persona") or ""),
            base_url=str(payload.get("base_url") or ""),
            hermes=bool(payload["hermes"]) if "hermes" in payload else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "bot": b.public()}


@router.delete("/bots/{bot_id}")
def delete_bot(bot_id: str, authorization: str | None = Header(default=None)) -> dict:
    """删除管家角色。绑定了它的设备会**改回默认管家**（不删设备档案）。"""
    _check_auth(authorization)
    try:
        ok = profiles.remove_bot(urllib.parse.unquote(bot_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="bot not found")
    return {"ok": True}


@router.put("/upload/{seg_id}")
async def upload(seg_id: int, request: Request,
                 authorization: str | None = Header(default=None),
                 x_session_id: str = Header(default="0"),
                 x_start_offset_ms: str = Header(default="0"),
                 x_duration_ms: str = Header(default="0"),
                 x_speech_ratio: str = Header(default="0"),
                 x_tenant: str = Header(default=""),
                 device: str = "", tenant: str = "", install: str = "") -> dict:
    """device 可选参数：设备/成员标识（2026-09-09 多团队成员支持）。

    手机端把设置里的「设备名称」URL 编码后放 query（?device=...）；
    兼容 header X-Device-Name（同样需 URL 编码，header 不允许非 ASCII）。
    不带则落库为 ''，统计里显示「未标记」——旧版 App 完全兼容。

    install 可选参数（2026-09-15）：客户端安装实例 UUID（?install=...），
    用作**设备命名空间键**，让多台设备的本机段号不再互相顶掉（见 store 的
    device_registry 说明）。旧版 App 不带 → 退回按 device_label 分空间。

    同时 install 也是**设备档案**的键（2026-09-15）：档案决定这台设备的数据
    落在哪个租户、用哪个管家角色、允不允许走本机 Hermes。设备未登记时按客户端
    自报的租户自动登记一条（零配置部署），之后以服务端档案为权威。
    """
    _check_auth(authorization)
    device = (device or request.headers.get("x-device-name") or "").strip()
    device = urllib.parse.unquote(device)[:40]
    # install：安装实例标识（客户端首次启动生成并持久化的 UUID）。
    # 缺省为空 → store 侧退回用 device_label 作命名空间键，旧版 App 完全兼容。
    install = urllib.parse.unquote((install or "").strip())[:64]

    tid = _resolve_tenant(tenant, x_tenant, install)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")

    # 自动登记（幂等）：只补显示名，绝不覆盖管理员设过的租户 / 管家 / Hermes 开关
    if install:
        profiles.ensure_profile(install, label=device, tenant=tid)

    session_id = _int_header("X-Session-Id", x_session_id, 0, 2 ** 31 - 1)
    # start_ms 必须落在合理区间：0 会被 fromtimestamp 解析成 1970-01-01，
    # 产生一个永远查不到的孤儿日期分区。
    start_ms = _int_header("X-Start-Offset-Ms", x_start_offset_ms, _MIN_START_MS, _MAX_START_MS)
    duration_ms = _int_header("X-Duration-Ms", x_duration_ms, 0, 30 * 60 * 1000)
    try:
        speech_ratio = float(x_speech_ratio)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad header X-Speech-Ratio")
    if not 0.0 <= speech_ratio <= 1.0:
        raise HTTPException(status_code=400, detail="bad header X-Speech-Ratio: out of range")

    # 日期由段起始时间推算（epoch ms → 当地 yyyy-MM-dd）
    date = datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%d")

    # 磁盘写入 + SQLite 都是阻塞调用；放进线程池，避免卡住事件循环。
    def _persist() -> None:
        # 设备命名空间（2026-09-15）：先算出全局唯一段号，**文件名也用它**。
        # 修复前文件名是设备本机的 <seg_id>.wav，两台设备各自的 3 号段会写到
        # 同一个路径 —— 后上传的把先上传的原始音频整段覆盖，不可恢复。
        gid = store.global_seg_id(install, seg_id, device, tid)
        path = tenants.resolve(tid).audio_dir / date / f"{gid}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)  # 幂等：同一台设备同一段重传直接覆盖
        store.upsert_segment(
            seg_id=gid, session_id=session_id, date=date,
            start_offset_ms=start_ms, duration_ms=duration_ms,
            speech_ratio=speech_ratio, path=str(path), device_label=device,
            tenant=tid, install_key=install, src_seg_id=seg_id,
        )

    await run_in_threadpool(_persist)
    return {"ok": True, "date": date}


@router.get("/devices")
def devices(days: int = 7, authorization: str | None = Header(default=None),
            x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """多设备上传概况（2026-09-09）：团队成员各自的手机都传了多少、最近何时在线。

    设备名来自手机端设置「设备名称」（随上传携带）；旧版 App 归入「未标记」。
    """
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    days = max(1, min(days, 90))
    return {"tenant": tid, "days": days, "devices": store.devices_summary(days, tid)}


@router.post("/done/{date}")
async def done(date: str, final: int = 0, force: int = 0,
               authorization: str | None = Header(default=None),
               x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """必须是 async def：pipeline.trigger 内部要拿当前事件循环创建后台任务。

    若写成同步 def，FastAPI 会把本端点丢进线程池执行，那里没有运行中的事件循环，
    asyncio.get_running_loop() 直接抛 RuntimeError: no running event loop。

    final=1 表示日终终稿（跑去口气 + 总结），final=0（默认）表示增量上传
    （只补 ASR 转写，沿用上一版干净稿/总结）。旧版 App 不带该参数时按增量处理，
    行为向后兼容——只是不会重算 LLM，等 17:30 的终稿触发。
    """
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    _require_date(date)
    # final=0 增量：ASR + LLM 干净稿/总结都跑（准实时三版一致），
    # 但不做日终重转与删音频；final=1 日终：额外全段重转 + 清理音频。
    # force=1：强制重算（1.0.4，手机端「重新上传某天」用）——即使当天全部段
    # 都已转写过（segs/roleless 皆空）也继续重建干净稿/总结/关键词，
    # 并绕过处理窗口（用户显式操作，不该被窗口挡掉）。
    started = pipeline.trigger(
        date, with_llm=True, final_pass=(final == 1),
        tenant=tid, force=(force == 1),
    )
    # in_window / agg_windows（2026-09-27 窗口 / 2026-10-21 数量）：pipeline_started=False
    # 且 in_window=False 表示「已落库、等触发」，客户端可据此显示「将于 13:00/17:45
    # 或积压满 N 段后处理」而不是「处理中」。老客户端忽略这两个新字段，行为不变。
    return {"ok": True, "pipeline_started": started, "final": bool(final), "tenant": tid,
            "in_window": config.agg_active(),
            "agg_windows": config.AGG_WINDOWS_RAW}


def _speaker_options(date: str, tenant: str = tenants.DEFAULT_TENANT) -> list[dict]:
    """当天出现过的说话人名单（2026-09-08）：本人置顶，其余按段数降序。

    只要存过/上传过该角色的录音（speaker_label 非空）就会出现在名单里，
    客户端与智能体据此做「按角色快速查看」。空标签（未判定）不进名单。
    """
    counts: dict[str, int] = {}
    for seg in store.segments_of_date(date, tenant):
        lab = (seg["speaker_label"] or "").strip()
        if lab:
            counts[lab] = counts.get(lab, 0) + 1
    return [
        {"label": k, "count": v}
        for k, v in sorted(counts.items(),
                           key=lambda kv: (kv[0] != config.SELF_LABEL, -kv[1]))
    ]


_LINE_SPEAKER_RE = re.compile(r"^\[\d{2}:\d{2}\]\s*\[([^\]]+)\]")


def _filter_lines_by_speaker(text: str, label: str) -> str:
    """按说话人过滤逐字稿：只保留行首标签匹配的行。未判定行（无标签）不保留。"""
    return "\n".join(
        ln for ln in (text or "").split("\n")
        if (m := _LINE_SPEAKER_RE.match(ln)) and m.group(1).strip() == label
    )


@router.get("/report/{date}")
def report(date: str, speaker: str = "",
           authorization: str | None = Header(default=None),
           x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """speaker 可选参数（2026-09-08）：按角色快速查看。

    - 不带 speaker：行为与旧版完全一致（兼容旧 App），响应额外多一个 speakers 名单
    - speaker=本人（或其别名，如「大锅」）：raw/clean 只保留本人发言行，
      summary_cards 保留【我的动态】卡（四栏本就逐条标注说话人，原样保留）
    - speaker=其他人名：raw/clean 只保留该人发言行（精确到行级标签），
      【我的动态】【他人动态】时间轴卡剔除（他人动态是多人合并的，按卡过滤会误导）
    """
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    _require_date(date)
    markers = store.markers_of_date(date, tid)
    speakers = _speaker_options(date, tid)
    speaker = speaker.strip()
    if speaker and speaker in config.SELF_ALIASES:
        speaker = config.SELF_LABEL
    # 说话人标记**先于日报正文构造**。
    # 手机端状态页靠 segment_flags 才能把「待判定」换成角色名，而日报正文（干净稿/
    # 总结）要等 LLM 跑完。旧实现把两者绑在同一个 if 后面，正文没就绪时连标记一起
    # 返回空数组 —— 于是只要管线崩过或 LLM 还没跑完，手机端就一直显示「待判定」。
    # 2026-09-14 实测：CED 长音频崩溃期间 131 段全是待判定，而段的角色其实早就标好了。
    flags = []
    for seg in store.segments_of_date(date, tid):
        flags.append({
            # id 回传的是**设备本机段号**，不是库里的全局 seg_id。
            # 手机端 status 页拿它直接 UPDATE 本地 Room 的 segment.id（applySegmentFlags），
            # 所以这里必须是本机号；库里那个全局号（含设备命名空间偏移）对客户端毫无意义。
            # 历史行（src_seg_id=0）落回 seg_id —— 那时两者本来就是同一个值。
            "id": seg["src_seg_id"] or seg["seg_id"],
            # 归属的安装实例。客户端据此只应用属于自己那些段的标记：
            # 同一租户下两台设备都可能有本机 3 号段，不区分就会互相涂改状态页。
            # 空串 = 历史行（归属未知），客户端一律接受，避免老数据永远显示「待判定」。
            "install_key": seg["install_key"] or "",
            "is_user": None if seg["is_user"] is None else bool(seg["is_user"]),
            "is_speech": bool(seg["is_speech"]),
            "speaker": seg["speaker_label"],
            "speech_ratio": seg["speech_ratio"],
        })

    r = store.get_report(date, tid)
    if not r:
        return {"ready": False, "raw_transcript": "", "clean_transcript": "", "summary": "",
                "summary_cards": [], "segment_flags": flags, "markers": markers,
                "speakers": speakers, "speaker_filter": speaker}
    cards = llm.parse_summary_cards(r["summary"])
    # 重要时刻作为一张确定性卡片追加在 LLM 卡片之后（App 端零改动即可见）。
    # 借鉴 DayoTag 的随手标记：按一下 = 日报里多一个可回查的时间锚点。
    if markers:
        cards.append({
            "title": "重要时刻",
            "body": "\n".join(f"{m['t']}{(' ' + m['note']) if m.get('note') else ''}"
                              for m in markers),
        })
    raw_t, clean_t = r["raw"], r["clean"]
    if speaker:
        raw_t = _filter_lines_by_speaker(raw_t, speaker)
        clean_t = _filter_lines_by_speaker(clean_t, speaker)
        cards = [c for c in cards
                 if c["title"] not in ("我的动态", "他人动态")
                 or (c["title"] == "我的动态" and speaker == config.SELF_LABEL)]
    return {
        "ready": bool(r["ready"]),
        "raw_transcript": raw_t,
        "clean_transcript": clean_t,
        "summary": r["summary"],
        # 总结卡片：由服务端解析【栏目】得到，App 直接渲染卡片，不自己猜格式。
        # 解析为空说明模型没按格式输出，App 回退成整段文本（内容不丢）。
        "summary_cards": cards,
        "segment_flags": flags,
        "markers": markers,
        # 当天出现过的说话人名单（本人置顶，其余按段数降序），供按角色快速选择
        "speakers": speakers,
        "speaker_filter": speaker,
    }


@router.post("/mark/{date}")
async def mark(date: str, body: dict = Body(default={}),
               authorization: str | None = Header(default=None),
               x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """标记此刻（重要时刻）。body: {"t": "HH:MM(:SS)", "note": "可选，≤120字"}

    手机「标记此刻」按钮与外部 agent 共用；t 是手机本地时间，仅作锚点展示，
    不参与转写对齐（段落时间戳以 X-Start-Offset-Ms 为准）。
    """
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    _require_date(date)
    t = str((body or {}).get("t") or "").strip()
    if not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", t):
        raise HTTPException(status_code=400, detail="bad t, expect HH:MM(:SS)")
    note = str((body or {}).get("note") or "").strip()[:120]
    item = await run_in_threadpool(store.add_marker, date, t, note, tid)
    return {"ok": True, "marker": item}


def _make_backup(scope: str) -> dict:
    """生成一份备份包，返回 {"file", "size_bytes", "tenant"}。

    scope：租户 id，或 "all"（全部租户打进一个包）。

    归档范围：text/（文字三件套）+ corpus/（语料导出）+ markers/ + digitalhuman.db。
    刻意排除：audio/（上传暂存，处理完即删）、tls/（私钥不该进备份包）、
    backups/ 自身（避免递归打包）。
    SQLite 可能正被管线写入：先拷一份快照再进归档，保证包内 DB 一致。

    ⚠️ 这是备份包生成的**唯一实现**：/backup 与「恢复前的安全快照」都走这里。
    复制一份改改的后果是两条路径产出的包结构不一致，等到真需要恢复时才发现。
    """
    import shutil
    import zipfile

    backup_dir = config.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"backup_{scope}_{datetime.now():%Y%m%d_%H%M%S}.zip"
    target = backup_dir / name

    scope_tenants = ([t for t in tenants.all_tenants()] if scope == "all"
                     else [tenants.resolve(scope)])
    snaps: list[tuple[str, Path]] = []
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for t in scope_tenants:
                db_snap = config.DATA_DIR / f"backup_db_{t.id}.tmp"
                if t.db_path.exists():
                    shutil.copyfile(t.db_path, db_snap)
                    snaps.append((f"tenants/{t.id}/digitalhuman.db", db_snap))
                for d, arc in ((t.text_dir, "text"), (t.corpus_dir, "corpus"),
                               (t.markers_dir, "markers")):
                    if d.exists():
                        for f in sorted(d.rglob("*")):
                            if f.is_file():
                                zf.write(f, f"tenants/{t.id}/{arc}/"
                                         + str(f.relative_to(d)))
                for arc, snap in snaps:
                    zf.write(snap, arc)
                snaps.clear()
    finally:
        for _, snap in snaps:
            snap.unlink(missing_ok=True)
        # 快照临时文件无论如何清一次（防上面中途异常残留）
        for tmp in config.DATA_DIR.glob("backup_db_*.tmp"):
            tmp.unlink(missing_ok=True)

    log.info("backup done (%s): %s (%d bytes)", scope, name, target.stat().st_size)
    return {"ok": True, "file": name, "size_bytes": target.stat().st_size,
            "tenant": scope}


@router.post("/backup")
async def backup(authorization: str | None = Header(default=None),
                 x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """触发一次全量数据备份（归档范围见 _make_backup）。"""
    _check_auth(authorization)
    # "all" 是备份特例（全部租户打进一个包），不走白名单校验
    scope = ((x_tenant or "").strip() or (tenant or "").strip()) or tenants.DEFAULT_TENANT
    if scope != "all":
        scope = _resolve_tenant(tenant, x_tenant)
    return await run_in_threadpool(_make_backup, scope)


# 备份包文件名白名单。这是**唯一**一处把客户端传来的字符串当路径用的地方，
# 必须严格正则：`..`、绝对路径、子目录都挡在外面，否则就是一个目录穿越读文件。
_BACKUP_NAME_RE = re.compile(r"^backup_[A-Za-z0-9_.-]{1,64}_\d{8}_\d{6}\.zip$")


@router.get("/backup/list")
def backup_list(authorization: str | None = Header(default=None)) -> dict:
    """列出服务端已有备份包（按修改时间倒序，最多 100 条）。

    手机端「数据备份」卡片用它渲染列表，并能选择下载或从某一份恢复。
    """
    _check_auth(authorization)
    d = config.DATA_DIR / "backups"
    out: list[dict] = []
    if d.is_dir():
        files = [f for f in d.glob("*.zip") if _BACKUP_NAME_RE.fullmatch(f.name)]
        for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            st = f.stat()
            out.append({
                "file": f.name,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return {"ok": True, "backups": out}


@router.get("/backup/download/{name}")
def backup_download(name: str, authorization: str | None = Header(default=None)):
    """下载一份备份包（zip）。客户端「导出数据」用它把归档拉到本机。"""
    _check_auth(authorization)
    if not _BACKUP_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="bad backup name")
    root = (config.DATA_DIR / "backups").resolve()
    p = (root / name).resolve()
    # 双保险：正则已挡住穿越，这里再确认解析后的路径确实落在 backups/ 之内
    if root != p.parent or not p.is_file():
        raise HTTPException(status_code=404, detail="backup not found")
    return FileResponse(p, media_type="application/zip", filename=name)


# 从备份包恢复时允许落地的目录/文件。audio/ 与 tls/ 一律不恢复：
# 前者是暂存、恢复旧音频会让已处理段被重复处理；后者是私钥，包内本来就没有。
_RESTORE_TOP = ("text", "corpus", "markers", "digitalhuman.db")
_MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB 上限，防误传大文件打爆内存


@router.post("/backup/restore")
async def backup_restore(file: UploadFile, tenant: str = "", confirm: int = 0,
                         authorization: str | None = Header(default=None),
                         x_tenant: str = Header(default="")) -> dict:
    """从备份包恢复数据（**破坏性**：会整体替换该租户的库与 text/corpus/markers）。

    安全设计（顺序即契约）：
    1. `confirm=1` 必填 —— 这是唯一会覆盖线上数据的接口，不允许被误调用。
    2. 先在服务端生成一份**恢复前快照**（_make_backup），返回其文件名。
       恢复错了还能用快照退回来；没有这一步的恢复接口是不负责任的。
    3. 校验包结构与每个条目路径（拒绝绝对路径与 `..`）。
    4. 关闭该租户的 sqlite 连接 → 替换文件 → 重新打开（重跑建表/迁移）。
       不关连接的话进程仍持有旧文件句柄：查询会读到恢复前的数据，
       写入则落进一个已经没人看的文件，而且不会报错。
    """
    _check_auth(authorization)
    if not confirm:
        raise HTTPException(status_code=400, detail="restore is destructive; pass confirm=1")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")
    if len(raw) > _MAX_RESTORE_BYTES:
        raise HTTPException(status_code=400, detail="backup too large (>2GiB)")
    want = (tenant or x_tenant or "").strip()

    def _do() -> dict:
        import io
        import shutil
        import zipfile

        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise HTTPException(status_code=400, detail="not a zip backup") from e
        with zf:
            names = [n for n in zf.namelist() if n and not n.endswith("/")]
            for n in names:
                # zip 条目名是攻击者可完全控制的内容：绝对路径与 .. 都要挡
                parts = Path(n).parts
                if n.startswith("/") or ".." in parts or "\\" in n:
                    raise HTTPException(status_code=400, detail=f"bad entry: {n}")
            tids = sorted({p[1] for p in (Path(n).parts for n in names)
                           if len(p) > 2 and p[0] == "tenants"})
            if not tids:
                raise HTTPException(status_code=400, detail="no tenants/<id>/ in backup")
            if want:
                if want not in tids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"backup has tenants {tids}, not '{want}'")
                tid = want
            elif len(tids) == 1:
                tid = tids[0]
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"backup contains {tids}; specify tenant=")

            # 恢复前快照：先留退路再动手
            safety = _make_backup(tid)

            t = tenants.resolve(tid)
            t.ensure_dirs()
            prefix = f"tenants/{tid}/"
            files = 0
            store.close_tenant(tid)
            try:
                # close 会把 WAL 落盘并清掉 -wal/-shm；万一残留，它们在替换主库后
                # 会让 sqlite 认为库损坏，所以显式清理。
                for suffix in ("-wal", "-shm"):
                    (t.base / f"digitalhuman.db{suffix}").unlink(missing_ok=True)
                for n in names:
                    if not n.startswith(prefix):
                        continue
                    rel = n[len(prefix):]
                    top = rel.split("/", 1)[0]
                    if top not in _RESTORE_TOP:
                        continue
                    dest = t.db_path if top == "digitalhuman.db" else (t.base / rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    files += 1
            finally:
                # 无论成功失败都要把连接重建回来，否则该租户此后所有请求都会失败
                store.connect(tid)

        ok_db = t.db_path.is_file()
        log.warning("restore done tenant=%s files=%d db=%s safety=%s",
                    tid, files, ok_db, safety["file"])
        return {"ok": True, "tenant": tid, "files": files,
                "db_restored": ok_db, "safety_backup": safety["file"]}

    return await run_in_threadpool(_do)


# 总结是**卡片式**文本（【他人动态】/【关键原话】/【我的动态】…），不是一段话。
# 播报稿要的是人话，所以必须先把卡片标题与行首时间戳/角色标签剥掉，
# 否则会念出「今日小结：【他人动态】」这种毫无意义的句子（2026-09-15 实测出现过）。
_CARD_TITLE_RE = re.compile(r"^【[^】]{1,16}】$")
_LINE_PREFIX_RE = re.compile(r"^(?:【\d{1,2}:\d{2}[^】]*】)?\s*(?:\[[^\]]{1,20}\]\s*[:：]?\s*)?")


def _summary_lead(summary: str, limit: int = 60) -> str:
    """从卡片式总结里抽出**第一句真正的话**，供语音播报用。"""
    for ln in (summary or "").split("\n"):
        t = ln.strip().lstrip("-*#").strip()
        if not t or _CARD_TITLE_RE.match(t):
            continue
        t = _LINE_PREFIX_RE.sub("", t).strip()
        t = re.split(r"[。！？]", t, maxsplit=1)[0].strip()
        if len(t) < 8:      # 太短的碎片（残留标点/孤立词）不值得念
            continue
        return t[:limit]
    return ""


def _tts_script(date: str, tenant: str) -> str:
    """把「今天的数据」压成一段 30 秒内能念完的中文播报稿。

    刻意不整篇念总结：一份日报的总结动辄上千字，念完要几分钟，而用户点这个
    按钮只是想确认「TTS 通了、今天的数据在」。取一句真实内容 + 段数/人数分布，
    信息量足够判断数据是否正常，合成也快。
    """
    segs = [s for s in store.segments_of_date(date, tenant)
            if s["is_speech"] and (s["text"] or "").strip()]
    if not segs:
        return f"语音播报测试成功。{date} 还没有可播报的语音数据。"
    counts: dict[str, int] = {}
    for s in segs:
        lab = (s["speaker_label"] or "").strip() or "未判定"
        counts[lab] = counts.get(lab, 0) + 1
    mins = sum(int(s["duration_ms"] or 0) for s in segs) // 60_000
    r = store.get_report(date, tenant) or {}
    lead = _summary_lead(r.get("summary") or "")
    # 「合计约 0 分钟」念出来很怪（数据量小时的常态），换成自然说法
    span = f"合计约 {mins} 分钟" if mins > 0 else "合计不足 1 分钟"
    out = [f"语音播报测试成功。{date} 共记录 {len(segs)} 段语音，{span}。"]
    if counts:
        who = "、".join(f"{k} {v} 段" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:4])
        out.append(f"说话人分布：{who}。")
    if lead:
        out.append(f"今日内容片段：{lead}。")
    return "".join(out)[:400]


@router.get("/tts/preview")
def tts_preview(date: str = "", authorization: str | None = Header(default=None),
                x_tenant: str = Header(default=""), tenant: str = "") -> Response:
    """合成一段播报今天数据的语音，直接返回 16kHz 单声道 WAV。

    设置页的「测试播报」按钮用它。走的是与真实回话**同一个** tts.synthesize，
    所以这里通过就等于真实播报链路可用；后端按 piper → macOS say → edge-tts
    顺序自动挑（见 app/tts.py）。

    播报稿经 X-TTS-Text 头回传（URL 编码，header 不允许非 ASCII），
    客户端可以把「念了什么」显示出来——不然用户听到一串数字也不知道对不对。
    合成失败返回 503 并带上当前后端名，省掉一轮猜。
    """
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    day = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    _require_date(day)
    text = _tts_script(day, tid)

    out = config.DATA_DIR / f"tts_preview_{secrets.token_hex(4)}.wav"
    try:
        res = tts.synthesize(text, out)
        data = Path(res).read_bytes() if res is not None and Path(res).is_file() else b""
    finally:
        out.unlink(missing_ok=True)
    if not data:
        raise HTTPException(
            status_code=503,
            detail=f"TTS 合成失败（当前后端 {tts.health().get('backend')}），"
                   "请在服务端检查 TTS 配置",
        )
    log.info("tts preview: date=%s tenant=%s chars=%d bytes=%d backend=%s",
             day, tid, len(text), len(data), tts.health().get("backend"))
    return Response(
        content=data, media_type="audio/wav",
        headers={"X-TTS-Text": urllib.parse.quote(text), "Cache-Control": "no-store"},
    )


@router.post("/tts/synthesize")
def tts_synthesize(payload: dict = Body(default_factory=dict),
                   authorization: str | None = Header(default=None)) -> Response:
    """把任意一段文本合成为语音，直接返回 16kHz 单声道 WAV。

    与 /tts/preview 共用**同一个** `tts.synthesize` 与同一套后端探测
    （app/tts.py 的 `backend()`）：预览能响，正式合成就必须能响。
    两边各写一套探测逻辑，迟早会漂移成「预览正常、正式合成没声音」，
    而这种故障在设置页完全看不出来——用户只会觉得「这个功能坏了」。

    文本长度有上限（`config.TTS_SYNTH_MAX_CHARS`，默认 20000 字）：
    合成是同步阻塞的外部调用，不设闸门等于让任何调用方拿它跑长文、
    把服务的工作线程占满。与对话回复那个 120 字的 `TTS_MAX_CHARS` 不是一回事，
    那个是「模型跑偏就掐掉」，这个是「公开入口」的防滥用闸门。

    播报内容经 X-TTS-Text 头回传（URL 编码，header 不允许非 ASCII），
    与 /tts/preview 同一口径：调用方要能确认「念的到底是哪段」。
    只回显前 `config.TTS_SYNTH_ECHO_MAX` 字：头不是正文，
    长文本 URL 编码后能把响应头撑到上百 KB，Node 客户端会直接报 Header overflow
    （服务端看着好好的，调用方却把响应丢了）。音频本身仍是全文。
    """
    _check_auth(authorization)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    limit = config.TTS_SYNTH_MAX_CHARS
    if len(text) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"text 超长：{len(text)} 字符，上限 {limit}（分句合成，别整篇丢进来）",
        )
    fmt = str(payload.get("format") or "wav").strip().lower()
    if fmt != "wav":
        # 只出 wav：手机端与 ASR 输入链路的格式就是 16k mono WAV，
        # 多支持一种格式就要多一套转码，多出来的收益远小于复杂度。
        raise HTTPException(status_code=400, detail="format 只支持 wav")

    out = config.DATA_DIR / f"tts_synth_{secrets.token_hex(4)}.wav"
    try:
        res = tts.synthesize(text, out)
        data = Path(res).read_bytes() if res is not None and Path(res).is_file() else b""
    finally:
        # 临时文件用完即删：DATA_DIR 会被备份与清理任务扫到，
        # 留一堆 wav 在那里只会让数据目录越滚越大。
        out.unlink(missing_ok=True)
    if not data:
        raise HTTPException(
            status_code=503,
            detail=f"TTS 合成失败（当前后端 {tts.health().get('backend')}），"
                   "请在服务端检查 TTS 配置",
        )
    log.info("tts synthesize: chars=%d bytes=%d backend=%s",
             len(text), len(data), tts.health().get("backend"))
    return Response(
        content=data, media_type="audio/wav",
        headers={"X-TTS-Text": urllib.parse.quote(text[:config.TTS_SYNTH_ECHO_MAX]),
                 "Cache-Control": "no-store"},
    )


@router.post("/push/workbench")
def push_workbench(payload: dict = Body(default_factory=dict),
                   authorization: str | None = Header(default=None),
                   x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """把一段口述物料描述推进「智能家居 AI 工作台」，由它做结构化抽取。

    工作台会对文本跑 LLM 抽取；判为产品类且无缺项时直接落库成产品。
    本端点只负责「把东西送过去」与「把结果摘要带回来」，抽取逻辑在工作台侧。

    请求体：
        tenant   采集端租户 id（缺省取 X-Tenant 头，再缺省 default）
        kind     目前只有 'transcript'
        title    标题（≤100 字）
        text     正文（20~200000 字）
        docType  product | meeting | generic，缺省 meeting
        ref      来源标识（通常是某段录音的 seg_id）。**给了就幂等**：
                  同一个 ref 第二次推送会被认成重复，不会产生第二次入库。
        viaHermes 是否经 Hermes 中转投递。缺省取部署配置
                  DH_WORKBENCH_PUSH_VIA_HERMES（默认关）。
                  开启后文本先过 Hermes 一遍（它做意图归一与判定），
                  再由本服务执行投递；**Hermes 不可达时明确报 503，
                  绝不静默直投** —— 否则调用方以为经过了 Hermes，其实没有。

    返回体是摘要（作业 id / 文档类型 / 缺项数 / 抽取得分）。
    `duplicate` 标明这次是「真推了」还是「之前已经推过」。
    走了中转时 `hermes` 段带 Hermes 的理解结果（应答、意图、耗时）。
    """
    _check_auth(authorization)
    # 采集端租户必须先过注册表校验（_resolve_tenant）：未注册一律 403，
    # 绝不静默落到 default，否则拼错 id 的素材会推进到别人的空间。
    tid = _resolve_tenant(str(payload.get("tenant") or tenant or ""), x_tenant)

    kind = str(payload.get("kind") or "transcript").strip().lower()
    if kind != "transcript":
        raise HTTPException(
            status_code=400, detail=f"unsupported kind: {kind}（目前只支持 transcript）")

    text = str(payload.get("text") or "").strip()
    if len(text) < workbench_push.TEXT_MIN:
        raise HTTPException(
            status_code=400, detail=f"text 太短：至少 {workbench_push.TEXT_MIN} 字")
    if len(text) > workbench_push.TEXT_MAX:
        raise HTTPException(
            status_code=400, detail=f"text 太长：最多 {workbench_push.TEXT_MAX} 字")

    doc_type = str(payload.get("docType") or "meeting").strip().lower()
    if doc_type not in workbench_push.DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"bad docType: {doc_type}（可选 {'/'.join(workbench_push.DOC_TYPES)}）")

    title = str(payload.get("title") or "").strip()
    ref = str(payload.get("ref") or "").strip()

    # 是否经 Hermes 中转投递。请求里的 viaHermes 优先于部署默认值。
    # 注意不能用 bool()：JSON 里 false 是布尔，但表单/查询串传进来的是字符串
    # "false"，而 bool("false") 是 True —— 「显式说不要」会被当成「要」，方向反了。
    via_hermes = config.WORKBENCH_PUSH_VIA_HERMES
    if "viaHermes" in payload:
        via_hermes = workbench_push.truthy(payload.get("viaHermes"))

    # 缺配置先说清楚缺哪一项：这是可选对接，配错了不该让调用方猜。
    missing = workbench_push.missing_config()
    if missing:
        raise HTTPException(
            status_code=503,
            detail="工作台对接未配置，缺少：" + "、".join(missing)
                   + "（写进 server/env.local 后重启服务）")
    # 工作台租户在这里定一次：映射表里有这个采集租户就用它，否则用默认租户。
    # 两者都没有 → 503。注意这一步必须显式做：映射表非空但**不含**当前租户时，
    # missing_config 是查不出来的（它只看「有没有映射」），只有 resolve 才知道。
    try:
        wb_tenant = workbench_push.resolve_workbench_tenant(tid)
    except workbench_push.WorkbenchError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    # ---- 幂等：先占坑，再推送 ----
    # 占坑与判重必须是同一条原子语句（store.claim_push 用 INSERT OR IGNORE）：
    # 「先查后插」在并发下会双双判定为新 ref，同一段素材被推进两次。
    if ref and not store.claim_push(ref, kind, tid):
        prev = store.push_record(ref, tid) or {}
        if prev.get("job_id"):
            # 已经成功推过：原样回显上次的结果，不再打工作台一次。
            return {
                "ok": True,
                "duplicate": True,
                "status": "already_pushed",
                "ref": ref,
                "tenant": tid,
                "workbenchTenant": wb_tenant,
                "jobId": prev.get("job_id", ""),
                "jobStatus": prev.get("status", ""),
                "docType": prev.get("doc_type", ""),
                "score": prev.get("score"),
                "deficiencyCount": prev.get("deficiency", 0),
                # 已经推过的不再走 Hermes：素材早就投出去了，
                # 再理解一遍只会多花一次模型调用，不会改变任何结果。
                "viaHermes": via_hermes,
                "hermes": None,
            }
        # 占过坑但没有 job_id：上一次是半路崩掉的（进程被杀/超时），
        # 工作台那边多半什么都没收到。清掉这条残记录重试，
        # 否则这个 ref 会被永久卡成「推过了」。
        store.drop_push(ref, tid)
        store.claim_push(ref, kind, tid)

    try:
        # 经 Hermes 中转时，文本先过 Hermes 一遍（它做意图归一与判定），
        # 再由采集端执行投递。Hermes 不可达 → 明确报错，**不静默直投**。
        hermes_info = (workbench_push.hermes_understand(text, tenant=tid, ref=ref)
                       if via_hermes else None)
        resp = workbench_push.push_transcript(
            text, title=title, doc_type=doc_type, tenant=tid)
    except workbench_push.WorkbenchError as e:
        # 推送失败要撤销占坑：否则一次网络抖动就把这段素材永久标记成
        # 「已推送」，而工作台其实什么都没收到，素材从此再也不会被推进。
        if ref:
            store.drop_push(ref, tid)
        log.warning("push workbench failed: ref=%s tenant=%s %s", ref, tid, e.detail)
        raise HTTPException(status_code=e.status, detail=e.detail)

    summary = workbench_push.summarize(resp)
    if ref:
        store.finish_push(
            ref, job_id=summary["jobId"], doc_type=summary["docType"],
            deficiency=summary["deficiencyCount"], score=summary.get("score") or 0,
            status=summary["status"], tenant=tid,
        )
    log.info("push workbench: ref=%s tenant=%s docType=%s job=%s deficiency=%d",
             ref, tid, summary["docType"], summary["jobId"], summary["deficiencyCount"])
    return {
        "ok": True,
        "duplicate": False,
        "status": "pushed",
        "ref": ref,
        "tenant": tid,
        "workbenchTenant": wb_tenant,
        "jobId": summary["jobId"],
        "jobStatus": summary["status"],
        "docType": summary["docType"],
        "score": summary["score"],
        "deficiencyCount": summary["deficiencyCount"],
        "deficiencies": summary["deficiencies"],
        "fieldCount": summary["fieldCount"],
        "confirmed": summary["confirmed"],
        # 经 Hermes 中转时，这里带它对这段物料的理解结果（应答、意图、耗时）。
        "viaHermes": via_hermes,
        "hermes": hermes_info,
    }


@router.post("/pair")
async def pair(file: UploadFile, name: str = "",
               authorization: str | None = Header(default=None),
               x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """注册一个具名角色声纹（WAV，建议 ≥30s）。

    name 为空 → 注册为「本人」（老 App 行为不变）；
    name 非空 → 注册为该名字的角色（如「张三」），后续识别到就按这个名字记录。
    """
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    label = (name or "").strip() or config.SELF_LABEL
    # 2-16 字符，禁掉会污染日报格式的方括号与控制字符
    if not 1 <= len(label) <= 16 or any(c in label for c in "[]【】\n\r\t"):
        raise HTTPException(status_code=400, detail="bad name: 1-16 chars, no brackets")
    tmp = config.DATA_DIR / f"pair_tmp_{secrets.token_hex(4)}.wav"
    tmp.write_bytes(await file.read())
    try:
        ok = await run_in_threadpool(voiceprint.register_named, label, tmp, tid)
    finally:
        tmp.unlink(missing_ok=True)
    if not ok:
        raise HTTPException(status_code=400, detail="invalid audio or engine unavailable")
    log.info("voiceprint registered for name=%s", label)
    return {"ok": True, "name": label}


@router.get("/industries")
def list_industries(authorization: str | None = Header(default=None)) -> dict:
    """可选行业 + 当前行业。手机端设置页用它渲染行业选择器。"""
    _check_auth(authorization)
    return {
        "current": config.INDUSTRY or "general",
        "current_label": config.industry_label(config.INDUSTRY or "general"),
        "available": config.available_industries(),
        "terms": len(config.INDUSTRY_TERMS),
        "pinned_by_env": bool(config._ENV_INDUSTRY),
    }


@router.put("/industry/{name}")
def set_industry(name: str, authorization: str | None = Header(default=None)) -> dict:
    """切换行业。切换后新的转写立即用新词表（识别器缓存会被清掉重建）。

    已转写的历史段不会重跑——要整体纠偏请用终端跑 rebuild_report.py。
    """
    _check_auth(authorization)
    if name not in {i["key"] for i in config.available_industries()}:
        raise HTTPException(status_code=404, detail="industry not found")
    try:
        config.set_industry(name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    asr.invalidate()   # 热词变了，已加载的识别器必须重建
    log.info("industry switched to %s (%d terms)", name, len(config.INDUSTRY_TERMS))
    return {
        "ok": True,
        "industry": config.INDUSTRY,
        "label": config.industry_label(config.INDUSTRY),
        "terms": len(config.INDUSTRY_TERMS),
    }


@router.get("/speakers")
def list_speakers(authorization: str | None = Header(default=None),
                  x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """已注册的具名角色列表（当前租户）。"""
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    return {"tenant": tid, "speakers": store.list_named_speakers(tid)}


@router.delete("/speakers/{name}")
def delete_speaker(name: str, authorization: str | None = Header(default=None),
                   x_tenant: str = Header(default=""), tenant: str = "") -> dict:
    """删除一个具名角色（不历史追溯改标签，只影响后续识别）。"""
    _check_auth(authorization)
    tid = _resolve_tenant(tenant, x_tenant)
    if not store.delete_named_speaker(name, tid):
        raise HTTPException(status_code=404, detail="speaker not found")
    return {"ok": True, "name": name}


# ============================================================================
# 门店 HA 设备视图（只读，2026-09-11）
#
# 存在的意义是「多端信息同步」：Mac 壳 / Android / Tauri 三端读的是**同一份**
# 服务端快照，同一时刻三端看到的状态一致；也让手机端能在对话之外直接看设备。
#
# 刻意只读：控制动作走 Hermes 的确定性执行层（symi_devices.py），
# 服务端不重复实现一份，避免两处逻辑漂移。
# ============================================================================

@router.get("/ha/overview")
def ha_overview(authorization: str | None = Header(default=None)) -> dict:
    """门店设备概览（总数/在线/离线/域分布）。"""
    _check_auth(authorization)
    return ha_client.overview()


@router.get("/ha/devices")
def ha_devices(q: str = "", domain: str = "", limit: int = 40,
               authorization: str | None = Header(default=None)) -> dict:
    """按关键词检索设备。

    `q` 为空时返回空列表而不是全量 6000+ —— 全量清单在手机上既没用又费流量，
    调用方应该带关键词（设备名、口语说法、entity_id 片段都行）。
    """
    _check_auth(authorization)
    limit = max(1, min(int(limit or 40), 200))
    if not q.strip():
        return {"q": q, "domain": domain, "count": 0, "devices": []}
    devices = ha_client.search(q, domain=domain, limit=limit)
    return {"q": q, "domain": domain, "count": len(devices), "devices": devices}


@router.get("/ha/health")
def ha_health(authorization: str | None = Header(default=None)) -> dict:
    """HA 连接自检：是否配置、缓存了几条、快照多旧、上次错误。"""
    _check_auth(authorization)
    return ha_client.health()
