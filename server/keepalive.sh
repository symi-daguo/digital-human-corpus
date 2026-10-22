#!/bin/bash
# 数字人语料服务守护循环：每 30s 检查 API 与 LLM，挂了自动拉起。
# 由 start-all.sh exec 进入（进程名就是本脚本），也可单独手工跑。
# Ctrl+C 或 ctl.sh stop 结束。
cd "$(dirname "$0")"
SRV_DIR="$(pwd -P)"
[ -f env.local ] && source env.local
mkdir -p data
LLM_PORT="${DH_LLM_PORT:-1235}"
LLM_MODEL_DIR="${DH_LLM_SERVED_ID:-}"
API_PORT="${DH_PORT:-8766}"

# 每轮循环重读 env.local（2026-09-05 修复）。
# 原来只在脚本开头 source 一次：keepalive 由 LaunchAgent 常驻，环境变量在它启动
# 那一刻就固化了，之后无论改多少次 env.local，它拉起的 run.py 都还是旧配置——
# 表现为「明明改了 DH_ASR_HOTWORDS，/api/console/state 里还是旧热词」。
# 放进循环里，改完配置 30 秒内自动生效，不必重启 LaunchAgent job。
reload_env() {
  [ -f env.local ] && source env.local
  LLM_PORT="${DH_LLM_PORT:-1235}"
  LLM_MODEL_DIR="${DH_LLM_SERVED_ID:-}"
  API_PORT="${DH_PORT:-8766}"
  AGG_WINDOWS="${DH_AGG_WINDOWS-13:00,17:45}"
  AGG_SPAN_MIN="${DH_AGG_SPAN_MIN-90}"
}

# 统一的本地探测 curl。不能用「把命令存进变量」的写法：$CURL 展开时裸 *
# 会被 glob 成当前目录文件列表（实测 curl 参数变成 README.md app data...），
# 靠 launchd 无代理环境才侥幸可用。函数 + 引号 '*' 才是正确写法。
hc() { curl -sk --noproxy '*' -m 3 "$@"; }

echo "[$(date '+%F %T')] keepalive 启动 (PID $$)" >> data/keepalive.log

llm_ready() {
  # 就绪判定不能只看端口：mlx-lm 进程起来后仍要加载模型，/v1/models 会先返回空列表。
  # 必须确认返回体里确实带模型 id，否则管线第一次调用就会拿到「无模型」。
  [ -n "$LLM_MODEL_DIR" ] || return 0
  hc "http://127.0.0.1:${LLM_PORT}/v1/models" 2>/dev/null | grep -q "$(basename "$LLM_MODEL_DIR")"
}

start_llm() {
  log() { echo "[$(date '+%F %T')] $*" >> data/keepalive.log; }
  log "LLM 不可达，重新拉起 mlx-lm (port ${LLM_PORT})"
  nohup "$SRV_DIR/.venv/bin/python" -m mlx_lm.server \
      --model "$LLM_MODEL_DIR" --port "$LLM_PORT" --host 127.0.0.1 \
      >> data/llm.log 2>&1 &
}

# 处理窗口判定（2026-09-27，与 server/app/config.py 的 in_agg_window() 同一套语义）：
# 窗口时刻起 AGG_SPAN_MIN 分钟内算「窗口内」；DH_AGG_WINDOWS 为空 = 实时模式（恒为内）。
# 窗口外要把 mlx-lm 停掉释放内存（Qwen3.5-4B 常驻 ≈3.6GB，是本机最大的单项占用）——
# 不能只靠「不拉起」：keepalive 的旧逻辑每 30s 都会把停掉的 LLM 拉回来，等于白停。
# ⚠️ run.py（API/上传/对话）不受窗口影响，只有 LLM 与处理管线按窗口启停。
agg_active() {
  [ -z "$AGG_WINDOWS" ] && return 0
  local nowm=$(( $(date +%H) * 60 + 10#$(date +%M) ))
  local w hh mm s e
  for w in $(printf '%s' "$AGG_WINDOWS" | sed 's/，/,/g; s/：/:/g' | tr ',' ' '); do
    w=$(printf '%s' "$w" | sed 's/ //g')
    hh="${w%%:*}"; mm="${w##*:}"
    case "$hh" in ''|*[!0-9]*) continue ;; esac
    case "$mm" in ''|*[!0-9]*) continue ;; esac
    s=$(( 10#$hh * 60 + 10#$mm ))
    e=$(( s + AGG_SPAN_MIN ))
    if [ "$nowm" -ge "$s" ] && [ "$nowm" -lt "$e" ]; then return 0; fi
  done
  return 1
}

_llm_running() { pgrep -f "mlx_lm.server" >/dev/null 2>&1; }

# 窗口状态只在实际翻转时记日志（循环 30s 一轮，不节流会刷爆 keepalive.log）
LLM_GUARD_STATE=""

window_guard_llm() {
  if agg_active; then
    if [ "$LLM_GUARD_STATE" != "in" ]; then
      log "进入处理窗口 ($AGG_WINDOWS ±${AGG_SPAN_MIN}min)，LLM 守护开启"
      LLM_GUARD_STATE="in"
    fi
    if ! llm_ready; then
      start_llm
    fi
  else
    if [ "$LLM_GUARD_STATE" != "out" ]; then
      log "窗口外：LLM 守护暂停，停掉 mlx-lm 释放内存（窗口内自动拉起）"
      LLM_GUARD_STATE="out"
    fi
    if _llm_running; then
      pkill -f "mlx_lm.server"
      sleep 1
      if _llm_running; then
        log "警告：mlx-lm 停止失败，下轮重试"
      else
        log "mlx-lm 已停止（窗口外），释放内存"
      fi
    fi
  fi
}

while true; do
  reload_env
  # --noproxy '*' 不可省：本机若注入 HTTP_PROXY，curl 访问 127.0.0.1 会走代理拿到 502，
  # 健康检查因此误判「服务已死」而反复重启 run.py（端口占用→退出→再拉起）的死循环。
  # LLM 守护（2026-09-27 窗口化）：只在处理窗口内保证 mlx-lm 存活；窗口外主动停掉
  # 释放内存。窗口内挂了仍会自动拉起（原逻辑保留）。
  window_guard_llm
  if ! hc "https://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1; then
    echo "[$(date '+%F %T')] 服务不可达，拉起 run.py" >> data/keepalive.log
    ./.venv/bin/python run.py >> data/server.log 2>&1
    echo "[$(date '+%F %T')] run.py 退出码 $?" >> data/keepalive.log
    # 拉起失败（典型：端口已被占用）时给退避，避免 30s 内疯狂重启刷爆日志
    sleep 5
  fi
  sleep 30
done
