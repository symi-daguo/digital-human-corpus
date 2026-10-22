"""启动脚本：python run.py（需在 server/ 目录下执行）"""
from __future__ import annotations

import logging.config
import os

import uvicorn

from app import config


def drop_proxy_env() -> None:
    """清掉进程内的代理环境变量。

    本服务的对端只有两类：手机（同局域网，直连）和本地 LLM（127.0.0.1）。
    两者都不该经过 HTTP(S)_PROXY。若启动服务的终端注入了代理，urllib 会默认
    读取这些变量，把 127.0.0.1 的请求也丢给代理 → 502 → LLM 静默返回空串，
    表现为「干净稿与总结全空白」而日志只有一行 502，极难定位。
    这里从源头清掉，比在每个调用点各自绕代理更不容易漏。
    """
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        if os.environ.pop(k, None):
            # 不要 print——启动后 Swift 外壳不重定向 stdout，print 会丢
            print(f"[run] 已移除代理环境变量 {k}（本地服务不走代理）")


# 单文件上限 20 MB × 5 份 = 100 MB 硬上限。20 MB 在 INFO 级下大约能装下
# 一整天的转写 + 全部 HTTP 访问 + pipeline 节点日志，超出后自动归档：
# api.log → api.log.1 → api.log.2 → ... → api.log.5，超出的直接丢。
# 100 MB 是为了把日志对 SSD 的写量限制在"诊断足够 + 不会涨满硬盘"的范围内，
# 几 GB 的 SSD 上这个量级不到 0.01%，不会影响寿命。
_LOG_MAX_BYTES = 20 * 1024 * 1024
_LOG_BACKUP_COUNT = 5

# 过滤噪声端点的访问日志（2026-09-07 审查发现）：菜单栏 app 每 3 秒轮询
# /api/agent/health，一天写 28k 行 200 记录，把真问题淹没在噪音里。
# 把它从 INFO 静默档降到 WARNING 之下 → 默认不再记。
# 故障排查确实需要看心跳时：临时把该 logger 调到 INFO 即可。
# 高频且正常时无排查价值的访问路径：2xx 静默（非 2xx 仍放行）。
# /api/upload 准实时上传每段一条、/api/report 手机轮询拉日报、/api/console/state
# 工作台轮询——会话密集时三项合计每分钟几十条，全量记录只会把日志刷屏，
# 真正要看的 POST /api/done、POST /api/mark 与所有非 2xx 都会照常记录。
_QUIET_ACCESS_PATHS = (
    "/api/agent/health", "/api/health",
    "/api/upload/", "/api/report/", "/api/console/state",
)


class _HealthCheckFilter(logging.Filter):
    """过掉高频心跳请求的访问日志，只放过异常路径。

    必须挂在 uvicorn.access 上而不是在 logger 级别（那是 INFO/WARNING 颗粒度），
    按 message 内容逐行过滤——uvicorn 的访问日志格式固定为
    '{client_addr} - "{request_line}" {status_code}'，按路径前缀匹配即可。
    4xx/5xx 不论端点一律保留，否则真错误会被这条规则吃掉。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        msg = record.getMessage()
        if any(p in msg for p in _QUIET_ACCESS_PATHS):
            # 只对 2xx 静默；非 2xx 一律放行（状态码是 msg 的最后一个空格段）
            tail = msg.rsplit(" ", 1)[-1]
            if tail.isdigit() and 200 <= int(tail) < 400:
                return False
        return True


def _configure_logging() -> None:
    """接管 uvicorn 与 app.* 所有 logger，统一走大小轮转。

    不接管的后果：api.log 一直追加，几天就能涨到几百 MB，把 SSD 写花。
    接管后：超过阈值自动归档到 api.log.1，保留最近 N 份。

    路径由 config.LOG_FILE 决定——必须与 Swift 外壳的 logsRoot 一致，
    否则就是两份日志写在两个不同位置。
    """
    log_path = config.LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "rotating": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_path),
                "maxBytes": _LOG_MAX_BYTES,
                "backupCount": _LOG_BACKUP_COUNT,
                "encoding": "utf-8",
                "formatter": "default",
            },
        },
        "loggers": {
            "uvicorn":       {"handlers": ["rotating"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["rotating"], "level": "INFO", "propagate": False},
            "uvicorn.access":{"handlers": ["rotating"], "level": "INFO", "propagate": False},
            "app":           {"handlers": ["rotating"], "level": "INFO", "propagate": False},
        },
        # 兜底：任何新写的 logger 也走轮转而不是 stdout，避免漏网
        "root": {"handlers": ["rotating"], "level": "WARNING"},
    })
    # dictConfig 的 filters 段不能直接引用类对象（必须是 importable 路径），
    # 这里改成直接注册实例；同时把过滤器装到访问日志 logger 与 handler 上，
    # 双保险——handler 漏装时 logger 还有一道。
    import logging as _logging
    hc_filter = _HealthCheckFilter()
    handler = _logging.getLogger().handlers[0]
    handler.addFilter(hc_filter)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _logging.getLogger(name).addFilter(hc_filter)


def main() -> None:
    drop_proxy_env()
    _configure_logging()
    # 必须先于 uvicorn 拿到证书路径：证书不存在时在这里生成，
    # 否则 uvicorn 会因 ssl_certfile 指向空文件而启动失败。
    cert, key = config.ensure_tls()
    config.ensure_dirs()
    kwargs: dict = {
        "host": config.HOST,
        "port": config.PORT,
        "log_level": "info",
        "ssl_certfile": cert,
        "ssl_keyfile": key,
        "log_config": None,    # 用我们上面 dictConfig 的结果，不要 uvicorn 默认的 stdout 配置
    }
    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    main()
