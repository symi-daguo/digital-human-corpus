"""桌面壳回环入口：给「窗口内嵌工作台」再开一个只绑 127.0.0.1 的明文 HTTP。

## 为什么需要它

桌面壳（`desktop/`，Tauri）要在自己的窗口里显示工作台，而不是把用户丢给浏览器。
但主服务恒定带 `ssl_certfile/ssl_keyfile`（见 `run.py`），只监听 TLS，而
Tauri 用的 wry/WebView2/WKWebView **没有暴露任何证书例外 API**（实测 wry 全仓
`certificate` 只命中 dev server 的 root-certificate-path，没有任何
`ignore_certificate` 之类入口）。WebView 碰到自签证书的处理是「导航失败」——
窗口白屏，且没有浏览器那种「高级 → 继续访问」的按钮。所以壳内不能加载 8766。

于是这里额外起一台**明文** HTTP，绑定地址写死回环：

    http://127.0.0.1:8767/console   ← 桌面壳窗口内加载这个

## 为什么是「另一台 Server」而不是「改现有那台」

主链路上有两条不能碰的既有行为：手机端走 https + 证书指纹 pin；局域网需要
`0.0.0.0` 可访问。把 8766 改成明文会同时打断这两条。所以新增而不是替换。

## 硬约束（改这里之前先看）

1. **必须在主服务完成初始化之后再开**（由 `main.py` 的 lifespan 调用）。
   本模块自己不做初始化：它承载的是同一个 ASGI app，lifespan 会再跑一次。
2. **同一进程内初始化只允许跑一次**。`lifespan` 里的 `sweeper.start()` 是在
   *当前事件循环* 建任务；两台 Server 各有一个事件循环，不设防就会有两个补跑
   扫描任务同时扫同一个库。防重入在 `main.py`（`_lifespan_owner`）。
3. **`log_config=None` 必须显式传**。否则 uvicorn 会用自带的 stdout 配置覆盖
   `run.py::_configure_logging()` 的轮转接管——表现是「日志忽然开始往 stdout
   刷、api.log 停止增长」，非常难联想到是这一行。
4. 线程必须是 daemon：主服务退出时进程要能立刻结束，不能被它吊住。
"""
from __future__ import annotations

import logging
import socket
import threading

import uvicorn

from . import config

log = logging.getLogger(__name__)

# 关闭时给线程的收尾时限（秒）。只是关一个本地监听，正常是毫秒级；
# 给 3 秒是为了不让「关不掉的线程」把整个进程的退出拖住。
_JOIN_TIMEOUT_SEC = 3.0


def _port_available(host: str, port: int) -> tuple[bool, str]:
    """探一次端口。返回 (是否可绑, 失败原因)。

    为什么要先探：端口被上次没退干净的进程占着时，uvicorn 会在**另一个线程**里
    `sys.exit(1)` 并打一串 traceback 到这种「明明能用只是没开」的场景上，
    把日志搅浑。先探一下就能给出一行中文说明，且壳侧本来就有降级路径
    （回退到系统浏览器打开 https 工作台），不影响可用性。
    """
    probe = socket.socket()
    try:
        probe.bind((host, port))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        probe.close()


def start() -> "uvicorn.Server | None":
    """启动回环入口。返回 Server 实例（供关闭时置 should_exit），未启用/失败返回 None。"""
    if not config.DESKTOP_HTTP_ENABLED:
        return None

    host, port = config.DESKTOP_HTTP_HOST, config.DESKTOP_HTTP_PORT
    ok, why = _port_available(host, port)
    if not ok:
        log.warning("[desktop] 回环工作台 %s:%s 不可用（%s），已跳过；"
                    "桌面壳会回退到系统浏览器打开工作台", host, port, why)
        return None

    server = uvicorn.Server(uvicorn.Config(
        # 与 run.py 同一份 app 字符串：uvicorn 走模块缓存，两台 Server 拿到的是
        # **同一个** FastAPI 实例，因此 /api/console/* 读到的就是主服务的状态。
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        log_config=None,   # 见模块 docstring 第 3 条，别删
    ))
    threading.Thread(
        target=server.run, name="desktop-loopback-http", daemon=True,
    ).start()
    log.info("[desktop] 回环工作台已启动 http://%s:%s/console（仅本机可访问）", host, port)
    return server


def stop(server: "uvicorn.Server | None") -> None:
    """请求回环入口退出。幂等，传 None 直接返回（未启用时的正常路径）。"""
    if server is None:
        return
    server.should_exit = True
    for t in threading.enumerate():
        if t.name == "desktop-loopback-http":
            t.join(timeout=_JOIN_TIMEOUT_SEC)
            break
