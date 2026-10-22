#!/usr/bin/env bash
# 静态防回归检查：仓库源码里不得出现「可创建 Apple 日历 / 提醒事项」的能力。
#
# 背景（2026-09-24）：用户 iPhone「日历」里出现大量「开启/关闭全天智记录音」条目。
# 排查结论是本机与本项目均无写入能力（见 docs/verification.md §49）。为把这条不变量
# 固化下来，凡在源码/技能里引入下列任一 API 或工具，本检查即失败。
#
#   EventKit / EKEvent / EKReminder / EKCalendar  —— macOS 原生日历与提醒
#   CalendarContract                              —— Android 日历 Provider
#   remindctl                                     —— steipete 的 Apple 提醒 CLI
#   UNCalendarNotificationTrigger                 —— 定时本地通知（可被误用为"提醒"）
#   VCALENDAR / BEGIN:VEVENT / BEGIN:VTODO        —— iCalendar 投放
#
# 用途：发布前跑一次；CI 亦可直接调用。退出码 0 = 干净，1 = 命中。
#
# ⚠️ 只扫**受版本控制的内容**，跳过：
#    · docs/                        —— 文档里出现这些词是在描述禁令，不是能力
#    · tools/check-no-apple-push.sh —— 本脚本自身含关键字
#    · */.runtime/                  —— 第三方内置运行时（如 desktop/src-tauri/.runtime 的 Python 标准库）

set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

PATTERN='EventKit|EKEvent|EKReminder|EKCalendar|CalendarContract|remindctl|UNCalendarNotificationTrigger|UNUserNotificationCenter|VCALENDAR|BEGIN:VEVENT|BEGIN:VTODO'

FILES=$(git ls-files -z 2>/dev/null \
  | tr '\0' '\n' \
  | sed '/^$/d' \
  | grep -vE '^docs/|^tools/check-no-apple-push\.sh$|/\.runtime/')

if [ -z "$FILES" ]; then
  echo "check-no-apple-push: 未取到文件清单（是否在仓库根运行？）" >&2
  exit 2
fi

COUNT=$(printf '%s\n' "$FILES" | wc -l | tr -d ' ')
HITS=$(printf '%s\n' "$FILES" | tr '\n' '\0' | xargs -0 grep -nE "$PATTERN" 2>/dev/null)

if [ -n "$HITS" ]; then
  echo "❌ check-no-apple-push: 源码中出现了 Apple 日历/提醒写入能力"
  echo "$HITS"
  echo
  echo "这些能力可以把条目经 iCloud 同步到用户 iPhone，属于明令禁止项。"
  echo "如确为误报（例如仅在注释里说明「本项目不调用 EventKit」），请调整措辞避开关键字。"
  exit 1
fi

echo "✅ check-no-apple-push: 已扫描 $COUNT 个受控文件，未发现 Apple 日历/提醒写入能力"
