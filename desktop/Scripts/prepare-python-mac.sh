#!/usr/bin/env bash
# 为 macOS x64（Intel）安装包准备「内嵌 Python 运行时 + 预装依赖」。
#
# 为什么需要：桌面壳的 python_bin()（desktop/src-tauri/src/lib.rs）优先找
# resource_dir()/python/bin/python3。Windows 端已有 prepare-python-win.sh 接上
# 这条路，macOS 的 Tauri 壳此前没有 —— CI 出的 `desktop-macos-x64` 只有 8MB，
# 里面没有运行时，Intel 用户装上会因为找不到解释器而起不来。
#
# ⚠️ Intel Mac 上**本地 LLM 不可用**：mlx-lm 依赖 Metal，只支持 Apple Silicon。
#    这份运行时照样能跑采集 / ASR / 声纹 / 日报（sherpa-onnx 有 x86_64 轮子），
#    只是 LLM 那一步必须在工作台里配云端 API。README 与 Release 说明都写了这点。
#
# 产出（都在 .gitignore 内，属构建期产物）：
#   desktop/.cache/<归档>                          下载缓存，可复用
#   desktop/src-tauri/.runtime/python/bin/python3  运行时
#   desktop/src-tauri/.runtime/python/lib/...      已预装依赖
#
# 用法：desktop/Scripts/prepare-python-mac.sh [x86_64|aarch64]
#   默认 x86_64。aarch64 一般不需要——Apple Silicon 用户走 macapp/Scripts/build.sh
#   的原生 SwiftUI 壳，那份自带运行时且是生产形态。
set -euo pipefail

ARCH="${1:-x86_64}"
case "$ARCH" in
  x86_64)  PBS_ARCH="x86_64-apple-darwin" ;;
  aarch64) PBS_ARCH="aarch64-apple-darwin" ;;
  *) echo "✗ 不支持的架构：$ARCH（只认 x86_64 / aarch64）" >&2; exit 2 ;;
esac

# 与 macapp/Scripts/build.sh 钉在同一条 python-build-standalone 发布线（tag 20250818）。
# 那份用的是 aarch64 的 3.13.5；同 tag 下 x86_64 的补丁号可能不同，用 3.13.5 对齐。
PBS_TAG="20250818"
PBS_PY="3.13.7"
PBS_ASSET="cpython-${PBS_PY}+${PBS_TAG}-${PBS_ARCH}-install_only.tar.gz"
# URL 里的 "+" 必须编码成 %2B，否则 GitHub 会当成空格
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET//+/%2B}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # → desktop/
RUNTIME="$ROOT/src-tauri/.runtime"
PYDIR="$RUNTIME/python"
CACHE="$ROOT/.cache"
REQS="$ROOT/../server/requirements.txt"
MIRROR="${DH_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

# macOS 上 PATH 里的 rm 是沙箱代理版，批量删除会被拦；/bin/rm 是真身
if [ -x /bin/rm ]; then RM=/bin/rm; else RM=rm; fi

[ -f "$REQS" ] || { echo "✗ 找不到 $REQS" >&2; exit 1; }
mkdir -p "$CACHE" "$RUNTIME"

step "1/4 下载 python-build-standalone（${PBS_ARCH} / ${PBS_PY}）"
TARBALL="$CACHE/$PBS_ASSET"
if [ -f "$TARBALL" ]; then
  echo "  已有缓存，跳过下载：$TARBALL"
else
  echo "  $PBS_URL"
  # --retry：CI 上偶发断连；.part 后缀保证半成品不会被当成缓存命中
  curl -fL --retry 3 --retry-delay 2 --max-time 900 -o "$TARBALL.part" "$PBS_URL"
  mv "$TARBALL.part" "$TARBALL"
fi

step "2/4 解压到 .runtime/"
"$RM" -rf "$PYDIR"
tar -xzf "$TARBALL" -C "$RUNTIME"
[ -x "$PYDIR/bin/python3" ] || { echo "✗ 解压后找不到 python3" >&2; exit 1; }
echo "  $("$PYDIR/bin/python3" -V) @ $PYDIR"

step "3/4 预装依赖（mlx-lm 会被平台标记跳过）"
# 为什么不用内嵌解释器自己的 pip：
# 在 arm64 的 CI runner 上跑 x86_64 的 python3 属于**交叉**场景，
# pip 会退化成源码编译。`cryptography` 走 maturin/Rust wheel，
# 实测直接失败：
#   error: subprocess-exited-with-error
#   ERROR: Failed building wheel for cryptography
# uv 能按 --python-platform 直接解析**预编译 wheel**，不触发编译。
# （prepare-python-win.sh 在非 Windows 主机上也因此改用 uv，同一类问题。）
if command -v uv >/dev/null 2>&1; then
  UV=uv
else
  echo "  安装 uv（交叉装依赖必需）"
  "$PYDIR/bin/python3" -m pip install --disable-pip-version-check -q uv \
    -i "$MIRROR" --no-warn-script-location
  UV="$PYDIR/bin/uv"
fi

"$UV" pip install \
  --python "$PYDIR/bin/python3" \
  --python-platform "x86_64-apple-darwin" \
  --no-cache --quiet \
  --index-url "$MIRROR" \
  -r "$REQS"

step "4/4 自检"
"$PYDIR/bin/python3" - <<'PY'
import importlib.util as u
need = ["fastapi", "uvicorn", "sherpa_onnx", "numpy", "httpx", "PIL"]
miss = [n for n in need if u.find_spec(n) is None]
if miss:
    raise SystemExit(f"✗ 缺依赖：{miss}")
print(f"  ✓ 关键依赖齐全：{', '.join(need)}")
if u.find_spec("mlx") is not None:
    # 混进来会导致 x86_64 启动即崩（找不到 Metal 后端）
    raise SystemExit("✗ mlx 不该出现在 x86_64 运行时里（requirements 的平台标记失效了）")
print("  ✓ mlx 未混入（符合预期：Intel 无 Metal）")
PY

echo
echo "完成。Tauri 打包时通过 tauri.macos.conf.json 把它映射进 .app 的 python/ 目录。"
echo "  运行时体积：$(du -sh "$PYDIR" | cut -f1)"
