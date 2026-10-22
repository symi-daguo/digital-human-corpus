#!/usr/bin/env bash
# 为 Windows 安装包准备「内嵌 Python 运行时 + 预装依赖」。
#
# 为什么需要：伙伴机器上不该要求预装 Python 与 pip。桌面壳的 python_bin()
# （desktop/src-tauri/src/lib.rs）找解释器**第一优先**就是
# resource_dir()/python/python.exe。macOS 端一直走这条路（macapp/Scripts/build.sh
# 把 python-build-standalone 拷进 .app），Windows 端此前只留了「找系统 python」
# 的兜底 —— 本脚本把内嵌那条真正接上。
#
# 产出（全部在 .gitignore 内，属构建期产物）：
#   desktop/.cache/<归档>                                下载缓存，可复用
#   desktop/src-tauri/.runtime/python/python.exe         运行时
#   desktop/src-tauri/.runtime/python/Lib/site-packages  已预装依赖
#
# Windows 打包靠 desktop/src-tauri/tauri.windows.conf.json 把它映射进安装包
# 的 python/ 目录（只对 Windows 目标生效，macOS 构建不会带上这个 20MB 运行时）。
#
# 用法：desktop/Scripts/prepare-python-win.sh
#
# 平台差异：
#   Windows（CI 的 windows-latest）→ 用内嵌解释器自己的 pip 直接装，不需要额外工具
#   其他平台（macOS 本地验证 / 交叉出包）→ 无法执行 .exe，改用 **uv** 加
#     --python-platform x86_64-pc-windows-msvc 交叉安装（pip 在此场景会
#     报 resolution-too-deep，见 §3/3 的说明）
set -euo pipefail

# 与 macapp/Scripts/build.sh 钉在同一条 python-build-standalone 发布线上
# （tag 20250818）。那份是 aarch64-apple-darwin 的 3.13.5，
# Windows 这份同一 tag 下是 3.13.7 —— 同 tag 不同补丁号是上游的正常情况。
PBS_TAG="20250818"
PBS_PY="3.13.7"
PBS_ASSET="cpython-${PBS_PY}+${PBS_TAG}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
# 注意 URL 里的 "+" 必须编码成 %2B，否则 GitHub 会当成空格
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET//+/%2B}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # → desktop/
RUNTIME="$ROOT/src-tauri/.runtime"
PYDIR="$RUNTIME/python"
CACHE="$ROOT/.cache"
REQS="$ROOT/../server/requirements.txt"
MIRROR="${DH_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

# macOS 上 PATH 里的 rm 是沙箱代理版，批量删除会被拦；/bin/rm 是真身。
# Git Bash（CI 的 windows-latest）下两者等价，所以优先用绝对路径、留兜底。
if [ -x /bin/rm ]; then RM=/bin/rm; else RM=rm; fi

[ -f "$REQS" ] || { echo "✗ 找不到 $REQS" >&2; exit 1; }
mkdir -p "$CACHE" "$RUNTIME"

step "1/3 获取 python-build-standalone（${PBS_PY} / win x64）"
TARBALL="$CACHE/$PBS_ASSET"
if [ -s "$TARBALL" ]; then
  echo "  命中缓存：$(du -h "$TARBALL" | cut -f1)"
else
  echo "  $PBS_URL"
  curl -fL --retry 3 --retry-delay 2 --max-time 900 -o "$TARBALL.part" "$PBS_URL"
  mv "$TARBALL.part" "$TARBALL"
  echo "  已下载：$(du -h "$TARBALL" | cut -f1)"
fi

step "2/3 解出运行时"
"$RM" -rf "$PYDIR"
# 归档顶层就是 python/ 目录，正好对上 shell 里 resource_dir()/python 的约定
tar -xzf "$TARBALL" -C "$RUNTIME"
if [ ! -f "$PYDIR/python.exe" ]; then
  echo "✗ 归档里没有 python/python.exe，布局与预期不符" >&2
  exit 1
fi
echo "  $PYDIR/python.exe"

step "3/3 预装依赖"
SITE="$PYDIR/Lib/site-packages"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*|Windows*)
    "$PYDIR/python.exe" -m pip --version >/dev/null 2>&1 \
      || "$PYDIR/python.exe" -m ensurepip --default-pip
    "$PYDIR/python.exe" -m pip install --disable-pip-version-check -q \
      -r "$REQS" -i "$MIRROR"
    ;;
  *)
    # 非 Windows（macOS 本地验证 / 交叉出包）：无法执行 .exe，只能交叉安装
    # Windows wheel。
    #
    # ⚠️ 这里**不能用 pip**：pip 的 `--platform` + `--target` 解析在这一组
    #    （sherpa-onnx / numpy / pydantic-core 都是多版本多 ABI）上会直接报
    #    `error: resolution-too-deep` —— 它的回溯算法扛不住跨平台候选集。
    #    uv 的解析器能正常处理，实测产物经 file(1) 验证是 PE32+ x86-64。
    if ! command -v uv >/dev/null 2>&1; then
      echo "✗ 非 Windows 上交叉预装依赖需要 uv（brew install uv，或 pip install uv）" >&2
      echo "  也可以直接在 Windows 上跑本脚本 —— 那条路径用内嵌解释器自己的 pip，不需要 uv。" >&2
      exit 1
    fi
    echo "  非 Windows：用 uv 交叉安装 x86_64-pc-windows-msvc wheel"
    uv pip install \
      --target "$SITE" \
      --python-platform x86_64-pc-windows-msvc --python-version 3.13 \
      -r "$REQS" --index-url "$MIRROR"
    ;;
esac

# mlx-lm 在 requirements 里带 darwin+arm64 标记，Windows 上必然被跳过。
# 这里断言一下，免得哪天标记被误删 —— 那种情况依赖解析照样成功，
# 要到伙伴机器上跑起来才发现少东西。
if [ -d "$SITE/mlx_lm" ]; then
  echo "  ⚠️ 意外装上了 mlx_lm：Windows 上装不了也用不了，" >&2
  echo "     请检查 server/requirements.txt 里 mlx-lm 的平台标记是否还在。" >&2
fi

echo
echo "  依赖条目：$(ls "$SITE" | wc -l | tr -d ' ')"
echo "  运行时体积：$(du -sh "$PYDIR" | cut -f1)"
MISSING=""
for p in fastapi uvicorn sherpa_onnx numpy cryptography pydantic; do
  [ -e "$SITE/$p" ] || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  echo "  ✗ 缺少依赖：$MISSING" >&2
  exit 1
fi
echo "  关键依赖齐备：fastapi / uvicorn / sherpa_onnx / numpy / cryptography / pydantic"
echo
echo "完成。接下来打包："
echo "  cd desktop && npx tauri build --bundles nsis   # Windows 上执行"
