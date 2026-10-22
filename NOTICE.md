# NOTICE — 第三方组件与模型归属

本项目以 **MIT License** 发布（见根目录 `LICENSE`）。以下是运行所依赖的第三方组件与模型权重及其许可证。
**所有依赖均为宽松许可证（MIT / Apache-2.0），允许商用、修改与再分发，无传染性（copyleft）义务。**

## 一、代码依赖

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| Android Gradle Plugin / Kotlin / Jetpack Compose / Room / WorkManager / Navigation / AndroidX Core | Android 客户端 | Apache-2.0 |
| OkHttp / kotlinx.serialization / kotlinx.coroutines | Android 网络与序列化 | Apache-2.0 |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（k2-fsa） | 端侧 ASR / VAD / 声纹 / 音频标签推理框架 | Apache-2.0 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | 语音活动检测（`silero_vad.onnx`） | MIT |
| FastAPI / uvicorn / pydantic / python-multipart | Mac 服务端 Web 框架 | MIT |
| NumPy / soundfile | 音频数值处理 | BSD / Apache-2.0 |
| [MLX](https://github.com/ml-explore/mlx) / [mlx-lm](https://github.com/ml-explore/mlx-lm)（Apple） | Apple Silicon 本地 LLM 推理 | MIT |
| [Tauri](https://github.com/tauri-apps/tauri) v2 / Rust crates | Windows / Linux 桌面壳 | Apache-2.0 / MIT |
| cryptography | 自签 TLS 证书生成 | Apache-2.0 / BSD-3 |

## 二、模型权重（**不入库**，需按下方来源自行下载）

仓库刻意**不包含任何模型权重文件**（合计约 5GB，且体积随版本变化），构建/运行前请从各官方渠道获取：

| 模型 | 用途 | 许可证 | 官方来源 |
| --- | --- | --- | --- |
| SenseVoice-Small | 增量轮 ASR（快） | MIT | 阿里 FunASR / ModelScope `iic/SenseVoiceSmall` |
| Fun-ASR-Nano-2512 | 日终轮 ASR（准，带热词） | Apache-2.0 | HuggingFace `FunAudioLLM/Fun-ASR-Nano-2512` |
| Silero VAD (onnx) | 手机端 VAD | MIT | `snakers4/silero-vad` Releases |
| 3D-Speaker ECAPA-TDNN (onnx) | 说话人声纹 embedding | Apache-2.0 | 阿里 3D-Speaker（经 sherpa-onnx 导出） |
| PANNs audio tagging (onnx) | 非人声过滤 | 见模型卡 | 经 sherpa-onnx 导出 |
| Qwen3.5-4B（4bit MLX） | 去口气 / 分块 / 总结 | Apache-2.0 | 阿里 Qwen / HuggingFace / ModelScope |
| Ming-3.0-tts-0.5B（MLX 8bit） | 日报语音播报 | **MIT** | 蚂蚁 inclusionAI / HuggingFace `inclusionAI/Ming-3.0-tts-0.5B`（MLX 版 `mlx-community/Ming-3.0-tts-0.5B-8bit`） |
| whisper.cpp + large-v3-turbo（可选兜底） | ASR 兜底后端 | MIT | `ggml-org/whisper.cpp` |

下载与配置步骤见 `server/README.md` 的「模型下载」节。

## 三、字体

项目**不使用任何第三方字体文件**（仓库内无 `.ttf` / `.otf` / `.woff` / `.ttc`），全部渲染依赖操作系统自带字体：

- macOS 壳：SwiftUI `.font(.system(...))` → San Francisco（随 macOS 授权，应用内使用无需额外许可）
- Android：Compose 默认字体 → Roboto（Apache-2.0）
- 桌面壳 / Web 工作台：`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` 等系统等宽字体栈

因此**不存在字体版权或许可风险**。

## 四、数据与隐私

- 仓库不含任何用户数据：音频、转写文本、数据库（`*.db`）、声纹向量、token、密钥、内网地址均已通过 `.gitignore` 排除，且历次提交前经脱敏复核。
- **关于 git 历史**：本仓库为私有开发仓库，早期提交中曾出现内网 IP 字面量（当前版本已全部清理）。
  计划公开时将**从当前版本另行创建新仓库（不带 git 历史）**，并在公开前按本文件全项复扫一遍。
- 采集到的语料属用户私有数据，仅存于本机，不上传、不含遥测。

## 五、商标

「此项目」及相关标识为权利人商标。MIT 许可证授予的是代码使用权，**不授予任何商标许可**；
再分发时请勿以权利人名义背书或暗示官方关联。
