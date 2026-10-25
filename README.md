# digital-human-corpus

**数字人语音语料采集与整理系统**——Android 全天录音，Mac 本地转写、分离说话人、去口气、出日报，并支持实时语音对话。

全部在本机完成，不上传云端。适合门店或办公空间做**长期对话语料沉淀**：谁在什么时候说了什么，一天结束得到一份可读的书面稿。

---

## 一、它解决什么问题

传统做法是录音 → 攒一堆音频 → 事后人工听写。问题是没人愿意听写，音频躺在那儿吃灰。

这个项目把整条链路自动化：

| 环节 | 做什么 |
| --- | --- |
| 采集 | 手机按「说话就录」策略全天分段录音，静音不占空间 |
| 转写 | Mac 端离线 ASR（SenseVoice），中文准确率高于通用云端方案 |
| 分离 | 声纹聚类，区分「谁在说话」并给出角色名 |
| 清洗 | 本地 LLM 把「嗯、那个、就是说」转成书面语，保留原意 |
| 汇总 | 按天生成日报，可直接复制给同事或存档 |
| 对话 | 唤醒词触发，问设备状态、查天气、控制灯，语音回答 |

---

## 二、下载安装包

不想自己编译的话，直接下现成产物：[**Releases**](https://github.com/symi-daguo/digital-human-corpus/releases/latest)

| 平台 | 产物 | 说明 |
| --- | --- | --- |
| Android | `app-arm64-v8a-release.apk`（39MB） | **推荐**，绝大多数手机 |
| Android | `app-armeabi-v7a-release.apk`（30MB） | 老旧 32 位设备 |
| Android | `app-x86_64-release.apk`（43MB） | 模拟器 / 极少数 x86_64 设备 |
| Android | `app-universal-release.apk`（129MB） | 不确定架构时用，全兼容 |
| macOS | `数字人服务-1.0.5.dmg`（505MB） | **Mac 生产形态：菜单栏应用**。内嵌 Python 运行时与全部依赖，**Developer ID 正式签名 + 已通过 Apple 公证**（拖进「应用程序」即可，无需右键→打开） |
| macOS | `DigitalHuman-1.0.0-macos-arm64.dmg`（499MB） | Tauri 桌面壳（Windows / Linux 同款界面）。Apple Silicon，Developer ID 正式签名（未公证，首次打开需右键→打开） |
| Windows | `DigitalHuman_1.0.0_x64-setup.exe`（40MB） | Win10/11 x64，**已内嵌 Python 运行时**，无需预装 |
| Linux | `DigitalHuman_1.0.0_amd64.deb`（3MB）/ `_amd64.AppImage`（78MB） | 需要系统 python3 + WebKitGTK |

> macOS 有**两个**产物，别下错：
> **`数字人服务-X.Y.Z.dmg` 是菜单栏应用（Mac 上的生产形态）**，
> 它把服务端（ASR / 说话人聚类 / LLM / 日报 / TTS）整个装进去，开菜单栏图标就能用；
> `DigitalHuman-*.dmg` 是同一套服务的 Tauri 界面壳，给 Windows / Linux 用，
> 也可以在 Apple Silicon 的 Mac 上跑，但功能与菜单栏应用一致、外形不同。

**平台限制**：本地 LLM 走 MLX（依赖 Metal），**只有 Apple Silicon 能跑**。
Intel Mac / Windows / Linux 上必须在工作台里把模型配成云端 API。

**Intel Mac 说明**：官方只发 Apple Silicon 的 dmg。Intel 机器请按下面「从源码构建」
自行编译 Tauri 壳（`cd desktop && npm install && npm run tauri build`），并先跑
`desktop/Scripts/prepare-python-mac.sh x86_64` 准备内嵌运行时。
> 这一步在 CI 上暂未跑通：`cryptography` 上游没有 macosx_x86_64 wheel，
> 源码编译又需要 OpenSSL 开发库。本机（Intel + 已装 `brew install openssl@3`）可以完成。

**架构说明**：Android 端只做采集；转写、说话人分离、LLM 清洗、日报都在 Mac 侧完成。
Windows / Linux 的桌面壳是"服务端 + 界面"的可视化外壳，功能与 Mac 版一致（除本地 LLM）。

---

## 三、从源码构建



### 环境要求

- **Mac**：Apple Silicon（M1 及以上）。本地 LLM 依赖 MLX / Metal，Intel Mac 跑不了
- **手机**：Android 9+（API 28），建议 2GB 内存以上
- **磁盘**：约 12GB（模型 9GB + 数据）

### 第一步：装 Mac 端

```bash
git clone https://github.com/symi-daguo/digital-human-corpus.git
cd digital-human-corpus

# 1) 装服务端依赖
cd server && pip install -r requirements.txt

# 2) 下载模型（体积大，不入库，清单见 docs/deployment.md）
#    - sherpa-onnx SenseVoice（ASR）
#    - Qwen3.5-4B-4bit（LLM）
#    - Ming-3.0-tts-0.5B（TTS，可选；不装则退回系统 say）

# 3) 构建并安装菜单栏应用
cd ../macapp && ./Scripts/build.sh
```

构建完成后会在 `/Applications` 生成「数字人服务.app」，菜单栏出现图标即启动成功。

### 第二步：装手机端

```bash
cd app && ./gradlew assembleRelease

# 走仓库自带脚本安装（会顺带解除 stopped 态并拉起采集）
../tools/install-apk.sh app/build/outputs/apk/release/app-arm64-v8a-release.apk
```

> **不要**直接 `adb install`。裸装后应用会停在 Android stopped 状态，广播收不到、采集服务不自启。仓库的 `install-apk.sh` 做了 `install -r` + `am start` 两步。

### 第三步：配对

1. 打开 Mac 端的「配对」界面，复制 Token 与地址
2. 手机 App → 设置 → 填入地址与 Token → 保存
3. 手机设置页副标题会显示版本号；连接状态显示「已连接」即成功

### 第四步：验证

对着手机说几句话，Mac 端工作台应能看到：段数增加、转写文字出现、声纹角色被标注。

---

## 四、日常使用

**设一次就行**：

| 配置 | 位置 | 说明 |
| --- | --- | --- |
| 采集时间段 | 手机设置 | 只在营业时间录，其余自动停 |
| 上传网络 | 手机设置 | 「仅 WiFi」或「WiFi + 流量」，离线期间不丢，回到网络自动补传 |
| 处理时机 | Mac 工作台 | 定时（如 13:00 / 17:45）或攒满 N 段，也可切成实时 |
| 播报音色 | Mac 设置 | Ming（高音质但慢）或系统 say（快） |

**每天你会得到**：

- 工作台「日报」页：当天对话的书面稿
- `~/Library/Application Support/DigitalHuman/data/tenants/<租户>/corpus/<日期>/`：导出的文本
- 原始音频：同目录 `audio/<日期>/`

**数据在哪、怎么备份**：

```
~/Library/Application Support/DigitalHuman/
├── data/tenants/<租户>/digitalhuman.db   数据库（段、文字、说话人、日报）
├── data/tenants/<租户>/audio/<日期>/      原始音频
├── data/tenants/<租户>/corpus/<日期>/     清洗后的语料
└── data/backups/                          自动快照（每次启动一份，保留最近 7 份）
```

**升级不会丢数据**：数据库在应用包外，APK 覆盖安装只换程序不碰私有数据，表结构迁移只加列不删列。详见第九节。

---

## 五、三种处理模式

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| **实时** | 上传即处理，秒级出结果 | 测试语音对话、需要即时反馈 |
| **定时** | 只在指定时间跑（默认 13:00 / 17:45） | 平时用，省电省内存 |
| **攒批** | 攒满 N 段（默认 30）再跑 | 白天没人管，晚上统一处理 |

切换命令：

```bash
tools/agg-mode.sh status     # 看当前模式
tools/agg-mode.sh realtime   # 切实时
tools/agg-mode.sh window     # 切定时 + 攒批
```

切换是**热更新**，不重启服务、不中断采集。

---

## 六、技术栈

| 层 | 选型 |
| --- | --- |
| Android | Kotlin 2.4.21 / AGP 9.4.1 / Compose BOM 2026.09 / Room / WorkManager |
| 服务端 | Python 3.13 / FastAPI / SQLite（WAL） |
| ASR | sherpa-onnx + SenseVoice-Small int8（离线） |
| VAD | Silero VAD |
| 声纹 | ECAPA-TDNN（speechbrain） |
| LLM | Qwen3.5-4B-4bit，经 mlx-lm 本地推理 |
| TTS | Ming-3.0-tts-0.5B（MLX）/ macOS say |
| Mac 壳 | SwiftUI（菜单栏） |
| 桌面壳 | Tauri 2 + Rust + Vue（复用工作台） |

---

## 七、目录结构

```
app/       Android 采集客户端
server/    FastAPI 服务（ASR / 聚类 / LLM / 日报 / TTS / 对话）
macapp/    macOS 菜单栏控制面板（生产形态）
desktop/   Tauri 桌面壳（Windows / Linux）
tools/     构建、装机、运维脚本
docs/      部署文档（模型清单 / 各平台注意事项 / 工作台对接）
```

---

## 八、文档

- [`docs/deployment.md`](docs/deployment.md)——模型下载清单、三端构建细节、Windows / Linux 注意事项、常见故障排查、macOS 签名与公证。
- [`docs/workbench-integration.md`](docs/workbench-integration.md)——**工作台对接**：把口述物料描述推进「智能家居 AI 工作台」建档成产品，含接口契约、配置项、经 Hermes 中转的语义、故障排查与自检方法。

其余信息以代码为准：接口定义在 `server/app/routes.py`，数据结构在 `server/app/store.py` 的 `SCHEMA`，都是自解释的。

---

## 九、数据保留与磁盘

| 数据 | 保留策略 | 说明 |
| --- | --- | --- |
| 原始音频 `audio/<日期>/*.wav` | 该日**日终（final）跑完后删除**；未终稿的**超 7 天自动删** | 1.0.3 起每天自动回收一轮 |
| 语料音频副本 `corpus/<日期>/wavs/` | 同上 | 与 audio 同一套规则（此前从未清理） |
| 低信息密度段音频 | ≥3 天即可优先删 | 无实体信号且有效字很少的「口气类」段 |
| 转写文本（数据库） | **永久** | 回收只删 wav，文本一条不动 |
| 语料 `corpus.jsonl` | **永久** | 日报与检索的数据源 |
| 日报 / 备份包 | **永久**（备份可手删） | `data/backups/` 越堆越多时手动清理旧包 |
| 日志 | 按天滚动 | `logs/api.log` |

可调参数（`server/env.local`）：`DH_RECLAIM_AFTER_DAYS`（默认 7）、
`DH_RECLAIM_LOWVALUE_DAYS`（默认 3）、`DH_RECLAIM=0` 可整体停用。

> 实测（2026-10-23）：按此规则回收 9999 个 wav / 3358.8 MB，空间 3430MB → 73MB；
> 抽查回收后的日期，`corpus.jsonl` 与数据库文本完整，日报接口正常。

---

## 十、数据安全

**为什么升级不会丢数据**（机制保证，不是口头承诺）：

1. 数据在 `~/Library/Application Support/`，在 `.app` 包外——换包不碰
2. APK 用 `install -r` 覆盖，只换 `/data/app`，不碰 `/data/data` 里的私有数据
3. **签名一致**才能覆盖安装；`versionCode` 必须递增
4. 建表全部 `CREATE TABLE IF NOT EXISTS`；迁移只 `ALTER TABLE ADD COLUMN`；全代码无 `DROP TABLE`
5. 每次启动自动做数据库快照，滚动保留最近 7 份

**自己再做一层**：定期把 `data/tenants/<租户>/` 整个目录复制到外部硬盘。

---

## 十一、许可

代码以 **MIT** 发布，见 [`LICENSE`](LICENSE)。第三方组件与模型的许可、以及各自的使用限制见 [`NOTICE.md`](NOTICE.md)。

> 模型权重不入库（体积与许可原因）。商用前请自行复核各模型条款，本项目不对第三方模型的可商用性作担保。
