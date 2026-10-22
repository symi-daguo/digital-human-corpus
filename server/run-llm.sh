#!/usr/bin/env bash
# 由 Swift 外壳调用。$1 = 模型目录，$2 = 端口
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$RES/site-packages"
export PYTHONHOME="$RES/python"
export PYTHONNOUSERSITE=1
unset PYTHONDONTWRITEBYTECODE
export PYTHONPYCACHEPREFIX="$HOME/Library/Caches/DigitalHuman/pycache"
# 必须是 mlx_lm.server 而不是 `mlx_lm server`：后者的子命令表（mlx_lm/cli.py）
# 里没有 server 这一项，会直接报 invalid choice。生产脚本 start-all.sh 用的是
# 前者，这里保持一致。
exec "$RES/python/bin/python3" -m mlx_lm.server \
  --model "$1" --port "${2:-1235}" --host 127.0.0.1
