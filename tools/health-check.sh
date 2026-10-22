#!/bin/bash
# 健康快照（观察期用）：一次采集服务/内存/日志/数据/磁盘/模型状态，追加一行到 health.log。
#
# 用法：
#   tools/health-check.sh            # 采集一次
#   tools/health-check.sh --tail 20  # 采集后打印最近 20 行，便于人工浏览
#
# 观察期（2026-10-21 起 2 天）由定时任务每 4 小时调一次，汇总见：
#   ~/Library/Application Support/DigitalHuman/logs/health.log

set -u
TS=$(date '+%Y-%m-%d %H:%M')
DATA="$HOME/Library/Application Support/DigitalHuman"
LOG="$DATA/logs/api.log"
OUT="$DATA/logs/health.log"
DB="$DATA/data/tenants/default/digitalhuman.db"
TODAY=$(date '+%Y-%m-%d')

mkdir -p "$DATA/logs"

# 1) 服务健康（返回体里含 "ok":true 即正常；bad token 也说明服务在跑）
HEALTH=$(curl -sk --noproxy '*' -m 5 https://127.0.0.1:8766/api/agent/health 2>/dev/null | tr -d '\n' | cut -c1-46)
[ -z "$HEALTH" ] && HEALTH="UNREACHABLE"

# 2) 进程内存（壳 / API / 本地模型，RSS）
MEM=""
for pid in $(pgrep -f "DigitalHumanApp|server/run\.py|mlx_lm\.server" 2>/dev/null); do
  rss=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -z "$rss" ] && continue
  name=$(ps -o comm= -p "$pid" 2>/dev/null | xargs basename 2>/dev/null)
  case "$name" in
    python3) name=$(ps -o command= -p "$pid" 2>/dev/null | grep -q 'mlx_lm' && echo mlx || echo api) ;;
  esac
  MEM="${MEM}${name}=$((rss/1024))MB "
done
[ -z "$MEM" ] && MEM="NONE"

# 3) 本次运行（最后一次 server ready 之后）的 ERROR / WARNING 计数
START=$(grep -n "server ready" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
if [ -n "${START:-}" ]; then
  ERRS=$(tail -n +"$START" "$LOG" 2>/dev/null | grep -cE " ERROR ")
  WARNS=$(tail -n +"$START" "$LOG" 2>/dev/null | grep -cE " WARNING ")
else
  ERRS="?"; WARNS="?"
fi

# 4) 当日数据：总段 / 已处理 / 全库待转写积压
SEGS=$(sqlite3 "$DB" "SELECT count(*) FROM uploaded_segment WHERE date='$TODAY';" 2>/dev/null)
PROC=$(sqlite3 "$DB" "SELECT coalesce(sum(processed),0) FROM uploaded_segment WHERE date='$TODAY';" 2>/dev/null)
PEND=$(sqlite3 "$DB" "SELECT count(*) FROM uploaded_segment WHERE processed=0 AND is_speech=1;" 2>/dev/null)
REPORT=$(sqlite3 "$DB" "SELECT CASE WHEN ready=1 THEN 'ready('||length(raw)||'/'||length(clean)||'/'||length(summary)||')' ELSE 'none' END FROM report WHERE date='$TODAY';" 2>/dev/null)

# 5) 本地模型状态 + 磁盘可用 + 处理窗口/阈值
MLX=$(pgrep -f 'mlx_lm\.server' >/dev/null 2>&1 && echo on || echo off)
DISK=$(df -h "$HOME" 2>/dev/null | tail -1 | awk '{print $4}')
WIN=$(grep -E "^DH_AGG_WINDOWS=" "$DATA/env.local" 2>/dev/null | cut -d= -f2- | tr -d ' ')
BATCH=$(grep -E "^DH_AGG_BATCH_MIN=" "$DATA/env.local" 2>/dev/null | cut -d= -f2- | tr -d ' ')

LINE="$TS | health=${HEALTH} | mem=[${MEM% }] | err=${ERRS} warn=${WARNS} | today=${SEGS:-0}段/${PROC:-0}处理 | pending=${PEND:-?} | report=${REPORT:-none} | mlx=$MLX | win=${WIN:-空} batch=${BATCH:-?} | disk=$DISK"
echo "$LINE" >> "$OUT"
echo "$LINE"

if [ "${1:-}" = "--tail" ]; then
  echo "--- 最近 $2 行 ---"
  tail -n "${2:-20}" "$OUT"
fi
