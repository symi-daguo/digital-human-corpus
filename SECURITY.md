# Security

数字人语料采集服务（Mac 端 + Android 端）。定位：**局域网内私有语音语料管线**，
不向任何云端上传数据（LLM 为本地 mlx-lm，转写本地 onnx）。

## 产品级保障

| 控制项 | 行为 |
|--------|------|
| 网络绑定 | 0.0.0.0:8766（手机需 LAN 访问）；自签 TLS，Android 端 SPKI 指纹 pinning |
| 认证 | 全部敏感路由 `Authorization: Bearer`，恒定时间比较（`secrets.compare_digest`） |
| 双凭证隔离 | 手机端配对 Token 与 Agent API token（`data/agent.token`，0600）相互独立 |
| 启动校验 | `DH_TOKEN` 为空且绑定非 loopback → **拒绝启动**（防局域网裸奔） |
| 存储 | SQLite + text/corpus 备份；TLS 私钥 0600 且排除在备份外 |
| 数据最小化 | 原始音频日终（17:30）全量删除，仅保留转写文本 |
| 云 | 无遥测、无分析、无远程同步 |
| 输入校验 | 日期参数防路径遍历；数值头显式区间校验；声纹名过滤控制字符 |
| 多设备权限模型 | 数据空间 / 管家角色 / 本机 Hermes 开关**由服务端档案裁定**，客户端自报只当身份用；自动建档只补设备名，**不覆盖服务端设置**（v1.5.0） |
| Agent 出口 | `/api/agent/*` 只读 GET + 出口脱敏（秘密 → `[REDACTED]`）+ 不含音频/声纹数据 |

## Agent API

```bash
TOKEN=$(cat "$HOME/Library/Application Support/DigitalHuman/data/agent.token")
curl -k -H "Authorization: Bearer $TOKEN" https://127.0.0.1:8766/api/agent/health
```

`GET /api/agent/health` 公开且只返能力标志，不返任何语料。契约见
`skills/digital-human/SKILL.md`。

## 残余风险（诚实声明）

- 手机端配对 Token 有出厂默认值（`digital-human`）：远程轮换会断开手机端，
  需在手机设置页同步更新后重启服务。同 WiFi 攻击者拿到默认 token 可读写语料，
  **建议首次部署后立即在手机端与服务端同步改为随机值**。
- 转写文本内容为用户口述原文，服务端不做采集侧脱敏（原始稿是私有记录）；
  秘密防护依赖 agent 出口脱敏 + agent 侧二次扫描，启发式非穷尽。
- 语料目录（corpus/）含本人语音——生物特征数据；agent API 永不返回，但本机
  可读该目录的进程可访问。保护 macOS 用户账户。
- 同一 macOS 用户下任何可读 `agent.token` 的进程可读日报知识。
- 语音段上传依赖 LAN：同网段攻击者即使无法伪造 TLS 指纹，也可拒绝服务（占端口/干扰 ARP）。

## 轮换与事件响应

疑似本地泄露后：删除 `data/agent.token` 重启服务（自动重新生成）；
手机端 Token 在手机「设置 → Mac 服务连接」修改并在 `env.local` 同步 `DH_TOKEN`。
