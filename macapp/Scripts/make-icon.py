#!/usr/bin/env python3
"""把图标源图加工成 macOS 规范的 .icns。

流程：中心裁成正方形 → 放大到 1024 → 套圆角透明遮罩 → 生成 iconset 全部尺寸 →
iconutil 打包。

源图要求：方形（或接近方形）、主体居中、**右下角不放内容** —— AI 生成图常带
水印，取景时就该把水印避开，而不是事后去抹。

幂等：源图如果已经是「带透明圆角的方形图」（四角 alpha 全 0），就不再重复套
遮罩 —— 两次抗锯齿叠加会把边缘压得更透。这样「先人工做出圆角母版、再由它
派生两端图标」和「直接把方形源图交给脚本」两种用法都成立。

用法：make-icon.py <源图.png> <输出目录>
输出：<输出目录>/AppIcon.icns
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# Apple 连续圆角（squircle）的近似半径比例
RADIUS_RATIO = 0.2237


def has_rounded_alpha(img: Image.Image) -> bool:
    """四角全透明 → 认定源图已自带圆角遮罩，不必再套一层。"""
    if img.mode != "RGBA":
        return False
    alpha = img.getchannel("A")
    w, h = alpha.size
    corners = (alpha.getpixel((1, 1)), alpha.getpixel((w - 2, 1)),
               alpha.getpixel((1, h - 2)), alpha.getpixel((w - 2, h - 2)))
    return all(c == 0 for c in corners)


def build_master(src: Path) -> Image.Image:
    img = Image.open(src).convert("RGBA")

    # 中心裁成正方形：源图不一定是正方的，直接缩到 1024 会拉变形
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    master = img.crop((left, top, left + side, top + side)).resize((1024, 1024), Image.LANCZOS)

    if has_rounded_alpha(master):
        return master

    # 1px 抗锯齿羽化：纯硬边遮罩在 Dock 放大时会有锯齿
    mask = Image.new("L", (2048, 2048), 0)
    r = int(2048 * RADIUS_RATIO)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 2047, 2047], radius=r, fill=255)
    mask = mask.resize((1024, 1024), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.5))
    master.putalpha(mask)
    return master


def main() -> None:
    src = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    master = build_master(src)

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        sizes = [16, 32, 128, 256, 512]
        for s in sizes:
            for scale, name in ((1, f"icon_{s}x{s}.png"), (2, f"icon_{s}x{s}@2x.png")):
                px = s * scale
                # 最大档直接用 1024 原图，避免二次缩放
                im = master if px == 1024 else master.resize((px, px), Image.LANCZOS)
                im.save(iconset / name)

        icns = out_dir / "AppIcon.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
            check=True,
        )
    print(f"OK  {icns}  ({icns.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
