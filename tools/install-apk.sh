#!/usr/bin/env bash
#
# 装机并自动拉起（数字人语料采集 App）。
#
# 为什么必须带「拉起」这一步：
#   `adb install -r` 会让系统对目标包执行 Force Stop（系统日志 installPackageLI，
#   ActivityManager 会打印 "Force stopping ... pkg removed"），应用随即进入
#   Android 的 stopped 状态。stopped 状态下：
#     · 收不到任何广播（BOOT_COMPLETED / MY_PACKAGE_REPLACED 都不会来）
#     · 服务不会被系统重新拉起
#   也就是说麦克风前台服务不会自己回来。而 Android 14+ 又禁止 App 从后台启动
#   microphone FGS（CaptureService.start 里 `SDK_INT >= 34 && !activityResumed`
#   直接降级为挂恢复通知），所以它自己也救不了，只能等用户手动打开一次 App。
#
#   2026-09-11 实测教训：11:14 装机后，直到 11:35 用户点亮屏幕手动打开 App
#   采集才恢复（CaptureSvc recordLoop session=89）。中间 21 分钟一句都没录到，
#   用户反馈「apk 怎么没在工作，讲了 20 分钟没有任何数据」。
#
#   ——所以：装机动作和「拉起动作」必须绑在一起执行，永远不要只 install。
#
# 用法：
#   tools/install-apk.sh [apk路径] [设备序列号]
#   默认 APK = app/app/build/outputs/apk/release/app-universal-release.apk
#
set -euo pipefail

ADB="${ADB:-$HOME/Library/Android/sdk/platform-tools/adb}"
PKG="com.symi.digitalhuman"
ACTIVITY="$PKG/.ui.MainActivity"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APK="${1:-$ROOT/app/app/build/outputs/apk/release/app-universal-release.apk}"
DEV="${2:-}"

if [ ! -f "$APK" ]; then
  echo "找不到 APK：$APK" >&2
  echo "先构建：cd app && ./gradlew assembleRelease" >&2
  exit 1
fi

A=("$ADB")
[ -n "$DEV" ] && A+=(-s "$DEV")

echo "== 1/4 安装 =="
echo "$APK"
"${A[@]}" install -r "$APK"

echo
echo "== 2/4 唤醒屏幕、解除锁屏并拉起应用（解除 Force Stop 后的 stopped 状态）=="
# am start 由 adb shell 发起，具备从后台启动 Activity 的权限，是 stopped 状态
# 唯一可靠的解除方式（比等用户自己发现 App 没在录要好得多）。
#
# ⚠️ 三个动作缺一不可（2026-09-11 实测，全部踩过）：
#   · 不拉起        → 应用停在 Android stopped 状态，服务不会自动重启
#   · 屏幕关闭时拉起 → am start 报 Status: ok、进程也变 TOP，但 Activity 不可见，
#                     系统随后把刚起来的麦克风前台服务一并回收（GuardService 存活
#                     约 20~30 秒后消失，采集始终没恢复）
#   · 锁屏时拉起    → 同上（dumpsys 里 mDreamingLockscreen=true 即为此态）
"${A[@]}" shell input keyevent KEYCODE_WAKEUP
# 无密码锁屏有效；设了密码/PIN 时这条无效，需要用户手动解锁后再打开 App
"${A[@]}" shell wm dismiss-keyguard || true
"${A[@]}" shell am start -W -n "$ACTIVITY"

echo
echo "== 3/4 等待采集服务起来 =="
sleep 12

echo "== 4/4 状态确认 =="
echo "--- 进程 ---"
# 这台 MIUI（25053RT47C）的 toybox ps 不支持 -o PID,ETIME,NAME，只能靠 grep
"${A[@]}" shell "ps -A | grep digitalhuman" || true
echo "--- 装机版本 ---"
"${A[@]}" shell "dumpsys package $PKG | grep -E 'versionName|lastUpdateTime'" || true
echo "--- 前台服务 ---"
"${A[@]}" shell "dumpsys activity services $PKG | grep -E 'service/(Guard|Capture)Service|isForeground'" || true
echo "--- 采集循环（真正在录的证据）---"
LOOP=$("${A[@]}" shell "logcat -d | grep 'recordLoop 开始' | tail -2" 2>/dev/null | tr -d '\r')
echo "${LOOP:-（无）}"
echo
if [ -z "$LOOP" ]; then
  echo "⚠️  未检测到采集循环：多半是手机处于熄屏/锁屏，Activity 不可见导致 FGS 被回收。"
  echo "    请在手机上解锁并打开「数字人」App 一次 —— 打开后约 1 秒内会自动恢复采集。"
else
  echo "✅ 采集循环已在运行。下拉通知栏可见「语料采集中 · 已录 N 段 · 最近 HH:mm」。"
  echo "   若这条通知不见了，说明进程已被系统停止。"
fi
