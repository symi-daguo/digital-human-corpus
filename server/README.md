# Mac 服务端（数字人语料采集）

FastAPI 单服务：接收手机准实时增量上传的音频（段落落盘即传）→ 处理管线（非人声检测 → 两级 ASR → 说话人聚类 → LLM）→ 日报回流 → 语料导出 → 日终删除原始音频；另提供 `/api/agent/*` 只读知识端点供本地智能体学习日报。

> **架构分工**：手机端录音时用 Silero VAD 切好 ≤30s 语音段，Mac 端直接 ASR，**不做二次 VAD**
> （sherpa-onnx Python VAD 实测不稳定，早已不用；残留的 `app/vad.py` 已于 2026-09-02 删除）。
> 手机端只负责「切段 + 上传 + 展示」，全部智能处理都在 Mac 端。

## 处理管线

```
手机上传 WAV 段 (PUT /api/upload/{segId})
        ↓
   落盘 data/audio/{date}/{segId}.wav + 写 uploaded_segment 行
        ↓
手机通知完毕 (POST /api/done/{date})
        ↓
┌─────────────── 处理管线（run_for_date，幂等可重跑）───────────────┐
│ ① 非人声检测   AudioTagging(Zipformer/AudioSet 527 类)             │
│                is_speech=0 → 跳过转写，不进日报/语料               │
│ ② 两级 ASR     增量轮 SenseVoice-Small int8（58x 实时）            │
│                日终轮 Fun-ASR-Nano int8 + 热词（语义纠错）          │
│                whisper.cpp 仅作加载失败兜底                        │
│ ③ 说话人聚类   SpeakerEmbeddingManager 增量贪心聚类                 │
│                标签：本人/具名/甲/乙/丙…，持久化到 speaker 表       │
│ ④ 日报聚合     带 [说话人] 前缀 → LLM 去口气 → LLM 每日总结         │
│                （增量轮也跑，三版约 1 分钟内实时一致）              │
│ ⑤ 语料导出     仅「本人」人声段 → GPT-SoVITS 目录 + corpus.jsonl     │
│ ⑥ 清理         仅删 WAV 文件，数据库行保留（segment_flags 依赖它）  │
└────────────────────────────────────────────────────────────────┘
        ↓
手机轮询拉取 (GET /api/report/{date})，ready=true 后落本地 Room 库
```

**幂等设计**：`run_for_date` 只处理 `processed=0` 的新段，但日报聚合读取**当天全部人声段**，
所以任一次增量触发产出的都是完整日报。全部步骤成功才删原始音频。

## 环境要求

- Python **3.13**（本机 venv 实际版本；3.12+ 均可）
- macOS + Apple Silicon（mlx-lm 依赖 Metal）
- `whisper-cli`（`brew install whisper-cpp`）

## 本机现状（2026-09-02 核对）

| 组件 | 状态 | 位置 / 说明 |
| --- | --- | --- |
| Python | 3.13.12 | `server/.venv` |
| FastAPI | 0.141.1 | 声明 `>=0.115,<1.0` |
| Uvicorn | 0.52.4 | 声明 `>=0.32,<1.0` |
| Starlette / Pydantic | 1.6.0 / 2.13.5 | 显式声明，不再只靠传递依赖 |
| sherpa-onnx | 1.13.6 | 提供 声纹 / AudioTagging 两套能力（VAD 不在 Mac 端跑） |
| numpy | 2.5.2 | ✅ 已于 2026-09-02 显式声明 `>=2.0,<3.0` |
| python-multipart | 0.0.32 | `/api/pair` 上传需要 |
| whisper-cli | 1.9.2 | `/opt/homebrew/bin/whisper-cli`（2026-09-04 起降为兜底后端） |
| whisper large-v3-turbo | 已下载（1.6GB） | `server/models/ggml-large-v3-turbo.bin` |
| SenseVoice-Small int8 | 已下载 | `server/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`（增量轮，57x 实时。**不要换成 2025-09-09 版**，实测更差，见下方 A/B） |
| Fun-ASR-Nano int8（max1024） | 已下载 | `server/models/FunASR-nano-onnx`（日终轮；LLM 必须用 max1024 版，512 上下文 >20s 音频空输出） |
| 声纹 ERes2Net (200k) | 已下载（39.6MB） | `server/models/speaker_ecapa.onnx` |
| AudioTagging Zipformer | 已下载（259MB） | `server/models/audio_tagging/` |
| mlx-lm server | 0.31.3 | OpenAI 兼容 API `http://127.0.0.1:1234/v1` |
| Qwen3.5-4B-4bit | 已下载（3GB） | `$HOME/models/Qwen3.5-4B-4bit` |

> `models/silero_vad.onnx` 已无用：Mac 端不做二次 VAD，`vad.py` 已删除。
> Silero VAD 只在**手机端**（sherpa-onnx Android AAR）使用，模型随 APK 打包。

PoC 基线：11.3s 中文语音转写全对，耗时 5.1s（含模型加载，约 2.2x 实时）。

### ASR 选型实测（2026-09-05，60 段真实录音）

换模型不能靠「版本号更新了所以更好」来判断。用 `tools/asr_bench.py` 在同一批
真实录音上跑出来的结果：

| 模型 | 空转 | 术语命中 | 与参照一致 | 实时倍率 |
|------|------|---------|-----------|---------|
| Fun-ASR-Nano（参照） | 0 段 | 10 | — | 8.0x（无热词） |
| SenseVoice **2024-07-17**（在用） | 0 段 | 10 | **78.6%** | 56.9x |
| SenseVoice 2025-09-09 | 0 段 | 8 | 71.3% | 57.2x |

结论：**继续用 2024-07-17**。新版字数少 14%（丢内容）、与参照一致度低 7 个点
（实测「一键配置」被新版写成「一件配置」、「我抄不了」被写成「我不了」）。

### 热词的坑（同一个脚本测出来的）

| 热词配置 | 实时倍率 | 术语命中 | 内容质量 |
|---------|---------|---------|---------|
| 无热词 | **8.0x** | 10 | 正常 |
| 101 个行业词 | 2.9x | 10 | **退化**：一段从 37 字变 535 字（「可以了」复读 20 次） |

热词会写进解码上下文，把「场景/升级/面板」这种高频常用词反复示范，模型于是
复读它们，还慢 2.8 倍。**热词只写真专名**（人名/项目名），行业通用词的纠偏
交给下面的词表在文本层做。

## 安装

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

> ⚠️ **第 1 行的 `# -*- coding: utf-8 -*-` 不能删。** 这份文件里有中文注释，
> 而 pip 读取 requirements 的解码顺序是：BOM → 前两行的 PEP 263 编码声明 →
> 系统 locale（Windows 上是 cp1252）→ 第三步必然 `UnicodeDecodeError`。
> 报错发生在**解析阶段**、与具体包无关，很容易被误判成「依赖坏了」。
> 症状是 `'charmap' codec can't decode byte 0x8d`。
> （较新的 pip 会先试 utf-8，所以这个问题只在旧 pip / Windows 上暴露 ——
> 而内嵌 Python 安装包带的就是旧 pip，实测踩过。）
>
> `numpy` 必须显式声明：`voiceprint.py` 与 `audio_tagging.py` 直接 `import numpy`，
> 而 `sherpa-onnx` 只依赖 `sherpa-onnx-core`，**不传递 numpy**。已在 requirements 中固定 `>=2.0,<3.0`。
>
> 尚未生成 lockfile，如需完全可复现环境：`pip install pip-tools && pip-compile requirements.txt`。
>
> **跨平台安装（Windows / Linux 部署必读）**：`mlx-lm` 带 PEP 508 环境标记
> `; sys_platform == "darwin" and platform_machine == "arm64"`，在上述之外的平台会被
> 自动跳过。标记不是可选项——去掉它 pip 会在**解析阶段**直接失败，`fastapi`、
> `sherpa-onnx`、`numpy` 一个都装不上，桌面壳在这些平台上根本起不来。
> 这些平台不装 mlx-lm 不影响主流程：把 `DH_LLM_PROVIDER` 配成云端（DeepSeek 等）
> 即可，`config.llm_is_cloud()` 会走 HTTP 出网，完全不经过 mlx。

## 模型下载（国内镜像，勿放系统盘）

```bash
mkdir -p models

# 声纹：3D-Speaker ERes2Net base 200k 说话人版（39.6MB）
# 注意 tag 是官方拼写错误的 speaker-recongition-models（recognition 拼成 recongition）
curl -L --noproxy '*' -o models/speaker_ecapa.onnx \
  https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_200k_sv_zh-cn_16k-common.onnx

# 非人声检测：AudioTagging Zipformer（model.onnx 259MB + 标签 csv）
# 用于区分「人声」与「音乐/噪声/静默」。电视/音箱里的人声仍判为 speech（符合产品诉求）
curl -L --noproxy '*' -o /tmp/audio-tagging.tar.bz2 \
  https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/sherpa-onnx-zipformer-audio-tagging-2024-04-09.tar.bz2
mkdir -p models/audio_tagging
tar -xjf /tmp/audio-tagging.tar.bz2 -C /tmp && \
  mv /tmp/sherpa-onnx-zipformer-audio-tagging-2024-04-09/model.onnx models/audio_tagging/ && \
  mv /tmp/sherpa-onnx-zipformer-audio-tagging-2024-04-09/class_labels_indices.csv models/audio_tagging/ && \
  rm -f /tmp/audio-tagging.tar.bz2 && rm -rf /tmp/sherpa-onnx-zipformer-audio-tagging-2024-04-09

# whisper 模型：走 hf-mirror（ghfast 不支持 HF 路径；本机代理会拦，必须 --noproxy '*'）
curl -L --noproxy '*' -o models/ggml-large-v3-turbo.bin \
  https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

**声纹实测参考值**（真实人声，嵌入维度 512）：同人跨句 0.87，他人 0.13 / 0.05，阈值 0.6 区分度充足。

> **坑**：不要用 macOS `say` 合成音做声纹测试——所有系统音色共享同一声码器管线，
> 嵌入被合成通道特征主导（同人/他人均 ~0.96），会误判模型无区分能力；真实人声正常。
>
> **坑**：sherpa-onnx ≥1.13 的 `SpeakerEmbeddingExtractor` 构造参数是
> `SpeakerEmbeddingExtractorConfig(model=...)` 对象，**不再接受路径字符串**。

## LLM（去口气 + 总结）

方案：**mlx-lm server**（Qwen3.5-4B-4bit 仅 3GB，去口气/补标点/总结足够；27B 属过度配置，已弃用）。

```bash
# 启动（模型按需加载，首次请求约 10s，之后常驻内存约 3.5GB）
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  server/.venv/bin/python -m mlx_lm.server \
  --model $HOME/models/Qwen3.5-4B-4bit \
  --host 127.0.0.1 --port 1234
```

踩坑记录：

- 必须带 `HF_HUB_OFFLINE=1`，否则 mlx-lm 会尝试联网查 HF 缓存，被代理拦截报 502
- 请求体 `model` 字段必须传**模型目录绝对路径**（mlx-lm 按需加载机制决定）；由 `LLM_SERVED_ID` 承载
- Qwen3.5 是思考型模型，已通过 `chat_template_kwargs: {enable_thinking: false}` 关闭思考直出正文
- 备选：LM Studio / Ollama 同样兼容（改 `DH_LLM_BASE_URL` 与 `DH_LLM_MODEL` 即可；
  LM Studio 0.4.7 对手动放置的外部模型不索引，仅适合经其自身下载的模型）

## 行业词表（术语与同音错字纠偏）

一个行业 = `industries/` 下的一份 `.txt`。内置三份：

| 文件 | 行业 | 术语数 | 错字映射 |
|------|------|-------|---------|
| `smart_home.txt` | 智能家居 / 弱电集成 | 106 | 11 |
| `decoration.txt` | 装修 / 建材 | 86 | 3 |
| `general.txt` | 通用（不偏置） | 0 | 0 |

**词表格式**：一行一词，`#` 开头是注释，首行 `# label: xxx` 是手机端显示的中文名。
`错写=正写` 形式的行是同音错字映射（正写部分也会进术语表）。

**三条消费路径**：

1. **whisper prompt**（兜底后端）：行业术语拼进 `--prompt`，压低同音错字
2. **LLM 提示词**（去口气/分块/总结）：术语表 + `错写→正写` 示范
3. **Agent 术语表**：`/api/agent/days` 返回，供更强的模型二次纠偏

**纠偏到底靠什么**（这是踩过坑才定下来的）：

| 方案 | 实测结果 |
|------|---------|
| 只把术语表塞进提示词 +「遇到可疑词就改」 | 4 个真实错字纠对 **1 / 4** |
| 术语表 +「逐词扫描同音词」 | **1 / 4**，还臆造（「力量器」→「调光器」，因为术语表里有「调光器」） |
| 术语表 +「错写→正写」示范（单行输入） | **4 / 4** |
| 同上，但换成真实日报形态（多行 + 说话人标签） | **0 / 3** —— 复现不了 |

最后一行是关键：**4B 模型的指令遵循随输入形态漂移，同一个提示词换个输入就失效**。
最优提示词测出的 4/4 是偶然，不能当依赖。

所以主线改成**确定性替换**：`错写=正写` 是人工从真实转写里挑出来的高置信映射，
既然确定，就用 `str.replace` 落地 —— 100% 命中、零延迟、可预测、可回滚。
提示词那份保留作为补充（偶尔能纠到表里没有的漏网词），但不承担主责。

替换发生在 ASR 转写之后、入库之前（`llm.apply_industry_fixes`），
所以原始稿 / 干净稿 / 总结 / 语料全部用的是纠正后的文本。

**切换方式**：手机端「设置 → 行业词表」（写入 `data/industry.txt`，即时生效）；
或在 Mac 端 `echo decoration > data/industry.txt` 后重启。
`DH_INDUSTRY` 环境变量会把行业**锁死**（手机端切不动），只在运维需要时用。

**收录纪律**：`错写=正写` 只写**在真实转写里见过**的错字，不要凭语感推测。
误改比不改更糟——「走顶」在弱电和装修里本身就是正确术语，曾差点被写成错字映射。

## 工作台（浏览器控制台）

改完配置看不到东西是很痛苦的，所以在服务里内置了一个网页界面：

```
https://<mac-ip>:8766/console        # 本机访问用 https://127.0.0.1:8766/console
```

打开后填「配对 Token」（就是 `DH_TOKEN`）即可。四个页签：

| 页签 | 内容 |
|------|------|
| 概览 | 服务状态 / 模型可用性 / 当前行业 / 各日期段数与就绪状态 |
| 日报 | 选日期看原始稿 / 干净稿 / 总结三版 |
| 行业词表 | 点一下切换行业，并列出当前生效的同音错字映射 |
| 角色 | 已注册的具名声纹角色 |

设计上刻意做成**低开销**：

- 单个 HTML 内嵌 CSS/JS，**不引任何 CDN**——局域网服务不保证外网可达，引 CDN 会白屏
- 服务端只多一个返回字符串的路由，不引模板引擎、不做 SSR，内存开销约 20KB
- `/api/console/state` 把状态聚合成一个响应，前端不发多次请求
- 复用手机端 Bearer token（不用权限更大的 Agent token）

注意：自签证书首次访问浏览器会拦，手动信任一次即可。

## 运行

### 服务管理

> **2026-09-10 更新**：当前生产形态是 **Mac 菜单栏 App**（`/Applications/数字人服务.app`），
> 由 App 自己托管 API 与 mlx-lm 两个进程，数据落在 `~/Library/Application Support/DigitalHuman/`。
> 本节的 `ctl.sh` / `keepalive.sh` / `start-all.sh` 属于**脚本式部署（备选路径）**，
> 只在 App 不可用（CI、纯命令行调试、Linux/Windows 侧）时使用。
> **`~/dh-runtime` 已废弃**，本机不存在该目录——旧文档里的这条路径请忽略。

```bash
cd server                 # 仓库内（脚本式部署的入口目录）
./ctl.sh status           # 服务 + 守护状态
./ctl.sh start            # 启动（带守护：每 30s 健康检查，崩溃自动拉起）
./ctl.sh stop             # 停止（同时停守护）
./ctl.sh restart          # 重启
./ctl.sh mem              # 查看服务进程实时内存
./ctl.sh log              # 看最近日志
```

- **部署布局**：
  - 仓库（开发）：本仓库的 `server/` 目录
  - 生产（App 形态）：`/Applications/数字人服务.app`，内置 Python 运行时与依赖，
    服务端代码在 `Contents/Resources/server/`，数据在 `~/Library/Application Support/DigitalHuman/`
  - 生产（脚本形态，备选）：代码与依赖放内建盘，原因见下方「开机自启」的外置卷 TCC 坑
- **两个监听口**（2026-10-26 实测，用错就是连不上）：

  | 端口 | 协议 | 监听 | 谁用 |
  |---|---|---|---|
  | 8766 | **HTTPS**（自签证书 `CN=digital-human`） | `0.0.0.0` | 手机端在局域网上传 |
  | 8767 | 纯 HTTP | `127.0.0.1` | 桌面壳的回环工作台；`/api/tts/synthesize` 也在这个口上 |

  - 外部服务（工作台容器等）要连 **8767**：纯 HTTP 无证书问题，
    且容器可用 `host.docker.internal` **穿透到宿主的回环端口**
    （Docker Desktop 的 vpnkit 会转发这类连接）。
  - 8766 是自签证书，不配信任链的客户端握手直接失败 ——
    宿主机自己用 `curl -k https://127.0.0.1:8766` 才能通。
  - 容器内的 `127.0.0.1` 指向**容器自己**，不是宿主；宿主的回环要用
    `host.docker.internal` 或宿主 IP。
- **代码升级流程**：改完仓库 → 重打 bundle 并重启 App：

  ```bash
  macapp/Scripts/build.sh          # 重打 .app 并自动覆盖安装到 /Applications
  # 然后重启三进程（App 壳 + run.py + mlx-lm）
  ```
- `keepalive.sh` 是守护循环：服务不可达时自动拉起 `run.py`；`ctl.sh start` 会以 nohup 方式拉起它并写 pid 文件
- **环境变量**：运行时 `env.local`（不入 git，0600 权限）集中管理 DH_* 变量。
  `config.py` 在导入时**自动读取**该文件，因此无论 `./start-all.sh`、`./ctl.sh`
  还是直接 `python run.py`，配置都一致；命令行显式 export 的值仍然优先。
  模板见 `env.local.example`
- **完整启动入口 `./start-all.sh`（开机自启用它）**：先把 mlx-lm 拉起来并等到
  `/v1/models` 能列出模型（最长 180s），再 `exec` 进 keepalive 循环。只用 keepalive 也能跑，
  但 LLM 未就绪时管线会跳过去口气，日报只剩原始转写而日志里只有一行 warning
- **开机自启（当前生产形态，且是唯一自启路径）**：`~/Library/LaunchAgents/com.symi.digitalhuman.plist`
  指向 `/Applications/数字人服务.app/Contents/MacOS/DigitalHumanApp --autostart`
  （`RunAtLoad=true`、`KeepAlive=false`，launchd 拉起后由 App 自己托管 API 与 mlx-lm）。

  ⚠️ **不要再往系统「登录项」里加本 app**（系统设置 → 通用 → 登录项与扩展）。
  那是与本项重复的第二条自启路径，两者都在登录瞬间触发、互不知情；
  2026-09-18 实测因此出现「菜单栏两个图标，一个运行中一个未启动」，而且后启动的实例
  会 `pkill` 掉先启动那个正在跑的服务端子进程。从 **v1.5.13** 起壳内加了
  `flock` 单实例保护兜底（第二个实例会自己 `exit(0)`），但仍应只保留本项一条。

  验证 —— **判据在 v1.5.13 变了，别再用旧的「期望 runs = 1」**：

  ```bash
  launchctl print gui/$UID/com.symi.digitalhuman | grep -E "state|program|runs|last exit code"
  ```

  - 本实例若是**登录时由 launchd 拉起**的 → `state = running`、`pid` 有值、`runs` 递增
  - 本实例若是**用户双击 / `open -a` 先起来的**，launchd 这次拉起会被单实例守卫拒绝 →
    `state = not running` + **`last exit code = 0`**。**这是正确结果，不是故障**
    （第二实例按设计 `exit(0)` 秒退）。此时真实壳进程看
    `pgrep -x DigitalHumanApp`，锁持有者看
    `~/Library/Application Support/DigitalHuman/app.lock`
  - `last exit code = 9`（SIGKILL）只说明上一次是被 `kill -9` 收掉的（例如手动重启服务），
    **不代表当前有问题** —— 判「现在到底有几个实例」只看
    `pgrep -x DigitalHumanApp | wc -l`（期望 `1`；**必须带 `-x`** ——
    不带会按进程名做子串匹配，把别的进程一起数进来）

- **开机自启（脚本形态）**：不再提供 LaunchAgent 模板——旧模板
  `com.symidaguo.digitalhuman-server.plist` 指向已废弃的 `~/dh-runtime`（2026-10-12 已删除）。
  脚本形态**手动运行**即可（`./start-all.sh` / `./ctl.sh start`）；需要开机自启时用 App 形态
  （`com.symi.digitalhuman.plist`，见上）。
- **外置卷 TCC 坑（2026-09-03 对照实验定位，重要）**：`/Volumes/<你的卷>` 是外置卷，
  macOS 隐私保护下 **launchd 拉起的进程访问它一律 `Operation not permitted`**——
  终端/IDE 有「可移动卷」授权所以手工跑一切正常，极具迷惑性。实测矩阵：
  - 程序在外置卷 → `bash: .../start-all.sh: Operation not permitted`
  - 仅日志路径在外置卷 → launchd 打不开 StandardOutPath，spawn 直接中止
  - EX_CONFIG(78) 的具体错误只进系统日志（`log show --predicate 'process=="launchd"'`），
    任务自己的 stdout/err 文件一片空白，极易漏判
  - 顺带：仓库目录真名 `digital-human` 末尾含零宽字符 U+200C（`ls | cat -v` 可见），
    复制路径易踩坑，但这**不是** EX_CONFIG 的原因（纯 ASCII 符号链接同样失败）
  - 结论：LaunchAgent 的程序、WorkingDirectory、日志路径必须全在内建盘。
    当前生产形态（`/Applications/数字人服务.app`）已天然满足：程序在 `/Applications`、
    数据与日志在 `~/Library/Application Support/DigitalHuman/`，都不在外置卷上。
- 两种机制可共存：keepalive 有健康检查前置，服务已在跑时不会重复拉起
- **代理防御**：`run.py` 启动时会清掉进程内的 `HTTP(S)_PROXY` 等变量，`llm.py` 的本地请求
  一律直连——环境若注入代理（公司网络/科学上网），不修这两处的话 127.0.0.1 的 LLM 请求会
  被丢给代理拿 502，表现为干净稿/总结全空白而日志只有一行 502
- 内存实测（M1 Max）：空载约 78MB；首次处理（批处理/声纹注册）后 audio_tagging + 声纹模型
  常驻约 650MB；whisper.cpp 批处理为子进程瞬时峰值约 2GB（跑完释放）；LLM 为独立进程（约 2.5-4GB）

### 手动启动（原始方式）

```bash
export DH_TOKEN="你的配对token"            # 与手机端设置页一致；为空则不鉴权（仅调试）
export DH_WHISPER_BIN="$(brew --prefix)/bin/whisper-cli"
export DH_WHISPER_MODEL="$PWD/models/ggml-large-v3-turbo.bin"
export DH_SPEAKER_MODEL="$PWD/models/speaker_ecapa.onnx"   # 可选，config 已默认指向此处
export DH_LLM_BASE_URL="http://127.0.0.1:1235/v1"          # mlx-lm；LM Studio(1234) 会让思考型模型空回复
export DH_LLM_MODEL="qwen3.5-4b"
export DH_LLM_SERVED_ID="$HOME/models/Qwen3.5-4B-4bit"

python run.py          # 或 uvicorn app.main:app --host 0.0.0.0 --port 8766
```

自签 TLS（手机端 token 非空时自动走 https）：

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes -subj "/CN=digitalhuman"
export DH_TLS_CERT="$PWD/cert.pem" DH_TLS_KEY="$PWD/key.pem"

# 取证书 SHA-256 指纹，填到手机端「设置 → 证书指纹」，启用证书锁定
openssl x509 -in cert.pem -noout -fingerprint -sha256
```

**证书锁定（2026-09-02 新增，2026-09-04 修订实现方式）**：手机端原先信任所有证书（空 `X509TrustManager` + 恒真
`hostnameVerifier`），局域网内任何中间人都可窃听语音并截获 token。现改为自定义
`TrustManager.checkServerTrusted` 做 SPKI SHA-256 指纹校验（OkHttp `CertificatePinner` 实测
握手 5 次内必失败，已弃用），只信任指纹匹配的证书。

> 为何 `hostnameVerifier` 仍保持宽松？Mac 用 IP + 自签证书，证书里没有匹配的 SAN，
> 严格校验会 100% 失败。安全性由 pinning 承担——指纹对不上就直接断开，与主机名无关。
> 指纹留空 = 不启用 pinning（沿旧行为），设置页会用琥珀色文字提示「未锁定，存在中间人风险」。

### 环境变量全表

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DH_DATA_DIR` | `server/data` | 数据根目录（音频/文字/语料/SQLite） |
| `DH_TOKEN` | `digital-human` | Bearer 鉴权 token。**为空且绑定非 loopback 时拒绝启动**（防局域网裸奔）；建议部署后与手机端同步改为随机值。可用 `POST /api/console/rotate-token` 一键轮换（热生效，网页工作台安全卡与 Mac 设置页均有按钮） |
| `DH_RELAY_HINT` | `""` | 外网中转地址提示（如 `relay.example.com:39999`，**填域名，不要填公网 IP**）。经 `/api/console/state` 的 `relay_hint` 下发，手机端启动/保存设置时自动填进「外网中转地址」字段（**用户手填优先，绝不覆盖**）；配合手机端自适应端点：局域网探活成功走 LAN 直连，失败才走此中转 |
| `DH_HOST` / `DH_PORT` | `0.0.0.0` / `8766` | 监听地址与端口 |
| `DH_TLS_CERT` / `DH_TLS_KEY` | `""` | 自签证书路径；两者都设才启用 https |
| `DH_WHISPER_BIN` | `whisper-cli` | whisper.cpp 可执行文件路径 |
| `DH_WHISPER_MODEL` | `""` | ggml 模型路径（**必填**，否则转写全空） |
| `DH_SPEAKER_MODEL` | `models/speaker_ecapa.onnx` | 声纹模型路径 |
| `DH_VOICEPRINT_THRESHOLD` | `0.6` | 说话人聚类余弦相似度阈值 |
| `DH_AUDIO_TAGGING_MODEL` | `models/audio_tagging/model.onnx` | 非人声检测模型 |
| `DH_AUDIO_TAGGING_LABELS` | `models/audio_tagging/class_labels_indices.csv` | AudioSet 标签表 |
| `DH_AUDIO_TAGGING_SPEECH_THRESHOLD` | `0.25` | speech 概率阈值，**初值，需真实环境微调** |
| `DH_TIMESTAMP_GAP_MIN` | `10` | 逐字稿时间戳分桶间隔（分钟）：相邻段间隔超过它才插入「— HH:MM —」分隔行；0 关闭 |
| `DH_SUMMARY_MIN_INTERVAL` | `600` | 总结稿最小更新间隔（秒）：不足时增量轮沿用旧总结（干净稿/原始稿照常刷新），攒一批数据再重算；0 恢复每轮都算；日终 17:30 终稿不受限 |
| `DH_SWEEP_MIN` | `10` | 补跑扫描间隔（分钟）：服务端周期检查未处理段并自动补跑，手机不发 done / Mac 关机重启后数据也能续上；0 关闭 |
| `DH_DAILY_FINAL_TIME` | `17:30` | 日终终稿时间（本地）：到点服务端自己跑 final=1，不再依赖手机端定时器；错过（如关机）后开机补跑 |
| `DH_CATCHUP_DAYS` | `3` | 开机/周期补跑向前回溯的天数上限，避免翻整个历史库 |
| `DH_LLM_BASE_URL` | `http://127.0.0.1:1235/v1` | OpenAI 兼容端点（**mlx-lm**；LM Studio 1234 会让思考型模型空回复） |
| `DH_LLM_MODEL` | `qwen3.5-4b` | 模型名（用于 `/models` 可用性探测） |
| `DH_LLM_SERVED_ID` | `""` | 请求体 `model` 字段实际取值（mlx-lm 需填模型目录绝对路径）。留空时 `available()` 仍按服务可达判定 |
| `DH_LLM_PORT` | `1235` | start-all.sh 拉起 mlx-lm 用的端口 |
| `DH_OLLAMA_URL` / `DH_OLLAMA_MODEL` | — | 旧变量名兼容，会覆盖 `DH_LLM_*` |

## API（与 Android 端 `MacClient` 对齐）

所有手机端端点均需 `Authorization: Bearer <DH_TOKEN>`（恒定时间比较）。
`/api/agent/*` 为独立凭证（`data/agent.token`，启动自动生成，0600），与手机端 Token 分离。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（App 探测 ping） |
| PUT | `/api/upload/{segId}?device=&install=` | 上传一个 WAV 段（同 (install, segId) 幂等覆盖）。`install` 是客户端 `install_id`，服务端据此分配设备 `slot` 并把段落 id 映射为全局 id（见下） |
| POST | `/api/done/{date}?final=0\|1` | 触发处理管线：`final=0` 增量（ASR + LLM 都跑，不重转不删音频），`final=1` 日终（全段 Fun-ASR-Nano 重转 + LLM + 删音频）。省略参数按增量处理（向后兼容） |
| GET | `/api/report/{date}` | 日报 JSON（含 `markers` 与确定性「重要时刻」卡片） |
| POST | `/api/mark/{date}` | 标记此刻：`{"t":"HH:MM(:SS)","note":"可选≤120字"}`，落 `data/markers/{date}.json`，日报呈现「重要时刻」卡片（不依赖 LLM） |
| POST | `/api/pair` | 配对：multipart 上传 ≥30s 录音注册具名声纹（name 空 = 本人） |
| GET/DELETE | `/api/speakers` | 具名角色列表 / 删除（URL 含中文需 encode） |
| GET | `/api/industries` | 当前行业 + 可选行业（key/label/terms） |
| PUT | `/api/industry/{name}` | 切换行业（即时生效，清识别器缓存重建；404 无此词表，409 被环境变量锁定） |
| GET | `/console` | 工作台网页（无数据，免鉴权；数据走下面三个接口） |
| GET | `/api/console/state` | 工作台聚合状态（服务/模型/行业/日期/角色，一次拿全） |
| GET | `/api/console/report/{date}` | 某日三版日报 |
| PUT | `/api/console/industry/{name}` | 与 `/api/industry/{name}` 同逻辑，供工作台调用 |
| POST | `/api/backup` | 全量备份 text/ + corpus/ + DB 到 backups/（排除 tls/ 与 audio/） |
| GET | `/api/backup/list` | 备份列表（最多 100 条，按 mtime 倒序，返 `{file, size, mtime}`） |
| GET | `/api/backup/download/{name}` | 下载一份备份 zip（文件名白名单 + 父目录双保险，防目录穿越） |
| POST | `/api/backup/restore?confirm=1` | **破坏性**：multipart 上传备份 zip 覆盖当前数据。必填 `confirm=1`；只接受 `text/`、`corpus/`、`markers/`、`digitalhuman.db` 四个顶层；恢复前**自动留安全快照**（响应回传 `safety_backup`） |
| GET | `/api/tts/preview` | 用 TTS 试播当天真实数据，返回 WAV；播报稿放在 `X-TTS-Text` 响应头（URL 编码）供客户端核对 |
| POST | `/api/tts/synthesize` | **任意文本** → 语音，返回 16kHz 单声道 WAV（`{"text":"…","format":"wav"}`，文本上限 20000 字）。与 `/tts/preview` 共用同一个 `tts.synthesize` 与同一套后端探测：预览能响，正式合成就必须能响 |
| GET | `/api/profiles` | 设备档案 + 管家角色全量（含 `limits` 上限），供工作台渲染 |
| GET | `/api/profiles/self` | **当前设备自己**的档案：`registered` / `tenant` / `bot` / `bot_name` / `hermes_allowed`（按请求头 `X-Install-Id` 识别） |
| PUT | `/api/profiles/{install_key}` | 设置某设备的「数据空间 + 管家角色 + Hermes 开关 + 备注」 |
| DELETE | `/api/profiles/{install_key}` | 删除档案（**该设备已有数据保留在原租户**，不删数据） |
| GET | `/api/bots` | 管家角色列表（`default` 恒存在） |
| PUT | `/api/bots/{bot_id}` | 新建/更新角色：`name` / `persona` / `base_url` / `hermes` |
| DELETE | `/api/bots/{bot_id}` | 删除角色（`default` 不可删；关联设备自动回落默认角色） |
| GET | `/api/agent/health` | **只读**，公开，仅返版本与上限 |
| GET | `/api/agent/days` | **只读**，可学习日期 + ready + 人声段数 |
| GET | `/api/agent/report/{date}?since=&limit=` | **只读**，增量转写行（排他水位线 + `next_since`，出口脱敏，上限 200 行） |
| GET | `/api/agent/summary/{date}` | **只读**，总结卡片（文本已脱敏） |
| POST | `/api/push/workbench` | 把一段口述物料描述推进「智能家居 AI 工作台」建档成产品（见下节）。**可选**，未配置时返回 503 且不影响采集主链路 |

### 工作台对接（v1.0.5 新增）

把一段口述物料描述送进「智能家居 AI 工作台」，由工作台跑 LLM 结构化抽取，
判为产品类且无缺项时直接落库成产品。

请求体（`Authorization: Bearer <DH_TOKEN>`，与手机端同一个配对 Token）：

| 字段 | 说明 |
| --- | --- |
| `text` | **必填**，20 ~ 200000 字 |
| `title` | ≤100 字 |
| `docType` | `product` \| `meeting` \| `generic`，缺省 `meeting`（要落成产品必须显式传 `product`） |
| `ref` | 来源标识。**给了就幂等**：同一个 `ref` 第二次推返回 `duplicate=true`，不再打工作台 |
| `viaHermes` | 是否经 Hermes 中转，优先于部署默认值 |

响应是摘要（作业 id / 文档类型 / 缺项数 / 抽取得分），不是工作台的全量响应。

配置项（`server/env.local`，模板见 `env.local.example`）：
`DH_WORKBENCH_BASE_URL` / `DH_WORKBENCH_API_KEY` / `DH_WORKBENCH_TENANT_ID` /
`DH_WORKBENCH_TENANT_MAP` / `DH_WORKBENCH_TIMEOUT` / `DH_WORKBENCH_PUSH_VIA_HERMES`。

失败处理与别处一致：**任何失败都明确报错，不静默降级**。
经 Hermes 中转时，Hermes 不可达 → 503 并说清「不做静默直投」——
调用方以为经过了 Hermes、其实没有，那比直接报错更坏。

**完整契约、配置说明、故障排查与自检方法见
[`docs/workbench-integration.md`](../docs/workbench-integration.md)**。

自检（自带假 Hermes，不依赖本机真的 Hermes 在跑）：

```bash
"$APPPY" tools/check_push_and_tts.py
# → 通过 37 项，失败 0 项
```

其中第 [8] 组四条断言查的是「门禁隔离」：推送请求**必须**进自带的假工作台，
不能打真实工作台 —— 否则跑一次门禁就在生产库里留一批抽取作业与审计，
门禁本身成了数据污染源。判据落在数据上：假工作台把收到的请求写进
`received.jsonl`，脚本跑完读它，收不到就说明打去了别处。

### 设备档案与管家角色（v1.5.0）

**动机**：多台设备接同一个服务端时，「数据归谁」「能不能用本机 Hermes」「用哪个人格」不能交给客户端自说自话。

**三层身份，彼此正交**：

| 层 | 键 | 载体 | 作用 |
| --- | --- | --- | --- |
| 数据空间（租户） | `tenant` | `data/tenants/<id>/` | 目录级物理隔离：音频 / 语料 / 日报 / DB 各一份 |
| 设备命名空间 | `install_key` | `device_registry.slot` | 段落全局 id 前缀，两台设备段号撞车也不会互相覆盖 |
| **设备档案** | `install_key` | `data/profiles.json` | 绑定「数据空间 + 管家角色 + Hermes 开关」，**服务端权威** |

**存储**：单文件 `data/profiles.json`，**原子写**（先写 `.json.tmp` 再 `replace`），进程内带锁，
上限 **200 台设备 / 50 个角色**（超限拒绝写入，避免单文件无限膨胀）。

```json
{
  "version": 1,
  "profiles": {
    "4ef62cb402e77f1db13e52bf478462c6": {
      "install_key": "4ef62cb402e77f1db13e52bf478462c6",
      "label": "目标设备", "tenant": "default", "bot": "default",
      "hermes": true, "note": "", "first_seen": "…", "updated_at": "…"
    }
  },
  "bots": {
    "default":     {"id": "default",     "name": "默认管家", "persona": "", "base_url": "", "hermes": true},
    "demo_quiet":  {"id": "demo_quiet",  "name": "演示只记录", "persona": "", "base_url": "", "hermes": false}
  }
}
```

**权威性规则（重要）**：

1. **已登记设备**：上传时按档案里的 `tenant` 路由。客户端自报的租户若与档案不一致，
   以档案为准并记一条 WARNING（客户端设置可能过期）。
2. **未登记设备**：按自报租户路由，并在首次上传时**自动建档**（`label` 取 `device` 参数）。
   → **旧客户端零影响**。
3. ⚠️ **自动建档只补 `label`，绝不覆盖已有的 `tenant` / `bot` / `hermes`** —— 客户端永远无法夺回权限。

**Hermes 门控 = 「与」关系**：

```
最终是否可用 = 设备档案 hermes AND 所属管家角色 hermes
```

- 角色级关闭（管理台一个开关）→ 该角色下**所有**设备都停用本机 Hermes；
- 设备级关闭 → 只影响这一台，**不干扰其它设备**；
- **未登记设备（无档案）视为允许**，保持向后兼容。
- 关闭时**不是报错**：本轮降级为通用 AI 回答，`/api/talk/start` 返回 `fallback=hermes_off`
  （有意关闭），与 `ai_only`（管家不可达）区分，便于排障。

**会话隔离**：会话键是三元组 `tenant \x00 bot \x00 session_id`（`bot` 为空用 `-` 占位）。
设备 A 的历史不会被设备 B 或另一个角色读到。一轮对话在 `/start` 时就把
`bot` / `persona` / `base_url` / `hermes_allowed` 锁在 turn 上，**中途改设置不会让同一轮换人格**。

**curl 示例**：

```bash
B="https://127.0.0.1:8766"; A="Authorization: Bearer $DH_TOKEN"; K="4ef62cb402e77f1db13e52bf478462c6"

curl -sk --noproxy '*' -H "$A" "$B/api/profiles"
curl -sk --noproxy '*' -H "$A" -H "X-Install-Id: $K" "$B/api/profiles/self"   # 设备自查

# 建一个「只记录不打扰」的角色，并把某台设备挂上去
curl -sk --noproxy '*' -X PUT -H "$A" -H 'Content-Type: application/json' \
  -d '{"name":"演示只记录","hermes":false}' "$B/api/bots/demo_quiet"
curl -sk --noproxy '*' -X PUT -H "$A" -H 'Content-Type: application/json' \
  -d '{"tenant":"default","bot":"demo_quiet","hermes":true}' "$B/api/profiles/$K"
# → hermes_effective = true AND false = false（角色级关闭生效）
```

**实现坑**（改动时必看）：

- ⚠️ `GET /api/profiles/self` **必须注册在 `/api/profiles/{install_key}` 之前**，否则会被路径参数吃掉。
- ⚠️ 对话路径用的 `_tenant_of()` 辅助函数**必须写在 `@router.post("/start")` 装饰器上方** ——
  FastAPI 的装饰器绑定的是**紧跟其后的那个函数**，中间插辅助函数会让装饰器改绑（实测报
  `422 query x_tenant Field required`）。
- 未注册租户仍然 403；档案里指向不存在的租户时，加载阶段会兜底拉回 `default` 并告警（防孤儿档案）。

### 工作台「设备与管家」页签

`/console` 新增第四个页签，一张表把「设备 → 数据空间 / 管家角色 / 本机 Hermes」全列出来，
**改动即保存**（`onchange` 直接 PUT，无「保存」按钮）；下方是角色列表与新建表单。
概览页另有「硬盘占用（长期驻留看这里）」卡片，分项展示语料 / 备份 / 日志 / 数据库占用。

### 新空间接入：空间 → 设备 → 声纹 → 云端端口

让一台设备「自己采集的数据只进自己的空间」要动四处，照顺序做即可，**不用改代码**。
下面每一条都是 2026-09-15 给目标设备开机顶盒空间时实跑过的。

**① 建空间**——工作台 `空间管理 → 新增空间`，或直接打接口（`$DH_TOKEN` 见 Mac 应用
「概览 → 手机端配对」的 Token 行）：

```bash
curl -sk --noproxy '*' -X POST https://127.0.0.1:8766/api/console/tenants \
  -H "Authorization: Bearer $DH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"store-a","name":"目标设备"}'
```

- ID 受 `^[a-z][a-z0-9_-]{0,23}$` 约束（小写字母开头），显示名随便写中文。
- 建成即落 `data/tenants/<ID>/{audio,text,corpus,markers}/` + 一个建好表的空库。
- **开销可以忽略**：新空间实测 **80 KB**；服务端按租户**懒建连接**，没有请求就不占内存。
  真正吃盘的是 `audio/` 与 `corpus/` 里的语料本身 —— 那部分按空间各存一份，
  是隔离的必然代价，不是「多开空间」带来的额外损耗（对照：两周前建的 `zhangsan` 至今 40 KB）。

**② 把设备挂到空间**——两条路二选一：

- **客户端自报**（零配置）：设备第一次上传时按它带的 `X-Tenant` 自动登记一条档案。
  新装的手机在 App「设置 → 空间 ID」填上 ID 即可。
- **服务端指定**（权威）：工作台「设备与管家」页签直接改那一行的空间下拉，或
  ```bash
  curl -sk --noproxy '*' -X PUT https://127.0.0.1:8766/api/profiles/<install_key> \
    -H "Authorization: Bearer $DH_TOKEN" -H 'Content-Type: application/json' \
    -d '{"tenant":"store-a"}'
  ```
- 改完之后**以服务端为准**：设备那边即使还留着旧空间 ID，数据也会落到这里指定的空间
  （服务端记一条「自报与档案不一致」的告警）。反过来说，**已登记设备不能靠改客户端设置换空间**。
- 档案指向不存在的租户时，加载阶段会兜底拉回 `default` 并告警 ——
  所以**先建空间、再改档案**，顺序不能反。

**③ 已有数据要跟着搬**（改档案只影响之后的落点）：

1. **段号必须重算**。全局段号 = `slot × 10⁷ + 设备本机段号`，而 `slot` 是**每个空间内重新分配**的；
   空库空间的第一个设备拿 `slot 0`（见 `store.install_slot` 的「空库特例」），
   于是一段搬进新空间后会从 `20000004` 变成 `4`。
2. **音频文件名就是全局段号**（`audio/<日期>/<全局段号>.wav`）：改段号要同步改名，
   并把库里 `path` 列一起改掉。
3. `device_registry` 那条要一起搬，否则目标空间会当新设备重新发号；
   段里引用到的 `speaker` 匿名声纹簇也一并复制，免得标签悬空。
4. 搬完**服务端不会自动重算那天的日报**（该日期已是 `ready=1`）。重启服务后管线会按新段集
   重算（实测），或手动 `rebuild_report.py --tenant=<ID> <日期>`。

**④ 声纹注册到指定空间**——注册走 `POST /api/pair`（multipart，字段 `file`，
WAV 建议 ≥30 秒），**按 `X-Tenant` 落库，和上传不同、它不看设备档案**：

```bash
curl -sk --noproxy '*' -X POST "https://127.0.0.1:8766/api/pair?name=大锅" \
  -H "Authorization: Bearer $DH_TOKEN" -H 'X-Tenant: store-a' -F "file=@/path/to/30s.wav"
```

手机上更方便：临时把「空间 ID」改成目标空间，在「设置 → 声纹角色」录一次再改回来 ——
手机上传仍按档案路由，所以临时改一下**不会**把录音挪到别的空间去。

**也可以直接上传，不必拿手机录**（2026-09-16 起，两个入口）：

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| 工作台网页 | 「角色」页签 → 上传录音创建角色 | 选 WAV + 填角色名 → 提交；落进工作台右上角选中的**当前空间** |
| Mac 壳 | 侧栏「角色」→ 上传录音创建角色 | 同上；落进**工具栏选中的空间**，选好空间再传即可给任意空间注册 |

两处走的是同一个 `POST /api/pair`，角色名留空 = 注册为「本人」。服务端会重采样，
采样率不限；名字 1-16 字、不含方括号。同名再传一次 = 覆盖旧声纹（历史日报不受影响）。

⚠️ 手工拼 multipart 时**不要自己写 `Content-Type`**（要留给客户端填 boundary）；
Mac 壳 `APIClient.registerSpeaker` 与工作台 `FormData` 都是这个写法，实测 200。

**注册样本的口径由服务端下发**：`voiceprint.ENROLL_SECONDS / ENROLL_SCRIPT`，
经 `/api/console/state` 的 `enroll` 字段给到客户端。Mac 壳的角色页与工作台的角色页
都把它**原样显示**（含「复制文本」，方便把朗读稿发给要注册的人），两端不各写一份 ——
否则会出现「手机提示读 A、电脑提示读 B」，锚点可比性下降。

口径本身是**连续朗读同一段固定文字 30 秒**。为什么要固定文本：声纹是拿去做锚点比对的，
各读各的内容时样本差异里混进了「内容不同」这一项，锚点会偏。
Android 采集端本地也存了同一份文本（`Constants.ENROLL_SCRIPT`）—— 那边注册样本走
`EnrollQueue` 补交，断网时也要能录，不能依赖服务端下发。**改一处必须同步改另一处。**

**⑤ 云端端口**——设备不在同一局域网时走 frp。`frpc.toml` 里**一个空间一个端口**，按序顺延：

| 端口 | 用途 |
| --- | --- |
| 39999 | 默认空间（手机端） |
| 39998 | 手机侧预留（未启用） |
| 39997 | 目标设备空间（机顶盒） |
| 39888 | 其他设备入口（2026-09-22 新增） |
| 39996 及以下 | 新空间继续顺延 |

> ⚠️ **端点 ≠ 空间**：分端口只是「哪台设备走哪个入口」的运维标签。落哪个空间完全由
> 请求头 `X-Install-Id` + 设备档案（`data/profiles.json`）决定 —— 新设备接 39888 时，
> 它照样落自己原来的空间，**不需要在服务端做任何改动**。反过来，想给某台设备开
> **新空间**，要走的还是 `开新空间 → 挂设备 → 声纹注册 → 分配云端端口` 那条流程。

端口后面是同一个本机 API（`127.0.0.1:8766`），服务端靠 `X-Install-Id` + 设备档案区分
「哪台设备、落哪个空间」；分端口只为运维上「哪台设备走哪个入口」一眼可查，
日后也能单独给某个空间限速 / 断流。

改完 `frpc.toml` 的生效方式要注意：frpc 由 LaunchAgent `com.symi.digitalhuman.frpc` 托管
（KeepAlive），**`kill` 掉它会被 launchd 立刻按新配置拉起来**。不要顺手再手动 `frpc -c` 起一份 ——
两个进程会抢同名代理，后起的那个报 `proxy already exists` 并空转。

**心跳必须显式配**（2026-09-21 修，别再删）：`frpc.toml` 里那行
`transport.heartbeatInterval = 10` 不能省。云端 frps 的 `frps.ini` 写着 `heartbeat_timeout = 90`，
而 frps 判"活性"**只认心跳消息**（工作连接不算），客户端不显式配心跳就会每 90 秒被踢一次
——表现为 `frpc.log` 里 `login to server success` 每 90 秒循环，人体验则是"外网时好时坏"。
排障要点：这条**在 `log.level = "info"` 下也看得见**（重连会打 `try to connect`），
但要看清"客户端是被谁关的"必须开 `debug`（`control message dispatcher exited` 且无任何 `[W]/[E]`
= 服务端单方面关连接）。**先别急着怀疑"版本不匹配"**：实测把 frpc 降到与 frps 同版本照样每 90 秒被踢。

### PUT `/api/upload/{segId}`

请求头：

| 头 | 说明 |
| --- | --- |
| `X-Session-Id` | 手机端 session 主键 |
| `X-Install-Id` | 设备稳定标识（32 位随机 hex）。服务端据此分配 `slot`、查设备档案、决定数据空间 |
| `X-Start-Offset-Ms` | 段起始时间（epoch ms）→ **服务端据此推算日期** |
| `X-Duration-Ms` | 段时长 |
| `X-Speech-Ratio` | VAD 语音占比 |

请求体：WAV 原始字节。响应：`{"ok": true, "date": "2026-09-01"}`。

**多设备隔离（v1.5.0）**：`seg_id` 是**每台设备各自的自增号**，直接在服务端做主键会互相覆盖。
服务端用命名空间映射：注册表 `device_registry` 给每个 `install_key` 分配一个 `slot`，
**全局 id = `slot × 10 000 000 + 本机 seg_id`**（本机号原封存在 `uploaded_segment.src_seg_id`）。
音频文件名与库中主键都用全局 id，因此两台设备即使本机段号相同也互不覆盖。
`install_key` 优先用客户端 `install_id`（32 位随机 hex）；老客户端没有则回落 `label:<owner_name>`。
`/api/report/{date}` 的 `segment_flags` 回传的 `id` **仍是本机号**（客户端无需改动），并附 `install_key` 供客户端过滤他机段。

**数据空间路由（v1.5.0）**：段落落进哪个租户，**由服务端设备档案决定**，不看客户端自报。
`_resolve_tenant(tenant, x_tenant, install)` 三态：

| 情况 | 行为 |
| --- | --- |
| 未注册的租户名 | `403 unknown tenant` |
| 请求没带 `install`（老客户端） | 按自报租户走，**不建档** |
| 带 `install` 但无档案 | 按自报租户走 + **自动建档**（只写 `label`） |
| 带 `install` 且有档案 | **按档案的 `tenant` 走**；自报租户不一致时记 WARNING |

### GET `/api/report/{date}`

```json
{
  "ready": true,
  "raw_transcript": "— 10:05 —\n[本人] 今天去了超市\n[甲] 我也一起去过\n— 14:30 —\n[甲] 下午聊一下方案",
  "clean_transcript": "……（去口气后，时间标记行原样保留）",
  "summary": "……（每日总结）",
  "segment_flags": [
    {"id": 12, "is_user": true,  "is_speech": true,  "speaker": "本人", "speech_ratio": 1.0},
    {"id": 13, "is_user": false, "is_speech": true,  "speaker": "甲",   "speech_ratio": 0.95},
    {"id": 14, "is_user": false, "is_speech": false, "speaker": "",     "speech_ratio": 0.2}
  ]
}
```

字段语义：

- `is_user`：`true`=本人 / `false`=他人 / `null`=未判定（引擎不可用或聚类失败）
- `is_speech`：`false` = 非人声（音乐/噪声/静默），已跳过转写、不进日报与语料
- `speaker`：说话人标签（`本人` / `甲` / `乙` …），空串 = 未判定
- `speech_ratio`：手机端 VAD 语音占比
- **时间戳分桶**：逐字稿按段起始时间排序后，相邻段间隔 > `DH_TIMESTAMP_GAP_MIN`（默认 10 分钟）
  才插入一行 `— HH:MM —`。连续聊天聚在一块，长静默（话题切换/吃饭/出门）后才出现下一个时间戳。
  只标块起点，不逐句标

## 实时对话（`/api/talk`，Hermes 对接）

规格：`docs/hermes-voice-conversation-2026-09-10.md`

把手机变成 Hermes（AI 管家）的实时语音输入源：说一句「大锅，演示区灯全开」，
服务端转写 → 交给管家（带 HA 实时数据）→ 合成语音回传给手机播报。

这条链路与原有的「上传 → 日报」**并行不冲突**：同一段音频既进日报，也可进对话。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/talk/start` | 开一轮，返回 `turn_id`（默认 TTL 120s） |
| PUT | `/api/talk/audio/{turn_id}` | 上传 16k 单声道 WAV（≤10s / ≤2MB）→ 同步返回 ASR + 管家回复 |
| GET | `/api/talk/reply/{turn_id}` | 轮询回复（同步路径已带回结果，用于超时补拉） |
| GET | `/api/talk/audio/{turn_id}/reply.wav` | 下载 TTS 音频 |
| POST | `/api/talk/end/{turn_id}` | 结束：删音频、保留文字，回传播放时长给 Hermes |
| GET | `/api/talk/health` | 自检：TTS 后端 / Hermes 可达性 / 唤醒词表 |

### 三级降级（重要）

```
Hermes（含 HA 实时数据）
  ↓ 连不上 / 超时
纯 AI（复用 DH_LLM_* 配置直答）
  ↓ 也失败
只回文字，不合成语音
```

响应的 `hermes.via` 字段标明实际走了哪级：`hermes` / `ai_only` / `none`。
**任何一级失败都不会让用户「说完没反应」**——最差也有文字回复。

### TTS 后端

`DH_TTS_BACKEND` 可选 `auto` / `ming` / `piper` / `say` / `edge`：

- **`auto`（默认）的探测顺序是 Piper → macOS `say` → edge-tts —— 不含 ming**。
  ming 只能**显式**配 `DH_TTS_BACKEND=ming` 才会启用。
- 本机生产 **2026-10-26 实测生效的是 ming**（`DH_TTS_BACKEND=ming`，
  模型 `ming-3.0-tts-0.5b-mlx`，MLX 0.5B 8bit），产物经 `afconvert` 转 16k 单声道。

**分工是刻意的，不是降级**（`tts.backend(fast=)`）：

| 场景 | 走哪条 | 为什么 |
|---|---|---|
| 实时对话（`fast=True`） | **macOS `say`** | ming 每次要起子进程加载 1.5GB 权重（3~4 秒），对话回复只有几十字，等 4 秒才出声体验很差 |
| 长文本合成（日报播报、`/api/tts/synthesize`） | **ming** | 低频、能等，换取更好的发音鲁棒性 |

> 显式配了 `ming` 但撞上对话场景时，代码会**照样改走 `say` 并在日志里说明一次**
> （`TTS fast 路径跳过 ming（对话要低延迟），改用系统 say`），
> 不会为了「配置即生效」让对话慢 4 秒。

**合成耗时与字数成正比**（2026-10-26 实测）：

| 输入 | 产出 | 耗时 |
|---|---|---|
| 51 字 | 394,496 字节 | **12.20 秒** |
| 839 字 | 4,750,976 字节 | **148.34 秒** |
| 1420 字 | 8,621,696 字节 | **173 秒** |

约**每千字 120 秒**（首次还要算模型加载）。服务端自己的超时是
`max(60, 字数 × 2.5)` 秒 —— **调用方给的超时必须按这个量级给**，
否则长文本必然失败（工作台侧就因为默认 60 秒在 1421 字讲稿上失败过，改成了 300 秒）。

> 另一个实测坑：**llm 后端正在加载时会拖慢 ming**。
> 2026-10-26 10:09 的一次 28 字合成在 LLM 拉起 Qwen3.5-4B 的同时超时失败（503），
> 紧接着重试 2 个字 7.7 秒就过了 —— 同一个后端、同一份代码。
> 合成高峰与 LLM 加载撞在一起时偶发 503，重试即可，不必改代码。

> 声音名的坑：`say -v Eddy`（Flo / Grandma 同理）属 Apple「新奇声音」，
> 未下载时**退出码 0、不报错，但输出 0.02 秒空音频**，手机端表现为「点了没反应」。
> 默认声音写死 `Tingting`（经典中文女声，系统自带）。

### 唤醒词

`config.TALK_ALIAS_GROUPS` 与 Hermes 侧 `ALIAS_GROUPS` **逐条对齐**，共 9 组、
104 个同音变体。只有命中才调 Hermes，日常段落不受影响。

为什么需要同音变体：2026-09-10 实测 SenseVoice 把「大锅，演示区灯全开」转成
**「大过演示区灯全开」**——只写规范名会让这次唤醒直接丢失（用户喊了管家不理人）。
增量轮用的 SenseVoice 后端不支持热词，只能靠变体表兜住。

> 注意与 `SELF_ALIASES` 的区别：`SELF_ALIASES` 是「本人声纹的别称」（标注谁在说话），
> `TALK_ALIAS_GROUPS` 是「唤醒管家的话头」（决定要不要调 Hermes）。别混用。

### 相关配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `DH_HERMES_BASE_URL` | `http://127.0.0.1:9876` | Hermes 语音接收服务地址 |
| `DH_HERMES_TIMEOUT` | `30` | 超时后降级纯 AI |
| `DH_TTS_VOICE` | `Tingting` | macOS 声音名 |
| `DH_TTS_RATE` | `200` | 语速 |
| `DH_TALK_TTL_SEC` | `120` | 一轮有效期（超时自动回收音频） |
| `DH_TALK_MAX_TURNS_PER_DEVICE` | `3` | 单设备并发上限（超出 429） |
| `DH_TALK_ALIASES` | 空 | 额外唤醒词（内置表已覆盖常见说法） |

对话音频落在 `data/talk/<date>/`，**刻意不放 `data/audio/`**——sweeper 会把
`audio/` 下未处理的 wav 当漏传的日记段反复送进管线。文字记录进 `talk_message` 表。

## 日报重算（rebuild_report.py）

改动了 raw 格式或 LLM 提示词后，历史日期可用它按新逻辑重建（只读库内文本，不需要音频）：

```bash
./.venv/bin/python rebuild_report.py 2026-09-02 2026-09-01
```

LLM 不可用时只重建 raw（clean/summary 沿用旧值）；跑完自动更新 `data/text/` 双格式备份。

## 数据模型（SQLite，标准库 sqlite3）

| 表 | 用途 | 关键列 |
| --- | --- | --- |
| `uploaded_segment` | 上传段归档（处理后仍保留，供报表与语料查询） | `seg_id`(PK) `session_id` `date` `start_offset_ms` `duration_ms` `speech_ratio` `path` `text` `is_user` `is_speech` `speaker_label` `processed` `received_at` |
| `report` | 每日三件套 | `date`(PK) `raw` `clean` `summary` `generated_at` `ready` |
| `voiceprint` | 本人声纹（单行，`id=1` CHECK 约束） | `id` `embedding`(BLOB) `updated_at` |
| `speaker` | 说话人库（聚类 centroid 持久化） | `label`(PK) `embedding` `count` `updated_at` |

**迁移机制**：`store._migrate()` 用 `PRAGMA table_info` 检测缺失列并 `ALTER TABLE ADD COLUMN`。
新表（`speaker`）靠 `CREATE TABLE IF NOT EXISTS` 在**服务启动时**创建（`connect()` 内执行 SCHEMA）。

## 数据目录

```
data/
  tenants/<id>/            每个「数据空间」一份，物理隔离（目录级）
    audio/<date>/          上传音频（final 轮 / 17:30 日终后清理当天文件，行记录保留）
    text/<date>.json|.md   文字三件套备份
    corpus/<date>/         GPT-SoVITS 语料（wavs/ + list.wav.txt + corpus.jsonl）
    markers/<date>.json    手工标记（「重要时刻」卡片）
    digitalhuman.db        SQLite 权威库（段落 / 日报 / 声纹 / 设备注册表）
  backups/                 /api/backup 归档（zip；**无自动清理策略**）
  profiles.json            **设备档案与管家角色注册表**（原子写，v1.5.0）
  tenants.json             数据空间注册表
  agent.token              Agent API 凭证（0600，启动自动生成；删除后重启即重新生成）
  llm-clean-cache.json     干净稿分块缓存
  industries/ industry.txt 行业词表与当前行业
  tls/                     自签证书
```

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 日报 `clean_transcript` 为空 | `llm.available()` 判为可用但 `_chat` 失败返回 `""` | 检查 LLM 服务；见「已知限制」 |
| 说话人标签全是空串 | 声纹模型缺失或 `_embed` 抛异常 | 检查 `models/speaker_ecapa.onnx` 是否存在 |
| 转写全空 | `DH_WHISPER_MODEL` 未设置 | 见环境变量表 |
| `segment_flags` 永远为空 | 曾误用「删整行」清理 | 已修：只删 WAV 文件、保留数据库行 |
| 日报被清空 | `/api/done` 重复触发且无新段 | 已修：`run_for_date` 无新段时直接 return，不重建日报 |
| `speaker` 表不存在 | 数据库创建早于该表 | 重启服务即自动创建（`CREATE TABLE IF NOT EXISTS`） |
| LLM 报 502 | mlx-lm 联网查 HF 被代理拦截 | 启动时带 `HF_HUB_OFFLINE=1` |

## 已验证事项

- [x] API 全部实现，与 Android 端协议对齐（冒烟通过）
- [x] 处理管线端到端：上传 → 非人声过滤 → ASR → 说话人聚类 → 日报 → 备份 → 语料 → 删原始音频
- [x] whisper.cpp PoC 定版（2.2x 实时，中文全对）；stdout 时间戳已清洗
- [x] LLM 去口气/总结实测通过（Qwen3.5-4B-4bit：去口气 3.1s、总结 3.8s）
- [x] 非人声检测三分类正确：人声 Speech(0.999) / 白噪声 Static(0.794) / 音乐 Sine wave(0.894)
- [x] 说话人聚类跨批一致：`speaker` 表持久化 centroid，第二轮仍能归入同一簇
- [x] 日报带 `[说话人]` 前缀标注
- [x] 旧库迁移：`is_speech` / `speaker_label` 列自动补齐
- [ ] Qwen3-ASR-1.7B 对比 PoC（whisper 已达标，暂缓）
- [ ] M2 验收：连续 3 天无人工干预

## 2026-09-02 审查修复结果

审查共 10 项 P1，全部已修复并经**实际运行验证**（非仅编译通过）：

| # | 问题 | 修复 | 验证方式 |
| --- | --- | --- | --- |
| 1 | `requirements.txt` 缺 numpy | 重写为带上下界声明，显式加 `numpy>=2.0,<3.0` | 全新解析依赖树确认 |
| 2 | `asr.py` 多行转写被无分隔拼接 | `"".join` → `"\n".join` | 转写输出确认句子边界保留 |
| 3 | `llm.available()` 匹配逻辑脆弱 | 同时匹配 `LLM_SERVED_ID` 与 `LLM_MODEL`（双向子串）；`/models` 为空时判定可达并告警；`timeout` 1800→300 | 模拟 mlx-lm 返回路径型 id 命中 |
| 4 | `corpus.jsonl` 缺少日期过滤 | `export_corpus_jsonl` 增加**必需**参数 `date` + `AND date = ?` | 导出 2026-09-02 语料含 **0** 条 2026-09-01 数据 |
| 5 | `_embed` 用 `assert` 校验采样率 | 改 16k/单声道/最小帧数校验 + try-except 返回 `None`，与 `audio_tagging` 优雅降级一致 | 喂非法格式音频确认不再整批失败 |
| 6 | `/api/done/{date}` 未校验日期格式 | 新增 `config.is_valid_date`（正则 + `strptime`），`routes._require_date()` 拦截 | **7 个用例全通过**（含 `../../etc/passwd`、非法日期、空值） |
| 7 | SQLite 单例连接跨线程共享 | 加 `RLock` + `_query`/`_query_one`/`_write`（锁覆盖 execute+fetch） | **8 写线程×60 次 + 8 读线程 = 480 行，0 错误，无 `database is locked`** |
| 8 | 注册声纹未同步 `speaker` 表 | `register_user` 同时调 `replace_speaker_embedding("本人", emb)` | centroid 被覆盖且总数不变 |
| 9 | `vad.py` / `is_user()` 死代码 | 已删除 | grep 确认无残留引用 |
| 10 | `DH_ASR_BACKEND=qwen3_asr` 桩代码 | 已删除（含 config 中的开关） | 无 `qwen_asr` 依赖声明，启用即报错 → 移除 |

顺带清理：`config` 删 `CLIP_DIR` / `VAD_MODEL`，`main` 删未用 import，
`pipeline` 的 `create_task` 加模块级强引用 set 防 GC，`routes` 的 token 比较改 `secrets.compare_digest`。

> **一个反直觉的坑**：`routes.done()` 必须是 `async def`。改成同步 `def` 后 FastAPI 会在线程池执行，
> 而 `pipeline.trigger` 内部要 `asyncio.get_running_loop()` 创建后台任务，直接抛
> `RuntimeError: no running event loop`。**编译期发现不了，只有端到端 HTTP 请求才会暴露。**

## 已知限制与遗留待办

剩余（均为非阻塞）：

1. **`AUDIO_TAGGING_SPEECH_THRESHOLD=0.25` 为初值** — 需真实环境实测微调。
2. **段级聚类假设「一段 = 一个说话人」** — 一段内多人对话需升级 frame-level diarization（pyannote segmentation 模型约 90MB）。
3. **非人声段一旦标记无法回溯重标** — `processed=1` 后重跑不会重新判定；调阈值需手动重置该列。
4. **无 lockfile** — `requirements.txt` 只声明范围，未锁死版本；可 `pip-compile` 生成。
5. **手机端尚未做真机回归** — 2026-09-02 升级时设备未连接，待插机验证三种触发模式 + 说话人标签回流。
