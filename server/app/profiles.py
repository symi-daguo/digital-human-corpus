"""设备档案与管家角色（bot）注册表（2026-09-15）。

## 这个模块解决什么

采集端（手机 / 机顶盒）上报数据时，服务端此前只能**相信客户端自报**的 `X-Tenant`：
任何一台设备把 header 改成别人的租户 id，数据就落进别人的空间。真实部署里
两台设备本来就是不同角色（老板的手机、门店的机顶盒），需要服务端**权威地**回答三个问题：

1. 这台设备属于**哪个租户**（数据落哪里）
2. 这台设备**能不能用本机 Hermes**（门店管家会控设备、读门店数据，
   访客设备只能用通用 AI）
3. 这台设备用**哪个管家角色（bot）**——多台设备可以共用一个默认 bot，
   也可以各自建专属 bot，互不干扰

## 存储

`data/profiles.json`（全局，与 `tenants.json` 同级）：

```json
{
  "bots": [
    {"id": "default", "name": "默认管家", "persona": "", "base_url": "", "hermes": true}
  ],
  "profiles": [
    {"install_key": "…", "label": "目标设备", "tenant": "default",
     "bot": "default", "hermes": true, "note": "",
     "first_seen": "2026-09-15T11:00:00", "updated_at": "2026-09-15T11:00:00"}
  ]
}
```

## 为什么放全局 JSON 而不是租户库

档案要回答的第一个问题就是「这台设备属于哪个租户」，它必须能在**知道租户之前**被读到
（上传请求只带 `install_key`）。放进租户库会变成先有鸡还是先有蛋。
与 `tenants.py` 同模式：文件写入只发生在管理操作时，请求路径上只读内存 dict。

## 与 tenants.py 的分工

| 模块 | 管什么 |
| --- | --- |
| `tenants.py` | 租户的**存在性**与目录（数据空间本体） |
| `profiles.py` | 设备 → 租户的**绑定**，以及设备 / 管家角色的行为开关 |

## 内存与硬盘约束（长期驻留）

- 内存：全部是内存 dict 查询，条目数有硬上限（`_MAX_PROFILES`），
  单条记录是几个短字符串，量级可忽略。
- 硬盘：单文件 JSON，每台设备约 200 字节，100 台设备约 20 KB。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, tenants

log = logging.getLogger(__name__)

# 内置管家角色：永远存在、不可删除。没配 bot 或配了不存在的 bot 都回落它。
BOT_DEFAULT = "default"

# 角色 id 同租户 id 规则：小写字母开头，防路径穿越与大小写混乱。
_BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
# 设备键：客户端 install_id 是 32 位 hex；旧客户端回落 "label:<owner_name>"（可能含中文）。
# 只排除控制字符与路径分隔符，长度设上限防滥用。
_KEY_RE = re.compile(r"^[^\x00-\x1f/\\]{1,64}$")
# display name / note 的长度上限（避免单个档案把 JSON 撑大）
_MAX_NAME = 40
_MAX_NOTE = 120
_MAX_PERSONA = 500
# 档案条数上限。自动登记只在上传路径发生，超限后不再新增（只记日志），
# 防止异常客户端刷爆注册表。
_MAX_PROFILES = 200
_MAX_BOTS = 50


@dataclass(frozen=True)
class Bot:
    """管家角色。`hermes` 是该角色**是否允许**走本机 Hermes 的默认值。"""

    id: str
    name: str
    persona: str = ""
    base_url: str = ""
    hermes: bool = True

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "persona": self.persona,
            "base_url": self.base_url,
            "hermes": self.hermes,
        }


@dataclass(frozen=True)
class Profile:
    """一台采集端设备的档案。"""

    install_key: str
    label: str
    tenant: str
    bot: str
    hermes: bool
    note: str = ""
    first_seen: str = ""
    updated_at: str = ""

    def public(self, *, bot_obj: "Bot | None" = None) -> dict:
        b = bot_obj or resolve_bot(self.bot)
        return {
            "install_key": self.install_key,
            "label": self.label,
            "tenant": self.tenant,
            "bot": b.id,
            "bot_name": b.name,
            "hermes": self.hermes,
            "hermes_effective": self.hermes and b.hermes,
            "note": self.note,
            "first_seen": self.first_seen,
            "updated_at": self.updated_at,
        }


# --------------------------------------------------------------------------
# 注册表（内存缓存 + 文件持久化）
# --------------------------------------------------------------------------

_bots: dict[str, Bot] = {}
_profiles: dict[str, Profile] = {}


def _file() -> Path:
    return config.DATA_DIR / "profiles.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _default_bot() -> Bot:
    return Bot(BOT_DEFAULT, "默认管家", "", "", True)


def _load() -> tuple[dict[str, Bot], dict[str, Profile]]:
    bots: dict[str, Bot] = {BOT_DEFAULT: _default_bot()}
    profiles: dict[str, Profile] = {}
    f = _file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.error("profiles.json 不可读（%s），本次按空注册表处理", e)
            data = {}
        for item in (data.get("bots") or [])[: _MAX_BOTS * 2]:
            bid = str(item.get("id", "")).strip()
            if not _BOT_ID_RE.fullmatch(bid):
                log.warning("profiles.json: 跳过非法 bot id %r", bid)
                continue
            if bid == BOT_DEFAULT:
                continue  # default 由内置定义，不用文件里的
            bots[bid] = Bot(
                bid,
                str(item.get("name", "")).strip()[:_MAX_NAME] or bid,
                str(item.get("persona", ""))[:_MAX_PERSONA],
                str(item.get("base_url", "")).strip().rstrip("/"),
                bool(item.get("hermes", True)),
            )
        for item in (data.get("profiles") or [])[: _MAX_PROFILES * 2]:
            key = str(item.get("install_key", "")).strip()
            if not _KEY_RE.fullmatch(key):
                log.warning("profiles.json: 跳过非法设备键 %r", key[:40])
                continue
            tenant = str(item.get("tenant", "")).strip() or tenants.DEFAULT_TENANT
            if tenant not in {t.id for t in tenants.all_tenants()}:
                # 租户被注销后档案仍留着：不丢弃（用户可能只是改名），
                # 但拉回默认租户，避免上传时 403 把数据卡死。
                log.warning("设备档案 %s 指向未注册租户 %s，已拉回默认", key[:16], tenant)
                tenant = tenants.DEFAULT_TENANT
            bot_id = str(item.get("bot", "")).strip() or BOT_DEFAULT
            if bot_id not in bots:
                bot_id = BOT_DEFAULT
            profiles[key] = Profile(
                install_key=key,
                label=str(item.get("label", "")).strip()[:_MAX_NAME],
                tenant=tenant,
                bot=bot_id,
                hermes=bool(item.get("hermes", True)),
                note=str(item.get("note", ""))[:_MAX_NOTE],
                first_seen=str(item.get("first_seen", "")),
                updated_at=str(item.get("updated_at", "")),
            )
    return bots, profiles


def reload() -> None:
    """重新加载注册表（改动文件或新增租户后调用）。"""
    global _bots, _profiles
    _bots, _profiles = _load()
    log.info("设备档案注册表：%d 台设备 / %d 个管家角色", len(_profiles), len(_bots))


def _ensure_loaded() -> None:
    if not _bots:
        reload()


def _save() -> None:
    f = _file()
    payload = {
        "bots": [b.public() for b in sorted(_bots.values(), key=lambda x: (x.id != BOT_DEFAULT, x.id))],
        "profiles": [
            {
                "install_key": p.install_key,
                "label": p.label,
                "tenant": p.tenant,
                "bot": p.bot,
                "hermes": p.hermes,
                "note": p.note,
                "first_seen": p.first_seen,
                "updated_at": p.updated_at,
            }
            for p in sorted(_profiles.values(), key=lambda x: x.install_key)
        ],
    }
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)  # 原子替换：进程在写一半时被杀不会留下半个 JSON


# --------------------------------------------------------------------------
# 管家角色（bot）
# --------------------------------------------------------------------------


def all_bots() -> list[Bot]:
    _ensure_loaded()
    return sorted(_bots.values(), key=lambda b: (b.id != BOT_DEFAULT, b.id))


def resolve_bot(bot_id: str) -> Bot:
    """解析管家角色；未注册一律回落默认 bot（**不抛错**——对话路径不能因为
    一个配错的 bot id 直接 500）。"""
    _ensure_loaded()
    return _bots.get((bot_id or "").strip()) or _bots[BOT_DEFAULT]


def upsert_bot(bot_id: str, *, name: str = "", persona: str = "",
               base_url: str = "", hermes: bool | None = None) -> Bot:
    """新建或更新管家角色。id 非法抛 ValueError。"""
    _ensure_loaded()
    bid = (bot_id or "").strip()
    if not _BOT_ID_RE.fullmatch(bid):
        raise ValueError("管家 ID 需为小写字母开头，1-24 位小写字母/数字/中划线/下划线")
    if bid == BOT_DEFAULT:
        # default 只允许改名字与 persona，不允许改开关语义（它是所有人的兜底）
        cur = _bots[BOT_DEFAULT]
        _bots[BOT_DEFAULT] = Bot(
            BOT_DEFAULT,
            (name or "").strip()[:_MAX_NAME] or cur.name,
            (persona if persona else cur.persona)[:_MAX_PERSONA],
            "",
            True,
        )
        _save()
        return _bots[BOT_DEFAULT]
    if bid not in _bots and len(_bots) >= _MAX_BOTS:
        raise ValueError(f"管家角色数量已达上限 {_MAX_BOTS}")
    cur = _bots.get(bid) or Bot(bid, bid, "", "", True)
    _bots[bid] = Bot(
        bid,
        (name or "").strip()[:_MAX_NAME] or cur.name,
        (persona if persona is not None else cur.persona)[:_MAX_PERSONA],
        (base_url or "").strip().rstrip("/"),
        cur.hermes if hermes is None else bool(hermes),
    )
    _save()
    log.info("管家角色已保存：%s（%s）", bid, _bots[bid].name)
    return _bots[bid]


def remove_bot(bot_id: str) -> bool:
    """删除管家角色。绑定了它的设备会被**改回默认 bot**（不删设备档案）。"""
    _ensure_loaded()
    if bot_id == BOT_DEFAULT:
        raise ValueError("默认管家不可删除")
    if bot_id not in _bots:
        return False
    del _bots[bot_id]
    for key, p in list(_profiles.items()):
        if p.bot == bot_id:
            _profiles[key] = _replace(p, bot=BOT_DEFAULT)
    _save()
    log.info("管家角色已删除，相关设备回落到默认管家：%s", bot_id)
    return True


# --------------------------------------------------------------------------
# 设备档案
# --------------------------------------------------------------------------


def _replace(p: Profile, **kw) -> Profile:
    data = {
        "install_key": p.install_key, "label": p.label, "tenant": p.tenant,
        "bot": p.bot, "hermes": p.hermes, "note": p.note,
        "first_seen": p.first_seen, "updated_at": p.updated_at,
    }
    data.update(kw)
    return Profile(**data)


def all_profiles() -> list[Profile]:
    _ensure_loaded()
    return sorted(_profiles.values(), key=lambda p: p.first_seen or p.install_key)


def get_profile(install_key: str) -> Profile | None:
    """只读查询。未知设备返回 None（调用方走默认行为）。"""
    _ensure_loaded()
    return _profiles.get((install_key or "").strip())


def ensure_profile(install_key: str, *, label: str = "",
                   tenant: str = tenants.DEFAULT_TENANT) -> Profile | None:
    """上传路径调用：设备没档案就**自动登记**一条，让零配置部署也能工作。

    自动登记用客户端自报的租户（调用方已校验过存在性）与默认 bot。
    之后管理员在服务端改档案，就以档案为准。
    """
    _ensure_loaded()
    key = (install_key or "").strip()
    if not _KEY_RE.fullmatch(key):
        return None
    cur = _profiles.get(key)
    if cur is not None:
        # 只补 label：客户端显示名改了要跟上，但**绝不覆盖租户/bot/hermes**
        # （那三项是服务端权威设置，被上传请求改掉就失去意义了）
        lab = (label or "").strip()[:_MAX_NAME]
        if lab and lab != cur.label:
            _profiles[key] = _replace(cur, label=lab, updated_at=_now())
            _save()
            return _profiles[key]
        return cur
    if len(_profiles) >= _MAX_PROFILES:
        log.warning("设备档案已达上限 %d，拒绝为 %s 自动登记", _MAX_PROFILES, key[:16])
        return None
    t = tenants.DEFAULT_TENANT
    try:
        t = tenants.resolve(tenant).id
    except KeyError:
        pass
    now = _now()
    p = Profile(
        install_key=key, label=(label or "").strip()[:_MAX_NAME], tenant=t,
        bot=BOT_DEFAULT, hermes=True, note="", first_seen=now, updated_at=now,
    )
    _profiles[key] = p
    _save()
    log.info("新设备自动登记：%s（%s）→ 租户 %s / 管家 %s",
             key[:16], p.label or "(未命名)", p.tenant, p.bot)
    return p


def set_profile(install_key: str, *, label: str | None = None,
                tenant: str | None = None, bot: str | None = None,
                hermes: bool | None = None, note: str | None = None,
                create: bool = True) -> Profile:
    """服务端权威设置。只改传入的字段。

    设备不存在时默认**新建**（`create=True`），方便管理员先建后接。
    """
    _ensure_loaded()
    key = (install_key or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise ValueError("设备键不合法（1-64 位，不含控制字符与路径分隔符）")
    cur = _profiles.get(key)
    if cur is None:
        if not create:
            raise KeyError(key)
        now = _now()
        cur = Profile(install_key=key, label="", tenant=tenants.DEFAULT_TENANT,
                      bot=BOT_DEFAULT, hermes=True, note="",
                      first_seen=now, updated_at=now)
    if tenant is not None:
        tid = (tenant or "").strip() or tenants.DEFAULT_TENANT
        try:
            t = tenants.resolve(tid).id
        except KeyError:
            raise ValueError(f"租户 {tid[:24]} 未注册，请先在「数据空间」里新建")
        cur = _replace(cur, tenant=t)
    if bot is not None:
        bid = (bot or "").strip() or BOT_DEFAULT
        if bid not in _bots:
            raise ValueError(f"管家角色 {bid[:24]} 不存在")
        cur = _replace(cur, bot=bid)
    if hermes is not None:
        cur = _replace(cur, hermes=bool(hermes))
    if label is not None:
        cur = _replace(cur, label=(label or "").strip()[:_MAX_NAME])
    if note is not None:
        cur = _replace(cur, note=(note or "").strip()[:_MAX_NOTE])
    cur = _replace(cur, updated_at=_now())
    _profiles[key] = cur
    _save()
    log.info("设备档案已更新：%s → 租户 %s / 管家 %s / hermes=%s",
             key[:16], cur.tenant, cur.bot, cur.hermes)
    return cur


def remove_profile(install_key: str) -> bool:
    """删除档案（**不删数据**，数据留在原租户里由人工处置）。"""
    _ensure_loaded()
    key = (install_key or "").strip()
    if key not in _profiles:
        return False
    del _profiles[key]
    _save()
    log.info("设备档案已删除（数据保留在原租户）：%s", key[:16])
    return True


def hermes_allowed(profile: Profile | None) -> bool:
    """设备是否允许走本机 Hermes。设备与 bot 两级开关是**与**关系：

    - 设备级 `hermes=False`：这台设备一律不用本机管家（访客 / 外来设备）
    - 角色级 `hermes=False`：这个角色一律不用（例如只做语料采集的角色）
    """
    if profile is None:
        return True  # 未登记设备走默认行为，保持旧客户端完全兼容
    return profile.hermes and resolve_bot(profile.bot).hermes


def summary() -> dict:
    """给设置页/工作台用的概览。"""
    _ensure_loaded()
    return {
        "profiles": [p.public() for p in all_profiles()],
        "bots": [b.public() for b in all_bots()],
        "limits": {"max_profiles": _MAX_PROFILES, "max_bots": _MAX_BOTS},
    }
