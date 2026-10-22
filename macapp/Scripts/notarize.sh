#!/usr/bin/env bash
#
# 公证 + 钉票（staple）
#
# 公证是 Apple 对安装包做一次恶意软件扫描，通过后会签发一张「票」。
# staple 把票钉进包里，这样用户**离线**安装时 Gatekeeper 也能放行；
# 不钉票的话，用户第一次打开必须联网去 Apple 查票。
#
#   前置：先跑 Scripts/make-dmg.sh 生成 dmg
#   用法：Scripts/notarize.sh <dmg 路径>
#
set -euo pipefail

FILE="${1:?用法: notarize.sh <dmg 或 zip 路径>}"
PROFILE="${DH_NOTARY_PROFILE:-digitalhuman-notary}"

[[ -f "$FILE" ]] || { echo "找不到 $FILE" >&2; exit 1; }

if ! xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
  cat >&2 <<'GUIDE'
找不到公证凭据 profile。

先创建一次（只需一次，之后脚本自动复用）：

  方式一（推荐，App Store Connect API Key，便于 CI）：
    xcrun notarytool store-credentials "$PROFILE" \
      --apple-id <你的 Apple ID> \
      --team-id <Team ID> \
      --key <AuthKey_XXXX.p8 路径> \
      --key-id <Key ID> \
      --issuer <Issuer ID>

  方式二（Apple ID + App 专用密码）：
    xcrun notarytool store-credentials "$PROFILE" \
      --apple-id <你的 Apple ID> \
      --team-id <Team ID> \
      --password <App 专用密码>

App 专用密码在 appleid.apple.com 生成，不是 Apple ID 登录密码。
GUIDE
  exit 1
fi

echo "提交公证（通常需要 1–5 分钟）…"
xcrun notarytool submit "$FILE" --keychain-profile "$PROFILE" --wait

echo
echo "钉票（staple）…"
xcrun stapler staple "$FILE"

echo
echo "最终校验（离线可安装性）："
spctl --assess --type install --verbose=4 "$FILE" 2>&1 | tail -3
