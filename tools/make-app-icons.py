#!/usr/bin/env python3
"""由图标母版一键生成三端图标（macOS .icns / Windows .ico + png / 托盘 / 菜单栏面板 / Web 工作台顶栏）。

**母版是唯一事实源**：`macapp/Resources/AppIcon.src/AppIcon-master-1024.png`
——1024×1024、正方形、已带圆角透明。三端图标全部由它派生，所以「mac、win 与
网页工作台的 logo 一致」是结构上保证的，不靠人工比对。

除两端 app 图标外，本脚本还派生 **Windows NSIS 安装向导的两张品牌位图**
（`nsis-sidebar.bmp` 164×314 / `nsis-header.bmp` 150×57）。Tauri 的
`bundle.windows.nsis.installerIcon / headerImage / sidebarImage` 默认值是 `None`
—— 不显式指定，安装器就是 **NSIS 自带图标 + MUI 那张蓝色「电脑」欢迎图**，
与我们无关（v1.5.4 用户实测发现，见 pitfalls P-53）。

换 logo 的流程：

1. 用新图替换母版。注意两点：
   - **取景先避开 AI 水印**（生成图右下角通常有「AI生成」水印）。本母版的
     取景框是 (80,0,960,880)，右/下边界都在水印之外，裁掉的部分落在纯背景上。
   - 母版自己要先套好圆角（半径比例 0.2237，见 `macapp/Scripts/make-icon.py`）。
2. `python3 tools/make-app-icons.py`
3. 重建两端：macapp 走 `Scripts/build.sh`，desktop 走 `npx tauri build --bundles nsis`。

本机是 macOS，NSIS 位图**不做本地构建验证**（打不出 win 包），只做两件事：
① 重跑脚本 `git status` 必须无变化（派生是确定性的）；② BMP 头必须是
24bpp 无调色板 —— MUI 用 `LoadImage` 读盘，**不认 PNG**，8bpp/32bpp 也有风险。
真正的验收在 CI 产物上（见「验收」一节）。

用法：make-app-icons.py
依赖：Pillow（`pip install pillow`）；macOS 端另需系统自带的 `iconutil`。
"""
import base64
import io
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("缺少 Pillow：pip install pillow")

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "macapp/Resources/AppIcon.src/AppIcon-master-1024.png"
MAC_ICNS_DIR = ROOT / "macapp/Resources"
ICON_SCRIPT = ROOT / "macapp/Scripts/make-icon.py"
DESKTOP_ICONS = ROOT / "desktop/src-tauri/icons"

# Tauri 约定：bundle.icon 里列的就是这几个名字，改名要同步改 tauri.conf.json
PNGS = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}
# Windows .ico 的档位。256 是给 NSIS 快捷方式在「大图标」视图下用的，
# 少了它系统会把 48 拉大、边缘发虚。
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# 菜单栏剪影的「亮度 → 不透明」映射：低于 LO 全透明、高于 HI 全不透明，中间过渡
TRAY_LO, TRAY_HI = 45, 165

# ---- Windows NSIS 安装向导位图 ------------------------------------------
# MUI 要求**精确**尺寸：大了被裁、小了被拉伸。
#   sidebar = MUI_WELCOMEFINISHPAGE_BITMAP（欢迎页 / 完成页左侧竖图）
#   header  = MUI_HEADERIMAGE_BITMAP（内页顶部横图）
NSIS_SIDEBAR = (164, 314)
NSIS_HEADER = (150, 57)

NSIS_BG = (10, 22, 34)        # 深海军蓝，取自母版深底
NSIS_FG = (233, 246, 249)     # 近白（标题）
NSIS_MUTED = (108, 138, 160)  # 次级说明
NSIS_ACCENT = (94, 230, 208)  # 品牌青（母版发光色）

NSIS_TITLE = "数字人服务"
NSIS_SUBTITLE = "语料采集与处理"
NSIS_BADGE = "Windows 版"

# ---- 菜单栏下拉面板顶部的 logo ------------------------------------------
# 40px = 20pt @2x。面板里就按 20pt 显示（与那一行两行状态文字同高），
# 单独出一份而不是复用 AppIcon.icns：icns 里最小档是 16px，系统按 20pt 渲染时
# 会把 32px 那一档拉大，边缘发虚。
PANEL_LOGO_PX = 40

# ---- 菜单栏状态项图标 ----------------------------------------------------
# 36px = 18pt @2x。macOS 菜单栏图标惯例是 16–18pt：desktop 那份 tray.png 用了
# 22pt，在 24pt 高的菜单栏里几乎顶天立地，比相邻的 WiFi / 音量明显大一圈。
MENU_ICON_PX = 36

# 未运行时的固定灰。用 systemGray（#8E8E93）而不是纯灰：在深色菜单栏上
# 纯 50% 灰偏暗、容易看成「禁用」，systemGray 是这个系统里「次要/非活动」
# 的既定语义色。
MENU_ICON_IDLE_RGB = (142, 142, 147)

# 生成物里要写中文，必须显式给字体，不能指望 PIL 的默认位图字体（缺字会画成方块）。
# macOS 各版本 PingFang 不一定在 /System/Library/Fonts 下，Hiragino Sans GB 更稳。
NSIS_FONTS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _cjk_font(px: int) -> "ImageFont.FreeTypeFont":
    for path in NSIS_FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    sys.exit("找不到可用的中文字体，向导位图会缺字；请在 NSIS_FONTS 里补一条路径")


def mac_icns(master_path: Path) -> None:
    """走 macapp 的 make-icon.py，保证与 build.sh 的 fallback 是同一条路。"""
    subprocess.run([sys.executable, str(ICON_SCRIPT), str(master_path),
                    str(MAC_ICNS_DIR)], check=True)
    size_kb = (MAC_ICNS_DIR / "AppIcon.icns").stat().st_size // 1024
    print(f"  icns  macapp/Resources/AppIcon.icns          {size_kb} KB")


def menu_bar_logo(master: Image.Image) -> None:
    """菜单栏下拉面板顶部的 logo —— 彩色母版本身，不是托盘那种单色剪影。

    与 `server_logo()`（网页工作台顶栏）的关系：同一个母版、同一套取景，
    只是显示尺寸不同（面板 20pt / 网页 22pt），所以各出一份、互不复用。

    为什么不用 `tray_silhouette()`：那个产物是**模板图标**——系统只取 alpha、
    用菜单栏文字颜色重绘，是单色的。面板里的 logo 要和两端 app 图标、网页顶栏
    一致，必须保留母版的颜色与发光。
    """
    out = MAC_ICNS_DIR / "MenuBarLogo.png"
    master.resize((PANEL_LOGO_PX, PANEL_LOGO_PX), Image.LANCZOS).save(out, optimize=True)
    print(f"  logo  macapp/Resources/MenuBarLogo.png       "
          f"{PANEL_LOGO_PX}x{PANEL_LOGO_PX} ({out.stat().st_size // 1024} KB)")


def menu_bar_icon(master: Image.Image) -> None:
    """菜单栏状态项的小 logo —— 出两份：运行中（模板）/ 未运行（灰）。

    为什么不像面板 logo 那样用彩色：菜单栏上相邻的 WiFi、音量、控制中心全是单色，
    插一个彩色方块非常突兀；更要命的是彩色图不随亮暗菜单栏变化，浅色壁纸下直接
    看不见。所以走 `tray_silhouette()` 的剪影——系统只取 alpha、用菜单栏前景色重绘。

    未运行那份把 RGB 固定成 systemGray 并且**故意不做模板**：灰色是「没在跑」的
    唯一信号，若也交给系统重绘，就永远和运行中同色了。
    """
    sil = tray_silhouette(master).resize((MENU_ICON_PX, MENU_ICON_PX), Image.LANCZOS)
    run_out = MAC_ICNS_DIR / "MenuBarIcon.png"
    sil.save(run_out, optimize=True)

    idle = Image.new("RGBA", sil.size, MENU_ICON_IDLE_RGB + (0,))
    idle.putalpha(sil.getchannel("A"))
    idle_out = MAC_ICNS_DIR / "MenuBarIconIdle.png"
    idle.save(idle_out, optimize=True)

    print(f"  icon  macapp/Resources/MenuBarIcon.png       "
          f"{MENU_ICON_PX}x{MENU_ICON_PX} 运行中(模板) "
          f"{run_out.stat().st_size // 1024} KB")
    print(f"  icon  macapp/Resources/MenuBarIconIdle.png   "
          f"{MENU_ICON_PX}x{MENU_ICON_PX} 未运行(灰) "
          f"{idle_out.stat().st_size // 1024} KB")


def tray_silhouette(master: Image.Image) -> Image.Image:
    """菜单栏要的**模板图标**：系统只取 alpha 通道、用当前菜单栏颜色渲染。

    直接把 app 图标缩小是不行的 —— 那是个实心圆角方块，模板渲染出来就是一块
    方块。这里从母版取亮度做 alpha：发光主体留下、深色背景变透明。
    RGB 统一成黑（模板渲染色由系统决定，写什么颜色都会被忽略）。
    """
    gray = master.convert("RGB").convert("L")

    def ramp(v: int) -> int:
        if v <= TRAY_LO:
            return 0
        if v >= TRAY_HI:
            return 255
        return (v - TRAY_LO) * 255 // (TRAY_HI - TRAY_LO)

    silhouette = Image.new("RGBA", master.size, (0, 0, 0, 0))
    silhouette.putalpha(gray.point(ramp))
    return silhouette


def desktop_icons(master: Image.Image) -> None:
    DESKTOP_ICONS.mkdir(parents=True, exist_ok=True)

    for name, px in PNGS.items():
        master.resize((px, px), Image.LANCZOS).save(DESKTOP_ICONS / name)
        print(f"  png   desktop/src-tauri/icons/{name:16s} {px}x{px}")

    ico = DESKTOP_ICONS / "icon.ico"
    master.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  ico   desktop/src-tauri/icons/icon.ico          {ico.stat().st_size // 1024} KB")

    # macOS 菜单栏（模板剪影，22pt）/ Windows 托盘（彩色，32px）
    tray_silhouette(master).resize((22, 22), Image.LANCZOS).save(DESKTOP_ICONS / "tray.png")
    master.resize((32, 32), Image.LANCZOS).save(DESKTOP_ICONS / "tray-32.png")
    print("  tray  desktop/src-tauri/icons/tray.png (剪影 22) + tray-32.png (彩色 32)")

    # desktop 的 bundle.targets 里也有 app/dmg，所以要给它一份 icns。
    # 直接复制 macapp 那份（同一张母版，内容完全一致），不必算两次。
    shutil.copyfile(MAC_ICNS_DIR / "AppIcon.icns", DESKTOP_ICONS / "icon.icns")
    print("  icns  desktop/src-tauri/icons/icon.icns         (与 macapp 同源)")


def _save_bmp(img: "Image.Image", name: str, expect: tuple) -> None:
    """落盘并自检：MUI 用 LoadImage 读盘，只认 24bpp 无调色板 BMP，不认 PNG。"""
    if img.size != expect:
        sys.exit(f"{name} 尺寸 {img.size} != {expect}")
    path = DESKTOP_ICONS / name
    img.save(path, format="BMP")
    raw = path.read_bytes()
    if raw[:2] != b"BM":
        sys.exit(f"{name} 不是 BMP")
    bpp = int.from_bytes(raw[28:30], "little")
    if bpp != 24:
        sys.exit(f"{name} 位深 {bpp}bpp，MUI 需要 24bpp")
    print(f"  bmp   desktop/src-tauri/icons/{name:18s} "
          f"{expect[0]}x{expect[1]} 24bpp {path.stat().st_size // 1024} KB")


def _draw_text(d: "ImageDraw.ImageDraw", xy: tuple, text: str, font, fill,
               limit: int, anchor: str = "mm") -> None:
    """写字并挡住「换了个长标题就悄悄溢出画布」这种只有渲出来才看得见的问题。"""
    w = d.textbbox((0, 0), text, font=font)[2]
    if w > limit:
        sys.exit(f"文案「{text}」宽 {w}px 超出可用 {limit}px，向导位图会被裁")
    d.text(xy, text, font=font, fill=fill, anchor=anchor)


def nsis_art(master: "Image.Image") -> None:
    """安装向导的品牌位图。Tauri 这几个字段默认是 None，不显式指定就是 NSIS 自带图。"""
    DESKTOP_ICONS.mkdir(parents=True, exist_ok=True)

    # 欢迎页 / 完成页左侧竖图：徽标 + 名称（不写版本号，免得每次升版本都要重生成）
    w, h = NSIS_SIDEBAR
    side = Image.new("RGB", (w, h), NSIS_BG)
    d = ImageDraw.Draw(side)
    badge = master.resize((104, 104), Image.LANCZOS)
    side.paste(badge, ((w - 104) // 2, 38), badge)  # 带 alpha 贴，圆角外露出底色
    _draw_text(d, (w // 2, 174), NSIS_TITLE, _cjk_font(21), NSIS_FG, w - 24)
    _draw_text(d, (w // 2, 204), NSIS_SUBTITLE, _cjk_font(12), NSIS_MUTED, w - 24)
    d.line([(w // 2 - 20, 232), (w // 2 + 20, 232)], fill=NSIS_ACCENT, width=2)
    _draw_text(d, (w // 2, 278), NSIS_BADGE, _cjk_font(12), NSIS_MUTED, w - 24)
    _save_bmp(side, "nsis-sidebar.bmp", NSIS_SIDEBAR)

    # 内页顶部横图
    w, h = NSIS_HEADER
    head = Image.new("RGB", (w, h), NSIS_BG)
    d = ImageDraw.Draw(head)
    logo = master.resize((38, 38), Image.LANCZOS)
    head.paste(logo, (8, (h - 38) // 2), logo)
    _draw_text(d, (54, h // 2), NSIS_TITLE, _cjk_font(15), NSIS_FG, w - 54 - 6, "lm")
    _save_bmp(head, "nsis-header.bmp", NSIS_HEADER)


def server_logo(master: Image.Image) -> None:
    """Web 工作台顶栏的 logo —— 内嵌 data URI，落成一个 Python 常量模块。

    为什么不放静态文件：服务端打出来的 /console 是一个**自包含的 HTML 字符串**
    （见 console.py 的设计约束：零外部依赖、不引 CDN、不做 SSR）。改成一个
    `/console/logo.png` 静态路由，就要解决「bundle 里图标放哪、开发态又放哪」——
    而图标本来只存在于 macapp/ 与 desktop/ 两棵树里，服务端的资源清单里没有它。
    40×40 的 PNG 只有 5 KB 左右，内嵌换来的是零路径依赖。

    尺寸 44px = 顶栏 22px 的 @2x，Retina 下不会发虚。
    """
    buf = io.BytesIO()
    master.resize((44, 44), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    out = ROOT / "server/app/_logo.py"
    out.write_text(
        '"""Web 工作台顶栏的 logo（PNG data URI）。\n'
        "\n"
        "**自动生成，不要手改** —— 由 tools/make-app-icons.py 从图标母版\n"
        "macapp/Resources/AppIcon.src/AppIcon-master-1024.png 派生，\n"
        "与 mac / Windows 两端 app 图标、托盘图标同源。手改会让网页顶栏\n"
        "与两端图标对不上。换 logo 请替换母版后重跑那个脚本。\n"
        '"""\n'
        "\n"
        '# 44×44 PNG（顶栏按 22px 显示，@2x）。\n'
        f'DATA_URI = "data:image/png;base64,{b64}"\n',
        encoding="utf-8",
    )
    print(f"  uri   server/app/_logo.py                    {len(b64) // 1024} KB base64")


def main() -> None:
    if not MASTER.exists():
        sys.exit(f"找不到母版：{MASTER}")
    master = Image.open(MASTER).convert("RGBA")
    if master.size[0] != master.size[1]:
        sys.exit(f"母版必须是正方形，当前 {master.size}")
    print(f"母版 {MASTER.relative_to(ROOT)}  {master.size[0]}x{master.size[1]}")

    mac_icns(MASTER)
    menu_bar_logo(master)
    menu_bar_icon(master)
    desktop_icons(master)
    nsis_art(master)
    server_logo(master)
    print("\n完成。三端（mac / Windows / Web 工作台）图标同源，重建后生效。")


if __name__ == "__main__":
    main()
