# 部署文档

面向自己部署的人或替他执行的智能体。**从上往下做，每步给了判据；判据不过就别往下走。**

---

## 零、这套东西有哪些件

| 件 | 是什么 | 关键事实 |
| --- | --- | --- |
| 服务端 | Python（FastAPI），监听 `0.0.0.0:8766`（TLS）+ `127.0.0.1:8767`（明文，仅本机） | 对外只走 HTTPS，证书首次启动自动生成 |
| 工作台 | 服务端渲染的网页 | 壳内是 `http://127.0.0.1:8767/console`；局域网是 `https://<IP>:8766/console` |
| Mac 壳 | SwiftUI 菜单栏应用「数字人服务」 | 拉起服务、探活、托盘常驻、开机自启。**关窗口不退服务** |
| 桌面壳 | Tauri 应用（Windows / Linux） | 同上，复用工作台 |
| 数据 | `~/Library/Application Support/DigitalHuman`（Mac）<br>`%APPDATA%\DigitalHuman`（Win） | 在应用包外，升级不会带走 |
| 手机端 | Android App | 局域网连 8766 上传语料 |

---

## 一、模型下载（必做）

模型体积大、许可各异，**不入库**。放到 `~/Library/Application Support/DigitalHuman/models/` 与 `llm-models/` 下。

| 模型 | 用途 | 大小 | 来源 |
| --- | --- | --- | --- |
| `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17` | 离线 ASR | 229M | [sherpa-onnx 模型库](https://github.com/k2-fsa/sherpa-onnx/releases) |
| `FunASR-nano-onnx` | 二级 ASR（可选，提升准确率） | 1.5G | ModelScope / HuggingFace |
| `speaker_ecapa.onnx` | 声纹（说话人分离） | 小 | speechbrain |
| `silero_vad.onnx` | 语音活动检测 | 2.2M | 仓库已含（`app/app/src/main/assets/`） |
| `audio_tagging` | 非人声过滤 | 247M | YAMNet / AST |
| `Qwen3.5-4B-4bit` | 文本清洗 / 日报 / 对话 | 2.9G | HuggingFace（mlx-community） |
| `ming-3.0-tts-0.5b-mlx` | 语音播报（可选） | 1.5G | HuggingFace（inclusionAI） |

**判据**：`ls ~/Library/Application\ Support/DigitalHuman/models/` 能看到上表目录。

> Ming 不装也能跑——播报会退回 macOS `say`，只是音质差一些。

---

## 二、Mac 端（生产形态）

### 2.1 服务端依赖

```bash
cd server
pip install -r requirements.txt
```

**判据**：`python -c "import fastapi, sherpa_onnx, numpy"` 无报错。

### 2.2 构建菜单栏应用

```bash
cd macapp
./Scripts/build.sh
```

脚本会：编译 Swift → 打包内嵌 Python 运行时 → 装到 `/Applications`。

**判据**：
```bash
ls /Applications/数字人服务.app          # 存在
cat /Applications/数字人服务.app/Contents/Resources/VERSION   # 与仓库根 VERSION 一致
```

### 2.3 启动

```bash
open -a "数字人服务"
sleep 15
curl -sk --noproxy '*' https://127.0.0.1:8766/api/agent/health
```

**判据**：返回 `{"ok": true, "version": "..."}`。菜单栏出现图标。

> 首次启动会生成自签证书，`curl` 需要 `-k`。

### 2.4 数据目录

```
~/Library/Application Support/DigitalHuman/
├── data/tenants/<租户>/digitalhuman.db
├── data/tenants/<租户>/audio/<日期>/
├── data/tenants/<租户>/corpus/<日期>/
├── data/backups/            自动快照，保留最近 7 份
├── models/                  ASR / VAD / 声纹 / 音频标签
└── llm-models/              Qwen3.5-4B-4bit
```

---

## 三、Android 端

### 3.1 构建

```bash
cd app
./gradlew assembleRelease
```

**判据**：`app/build/outputs/apk/release/app-arm64-v8a-release.apk` 存在（约 39M）。

**签名**：需要 `keystore.properties` + `keystore/*.jks`（不入库）。没有的话会退到 debug 签名，**能装但不能覆盖升级**。

### 3.2 安装

```bash
../tools/install-apk.sh app/build/outputs/apk/release/app-arm64-v8a-release.apk <设备序列号>
```

**不要直接 `adb install`**：裸装后应用停在 Android stopped 状态，广播收不到、采集服务不自启。脚本做了 `install -r` + `am start` 两步，还会唤醒屏幕解屏。

**判据**：
```bash
adb shell dumpsys package com.symi.digitalhuman | grep versionName
adb logcat -d | grep "recordLoop 开始"
```
两行都有输出才算装上且采集中。

### 3.3 配对

1. Mac 端「配对」界面复制 Token 与地址
2. 手机 App → 设置 → 填地址与 Token → 保存
3. 设置页副标题显示版本号，连接状态「已连接」即成功

---

## 四、桌面壳（Windows / Linux）

```bash
cd desktop
npm install
npm run tauri build
```

**前置**：Rust 工具链、Node 18+。Windows 还需 WebView2 Runtime（Win11 自带）。

**注意**：本地 LLM 走 MLX，**只有 Apple Silicon 能跑**。Windows / Linux 上必须把模型配成云端 API（工作台 → 设置 → 模型提供商）。

---

## 五、常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `curl` 报证书错误 | 自签证书 | 加 `-k` |
| `Errno 61 Connection refused` | 注入了 `HTTP(S)_PROXY`，截走了 127.0.0.1 | `--noproxy '*'` 或 unset 代理变量 |
| 手机上传了但服务端没数据 | 走 `_QUIET_ACCESS_PATHS`，2xx 不写 access 日志 | 查 `uploaded_segment.received_at` 与手机端 `status=SYNCED` |
| 装完 APK 不采集 | 裸 `adb install` 导致 stopped 态 | 用 `tools/install-apk.sh` |
| 处理不启动 | 处在定时窗口外 | `tools/agg-mode.sh status`，需要即时处理就切 `realtime` |
| 播报没声音 | Ming 未装 | 正常，会退回 `say`；检查系统音量 |
| 内存占用高 | 实时模式下 mlx 常驻约 1.6GB | 平时用 `tools/agg-mode.sh window` |

---

## 六、运维命令

```bash
tools/agg-mode.sh status      # 处理模式与真实生效状态
tools/agg-mode.sh realtime    # 上传即处理
tools/agg-mode.sh window      # 定时 + 攒批

tools/health-check.sh         # 健康快照（进程 / 端口 / 内存 / 段数）
server/ctl.sh {start|stop|status}   # 服务端进程管理
```

**重启服务**（改完 Python 需重启才生效）：

```bash
pkill -9 -x DigitalHumanApp; pkill -9 -f 'server/run\.py'; sleep 3
open -a "数字人服务"
```

---

## 七、知识库对接（WeKnora 路线）

日报与语音语料的长期沉淀需要一个可检索的知识库。选型腾讯开源 **WeKnora**
（v0.8.3，MIT），本地源码位于 `/Volumes/hasskit/WeKnora`（未入本仓）。

### 形态确认（2026-10-23 调研）

| 形态 | 依赖 | 结论 |
| --- | --- | --- |
| **WeKnora-lite 单二进制** | SQLite + 内存队列，零外部依赖 | **最契合本机**；但 v0.8.3 未发布二进制资产，formula 指向的 v0.3.6-test 资产也已下架 |
| 标准版 docker-compose | PostgreSQL + Redis + MinIO | 本机资源重，不采用 |

### 对接设计（与数字人管线的接点）

1. **写入侧**：每日语料导出（`data/tenants/<租户>/corpus/<日期>/`）推送为
   WeKnora 知识库文档。**推送前必须过 `redact.redact()`**——凭证与 PII
   绝不入知识库（出口脱敏同一标准）。
2. **查询侧**：对话/日报智能体遇到「之前说过/历史记录」类问题时，检索
   WeKnora（REST API 或其 MCP server，仓库内 `mcp-server/weknora_mcp_server.py`），
   检索结果注入 LLM 上下文。
3. **检索种子**：`/summary` 已返回的 `keywords`（insights.extract_keywords）
   直接作为 KB 检索 query 的种子词，不必再对原文做理解。
4. **隐私红线**：知识库内容视为对外数据，与 agent 出口同一脱敏等级。

### 启用步骤（待上游发二进制后）

```bash
# 1) 获取 lite 二进制（上游 release 或源码 make 构建）
# 2) 启动（默认端口以官方 .env.lite.example 为准）
WEKNORA_DATA_DIR=~/Library/Application\ Support/DigitalHuman/weknora ./WeKnora-lite
# 3) 建知识库 + 推送当日语料（tools/weknora_push.py，待 lite 就绪后补充）
# 4) 智能体侧：SKILL.md 增加检索工具说明
```


---

## 八、macOS 签名与公证（1.0.2 起）

### 两者不是一件事

| | 签名（codesign） | 公证（notarize） |
| --- | --- | --- |
| 用什么 | **Developer ID 证书**（本机钥匙串） | **Apple ID + App 专用密码**（notarytool profile） |
| 做什么 | 给包盖身份章 | 上传给 Apple 云端做恶意软件扫描，签发「票」 |
| 没有会怎样 | Gatekeeper 直接拒绝 | 首次打开提示「无法验证开发者」，需右键 → 打开 |

本机凭据：

- 签名：`Developer ID Application: guoqing wang (48YT35UPKC)`
- 公证 profile：**`deskcap-notary`**（与 Desktop-Capture-Agent 共用，已存在）
- Apple ID：`303316404@qq.com`

### 命令

```bash
cd macapp
DH_SIGN_IDENTITY="Developer ID Application: guoqing wang (48YT35UPKC)" \
  ./Scripts/sign.sh dist/数字人服务.app
./Scripts/make-dmg.sh
DH_NOTARY_PROFILE=deskcap-notary ./Scripts/notarize.sh dist/数字人服务-X.Y.Z.dmg
```

### 踩过的两个硬坑

1. **site-packages 里的无扩展名可执行文件**（`torch/bin/torch_shm_manager`、
   `protoc-*`）不匹配 `*.so/*.dylib` 规则，Apple 会判 Invalid 三连：
   无 Developer ID / 无安全时间戳 / 无 hardened runtime。
   sign.sh 已加「Mach-O 兜底签名」段（本次兜底 191 个）。
2. **483MB dmg 上传会断**（`NWError 54 Connection reset by peer`）。notarytool
   不支持续传，只能重试；网络差时多跑几次，或换网络。公证本身很快（1-3 分钟），
   慢的永远是上传。
