# Hermes 语音桥接常驻（macOS LaunchAgent）

## 这是什么

`hermes_wake_receiver.py`（监听 9876，提供 `/hermes/voice/invoke`）是
「手机 → 数字人服务 → Hermes」这条链路的入口。它必须常驻，否则：

- talk 会退到 `quick_answer`（快，但**没有 HA 能力**）
- 与「HA 必须能用」的要求冲突

## 为什么需要 wrapper

桥接**自己不读 `~/.hermes/.env`**，只认进程环境变量。

手工 `set -a; . .env` 在 macOS 自带的 **bash 3.2** 下解析不可靠——实测
`MINIMAX_CN_API_KEY` 会被读成空串，桥接自报 `AI: minimax=False`，
所有回复退化成固定模板「我听到了您说…」。

`start-bridge.py` 用 Python 逐行解析后 `execve` 注入，实测注入 36 个变量。

> ⚠️ 必须用 `os.execve`（会传 env）；`os.execv` 不传 env，这是 2026-10-22 踩过的坑。

## 部署

```bash
# 1) 放 wrapper
cp start-bridge.py ~/.hermes/scripts/start-bridge.py
chmod +x ~/.hermes/scripts/start-bridge.py

# 2) 放 plist（按需改路径）
cp com.symi.hermes-bridge.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.symi.hermes-bridge.plist

# 3) 加载
launchctl unload ~/Library/LaunchAgents/com.symi.hermes-bridge.plist 2>/dev/null
launchctl load  ~/Library/LaunchAgents/com.symi.hermes-bridge.plist

# 4) 验证（应看到 AI: hermes_agent=True minimax=True）
grep -E "launcher|listening|AI:" ~/.hermes/logs/bridge.log | tail -5

# 5) 崩溃重拉验证
kill -9 $(pgrep -f hermes_wake_receiver); sleep 12; pgrep -af hermes_wake_receiver
```

## 设计要点

| 项 | 值 | 原因 |
| --- | --- | --- |
| 解释器 | `/usr/bin/python3` | 桥接只用标准库；系统解释器不随 WorkBuddy 升级消失 |
| `RunAtLoad` | true | 开机/登录即起 |
| `KeepAlive` | `SuccessfulExit: false` | 正常退出不重拉（避免端口占用时疯狂重启），崩溃才重拉 |
| `ThrottleInterval` | 10 | 最短 10 秒重试间隔 |
| `Nice` | 5 | 后台低优先级，不抢前台资源 |
| 日志 | `~/.hermes/logs/bridge.log` | launchd 会把 stdout/stderr 写这里 |

## 卸载

```bash
launchctl unload ~/Library/LaunchAgents/com.symi.hermes-bridge.plist
rm ~/Library/LaunchAgents/com.symi.hermes-bridge.plist
```
