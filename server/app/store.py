"""SQLite 权威归档库（标准库 sqlite3，成熟零依赖）。**多租户版（2026-09-09）**。

隔离模型：database-per-tenant（见 tenants.py 设计说明）。每个租户一个独立
SQLite 文件（data/tenants/<id>/digitalhuman.db），表结构完全一致：

- uploaded_segment：手机上传的音频段（处理后带文本/本人标记/设备名）
- report：每日三件套（原始转写/干净稿/总结）
- voiceprint：本人声纹 embedding（单行）
- speaker：说话人 centroid 库（本人/甲乙丙丁，跨批跨天身份一致）

并发模型：
    每租户一个连接 + check_same_thread=False。HTTP 请求线程（FastAPI 线程池）
    与管线线程（asyncio.to_thread）会并发访问同一个连接，因此**所有** SQL 执行
    都必须在锁内完成（包括 fetch——sqlite3 的 Cursor 不是线程安全的）。
    一把全局 RLock 保护全部租户连接：粗粒度但正确，且管线侧本就被
    pipeline._gate 全局串行化，锁竞争不是瓶颈。
    对外只暴露 _query / _query_one / _write 三个入口，均已在锁内取完结果。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import asr, config, tenants

log = logging.getLogger(__name__)

# 可重入锁：connect() 与 _write() 会嵌套取锁；同时保护 _conns 字典本身
_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploaded_segment (
    seg_id          INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL,
    date            TEXT    NOT NULL,
    start_offset_ms INTEGER NOT NULL,
    duration_ms     INTEGER NOT NULL,
    speech_ratio    REAL    NOT NULL DEFAULT 0,
    path            TEXT    NOT NULL,
    text            TEXT    NOT NULL DEFAULT '',
    is_user         INTEGER,
    is_speech       INTEGER NOT NULL DEFAULT 1,
    speaker_label   TEXT    NOT NULL DEFAULT '',
    processed       INTEGER NOT NULL DEFAULT 0,
    received_at     REAL    NOT NULL,
    device_label    TEXT    NOT NULL DEFAULT '',
    -- 设备命名空间（2026-09-15）。见 device_registry 的说明。
    install_key     TEXT    NOT NULL DEFAULT '',
    src_seg_id      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seg_date ON uploaded_segment(date);

-- 设备命名空间注册表（2026-09-15，多设备互不干扰的根治方案）。
--
-- 问题（实测）：uploaded_segment 的主键是手机本地的自增 seg_id，两台设备各自
-- 从 1 开始 → 第二台设备上传的 seg 1/2/3 会**覆盖**第一台的 seg 1/2/3
-- （连同 audio/<date>/<seg_id>.wav 文件一起覆盖，原始音频不可恢复）。
-- 目标设备全新安装后正是从 1 开始，上线当天就把手机 9-02 的同名段顶掉了。
--
-- 方案：给每个安装实例分配一个**互不重叠的数字槽位**，落库的全局 id 取
-- `slot * STRIDE + 本机 seg_id`。id 仍是单列整数、仍严格递增，
-- 因此 agent 端 `?since=<seg_id>` 的增量游标、pipeline 的按 id 回写、
-- 报表里的 COUNT 全部无需改动。
--
-- 为什么用 install_key（安装实例 UUID）而不是 device_label（用户可改的中文名）：
-- 两台设备完全可能被用户起成同一个名字（「目标设备」/「客厅」），
-- 拿可改的名字当唯一键，等于把「互不影响」建立在用户不起重名之上。
CREATE TABLE IF NOT EXISTS device_registry (
    install_key TEXT PRIMARY KEY,
    slot        INTEGER NOT NULL UNIQUE,
    label       TEXT    NOT NULL DEFAULT '',
    first_seen  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS report (
    date          TEXT PRIMARY KEY,
    raw           TEXT NOT NULL DEFAULT '',
    clean         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    generated_at  REAL,
    ready         INTEGER NOT NULL DEFAULT 0,
    summary_generated_at REAL NOT NULL DEFAULT 0,
    final_at      REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS voiceprint (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    embedding   BLOB NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS speaker (
    label      TEXT PRIMARY KEY,
    embedding  BLOB NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

-- 具名角色声纹（用户显式注册：本人 / 张三 / 李四…）。
-- 与 speaker（聚类 centroid）分开存：named_speaker 是用户授权写入、语义明确、
-- 匹配优先级高于聚类簇的锚点；speaker 是系统自动累积的匿名簇（甲乙丙丁）。
CREATE TABLE IF NOT EXISTS named_speaker (
    name       TEXT PRIMARY KEY,
    embedding  BLOB NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 具名角色的**多锚点**（2026-09-10 声纹判定质量修复）。
-- named_speaker 的 name 是主键 → 一个角色只能有一个 embedding。但同一个人的
-- 手机近场录音与远场录音是两套声学特征（实测本人自己的历史簇与注册锚点余弦只有
-- 0.55~0.62，刚好卡在 0.6 阈值上下），单锚点必然漏判。
-- 这里允许一个角色挂多个锚点，匹配时取 max —— 不加权平均，避免把两套特征
-- 平均成一个「谁都不像」的中间点。
-- source 区分来源：'register'（用户注册，可覆盖）/ 'merge:<簇名>'（归并历史簇）。
CREATE TABLE IF NOT EXISTS named_anchor (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    embedding  BLOB    NOT NULL,
    source     TEXT    NOT NULL DEFAULT '',
    count      INTEGER NOT NULL DEFAULT 1,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_named_anchor_name ON named_anchor(name);

-- 实时对话记录（2026-09-10，Hermes 对接）。只存文本与时延，不存音频——
-- 音频在 config.talk_dir()，TTL 到期即删（对话录音不该长期滞留）。
-- 用途：日报关联（「今天跟管家说了什么」）+ 播报成功率复盘。
CREATE TABLE IF NOT EXISTS talk_message (
    turn_id     TEXT PRIMARY KEY,
    date        TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    device      TEXT    NOT NULL DEFAULT '',
    session_id  TEXT    NOT NULL DEFAULT '',
    asr_text    TEXT    NOT NULL DEFAULT '',
    is_user     INTEGER,
    speaker     TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT '',
    reply_text  TEXT    NOT NULL DEFAULT '',
    reply_speaker TEXT  NOT NULL DEFAULT '',
    call_id     TEXT    NOT NULL DEFAULT '',
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    via         TEXT    NOT NULL DEFAULT '',
    played_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_talk_date ON talk_message(date);
CREATE INDEX IF NOT EXISTS idx_talk_session ON talk_message(session_id);

-- 推进「智能家居 AI 工作台」的推送记录（2026-10-25）。
--
-- 幂等靠 ref（调用方给的来源标识，通常是某段录音的 seg_id）做主键：
-- 同一个 ref 第二次推送会被认成重复，工作台侧不再产生第二条入库。
--
-- 为什么存进 SQLite 而不是另起一个标记文件：本文件已经是本项目的权威记录层
-- （每租户一个库、并发有锁、随备份走），再造一套平行存储等于多一处要维护的状态；
-- 而「哪些素材已经推进过去了」本来就是一条需要随数据备份一起走的记录。
-- 每租户独立库 → ref 的唯一性天然按租户隔离，不同租户的同名 seg_id 互不干扰。
CREATE TABLE IF NOT EXISTS workbench_push (
    ref          TEXT PRIMARY KEY,
    kind         TEXT    NOT NULL DEFAULT 'transcript',
    job_id       TEXT    NOT NULL DEFAULT '',
    doc_type     TEXT    NOT NULL DEFAULT '',
    deficiency   INTEGER NOT NULL DEFAULT 0,
    score        INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT '',
    pushed_at    REAL    NOT NULL
);
"""

# 旧库迁移：为 uploaded_segment 补 is_speech / speaker_label 列（CREATE IF NOT EXISTS 不会加列）
_MIGRATIONS = [
    ("uploaded_segment", "is_speech", "ALTER TABLE uploaded_segment ADD COLUMN is_speech INTEGER NOT NULL DEFAULT 1"),
    ("uploaded_segment", "speaker_label", "ALTER TABLE uploaded_segment ADD COLUMN speaker_label TEXT NOT NULL DEFAULT ''"),
    ("uploaded_segment", "device_label", "ALTER TABLE uploaded_segment ADD COLUMN device_label TEXT NOT NULL DEFAULT ''"),
    # 设备命名空间（2026-09-15）：install_key 记录该段来自哪个安装实例，
    # src_seg_id 保留设备本机的原始段号（排查时「这台的 3 号段」比全局 id 直观）。
    # 历史行一律留空 / 0：它们的 id 都小于 STRIDE，与新分配的命名空间天然不重叠，
    # 不需要（也不该）回填一个猜测出来的设备身份。
    ("uploaded_segment", "install_key", "ALTER TABLE uploaded_segment ADD COLUMN install_key TEXT NOT NULL DEFAULT ''"),
    ("uploaded_segment", "src_seg_id", "ALTER TABLE uploaded_segment ADD COLUMN src_seg_id INTEGER NOT NULL DEFAULT 0"),
    ("report", "summary_generated_at", "ALTER TABLE report ADD COLUMN summary_generated_at REAL NOT NULL DEFAULT 0"),
    ("report", "final_at", "ALTER TABLE report ADD COLUMN final_at REAL NOT NULL DEFAULT 0"),
    # 管家回复的角色标注（2026-09-10）：形如「大锅的hermes」。
    # 不回填历史行——老记录本来就没这个概念，硬编一个角色名等于伪造数据。
    ("talk_message", "reply_speaker", "ALTER TABLE talk_message ADD COLUMN reply_speaker TEXT NOT NULL DEFAULT ''"),
]


# --------------------------------------------------------------------------
# 并发安全基元（per-tenant 连接）
# --------------------------------------------------------------------------
def connect(tenant: str = tenants.DEFAULT_TENANT) -> sqlite3.Connection:
    """获取指定租户的（首次调用时创建并完成建表/迁移的）单例连接。"""
    key = (tenant or tenants.DEFAULT_TENANT).strip() or tenants.DEFAULT_TENANT
    with _lock:
        conn = _conns.get(key)
        if conn is None:
            t = tenants.resolve(key)   # 未注册租户在这里 KeyError，绝不静默建库
            t.ensure_dirs()
            conn = sqlite3.connect(t.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
            _conns[key] = conn
        return conn


def _query(sql: str, params: tuple = (), tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    """查询多行。execute + fetchall 全程持锁。"""
    with _lock:
        rows = connect(tenant).execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _query_one(sql: str, params: tuple = (), tenant: str = tenants.DEFAULT_TENANT) -> dict[str, Any] | None:
    """查询单行。execute + fetchone 全程持锁。"""
    with _lock:
        row = connect(tenant).execute(sql, params).fetchone()
        return dict(row) if row else None


def _write(sql: str, params: tuple = (), tenant: str = tenants.DEFAULT_TENANT) -> None:
    """写入。execute + commit 全程持锁。"""
    with _lock:
        conn = connect(tenant)
        conn.execute(sql, params)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    # 迁移项可能属于不同的表，必须逐表取列名再判断（此前只查 uploaded_segment，
    # 给 report 加列时会把 report 的迁移误判为已应用而不执行）。
    table_cols: dict[str, set[str]] = {}

    def cols_of(table: str) -> set[str]:
        if table not in table_cols:
            table_cols[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        return table_cols[table]

    for table, col, ddl in _MIGRATIONS:
        if col not in cols_of(table):
            conn.execute(ddl)


# 自动快照保留份数。22MB/份 → 7 份约 154MB，用这点空间换「能回退到任意一天」。
# 不采用「只留最新一份」：最新那份若恰好是在损坏状态下做的，就彻底没有退路。
_AUTO_BACKUP_KEEP = 7


def _auto_backup(tenant_id: str) -> None:
    """启动时给租户库做一份快照，滚动保留最近 N 份。

    为什么必须在 `connect()` **之前**：连上之后 `executescript(SCHEMA)` 与
    `_migrate()` 会改表结构。快照要在改动**之前**落地，否则回滚到的是「已经改过」
    的库，等于没备份。

    为什么用 `sqlite3.Connection.backup()` 而不是 `shutil.copy`：库处于 WAL 模式，
    直接复制文件会漏掉 `-wal` 里尚未 checkpoint 的事务，恢复后数据会缺一截。
    `backup()` 走 SQLite 在线备份 API，拿到的是一致性快照。

    失败只记 warning 不抛异常：备份是保险，不该因为它挂掉就让服务起不来。
    """
    try:
        db_path = Path(tenants.resolve(tenant_id).db_path)
        if not db_path.is_file():
            return  # 首次启动还没库，没什么可备
        out_dir = db_path.parent.parent.parent / "backups"  # data/backups/
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = out_dir / f"auto_{tenant_id}_{stamp}.db"

        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        size_mb = dest.stat().st_size / 1048576
        log.info("[store] 启动快照 %s（%.1fMB）", dest.name, size_mb)

        # 滚动清理：同租户的 auto_ 快照只留最近 N 份
        snaps = sorted(out_dir.glob(f"auto_{tenant_id}_*.db"))
        for old in snaps[:-_AUTO_BACKUP_KEEP]:
            try:
                old.unlink()
                log.info("[store] 清理旧快照 %s", old.name)
            except OSError as e:
                log.warning("[store] 清理快照失败 %s：%s", old.name, e)
    except Exception as e:  # noqa: BLE001
        log.warning("[store] 启动快照失败（不影响启动）：%s", e)


def open_all() -> None:
    """启动时把所有已注册租户的库打开（建表/迁移前置），并打印概览。"""
    for t in tenants.all_tenants():
        _auto_backup(t.id)   # 迁移前快照，放在 connect() 之前是刻意的
        connect(t.id)
    log_conn_info()


def log_conn_info() -> None:
    for key, conn in _conns.items():
        n = conn.execute("SELECT COUNT(*) FROM uploaded_segment").fetchone()[0]
        r = conn.execute("SELECT COUNT(*) FROM report").fetchone()[0]
        log.info("[store] tenant=%s segments=%d reports=%d", key, n, r)


def close_tenant(tenant: str = tenants.DEFAULT_TENANT) -> None:
    """关闭并移出连接缓存（2026-09-15，备份恢复专用）。

    从备份包恢复 = **整体替换** digitalhuman.db 文件。文件被换掉后，进程里
    那个已打开的连接仍然指向旧 inode：后续查询会静默读到「恢复前」的数据，
    写入更是落到一个已经没人看的文件上（而且不回滚，因为看不见它）。
    所以替换文件之前必须先真正 close，替换之后由 connect() 重建。

    幂等：没打开过也不报错（恢复流程里调用方不必判断状态）。
    """
    key = (tenant or tenants.DEFAULT_TENANT).strip() or tenants.DEFAULT_TENANT
    with _lock:
        conn = _conns.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception as e:  # noqa: BLE001
                log.warning("[store] close tenant=%s 失败: %s", key, e)


# --------------------------------------------------------------------------
# 音频段
# --------------------------------------------------------------------------
# 设备命名空间步长。每个安装实例独占 [slot*STRIDE+1, slot*STRIDE+STRIDE-1]，
# 一台设备要录满 1000 万段才会溢到下一个槽位（现实里不可能）。
# 取这么大是为了让「历史行（id 都很小）永不与新冠军 id 冲突」这件事
# 不需要任何回填或特判。
_SEG_STRIDE = 10_000_000


def install_slot(install_key: str, device_label: str = "",
                 tenant: str = tenants.DEFAULT_TENANT) -> int:
    """取（必要时分配）该安装实例的 id 命名空间槽位。

    - 首次见到的 install_key：分配一个新的槽位并落库（幂等：并发由 _lock 串行化）
    - 已见过的：返回既有槽位，并顺手更新展示名（用户在 App 里改了「设备名称」）
    - 没带 install_key 的旧版客户端：退回用 device_label 作键；再没有就用 'legacy'

    空库特例：**一个段都没有、也没登记过设备**的全新部署，第一个设备拿槽位 0，
    也就是 id 与设备本机号完全一致——保持新装用户看到的就是 1/2/3…，
    不为了一个理论上的冲突把所有人推到 1000 万号段。
    """
    key = (install_key or "").strip()
    if not key:
        lab = (device_label or "").strip()
        key = f"label:{lab}" if lab else "legacy"
    with _lock:
        conn = connect(tenant)
        row = conn.execute(
            "SELECT slot FROM device_registry WHERE install_key = ?", (key,)
        ).fetchone()
        if row is not None:
            if (device_label or "").strip():
                conn.execute(
                    "UPDATE device_registry SET label = ? WHERE install_key = ?",
                    ((device_label or "").strip()[:40], key),
                )
                conn.commit()
            return int(row[0])

        # 「空库特例」必须**同时**看两张表：只看 uploaded_segment 的话，
        # 在一段都还没上传、但已经有设备登记过的窗口期里（服务刚起来、第一段还没到），
        # 第二个设备也会被判成「空库」拿 slot 0 → 撞 UNIQUE 约束直接 500。
        empty = (
            conn.execute("SELECT COUNT(*) FROM uploaded_segment").fetchone()[0] == 0
            and conn.execute("SELECT COUNT(*) FROM device_registry").fetchone()[0] == 0
        )
        nxt = 0 if empty else int(
            conn.execute("SELECT COALESCE(MAX(slot), 0) + 1 FROM device_registry").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO device_registry (install_key, slot, label, first_seen) VALUES (?,?,?,?)",
            (key, nxt, (device_label or "").strip()[:40], time.time()),
        )
        conn.commit()
        log.info("[store] 新设备命名空间 tenant=%s key=%s slot=%d（全局 id 自 %d 起）",
                 tenant, key, nxt, nxt * _SEG_STRIDE + 1)
        return nxt


def global_seg_id(install_key: str, src_seg_id: int, device_label: str = "",
                  tenant: str = tenants.DEFAULT_TENANT) -> int:
    """把「设备本机段号」映射为全局唯一的 seg_id。

    纯函数式：槽位定了之后映射结果只取决于 (slot, 本机段号)，因此重复上传
    同一段会落到同一行（幂等），而不是不断产生新行。
    """
    src = int(src_seg_id)
    if src < 0:
        src = 0
    if src >= _SEG_STRIDE:
        # 理论上不可达（一台设备 1000 万段）。真出现就让它溢到下一槽位并留痕，
        # 宁可 id 不连续，也不能静默丢弃一段已经录到的音频。
        log.error("[store] 设备本机段号 %d 超出命名空间步长 %d，已按溢出处理", src, _SEG_STRIDE)
    slot = install_slot(install_key, device_label, tenant)
    return slot * _SEG_STRIDE + src


def upsert_segment(seg_id: int, session_id: int, date: str, start_offset_ms: int,
                   duration_ms: int, speech_ratio: float, path: str,
                   device_label: str = "", tenant: str = tenants.DEFAULT_TENANT,
                   install_key: str = "", src_seg_id: int = 0) -> None:
    _write(
        "INSERT INTO uploaded_segment (seg_id, session_id, date, start_offset_ms, duration_ms,"
        " speech_ratio, path, device_label, install_key, src_seg_id, received_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(seg_id) DO UPDATE SET session_id=excluded.session_id, date=excluded.date,"
        " start_offset_ms=excluded.start_offset_ms, duration_ms=excluded.duration_ms,"
        " speech_ratio=excluded.speech_ratio, path=excluded.path,"
        # 重传保护（2026-10-23 实测事故）：手机「重新上传某天」会 upsert 当天全部段，
        # 原实现无条件 processed=0, text='' 把已有转写清空——实测 419 段被清掉 263 段，
        # 日报从 23404 字缩到 7938 字。同一 seg_id 的音频内容不会变（seg_id 单调递增），
        # 所以：已有文本就保留（连同 processed / is_user），没有才走全新转写。
        " processed=CASE WHEN LENGTH(COALESCE(text,''))>0 THEN processed ELSE 0 END,"
        " text=COALESCE(text,''),"
        " is_user=CASE WHEN LENGTH(COALESCE(text,''))>0 THEN is_user ELSE NULL END,"
        " received_at=excluded.received_at,"
        " is_user=NULL, received_at=excluded.received_at,"
        # device_label 必须在 SET 里（2026-09-15 修）：漏掉它时，第二台设备上传
        # 同一 seg_id 会继承第一台的设备名，统计里两台设备的数据会串在一起。
        " device_label=excluded.device_label, install_key=excluded.install_key,"
        " src_seg_id=excluded.src_seg_id",
        (seg_id, session_id, date, start_offset_ms, duration_ms, speech_ratio, path,
         device_label, install_key, src_seg_id, time.time()),
        tenant,
    )


def devices_summary(days: int = 7, tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    """按设备汇总上传情况（多团队成员场景，2026-09-09）。

    device_label 由手机端随上传携带（设置里的「设备名称」），旧版 App 不带则
    归入 ''（显示为「未标记」）。返回按最近上传时间倒序。
    """
    since = time.time() - days * 86400
    return _query(
        "SELECT COALESCE(NULLIF(device_label,''),'未标记') AS label,"
        " COUNT(*) AS segments,"
        " SUM(CASE WHEN processed=0 THEN 1 ELSE 0 END) AS pending,"
        " MAX(received_at) AS last_seen"
        " FROM uploaded_segment WHERE received_at >= ?"
        " GROUP BY label ORDER BY last_seen DESC",
        (since,),
        tenant,
    )


def segments_of_date(date: str, tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    return _query(
        "SELECT * FROM uploaded_segment WHERE date = ? ORDER BY start_offset_ms", (date,),
        tenant,
    )


# --------------------------------------------------------------------------
# Agent 知识端点（只读，/api/agent/* 专用）
# --------------------------------------------------------------------------
def report_dates(tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    """有日报的日期 + 就绪标志 + 人声段数（agent 侧挑选学习范围用）。"""
    return _query(
        "SELECT r.date, r.ready, COUNT(s.seg_id) AS speech_segments"
        " FROM report r LEFT JOIN uploaded_segment s"
        "   ON s.date = r.date AND s.is_speech = 1 AND s.text != ''"
        " GROUP BY r.date, r.ready ORDER BY r.date",
        (),
        tenant,
    )


def segments_for_agent(date: str, since: int, limit: int,
                       tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    """增量取人声转写行：seg_id > since（排他），按 seg_id 升序。

    只给 agent 需要的字段：绝不含音频路径（path 不进 SELECT），
    声纹向量本来就存在 voiceprint/speaker 表，与 segment 无关。
    """
    return _query(
        "SELECT seg_id, start_offset_ms, duration_ms, text, is_user, speaker_label"
        " FROM uploaded_segment"
        " WHERE date = ? AND processed = 1 AND is_speech = 1 AND text != '' AND seg_id > ?"
        " ORDER BY seg_id LIMIT ?",
        (date, since, limit),
        tenant,
    )


def mark_segment_processed(seg_id: int, text: str, is_user: bool | None,
                           is_speech: int = 1, speaker_label: str = "",
                           tenant: str = tenants.DEFAULT_TENANT) -> None:
    _write(
        "UPDATE uploaded_segment SET processed = 1, text = ?, is_user = ?,"
        " is_speech = ?, speaker_label = ? WHERE seg_id = ?",
        (text, None if is_user is None else int(is_user), int(is_speech), speaker_label, seg_id),
        tenant,
    )


def update_segment_speaker(seg_id: int, speaker_label: str, is_user: bool | None,
                           tenant: str = tenants.DEFAULT_TENANT) -> None:
    """说话人聚类后回写标签（本人/甲乙丙丁）与是否本人标记。"""
    _write(
        "UPDATE uploaded_segment SET speaker_label = ?, is_user = ? WHERE seg_id = ?",
        (speaker_label, None if is_user is None else int(is_user), seg_id),
        tenant,
    )


# --------------------------------------------------------------------------
# 日报
# --------------------------------------------------------------------------
def upsert_report(date: str, raw: str, clean: str, summary: str, ready: bool,
                  summary_generated_at: float | None = None,
                  tenant: str = tenants.DEFAULT_TENANT) -> None:
    # summary_generated_at：总结稿自己的生成时间（与 generated_at 区分——
    # 总结按 DH_SUMMARY_MIN_INTERVAL 节流复用旧值时，时间戳不能跟着刷新）。
    if summary_generated_at is None:
        summary_generated_at = time.time()
    _write(
        "INSERT INTO report (date, raw, clean, summary, generated_at, ready, summary_generated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(date) DO UPDATE SET raw=excluded.raw, clean=excluded.clean,"
        " summary=excluded.summary, generated_at=excluded.generated_at, ready=excluded.ready,"
        " summary_generated_at=excluded.summary_generated_at",
        (date, raw, clean, summary, time.time(), int(ready), float(summary_generated_at)),
        tenant,
    )


def get_report(date: str, tenant: str = tenants.DEFAULT_TENANT) -> dict[str, Any] | None:
    return _query_one("SELECT * FROM report WHERE date = ?", (date,), tenant)


def mark_final(date: str, ts: float | None = None,
               tenant: str = tenants.DEFAULT_TENANT) -> None:
    """标记该日期已跑过日终终稿（补跑扫描靠它避免重复出终稿）。"""
    _write("INSERT INTO report (date, final_at) VALUES (?, ?)"
           " ON CONFLICT(date) DO UPDATE SET final_at=excluded.final_at",
           (date, float(ts if ts is not None else time.time())),
           tenant)


def dates_with_pending(tenant: str = tenants.DEFAULT_TENANT) -> list[str]:
    """有未处理段的日期，按日期升序——补跑扫描用。

    这是「Mac 关机/手机没发 done」兜底的核心：只要段还在库里且没转写，
    服务起来就该把它处理掉，而不是等手机再发一次通知。

    2026-09-14 扩充：**也包括「转写完成但没拿到说话人角色」的段**。
    转写（置 processed=1、角色留空）与聚类（写角色）是管线里的两步，聚类在整轮
    末尾。中途崩溃会让先转好的段永久停在 processed=1 + speaker_label=''，
    而旧判据只找 processed=0 —— 这些段再也不会被重跑，手机端一直显示「待判定」。
    实测就是这么让 09-14 的 50 段卡住的（CED 长音频崩溃那次）。

    `path <> ''` 是必要护栏（09-14 补）：音频已被终稿轮删掉的段永远补不上角色，
    把它们算进来会让该日期每轮扫描都被重新排队——管线内部会跳过，但日期一直不出队，
    形成不收敛的空转。历史库里 09-04/05/07/09/10 都有这种段。
    """
    rows = _query(
        "SELECT date FROM uploaded_segment"
        " WHERE processed = 0"
        "    OR (is_speech = 1 AND speaker_label = '' AND text <> '' AND path <> '')"
        " GROUP BY date ORDER BY date ASC",
        (), tenant,
    )
    return [r["date"] for r in rows]


def count_pending_speech(tenant: str = tenants.DEFAULT_TENANT) -> int:
    """未转写的人声段数（跨天累计）——「数量触发」模式的判据。

    口径取 `processed = 0 AND is_speech = 1`：手机端显示「待判定」的正是这批。
    刻意**不含**「已转写但缺角色」的段（dates_with_pending 含它们）——那批是
    管线中途崩溃的残留，交给 sweeper 兜底；若也算进触发量，聚类残留会让
    计数永不归零，数量模式会误判为「一直有积压」。
    """
    rows = _query(
        "SELECT count(*) AS n FROM uploaded_segment"
        " WHERE processed = 0 AND is_speech = 1",
        (), tenant,
    )
    return int(rows[0]["n"]) if rows else 0


def dates_without_report(tenant: str = tenants.DEFAULT_TENANT) -> list[str]:
    """有段但还没有任何日报记录的日期（补跑扫描用）。"""
    rows = _query(
        "SELECT DISTINCT s.date FROM uploaded_segment s"
        " LEFT JOIN report r ON r.date = s.date"
        " WHERE r.date IS NULL ORDER BY s.date ASC",
        (), tenant,
    )
    return [r["date"] for r in rows]


def dates_missing_final(since_date: str, limit: int = 5,
                        tenant: str = tenants.DEFAULT_TENANT) -> list[str]:
    """已过日终时间但没出过终稿的日期（最近 limit 天，按升序）。

    since_date：只扫描 >= 该日期的数据，避免把整个历史库翻一遍。
    """
    rows = _query(
        "SELECT s.date FROM (SELECT DISTINCT date FROM uploaded_segment"
        " WHERE date >= ?) s"
        " LEFT JOIN report r ON r.date = s.date"
        " WHERE COALESCE(r.final_at, 0) = 0"
        " ORDER BY s.date DESC LIMIT ?",
        (since_date, limit),
        tenant,
    )
    return sorted(r["date"] for r in rows)


# --------------------------------------------------------------------------
# 声纹 / 说话人库（per-tenant：各租户的声纹完全独立）
# --------------------------------------------------------------------------
def set_voiceprint(embedding: bytes, tenant: str = tenants.DEFAULT_TENANT) -> None:
    _write(
        "INSERT INTO voiceprint (id, embedding, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET embedding=excluded.embedding, updated_at=excluded.updated_at",
        (embedding, time.time()),
        tenant,
    )


def get_voiceprint(tenant: str = tenants.DEFAULT_TENANT) -> bytes | None:
    row = _query_one("SELECT embedding FROM voiceprint WHERE id = 1", (), tenant)
    return row["embedding"] if row else None


def list_speakers(tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    """返回持久化说话人库（label/embedding/count），供聚类加载历史 centroid。"""
    return _query("SELECT label, embedding, count FROM speaker ORDER BY label", (), tenant)


def upsert_speaker(label: str, embedding: bytes, count: int,
                   tenant: str = tenants.DEFAULT_TENANT) -> None:
    _write(
        "INSERT INTO speaker (label, embedding, count, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(label) DO UPDATE SET embedding=excluded.embedding,"
        " count=excluded.count, updated_at=excluded.updated_at",
        (label, embedding, count, time.time()),
        tenant,
    )


def upsert_named_speaker(name: str, embedding: bytes,
                         tenant: str = tenants.DEFAULT_TENANT) -> None:
    """注册/更新一个具名角色声纹（本人 / 张三 / …）。"""
    now = time.time()
    _write(
        "INSERT INTO named_speaker (name, embedding, created_at, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET embedding=excluded.embedding,"
        " updated_at=excluded.updated_at",
        (name, embedding, now, now),
        tenant,
    )


def list_named_speakers(tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    """具名角色列表（不含 embedding，供 App 展示）。"""
    return _query(
        "SELECT name, created_at, updated_at FROM named_speaker ORDER BY created_at",
        (), tenant,
    )


def get_named_speakers_with_embeddings(tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    return _query("SELECT name, embedding FROM named_speaker", (), tenant)


def delete_named_speaker(name: str, tenant: str = tenants.DEFAULT_TENANT) -> bool:
    """删除具名角色。返回 False 表示该角色本就不存在（供接口区分 404）。"""
    if not _query_one("SELECT name FROM named_speaker WHERE name = ?", (name,), tenant):
        return False
    _write("DELETE FROM named_speaker WHERE name = ?", (name,), tenant)
    return True


def replace_speaker_embedding(label: str, embedding: bytes,
                              tenant: str = tenants.DEFAULT_TENANT) -> None:
    """用新 embedding 覆盖既有说话人，保留累计段数。

    用于「配对注册本人声纹」：必须覆盖聚类阶段自动写入的「本人」centroid，
    否则注册前被误判为本人的历史 centroid 会一直生效（S8）。
    """
    _write(
        "INSERT INTO speaker (label, embedding, count, updated_at) VALUES (?,?,1,?)"
        " ON CONFLICT(label) DO UPDATE SET embedding=excluded.embedding,"
        " updated_at=excluded.updated_at",
        (label, embedding, time.time()),
        tenant,
    )


# --------------------------------------------------------------------------
# 具名角色多锚点（2026-09-10 声纹判定质量修复）
# --------------------------------------------------------------------------
def list_named_anchors(tenant: str = tenants.DEFAULT_TENANT,
                       name: str | None = None) -> list[dict[str, Any]]:
    """具名角色的锚点列表。传 name 只取该角色。含 embedding（匹配用）。"""
    if name:
        return _query(
            "SELECT id, name, embedding, source, count, created_at FROM named_anchor"
            " WHERE name = ? ORDER BY id", (name,), tenant)
    return _query(
        "SELECT id, name, embedding, source, count, created_at FROM named_anchor"
        " ORDER BY name, id", (), tenant)


def add_named_anchor(name: str, embedding: bytes, source: str = "", count: int = 1,
                     tenant: str = tenants.DEFAULT_TENANT) -> None:
    _write(
        "INSERT INTO named_anchor (name, embedding, source, count, created_at)"
        " VALUES (?,?,?,?,?)",
        (name, embedding, source, count, time.time()),
        tenant,
    )


def clear_named_anchors(name: str, source: str | None = None,
                        tenant: str = tenants.DEFAULT_TENANT) -> int:
    """删除某角色的锚点，返回删除条数。

    `source` 是**安全阀**：注册新声纹时只能覆盖 `'register'` 那一条，
    绝不能顺手把归并进来的锚点一起删掉 —— 那是用户逐条确认过的历史证据。
    传 None 才是「全删」（删除整个角色时用）。
    """
    with _lock:
        conn = connect(tenant)
        if source is None:
            cur = conn.execute("DELETE FROM named_anchor WHERE name = ?", (name,))
        else:
            cur = conn.execute(
                "DELETE FROM named_anchor WHERE name = ? AND source = ?", (name, source))
        conn.commit()
        return int(cur.rowcount or 0)


def seed_named_anchors(tenant: str = tenants.DEFAULT_TENANT) -> int:
    """把已有 named_speaker 迁成 named_anchor(source='register')，返回迁移条数。幂等。

    老库升级必用：升级前只有 named_speaker，不迁移就等于全部锚点丢失，
    判定会从「时好时坏」直接变成「永远未判定」。
    """
    moved = 0
    for r in _query("SELECT name, embedding FROM named_speaker", (), tenant):
        if _query_one(
            "SELECT id FROM named_anchor WHERE name = ? AND source = 'register'",
            (r["name"],), tenant,
        ):
            continue
        add_named_anchor(r["name"], r["embedding"], "register", 1, tenant)
        moved += 1
    return moved


def merge_speaker_into_named(label: str, name: str, is_user: bool | None = None,
                             tenant: str = tenants.DEFAULT_TENANT) -> dict[str, Any]:
    """把匿名簇 `label` 归并进具名角色 `name`。

    返回 `{ok, added_anchor, deleted_cluster, relabeled_segments, ...}`。

    三步在同一把锁内完成（顺序不可换）：
    ① 读该簇的 centroid；
    ② 把它作为 `name` 的一个**新锚点**写入（source=`merge:<label>`）；
    ③ **删除该簇** —— 这一步不能省。簇还在的话 `identify()` 第③步依旧会命中它，
       返回值仍是 `hit in SELF_ALIASES` = False，等于没修。
    ④ 顺带把历史段标签从 `说话人18` 改写为 `大锅`，否则总结稿继续把本人拆成两个人。

    注意：调用方必须先让用户确认这个簇确实属于该角色。把客人误并进「大锅」，
    声纹门控就会放客人进来 —— 这一步是安全边界，不做自动推断。
    """
    with _lock:
        conn = connect(tenant)
        row = conn.execute(
            "SELECT embedding, count FROM speaker WHERE label = ?", (label,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "cluster_not_found", "label": label}
        conn.execute(
            "INSERT INTO named_anchor (name, embedding, source, count, created_at)"
            " VALUES (?,?,?,?,?)",
            (name, row["embedding"], f"merge:{label}", int(row["count"]), time.time()),
        )
        conn.execute("DELETE FROM speaker WHERE label = ?", (label,))
        cur = conn.execute(
            "UPDATE uploaded_segment SET speaker_label = ?, is_user = ?"
            " WHERE speaker_label = ?",
            (name, None if is_user is None else int(bool(is_user)), label),
        )
        relabeled = int(cur.rowcount or 0)
        conn.commit()
    return {"ok": True, "name": name, "from_label": label,
            "count": int(row["count"]), "relabeled_segments": relabeled}


def rename_speaker_label(old: str, new: str,
                         tenant: str = tenants.DEFAULT_TENANT) -> int:
    """把某个簇标签整体改名（不改 embedding）。返回受影响的段数。

    与 `merge_speaker_into_named` 的区别：本函数**不产生新锚点**，只改标签。
    用于「两个簇其实是同一个人但我只要标签一致」的场景。
    """
    with _lock:
        conn = connect(tenant)
        cur = conn.execute(
            "UPDATE uploaded_segment SET speaker_label = ? WHERE speaker_label = ?",
            (new, old))
        n = int(cur.rowcount or 0)
        conn.commit()
        return n


# --------------------------------------------------------------------------
# 清理与导出
# --------------------------------------------------------------------------
def cleanup_audio_files(date: str, tenant: str = tenants.DEFAULT_TENANT) -> None:
    """处理成功后仅删除该日期的原始 WAV 文件。

    行记录保留作归档：text / is_user / speech_ratio 供日报 segment_flags
    与语料导出查询（delete 整行会导致报表声纹标记永远为空——实测踩坑）。
    """
    rows = _query("SELECT path FROM uploaded_segment WHERE date = ?", (date,), tenant)
    for r in rows:
        try:
            Path(r["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    _write(
        "UPDATE uploaded_segment SET path = '' WHERE date = ? AND path != ''", (date,),
        tenant,
    )


def export_corpus_jsonl(out_path: Path, date: str, min_ratio: float = 0.8,
                        tenant: str = tenants.DEFAULT_TENANT) -> int:
    """导出本人多轮对话 JSONL 语料（GPT-SoVITS 辅助文本样本）。

    仅收录**该日期下**「本人」且「人声」的段（date + speaker_label='本人'
    + is_speech=1）。date 过滤是必须的：本文件写在 corpus/{date}/ 目录下，
    少了它会把其他日期的本人语料混进来，污染该批训练数据。
    """
    rows = _query(
        "SELECT text FROM uploaded_segment WHERE processed = 1 AND is_speech = 1"
        " AND speaker_label = ? AND date = ? AND speech_ratio >= ?"
        " AND length(text) > 4 ORDER BY start_offset_ms",
        (config.SELF_LABEL, date, min_ratio),
        tenant,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            # 重复幻觉兜底（2026-09-17）：语料是拿去训 GPT-SoVITS 的，
            # 库里早于建稿修复入库的旧 text 里带着 ASR 的循环幻觉
            # （实测 09-16 有 4 条、09-14 有 7 条，如「贴」×466、「你」×491），
            # 这种文本混进训练集是纯污染，必须在导出出口再压一道。
            # 与 pipeline._line() 同一函数，判据一致、幂等。
            f.write(json.dumps({"text": asr.collapse_repeats(r["text"]),
                                "speaker": "user"}, ensure_ascii=False) + "\n")
    return len(rows)


# --------------------------------------------------------------------------
# 重要时刻标记（DayoTag 式随手标记）
# --------------------------------------------------------------------------
# 刻意用 JSON 文件而不是 SQLite 表：一天几十个封顶、只追加、无需跨天查询，
# 建表 + 迁移的成本远超收益。per-tenant：各租户 markers 目录独立。
def _marker_path(date: str, tenant: str = tenants.DEFAULT_TENANT) -> Path:
    t = tenants.resolve(tenant)
    return t.markers_dir / f"{date}.json"


def add_marker(date: str, t: str, note: str = "",
               tenant: str = tenants.DEFAULT_TENANT) -> dict:
    """记一个重要时刻。幂等性不做同 t 去重——同秒连点两次就多记一条，无害。"""
    f = _marker_path(date, tenant)
    f.parent.mkdir(parents=True, exist_ok=True)
    items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    item = {"t": t, "note": note}
    items.append(item)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return item


def markers_of_date(date: str, tenant: str = tenants.DEFAULT_TENANT) -> list[dict]:
    f = _marker_path(date, tenant)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # 文件损坏时不炸日报，标记丢了可以重按


# --------------------------------------------------------------------------
# 实时对话记录（2026-09-10，Hermes 对接）
# --------------------------------------------------------------------------
def create_talk_message(turn_id: str, date: str, ts: float, device: str = "",
                        session_id: str = "",
                        tenant: str = tenants.DEFAULT_TENANT) -> None:
    """一轮对话的起点记录。此刻还不知道 ASR 结果，先占位。

    分两次写（create → update_reply）而不是一次写全：ASR 与 Hermes 调用是
    两个可能各自失败的阶段，中途崩溃时留下「有提问没回复」的记录，
    比整条丢失更容易排查（用户报「说了没反应」时能确认是丢在哪一步）。
    """
    _write(
        "INSERT INTO talk_message (turn_id, date, ts, device, session_id)"
        " VALUES (?,?,?,?,?) ON CONFLICT(turn_id) DO NOTHING",
        (turn_id, date, ts, device, session_id), tenant,
    )


def update_talk_asr(turn_id: str, asr_text: str, is_user: bool | None,
                    speaker: str = "", tenant: str = tenants.DEFAULT_TENANT) -> None:
    _write(
        "UPDATE talk_message SET asr_text=?, is_user=?, speaker=? WHERE turn_id=?",
        (asr_text, None if is_user is None else int(is_user), speaker, turn_id),
        tenant,
    )


def update_talk_reply(turn_id: str, status: str, reply_text: str = "",
                      call_id: str = "", latency_ms: int = 0, via: str = "",
                      reply_speaker: str = "",
                      tenant: str = tenants.DEFAULT_TENANT) -> None:
    """via 取值：hermes（走了管家）/ ai_only（降级纯 AI）/ failed（都没成）。

    `reply_speaker`：回复的角色归属，形如「大锅的hermes」。管家不是泛泛的
    「机器人」，它属于唤醒它的那个角色——回看记录时要能看出「这是大锅的管家
    在答」。不采集它的音频（回复文本已直接拿到，录一遍纯属冗余），
    所以它只以**文本 + 时间戳 + 角色名**的形态进记录。
    """
    _write(
        "UPDATE talk_message SET status=?, reply_text=?, call_id=?, latency_ms=?, via=?,"
        " reply_speaker=? WHERE turn_id=?",
        (status, reply_text, call_id, int(latency_ms), via, reply_speaker, turn_id), tenant,
    )


def mark_talk_played(turn_id: str, played_ms: int,
                     tenant: str = tenants.DEFAULT_TENANT) -> None:
    _write("UPDATE talk_message SET played_ms=? WHERE turn_id=?",
           (int(played_ms), turn_id), tenant)


def get_talk_message(turn_id: str,
                     tenant: str = tenants.DEFAULT_TENANT) -> dict[str, Any] | None:
    return _query_one("SELECT * FROM talk_message WHERE turn_id=?", (turn_id,), tenant)


def talk_messages_of_date(date: str,
                          tenant: str = tenants.DEFAULT_TENANT) -> list[dict[str, Any]]:
    return _query(
        "SELECT * FROM talk_message WHERE date=? ORDER BY ts ASC", (date,), tenant)


def cleanup_talk_messages(before_ts: float,
                          tenant: str = tenants.DEFAULT_TENANT) -> int:
    """清理过期对话记录（默认保留策略由 sweeper 决定）。

    只删没有回复的孤儿行：有回复的记录是「今天跟管家说了什么」的证据，
    日报关联与效果复盘都要用，不能因为时间久了就清掉。
    """
    with _lock:
        conn = connect(tenant)
        cur = conn.execute(
            "DELETE FROM talk_message WHERE ts < ? AND (reply_text IS NULL OR reply_text='')",
            (before_ts,),
        )
        conn.commit()
        return cur.rowcount


# --------------------------------------------------------------------------
# 推进工作台的推送记录（幂等）
# --------------------------------------------------------------------------
def claim_push(ref: str, kind: str = "transcript",
               tenant: str = tenants.DEFAULT_TENANT) -> bool:
    """给一个 ref 占坑：第一次返回 True（登记成功），已存在返回 False。

    用 `INSERT OR IGNORE` + rowcount 判重，**不是**「先查后插」：
    后者在两个并发请求下会双双查到「不存在」而都判定为新 ref，
    结果就是同一段素材被推进两次、工作台侧多出一条入库。
    占坑与判重必须在同一条语句里原子完成。
    """
    with _lock:
        conn = connect(tenant)
        cur = conn.execute(
            "INSERT OR IGNORE INTO workbench_push (ref, kind, pushed_at) VALUES (?,?,?)",
            (ref, kind, time.time()),
        )
        conn.commit()
        return cur.rowcount > 0


def finish_push(ref: str, job_id: str = "", doc_type: str = "",
                deficiency: int = 0, score: int = 0, status: str = "",
                tenant: str = tenants.DEFAULT_TENANT) -> None:
    """回填推送结果（工作台那边的作业 id、文档类型、缺项数等）。

    放在推送**成功之后**回填：占坑那一刻还不知道结果，而重复推送时
    要把这份结果原样回显给调用方（"上次已经推进过了，这是那次的作业 id"）。
    """
    _write(
        "UPDATE workbench_push SET job_id=?, doc_type=?, deficiency=?, score=?, status=? "
        "WHERE ref=?",
        (job_id, doc_type, int(deficiency), int(score), status, ref), tenant,
    )


def drop_push(ref: str, tenant: str = tenants.DEFAULT_TENANT) -> None:
    """推送失败时撤销占坑，让同一个 ref 还能重试。

    不撤销的后果很实际：一次网络抖动就把这段素材永久标记成「已推送」，
    而工作台那边其实什么都没收到——素材从此再也不会被推进过去。
    """
    _write("DELETE FROM workbench_push WHERE ref=?", (ref,), tenant)


def push_record(ref: str, tenant: str = tenants.DEFAULT_TENANT) -> dict[str, Any] | None:
    """读回某个 ref 的推送记录（没有则 None）。"""
    return _query_one("SELECT * FROM workbench_push WHERE ref=?", (ref,), tenant)

