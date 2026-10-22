"""实时对话端点（FastAPI Router，prefix=`/api/talk`）。

规格：`docs/hermes-voice-conversation-2026-09-10.md`

## 这个模块解决什么

原有链路是「手机录音 → 攒段 → 增量上传 → 离线总结」，本质是**事后归档**。
Hermes 要的是**实时问答**：说「大锅，演示区灯全开」→ 1 秒内听到语音回复。

两条链路**并行不冲突**（规格 §四）：同一段音频既走 `/api/upload` 进日报，
也走 `/api/talk/audio` 进对话。所以本模块**不碰 pipeline.py**——
它只做「短段 → ASR → Hermes → TTS」这一条快路径。

## 端点

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/talk/start` | 开一轮对话，拿 turn_id |
| PUT  | `/api/talk/audio/{turn_id}` | 上传 wav → 返回 ASR + Hermes 回复 |
| GET  | `/api/talk/reply/{turn_id}` | 轮询回复（start 已同步返回时用不到） |
| GET  | `/api/talk/audio/{turn_id}/reply.wav` | 下载 TTS 音频 |
| POST | `/api/talk/end/{turn_id}` | 结束一轮，删音频 |
| GET  | `/api/talk/health` | 自检（TTS 后端 / Hermes 可达性） |

## 四个设计决定

**1. turn 状态放内存，不放数据库。**
一轮对话活 120 秒，这是热状态不是持久数据。塞进 SQLite 会引入
「服务重启后一堆过期 turn 需要清理」的额外复杂度。进程重启 = 全部作废，
客户端拿到 404 重开一轮即可（`/start` 本来就是幂等的轻操作）。
数据库只留**事后可查的记录**（`store.talk_message`）。

**2. 音频写 `data/talk/` 而不是 `data/audio/`。**
sweeper 按日期扫 `audio/<date>/` 找「未处理段」补转写。对话 wav 混进去会被
它当成漏传的日记段反复送进管线（而且对话本来就已经转写过了）。物理隔离最省事。

**3. Hermes 挂了也要能答。**
调用链是 `Hermes → 纯 AI（llm.quick_answer）→ 只回文字`，逐级降级。
用户说「大锅，今天几度」时，最差的情况应该是「听到一句没有 HA 数据的回答」，
而不是「说完没反应」。每一级降级都在响应的 `via` 字段里标明，便于复盘。

**4. 回声自激必须能被服务端掐断。**
AUTO 模式下 TTS 外放、麦克风常开，而回复又常以唤醒词开头，天然是个自激环。
客户端负责在播报期挂起拾音（第一道闸），但服务端不能假设客户端永远正确——
旧版 APK、被系统打断的播报、以后新增的播放路径都可能漏设标志。所以这里用
「连续 N 轮转写高度相似」做兜底熔断（`loop_guard`），判据与参数见 `config`。
"""
from __future__ import annotations

import difflib
import io
import logging
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import wave
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from . import asr, config, ha_client, hermes_client, llm, profiles, store, tenants, tts, voiceprint
from ._version import VERSION
from .redact import redact
from .routes import _check_auth, _resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/talk", tags=["talk"])

# 一轮对话的内存状态。key = turn_id
_turns: dict[str, dict] = {}
_lock = threading.Lock()

# 回声自激检测状态。key = tenant
#   {"hits": [(ts, 归一化文本), ...], "until": 熔断截止时间戳}
# 参数与判据说明见 config.TALK_LOOP_HITS 上方注释。
_loop_state: dict[str, dict] = {}

# 会话多轮历史（2026-09-10）。key = "tenant\x00session_id"
#   value = [(role, content), ...]，role 取 "user" / "assistant"，按时间升序。
#
# ## 为什么服务端要自己存一份
#
# Hermes 侧按 session_id 维护上下文，正常路径不需要我们管。但 Hermes 不可用时会
# 降级到本地 LLM 直答（见 _ask_hermes），**降级路径原本是无状态的单轮**——用户说
# 「那第二个呢」这种指代完全接不住，4B 模型只能硬编。这份历史只服务于降级路径，
# 不改变 Hermes 的调用方式（它拿 session_id 自己管，我们不多发一遍）。
#
# ## 内存约束（长期稳定）
#
# 每会话最多 config.TALK_SESSION_TURNS 轮、最多 _SESSION_MAX 个会话，
# 超了按最久未用淘汰。上限 ≈ 6 轮 × 2 条 × 64 会话 ≈ 768 条短字符串，
# 相对本项目其它内存占用可忽略；且 _prune_sessions 只在写入时线性扫一遍
# 64 个 key，成本极低。
_sessions: dict[str, list[tuple[str, str]]] = {}
_session_seen: dict[str, float] = {}
_SESSION_MAX = 64

# 孤儿音频保留时长：turn 过期后 wav 还会留这么久，供排查用
_ORPHAN_KEEP_SEC = 3600


def _session_key(tid: str, session_id: str, bot: str = "") -> str:
    """会话历史的键。

    带上 tenant 是**硬要求**：session_id 由客户端提供，不同租户完全可能
    撞上同一个值（尤其客户端还没修好、都发 "0" 的时候）。不隔离就会
    跨租户读到别人的对话内容（OWASP 多租户基线）。

    带上 bot（2026-09-15）同理：两台设备的 session_id 完全可能相同（都是
    开机以来的计数），而它们可能被配到不同管家角色——不隔离就会让降级回答
    把 A 角色的上下文说给 B 听。bot 为空时用 `-` 占位，保证旧客户端键不变。
    """
    return f"{tid}\x00{bot or '-'}\x00{session_id}"


def _prune_sessions() -> None:
    """淘汰最久未用的会话。只在超限时动手，正常路径是一次 len() 判断。"""
    if len(_sessions) <= _SESSION_MAX:
        return
    for key, _ in sorted(_session_seen.items(), key=lambda kv: kv[1])[
            : len(_sessions) - _SESSION_MAX]:
        _sessions.pop(key, None)
        _session_seen.pop(key, None)


def _session_history(tid: str, session_id: str, bot: str = "") -> list[tuple[str, str]]:
    """取该会话的既往轮次（副本）。无 session_id 或未记录 → 空列表。"""
    if not session_id:
        return []
    return list(_sessions.get(_session_key(tid, session_id, bot), ()))


def _session_append(tid: str, session_id: str, user_text: str,
                    reply_text: str, bot: str = "") -> None:
    """记一轮完整问答。**只记有回复的轮次**——半截的问答比没有更坏。"""
    if not session_id or not user_text or not reply_text:
        return
    key = _session_key(tid, session_id, bot)
    turns = max(1, config.TALK_SESSION_TURNS)
    hist = _sessions.setdefault(key, [])
    hist.append(("user", user_text))
    hist.append(("assistant", reply_text))
    # 只保留最近 N 轮（每轮 2 条）
    del hist[: max(0, len(hist) - turns * 2)]
    _session_seen[key] = time.time()
    _prune_sessions()



# --------------------------------------------------------------------------
# turn 生命周期
# --------------------------------------------------------------------------
def _gc() -> None:
    """清理过期 turn：删内存条目 + 删对应 wav。

    每次 /start 顺带跑一次（对话频率低，没必要为它单开一个 sweeper 任务）。
    另外扫一遍 data/talk 下超期的孤儿文件——进程被 kill 时内存里的 turn 没了，
    但 wav 还躺在磁盘上。
    """
    now = time.time()
    with _lock:
        dead = [tid for tid, t in _turns.items() if t["expires_at"] < now]
        for tid in dead:
            t = _turns.pop(tid)
            for key in ("wav", "reply_wav"):
                p = t.get(key)
                if p:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass

    root = config.talk_dir()
    if not root.is_dir():
        return
    for f in root.rglob("*.wav"):
        try:
            if now - f.stat().st_mtime > _ORPHAN_KEEP_SEC:
                f.unlink(missing_ok=True)
        except OSError:
            pass


def _live_turns_of_device(device: str) -> int:
    now = time.time()
    with _lock:
        return sum(
            1 for t in _turns.values()
            if t["device"] == device and t["expires_at"] > now
            and t.get("status") != "completed"
        )


# --------------------------------------------------------------------------
# 回声自激熔断（第二道闸）
# --------------------------------------------------------------------------
# 客户端已在播报期用 CaptureState.playingBack 挂起拾音（第一道闸，见
# TalkController.play）。这里是服务端兜底：客户端版本旧、播报路径漏设标志、
# 或被系统打断时，靠「转写内容高度相似」把环掐断。判据与参数见
# config.TALK_LOOP_HITS 上方注释。
#
# 并发说明：本模块的熔断读写都是**纯同步**操作（无 await），跑在 FastAPI 的
# 事件循环里，单个函数执行期间不会被打断，因此不需要额外加锁。
def _norm_for_loop(text: str) -> str:
    """归一化：只留字与数字。自激各轮的差异通常只在标点/空格上。"""
    return re.sub(r"[^\w]", "", (text or "").lower())


def _loop_blocked(tid: str) -> bool:
    """本租户是否处于熔断静默期。"""
    st = _loop_state.get(tid)
    return bool(st) and time.time() < st.get("until", 0.0)


def _loop_note(tid: str, text: str) -> bool:
    """记一次唤醒词命中，返回 True = 判定自激（本轮也不应答）。

    只看「最近 N 轮是否**两两相似**」，不数绝对次数：真人连问三句内容不同
    → 相似度低 → 不熔断；自激三轮录的是同一段外放音频 → 高度相似 → 熔断。
    """
    now = time.time()
    st = _loop_state.setdefault(tid, {"hits": [], "until": 0.0})
    hits = [(t, x) for t, x in st["hits"] if now - t <= config.TALK_LOOP_WINDOW]
    hits.append((now, _norm_for_loop(text)))
    need = max(2, config.TALK_LOOP_HITS)
    hits = hits[-need:]
    st["hits"] = hits
    if len(hits) < need:
        return False
    for (_, a), (_, b) in zip(hits, hits[1:]):
        if difflib.SequenceMatcher(None, a, b).ratio() < config.TALK_LOOP_SIMILARITY:
            return False
    st["until"] = now + config.TALK_LOOP_COOLDOWN
    st["hits"] = []
    log.warning("talk 回声自激熔断：tenant=%s 连续 %d 轮高度相似，静默 %.0fs；样本=%r",
                tid, need, config.TALK_LOOP_COOLDOWN, hits[-1][1][:40])
    return True


# --------------------------------------------------------------------------
# 近重复抑制（第三道闸，2026-09-10）
# --------------------------------------------------------------------------
# 与自激熔断的区别见 config.TALK_DUP_WINDOW 上方注释：熔断看「连续 N 轮的
# 趋势」，这里只看「相邻两轮」。触发条件是**同一句话被两条路径各切一份**——
# 客户端日报 VAD 与对话 VAD 同时喂同一路音频，各自切段、各自触发一轮对话，
# 而两个 VAD 的边界不同，ASR 结果只差一两个字（实测「大郭」/「大哥」），
# 于是 Hermes 侧的 `hash(text)` 去重失效，同一句话被答两遍、播两遍。
#
# 键取 (租户, 会话)：不同会话/不同设备上的相同问句是两条独立对话，
# 不该互相压制。会话 id 为空时退化成「该租户全局」，与客户端的
# conversationSessionId 兜底行为一致。
#
# 并发说明同 _loop_note：纯同步读写，无 await。
_dup_state: dict[tuple[str, str], tuple[float, str]] = {}


def _dup_note(tid: str, session_id: str, text: str) -> bool:
    """记一轮转写，返回 True = 判定为上一轮的同句重复（本轮不应答）。"""
    cur = _norm_for_loop(text)
    key = (tid, session_id or "")
    now = time.time()
    prev = _dup_state.get(key)
    _dup_state[key] = (now, cur)
    if not prev:
        return False
    ts, old = prev
    if now - ts > config.TALK_DUP_WINDOW:
        return False
    # 短句不参与：见 config.TALK_DUP_MIN_CHARS 的说明。
    if len(cur) < config.TALK_DUP_MIN_CHARS or len(old) < config.TALK_DUP_MIN_CHARS:
        return False
    sim = difflib.SequenceMatcher(None, old, cur).ratio()
    if sim < config.TALK_DUP_SIMILARITY:
        return False
    log.warning("talk 近重复抑制：tenant=%s session=%s 与上一轮相似度 %.2f，跳过；"
                "上一轮=%r 本轮=%r", tid, session_id or "-", sim, old[:40], cur[:40])
    return True


# --------------------------------------------------------------------------
# 声纹角色门控
# --------------------------------------------------------------------------
def _owner_allowed(is_user: bool | None, gate: str) -> bool:
    """声纹门控判据。gate 取值见 config.TALK_OWNER_GATE 上方注释。

    三态语义（关键在 None 怎么算）：

    - `lenient`：只拒绝**明确判定为他人**（is_user=False）的轮次。
    - `strict` ：必须 is_user=True。

    `is_user=None` 表示**未判定**（声纹引擎不可用、音频太短、没匹配上任何锚点），
    这不是「是别人」。实测短促对话段上这个状态很常见，若在 strict 下把它当拒绝，
    本人会大量「喊了没反应」——比不门控更糟。所以 strict 也是显式只认 True，
    而不是「非 False 即通过」的反面。
    """
    if gate == "strict":
        return is_user is True
    return is_user is not False


def _get_turn(turn_id: str, tid: str) -> dict:
    """取 turn 并做三重校验：存在 / 未过期 / 租户匹配。

    租户校验不能省：turn_id 虽然是 16 位随机串，但一旦泄露，
    跨租户读到别人的对话内容就是数据泄漏（OWASP 多租户基线）。
    """
    with _lock:
        t = _turns.get(turn_id)
    if t is None or t["expires_at"] < time.time():
        raise HTTPException(status_code=404, detail="turn expired or not found")
    if t["tenant"] != tid:
        raise HTTPException(status_code=403, detail="turn belongs to another tenant")
    return t


# --------------------------------------------------------------------------
# 音频校验
# --------------------------------------------------------------------------
def _probe_wav(data: bytes) -> tuple[bool, str, float]:
    """校验并读出时长。返回 (ok, 错误说明, 秒数)。

    规格要求 16kHz 单声道：ASR / 声纹 / TTS 全链路都是这个格式，
    放一个 44.1kHz 立体声进来会在 sherpa-onnx 内部被静默降级成噪声，
    与其下游出怪结果，不如在这里明确拒绝。
    """
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
            width = w.getsampwidth()
    except Exception as e:
        return False, f"not a valid wav: {e}", 0.0
    if ch != 1 or rate != 16000 or width != 2:
        return False, (f"must be 16kHz mono 16bit wav, got {rate}Hz {ch}ch {width * 8}bit"), 0.0
    return True, "", n / float(rate)


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------
def _tenant_of(x_tenant: str, x_install_id: str = "") -> str:
    """解析租户，**带上 install 才与 /start 走同一条路由逻辑**。

    不加这一层会出现只在「设备档案的租户 ≠ 客户端自报租户」时复现的 404：
    `/start` 按档案算出 tid=A 建了 turn，`/audio` 只按 header 算出 tid=B，
    `_get_turn` 在两套 tid 下查不到 → 客户端表现为「对话开不起来」，
    而日志里只有一行 404，极难定位。这正是管理员在服务端改了设备归属后
    必然触发的场景，所以必须在读路径上也解析 install。

    ⚠️ 本函数必须留在 `@router.post("/start")` **上方**：装饰器绑的是紧跟其后
    的那个函数，插在中间会让装饰器改绑到本函数（实测报 422 query x_tenant）。
    """
    install = urllib.parse.unquote((x_install_id or "").strip())[:64]
    return _resolve_tenant("", x_tenant, install)


@router.post("/start")
def talk_start(payload: dict = Body(default_factory=dict),
               authorization: str | None = Header(default=None),
               x_tenant: str = Header(default="")) -> dict:
    """开一轮对话。

    客户端拿到 turn_id 后立刻开始录音，录满就 PUT /audio/{turn_id}。
    TTL 内没上传就作废（避免手机崩溃后留一堆僵尸 turn）。
    """
    _check_auth(authorization)
    # install_id（2026-09-15）：客户端安装实例标识 → 定位**设备档案**，
    # 决定这台设备用哪个管家角色、允不允许调本机 Hermes、数据落哪个租户。
    # 旧客户端不带 → profile=None，走原有的全局行为，完全兼容。
    install = urllib.parse.unquote(str(payload.get("install_id", "") or ""))[:64]
    tid = _resolve_tenant(str(payload.get("tenant", "")), x_tenant, install)
    device = urllib.parse.unquote(str(payload.get("device_id", "") or ""))[:40]
    session_id = str(payload.get("session_id", "") or "")
    prof = profiles.get_profile(install) if install else None
    bot = profiles.resolve_bot(prof.bot if prof else profiles.BOT_DEFAULT)
    hermes_ok = profiles.hermes_allowed(prof)
    # 客户端可以传自己的唤醒词表（规格 §5.1）；传了就以它为准，
    # 没传或传空则用服务端配置的默认表。
    extra = [str(x).strip() for x in (payload.get("wake_word_aliases") or []) if str(x).strip()]

    _gc()

    limit = config.TALK_MAX_TURNS_PER_DEVICE
    if device and _live_turns_of_device(device) >= limit:
        raise HTTPException(status_code=429,
                            detail=f"too many concurrent turns for device (max {limit})")

    turn_id = secrets.token_hex(8)
    now = time.time()
    with _lock:
        _turns[turn_id] = {
            "tenant": tid,
            "device": device,
            "session_id": session_id,
            "created_at": now,
            "expires_at": now + config.TALK_TTL_SEC,
            "status": "open",
            "extra_aliases": extra,
            # 管家角色与 Hermes 开关（2026-09-15）。**在 start 时就定下来**，
            # 而不是每次 audio 现查：一轮对话中途管理员改了设置，不应该让
            # 这一轮的回答换人格（用户会听到前后不一致的语气）。
            "install": install,
            "bot": bot.id,
            "bot_persona": bot.persona,
            "bot_base_url": bot.base_url,
            "hermes_allowed": hermes_ok,
            "asr_text": "",
            "is_user": None,
            "speaker": "",
            "reply_text": "",
            "reply_short": "",
            "call_id": "",
            "via": "",
            "wav": None,
            "reply_wav": None,
        }

    # 对话记录先落一条占位（见 store.create_talk_message 注释）
    try:
        date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        store.create_talk_message(turn_id, date, now, device, session_id, tid)
    except Exception as e:
        log.warning("talk 记录落库失败（不影响对话）：%s", e)

    hermes_on = hermes_ok and hermes_client.available(base_url=bot.base_url)
    return {
        "turn_id": turn_id,
        "expires_at_ms": int((now + config.TALK_TTL_SEC) * 1000),
        "ttl_seconds": config.TALK_TTL_SEC,
        # 回传角色与门控结果，让客户端 UI 能显示「本机管家 / 通用助手」，
        # 而不是让用户对着两种完全不同的行为猜原因。
        "tenant": tid,
        "bot": bot.id,
        "bot_name": bot.name,
        "hermes_enabled": hermes_on,
        "hermes_allowed": hermes_ok,
        "fallback": None if hermes_on else ("hermes_off" if not hermes_ok else "ai_only"),
    }


@router.put("/audio/{turn_id}")
async def talk_audio(turn_id: str, request: Request,
                     authorization: str | None = Header(default=None),
                     x_tenant: str = Header(default=""),
                     x_device_name: str = Header(default=""),
                     x_speech_ratio: str = Header(default=""),
                     x_has_alias: str = Header(default=""),
                     x_talk_scope: str = Header(default=""),
                     x_install_id: str = Header(default="")) -> dict:
    """上传一段对话音频，同步走完 ASR → Hermes → TTS，直接带回回复。

    规格 §5.2 允许 hermes.status=pending + 客户端轮询。本实现**始终同步**：
    实测 Hermes（MiniMax-M3）耗时 0.6~1.5s，加上 ASR 约 2s，用户完全可接受；
    同步路径少一次往返、少一套轮询状态机，端到端更快也更好排查。
    `/reply/{turn_id}` 仍然实现，供客户端在超时后补拉。
    """
    _check_auth(authorization)
    tid = _tenant_of(x_tenant, x_install_id)
    turn = _get_turn(turn_id, tid)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty audio body")
    if len(body) > config.TALK_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"audio too large ({len(body)} > {config.TALK_MAX_BYTES} bytes)")

    ok, why, seconds = _probe_wav(body)
    if not ok:
        raise HTTPException(status_code=400, detail=f"bad audio: {why}")
    if seconds > config.TALK_MAX_SEC:
        # 先落一条记录再拒：被拒的音频是「用户确实说了话、我们没接住」的证据。
        # 旧实现只 raise，日志里只剩一行 access log（看不出来龙去脉），用户侧
        # 表现为「说了没反应」，复盘时两头都查不到——实测就这样丢过 24 轮。
        log.warning("talk 音频超限被拒：%.1fs > %.0fs（客户端上限必须 ≤ 服务端），turn=%s",
                    seconds, config.TALK_MAX_SEC, turn_id)
        turn["status"] = "rejected"
        turn["via"] = "server"
        _save_talk(turn_id, "", None, "", tid, status="rejected", via="server")
        raise HTTPException(
            status_code=400,
            detail=f"audio too long ({seconds:.1f}s > {config.TALK_MAX_SEC}s)")

    device = urllib.parse.unquote(x_device_name or turn["device"] or "")[:40]
    t0 = time.time()

    # ---- 落盘（放线程池：磁盘 IO 不该占着事件循环） ----
    date = datetime.fromtimestamp(turn["created_at"]).strftime("%Y-%m-%d")
    wav_path = config.talk_dir() / date / f"{turn_id}.wav"

    def _persist() -> None:
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(body)

    await run_in_threadpool(_persist)
    turn["wav"] = str(wav_path)

    # ---- ASR（incremental 后端：对话要快，不等日终的 Fun-ASR） ----
    try:
        asr_text = await run_in_threadpool(asr.transcribe, wav_path, "incremental")
    except Exception as e:
        log.warning("talk ASR 失败：%s", e)
        asr_text = ""
    asr_ms = int((time.time() - t0) * 1000)

    if not asr_text.strip():
        # 空转写不是错误：可能是纯静音段（唤醒词误触发）。如实返回，
        # 让客户端决定要不要提示用户，不要伪装成一次成功对话。
        turn["status"] = "empty"
        _save_talk(turn_id, "", None, "", tid, status="empty")
        return {
            "turn_id": turn_id,
            "asr_text": "",
            "is_user": None,
            "speaker_label": "",
            "asr_latency_ms": asr_ms,
            "hermes": {"status": "skipped", "reason": "empty_transcript"},
        }

    # ---- 说话人判定（失败不影响对话，只影响 is_user 标注） ----
    try:
        is_user, speaker = await run_in_threadpool(voiceprint.identify, wav_path, tid)
    except Exception as e:
        log.warning("talk 声纹判定失败：%s", e)
        is_user, speaker = None, ""
    turn["asr_text"] = asr_text
    turn["is_user"] = is_user
    turn["speaker"] = speaker
    _save_talk(turn_id, asr_text, is_user, speaker, tid)

    # ---- 唤醒词：从「硬门」降级为「线索」（2026-09-11 重新规划）----
    #
    # 旧实现在这里就把不含唤醒词的轮次丢掉（status=no_alias）。实测 930 轮里
    # 753 轮（81%）走了这条路——用户明明说了话，Hermes 从不知道它存在。
    #
    # 用户的要求是「只要包含语意的文字就要给 hermes 去处理」：判据不该是关键词，
    # 该是内容。所以这里只**计算**命中与否，作为一条线索随请求带给 Hermes
    # （它自己有一张更全的变体表），要不要应答由 Hermes 按内容决定：
    # 需要处理的回复并播报，不需要的落库记录（status=noted）。
    #
    # `DH_TALK_FORCE_HERMES=0` 可回到旧行为，仅供排障——那等于扔掉 8 成语音。
    #
    # ---- 唤醒词命中**只认 ASR 文本**（2026-09-14 用户硬要求）----
    #
    # 客户端的 `X-Has-Alias` 曾与文本命中**取或**：只要本地 KWS 说「有」就算有。
    # 但本地 KWS 会误触发（相似音、远场、环境声），一旦它误报，一条**识别文本里
    # 根本没有唤醒词**的轮次就被放进了 Hermes；Hermes 又按 `has_alias=True`
    # 走「命中唤醒词」那条宽松通路 → 凭空播一句回复打断用户。
    #
    # 取证（2026-09-14）：163 条真正回复过的轮次里，**49 条（30.1%）文本里不含
    # 任何唤醒词**，例如「诶，群在里面是这个群吗？」「局域网为啥不用呢？」
    # 「哦可以选择播放的内容。」。用户原话：「我都没说大锅/四米之类的文字，
    # 怎么能错误识别并跟我反馈呢？」
    #
    # 现在**文本优先**：客户端线索不再参与判定，只用来对比、把本地 KWS 的误报
    # 打进日志。两边变体表必须对齐，见 config.TALK_ALIAS_GROUPS 的约定。
    text_hit = config.has_talk_alias(asr_text, turn["extra_aliases"])
    if x_has_alias.strip() == "1" and not text_hit:
        log.info("talk 本地 KWS 报命中但文本无唤醒词，按未命中处理：%r", asr_text[:40])
    hit = text_hit

    # ---- 传输范围：由客户端在 APK 上选择（2026-09-12 用户要求）----
    #
    # 用户要能自己决定「只传命中唤醒词的」还是「全部都传」，两种都保留为一个开关：
    #   keyword —— 旧行为。不命中唤醒词的轮次落 no_alias 直接丢，Hermes 根本看不到。
    #              绝对不打扰，代价是漏听（实测曾因此丢掉 81% 的语音）。
    #   all     —— 默认。有文本就交给 Hermes 按**内容**判定要不要应答，
    #              不需要应答的落 noted 且**不调模型**（成本闸门在 Hermes 侧）。
    #
    # header 缺省 / 取值非法时**沿用服务端全局配置**，保证老客户端行为完全不变。
    scope = (x_talk_scope or "").strip().lower()
    allow_all = config.TALK_FORCE_HERMES
    if scope == "all":
        allow_all = True
    elif scope == "keyword":
        allow_all = False
    if not hit and not allow_all:
        log.info("talk 未命中唤醒词且 scope=%s，落 no_alias（客户端选了关键词模式）",
                 scope or "(默认)")
        turn["status"] = "no_alias"
        turn["via"] = "none"
        _save_talk(turn_id, asr_text, is_user, speaker, tid, status="no_alias", via="none")
        return {
            "turn_id": turn_id,
            "asr_text": asr_text,
            "is_user": is_user,
            "speaker_label": speaker,
            "asr_latency_ms": asr_ms,
            "hermes": {"status": "skipped", "reason": "no_alias"},
        }

    # ---- 近重复抑制（2026-09-10）----
    # **排在自激熔断之前**（顺序是刻意的）：熔断的触发条件是「连续 N 轮相似」，
    # 如果让被本闸挡掉的轮次也进熔断的计数，一次「同一句话送来两遍」就会
    # 在熔断里累进两份——实测就是这么把 2 轮真实对话放大成 60 秒全静默的
    # （19:02:36 的日志）。本闸是精确的**相邻两轮**判据，该由它先做决定；
    # 熔断只处理本闸管不到的模式（例如相似度漂移在多轮之间累积）。
    #
    # 也不排在门控之前还是之后的问题：门控问的是「这个人该不该答」，
    # 与本闸无关，而本闸不消耗 Hermes 调用，排前面能多省一次。
    if _dup_note(tid, turn["session_id"], asr_text):
        turn["status"] = "dup"
        turn["via"] = "dup"
        _save_talk(turn_id, asr_text, is_user, speaker, tid, status="dup", via="dup")
        return {
            "turn_id": turn_id,
            "asr_text": asr_text,
            "is_user": is_user,
            "speaker_label": speaker,
            "asr_latency_ms": asr_ms,
            "hermes": {"status": "dup", "reason": "near_duplicate"},
        }

    # ---- 回声自激熔断：命中唤醒词后再过一道，防止外放声被录回形成无限环 ----
    # 放在 has_talk_alias 之后而不是之前：只有「确实命中唤醒词」的轮次才有
    # 自激嫌疑，不含唤醒词的正常语料不必进相似度比较（省一次 difflib）。
    if _loop_blocked(tid) or _loop_note(tid, asr_text):
        turn["status"] = "loop_guard"
        turn["via"] = "loop_guard"
        _save_talk(turn_id, asr_text, is_user, speaker, tid,
                   status="loop_guard", via="loop_guard")
        return {
            "turn_id": turn_id,
            "asr_text": asr_text,
            "is_user": is_user,
            "speaker_label": speaker,
            "asr_latency_ms": asr_ms,
            "hermes": {"status": "loop_guard", "reason": "loop_guard"},
        }

    # ---- 声纹角色门控（2026-09-10）----
    # 「只有识别出来的角色，才能触发属于自己的管家」。放在自激熔断之后、调
    # Hermes 之前：熔断管的是「这轮不该答」，门控管的是「这个人不该答」，
    # 两者互不相关，但都在浪费一次 Hermes 调用之前拦下最划算。
    gate = config.TALK_OWNER_GATE
    if gate != "off" and not _owner_allowed(is_user, gate):
        turn["status"] = "not_owner"
        turn["via"] = "gate"
        _save_talk(turn_id, asr_text, is_user, speaker, tid,
                   status="not_owner", via="gate")
        log.info("talk 角色门控拦截：speaker=%r is_user=%s gate=%s text=%r",
                 speaker, is_user, gate, asr_text[:60])
        return {
            "turn_id": turn_id,
            "asr_text": asr_text,
            "is_user": is_user,
            "speaker_label": speaker,
            "asr_latency_ms": asr_ms,
            "hermes": {"status": "not_owner", "reason": "not_owner"},
        }

    # ---- Hermes → 纯 AI 逐级降级 ----
    call_id = "h-" + secrets.token_hex(4)
    turn["call_id"] = call_id
    h0 = time.time()
    # 管家角色与门控在 /start 时就固定在 turn 上（见 talk_start 注释）。
    # 旧客户端不带 install_id → turn 上是默认管家 + 允许，行为与从前一致。
    turn_bot = turn.get("bot") or profiles.BOT_DEFAULT
    # 会话历史只喂给**降级路径**（见 _session_history 注释）；
    # Hermes 侧自己按 session_id 管上下文，我们不多发一遍。
    history = _session_history(tid, turn["session_id"], turn_bot)
    reply_text, reply_short, via, hermes_meta = await _ask_hermes(
        asr_text, config.talk_aliases() + turn["extra_aliases"],
        tid, turn["session_id"], device, call_id, history, has_alias=hit,
        bot=turn_bot,
        persona=turn.get("bot_persona", ""),
        base_url=turn.get("bot_base_url", ""),
        allowed=turn.get("hermes_allowed", True))
    hermes_ms = int((time.time() - h0) * 1000)

    # ---- Hermes 判定「不需要应答」的收尾（2026-09-11 重新规划）----
    #
    # 有语意的文本一律已经送到 Hermes，由它按内容判断是不是在跟它说话。
    # 判为「只是记录」时它回 intent_type=note_only + 空 reply：
    # 那是**分流生效**，不是故障——用户在跟别人讲话，或只是自言自语，
    # 管家不该插嘴。落成 noted 档：文字进库可查，但不合成 TTS、不打扰用户。
    #
    # 这正是用户定下的规则「需要处理的进行回复，不需要处理的进行记录」。
    # 旧实现在这种轮次上连记录都没有（直接 no_alias 丢弃，占全部轮次的 81%），
    # 于是「用户说了什么」在系统里彻底不存在——那是比不回复更严重的信息损失。
    if not reply_text and hermes_meta.get("intent_type") == "note_only":
        turn["status"] = "noted"
        turn["via"] = "note"
        _save_talk(turn_id, asr_text, is_user, speaker, tid, status="noted", via="note")
        log.info("talk 判为仅记录（Hermes note_only）：reason=%s text=%r",
                 hermes_meta.get("note_reason", ""), asr_text[:60])
        _note_to_corpus(asr_text, wav_path, tid, date)
        return {
            "turn_id": turn_id,
            "asr_text": asr_text,
            "is_user": is_user,
            "speaker_label": speaker,
            "asr_latency_ms": asr_ms,
            "total_latency_ms": int((time.time() - t0) * 1000),
            "hermes": {"status": "noted", "reason": "not_conversation",
                       "via": "hermes", "intent_type": "note_only"},
        }

    # ---- Hermes 侧回声去重命中的收尾（2026-09-10）----
    # Hermes 对「同 device 10s / 同 session 30s 内文本完全相同」的请求回
    # `intent_type=voice_echo` + 空 reply。那是**防护生效**，不是故障：
    # 原先走到下面就被判成 failed，客户端弹「管家没有给出回复」，
    # 用户看到的是「坏了」，实际是我们替他把一次重复播报挡掉了。
    # 归到 dup 一档，客户端按 Skipped 展示。
    if not reply_text and hermes_meta.get("intent_type") == "voice_echo":
        turn["status"] = "dup"
        turn["via"] = "dup"
        _save_talk(turn_id, asr_text, is_user, speaker, tid, status="dup", via="dup")
        log.info("talk 回声去重命中（Hermes voice_echo）：text=%r", asr_text[:60])
        return {
            "turn_id": turn_id,
            "asr_text": asr_text,
            "is_user": is_user,
            "speaker_label": speaker,
            "asr_latency_ms": asr_ms,
            "total_latency_ms": int((time.time() - t0) * 1000),
            "hermes": {"status": "dup", "reason": "echo_dedup",
                       "via": "hermes", "intent_type": "voice_echo"},
        }

    # 洗一遍再进 TTS 与入库：宁可这轮没播报，也不能把上下文统计念给用户听。
    # 洗空则下面照常走 failed 分支——「没回」比「念一堆乱码」体验好。
    reply_text = _sanitize_reply(reply_text, asr_text)
    if reply_text:
        reply_short = _sanitize_reply(reply_short, asr_text) or reply_text

    # ---- TTS ----
    reply_wav = None
    if reply_text:
        reply_wav = config.talk_dir() / date / f"{turn_id}-reply.wav"
        try:
            # fast=True：对话要低延迟，跳过 Ming 的 3~4 秒子进程加载，走系统 say。
            # 日报播报（/api/tts/preview）才用 Ming 的高音质。
            got = await run_in_threadpool(tts.synthesize, reply_text, reply_wav, True)
            if got is None:
                reply_wav = None
        except Exception as e:
            log.warning("talk TTS 失败：%s", e)
            reply_wav = None
    turn["reply_wav"] = str(reply_wav) if reply_wav else None
    turn["reply_text"] = reply_text
    turn["reply_short"] = reply_short
    turn["via"] = via
    turn["status"] = "done" if reply_text else "failed"

    total_ms = int((time.time() - t0) * 1000)
    hermes_field: dict = {
        "status": "done" if reply_text else "failed",
        "call_id": call_id,
        "started_ms": int(h0 * 1000),
        "finished_ms": int(time.time() * 1000),
        "reply_text": reply_text,
        "reply_short": reply_short,
        "via": via,
        "latency_ms": hermes_ms,
    }
    if hermes_meta.get("intent_type"):
        hermes_field["intent_type"] = hermes_meta["intent_type"]
    if "ha_used" in hermes_meta:
        hermes_field["ha_used"] = hermes_meta["ha_used"]
    if hermes_meta.get("model"):
        hermes_field["model"] = hermes_meta["model"]
    # ★ 2026-09-11：把确定性设备控制的执行结果透传给客户端。
    #   三端因此能显示"哪个设备、做了什么、状态被改成什么"——这是多端信息同步的
    #   最小单元：用户听到 TTS 说"继电器1 已开"的同时，界面上看到的是同一份事实，
    #   而不是各自猜。`path=deterministic` 让复盘能区分"真执行了"和"只是回答了一句"。
    for _k in ("device", "action", "state_before", "state_after", "verified", "path"):
        if hermes_meta.get(_k) is not None:
            hermes_field[_k] = hermes_meta[_k]
    if reply_wav:
        hermes_field["reply_audio_url"] = f"/api/talk/audio/{turn_id}/reply.wav"

    _save_talk(turn_id, asr_text, is_user, speaker, tid,
               status=hermes_field["status"], reply_text=reply_text,
               call_id=call_id, latency_ms=total_ms, via=via)

    # 记入会话历史：只记完整问答，半截的比没有更坏（见 _session_append）
    if reply_text:
        _session_append(tid, turn["session_id"], asr_text, reply_text, turn_bot)

    # ------------------------------------------------------------------
    # 一行式对话留痕（2026-09-10）
    #
    # 用户要求「日志窗口能看到 hermes 做了什么」。原先 talk 流程只写
    # uvicorn.access 的 HTTP 行（`POST /api/talk/audio 200`），看不到任何内容，
    # 排查时只能去翻数据库。这里把一轮对话的关键信息压成一行：
    #   谁说的 → 说了什么 → 管家怎么回的 → 走的哪条链路 → 三段耗时
    #
    # 用单行而不是多行：日志窗口是 tail 展示，多行会把有效信息稀释掉。
    # 文本用 %r 而非 %s——转写里常有换行/引号，repr 能一眼看出边界。
    # ------------------------------------------------------------------
    log.info(
        "对话 %s [%s] 说 %r → 回 %r (via=%s bot=%s model=%s) asr=%dms hermes=%dms total=%dms",
        turn_id,
        speaker or ("本人" if is_user else ("他人" if is_user is False else "未判定")),
        asr_text[:80], reply_text[:80],
        via, turn_bot,
        hermes_meta.get("model") or hermes_meta.get("intent_type") or "-",
        asr_ms, hermes_ms, total_ms,
    )

    return {
        "turn_id": turn_id,
        "asr_text": asr_text,
        "is_user": is_user,
        "speaker_label": speaker,
        "asr_latency_ms": asr_ms,
        "total_latency_ms": total_ms,
        "hermes": hermes_field,
    }


async def _ask_hermes(text: str, aliases: list[str], tid: str, session_id: str,
                      device: str, call_id: str,
                      history: list[tuple[str, str]] | None = None,
                      has_alias: bool = False,
                      bot: str = "", persona: str = "", base_url: str = "",
                      allowed: bool = True
                      ) -> tuple[str, str, str, dict]:
    """调 Hermes，失败降级纯 AI。返回 (reply_text, reply_short, via, meta)。

    via 取值：
      `hermes`   —— 走了管家（含 HA 实时数据）
      `ai_only`  —— Hermes 不可用，用本地配置的模型直答
      `failed`   —— 两级都拿不到回复

    `allowed=False`（设备档案里关掉了「允许本机 Hermes」，2026-09-15）时
    **直接跳过管家**，不试探、不超时等待，直接走通用助手。这不是故障，
    是管理员的有意设置：外来设备不该读到门店实时数据、也不该能控设备。
    调用方从 `via=ai_only` 与日志能区分这两种情况。

    `has_alias` 是本侧算出的唤醒词命中结果，作为**线索**随请求带给 Hermes
    （它不决定要不要送，只帮 Hermes 判断「是不是在跟我说话」，见 talk_audio
    里「唤醒词从硬门降级为线索」那段）。两边只要有一边认为在叫它，就算在叫它。

    `bot` / `persona` / `base_url` 是管家角色（2026-09-15）：同一台 Hermes
    里的不同角色，或一个完全独立的 Hermes 实例（base_url 非空时）。
    """
    if not allowed:
        log.info("设备档案已关闭本机 Hermes，本轮直接用通用助手（bot=%s）", bot or "-")
    else:
        # ★ 注入本机 HA 事实（2026-09-11）：设备概览 + 与本轮文本相关的设备。
        #   此前写死 ha_context=None，于是每一轮 Hermes 都要自己全量查一次 HA
        #   （门店 6000+ 实体），且服务端对"门店现在有什么"完全无知。
        #   注入失败不影响对话 —— 拿不到就给 None，Hermes 会自己兜底查询。
        try:
            ha_ctx = await run_in_threadpool(ha_client.context_for, text)
        except Exception as e:  # noqa: BLE001
            log.warning("构造 ha_context 失败，本轮交回 Hermes 自行查询 HA：%s", e)
            ha_ctx = None

        if hermes_client.available(base_url=base_url):
            res = await run_in_threadpool(
                hermes_client.invoke,
                text,
                user_aliases=aliases, tenant=tid, session_id=session_id,
                device=device, call_id=call_id, ha_context=ha_ctx,
                bot=bot, persona=persona, base_url=base_url,
            )
            if res:
                if res.get("skip"):
                    # 保留完整的分流信息：intent_type 区分「只记录」（note_only）与
                    # 「回声去重」（voice_echo），note_reason 说明判为记录的依据
                    # （too_short / not_addressed）——复盘时要看得到为什么没回复。
                    return "", "", "hermes", {
                        "intent_type": res.get("intent_type", ""),
                        "reason": res.get("reason", ""),
                        "note_reason": res.get("note_reason", ""),
                    }
                # 出口脱敏：Hermes 回复里可能带出 key / token（规格 §3.5）
                reply, _ = redact(res.get("reply_text", "") or "")
                short, _ = redact(res.get("reply_short", "") or reply)
                if reply:
                    return reply, short, "hermes", res

    # ---- 降级：本地/云端 LLM 直答（带上会话历史，接得住「那第二个呢」） ----
    log.info("Hermes 未启用或不可达，talk 降级为纯 AI 回答（bot=%s 历史 %d 条）",
             bot or "-", len(history or ()))
    reply = await run_in_threadpool(llm.quick_answer, text, "", history)
    if reply:
        return reply, reply, "ai_only", {}
    return "", "", "failed", {}


def _reply_speaker(speaker: str) -> str:
    """管家的角色名。属于唤醒它的那个角色，如「大锅」→「大锅的hermes」。

    没有识别出角色时退回中性的「hermes」：绝不能编一个假角色名塞进去，
    否则回看记录时会以为是某个真人的管家在答。
    """
    s = (speaker or "").strip()
    return f"{s}的hermes" if s else "hermes"


# 出现这些片段说明模型把「注入给它的上下文/提示词模板」当成回复吐回来了。
# 全部取自已观测到的真实污染样本（2026-09-10 17:33:57），不是凭空假设的词。
_REPLY_LEAK_MARKERS = (
    "[SYMI",            # 注入的 HA 上下文标题
    "HA 总实体",         # 上面那段里的统计行
    "实际设备（排除",     # 同上
)

# Hermes 侧拼 prompt 的模板外壳：「我听到了您说「<用户的原文>」…」。
#
# 为什么要单独剥它而不是丢进 _REPLY_LEAK_MARKERS：**它出现在开头**，
# 用「砍掉标记及之后」的规则会连后面可能存在的正经回复一起砍掉。
#
# 收尾引号写成可选：实测污染样本里这个引号恰好被截掉了（`…南京六合。` 后面
# 直接换行），必选就匹配不上。字符类排除换行，避免贪婪吃穿到下一段正文。
# 用命名组把引号里的内容抓出来，交给 _sanitize_reply 判「是不是本轮转写」。
_REPLY_ECHO_RE = re.compile(
    r"^我(?:听到了|听)您?说\s*[「『\"“](?P<said>[^」』\"”\n]{0,80})[」』\"”]?\s*"
)

# 「引号里就是本轮转写」的判定阈值。取 0.7 而不是 1.0：ASR 每次结果不逐字相同
# （实测同一句里「大锅」/「大哥」会互换），要求完全一致会漏掉绝大多数真回显。
_REPLY_ECHO_SIM = 0.7


def _note_to_corpus(asr_text: str, wav_path: Path, tid: str, date: str) -> None:
    """「仅记录」的轮次命中关键词时，把这轮语料沉淀到 GPT-SoVITS 语料目录。

    写在**独立文件** `list.talk.txt` 而不是 `list.wav.txt`：pipeline 的
    `_export_corpus` 每轮都会**全量重建** `list.wav.txt`（只保留它自己归档过的段），
    对话轮次写进去会在下一次处理时被整段抹掉。独立文件格式与它完全一致
    （`wavs/<name>|<speaker>|zh|<text>`），要合并时 `cat list.talk.txt >>
    list.wav.txt` 即可。

    这条链路服务的是「客户咨询、报价、安装、故障」这类句子——它们几乎不含
    唤醒词（客户不会喊「大锅」），却恰恰是最该积累的真实口音素材。
    词表见 config.TALK_NOTE_KEYWORDS。
    """
    kws = config.TALK_NOTE_KEYWORDS
    if not kws:
        return
    hit = [k for k in kws if k in asr_text]
    if not hit:
        return
    try:
        out = tenants.resolve(tid).corpus_dir / date
        (out / "wavs").mkdir(parents=True, exist_ok=True)
        name = wav_path.name
        dst = out / "wavs" / name
        if not dst.exists() and wav_path.exists():
            shutil.copy2(wav_path, dst)
        line = f"wavs/{name}|user|zh|{asr_text}"
        list_file = out / "list.talk.txt"
        existing = set()
        if list_file.exists():
            existing = set(list_file.read_text(encoding="utf-8").splitlines())
        if line not in existing:
            with list_file.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        log.info("talk 语料沉淀：命中 %s，写入 corpus/%s/list.talk.txt", hit, date)
    except Exception as e:  # noqa: BLE001
        # 语料沉淀是**增值动作**，失败绝不能影响对话本身
        log.warning("talk 语料沉淀失败（不影响本轮记录）：%s", e)


def _sanitize_reply(reply: str, asr_text: str) -> str:
    """洗掉「把提示词吐回来」的回复，返回可播报文本（可能为空串）。

    ## 为什么要在数字人侧做这道防御

    实测 Hermes 偶发把构造好的 prompt 整段当回复返回：

        我听到了您说「请大锅帮我看一下，今天天气怎么样，南京六合。
        [SYMI 门店 HA 实时数据]
        HA 总实体: 6099 … 离线/未知: 3850 …

    这条 213 字，TTS 会**一字不落地念出来**。同一句话 30 秒后重问就是正常的
    （「大锅，HA里没接入天气实体，查不了六合天气」），所以是偶发而非配置错。

    根因在 Hermes 侧（模板没剥离 / 模型复述了 prompt），但**受害的是用户耳朵**，
    而我们是最后一环。在最后一环兜底比等上游修更快，也不会互相甩锅。

    三步，顺序不能换：
    ① 剥模板外壳 —— 它在开头，用「砍标记」的规则会误伤后面的正文；
    ② 砍注入上下文 —— 标记及其之后全是内部数据，对用户毫无意义；
    ③ 超长截断 —— 仍然超过 TTS_MAX_CHARS 就按句末切，至少让人听懂大意。
    """
    text = (reply or "").strip()
    if not text:
        return ""

    # ① 模板外壳。**必须确认引号里就是本轮转写**才算回显——
    #    否则用户真问「我听到了您说「开灯」是什么意思」会被误伤成「是什么意思」。
    #    用相似度而非全等：ASR 同句不同次会「大锅」/「大哥」互换（实测）。
    m = _REPLY_ECHO_RE.match(text)
    if m:
        said = _norm_for_loop(m.group("said"))
        heard = _norm_for_loop(asr_text)
        sim = (difflib.SequenceMatcher(None, said, heard).ratio()
               if len(said) >= 4 and heard else 0.0)
        if sim >= _REPLY_ECHO_SIM:
            stripped = text[m.end():].lstrip("，。！？、 \n\t")
            log.warning("talk 回复含模板外壳（相似度 %.2f），已剥离：%d 字 → 余 %d 字",
                        sim, len(text), len(stripped))
            text = stripped
            if not text:
                return ""

    # ② 注入上下文标记
    for marker in _REPLY_LEAK_MARKERS:
        idx = text.find(marker)
        if idx == 0:
            # 整条都是上下文，没有可用的人话
            log.warning("talk 回复整条是注入上下文（%r 开头），丢弃", marker)
            return ""
        if idx > 0:
            log.warning("talk 回复疑似含注入上下文，已截断到标记 %r 之前", marker)
            text = text[:idx].strip()
            break

    if not text:
        return ""

    # ③ 长度上限
    limit = max(20, config.TTS_MAX_CHARS)
    if len(text) > limit:
        cut = text[:limit]
        # 尽量断在句末标点，避免念到一半被硬切
        for p in ("。", "！", "？", "；", "\n"):
            i = cut.rfind(p)
            if i >= limit // 2:
                cut = cut[: i + 1]
                break
        log.warning("talk 回复超长（%d > %d 字），已截断", len(text), limit)
        text = cut

    return text.strip()


def _save_talk(turn_id: str, asr_text: str, is_user: bool | None, speaker: str,
               tenant: str, *, status: str = "", reply_text: str = "",
               call_id: str = "", latency_ms: int = 0, via: str = "") -> None:
    """把对话结果写库。**任何失败都只记日志**——记录是辅助，不能反过来弄挂对话。"""
    try:
        store.update_talk_asr(turn_id, asr_text, is_user, speaker, tenant)
        if status:
            store.update_talk_reply(turn_id, status, reply_text, call_id,
                                    latency_ms, via, _reply_speaker(speaker), tenant)
    except Exception as e:
        log.warning("talk 记录写入失败：%s", e)


@router.get("/reply/{turn_id}")
def talk_reply(turn_id: str,
               authorization: str | None = Header(default=None),
               x_tenant: str = Header(default=""),
               x_install_id: str = Header(default="")) -> dict:
    """轮询用：返回与 /audio 相同的 hermes 字段。

    同步路径已把结果写在响应里，本端点用于客户端超时后补拉
    （规格 §5.3：客户端 200ms 重试，最多 30 次 = 6 秒）。
    """
    _check_auth(authorization)
    tid = _tenant_of(x_tenant, x_install_id)
    turn = _get_turn(turn_id, tid)
    reply_text = turn.get("reply_text", "")
    return {
        "turn_id": turn_id,
        "asr_text": turn.get("asr_text", ""),
        "is_user": turn.get("is_user"),
        "speaker_label": turn.get("speaker", ""),
        "hermes": {
            "status": "done" if reply_text else (turn.get("status") or "pending"),
            "call_id": turn.get("call_id", ""),
            "reply_text": reply_text,
            "reply_short": turn.get("reply_short", ""),
            "via": turn.get("via", ""),
            "reply_audio_url": (f"/api/talk/audio/{turn_id}/reply.wav"
                                if turn.get("reply_wav") else None),
        },
    }


@router.get("/audio/{turn_id}/reply.wav")
def talk_reply_audio(turn_id: str,
                     authorization: str | None = Header(default=None),
                     x_tenant: str = Header(default=""),
                     x_install_id: str = Header(default="")):
    """下载 TTS 音频。"""
    _check_auth(authorization)
    tid = _tenant_of(x_tenant, x_install_id)
    turn = _get_turn(turn_id, tid)
    p = turn.get("reply_wav")
    if not p or not Path(p).is_file():
        raise HTTPException(status_code=404, detail="reply audio not ready")
    return FileResponse(p, media_type="audio/wav",
                        headers={"Cache-Control": "max-age=3600"})


@router.post("/end/{turn_id}")
async def talk_end(turn_id: str, payload: dict = Body(default_factory=dict),
                   authorization: str | None = Header(default=None),
                   x_tenant: str = Header(default=""),
                   x_install_id: str = Header(default="")) -> dict:
    """结束一轮：删音频，保留文字记录。

    同时把播放结果回传 Hermes（规格 §6.2），让管家那边能统计播报成功率。
    `played_ms` 由客户端在播完后测量传入；不传就记 0。
    """
    _check_auth(authorization)
    tid = _tenant_of(x_tenant, x_install_id)
    turn = _get_turn(turn_id, tid)

    played_ms = int(payload.get("played_ms", 0) or 0)
    if played_ms > 0:
        try:
            store.mark_talk_played(turn_id, played_ms, tid)
        except Exception as e:
            log.warning("talk played_ms 写入失败：%s", e)

    # 回传 Hermes（统计用途）。失败只记日志——播报已经发生了，
    # 不能因为统计接口挂了就让客户端认为「结束失败」。
    call_id = turn.get("call_id", "")
    if call_id and hermes_client.available():
        try:
            await run_in_threadpool(
                hermes_client.ack, call_id, turn_id, played_ms,
                str(payload.get("user_correction", "") or ""))
        except Exception as e:
            log.warning("Hermes ack 失败（不影响本轮）：%s", e)

    for key in ("wav", "reply_wav"):
        p = turn.get(key)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
            turn[key] = None
    turn["status"] = "completed"
    with _lock:
        _turns.pop(turn_id, None)
    return {"ok": True, "turn_id": turn_id}


@router.get("/health")
def talk_health(authorization: str | None = Header(default=None)) -> dict:
    """自检。**不校验租户**（这是服务级健康检查，不是租户数据）。"""
    _check_auth(authorization)
    return {
        "ok": True,
        "version": VERSION,
        "tts": tts.health(),
        "hermes": hermes_client.health(),
        "aliases": config.talk_aliases(),
        "active_turns": len(_turns),
        "max_turns_per_device": config.TALK_MAX_TURNS_PER_DEVICE,
        "ttl_seconds": config.TALK_TTL_SEC,
    }
