#!/bin/bash
# 采集处理模式切换（实时 / 窗口）
#
# 用法：
#   tools/agg-mode.sh status      # 看当前模式和是否生效
#   tools/agg-mode.sh realtime    # 实时：上传即处理（测 Hermes 时用）
#   tools/agg-mode.sh window      # 窗口：13:00/17:45 或攒满 30 段才处理（平时/收工）
#
# 为什么不重启服务：
#   `DH_AGG_WINDOWS` / `DH_AGG_BATCH_MIN` 在设置页的字段定义里都标了 `hot: true`，
#   走 `PUT /api/console/config` 会调 `config._apply_env()` 并重置窗口解析缓存，
#   当前进程立刻生效。所以切换是**零中断**的——手机正在上传的段不受影响。
#
# 生效时机：
#   - `agg_active()` 立刻变（下次 trigger 就用新判定）
#   - mlx 的启停由 `llm.backend_window_manager` 每 60s 对一次，最多等 1 分钟
#
# 安全设计：
#   - 只改这两个键，不碰 env.local 其他内容（`set_env_values` 是逐键替换）
#   - 切换后立刻回读校验，不一致就报错退出（不静默成功）
#   - 幂等：重复执行同一模式不会报错，也不会重复写

set -u

BASE="${DH_BASE:-https://127.0.0.1:8766}"
ENVF="$HOME/Library/Application Support/DigitalHuman/env.local"
MODE="${1:-status}"

# 读配对 token（env.local 里那一个，脚本不硬编码任何密钥）
TOKEN=$(grep -E '^DH_TOKEN=' "$ENVF" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ -z "${TOKEN:-}" ]; then
  echo "✗ 读不到 DH_TOKEN（${ENVF}）" >&2
  exit 1
fi

api() {  # api <method> <path> [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sk --noproxy '*' -m 15 -X "$method" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data "$body" "$BASE$path"
  else
    curl -sk --noproxy '*' -m 15 -X "$method" \
      -H "Authorization: Bearer $TOKEN" "$BASE$path"
  fi
}

# 从 config 响应里取某个键的当前值（jq 不在时退回 python3）
get_val() {  # get_val <json> <key>
  /usr/bin/python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print(""); raise SystemExit
key = sys.argv[2]
vals = (d.get("values") or d.get("config", {}).get("values") or {})
# 有的实现把 values 放在 config 里，扁平结构也兼容
if isinstance(vals, dict) and key in vals:
    print(vals[key]); raise SystemExit
def walk(o):
    if isinstance(o, dict):
        if o.get("name") == key:
            v = o.get("value")
            print("" if v is None else v); return True
        for x in o.values():
            if walk(x): return True
    elif isinstance(o, list):
        for x in o:
            if walk(x): return True
    return False
walk(d)
PY
}

show_status() {
  local resp
  resp=$(api GET /api/console/config)
  local win batch
  win=$(get_val "$resp" DH_AGG_WINDOWS)
  batch=$(get_val "$resp" DH_AGG_BATCH_MIN)
  echo "  当前配置："
  echo "    DH_AGG_WINDOWS   = '${win}'   $([ -z "$win" ] && echo '（空 → 不等窗口）' || echo '（只在这些时间处理）')"
  echo "    DH_AGG_BATCH_MIN = '${batch}'  $([ "${batch:-1}" = "0" ] && echo '（0 → 关掉数量触发）' || echo "（攒满 $batch 段也处理）")"
  if [ -z "$win" ] && [ "${batch:-1}" = "0" ]; then
    echo "  判定：**实时模式**（上传即处理）"
  elif [ -n "$win" ] && [ "${batch:-1}" != "0" ]; then
    echo "  判定：**窗口 + 数量**（窗口内或攒满 ${batch} 段才处理）"
  elif [ -n "$win" ]; then
    echo "  判定：**仅窗口**（只在 ${win} 处理）"
  else
    echo "  判定：**仅数量**（攒满 ${batch} 段才处理）"
  fi
  # 服务进程内真实判定（配置值 ≠ 生效值，以这里为准）
  echo "  服务进程实际判定："
  curl -sk --noproxy '*' -m 10 "$BASE/api/agent/health" 2>/dev/null \
    | /usr/bin/python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); a=d.get('agg') or {}; l=d.get('llm') or {}
    print(f\"    agg.active={a.get('active')} in_window={a.get('in_window')} windows={a.get('windows')!r} batch={a.get('batch_min')}\")
    print(f\"    llm={l.get('backend')}/{l.get('model')}\")
except Exception as e:
    print('    (health 解析失败:', e, ')')
" 2>/dev/null || true
}

apply_mode() {  # apply_mode <label> <windows> <batch>
  local label="$1" win="$2" batch="$3"
  local body
  body=$(/usr/bin/python3 -c "
import json,sys
print(json.dumps({'values': {'DH_AGG_WINDOWS': sys.argv[1], 'DH_AGG_BATCH_MIN': sys.argv[2]}}, ensure_ascii=False))
" "$win" "$batch")
  local resp
  resp=$(api PUT /api/console/config "$body")
  if ! echo "$resp" | grep -q '"ok": *true'; then
    echo "✗ 写入失败：$(echo "$resp" | head -c 200)" >&2
    exit 1
  fi
  local restart
  restart=$(echo "$resp" | /usr/bin/python3 -c "
import json,sys
try: print(','.join(json.load(sys.stdin).get('restart_required') or []))
except Exception: print('')
" 2>/dev/null)
  echo "  ✓ 已切到「${label}」"
  [ -n "$restart" ] && echo "  ⚠ 需要重启才生效的字段：${restart}" || echo "  · 热更新生效，无需重启"

  # 回读校验（不轻信写入返回值）
  sleep 1
  local check win2 batch2
  check=$(api GET /api/console/config)
  win2=$(get_val "$check" DH_AGG_WINDOWS)
  batch2=$(get_val "$check" DH_AGG_BATCH_MIN)
  if [ "$win2" = "$win" ] && [ "$batch2" = "$batch" ]; then
    echo "  ✓ 回读校验通过（WINDOWS='$win2' BATCH='$batch2'）"
  else
    echo "  ✗ 回读不一致！期望 WINDOWS='${win}' BATCH='${batch}'，实际 WINDOWS='${win2}' BATCH='${batch2}'" >&2
    exit 1
  fi
  echo "  · mlx 启停由 60s 守护循环对齐，最多 1 分钟后到位"
}

case "$MODE" in
  status)
    echo "== 采集处理模式 =="
    show_status
    ;;
  realtime)
    echo "== 切实时模式（上传即处理，测 Hermes 时用）=="
    apply_mode "实时" "" "0"
    echo
    show_status
    ;;
  window)
    echo "== 切窗口模式（13:00/17:45 或攒满 30 段，平时/收工用）=="
    apply_mode "窗口+数量" "13:00,17:45" "30"
    echo
    show_status
    ;;
  *)
    echo "用法：$0 {status|realtime|window}" >&2
    exit 2
    ;;
esac
