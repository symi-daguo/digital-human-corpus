#!/usr/bin/env python3
"""桥接启动器：加载 ~/.hermes/.env 后 exec hermes_wake_receiver.py。

## 为什么需要这个 wrapper

`hermes_wake_receiver.py` **自己不读 `.env`**，只认进程环境变量。手工启动时
可以用 `set -a; . .env`，但实测 bash 3.2 解析不了里面带空格/特殊字符的值
（`MINIMAX_CN_API_KEY` 读出来是空串，桥接就自报 `AI: minimax=False`，
所有回复退化成固定模板「我听到了您说…」）。

所以这里用 Python 逐行解析（与已验证的 Python 注入逻辑一致），再 `execve` 把
环境变量完整交给桥接进程 —— `os.execv` 不传 env，必须用 `execve`，这是
2026-10-22 踩过的坑。

## 被谁调用

LaunchAgent `com.symi.hermes-bridge`（RunAtLoad + KeepAlive）。
崩溃后 launchd 会重拉，所以这里不需要自己写重试。
"""
from __future__ import annotations

import os
import sys

ENV_FILE = os.path.expanduser("~/.hermes/.env")
BRIDGE = os.path.expanduser("~/.hermes/scripts/hermes_wake_receiver.py")


def load_env(path: str) -> dict[str, str]:
    """逐行解析 KEY=VALUE，跳过注释与空行，剥掉成对引号。"""
    env = dict(os.environ)
    if not os.path.isfile(path):
        print(f"[launcher] 警告：{path} 不存在，桥接将用环境里的已有值", flush=True)
        return env
    n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k:
                env[k] = v
                n += 1
    print(f"[launcher] 已从 .env 注入 {n} 个变量", flush=True)
    return env


def main() -> None:
    if not os.path.isfile(BRIDGE):
        print(f"[launcher] 致命：找不到桥接脚本 {BRIDGE}", file=sys.stderr, flush=True)
        sys.exit(1)
    env = load_env(ENV_FILE)
    # 关键：-u 关缓冲，状态行立刻出现在 launchd 日志里
    os.execve(sys.executable, [sys.executable, "-u", BRIDGE], env)


if __name__ == "__main__":
    main()
