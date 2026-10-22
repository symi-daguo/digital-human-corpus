"""多租户隔离（2026-09-09）：每部手机 = 一个租户，数据目录级物理隔离。

设计（选型依据见 docs/verification.md §18）：
    SQLite 生态下三种主流多租户模式——共享表+tenant_id 列 / schema 隔离 / 库级隔离，
    本项目选 **库级隔离（database-per-tenant）**：
    - row-level 隔离要求每条 SQL 都带 WHERE tenant_id，漏一处就跨租户泄漏，
      且泄漏难以察觉（OWASP Multi-Tenant Cheat Sheet 列为首要风险）；
    - 物理隔离让跨租户泄漏**结构性不可能**，备份/删除/导出都是目录级操作，
      与「各自手机的数据完整备份隔离、相互不干扰」的产品诉求一一对应；
    - 租户数是个团队级别（个位数），不存在连接池/迁移爆炸问题。

结构：
    data/
      tenants.json                 租户注册表（id + 显示名）
      tenants/<id>/digitalhuman.db 租户独立库（segment/report/声纹/说话人）
      tenants/<id>/audio|text|corpus|markers/   租户独立文件
      tls/ logs/ backups/          全局共享（证书、日志、备份归档）

租户识别（OWASP：绝不信任客户端自报的租户 ID）：
    手机端请求带 header X-Tenant（ASCII 安全）；服务端在**注册表白名单**里
    校验后才路由，未注册 → 403。不带 X-Tenant → 默认租户 default（旧版 App 完全兼容）。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

DEFAULT_TENANT = "default"
# 租户 id 规则：小写字母开头，小写字母/数字/中划线/下划线，1-24 字。
# 同时防路径穿越（不允许 . / 空格等）与大小写混乱（macOS 文件系统大小写不敏感）。
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str

    @property
    def base(self) -> Path:
        return config.DATA_DIR / "tenants" / self.id

    @property
    def db_path(self) -> Path:
        return self.base / "digitalhuman.db"

    @property
    def audio_dir(self) -> Path:
        return self.base / "audio"

    @property
    def text_dir(self) -> Path:
        return self.base / "text"

    @property
    def corpus_dir(self) -> Path:
        return self.base / "corpus"

    @property
    def markers_dir(self) -> Path:
        return self.base / "markers"

    def ensure_dirs(self) -> None:
        for d in (self.base, self.audio_dir, self.text_dir, self.corpus_dir, self.markers_dir):
            d.mkdir(parents=True, exist_ok=True)


# 注册表缓存：请求路径上只做内存查询，文件 IO 只发生在管理操作时
_registry: dict[str, Tenant] = {}


def _registry_file() -> Path:
    return config.DATA_DIR / "tenants.json"


def _load_registry() -> dict[str, Tenant]:
    """加载租户注册表：tenants.json 优先，env DH_TENANTS（逗号分隔 id）合并进来。

    default 永远存在（旧版 App 与旧数据的落点），不会被删除。
    """
    reg: dict[str, Tenant] = {}
    f = _registry_file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data.get("tenants", []):
                tid = str(item.get("id", "")).strip()
                if _ID_RE.fullmatch(tid):
                    reg[tid] = Tenant(tid, str(item.get("name", "")).strip() or tid)
                else:
                    log.warning("tenants.json: skip invalid id %r", tid)
        except (OSError, ValueError) as e:
            log.error("tenants.json unreadable (%s); fall back to default only", e)
    # env 追加（与 config 三件套同模式：env.local 行级 upsert 即生效于重启后）
    env_val = (config.ENV_TENANTS or "").strip()
    for tid in [x.strip() for x in env_val.split(",") if x.strip()]:
        if _ID_RE.fullmatch(tid):
            reg.setdefault(tid, Tenant(tid, tid))
        else:
            log.warning("DH_TENANTS: skip invalid id %r", tid)
    reg.setdefault(DEFAULT_TENANT, Tenant(DEFAULT_TENANT, "默认空间"))
    if not reg[DEFAULT_TENANT].name or reg[DEFAULT_TENANT].name == DEFAULT_TENANT:
        reg[DEFAULT_TENANT] = Tenant(DEFAULT_TENANT, reg[DEFAULT_TENANT].name or "默认空间")
    return reg


def reload() -> None:
    """重新加载注册表（新增租户后调用）。"""
    global _registry
    _registry = _load_registry()
    for t in _registry.values():
        t.ensure_dirs()
    log.info("tenant registry: %s", sorted(_registry))


def all_tenants() -> list[Tenant]:
    if not _registry:
        reload()
    return sorted(_registry.values(), key=lambda t: (t.id != DEFAULT_TENANT, t.id))


def resolve(tenant_id: str) -> Tenant:
    """校验并解析租户。未注册返回 KeyError（routes 转 403）——绝不静默落 default，
    否则客户端拼错 id 时数据会悄悄混进别人的空间。"""
    if not _registry:
        reload()
    tid = (tenant_id or "").strip() or DEFAULT_TENANT
    if tid not in _registry:
        raise KeyError(tid)
    return _registry[tid]


def add(tenant_id: str, name: str = "") -> Tenant:
    """注册新租户（控制台/管理用）。id 非法或已存在抛 ValueError。"""
    tid = (tenant_id or "").strip()
    if not _ID_RE.fullmatch(tid):
        raise ValueError("租户 ID 需为小写字母开头，1-24 位小写字母/数字/中划线/下划线")
    if not _registry:
        reload()
    if tid in _registry:
        raise ValueError(f"租户 {tid} 已存在")
    t = Tenant(tid, (name or "").strip() or tid)
    _registry[tid] = t
    _save_registry()
    t.ensure_dirs()
    log.info("tenant added: %s (%s)", tid, t.name)
    return t


def remove(tenant_id: str) -> bool:
    """注销租户：只从注册表移除，**不删数据目录**（防误删，物理清除由人工做）。"""
    if tenant_id == DEFAULT_TENANT:
        raise ValueError("默认租户不可删除")
    if not _registry:
        reload()
    if tenant_id not in _registry:
        return False
    del _registry[tenant_id]
    _save_registry()
    log.info("tenant removed from registry (data dir kept): %s", tenant_id)
    return True


def _save_registry() -> None:
    f = _registry_file()
    payload = {"tenants": [{"id": t.id, "name": t.name} for t in all_tenants()]}
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def migrate_legacy_data() -> None:
    """一次性迁移：单租户时代的旧数据 → tenants/default/。

    幂等：default 库已存在（或旧库已不在原位）就直接跳过。
    注意顺序：必须**先 move 再 ensure_dirs**——ensure_dirs 若先建好空的
    corpus/ 等目录，shutil.move(src, dst) 在 dst 已存在时会把 src 整个
    移进 dst 内部（实测变成 corpus/corpus 双层嵌套，2026-09-09 修）。
    """
    default = resolve(DEFAULT_TENANT)
    if default.db_path.exists():
        return  # 已迁移
    legacy_db = config.DATA_DIR / "digitalhuman.db"
    if not legacy_db.exists():
        default.ensure_dirs()  # 全新部署：只需建目录
        return
    moved = []
    for src, dst in [
        (legacy_db, default.db_path),
        (config.AUDIO_DIR, default.audio_dir),
        (config.TEXT_DIR, default.text_dir),
        (config.CORPUS_DIR, default.corpus_dir),
        (config.DATA_DIR / "markers", default.markers_dir),
    ]:
        if not src.exists() or src == dst:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            # 目标已存在（目录）：合并语义，逐项移入，绝不整目录套娃
            if src.is_dir():
                for child in src.iterdir():
                    shutil.move(str(child), str(dst / child.name))
                    moved.append(child.name)
                src.rmdir()
            else:
                shutil.move(str(src), str(dst))
                moved.append(src.name)
        else:
            shutil.move(str(src), str(dst))
            moved.append(src.name)
    default.ensure_dirs()
    log.info("legacy data migrated to tenants/default/: %s", ", ".join(moved) or "(db only)")


def status_of(t: Tenant) -> dict:
    """租户概览（控制台/接口展示用）：库大小、各目录文件数与占用。"""
    db_size = t.db_path.stat().st_size if t.db_path.exists() else 0

    def _dir_files(d: Path) -> int:
        return sum(1 for f in d.rglob("*") if f.is_file()) if d.exists() else 0

    sizes = {name: _dir_bytes(d) for name, d in (
        ("audio", t.audio_dir), ("text", t.text_dir),
        ("corpus", t.corpus_dir), ("markers", t.markers_dir))}
    return {
        "id": t.id,
        "name": t.name,
        "db_bytes": db_size,
        "text_files": _dir_files(t.text_dir),
        "corpus_files": _dir_files(t.corpus_dir),
        "markers": _dir_files(t.markers_dir),
        "dir_bytes": sizes,
        "bytes": db_size + sum(sizes.values()),
    }


# --------------------------------------------------------------------------
# 硬盘占用概览（2026-09-15）
#
# 存在的意义是「占用可见」：长期驻留部署最怕的是某个目录悄悄涨到把盘写满，
# 而服务照常跑、没有报警。这里给出分项数字，工作台概览卡片直接显示。
#
# 为什么要缓存：audio/ 里是几千个小文件，全量 walk 一次要上百毫秒；
# 控制台每次启动、每次切空间都会打这个接口，不缓存会把 UI 拖慢。
# 30 秒 TTL 对「看占用」这个用途完全够。
# --------------------------------------------------------------------------
_storage_cache: tuple[float, dict] = (0.0, {})
_STORAGE_TTL = 30.0


def _dir_bytes(d: Path) -> int:
    """递归求目录占用（字节）。不存在返回 0；单个文件 stat 失败按 0 计
    （并发删除/权限问题不该让整个统计崩掉）。"""
    if not d.exists():
        return 0
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def storage_overview(force: bool = False) -> dict:
    """数据目录总览：分项占用 + 各租户占用。带 30 秒缓存。"""
    global _storage_cache
    import time as _time
    now = _time.time()
    ts, cached = _storage_cache
    if not force and cached and now - ts < _STORAGE_TTL:
        return cached

    data = config.DATA_DIR
    backups = data / "backups"
    logs = data.parent / "logs"
    per_tenant = []
    for t in all_tenants():
        st = status_of(t)
        per_tenant.append({"id": t.id, "name": t.name, "bytes": st["bytes"],
                           "db_bytes": st["db_bytes"], "dir_bytes": st["dir_bytes"]})
    per_tenant.sort(key=lambda x: -x["bytes"])

    backup_files = sorted(backups.glob("*.zip")) if backups.exists() else []
    total = sum(x["bytes"] for x in per_tenant)
    result = {
        "data_dir": str(data),
        "tenants": per_tenant,
        "tenants_bytes": total,
        "backups_bytes": _dir_bytes(backups),
        "backups_count": len(backup_files),
        "backups_oldest": (backup_files[0].name if backup_files else ""),
        "backups_newest": (backup_files[-1].name if backup_files else ""),
        "logs_bytes": _dir_bytes(logs),
        "total_bytes": total + _dir_bytes(backups) + _dir_bytes(logs),
        "scanned_at": int(now),
    }
    _storage_cache = (now, result)
    return result
