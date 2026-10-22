#!/usr/bin/env bash
# 由 Swift 外壳调用。所有路径相对本文件解析，因此 .app 放在任何位置都可用。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$RES/site-packages"
export PYTHONHOME="$RES/python"
export PYTHONNOUSERSITE=1
# bundle 签名后只读，.pyc 必须落到可写目录；放 Caches 符合 macOS 规范。
# 有些受管环境会注入 PYTHONDONTWRITEBYTECODE=1，导致缓存静默失效、
# 每次启动都全量重新编译——显式清掉。
unset PYTHONDONTWRITEBYTECODE
export PYTHONPYCACHEPREFIX="$HOME/Library/Caches/DigitalHuman/pycache"
exec "$RES/python/bin/python3" "$HERE/run.py"
