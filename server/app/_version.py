"""项目版本：根目录 VERSION 文件是唯一来源。

修改流程：
  1. 编辑 VERSION（语义版本三段 MAJOR.MINOR.PATCH）
  2. 跑 ./tools/bump-version.sh 同步到 4 个组件的版本字段
  3. 重打 bundle（macapp）或重 build APK（Android）

不要在 FastAPI、console、Agent API 等地方硬编码版本号——它们都从这里读，
避免「A 改了 B 没改」的不一致。
"""
from __future__ import annotations

from pathlib import Path


def _find_version_file() -> Path | None:
    """沿父目录向上找 VERSION，直到根目录。

    为什么要搜：源码布局（server/app/_version.py）与 bundle 布局
    （数字人服务.app/Contents/Resources/server/app/_version.py）的「上两级」
    指向不同位置——源码里是仓库根，bundle 里是 Resources/，而 VERSION 在
    bundle 的 Resources/server/ 下。固定「上 N 级」会两边都错。
    向上搜直到找到 VERSION 文件为止。
    """
    cur = Path(__file__).resolve().parent
    for _ in range(8):  # 防御性上限，正常 2-3 级就到
        cand = cur / "VERSION"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _read_version_file() -> str:
    """从 VERSION 文件读取版本号。

    故意不抛异常——版本读取失败不应阻塞服务启动，
    回退到「unknown」让 /api/agent/health 仍能返回 200，
    排查时一眼能看到问题（而不是连健康检查都失败）。
    """
    p = _find_version_file()
    if p is None:
        return "unknown"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


VERSION: str = _read_version_file()

# 版本号三段拆解，方便单独展示 major/minor/patch
_PARTS: tuple[int, ...] = tuple(
    int(p) for p in VERSION.split(".") if p.isdigit()
) if VERSION != "unknown" else (0, 0, 0)
MAJOR: int = _PARTS[0] if len(_PARTS) > 0 else 0
MINOR: int = _PARTS[1] if len(_PARTS) > 1 else 0
PATCH: int = _PARTS[2] if len(_PARTS) > 2 else 0
