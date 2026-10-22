#!/bin/bash
# 数字人语料服务管理  用法: ./ctl.sh status|start|stop|restart|mem|log
URL=https://127.0.0.1:8766/api/health
SRV=$(cd "$(dirname "$0")" && pwd)
PIDFILE="$SRV/data/keepalive.pid"
PY="$SRV/.venv/bin/python"

# --noproxy '*' 不可省：环境若注入 HTTP_PROXY，curl 访问 127.0.0.1 会被代理
# 挡回 502，于是 status 明明在跑却报「未运行」，start 又会去重复拉起一个实例。
alive() { curl -sk --noproxy '*' -m 2 "$URL" >/dev/null 2>&1; }

case "$1" in
  start)
    if alive; then echo "服务已在运行，无需启动"; exit 0; fi
    nohup "$SRV/keepalive.sh" >> "$SRV/data/keepalive.log" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    alive && echo "已启动（守护 PID $(cat "$PIDFILE")，崩溃自动拉起）" || echo "启动中，请 10 秒后用 status 确认"
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE"; fi
    DH_SRV_DIR="$SRV" "$PY" -c '
import psutil, glob, os, signal
srv = os.environ.get("DH_SRV_DIR", "")  # 由外层 shell 的 $SRV 传入
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        cl = p.info["cmdline"] or []
        cwd = p.cwd()
        if len(cl) >= 2 and cl[-1] == "run.py" and cwd == srv and p.info["pid"] != os.getpid():
            p.send_signal(signal.SIGTERM); print("已停止服务进程", p.info["pid"])
    except Exception: pass
' 
    pkill -f "keepalive.sh" 2>/dev/null
    sleep 1
    alive && echo "仍有进程在响应，请重试" || echo "已完全停止"
    ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  status)
    alive && echo "服务: 运行中" || echo "服务: 未运行"
    # 守护检测不依赖 pidfile：无论 keepalive 由 ctl.sh / 终端 / 开机自启哪种方式拉起都算
    if pgrep -f "keepalive.sh" >/dev/null 2>&1; then
      echo "守护: 运行中 (PID $(pgrep -f "keepalive.sh" | tr '\n' ' '))"
    else
      echo "守护: 未运行（服务挂了不会自动拉起）"
    fi
    ;;
  mem)
    DH_SRV_DIR="$SRV" "$PY" -c '
import psutil, glob, os
srv = os.environ.get("DH_SRV_DIR", "")  # 由外层 shell 的 $SRV 传入
found = False
for p in psutil.process_iter(["pid", "cmdline", "memory_info"]):
    try:
        cl = p.info["cmdline"] or []
        cwd = p.cwd()
        if len(cl) >= 2 and cl[-1] == "run.py" and cwd == srv and p.info["pid"] != os.getpid():
            print("服务进程 PID=%d  RSS=%.0f MB" % (p.info["pid"], p.info["memory_info"].rss / 1048576))
            found = True
    except Exception: pass
print("结果: " + ("见上" if found else "服务未运行"))
'
    ;;
  log) tail -20 "$SRV/data/server.log" 2>/dev/null; echo "--- keepalive ---"; tail -5 "$SRV/data/keepalive.log" 2>/dev/null ;;
  *)   echo "用法: ./ctl.sh status|start|stop|restart|mem|log" ;;
esac
