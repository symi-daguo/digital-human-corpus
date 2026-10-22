#!/usr/bin/env bash
#
# 用 Developer ID 签名 .app（公证前必须先签）
#
#   用法：Scripts/sign.sh <.app 路径>
#   可选：DH_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#
set -euo pipefail

APP="${1:?用法: sign.sh <.app 路径>}"
IDENTITY="${DH_SIGN_IDENTITY:-Developer ID Application}"

[[ -d "$APP" ]] || { echo "找不到 $APP" >&2; exit 1; }

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# 确认证书存在，免得签了一半才发现
if ! security find-identity -v -p codesigning | grep -q "$IDENTITY"; then
  cat >&2 <<EOF
钥匙串里找不到签名身份：$IDENTITY

站外分发必须用 **Developer ID Application** 证书，不是 Apple Development。
若尚未创建：
  1. 打开 developer.apple.com → Certificates → '+' → Developer ID Application
  2. 下载 .cer 并双击导入钥匙串
  3. 重新运行本脚本

也可以在环境变量里指定完整身份名：
  DH_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" Scripts/sign.sh ...
EOF
  exit 1
fi

# ---------------------------------------------------------------------------
# 签名顺序必须是「由内到外」：外层的 bundle 签名会把内层文件的哈希记进
# CodeResources，任何内层文件在签完 bundle 之后再变动，整个签名就废了。
# ---------------------------------------------------------------------------
sign_one() {
  codesign --force --sign "$IDENTITY" \
    --options runtime \
    --timestamp \
    --preserve-metadata=entitlements \
    "$1" 2>/dev/null
}

step "签名：Python native 扩展（.so / .dylib）"
count=0
while IFS= read -r f; do
  sign_one "$f" && count=$((count + 1))
done < <(find "$APP/Contents/Resources" \( -name '*.so' -o -name '*.dylib' \) -type f)
echo "  已签 $count 个"

step "签名：Python 解释器与可执行脚本"
count=0
while IFS= read -r f; do
  # 符号链接跳过：codesign 会跟随链接改到目标文件，导致重复签名
  [[ -L "$f" ]] && continue
  sign_one "$f" && count=$((count + 1))
done < <(find "$APP/Contents/Resources/python/bin" -type f -perm +111 2>/dev/null)
echo "  已签 $count 个"

step "签名：启动器与 shell 脚本"
while IFS= read -r f; do
  sign_one "$f" 2>/dev/null || true
done < <(find "$APP/Contents/Resources/server" -name '*.sh' -type f 2>/dev/null)

step "签名：主 bundle"
codesign --force --sign "$IDENTITY" \
  --options runtime \
  --timestamp \
  --entitlements "$(dirname "$0")/../Resources/DigitalHuman.entitlements" \
  "$APP"

step "校验"
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tail -5
spctl --assess --type execute --verbose=4 "$APP" 2>&1 | tail -3 || true

echo
echo "签名完成。注意：此时 spctl 可能仍报 'source: no usable signature'，"
echo "那是还没公证（notarize.sh），不是签名失败。"
