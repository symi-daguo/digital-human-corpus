#!/usr/bin/env bash
# bump-version.sh: 一次性同步仓库根 VERSION 文件到 4 个组件。
#
# 用法：
#   ./tools/bump-version.sh <new_version>     # 指定具体版本
#   ./tools/bump-version.sh patch              # MAJOR.MINOR.PATCH+1
#   ./tools/bump-version.sh minor              # MAJOR.MINOR+1.0
#   ./tools/bump-version.sh major              # MAJOR+1.0.0
#
# 语义版本约定（SemVer）：
#   - PATCH：bug 修复、性能优化、文档更新（向后兼容）
#   - MINOR：新功能、新页面、不破坏 API 的变更
#   - MAJOR：不兼容的 API 变更、UI 大改
#
# 警告：脚本会直接改 VERSION、Android versionName、macOS Info.plist、
#       desktop/package.json、tauri.conf.json 共 5 个文件。运行前请确认
#       工作树干净或有未提交修改已备份。
set -eo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

VERSION_FILE="VERSION"
GRADLE_FILE="app/app/build.gradle.kts"
PLIST_FILE="macapp/Resources/Info.plist"
DESKTOP_PKG="desktop/package.json"
TAURI_CFG="desktop/src-tauri/tauri.conf.json"

current="$(cat "$VERSION_FILE" | tr -d '[:space:]')"

usage() {
    echo "用法: $0 <new_version|major|minor|patch>" >&2
    exit 1
}

[[ $# -eq 1 ]] || usage

target="$1"
if [[ "$target" = "major" || "$target" = "minor" || "$target" = "patch" ]]; then
    IFS='.' read -r M m p <<< "$current"
    case "$target" in
        major) M=$((M + 1)); m=0; p=0 ;;
        minor) m=$((m + 1)); p=0 ;;
        patch) p=$((p + 1)) ;;
    esac
    target="$M.$m.$p"
fi

if ! [[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: 版本号必须是 MAJOR.MINOR.PATCH 形式（如 1.2.3），收到 '$target'" >&2
    exit 2
fi

if [[ "$current" = "$target" ]]; then
    echo "INFO: 当前版本已经是 ${current}，无需改动"
    exit 0
fi

# 拒绝降版本：数字比较
IFS='.' read -r CM Cm Cp <<< "$current"
IFS='.' read -r TM Tm Tp <<< "$target"
if (( TM < CM )) || (( TM == CM && Tm < Cm )) || (( TM == CM && Tm == Cm && Tp < Cp )); then
    echo "ERROR: 不允许从 ${current} 降到 ${target}（SemVer 不允许）" >&2
    exit 3
fi

echo "Bumping $current → $target"

# 1. 根 VERSION
echo "$target" > "$VERSION_FILE"

# 2. Android: app/app/build.gradle.kts versionName
sed -i.bak "s|versionName = \".*\"|versionName = \"$target\"|" "$GRADLE_FILE"
rm -f "$GRADLE_FILE.bak"

# 3. macOS: Info.plist
# 用 key 锚定下一行的 <string>，不依赖「旧值恰好等于 VERSION」。
# 旧写法 s|<string>$current</string>|<string>$target</string>| 在版本已漂移
# （如 VERSION=1.1.0 而 plist 仍是 1.0.0）时静默不匹配，于是 plist 永远停在
# 旧版本——2026-09-10 审查发现的真实漂移，就是这条 sed 造成的。
#
# 反向引用必须写成 ${1}/${2}（**不能**写 \1/\2，也**不能**写 \g{1}/\g{2}）：
#
#   1. \1 后面紧跟版本号首字符（如 1.2.0 的 1）会被解析成 \11（第 11 个捕获组），
#      整段被替换成空 —— 实测把 <key>CFBundleShortVersionString</key> 整行吃掉。
#   2. \g{1} 在 perl 的 **s/// 替换段**里不生效（实测 perl 5.34.1 darwin）：
#      替换段只做变量插值，不解析 \g 这类「匹配段专用」转义，反斜杠被丢弃，
#      结果原样写出字面量 `g{1}1.2.0g{2}`，plist 直接损坏。
#      （\g{1} 只在匹配段或开启 use re 'strict' 时有意义。）
#   3. ${1} 是替换段的标准变量插值，边界明确，永不含糊 —— 用它。
#
# 这个坑在 2026-09-10 连续踩了两次（先 \1 后 \g{1}），两次都损坏了 Info.plist。
# 改这里之前先用临时文件验证：perl -0pi -e 's|(a)|${1}X|' 打一个测试文件。
perl -0pi -e 's|(<key>CFBundleShortVersionString</key>\s*<string>)[^<]*(</string>)|${1}'"$target"'${2}|' "$PLIST_FILE"

# plist 是 XML，改坏了整个 macOS 壳都起不来 —— 语法必须当场校验，
# 不能让脚本「成功」退出后留下一个损坏的 bundle 描述文件。
if command -v plutil >/dev/null 2>&1; then
    if ! plutil -lint "$PLIST_FILE" >/dev/null 2>&1; then
        echo "ERROR: $PLIST_FILE 语法校验失败（bump 脚本改坏了 plist）" >&2
        plutil -lint "$PLIST_FILE" >&2 || true
        exit 4
    fi
fi

# 4. Desktop: package.json
sed -i.bak "s|\"version\": \".*\"|\"version\": \"$target\"|" "$DESKTOP_PKG"
rm -f "$DESKTOP_PKG.bak"

# 5. Desktop: tauri.conf.json
sed -i.bak "s|\"version\": \".*\"|\"version\": \"$target\"|" "$TAURI_CFG"
rm -f "$TAURI_CFG.bak"

echo ""
echo "=== 验证同步结果 ==="
echo "VERSION:           $(cat "$VERSION_FILE")"
grep "versionName = " "$GRADLE_FILE" | head -1
grep -A 1 "CFBundleShortVersionString" "$PLIST_FILE" | tail -1
grep "\"version\"" "$DESKTOP_PKG"
grep "\"version\"" "$TAURI_CFG"

echo ""
echo "✅ 完成。下一步："
echo "  - Android: cd app && ./gradlew :app:assembleRelease"
echo "  - macOS:   cd macapp && swift build -c release + Scripts/build.sh"
echo "  - Desktop: cd desktop && npx tauri build"
echo "  - Server:  无需重 build（启动时从 VERSION 文件读取，bundle 内会自动同步）"
