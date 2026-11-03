#!/bin/bash
# 版本一致性自检：VERSION / gradle versionName / plist / desktop package.json / tauri.conf.json
#
# 2026-11-03 立。起因：1.0.5 发布时 `desktop/package.json` 与
# `desktop/src-tauri/tauri.conf.json` 漏改，仍停在 1.0.4 —— 桌面端打出来的包
# 版本号会与 Android/macOS 不一致，而打包流程本身不会报错（静默漂移）。
# tones 之前也踩过同类（plist 停在 1.0.0）。
#
# 用法：
#   tools/check-version.sh        # 一致 exit 0，不一致 exit 1
#   tools/check-version.sh -q     # 只输出结论
set -o pipefail
cd "$(dirname "$0")/.." || exit 1

QUIET=0
[ "${1:-}" = "-q" ] && QUIET=1

V=$(tr -d '[:space:]' < VERSION 2>/dev/null)
G=$(sed -nE 's/^.*versionName = "([^"]+)".*$/\1/p' app/app/build.gradle.kts 2>/dev/null | head -1)
P=$(plutil -extract CFBundleShortVersionString raw macapp/Resources/Info.plist 2>/dev/null)
D=$(sed -nE 's/^.*"version": "([^"]+)".*$/\1/p' desktop/package.json 2>/dev/null | head -1)
T=$(sed -nE 's/^.*"version": "([^"]+)".*$/\1/p' desktop/src-tauri/tauri.conf.json 2>/dev/null | head -1)

fail=0
[ "$QUIET" -eq 0 ] && echo "版本一致性检查（基准 VERSION=${V}）"

check() {
    local name="$1" val="$2"
    if [ -z "$val" ]; then
        [ "$QUIET" -eq 0 ] && printf "  FAIL %-8s 读不到\n" "$name"
        fail=1
    elif [ "$val" = "$V" ]; then
        [ "$QUIET" -eq 0 ] && printf "  OK   %-8s %s\n" "$name" "$val"
    else
        [ "$QUIET" -eq 0 ] && printf "  FAIL %-8s %s  (应为 %s)\n" "$name" "$val" "${V}"
        fail=1
    fi
}

check "VERSION" "$V"
check "gradle" "$G"
check "plist" "$P"
check "desktop" "$D"
check "tauri" "$T"

if [ $fail -ne 0 ]; then
    echo "版本不一致 -> 跑 tools/bump-version.sh <目标版本> 统一，再手工把 versionCode +1" >&2
    exit 1
fi
[ "$QUIET" -eq 0 ] && echo "  OK   五处一致：${V}"
exit 0
