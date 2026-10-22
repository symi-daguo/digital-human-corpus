"""Mac 服务端入口：uvicorn 启动（可配自签 TLS）。

用法：
    python -m app.main
    # 或
    uvicorn app.main:app --host 0.0.0.0 --port 8766
"""
from __future__ import annotations

import contextlib
import logging
import threading

from fastapi import FastAPI

from . import config, desktop_bridge, llm, store, sweeper, tenants
from ._version import VERSION
from .agent_api import router as agent_router
from .console import router as console_router
from .routes import router
from .talk import router as talk_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# 进程内是否已有承载入口完成过初始化（2026-09-16）。
#
# 桌面壳会再起一台**只绑回环**的明文 Server 来承载同一个 app
# （desktop_bridge.py），那台也会跑一次 lifespan。不设防的后果很具体：
# `sweeper.start()` 是在*当前事件循环*里建任务，两台 Server 各有一个事件循环，
# 于是会出现两个补跑扫描任务同时扫同一个库（重复处理 + 相互抢锁）。
#
# 判定必须在锁内一次完成（不能先读后写）：两台 Server 的 lifespan 是并发进入的，
# 先读后写会让双方都判定为「我是第一个」。
_lifespan_lock = threading.Lock()
_lifespan_owner_taken = False


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _lifespan_owner_taken
    with _lifespan_lock:
        owner = not _lifespan_owner_taken
        if owner:
            _lifespan_owner_taken = True
    if not owner:
        # 第二个承载入口（桌面壳的回环明文 Server）：初始化已经做完了，
        # 这里只等放行，绝不能重做一遍。它承载的是同一个 app 实例，
        # 因此 /api/console/* 读到的就是主服务的状态。
        yield
        return

    # @app.on_event("startup") 自 FastAPI 0.93 起废弃，lifespan 是官方替代。
    config.ensure_dirs()

    # 安全 fail-fast（2026-09-04）：TOKEN 为空时 routes._check_auth 会全端点放行，
    # 而服务绑 0.0.0.0（手机要 LAN 访问）——空 token 等于局域网裸奔。
    # 只允许显式 loopback 绑定的本机调试场景关闭鉴权。
    if not config.TOKEN.strip() and config.HOST not in ("127.0.0.1", "localhost"):
        raise RuntimeError(
            "DH_TOKEN 为空且绑定在非 loopback 地址（%s）——这会让局域网内任何设备"
            "无鉴权读写全部语料。请设置 DH_TOKEN 或改 DH_HOST=127.0.0.1。" % config.HOST
        )

    # 多租户（2026-09-09）：先做一次性旧数据迁移（单租户 → tenants/default/），
    # 再加载注册表并打开所有租户的库（建表/迁移前置，sweeper 才能逐租户扫描）。
    tenants.migrate_legacy_data()
    tenants.reload()
    store.open_all()
    agent_token = config.ensure_agent_token()
    log.info("server ready, data dir: %s, tenants: %s",
             config.DATA_DIR, [t.id for t in tenants.all_tenants()])

    cert, _ = config.ensure_tls()
    fp = config.tls_fingerprint()
    # 这个横幅是给「拿着手机来配置」的人看的：IP / 端口 / Token / 指纹四项凑齐就能配对。
    # 走 log 而不是 print：重定向到文件时 stdout 是块缓冲，print 会被攒住不输出，
    # 而 logging 走 stderr 是行缓冲，日志里一定看得到。
    banner = "\n".join([
        "=" * 62,
        "  数字人语料服务已启动",
        "-" * 62,
        f"  局域网地址： {', '.join(config._lan_ips())}",
        f"  端口：       {config.PORT}",
        f"  配对 Token： {config.TOKEN or '(未启用鉴权，仅限本机调试)'}",
        f"  租户空间：   {', '.join(t.id for t in tenants.all_tenants())}",
        f"  Agent API： /api/agent/*（token 见 {config.AGENT_TOKEN_FILE}）",
        f"  工作台：     https://{config._lan_ips()[0] if config._lan_ips() else '127.0.0.1'}:{config.PORT}/console",
        f"  证书指纹：   {fp or '(读取失败)'}",
        "-" * 62,
        "  手机端「设置 → Mac 服务连接」填上面四项；",
        "  指纹可点「获取证书指纹」自动填入，无需手抄。",
        "  浏览器打开上面的「工作台」地址可看服务状态与日报（自签证书需信任一次）。",
        "=" * 62,
    ])
    log.info("\n%s\n", banner)

    # 补跑扫描：开机先补积压（关机/离线期间滞留的段），之后周期兜底。
    # 处理不再只靠手机端 /api/done 事件——那套在补传历史日期时会永久漏数据。
    sweep_task = sweeper.start()
    # 本地 LLM 后端的窗口守护（2026-09-27）：mlx 常驻 3.6GB，改由 DH_AGG_WINDOWS
    # 窗口启停（延迟 20s 启动，避开与壳的 reconcileLLM 抢跑；壳只 reconcile 一次）。
    # 云端后端 / LM Studio / Ollama 不接管。
    backend_task = llm.start_backend_manager()
    # 桌面壳的回环工作台：**必须在初始化之后**才起。它一开端口就是可用的，
    # 否则会出现在「端口已通、数据库还没打开」的窗口里被壳内工作台连续 500。
    desktop_http = desktop_bridge.start()
    try:
        yield
    finally:
        if sweep_task is not None:
            sweep_task.cancel()
        if backend_task is not None:
            backend_task.cancel()
        desktop_bridge.stop(desktop_http)
    log.info("server shutting down")


app = FastAPI(title="Digital Human Corpus Server", version=VERSION, lifespan=lifespan)
app.include_router(router)
app.include_router(agent_router)
app.include_router(console_router)
# 实时对话（Hermes 对接，2026-09-10）：/api/talk/*
app.include_router(talk_router)
