#!/usr/bin/env bash
#
# 打 dmg 安装包
#
#   用法：Scripts/make-dmg.sh [版本号]
#
# 用系统自带的 hdiutil，不引 create-dmg —— 少一个 brew 依赖，
# 也就少一处「客户机器上没装所以构建失败」的可能。
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/数字人服务.app"
VERSION="${1:-$(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo 0.3.0)}"
DMG="$DIST/数字人服务-$VERSION.dmg"
STAGING="$DIST/.dmg-staging"

[[ -d "$APP" ]] || { echo "找不到 ${APP}，先跑 Scripts/build.sh" >&2; exit 1; }

echo "==> 准备 dmg 内容"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
# 让用户可以直接把 app 拖到 Applications。这是 dmg 的标准交互约定，
# 没有它用户得自己开 Finder 找目标文件夹。
ln -s /Applications "$STAGING/Applications"

# 安装卷图标：挂载 dmg 后 Finder 侧边栏与桌面显示的卷图标与应用一致。
# -a C 是「custom icon」Finder 属性，两个都要设：文件标记 + 卷采用
ICNS_SRC="$(cd "$(dirname "$0")/.." && pwd)/Resources/AppIcon.icns"
cp "$ICNS_SRC" "$STAGING/.VolumeIcon.icns"
SetFile -a C "$STAGING/.VolumeIcon.icns"
SetFile -a C "$STAGING"

echo "==> 生成 dmg（UDZO 压缩）"
rm -f "$DMG"
hdiutil create \
  -volname "数字人服务 $VERSION" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$DMG" >/dev/null

rm -rf "$STAGING"

# dmg **文件**在 Finder 里默认是通用磁盘图标。挂载后的卷图标由 .VolumeIcon.icns 负责，
# 但文件本身要显示我们的图标，得把图标写进 resource fork，这一步没有现成 CLI。
echo "==> 给 dmg 文件本身贴图标"
swift "$(dirname "$0")/set-file-icon.swift" "$ICNS_SRC" "$DMG"

echo
echo "产物：$DMG"
echo "体积：$(du -sh "$DMG" | cut -f1)"
echo
echo "下一步："
echo "  Scripts/sign.sh \"\$APP\"     # 先签名（dmg 由已签名的 app 打包）"
echo "  Scripts/make-dmg.sh            # 再打 dmg"
echo "  Scripts/notarize.sh \"$DMG\""
