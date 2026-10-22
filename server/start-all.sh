#!/bin/bash
# 数字人服务总启动入口：先拉起 LLM（mlx-lm server），再进入 API 服务守护循环。
# 由 LaunchAgent 调用，也可手工执行 ./start-all.sh
#
# 为什么要拆成两步：API 服务依赖 LLM 产出「干净稿/总结」。如果只起 API 而 LLM 未就绪，
# 管线会在 llm.available() 处判定不可用并跳过 NLP 步骤，日报只剩原始转写 ——
# 且日志只有一行 warning，用户看到的是「总结空白」而不是「服务没起来」。
set -u
cd "$(dirname "$0")"
SRV_DIR="$(pwd -P)"
[ -f env.local ] && source env.local
mkdir -p data

# 必需：本机环境可能注入 HTTP_PROXY，导致访问 127.0.0.1 走代理后 502，
# 健康检查因此误判「服务已死」而反复重启。所有本地探测一律 --noproxy。
# 不能写成 CURL="curl ... --noproxy * ..." 的变量形式：$CURL 展开时裸 *
# 会被 glob 成当前目录文件列表（实测 curl 参数变成 README.md app data...），
# 必须用函数 + 引号 '*'。
hc() { curl -s --noproxy '*' -m 5 "$@"; }

LLM_PORT="${DH_LLM_PORT:-1235}"
LLM_MODEL_DIR="${DH_LLM_SERVED_ID:-}"
AGG_WINDOWS="${DH_AGG_WINDOWS-13:00,17:45}"
AGG_SPAN_MIN="${DH_AGG_SPAN_MIN-90}"

log() { echo "[$(date '+%F %T')] $*" >> data/keepalive.log; }

llm_ready() {
  # 就绪判定不能只看端口：mlx-lm 进程起来后仍要加载模型，此时 /v1/models 会返回空列表。
  # 必须确认返回体里确实带模型 id，否则管线第一次调用就会拿到「无模型」。
  [ -n "$LLM_MODEL_DIR" ] || return 0
  hc "http://127.0.0.1:${LLM_PORT}/v1/models" 2>/dev/null | grep -q "$(basename "$LLM_MODEL_DIR")"
}

start_llm() {
  if llm_ready; then
    log "mlx-lm 已在运行，跳过启动"
    return 0
  fi
  if [ -z "$LLM_MODEL_DIR" ]; then
    log "未配置 DH_LLM_SERVED_ID，跳过 LLM 启动（去口气/总结将不可用）"
    return 0
  fi
  log "启动 mlx-lm server (port ${LLM_PORT}, model ${LLM_MODEL_DIR})"
  nohup "$SRV_DIR/.venv/bin/python" -m mlx_lm.server \
      --model "$LLM_MODEL_DIR" --port "$LLM_PORT" --host 127.0.0.1 \
      >> data/llm.log 2>&1 &
  local i
  for i in $(seq 1 90); do   # 最多等 3 分钟（4B 模型冷启动含权重加载）
    sleep 2
    if llm_ready; then
      log "mlx-lm 就绪（等待 $((i*2))s）"
      return 0
    fi
  done
  log "警告：mlx-lm 启动超时（180s），继续启动 API 服务（去口气/总结将不可用）"
  return 1
}

log "=== start-all 启动 (PID $$) ==="

# 处理窗口判定（与 keepalive.sh / server/app/config.py 同一套语义）：
# 窗口外开机就**不拉** LLM（Qwen3.5-4B 常驻 ≈3.6GB），交给 keepalive.sh 在窗口内拉起。
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

if agg_active; then
  start_llm
else
  log "窗口外启动（DH_AGG_WINDOWS=${AGG_WINDOWS}）：跳过 LLM，窗口内由 keepalive 拉起"
fi
exec ./keepalive.sh
