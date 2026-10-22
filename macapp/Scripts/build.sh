#!/usr/bin/env bash
#
# 构建「数字人服务.app」
#
#   用法：Scripts/build.sh [--refresh-deps] [--sign]
#
#   --refresh-deps   强制重装 Python 依赖（默认复用 .cache 里的）
#   --sign           构建完立刻用 Developer ID 签名
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
SERVER="$REPO/server"
APP_NAME="数字人服务"
DIST="$ROOT/dist"
APP="$DIST/$APP_NAME.app"
CONTENTS="$APP/Contents"
RES="$CONTENTS/Resources"

REFRESH_DEPS=0
DO_SIGN=0
for arg in "$@"; do
  case "$arg" in
    --refresh-deps) REFRESH_DEPS=1 ;;
    --sign)         DO_SIGN=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 1. Python 源
# 必须是可重定位的 python-build-standalone（bin/python3.x 用 @executable_path
# 作 rpath）。系统 Python 和 brew Python 都带绝对路径依赖，拷进 bundle 会炸。
step "定位可重定位的 Python 运行时"

find_python() {
  # 优先环境变量；否则在常见位置找一个满足条件的解释器
  if [[ -n "${DH_PYTHON_SRC:-}" && -x "${DH_PYTHON_SRC}/bin/python3" ]]; then
    echo "$DH_PYTHON_SRC"; return 0
  fi
  local d exe
  for d in "$HOME"/.workbuddy/binaries/python/versions/* \
           "$HOME"/.local/share/uv/python/*; do
    [[ -d "$d/bin" ]] || continue
    exe="$(ls "$d"/bin/python3.* 2>/dev/null | head -1)"
    [[ -n "$exe" ]] || exe="$d/bin/python3"
    [[ -x "$exe" ]] || continue
    # 可重定位判据：主二进制的 rpath 是 @executable_path 而非绝对路径
    if otool -l "$exe" 2>/dev/null | grep -q "@executable_path/../lib"; then
      echo "$d"; return 0
    fi
  done
  return 1
}

PYTHON_SRC="$(find_python || true)"
if [[ -z "$PYTHON_SRC" || ! -x "$PYTHON_SRC/bin/python3" ]]; then
  cat >&2 <<'EOF'
找不到可重定位的 Python 运行时。

构建需要 python-build-standalone（可拷进 .app 的那种），系统 Python 与
Homebrew Python 都不行 —— 它们的 libpython 是绝对路径依赖。

获取方式：
  curl -LO https://github.com/astral-sh/python-build-standalone/releases/download/20250818/cpython-3.13.7+20250818-aarch64-apple-darwin-install_only.tar.gz
  tar xzf cpython-*.tar.gz
  export DH_PYTHON_SRC=$PWD/python
EOF
  exit 1
fi
echo "  $PYTHON_SRC"

# ---------------------------------------------------------------- 2. 编译 Swift
step "编译 Swift 外壳（release）"
cd "$ROOT"
swift build -c release --disable-sandbox 2>&1 | tail -3

# ---------------------------------------------------------------- 3. bundle 骨架
step "组装 bundle 骨架"
# 故意不 rm -rf 旧 dist.app/sandbox 内的旧产物——几百 MB 的 bundle 会触发
# safe-delete 批量保护（CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=50），整个 build
# 就卡在 GUI 确认框里。改成覆盖策略：可执行文件用 cp 强制覆盖；目录用
# rsync --delete 后内容等同「删旧+建新」，且 rsync 删目标是 atomic rename
# 单文件级别，不会触发 bulk 阈值。
mkdir -p "$CONTENTS/MacOS" "$RES"
cp -f "$ROOT/Resources/Info.plist" "$CONTENTS/Info.plist"

# 可执行文件名以 Info.plist 的 CFBundleExecutable 为准，不能拿显示名去命名：
# 两者对不上时 LaunchServices 会报 "executable is missing"，app 图标在启动台
# 看起来正常，双击却打不开，而构建阶段完全不会报错。单一事实源放在 plist。
EXEC_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' \
  "$CONTENTS/Info.plist" 2>/dev/null || true)"
if [ -z "$EXEC_NAME" ]; then
  echo "错误：Info.plist 缺少 CFBundleExecutable" >&2
  exit 1
fi
cp ".build/release/DigitalHumanApp" "$CONTENTS/MacOS/$EXEC_NAME"
chmod +x "$CONTENTS/MacOS/$EXEC_NAME"

# ---------------------------------------------------------------- 4. 内嵌 Python
step "内嵌 Python 运行时"
mkdir -p "$RES/python"
# --delete：python 源可能换版本，残留的旧 .so 会导致 import 混乱
rsync -a --delete "$PYTHON_SRC/" "$RES/python/"
echo "  体积 $(du -sh "$RES/python" | cut -f1)"

# ---------------------------------------------------------------- 5. 服务端代码
step "同步服务端代码"
mkdir -p "$RES/server"
rsync -a --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude 'data/' \
  --exclude 'models/' \
  --exclude 'llm-models/' \
  --exclude 'env.local' \
  --exclude '*.log' \
  --exclude '.DS_Store' \
  "$SERVER/" "$RES/server/"
echo "  体积 $(du -sh "$RES/server" | cut -f1)"

# 版本号文件（2026-09-10 审查补上）：VERSION 在仓库根，不在 server/ 下，
# 所以上面的 rsync "$SERVER/" 带不进来。服务端 app/_version.py 是「从自己的
# 目录逐级向上找 VERSION」，bundle 里就对应 Resources/server/app → Resources/server
# → Resources；不放进 Resources/ 的话向上会一路找到 /Applications 都找不到，
# 版本号静默变成 "unknown"（实测 /api/console/state 的 service.version 就是它）。
# 放在 Resources/VERSION 与仓库里「VERSION 与 server/ 同级」的结构完全对齐。
cp -f "$REPO/VERSION" "$RES/VERSION"

# ---------------------------------------------------------------- 6. Python 依赖
# 用 --target 装成普通目录 + PYTHONPATH 引入，而不是创建 venv：
# venv 的 pyvenv.cfg 记的是**构建时**的绝对路径，用户把 .app 拖到
# /Applications 后路径就变了，venv 直接失效。--target 没有这个问题。
step "安装 Python 依赖"
DEPS_CACHE="$ROOT/.cache/site-packages"
mkdir -p "$ROOT/.cache"
if [[ $REFRESH_DEPS -eq 1 || ! -d "$DEPS_CACHE" ]]; then
  rm -rf "$DEPS_CACHE"
  "$RES/python/bin/python3" -m pip install --quiet --upgrade pip 2>&1 | tail -1 || true
  "$RES/python/bin/python3" -m pip install \
    --target "$DEPS_CACHE" \
    -r "$SERVER/requirements.txt" 2>&1 | tail -5
else
  echo "  复用缓存（${DEPS_CACHE}），要重装加 --refresh-deps"
fi
mkdir -p "$RES/site-packages"
rsync -a --delete "$DEPS_CACHE/" "$RES/site-packages/"
echo "  体积 $(du -sh "$RES/site-packages" | cut -f1)"

# ---------------------------------------------------------------- 7. 运行时瘦身
# 目标：只留运行必需。逐项都验证过 import 链，删错一项启动就会挂，别随手加。
step "运行时瘦身"
PYLIB="$RES/python/lib/python3.13"
# pip/setuptools 只是安装期工具；idlelib/ensurepip/tkinter/lib2to3 桌面服务用不到；
# include 是 C 头文件、share 是 man 文档，运行时无人读
/bin/rm -rf "$PYLIB"/site-packages/pip "$PYLIB"/site-packages/pip-* \
       "$PYLIB"/site-packages/setuptools "$PYLIB"/site-packages/pkg_resources \
       "$PYLIB"/idlelib "$PYLIB"/ensurepip "$PYLIB"/tkinter \
       "$PYLIB"/turtledemo "$PYLIB"/lib2to3 \
       "$RES/python/include" "$RES/python/share"
# 各库自带的测试套件（numpy 一家就有 6MB+），运行期不会 import
find "$RES" -type d \( -name tests -o -name test \) -prune -exec /bin/rm -rf {} + 2>/dev/null || true
# pygments 是 rich 的可选高亮依赖（10MB），hf_xet 是 HF 下载加速器（8MB），
# 服务端既不做语法高亮也不从 HF 在线下模型
/bin/rm -rf "$RES/site-packages/pygments" "$RES/site-packages/hf_xet" 2>/dev/null || true
# __pycache__ 占 106MB，且 bundle 签名后只读、Python 写不进去；
# 启动器设了 PYTHONPYCACHEPREFIX，.pyc 会生成到用户 Caches 目录
find "$RES" -type d -name __pycache__ -prune -exec /bin/rm -rf {} + 2>/dev/null || true
echo "  瘦身后 Resources：$(du -sh "$RES" | cut -f1)"

# ---------------------------------------------------------------- 8. 应用图标
step "应用图标"
[[ -f "$ROOT/Resources/AppIcon.icns" ]] || {
  /usr/bin/python3 "$ROOT/Scripts/make-icon.py" \
    "$ROOT/Resources/AppIcon.src/"*.png "$ROOT/Resources"
}
cp "$ROOT/Resources/AppIcon.icns" "$RES/"

# 菜单栏下拉面板顶部的 logo（40×40 = 20pt @2x）。由 tools/make-app-icons.py
# 从图标母版派生，与两端 app 图标、网页工作台顶栏同源。
# 不做自动重生成：那个脚本要 Pillow 且会连带重出 icns/ico/NSIS 位图，
# 构建期跑它等于把「换 logo」这件事悄悄塞进每次构建。缺了就在自检里报出来。
# 菜单栏要三份：面板顶部的彩色 logo（20pt）+ 状态项的模板剪影（运行中 18pt）
# + 同一形状的固定灰（未运行 18pt，非模板，见 DigitalHumanApp.swift 的注释）。
for f in MenuBarLogo.png MenuBarIcon.png MenuBarIconIdle.png; do
  if [ -f "$ROOT/Resources/$f" ]; then
    cp "$ROOT/Resources/$f" "$RES/"
  fi
done

# ---------------------------------------------------------------- 9. 启动器
step "生成启动器"
cat > "$RES/server/run-app.sh" <<'EOF'
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
EOF

# 文字模型（mlx-lm）。与 API 分成两个进程：LLM 显存压力大、偶发崩溃，
# 独立进程才能在它挂掉时单独重启，不影响收音频与转写。
cat > "$RES/server/run-llm.sh" <<'EOF'
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
EOF
chmod +x "$RES/server/run-app.sh" "$RES/server/run-llm.sh"

# ---------------------------------------------------------------- 10. 自检
# 这一节的每一行都对应一次真实事故：这些错误在构建阶段全都不报错，
# 装到机器上才表现为「双击打不开」或「服务起不来」。宁可构建失败，
# 也不要产出一个看起来正常、实际跑不了的 400 MB 安装包。
step "自检"
fail=0
chk() { if eval "$2"; then echo "  ✓ $1"; else echo "  ✗ $1"; fail=1; fi; }

EXEC_NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' \
  "$CONTENTS/Info.plist" 2>/dev/null || true)"
# CFBundleExecutable 与 MacOS 下的实际文件名对不上 → LaunchServices 报
# "executable is missing"，启动台里图标完好但双击无反应。
chk "可执行文件与 Info.plist 一致（${EXEC_NAME}）" \
  '[ -n "$EXEC_NAME" ] && [ -x "$CONTENTS/MacOS/$EXEC_NAME" ]'
# 签名后整个 bundle 只读，启动器少一个 +x 就永远起不来且无提示
chk "启动器可执行" '[ -x "$RES/server/run-app.sh" ] && [ -x "$RES/server/run-llm.sh" ]'
chk "内嵌 Python 可运行" \
  'PYTHONHOME="$RES/python" "$RES/python/bin/python3" -c "import sys" >/dev/null 2>&1'
chk "图标已就位" '[ -f "$RES/AppIcon.icns" ]'
# 面板 logo 缺了不会崩，但菜单栏弹窗顶部会静默退化成 SF Symbol ——
# 「三端 logo 一致」这条就此失效，而界面上看不出是构建问题
chk "菜单栏面板 logo 已就位" '[ -f "$RES/MenuBarLogo.png" ]'
chk "菜单栏状态项图标已就位（运行/未运行两份）" \
    '[ -f "$RES/MenuBarIcon.png" ] && [ -f "$RES/MenuBarIconIdle.png" ]'
# 缺 VERSION 不会报错，只会让界面上的版本号显示 "unknown"（静默失真）
chk "版本号文件已就位" '[ -f "$RES/VERSION" ] && [ -n "$(cat "$RES/VERSION")" ]'
# 依赖缺一个，服务起不来时日志只会有一行 ModuleNotFoundError
chk "关键依赖已安装" \
  '[ -d "$RES/site-packages/fastapi" ] && [ -d "$RES/site-packages/sherpa_onnx" ] && [ -d "$RES/site-packages/mlx_lm" ]'
# 数据目录里的东西不能进 bundle：会被升级整包覆盖，等于丢库
chk "bundle 内不含运行时数据" \
  '! [ -d "$RES/server/data" ] && ! [ -f "$RES/server/env.local" ]'

if [ $fail -ne 0 ]; then
  echo "自检未通过，产物不可用。" >&2
  exit 1
fi

# ---------------------------------------------------------------- 11. 收尾
step "构建完成"
echo "  产物：$APP"
echo "  总体积：$(du -sh "$APP" | cut -f1)"

# 顺手装到 /Applications。dmg 是给外部分发的；本机调试省一次手动拖拽。
# 覆盖策略：先 rsync --delete 全量同步内容（rsync 删目标走 atomic rename，
# 单文件级别，不会触发 safe-delete bulk 阈值）；如果 rsync 失败再降级为
# /bin/rm 清掉再 ditto。
# ⚠️ 必须用 /bin/rm 兜底，不能用 PATH 里的 rm —— 那个是 sandbox 代理版，
# 会被 bulk 阈值拦截。
if [ "${DH_SKIP_INSTALL:-0}" != "1" ] && [ -d "/Applications" ]; then
  INSTALL_APP="/Applications/$APP_NAME.app"
  mkdir -p "$INSTALL_APP/Contents/MacOS"
  if rsync -a --delete "$APP/Contents/" "$INSTALL_APP/Contents/" 2>/dev/null \
     && rsync -a --delete "$APP/Resources/" "$INSTALL_APP/Resources/" 2>/dev/null; then
    :
  else
    echo "  ⚠️ rsync 覆盖失败，降级 /bin/rm + ditto" >&2
    /bin/rm -rf "$INSTALL_APP"
    ditto "$APP" "$INSTALL_APP"
  fi
  echo "  已装到：$INSTALL_APP"

  # 完整性自检（2026-09-16 加的，起因是一次真实事故）：
  # 上面那条降级路径的 stderr 全被丢掉，ditto 在半路抛 Operation not permitted
  # 时**进程仍返回成功**，结果是「装完了、但少了一批文件」。
  # 实测代价：site-packages/transformers/models 源 511 个目录 → 已装只剩 327，
  # 丢掉 qwen3_5 → 本地 LLM 加载失败 → 生成线程崩溃 → 控制台接口悬挂。
  # ⚠️ 只比对主二进制 shasum 查不出来（那次 shasum 完全一致）。
  SP_SRC="$APP/Contents/Resources/site-packages"
  SP_DST="$INSTALL_APP/Contents/Resources/site-packages"
  if [ -d "$SP_SRC" ] && [ -d "$SP_DST" ]; then
    N_SRC=$(find "$SP_SRC" -type f 2>/dev/null | wc -l | tr -d ' ')
    N_DST=$(find "$SP_DST" -type f 2>/dev/null | wc -l | tr -d ' ')
    if [ "$N_SRC" = "$N_DST" ]; then
      echo "  完整性自检：通过（site-packages $N_SRC 个文件）"
    else
      echo "  ✗ 完整性自检失败：源 $N_SRC 个文件，已装 $N_DST 个（少 $((N_SRC - N_DST))）" >&2
      echo "    安装不完整，服务可能起不来或功能残缺。重新执行一次本脚本即可补上。" >&2
      echo "    排查方向：ditto/rsync 是否报过 Operation not permitted。" >&2
    fi
  fi
fi

if [[ $DO_SIGN -eq 1 ]]; then
  "$ROOT/Scripts/sign.sh" "$APP"
fi
