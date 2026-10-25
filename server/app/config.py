"""服务端配置：路径、鉴权、模型选型。全部可通过环境变量覆盖。"""
from __future__ import annotations

import base64
import os
import re
import secrets
from datetime import datetime
from pathlib import Path


def _env_file_candidates() -> list[Path]:
    """env.local 的候选位置，按优先级排列，取第一个真实存在的。

    为什么不止一个位置：打包成 .app 之后服务端代码位于**只读的 bundle 内**，
    把配置文件留在代码目录里，用户既改不了、下一次升级还会被整包覆盖。
    菜单栏 app 因此把 env.local 放在数据目录，并通过 DH_ENV_FILE 指过来。

    顺序：
      1. DH_ENV_FILE —— 显式指定，优先级最高（打包部署 / 多实例并存）
      2. 代码目录 env.local —— 源码部署与既有生产环境的既有行为，保持不变
      3. 数据目录的上一级 —— 未设 DH_ENV_FILE 时的兜底（DATA_DIR 一般是 <root>/data）
    """
    out: list[Path] = []
    explicit = os.environ.get("DH_ENV_FILE", "").strip()
    if explicit:
        out.append(Path(explicit).expanduser())
    srv_dir = Path(__file__).resolve().parent.parent
    out.append(srv_dir / "env.local")
    data_dir = os.environ.get("DH_DATA_DIR", "").strip()
    if data_dir:
        out.append(Path(data_dir).expanduser().parent / "env.local")
    return out


def _load_env_file() -> None:
    """加载 env.local（若存在），只填充尚未设置的环境变量。

    不能依赖「启动脚本先 source env.local」：手工执行 `python run.py`、
    用 IDE 调试、或被第三方进程拉起时都不会 source，于是所有 DH_* 配置
    静默回退到默认值 —— LLM 地址从 1235 掉回 1234，管线照常跑完，
    只有干净稿与总结是空的，日志里连一条 warning 都没有。
    这里从配置源头兜住，保证「怎么启动行为都一样」。

    已存在的环境变量优先（保持命令行覆盖能力），故只做 setdefault。

    支持 `$VAR` 展开：env.local 里大量使用 `$SRV_DIR/models/...` 这类写法，
    shell 的 `source` 会展开而这里不会，结果就是 whisper / 声纹 / AudioTagging
    的模型路径变成字面量 `$SRV_DIR/...` 而全部找不到。这里先预置 SRV_DIR
    再逐行 expandvars（用当前 os.environ 展开，故前面的定义可被后面引用，与 shell 一致）。
    """
    srv_dir = Path(__file__).resolve().parent.parent
    os.environ.setdefault("SRV_DIR", str(srv_dir))

    path = next((p for p in _env_file_candidates() if p.is_file()), None)
    if path is None:
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip()
        # 去掉成对引号；不去引号会把 "1235" 原样带进 URL，拼出 http://...:1235"/v1
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            os.environ.setdefault(k, os.path.expandvars(v))


_load_env_file()

# 数据根目录（原始音频临时区 / SQLite / 文字备份 / 语料导出）
DATA_DIR = Path(os.environ.get("DH_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
AUDIO_DIR = DATA_DIR / "audio"          # 上传音频暂存（处理完即删）
TEXT_DIR = DATA_DIR / "text"            # 文字三件套备份（JSON + Markdown）
CORPUS_DIR = DATA_DIR / "corpus"        # GPT-SoVITS 语料导出
DB_PATH = DATA_DIR / "digitalhuman.db"

# 日志目录：放在 DATA_DIR 的**父目录**下，与 Swift 外壳的 logsRoot 一致
# （~/Library/Application Support/DigitalHuman/logs/）。必须放对，否则会出现
# 「Swift 重定向写一份、Python logging 再写一份」的诡异双写，或写到没人看的
# 目录里导致用户以为日志丢了。
LOGS_DIR = Path(os.environ.get("DH_LOGS_DIR", str(DATA_DIR.parent / "logs")))
LOG_FILE = LOGS_DIR / "api.log"

# 鉴权：与手机端「配对 Token」一致。
# 刻意给非空默认值：手机端默认值必须与之相同，否则开箱即用会 401。
# 想要关掉鉴权（仅本机调试）就显式 DH_TOKEN="" —— 此时手机端 Token 也必须留空。
DEFAULT_TOKEN = "digital-human"
TOKEN = os.environ.get("DH_TOKEN", DEFAULT_TOKEN)

# ---- Agent 知识端点凭证（与手机端配对 Token 分离）----
# 独立随机 token，落 data/agent.token（0600），启动时自动生成。
# 泄露互不影响、可独立轮换（对齐 DeskCap 的 agent.token 模式）。
AGENT_TOKEN_FILE = DATA_DIR / "agent.token"
AGENT_TOKEN = ""


def ensure_agent_token() -> str:
    """读取或生成 agent token。只在启动时调用一次。"""
    global AGENT_TOKEN
    if AGENT_TOKEN:
        return AGENT_TOKEN
    if AGENT_TOKEN_FILE.is_file():
        AGENT_TOKEN = AGENT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not AGENT_TOKEN:
        AGENT_TOKEN = "dhag_" + secrets.token_hex(24)
        AGENT_TOKEN_FILE.write_text(AGENT_TOKEN + "\n", encoding="utf-8")
    try:
        AGENT_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return AGENT_TOKEN

# 服务绑定
HOST = os.environ.get("DH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DH_PORT", "8766"))
# 自签 TLS：手机端 Token 非空时一律走 https，所以服务端必须有证书。
# 默认落在 DATA_DIR/tls/ 下，首次启动由 ensure_tls() 自动生成，用户无需手工 openssl。
# 换成自己的证书就设置这两个环境变量。
TLS_CERT = os.environ.get("DH_TLS_CERT", str(DATA_DIR / "tls" / "cert.pem"))
TLS_KEY = os.environ.get("DH_TLS_KEY", str(DATA_DIR / "tls" / "key.pem"))

# ---- 桌面壳回环入口（2026-09-16）----
# 用途：桌面壳（desktop/，Tauri）要在**自己的窗口里**显示工作台，而 Tauri/wry
# 没有暴露任何证书例外 API，WebView 遇到自签 HTTPS 一律「导航失败」——窗口白屏，
# 且用户没有浏览器那种「高级 → 继续访问」的入口。所以壳不能加载上面那台 TLS 服务。
#
# 办法：服务端额外监听一个**只绑 127.0.0.1 的明文 HTTP**端口，专供壳内加载。
# 为什么不是「把 HTTPS 换成 HTTP」：手机端依赖 https + 证书指纹 pin，局域网也
# 需要 0.0.0.0 可访问，换协议等于同时打断这两条既有链路。
#
# 安全边界：
#   - 绑定地址写死 127.0.0.1，物理上出不了本机（对比 HOST 默认 0.0.0.0）；
#   - 只增一个**仅本机可见**的入口，既有 8766/TLS 一行未改，暴露面没有扩大：
#     本机进程本来就能直接读写 data 目录里的 SQLite；
#   - /console 页面本身不含数据，/api/console/* 仍走 Bearer token（console.py）；
#   - 不需要就在壳侧不设 DH_DESKTOP_HTTP（默认关）—— mac 原生壳与手工
#     `python run.py` 因此完全不受影响。
DESKTOP_HTTP_ENABLED = os.environ.get("DH_DESKTOP_HTTP", "0") == "1"
DESKTOP_HTTP_HOST = "127.0.0.1"
DESKTOP_HTTP_PORT = int(os.environ.get("DH_DESKTOP_HTTP_PORT", "8767"))

# ---- 模型选型 ----
# 两级 ASR（2026-09-04）：增量轮用 SenseVoice（快 58x，秒级出稿），
# 日终轮用 Fun-ASR-Nano + 热词（LLM 架构，语义纠错好，支持真热词）。
# 任一后端加载失败自动回退 whisper.cpp，管线不断。
# 可选后端：sensevoice / funasr_nano / whisper
ASR_BACKEND_INCREMENTAL = os.environ.get("DH_ASR_BACKEND_INCREMENTAL", "sensevoice")
ASR_BACKEND_FINAL = os.environ.get("DH_ASR_BACKEND_FINAL", "funasr_nano")

# 日终轮是否用终稿后端把当天全部人声段重转写一遍。
# 增量轮转过的段不会重跑（processed=1），若不重写，日终日报会永远停留在
# 增量后端的转写质量上。全天音频约几分钟，Nano 在 10x 实时下可接受。
ASR_FINAL_REWRITE = os.environ.get("DH_ASR_FINAL_REWRITE", "1") == "1"

WHISPER_CPP_BIN = os.environ.get("DH_WHISPER_BIN", "whisper-cli")
WHISPER_MODEL = os.environ.get("DH_WHISPER_MODEL", "")
ASR_LANGUAGE = "zh"
# ASR 术语引导（whisper initial prompt）由下方「行业词表」块统一组装：
# 用户自定义的 DH_ASR_PROMPT 会被保留并拼接行业词，不要在这里重复读它。
#
# 背景（2026-09-03 日报实测）：无引导时行业同音词错字率极高——「二维码」→
# 「奥维码/奥运码」、「浏览器」→「力量器」、「360」→「三油泥/三列人」、「卸载」→
# 「蟹/泄」、「升级」→「省极」、「版本」→「拔门」。whisper 的 --prompt 会显著
# 偏置解码词表，把领域高频词写进来即可大幅压低这类错字。注意：prompt 与音频
# 共享上下文窗口（max 为 n_text_ctx/2），保持在一两百 token 内，过长反而挤占识别空间。

# ---- 两级 ASR 模型路径（sherpa-onnx int8，ModelScope 下载） ----
# SenseVoice-Small：多语种 + 情感/事件标签，自带标点与 ITN，58x 实时。
SENSEVOICE_DIR = Path(os.environ.get(
    "DH_SENSEVOICE_DIR",
    str(Path(__file__).resolve().parent.parent / "models"
        / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"),
))
# Fun-ASR-Nano（LLM 架构 ASR）：encoder_adaptor + llm + embedding 三件套。
# llm 必须用 max_token_1024 版：默认 512 上下文下超过 ~20s 的音频会空输出。
FUNASR_NANO_DIR = Path(os.environ.get(
    "DH_FUNASR_NANO_DIR",
    str(Path(__file__).resolve().parent.parent / "models" / "FunASR-nano-onnx"),
))
FUNASR_NANO_LLM = os.environ.get("DH_FUNASR_NANO_LLM", "llm_int8_max1024.onnx")

# ---- 行业词表（2026-09-05）----
# 一个行业 = 一份词表文件（industries/<name>.txt，每行一词，# 开头为注释）。
# 词表同时喂给三条链路：
#   ① Fun-ASR-Nano 热词（真热词，命中率提升最直接）
#   ② whisper.cpp 的 --prompt（兜底后端的词表偏置）
#   ③ LLM 去口气/分块/总结提示词（同音词纠偏、行话理解）
# 新增行业只需丢一个 .txt 进去，无需改代码；手机端设置页可直接切换。
INDUSTRY_DIR = Path(os.environ.get(
    "DH_INDUSTRY_DIR", str(Path(__file__).resolve().parent.parent / "industries")))
DATA_DIR = Path(os.environ.get("DH_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
# 用户可编辑词表目录：App 的行业页把编辑后的词表写到这里。
# bundle 内的 INDUSTRY_DIR 只读（签名区），数据目录同名文件**覆盖**它——
# 这样既能拿内置词表当起点，又能让用户改动可持久化、升级不丢。
INDUSTRY_USER_DIR = DATA_DIR / "industries"
# 运行时选择的行业持久化在这里（手机端切换后重启不丢）。env 仍有最高优先级：
# 显式设了 DH_INDUSTRY 就以 env 为准，运维可强制锁定行业。
_ENV_INDUSTRY = os.environ.get("DH_INDUSTRY", "").strip()
INDUSTRY_FILE = DATA_DIR / "industry.txt"
# 租户追加名单（多租户，2026-09-09）：逗号分隔的租户 id，与 tenants.json 合并。
# 注意在模块加载处直接读 env——_apply_env 只在控制台保存配置时跑，启动路径不经过它。
ENV_TENANTS = os.environ.get("DH_TENANTS", "")

# 合法行业 key：字母数字下划线连字符（防路径穿越，行业名会拼进文件路径）
_INDUSTRY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def industry_file(name: str) -> Path:
    """某行业词表的实际文件：用户数据目录优先，bundle 内置兜底。"""
    if _INDUSTRY_KEY_RE.match(name or ""):
        u = INDUSTRY_USER_DIR / f"{name}.txt"
        if u.is_file():
            return u
        b = Path(INDUSTRY_DIR) / f"{name}.txt"
        if b.is_file():
            return b
    return Path(INDUSTRY_DIR) / "general.txt"


def read_industry_text(name: str) -> str:
    """词表原文（含注释行），给 App 编辑器做起点。取不到回空串。"""
    try:
        return industry_file(name).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_industry_text(name: str, text: str) -> Path:
    """把编辑后的词表写到用户数据目录（不动 bundle），返回写入路径。

    写完调用方必须 `_apply_industry()` + `asr.invalidate()` 才热生效。
    """
    if not _INDUSTRY_KEY_RE.match(name or ""):
        raise ValueError(f"bad industry key: {name!r}")
    INDUSTRY_USER_DIR.mkdir(parents=True, exist_ok=True)
    p = INDUSTRY_USER_DIR / f"{name}.txt"
    p.write_text(text.rstrip() + "\n", encoding="utf-8")
    return p


def _read_industry_file() -> str:
    try:
        return INDUSTRY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# 热词上限。Fun-ASR-Nano 是 LLM 架构，热词不是"权重加成"而是写进解码上下文，
# 塞多了会让模型失去停止条件。2026-09-05 用 60 段真实录音实测（同一批音频）：
#   无热词      8.0x 实时  术语命中 10  字数 1910
#   101 个行业词 2.9x 实时  术语命中 10  字数 2366（慢 2.8 倍，命中一个没多）
# 更糟的是内容退化：某段从 37 字变成 535 字（"可以了"复读 20 次）、某段后半截
# 变成"嗯嗯嗯嗯嗯"、"两千七到六千五"被写成"2000提到6000"。
# 根因：行业词表里全是「场景/升级/面板」这类高频常用词，把它们当热词偏置，
# 等于在每段解码前反复示范这些词，模型于是复读它们。
# 结论：热词只放真正罕见的专名（人名、项目名、自家产品名），且数量要少；
# 行业通用词的纠偏交给 LLM 提示词（不改解码，只在文本层改同音字）。
HOTWORD_MAX = int(os.environ.get("DH_HOTWORD_MAX", "30"))


def industry_label(name: str) -> str:
    """行业的中文显示名，取自词表首行注释 `# label: xxx`。

    标签放在词表里而不是 App 端的映射表里：新增行业只需丢一个 .txt 进去，
    手机端自动显示中文名，不必发版。取不到就退化成文件名本身。
    """
    f = industry_file(name)
    try:
        for line in f.read_text(encoding="utf-8").splitlines()[:5]:
            s = line.strip()
            if s.startswith("#") and "label:" in s:
                return s.split("label:", 1)[1].strip()
    except OSError:
        pass
    return name


def available_industries() -> list[dict]:
    """可选行业（按文件名排序）：[{"key","label","terms"}]。

    合并内置目录与用户数据目录：同名时数据目录生效（词表也读它的），
    这样 App 里编辑保存过的行业自然覆盖内置版本。
    """
    keys: dict[str, None] = {}          # dict 保序去重，内置在前、用户目录覆盖
    for d in [Path(INDUSTRY_DIR), INDUSTRY_USER_DIR]:
        if d.is_dir():
            for p in sorted(d.glob("*.txt")):
                keys[p.stem] = None
    return [{
        "key": k,
        "label": industry_label(k),
        "terms": len(load_industry_terms(k)),
    } for k in keys]


def _read_industry_file_lines(name: str) -> list[str]:
    if not name or name == "general":
        return []
    f = industry_file(name)
    if not f.is_file():
        return []
    out: list[str] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def load_industry_terms(name: str) -> list[str]:
    """行业正写词表（术语本体），用于 whisper prompt 与 Agent 术语表。

    `错写=正写` 形式的行取正写部分，与纯术语行合并去重。
    """
    out: list[str] = []
    for s in _read_industry_file_lines(name):
        out.append(s.split("=", 1)[1].strip() if "=" in s else s)
    return list(dict.fromkeys(w for w in out if w))


def load_industry_fixes(name: str) -> dict[str, str]:
    """同音错字映射 {错写: 正写}，取自词表里的 `错写=正写` 行。

    为什么需要它：光把术语表塞给 4B 模型几乎没用。2026-09-05 对照实测
    （4 个真实错字）：
      只给术语表 + "遇到可疑词就改"  → 1/4
      术语表 + "逐词扫描同音词"      → 1/4，还臆造出"力量器→调光器"
      术语表 + 几条 `错写=正写` 示范 → 4/4
    小模型不会自己判断"哪个词可疑"，但能照着给的例子照猫画虎。
    于是把纠错知识显式写成映射表，既是术语也是 few-shot 示范。
    """
    out: dict[str, str] = {}
    for s in _read_industry_file_lines(name):
        if "=" not in s:
            continue
        wrong, _, right = s.partition("=")
        wrong, right = wrong.strip(), right.strip()
        if wrong and right:
            out[wrong] = right
    return out


# 方言纠偏表（2026-09-17 加）：与行业词表**分开**、不分行业、始终生效。
#
# 为什么要单列：行业词表的语义是「讲这个行业时才需要」，切行业就该换一套；
# 而方言是「这个人一直这么说话」，切到任何行业都不该失效。六合/南京话的
# 「去啊」被 ASR 写成「K啊」（实测 2026-09-17 seg 10003329），两个后端都错，
# 而行业纠偏表里没有它的位置 —— 塞进 smart_home 就会在切到别的行业时失效。
#
# 后缀用 .fixes 而不是 .txt：industries/ 里的 *.txt 会被 available_industries()
# 当成「可切换的行业」列出来，而这个文件不是行业，不该出现在切换列表里。
DIALECT_FIX_FILES: tuple[Path, ...] = (
    Path(INDUSTRY_DIR) / "dialect.fixes",   # 内置默认，随应用分发
    DATA_DIR / "dialect.fixes",             # 用户覆盖，优先级更高
)


def _read_fix_lines(path: Path) -> list[str]:
    """读纠偏表的有效行（去空行、去注释）。文件不存在按空表处理。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [s for s in (ln.strip() for ln in raw.splitlines())
            if s and not s.startswith("#")]


def load_dialect_fixes() -> dict[str, str]:
    """方言纠偏映射 {错写: 正写}，**不分行业、始终生效**。

    两份文件按顺序合并（内置 → 数据目录），后者覆盖前者，与 industry_file()
    同一套「数据目录覆盖内置」规则 —— 于是能出厂带一份可用默认值，用户自己
    改一份就生效，且不会被应用更新冲掉。
    """
    out: dict[str, str] = {}
    for p in DIALECT_FIX_FILES:
        for s in _read_fix_lines(p):
            if "=" not in s:
                continue
            wrong, _, right = s.partition("=")
            wrong, right = wrong.strip(), right.strip()
            if wrong and right:
                out[wrong] = right
    return out


def dialect_fix_path() -> str:
    """方言表**用户可改**的文件路径（给工作台显示「去哪儿改」）。

    刻意返回数据目录那份、而不是实际生效的那份：实际生效的可能是应用包内的
    内置默认（`Contents/Resources/...`），那个目录在应用更新时会被整体替换，
    显示出来等于引导用户去改一个会被覆盖的文件。数据目录这份不存在时
    内置默认照样生效，在那里新建一份即可覆盖。
    """
    return str(DATA_DIR / "dialect.fixes")


def _apply_industry(name: str) -> None:
    """重算行业派生配置（词表 / 热词 / whisper prompt）。

    三处派生值都是模块级全局，运行时切换行业必须整体重算，
    否则会出现「UI 显示已切换但热词还是旧行业」的假切换。
    """
    global INDUSTRY, INDUSTRY_TERMS, INDUSTRY_FIXES, ASR_HOTWORDS, ASR_PROMPT
    INDUSTRY = name
    INDUSTRY_TERMS = load_industry_terms(name)
    # 方言纠偏 + 行业纠偏合并（2026-09-17）。顺序不能反：行业表在后，
    # 同名条目以行业表为准 —— 行业纠偏是「当前上下文里更具体的那条」。
    INDUSTRY_FIXES = {**load_dialect_fixes(), **load_industry_fixes(name)}
    # 热词只用用户自定义的专名（人名/项目名/产品名），不拼行业词表 —— 这是
    # 2026-09-05 实测推翻旧设计的结论，见下方 HOTWORD_MAX 注释。
    ASR_HOTWORDS = " ".join(
        list(dict.fromkeys(
            w for w in os.environ.get("DH_ASR_HOTWORDS", "").split() if w
        ))[:HOTWORD_MAX]
    )
    # 行业词改走 whisper prompt（兜底后端，偏置机制与热词不同，实测有效）
    # 与 LLM 提示词（文本层纠同音字），不动主后端的解码。
    _extra = (" 行业术语：" + "、".join(INDUSTRY_TERMS[:60])) if INDUSTRY_TERMS else ""
    ASR_PROMPT = os.environ.get("DH_ASR_PROMPT", "") + _extra


def set_industry(name: str) -> str:
    """切换当前行业并持久化。未知行业名 → 抛 ValueError。"""
    if name not in {i["key"] for i in available_industries()}:
        raise ValueError(f"unknown industry: {name}")
    if not _ENV_INDUSTRY:      # env 锁定时不写文件，避免徒增困惑
        try:
            INDUSTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
            INDUSTRY_FILE.write_text(name, encoding="utf-8")
        except OSError as e:
            raise ValueError(f"cannot persist industry: {e}") from e
    _apply_industry(name)
    return INDUSTRY


# 初始行业：env 优先，否则用上次持久化值
_apply_industry(_ENV_INDUSTRY or _read_industry_file())

# 声纹：3D-Speaker ERes2Net（200k 说话人版）ONNX（sherpa-onnx speaker embedding）
SPEAKER_MODEL = os.environ.get(
    "DH_SPEAKER_MODEL",
    str(Path(__file__).resolve().parent.parent / "models" / "speaker_ecapa.onnx"),
)
VOICEPRINT_THRESHOLD = float(os.environ.get("DH_VOICEPRINT_THRESHOLD", "0.6"))

# 具名角色（用户显式注册的「本人/张三/李四」）的匹配阈值。
# 默认比聚类阈值高一点：具名命中会直接用真实姓名写进日报与语料，
# 宁可漏（落到匿名聚类）也不错标成人名。
NAMED_VOICEPRINT_THRESHOLD = float(
    os.environ.get("DH_NAMED_VOICEPRINT_THRESHOLD",
                   os.environ.get("DH_VOICEPRINT_THRESHOLD", "0.6"))
)

# 「本人」标签：注册本人时固定用这个名字，本人语料筛选也以它为准
SELF_LABEL = os.environ.get("DH_SELF_LABEL", "本人")

# 本人别名（2026-09-08）：用户可能用昵称注册过自己的具名声纹锚点（实测「大锅」），
# 数据里会同时出现「本人」与昵称两种标签。is_user 判定与总结稿本人/他人拆流
# 都必须认全别名，否则本人的发言会被当成他人总结（用户硬要求绝不混）。
# 配置：DH_SELF_ALIASES=大锅,老王（逗号分隔；SELF_LABEL 條在内，无需重复写）。
# 注意：模块加载在 _load_env_file() 之后，这里必须直接读 env——
# _apply_env() 只在控制台保存配置时才跑，启动路径不经过它。
SELF_ALIASES: set[str] = {SELF_LABEL} | {
    x.strip() for x in os.environ.get("DH_SELF_ALIASES", "").split(",") if x.strip()
}

# 非人声检测：sherpa-onnx AudioTagging（Zipformer + AudioSet 527 类）
# 用于区分「人声」与「音乐/噪声/静默」，电视里的人声仍视为 speech。
AUDIO_TAGGING_MODEL = os.environ.get(
    "DH_AUDIO_TAGGING_MODEL",
    str(Path(__file__).resolve().parent.parent / "models" / "audio_tagging" / "model.onnx"),
)
AUDIO_TAGGING_LABELS = os.environ.get(
    "DH_AUDIO_TAGGING_LABELS",
    str(Path(__file__).resolve().parent.parent / "models" / "audio_tagging" / "class_labels_indices.csv"),
)
AUDIO_TAGGING_SPEECH_THRESHOLD = float(os.environ.get("DH_AUDIO_TAGGING_SPEECH_THRESHOLD", "0.25"))

# 说话人聚类的命名（本人固定第一，其余按天干顺序）
SPEAKER_OTHER_LABELS = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]

# 逐字稿时间戳分桶：相邻两段开始时间差超过该分钟数时，插入一行「— HH:MM —」。
# 用户诉求：不想逐句标时间戳（太碎），但纯文本完全看不出哪段话是几点说的。
# 按间隔分桶是折中：连续对话（间隔小）自然归入同一个时间块，跨话题的长静默
# 之后会出现新时间戳。设为 0 可关闭。
TIMESTAMP_GAP_MIN = int(os.environ.get("DH_TIMESTAMP_GAP_MIN", "10"))

# 总结稿节流：距离上次总结生成不足该秒数时，增量轮直接沿用旧总结（干净稿/原始稿
# 照常刷新）。总结要通读全天内容，4B 模型一次要跑几分钟，一段一段跟着算既拖慢
# 管线又没必要——用户定调：总结攒一段时间的数据再更新，保证时段覆盖完整即可。
SUMMARY_MIN_INTERVAL = int(os.environ.get("DH_SUMMARY_MIN_INTERVAL", "600"))

# 补跑扫描（2026-09-08）：处理不能只靠手机端 /api/done 事件——手机离线、
# Mac 关机、补传历史日期时没人发 done，段就永远停在未转写（实测 09-05 有 7 段、
# 09-06 有 3 段滞留，09-06 连日报都没有）。服务端必须自己兜底：
#   SWEEP_MIN         周期扫描未处理段的间隔（分钟），0 关闭
#   DAILY_FINAL_TIME  日终终稿时间（本地），到点服务端自己跑 final=1
#   CATCHUP_DAYS      开机补跑时向前回溯的天数（避免翻整个历史库）
SWEEP_MIN = int(os.environ.get("DH_SWEEP_MIN", "10"))
DAILY_FINAL_TIME = os.environ.get("DH_DAILY_FINAL_TIME", "17:45").strip()
CATCHUP_DAYS = int(os.environ.get("DH_CATCHUP_DAYS", "3"))

# 音频处理窗口（2026-09-27，资源优化任务书·任务 A 的服务端实现）：
#   AGG_WINDOWS  逗号分隔的 HH:MM 列表。只有窗口内的会话才允许跑管线（转写/聚类/LLM），
#                窗口外 /api/done 与 sweeper 一律不触发 —— 落库照常、处理延后。
#                默认「13:00,17:45」= 用户定的两段式；空串 = 关闭窗口，回到实时处理。
#   AGG_SPAN_MIN 窗口会话时长：从窗口时刻起允许**启动**新管线的分钟数（窗口内已开跑的
#                轮次不受影响，跑完为止）。积压大时一个会话内连续多轮消化，直到清空。
# 为什么用「时刻开启的会话」而不是整点一次性：错过窗口（Mac 睡着/关机）后，只要在
# 会话时长内醒来，sweeper 照常补跑；终稿（DAILY_FINAL_TIME=17:45）落在最后一个窗口内。
AGG_WINDOWS_RAW = os.environ.get("DH_AGG_WINDOWS", "13:00,17:45").strip()
AGG_SPAN_MIN = int(os.environ.get("DH_AGG_SPAN_MIN", "90"))
# 数量触发（2026-10-21）：待转写的人声段积压到该值就启动处理，跑完自动停、
# 释放内存。与固定窗口**可同时开启**（任一满足即触发）；0 = 关闭数量触发。
# 想要「上传即处理」的实时模式：把 DH_AGG_WINDOWS 留空、且本项设 0。
AGG_BATCH_MIN = int(os.environ.get("DH_AGG_BATCH_MIN", "30"))
_AGG_WINDOWS_MIN: tuple[int, ...] | None = None   # 惰性解析缓存；_apply_env 时重置

# LLM：OpenAI 兼容本地 API（mlx-lm / LM Studio / Ollama 均提供 /v1 端点）
# 默认后端：python -m mlx_lm.server（Qwen3.5-4B-4bit 仅 3GB，去口气/总结足够）
# LM Studio: http://127.0.0.1:1234/v1   Ollama: http://127.0.0.1:11434/v1
LLM_BASE_URL = os.environ.get("DH_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = os.environ.get("DH_LLM_MODEL", "qwen3.5-4b")
# mlx-lm server：请求体 model 字段需为模型目录路径，与 LLM_MODEL 通常不同。
# 默认置空（= 同 LLM_MODEL）：绝不把本机绝对路径写进默认值，避免换机器静默失配。
LLM_SERVED_ID = os.environ.get("DH_LLM_SERVED_ID", "")
# 兼容旧变量名
if os.environ.get("DH_OLLAMA_URL"):
    LLM_BASE_URL = os.environ["DH_OLLAMA_URL"].rstrip("/") + "/v1"
if os.environ.get("DH_OLLAMA_MODEL"):
    LLM_MODEL = os.environ["DH_OLLAMA_MODEL"]

# ---- 本地 / 云端 ----
# local  = 本机 mlx-lm（默认，语料不出网，需 2.9GB 模型 + 约 3GB 常驻内存）
# cloud  = 调云端大模型 API（省下载与内存，8GB 内存的 Mac 也能跑，代价是文本出网）
LLM_PROVIDER = os.environ.get("DH_LLM_PROVIDER", "").strip().lower()
# 请求协议：openai（绝大多数厂商）/ anthropic（Claude 原生协议，请求体与头都不同）
LLM_PROTOCOL = os.environ.get("DH_LLM_PROTOCOL", "openai").strip().lower()
# 云端 API Key。留空也能连本地服务（mlx-lm / Ollama 都不校验），
# 但云端厂商一律 401，所以连通性测试会显式提示「缺少 API Key」。
LLM_API_KEY = os.environ.get("DH_LLM_API_KEY", "").strip()
LLM_TIMEOUT = int(os.environ.get("DH_LLM_TIMEOUT", "300"))


def llm_is_cloud() -> bool:
    """是否走云端 API。

    provider 显式写死时以它为准；没写时按 base_url 推断——只有回环地址算本地。
    这样老配置（只有 DH_LLM_BASE_URL 指向 127.0.0.1）无需改动即保持本地语义，
    而把地址改成 api.deepseek.com 就会自动切到云端，不会出现「填了云端地址
    却仍然去连本机 1234 端口」的静默错误。
    """
    if LLM_PROVIDER in ("cloud", "remote", "api", "online"):
        return True
    if LLM_PROVIDER in ("local", "mlx", "offline"):
        return False
    host = (LLM_BASE_URL or "").lower()
    return not any(h in host for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))


def llm_base_url() -> str:
    """规范化后的 base_url（去掉结尾斜杠）。"""
    return (LLM_BASE_URL or "").rstrip("/")


# 云端出网代理。run.py 为本地调用已经把 HTTP(S)_PROXY 清空了，所以云端只认这一项，
# 由用户在设置里显式填；不填就是直连（国内厂商一般直连即可）。
LLM_PROXY = os.environ.get("DH_HTTPS_PROXY", "").strip()

# ---- 实时对话（Hermes 对接，2026-09-10）----
# 数字人服务作为 Hermes（AI 管家）的实时语音输入源：手机上传 6-8s 短段 →
# 服务端 ASR → 调 Hermes /hermes/voice/invoke → TTS 合成回播。
# 规格见 docs/hermes-voice-conversation-2026-09-10.md
HERMES_BASE_URL = os.environ.get("DH_HERMES_BASE_URL", "http://127.0.0.1:9876").rstrip("/")
HERMES_TIMEOUT = float(os.environ.get("DH_HERMES_TIMEOUT", "30"))
HERMES_RETRY = int(os.environ.get("DH_HERMES_RETRY", "3"))
# 未配置 Hermes 时降级为「纯 AI」（复用 DH_LLM_* 直调），不阻断对话。
# 注意：默认值是 127.0.0.1:9876——它是「本机 Hermes 存在与否」的探测地址，
# 不是「必须存在」的断言；探不通就静默降级，不打 ERROR 刷屏。

# ---- 门店 Home Assistant 事实源（2026-09-11 加）----
#
# 门店唯一家居事实源是**本机 Docker 部署的 HA**（127.0.0.1:8123），
# 它才有继电器 1-6 这类实际可控设备。局域网里另有独立的一套 HA 实例（另一台主机），
# 里面没有这些实体 —— 把两套搞混会让管家"看不见"门店设备。
#
# 服务端这里只做**只读**：设备概览 + 按关键词检索。
# 真正的"执行动作"在 Hermes 侧（hermes_wake_receiver + symi_devices 的
# 确定性执行层），服务端不重复实现一份控制逻辑，避免两处行为漂移。
HA_URL = os.environ.get("DH_HA_URL", "http://127.0.0.1:8123").rstrip("/")
HA_TOKEN = os.environ.get("DH_HA_TOKEN", "")
# 设备快照缓存时长（秒）。6000+ 实体的索引重建约 1s，不能每轮对话都重建；
# 但也不能太长 —— 设备开关状态是用户会立刻回看的，陈旧数据比慢更糟。
HA_CACHE_TTL = float(os.environ.get("DH_HA_CACHE_TTL", "60"))
# 单次 HTTP 超时。HA 是本机进程，1.5s 足够；超时即回落上一次快照，
# 不让"HA 慢"传导成"管家不回话"。
HA_TIMEOUT = float(os.environ.get("DH_HA_TIMEOUT", "3"))

# ---- 智能家居 AI 工作台对接（可选，2026-10-25 加）----
# 把采集到的口述物料推进工作台做结构化抽取（工作台侧会跑 LLM 抽取，
# 判定为产品类且无缺项时直接落库成产品）。
#
# 这是**可选对接**：没配好时 /api/push/workbench 返回 503 并说明缺哪一项，
# 采集主链路（录音 → ASR → 日报）完全不受影响。
#
# 为什么基址有默认值而凭据没有：基址是「工作台在哪」这种部署事实，
# 写死本机默认省一次配置；而 API Key 是机器凭证，只能落 env.local（不入库）。
WORKBENCH_BASE_URL = os.environ.get("DH_WORKBENCH_BASE_URL", "http://127.0.0.1:3080").rstrip("/")
WORKBENCH_API_KEY = os.environ.get("DH_WORKBENCH_API_KEY", "").strip()
WORKBENCH_TENANT_ID = os.environ.get("DH_WORKBENCH_TENANT_ID", "").strip()
# 采集端租户 → 工作台租户 id 的映射（JSON 串，如 {"default":"<uuid>"}）。
# 缺省时所有租户都用 DH_WORKBENCH_TENANT_ID：多数部署只有一个工作台空间，
# 没必要为它引入映射概念。真要一对一时再开这个开关。
WORKBENCH_TENANT_MAP_RAW = os.environ.get("DH_WORKBENCH_TENANT_MAP", "").strip()
# 单次推送超时（秒）。工作台侧要走 LLM 结构化抽取，比普通 API 慢一个量级；
# 但也不能无限等——否则一条坏请求能把采集端请求线程挂死。
WORKBENCH_TIMEOUT = float(os.environ.get("DH_WORKBENCH_TIMEOUT", "120"))
# 投递工作台时是否**强制经 Hermes 中转**。
#
# 需求是「hermes 把采集数据传给工作台做方案」。Hermes 自己的接口只有语音
# invoke/ack（一条「理解 + 应答」链路），**没有对外投递数据的通道**，
# 所以这里的分工是：**Hermes 负责理解与判定，采集端负责执行投递** ——
# 文本先过 Hermes 一遍（它会做意图归一、查实时设备数据、给出一句应答），
# 再由采集端把它投进工作台建档。
#
# 默认 0：直投是零依赖的最小路径，多数部署不需要这一跳。
# 置 1 后 Hermes 不可达就**明确报 503**，绝不静默直投 ——
# 否则调用方以为经过了 Hermes，其实没有；那比直接报错更坏。
WORKBENCH_PUSH_VIA_HERMES = os.environ.get("DH_WORKBENCH_PUSH_VIA_HERMES", "0") != "0"

# 一轮对话（turn）的有效期。手机端断线后服务端靠这个回收 wav，
# 避免 data/talk/ 无限增长（sweeper 不扫这目录，见下）。
TALK_TTL_SEC = int(os.environ.get("DH_TALK_TTL_SEC", "120"))
# 单个音频上限：规格写 ≤10s／≤1MB，留 2 倍余量容纳不同编码。
TALK_MAX_BYTES = int(os.environ.get("DH_TALK_MAX_BYTES", str(2 * 1024 * 1024)))
# 单轮音频时长上限（秒）。
#
# ★ 2026-09-11 从 10 提到 20：原值 10 与客户端的录音上限直接冲突——
#   TalkController.TALK_VAD_MAX_SEC 是 15（手动模式「说到说完」），
#   SherpaVad.forTalk 的 TALK_MAX_SPEECH 是 8（AUTO）。手动模式能录 15 秒、
#   服务端只收 10 秒，中间那 5 秒全是黑洞：日志实测 24 次 400
#   （`audio too long (12.3s > 10s)`），而客户端又把 400 吞成 null → 报「对话超时」。
#   用户感受就是「话说长一点就没反应」，内容连一条记录都不留。
#   取 20 而不是 15：留 5 秒余量，慢语速长句、句尾带停顿都不会踩线。
TALK_MAX_SEC = float(os.environ.get("DH_TALK_MAX_SEC", "20"))
# 每设备并发上限（规格风险表：超出返 429），防止脚本刷爆 TTS/LLM
TALK_MAX_TURNS_PER_DEVICE = int(os.environ.get("DH_TALK_MAX_TURNS_PER_DEVICE", "3"))

# ---- 唤醒词的角色：从「硬门」降级为「提示」（2026-09-11 重新规划）----
#
# 原设计里不含唤醒词的轮次在我们这侧就被丢掉（status=no_alias），
# 实测 930 轮里 753 轮（81%）走了这条路——**用户明明说了话，Hermes 却从不知道**。
# 这正是「说话稍微多一些文字就有概率不传给 Hermes」的本体。
#
# 改成 True（默认）之后：只要有 ASR 文本就发给 Hermes，由它按**内容**判断
# 「是不是在跟我说话」。需要处理的回复并播报；不需要的落库记录（status=noted）
# 而不打扰用户。唤醒词不再决定要不要送，只作为一条线索随请求带给 Hermes。
# 填 0 可回到旧行为（仅排障用，不建议长期开——那等于把 81% 的语音扔掉）。
TALK_FORCE_HERMES = os.environ.get("DH_TALK_FORCE_HERMES", "1") != "0"

# 「只记录」的轮次里，命中这些词的额外写进语料链路（2026-09-11）。
#
# 用户的选择是「记对话库 + 关键词触发进语料」：日常对话默认只进 talk_message
# （可查，也不污染日报链路），但**客户咨询、报价、安装、故障**这类有业务价值的
# 句子要额外沉淀成训练语料——它们往往不含唤醒词（客户不会喊「大锅」），
# 却恰恰是最该积累的真实口音素材。
#
# 门店场景默认词表如下；在设置页按需增删（逗号分隔，留空 = 全部只进对话库）。
_DEFAULT_TALK_NOTE_KEYWORDS = [
    "客户", "报价", "价格", "多少钱", "安装", "施工", "上门", "保修", "质保",
    "故障", "坏了", "不亮", "不工作", "维修", "售后", "发票", "合同", "订单",
    "定金", "发货", "送货", "地址", "电话", "微信", "优惠", "活动",
    "展厅", "门店", "方案", "预算", "参数", "型号", "功率", "回路",
    "灯带", "继电器", "窗帘", "空调", "地暖", "新风",
]
TALK_NOTE_KEYWORDS: list[str] = [
    x.strip() for x in os.environ.get(
        "DH_TALK_NOTE_KEYWORDS", ",".join(_DEFAULT_TALK_NOTE_KEYWORDS)
    ).split(",") if x.strip()
]

# ---- 回声自激熔断（2026-09-10 加）----
#
# AUTO 模式下 TTS 是外放的，麦克风一直在录，而管家回复又常以唤醒词开头
# （「大锅，演示区灯组已找到…」）。一旦播报期间没挂起拾音，就会形成
# 「录回 → ASR 命中唤醒词 → 再调管家 → 再外放」的无限环：
# 实测每轮回复措辞几乎逐字复制、音色相近，几秒一轮，把 Hermes 和 TTS 打满。
#
# 客户端已用 CaptureState.playingBack 在播报期挂起拾音（第一道闸，见
# TalkController.play）。这里是**服务端兜底的第二道闸**：客户端版本旧、
# 被系统打断、或播报路径漏设标志时，服务端仍能把环掐断。
#
# 判据用「文本相似度」而不是「命中次数」——真人连续问三句，三句内容不同；
# 自激的三轮录的是同一段外放音频，ASR 结果几乎逐字相同。只数次数会误伤
# 连问三个问题的用户，看相似度则不会。
TALK_LOOP_HITS = int(os.environ.get("DH_TALK_LOOP_HITS", "3"))
# 参与相似度比较的窗口：只在这个时间跨度内的命中才计入
TALK_LOOP_WINDOW = float(os.environ.get("DH_TALK_LOOP_WINDOW", "45"))
# 判定自激后的静默时长：这段时间内所有唤醒词命中一律跳过，返回 loop_guard
TALK_LOOP_COOLDOWN = float(os.environ.get("DH_TALK_LOOP_COOLDOWN", "60"))
# 相似度阈值（difflib SequenceMatcher ratio）。自激实测 >0.9，真人不同问句 <0.5，
# 0.72 落在两者之间且远离噪声区。
TALK_LOOP_SIMILARITY = float(os.environ.get("DH_TALK_LOOP_SIMILARITY", "0.72"))

# ---- 近重复抑制（2026-09-10 加）----
#
# 与上面的自激熔断是**两个不同的问题**，别混：
#   - 熔断管的是「连续 N 轮录到同一段外放音频」——要看趋势，所以数 N 轮；
#   - 这里管的是「**同一句话**被两条路径各切一份，前后脚送来」——只看相邻两轮。
#
# 实测 18:45:02 / 18:45:14 两轮：日报 VAD 与对话 VAD 把同一句话切成了不同边界，
# ASR 一次出「让大郭帮我查一下南京六合的天气」、一次出「让大哥帮我查一下…」。
# Hermes 侧按 `hash(text)` 去重（device 10s / session 30s），**差一个字哈希就变**，
# 两次都真答、真播 —— 用户听到的就是「问一遍回两遍」。
#
# 这里用编辑距离做同类判定：同会话内、与**上一轮**高度相似 → 本轮不答。
# 判定放在我们这一侧而不是改 Hermes，因为 Hermes 是独立代码库，
# 而这是我们自己的入口；且我们本来就已经 import 了 difflib（熔断在用）。
TALK_DUP_WINDOW = float(os.environ.get("DH_TALK_DUP_WINDOW", "30"))
# 阈值取 0.85 而不是熔断的 0.72：这里只比**相邻两轮**，没有「连续三轮」提供的
# 冗余，判松了会把用户「换个说法再问一遍」当成重复挡掉。0.85 能覆盖
# 「大郭/大哥」这类单字差异（16 字句里差 1 字 ≈ 0.94），又远高于不同问句的 <0.5。
TALK_DUP_SIMILARITY = float(os.environ.get("DH_TALK_DUP_SIMILARITY", "0.85"))
# 太短的转写不参与判重（**归一化后**的字符数，标点不计）。
#
# 为什么按长度分档：两条路径切同一句话只会发生在**长句**上——短口令
# （「大锅开灯」4 字）VAD 一眼就能切完，两条路径的边界不会差出一个量级，
# 而**用户真的会重复短口令**（说了一遍没反应，再说一遍）。把它判成重复
# 等于让用户在最需要重试的短指令上彻底没反应。
# 长句反过来：没人会把「大锅帮我查一下南京六合的天气」原样重复一遍。
# 实测出问题的那句归一化后 14 字，取 8 留足余量。
TALK_DUP_MIN_CHARS = int(os.environ.get("DH_TALK_DUP_MIN_CHARS", "8"))

# ---- 会话多轮历史（2026-09-10 加）----
#
# 只服务于**降级路径**：Hermes 正常时它按 session_id 自己维护上下文，我们不管；
# Hermes 不可用而降级到本地 LLM 直答时，没有上下文就接不住「那第二个呢」。
#
# 保留轮数取 6：实时语音对话一问一答都很短，6 轮（12 条消息）已覆盖绝大多数
# 指代场景；再长对 4B 模型的收益递减，还白占上下文窗口拖慢首字延迟。
TALK_SESSION_TURNS = int(os.environ.get("DH_TALK_SESSION_TURNS", "6"))

# 声纹角色门控：是否要求「说话人必须是已注册角色」才触发管家。
#   off     —— 不门控（默认，保持旧行为；升级不改变现有手感）
#   lenient —— 只拒绝**明确判定为他人**（is_user=False）的轮次；未判定(None)放行
#   strict  —— 只有 is_user=True（本人/已注册角色）才应答
# 默认 off 的理由：声纹判定在短促对话段上召回不稳，贸然开 strict 会让本人
# 大量指令被拒（表现为「喊了没反应」），比不门控更糟。先修判定质量再开。
TALK_OWNER_GATE = os.environ.get("DH_TALK_OWNER_GATE", "off").strip().lower()

# TTS 后端。auto = 按 Piper → macOS say → edge-tts 顺序探测第一个可用的。
# 实测本机（2026-09-10）未装 Piper 也未装 edge-tts，落到 macOS 原生 say，
# 音质可接受且零依赖，保证实时链路当天可跑通。
TTS_BACKEND = os.environ.get("DH_TTS_BACKEND", "auto").strip().lower()

# 单条播报文本的字数上限（2026-09-10 加）。语音助手回复正常在 10~70 字，
# 超过 120 字说明**要么模型跑偏、要么把注入的上下文吐回来了**——
# 实测过一次 219 字的 HA 统计被原样返回，念出来就是灾难。
# 与 talk._sanitize_reply 配合：超长先按句末截断，仍超再硬切。
TTS_MAX_CHARS = int(os.environ.get("DH_TTS_MAX_CHARS", "120"))
# say 的声音名（系统中文声音：Tingting / Sandy / Shelley…）。
# 注意 Eddy/Flo/Grandma 等属于「新奇声音」，未下载时 say 会静默输出空音频
# （实测 0.02s / 256 帧），所以默认用最稳的 Tingting。
TTS_VOICE = os.environ.get("DH_TTS_VOICE", "Tingting").strip()
TTS_RATE = int(os.environ.get("DH_TTS_RATE", "200"))       # say 语速（词/分）
PIPER_BIN = os.environ.get("DH_PIPER_BIN", "").strip()
PIPER_MODEL = os.environ.get("DH_PIPER_MODEL", "").strip()

# /api/tts/synthesize 的文本上限（2026-10-25 加）。
# 与 TTS_MAX_CHARS（对话回复用的 120 字）不是一回事：那个是「模型跑偏就掐掉」，
# 这个是「任意文本入口」的防滥用闸门——合成是同步阻塞的外部调用，
# 不设上限等于让任何人拿它跑一篇长文，把服务线程占满。
TTS_SYNTH_MAX_CHARS = int(os.environ.get("DH_TTS_SYNTH_MAX_CHARS", "20000"))

# X-TTS-Text 头里回显多少字。
#
# 为什么必须截：头不是正文，一个中文汉字 URL 编码后占 9 字节，
# 20000 字就是 180KB 的响应头。Node/undici 客户端默认 maxHeaderSize 只有 16KB，
# 超过就是「Parse Error: Header overflow」——服务端自己看着好好的，
# 调用方却把响应丢了。所以只回显前几百字：够人确认「念的是哪段」就行。
TTS_SYNTH_ECHO_MAX = int(os.environ.get("DH_TTS_SYNTH_ECHO_MAX", "500"))

# ---- Ming-3.0-tts（2026-10-22 接入）------------------------------------
# 蚂蚁 inclusionAI 的 0.5B TTS（MIT，MLX 8bit）。选它的唯一理由是**发音鲁棒性**：
# Artificial Analysis 2026-10 发音鲁棒性 93.2%（并列第一），数字 / 英文专名念得对，
# 而这正是 say 的老毛病（「12 台」念成「一二台」、「SYMI」念成字母串）。
# 实测 M1 Max：RTF 0.60、峰值内存 1.9GB、159 字 → 23.3s 音频耗时 14s。
# 模型不在位或 mlx-audio 没装时自动落到 say，不会让播报哑掉。
# 模型根目录：跟着 env.local 的 DH_MODEL_DIR 走，与其他模型（SenseVoice / Fun-ASR 等）
# 保持同一套约定；未设时用仓库/bundle 内的 models。
MODEL_DIR = Path(os.environ.get(
    "DH_MODEL_DIR", str(Path(__file__).resolve().parent.parent / "models")))
TTS_MING_DIR = os.environ.get(
    "DH_TTS_MING_DIR", str(MODEL_DIR / "ming-3.0-tts-0.5b-mlx")).strip()
# 必须显式 zh：默认 en 时同一句中文不仅读错，耗时还慢 2.5 倍（实测 10.10s vs 4.03s）
TTS_MING_LANG = os.environ.get("DH_TTS_MING_LANG", "zh").strip()
# 空闲多少秒后卸载模型释放 1.9GB（0 = 永不卸载）。默认 10 分钟。
TTS_MING_IDLE_UNLOAD = int(os.environ.get("DH_TTS_MING_IDLE_UNLOAD", "600"))

# 唤醒词别名组：**必须与 Hermes 侧 `~/.hermes/scripts/hermes_wake_receiver.py`
# 的 ALIAS_GROUPS 完全对齐**——两边不一致就会出现「喊了没反应」或
# 「明明在说别的事却被管家接管」。
#
# 为什么需要这么多同音变体：ASR 对「大锅」的识别极不稳定，
# 2026-09-10 实测 SenseVoice 把「大锅，演示区灯全开」转成「大过演示区灯全开」——
# 只写规范名「大锅」会让这次唤醒直接丢失（表现为用户喊了管家不理人）。
# 增量轮用的 SenseVoice 后端**不支持热词**（只有 Fun-ASR-Nano 支持），
# 没法从 ASR 侧纠偏，所以只能在这里靠变体表兜住。
#
# 与 SELF_ALIASES 的区别：SELF_ALIASES 是「本人声纹的别称」（标注谁在说话），
# 这里是「唤醒管家的话头」（决定要不要调 Hermes）。两者概念不同，别混用。
TALK_ALIAS_GROUPS: dict[str, list[str]] = {
    "大锅": ["大锅", "大哥", "大姑", "大咕", "大果", "大过", "搭锅", "打锅", "搭过",
             "打果", "打姑", "打咕", "大个", "大各", "大戈", "大咯", "大格", "大郭",
             "da guo", "daguo", "大锅子", "大锅啊"],
    "亖米": ["亖米", "四米", "寺米", "丝米", "思米", "似米", "私米", "si mi", "4米",
             "肆米", "肆谜", "死米", "斯米", "su mi", "sumi", "symi", "SYMI"],
    "小赫": ["小赫", "小贺", "小河", "小荷", "小盒", "小合", "小和", "小呵", "晓赫",
             "晓贺", "晓禾", "筱赫", "晓鹤", "晓和", "xiao he", "xiaohe",
             "Xiaoai", "小艾", "小爱"],
    "席米": ["席米", "喜米", "西米", "洗米", "细米", "系米", "xi mi", "ximi"],
    "赫斯": ["赫斯", "和死", "和思", "和司", "和寺", "禾死", "he si", "hesi"],
    "赫敏": ["赫敏", "和敏", "和闵", "禾敏", "和民", "he min", "hemin"],
    "嘿美丝": ["嘿美丝", "黑美丝", "赫美丝", "黑妹丝", "赫美思", "嘿妹丝", "赫妹丝",
              "黑梅丝", "和美丝", "hei mei si", "heimeisi"],
    "爱马仕": ["爱马仕", "爱马氏", "爱玛仕", "爱马士", "ai ma shi", "aimashi"],
    "Hermes": ["Hermes", "hermes", "赫耳墨斯", "赫斯莫", "赫尔墨斯", "赫尔姆斯"],
}

# **必须在句首**才算唤醒词的别名（2026-09-10 修）。
#
# 为什么要单独一条规则：上面那张表是按「子串命中」判定的（`a in low`），对
# 「大锅 / 大哥」这种**在门店语境里只有一种含义**的昵称是对的——宁可宽松也不漏。
# 但「小爱 / 小艾」是**小米音箱的产品名**，它同时是个普通名词：
#
#   - 实测 18:45:47 一轮的转写是「更小爱。」，内容是上一轮 TTS 的尾音
#     「…建议直接问小爱。」被录了回来 → 子串命中「小爱」→ 又唤醒一次管家，
#     管家再回一句、再念一遍，用户听到的就是「我什么都没说它又说了一遍」。
#   - 这一组的「小河 / 小荷 / 小和 / 小盒 / 小合 / 小呵」同样都是普通词，
#     「和」「合」更是高频字，子串命中会把大量无关句子判成唤醒。
#
# 判据从「出现过」收紧成「出现在句首」：`小爱，开灯` 仍然算（用户确实在喊它），
# `建议问小爱` / `更小爱` 不算。
#
# ⚠️ 这是**改动方**，与 Hermes 侧 ALIAS_GROUPS 的「逐条对齐」约定在此处有意松一格：
# 我们收紧的是**触发条件**，不是词表本身——收紧了只会让更多轮次在我们这侧被
# 判成 no_alias、根本不会发到 Hermes；Hermes 那张表继续服务小爱原生唤醒链路
# （`/hermes/wake`），不受影响。
TALK_ALIAS_HEAD_ONLY: set[str] = {
    "小爱", "小艾", "Xiaoai",                      # 小米音箱产品名
    "小和", "小盒", "小合", "小呵", "小河", "小荷", "小贺",   # 普通词 / 常用搭配
}

# 用户额外补充的唤醒词（可选）。默认空——上面那张表已经覆盖常见说法。
TALK_ALIASES: list[str] = [
    x.strip() for x in os.environ.get("DH_TALK_ALIASES", "").split(",") if x.strip()
]

_talk_alias_flat: set[str] = set()


def talk_alias_set() -> set[str]:
    """全部可命中的唤醒词（变体 + 用户补充），进程内缓存。"""
    global _talk_alias_flat
    if not _talk_alias_flat:
        s: set[str] = set()
        for variants in TALK_ALIAS_GROUPS.values():
            s.update(v for v in variants if v)
        s.update(v for v in TALK_ALIASES if v)
        _talk_alias_flat = s
    return _talk_alias_flat


def talk_aliases() -> list[str]:
    """规范名列表（health 展示 / 传给 Hermes 用）。"""
    return list(TALK_ALIAS_GROUPS.keys())


def has_talk_alias(text: str, extra: list[str] | None = None) -> bool:
    """文本里是否出现唤醒词。

    宁可宽松也不漏：命中了就让 Hermes 再判一次（它自己有严格归一化，
    不含别名会返回 skip，白跑一次的代价只是一次本机 HTTP）。反过来漏判的代价
    是用户喊了管家没反应——那是不可接受的。

    **例外**是 [TALK_ALIAS_HEAD_ONLY] 里那批「同时也是普通词」的别名：
    它们必须出现在句首才算命中（判据与理由见该常量上方注释）。宽松的前提是
    误命中的代价小；「小爱」误命中的代价是**凭空播一句回复打断用户**，
    这比漏一次更糟，所以对它们单独收紧。
    """
    low = (text or "").lower().lstrip()
    if not low:
        return False
    for a in talk_alias_set():
        if not a:
            continue
        al = a.lower()
        if a in TALK_ALIAS_HEAD_ONLY:
            if low.startswith(al):
                return True
        elif al in low:
            return True
    for a in (extra or []):
        if not a:
            continue
        al = str(a).lower()
        if a in TALK_ALIAS_HEAD_ONLY:
            if low.startswith(al):
                return True
        elif al in low:
            return True
    return False


def talk_dir() -> Path:
    """对话音频目录。刻意与 AUDIO_DIR 分开：sweeper 只扫 audio/ 下的日期分区，
    对话 wav 混进去会被当「未处理段」反复转写。"""
    return DATA_DIR / "talk"


# ---- 云端厂商预设 ----
# 只写「基址 + 协议 + 一个推荐模型」：模型名随时在变，真正的来源应该是厂商的
# /models 接口（App 里点「拉取模型列表」即可），这里只是免得用户去翻文档找域名。
# 全部为 OpenAI 兼容协议，唯独 Anthropic 是自家协议（system 独立字段 + x-api-key）。
LLM_PRESETS: list[dict] = [
    # 国内
    {"vendor": "DeepSeek", "region": "cn",
     "base_url": "https://api.deepseek.com/v1", "protocol": "openai",
     "model": "deepseek-flash", "key_url": "https://platform.deepseek.com/api_keys"},
    {"vendor": "阿里云百炼（通义）", "region": "cn",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "protocol": "openai",
     "model": "qwen3.8-max", "key_url": "https://bailian.console.aliyun.com/"},
    {"vendor": "月之暗面 Kimi", "region": "cn",
     "base_url": "https://api.moonshot.cn/v1", "protocol": "openai",
     "model": "kimi-k3", "key_url": "https://platform.moonshot.cn/"},
    {"vendor": "智谱 GLM", "region": "cn",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "protocol": "openai",
     "model": "glm-5.3", "key_url": "https://open.bigmodel.cn/usercenter/apikeys"},
    {"vendor": "火山方舟（豆包）", "region": "cn",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "protocol": "openai",
     "model": "doubao-seed-2.1-pro", "key_url": "https://console.volcengine.com/ark"},
    {"vendor": "SiliconFlow（聚合）", "region": "cn",
     "base_url": "https://api.siliconflow.cn/v1", "protocol": "openai",
     "model": "Qwen/Qwen3-8B", "key_url": "https://cloud.siliconflow.cn/"},
    # 国外
    {"vendor": "OpenAI", "region": "intl",
     "base_url": "https://api.openai.com/v1", "protocol": "openai",
     "model": "gpt-5.6-terra", "key_url": "https://platform.openai.com/api-keys"},
    {"vendor": "Anthropic Claude", "region": "intl",
     "base_url": "https://api.anthropic.com/v1", "protocol": "anthropic",
     "model": "claude-sonnet-5", "key_url": "https://console.anthropic.com/settings/keys"},
    {"vendor": "Google Gemini", "region": "intl",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "protocol": "openai",
     "model": "gemini-3.6-flash", "key_url": "https://aistudio.google.com/apikey"},
    {"vendor": "xAI Grok", "region": "intl",
     "base_url": "https://api.x.ai/v1", "protocol": "openai",
     "model": "grok-4.5", "key_url": "https://console.x.ai/"},
]

# ---- 设置项声明式描述 ----
# App 与 Web 工作台都按这份 schema 渲染表单，服务端是唯一真相：
# 加一个可配项只改这里，两端同时出现，不用各写一遍 UI。
#
# type: text / secret / int / number / bool / choice / select / textarea
#       choice 与 select 语义相同（都是「从固定枚举里挑一个」），Mac 壳
#       渲染成同一个下拉；保留两个名字只是为了兼容早期写法。
#
# ⚠️ 两条踩过的规则（2026-09-16）：
#   1. options 必须是 [{"value": ..., "label": ...}] 对象数组，**不能写裸字符串**。
#      console.py 的保存校验按 o["value"] 取值，Mac 壳的 Option 也按对象解析 ——
#      写成 ["1", "0"] 会让设置页整页变成「配置读取失败」，而接口返回的是 200、
#      响应体完全正常，只看服务端日志什么都查不出来。
#   2. choice / select / int / number / bool 都要写 default，且**必须与下面代码里
#      os.environ.get(..., 兜底值) 一致**。漏写时 env.local 里没配这一项，
#      界面显示「未设置」而服务端其实按兜底值在跑 —— 界面与真实生效值对不上。
#
# restart: 改了必须重启服务才生效（绑定地址、路径类）
# hot:     可热更新，保存后立即生效
CONFIG_SCHEMA: list[dict] = [
    {"key": "general", "label": "通用", "fields": [
        {"name": "DH_HOST", "label": "监听地址", "type": "text", "restart": True,
         "default": "0.0.0.0",
         "help": "0.0.0.0 = 允许局域网内的手机连接；127.0.0.1 = 只本机。改了要重启服务。"},
        {"name": "DH_PORT", "label": "端口", "type": "int", "restart": True,
         "default": "8766", "min": 1024, "max": 65535,
         "help": "改端口后手机端要重新填 Mac 地址。改了要重启服务。"},
        {"name": "DH_TOKEN", "label": "配对 Token", "type": "text", "restart": True,
         "default": "digital-human",
         "help": "手机端「配对 Token」必须与这里一致。改了要重启服务，并重新配对手机。"},
    ]},
    {"key": "llm", "label": "模型与接口", "fields": [
        {"name": "DH_LLM_PROVIDER", "label": "运行位置", "type": "choice", "hot": True,
         "options": [{"value": "local", "label": "本地模型"},
                     {"value": "cloud", "label": "云端 API"}],
         "help": "本地：语料完全不出网，但要下载约 2.9GB 模型、常驻约 3GB 内存。"
                 "云端：省下载与内存，8GB 内存的 Mac 也能跑，代价是文本会发给厂商。"},
        {"name": "DH_LLM_PROTOCOL", "label": "接口协议", "type": "choice", "hot": True,
         "options": [{"value": "openai", "label": "OpenAI 兼容（绝大多数厂商）"},
                     {"value": "anthropic", "label": "Anthropic 原生（Claude）"}],
         "help": "国内厂商与 OpenAI / Gemini / xAI 都是 OpenAI 兼容；只有直连 Claude 官方要选 Anthropic。"},
        {"name": "DH_LLM_BASE_URL", "label": "接口地址", "type": "text", "hot": True,
         "default": "http://127.0.0.1:1235/v1",
         "help": "选厂商预设会自动填。本机 mlx-lm 一般是 http://127.0.0.1:1235/v1。"},
        {"name": "DH_LLM_MODEL", "label": "模型名", "type": "text", "hot": True,
         "default": "qwen3.5-4b",
         "help": "云端填厂商的模型 id，不确定就点「拉取模型列表」。"},
        {"name": "DH_LLM_API_KEY", "label": "API Key", "type": "secret", "hot": True,
         "help": "只保存在本机 env.local，不会回传给除所选厂商之外的任何地方。"},
        {"name": "DH_LLM_SERVED_ID", "label": "本地模型路径（served id）", "type": "text",
         "hot": True, "local_only": True,
         "help": "mlx-lm 的 /v1/models 返回的是模型目录绝对路径，请求体里必须原样带回。"
                 "留空则与「模型名」相同。云端不要填。"},
        {"name": "DH_LLM_TIMEOUT", "label": "超时（秒）", "type": "int", "hot": True,
         "default": "300", "min": 30, "max": 1800,
         "help": "本地 4B 模型出长正文需要时间，默认 300 秒。"},
        {"name": "DH_HTTPS_PROXY", "label": "云端代理", "type": "text", "hot": True,
         "cloud_only": True,
         "help": "只有访问 OpenAI / Claude / Gemini 等境外厂商且本机需要代理时才填，"
                 "形如 http://127.0.0.1:7890。留空直连。"},
    ]},
    {"key": "schedule", "label": "处理窗口", "fields": [
        {"name": "DH_AGG_WINDOWS", "label": "音频处理窗口", "type": "text", "hot": True,
         "default": "13:00,17:45",
         "help": "只在窗口内处理音频（转写/聚类/去口气/总结）：窗口外手机照常上传落库、"
                 "不耗 CPU，本地文字模型也不加载（内存释放）。逗号分隔 HH:MM，"
                 "如 13:00,17:45；留空 = 恢复实时处理。"},
        {"name": "DH_AGG_SPAN_MIN", "label": "窗口会话时长（分钟）", "type": "int", "hot": True,
         "default": "90", "min": 10, "max": 480,
         "help": "从窗口时刻起允许启动新处理任务的时长，积压会在会话内连续消化清空；"
                 "已开跑的任务跑完为止。窗口结束后本地模型自动关闭并释放内存。"},
        {"name": "DH_AGG_BATCH_MIN", "label": "数量触发阈值（段）", "type": "int", "hot": True,
         "default": "30", "min": 0, "max": 100,
         "help": "待转写的录音积压到这么多段就自动开始处理，处理完自动停掉并释放内存。"
                 "填 0 关闭数量触发（只用上面的固定窗口）。与窗口可同时开启——任一满足即触发。"},
        {"name": "DH_DAILY_FINAL_TIME", "label": "日终终稿时刻", "type": "text", "hot": True,
         "default": "17:45",
         "help": "到点用终稿后端把当天全段重转一遍并出终稿、删原始音频。建议与最后一个处理窗口一致或更晚。"},
    ]},
    {"key": "asr", "label": "转写", "fields": [
        {"name": "DH_ASR_BACKEND_INCREMENTAL", "label": "增量轮后端", "type": "choice", "hot": True,
         # default 必须与第 134 行 os.environ.get 的兜底值一致：漏声明的话
         # env.local 里没写这一项时，values 给空串、schema 又没 default，
         # 设置页就显示「未设置」，而服务端实际是按 sensevoice 在跑
         # ——界面与真实生效值不一致（2026-09-16 实测）。
         "default": "sensevoice",
         "options": [{"value": "sensevoice", "label": "SenseVoice（快，秒级出稿）"},
                     {"value": "funasr_nano", "label": "Fun-ASR-Nano（准，慢）"},
                     {"value": "whisper", "label": "whisper.cpp（兜底）"}],
         "help": "手机每半小时推一轮时用的后端，要快。"},
        {"name": "DH_ASR_BACKEND_FINAL", "label": "日终轮后端", "type": "choice", "hot": True,
         "default": "funasr_nano",   # 同上，对应第 135 行的兜底值
         "options": [{"value": "funasr_nano", "label": "Fun-ASR-Nano（准，支持热词）"},
                     {"value": "sensevoice", "label": "SenseVoice（快）"},
                     {"value": "whisper", "label": "whisper.cpp（兜底）"}],
         "help": "每天 17:30 出终稿时用的后端，要准。"},
        {"name": "DH_ASR_FINAL_REWRITE", "label": "日终全段重转", "type": "bool", "hot": True,
         "default": "1",
         "help": "关掉的话日报会一直停留在增量轮的质量上。"},
        {"name": "DH_ASR_HOTWORDS", "label": "热词（空格分隔）", "type": "textarea", "hot": True,
         "help": "只写真专名：人名、项目名、自家产品名。行业通用词会拖慢解码并导致复读。"
                 "已实测的坑：塞 101 个行业词后日终轮从 8.0x 实时掉到 2.9x，术语命中一个没多。"},
        {"name": "DH_HOTWORD_MAX", "label": "热词上限", "type": "int", "hot": True,
         "default": "30", "min": 0, "max": 100},
        {"name": "DH_ASR_PROMPT", "label": "转写提示词", "type": "textarea", "hot": True,
         "help": "给兜底后端的词表偏置。行业术语会自动拼接在后面，不用手填。"},
        {"name": "DH_TIMESTAMP_GAP_MIN", "label": "时间戳分桶（分钟）", "type": "int", "hot": True,
         "default": "10", "min": 0, "max": 120,
         "help": "相邻两段间隔超过这个分钟数就插一行时间标记。填 0 关闭时间戳。"},
        {"name": "DH_SUMMARY_MIN_INTERVAL", "label": "总结稿最小更新间隔（秒）", "type": "int", "hot": True,
         "default": "600", "min": 0, "max": 7200,
         "help": "距离上次总结不足该秒数时，增量轮沿用旧总结（干净稿/原始稿照常刷新）。填 0 恢复每轮都重算。日终终稿不受限。"},
    ]},
    {"key": "speaker", "label": "说话人", "fields": [
        {"name": "DH_SELF_LABEL", "label": "本人标签", "type": "text", "hot": True,
         "default": "本人",
         "help": "注册本人声纹时用的名字，本人语料筛选也以它为准。"},
        {"name": "DH_SELF_ALIASES", "label": "本人别名", "type": "text", "hot": True,
         "default": "",
         "help": "本人用昵称注册过声纹时填这里（逗号分隔，如：大锅,老王）。总结稿本人/他人拆分与本人语料筛选都会把这些别名视为本人。"},
        {"name": "DH_VOICEPRINT_THRESHOLD", "label": "聚类阈值", "type": "number", "hot": True,
         "default": "0.6", "min": 0.3, "max": 0.95, "step": 0.01,
         "help": "匿名说话人聚类的相似门槛。调低更容易并成一个人，调高更容易拆成多人。"},
        {"name": "DH_NAMED_VOICEPRINT_THRESHOLD", "label": "具名匹配阈值", "type": "number",
         "hot": True, "default": "0.6", "min": 0.3, "max": 0.95, "step": 0.01,
         "help": "命中具名角色会直接把真实姓名写进日报，宁可漏也别错标。"},
        {"name": "DH_AUDIO_TAGGING_SPEECH_THRESHOLD", "label": "人声判定阈值", "type": "number",
         "hot": True, "default": "0.25", "min": 0.05, "max": 0.9, "step": 0.01,
         "help": "低于此值判为非人声（音乐/噪声），不转写也不进日报。电视里的人声仍算人声。"},
    ]},
    {"key": "talk", "label": "实时对话（Hermes）", "fields": [
        {"name": "DH_HERMES_BASE_URL", "label": "Hermes 地址", "type": "text", "hot": True,
         "default": "http://127.0.0.1:9876",
         "help": "Hermes 语音接收服务的地址（通常与本机同址）。连不上时自动降级为"
                 "「纯 AI 对话」（直接用上面的『模型与接口』配置回答），不会中断对话。"},
        {"name": "DH_HERMES_TIMEOUT", "label": "Hermes 超时（秒）", "type": "number", "hot": True,
         "default": "30", "min": 1, "max": 120, "step": 1,
         "help": "超时后自动降级为纯 AI 回答，不让手机端干等。"},
        {"name": "DH_TTS_BACKEND", "label": "语音合成后端", "type": "select", "hot": True,
         "default": "ming",
         "options": [["ming", "Ming-3.0-tts（发音最准，首次加载约 4 秒）"],
                     ["auto", "自动（Ming → Piper → 系统 say → Edge）"],
                     ["say", "macOS 系统声音（零延迟，机器人音）"],
                     ["piper", "Piper（需自行安装）"],
                     ["edge", "Edge TTS（需联网）"]],
         "help": "Ming 是蚂蚁开源的 0.5B 语音模型（MIT），发音鲁棒性排开源第一，"
                 "数字与英文专名念得对；代价是首次加载约 4 秒、常驻约 1.9GB。"
                 "不确定就选「自动」——模型缺失会自动回落，播报不会哑。"},
        {"name": "DH_TTS_MING_IDLE_UNLOAD", "label": "Ming 空闲卸载（秒）", "type": "int", "hot": True,
         "default": "600", "min": 0, "max": 7200,
         "help": "多少秒不用就卸载 Ming 模型、释放约 1.9GB 内存。填 0 表示永不卸载。"},
        {"name": "DH_TTS_VOICE", "label": "播报声音", "type": "text", "hot": True,
         "default": "Tingting",
         "help": "macOS 系统中文声音名（Tingting 婷婷 / Sandy / Shelley）。"
                 "Eddy、Flo、Grandma 等属新奇声音，未下载时会输出空音频，不要填。"},
        {"name": "DH_TTS_RATE", "label": "播报语速", "type": "int", "hot": True,
         "default": "200", "min": 100, "max": 400,
         "help": "数值越大越快。默认 200。"},
        {"name": "DH_TALK_ALIASES", "label": "额外唤醒词（逗号分隔）", "type": "textarea", "hot": True,
         "default": "",
         "help": "内置已覆盖 9 组常见唤醒词及其同音变体（大锅/亖米/小赫/席米/赫斯/赫敏/"
                 "嘿美丝/爱马仕/Hermes），一般留空即可。这里的补充项会与内置表合并。"},
        {"name": "DH_TALK_FORCE_HERMES", "label": "有语意就交 Hermes 判定", "type": "select", "hot": True,
         "default": "1",
         # options 必须是 {"value","label"} 对象（2026-09-16 修）：
         # console.py 的保存校验只认 o["value"]，写成裸字符串 `["1","0"]`
         # 会让保存那一刻抛 TypeError；Mac 壳的设置页更是直接整页解码失败
         # （它的 Option 按对象解析），表现为「配置读取失败」而接口是 200。
         "options": [
             {"value": "1", "label": "是（推荐）"},
             {"value": "0", "label": "否（仅排障）"},
         ],
         "help": "1（推荐）=只要 ASR 出了文本就交给 Hermes，由它按内容判断要不要应答："
                 "需要处理的回复并播报，不需要的落库记录、不打扰。"
                 "0=回到旧行为：不含唤醒词直接丢弃——实测丢掉 8 成语音，仅排障用。"},
        {"name": "DH_TALK_MAX_SEC", "label": "单轮音频上限（秒）", "type": "number", "hot": True,
         "default": "20", "min": 5, "max": 60, "step": 1,
         "help": "必须 ≥ 手机端录音上限（手动 15 秒 / 自动 8 秒），否则话说长一点就被拒。"
                 "2026-09-11 前是 10，与手机端 15 冲突，实测丢了 24 轮。"},
        {"name": "DH_TALK_NOTE_KEYWORDS", "label": "记录进语料的关键词", "type": "textarea", "hot": True,
         "default": "",
         "help": "「不需要处理」的轮次默认只进对话库；命中这里任一关键词（逗号分隔）时"
                 "额外沉淀成训练语料。留空 = 用内置门店词表（客户/报价/安装/故障…）。"},
        {"name": "DH_TALK_TTL_SEC", "label": "一轮对话有效期（秒）", "type": "int", "hot": True,
         "default": "120", "min": 30, "max": 600,
         "help": "手机断线后服务端靠这个回收对话音频，防止占满磁盘。"},
        {"name": "DH_TALK_MAX_TURNS_PER_DEVICE", "label": "单设备并发上限", "type": "int", "hot": True,
         "default": "3", "min": 1, "max": 10,
         "help": "同一台手机同时进行的对话轮数上限，超出返回 429。"},
        {"name": "DH_TALK_LOOP_HITS", "label": "自激熔断·连续命中轮数", "type": "int", "hot": True,
         "default": "3", "min": 2, "max": 10,
         "help": "连续几轮唤醒词命中的转写内容都高度相似时，判定为回声自激。"
                 "真人连问三句的内容不同，不会触发；自激每轮录的是同一段外放音频，会触发。"},
        {"name": "DH_TALK_LOOP_COOLDOWN", "label": "自激熔断·静默时长（秒）", "type": "int", "hot": True,
         "default": "60", "min": 10, "max": 600,
         "help": "判定自激后这段时间内不再应答（返回 loop_guard）。"
                 "手机端播报期间会自动挂起拾音，正常不会走到这里。"},
        {"name": "DH_TALK_LOOP_SIMILARITY", "label": "自激熔断·相似度阈值", "type": "number", "hot": True,
         "default": "0.72", "min": 0.5, "max": 0.99,
         "help": "0~1，越大越难触发熔断。自激实测 >0.9，真人不同问句 <0.5。"},
        {"name": "DH_TALK_SESSION_TURNS", "label": "会话记忆轮数", "type": "int", "hot": True,
         "default": "6", "min": 1, "max": 20,
         "help": "管家不可用、降级到本地模型直答时，记住同一会话最近几轮对话。"
                 "正常走 Hermes 时由 Hermes 自己管上下文，此项不影响。"},
        {"name": "DH_TALK_OWNER_GATE", "label": "声纹角色门控", "type": "select", "hot": True,
         "default": "off",
         # 同 DH_TALK_FORCE_HERMES：options 必须是对象形态
         "options": [
             {"value": "off", "label": "关闭（不校验）"},
             {"value": "lenient", "label": "宽松（只拒明确他人）"},
             {"value": "strict", "label": "严格（须已注册角色）"},
         ],
         "help": "off=不校验；lenient=只拒绝明确判定为他人的轮次；"
                 "strict=必须是已注册角色才应答。声纹在短促对话段上召回不稳，"
                 "开 strict 前请先在状态页复测「本人连续两句是否都判成本人」。"},
        {"name": "DH_TTS_MAX_CHARS", "label": "播报字数上限", "type": "int", "hot": True,
         "default": "120", "min": 20, "max": 400,
         "help": "单条回复超过这个字数就按句末截断再播。正常回复 10~70 字；"
                 "超过 120 字通常意味着模型把注入的上下文吐回来了，念出来是灾难。"},
        {"name": "DH_PIPER_BIN", "label": "Piper 可执行文件", "type": "text", "restart": True,
         "help": "留空则自动探测；探测不到就用 macOS 原生 say 播报。"},
        {"name": "DH_PIPER_MODEL", "label": "Piper 中文模型", "type": "text", "restart": True,
         "help": "形如 zh_CN-huayan-medium.onnx。填了且文件存在时优先用 Piper（音质更好）。"},
    ]},
    {"key": "advanced", "label": "高级", "fields": [
        {"name": "DH_DATA_DIR", "label": "数据目录", "type": "text", "restart": True,
         "help": "音频、数据库、日报备份都在里面。改了要重启服务。"},
        {"name": "DH_LOGS_DIR", "label": "日志目录", "type": "text", "restart": True,
         "help": "改了要重启服务。"},
        {"name": "DH_SENSEVOICE_DIR", "label": "SenseVoice 模型目录", "type": "text", "restart": True},
        {"name": "DH_FUNASR_NANO_DIR", "label": "Fun-ASR-Nano 模型目录", "type": "text", "restart": True},
        {"name": "DH_SPEAKER_MODEL", "label": "声纹模型", "type": "text", "restart": True},
        {"name": "DH_AUDIO_TAGGING_MODEL", "label": "人声检测模型", "type": "text", "restart": True},
        {"name": "DH_WHISPER_BIN", "label": "whisper 可执行文件", "type": "text", "restart": True,
         "default": "whisper-cli"},
        {"name": "DH_WHISPER_MODEL", "label": "whisper 模型", "type": "text", "restart": True},
    ]},
]

# ---- env.local 读写：设置页的落盘层 ----
# 约定：env.local 是**唯一**的配置持久化位置，UI 不再另起一套 plist / json，
# 否则「App 里改了、命令行起的实例没变」这类不一致会反复出现。
_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def env_file_path() -> Path:
    """env.local 的写入位置：优先已存在的那个，否则取第一个能创建的候选。

    不能无脑写 `_env_file_candidates()[0]`：打包后代码目录在只读的 bundle 里，
    写会直接失败，而失败若被吞掉就变成「保存成功但重启后回到原样」。
    """
    cands = _env_file_candidates()
    for p in cands:
        if p.is_file():
            return p
    for p in cands:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
            return p
        except OSError:
            continue
    return cands[0]


def _quote_env(v: str) -> str:
    v = "" if v is None else str(v)
    if not v:
        return ""
    if any(c in v for c in " \t#\"'"):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def set_env_values(values: dict[str, str]) -> list[str]:
    """行级 upsert 写回 env.local，并让当前进程立刻生效。

    两个都不能少：
    - 写文件，否则重启后配置丢失；
    - 改 os.environ + 重绑模块常量，否则 UI 上显示「已保存」，服务却还在用旧值，
      用户会以为功能坏了。这正是 keepalive 老坑的翻版（改了热词反复重启仍是旧词）。

    保留注释与行序：env.local 里大量说明性注释是排障依据，整文件重写会把它们抹掉。
    """
    if not values:
        return []
    path = env_file_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = _ENV_ASSIGN_RE.match(line)
        k = m.group(1) if m else None
        if k and k in values:
            out.append(f"{k}={_quote_env(str(values[k]))}")
            seen.add(k)
        else:
            out.append(line)
    pending = [(k, v) for k, v in values.items() if k not in seen]
    if pending:
        # 只在整块新键前插一个空行。逐个插会变成
        # 「DH_A=1\n\nDH_B=2\n\nDH_C=3」，读起来非常吵。
        if out and out[-1].strip():
            out.append("")
        for k, v in pending:
            out.append(f"{k}={_quote_env(str(v))}")
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    for k, v in values.items():
        os.environ[k] = str(v)
    _apply_env()
    return sorted(values)


def _apply_env() -> None:
    """把 os.environ 的最新值重新绑定到模块级常量。

    只重绑「运行时改了立刻有意义」的那些；HOST/PORT/模型路径这类必须重启，
    由 schema 的 restart 标记告诉 UI 去提示用户。
    """
    global TOKEN, HOST, PORT
    global LLM_BASE_URL, LLM_MODEL, LLM_SERVED_ID, LLM_PROVIDER, LLM_PROTOCOL
    global LLM_API_KEY, LLM_TIMEOUT, LLM_PROXY
    global ASR_BACKEND_INCREMENTAL, ASR_BACKEND_FINAL, ASR_FINAL_REWRITE
    global HOTWORD_MAX, TIMESTAMP_GAP_MIN, SELF_LABEL, SELF_ALIASES
    global VOICEPRINT_THRESHOLD, NAMED_VOICEPRINT_THRESHOLD, AUDIO_TAGGING_SPEECH_THRESHOLD
    global WHISPER_CPP_BIN, WHISPER_MODEL
    global HERMES_BASE_URL, HERMES_TIMEOUT, HERMES_RETRY
    global TALK_TTL_SEC, TALK_MAX_BYTES, TALK_MAX_SEC, TALK_MAX_TURNS_PER_DEVICE
    global TALK_FORCE_HERMES, TALK_NOTE_KEYWORDS
    global TALK_LOOP_HITS, TALK_LOOP_WINDOW, TALK_LOOP_COOLDOWN, TALK_LOOP_SIMILARITY
    global TALK_SESSION_TURNS, TALK_OWNER_GATE
    global TTS_BACKEND, TTS_VOICE, TTS_RATE, PIPER_BIN, PIPER_MODEL
    global TTS_MING_DIR, TTS_MING_LANG, TTS_MING_IDLE_UNLOAD
    global TALK_ALIASES, _talk_alias_flat

    TOKEN = os.environ.get("DH_TOKEN", DEFAULT_TOKEN)
    HOST = os.environ.get("DH_HOST", "0.0.0.0")
    PORT = int(os.environ.get("DH_PORT", "8766"))

    LLM_BASE_URL = os.environ.get("DH_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    LLM_MODEL = os.environ.get("DH_LLM_MODEL", "qwen3.5-4b")
    LLM_SERVED_ID = os.environ.get("DH_LLM_SERVED_ID", "")
    LLM_PROVIDER = os.environ.get("DH_LLM_PROVIDER", "").strip().lower()
    LLM_PROTOCOL = os.environ.get("DH_LLM_PROTOCOL", "openai").strip().lower()
    LLM_API_KEY = os.environ.get("DH_LLM_API_KEY", "").strip()
    LLM_TIMEOUT = int(os.environ.get("DH_LLM_TIMEOUT", "300"))
    LLM_PROXY = os.environ.get("DH_HTTPS_PROXY", "").strip()

    ASR_BACKEND_INCREMENTAL = os.environ.get("DH_ASR_BACKEND_INCREMENTAL", "sensevoice")
    ASR_BACKEND_FINAL = os.environ.get("DH_ASR_BACKEND_FINAL", "funasr_nano")
    ASR_FINAL_REWRITE = os.environ.get("DH_ASR_FINAL_REWRITE", "1") == "1"
    HOTWORD_MAX = int(os.environ.get("DH_HOTWORD_MAX", "30"))
    TIMESTAMP_GAP_MIN = int(os.environ.get("DH_TIMESTAMP_GAP_MIN", "10"))
    SUMMARY_MIN_INTERVAL = int(os.environ.get("DH_SUMMARY_MIN_INTERVAL", "600"))
    SWEEP_MIN = int(os.environ.get("DH_SWEEP_MIN", "10"))
    DAILY_FINAL_TIME = os.environ.get("DH_DAILY_FINAL_TIME", "17:45").strip()
    CATCHUP_DAYS = int(os.environ.get("DH_CATCHUP_DAYS", "3"))
    global AGG_WINDOWS_RAW, AGG_SPAN_MIN, _AGG_WINDOWS_MIN, AGG_BATCH_MIN
    AGG_WINDOWS_RAW = os.environ.get("DH_AGG_WINDOWS", "13:00,17:45").strip()
    AGG_SPAN_MIN = int(os.environ.get("DH_AGG_SPAN_MIN", "90"))
    AGG_BATCH_MIN = int(os.environ.get("DH_AGG_BATCH_MIN", "30"))
    _AGG_WINDOWS_MIN = None   # 窗口配置热更新后强制重解析
    SELF_LABEL = os.environ.get("DH_SELF_LABEL", "本人")
    SELF_ALIASES = {SELF_LABEL} | {
        x.strip() for x in os.environ.get("DH_SELF_ALIASES", "").split(",") if x.strip()
    }
    VOICEPRINT_THRESHOLD = float(os.environ.get("DH_VOICEPRINT_THRESHOLD", "0.6"))
    NAMED_VOICEPRINT_THRESHOLD = float(
        os.environ.get("DH_NAMED_VOICEPRINT_THRESHOLD",
                       os.environ.get("DH_VOICEPRINT_THRESHOLD", "0.6")))
    AUDIO_TAGGING_SPEECH_THRESHOLD = float(
        os.environ.get("DH_AUDIO_TAGGING_SPEECH_THRESHOLD", "0.25"))
    WHISPER_CPP_BIN = os.environ.get("DH_WHISPER_BIN", "whisper-cli")
    WHISPER_MODEL = os.environ.get("DH_WHISPER_MODEL", "")

    HERMES_BASE_URL = os.environ.get("DH_HERMES_BASE_URL", "http://127.0.0.1:9876").rstrip("/")
    HERMES_TIMEOUT = float(os.environ.get("DH_HERMES_TIMEOUT", "30"))
    HERMES_RETRY = int(os.environ.get("DH_HERMES_RETRY", "3"))
    TALK_TTL_SEC = int(os.environ.get("DH_TALK_TTL_SEC", "120"))
    TALK_MAX_BYTES = int(os.environ.get("DH_TALK_MAX_BYTES", str(2 * 1024 * 1024)))
    TALK_MAX_SEC = float(os.environ.get("DH_TALK_MAX_SEC", "20"))
    TALK_FORCE_HERMES = os.environ.get("DH_TALK_FORCE_HERMES", "1") != "0"
    _kw = [x.strip() for x in
           os.environ.get("DH_TALK_NOTE_KEYWORDS", "").split(",") if x.strip()]
    TALK_NOTE_KEYWORDS = _kw or _DEFAULT_TALK_NOTE_KEYWORDS
    TALK_MAX_TURNS_PER_DEVICE = int(os.environ.get("DH_TALK_MAX_TURNS_PER_DEVICE", "3"))
    TALK_LOOP_HITS = int(os.environ.get("DH_TALK_LOOP_HITS", "3"))
    TALK_LOOP_WINDOW = float(os.environ.get("DH_TALK_LOOP_WINDOW", "45"))
    TALK_LOOP_COOLDOWN = float(os.environ.get("DH_TALK_LOOP_COOLDOWN", "60"))
    TALK_LOOP_SIMILARITY = float(os.environ.get("DH_TALK_LOOP_SIMILARITY", "0.72"))
    TALK_DUP_WINDOW = float(os.environ.get("DH_TALK_DUP_WINDOW", "30"))
    TALK_DUP_SIMILARITY = float(os.environ.get("DH_TALK_DUP_SIMILARITY", "0.85"))
    TALK_DUP_MIN_CHARS = int(os.environ.get("DH_TALK_DUP_MIN_CHARS", "8"))
    TALK_SESSION_TURNS = int(os.environ.get("DH_TALK_SESSION_TURNS", "6"))
    TALK_OWNER_GATE = os.environ.get("DH_TALK_OWNER_GATE", "off").strip().lower()
    TTS_BACKEND = os.environ.get("DH_TTS_BACKEND", "auto").strip().lower()
    TTS_MAX_CHARS = int(os.environ.get("DH_TTS_MAX_CHARS", "120"))
    TTS_VOICE = os.environ.get("DH_TTS_VOICE", "Tingting").strip()
    TTS_RATE = int(os.environ.get("DH_TTS_RATE", "200"))
    PIPER_BIN = os.environ.get("DH_PIPER_BIN", "").strip()
    PIPER_MODEL = os.environ.get("DH_PIPER_MODEL", "").strip()
    _model_dir = Path(os.environ.get(
        "DH_MODEL_DIR", str(Path(__file__).resolve().parent.parent / "models")))
    TTS_MING_DIR = os.environ.get(
        "DH_TTS_MING_DIR", str(_model_dir / "ming-3.0-tts-0.5b-mlx")).strip()
    TTS_MING_LANG = os.environ.get("DH_TTS_MING_LANG", "zh").strip()
    TTS_MING_IDLE_UNLOAD = int(os.environ.get("DH_TTS_MING_IDLE_UNLOAD", "600"))
    TALK_ALIASES = [
        x.strip() for x in os.environ.get("DH_TALK_ALIASES", "").split(",") if x.strip()
    ]
    _talk_alias_flat = set()   # 唤醒词表变了，缓存要重建

    # 热词与 whisper prompt 是行业派生值，必须整体重算，
    # 否则出现「UI 显示已改但 ASR 仍用旧热词」的假保存。
    _apply_industry(INDUSTRY)


def config_values(mask_secrets: bool = True) -> dict:
    """给 UI 的当前生效值（schema 里没列的项不返回）。

    密钥默认只回 True/False：设置页是常驻界面，密钥明文回传再渲染进表单，
    一旦截图或投屏就是泄露。要改就填新的，留空表示不修改。
    """
    out: dict = {}
    for group in CONFIG_SCHEMA:
        for f in group["fields"]:
            name = f["name"]
            raw = os.environ.get(name, "")
            if f.get("type") == "bool":
                out[name] = os.environ.get(name, f.get("default", "1")) == "1"
            elif f.get("type") == "secret":
                out[name] = "" if mask_secrets else raw
            elif name == "DH_LLM_PROVIDER" and not raw:
                # 未显式设置时按 base_url 推断，UI 上要显示**实际生效**的那个，
                # 否则 Picker 会停在空白项，用户以为没配置成功。
                out[name] = "cloud" if llm_is_cloud() else "local"
            elif name == "DH_LLM_PROTOCOL" and not raw:
                out[name] = "openai"
            else:
                # 区分「未设置」和「显式设为空」：
                # 有些字段空值本身有意义——`DH_AGG_WINDOWS=` 空就表示「实时模式，
                # 不等窗口」。之前一律用 `raw != ""` 判空，把显式空值也替换成 default，
                # 于是设置页显示 13:00,17:45 而实际在跑实时模式（2026-10-22 实测踩到）。
                out[name] = raw if name in os.environ else f.get("default", "")
    return out


def secret_status() -> dict:
    return {
        f["name"]: bool(os.environ.get(f["name"], "").strip())
        for group in CONFIG_SCHEMA for f in group["fields"]
        if f.get("type") == "secret"
    }


def llm_settings() -> dict:
    """模型与接口的当前生效状态，UI 顶部那张卡就是它。"""
    return {
        "is_cloud": llm_is_cloud(),
        "provider": LLM_PROVIDER,
        "protocol": LLM_PROTOCOL,
        "base_url": llm_base_url(),
        "model": LLM_MODEL,
        "model_id": (LLM_MODEL if llm_is_cloud() else (LLM_SERVED_ID or LLM_MODEL)),
        "served_id": LLM_SERVED_ID,
        "timeout": LLM_TIMEOUT,
        "proxy": LLM_PROXY,
        "has_key": bool(LLM_API_KEY),
    }


# 管线超时兜底重试
PIPELINE_MAX_RETRIES = 2

# 日期格式 yyyy-MM-dd。所有把 date 拼进文件路径 / SQL 的入口都必须先过这个校验，
# 否则 /api/done/..%2f..%2fetc 之类的输入可写出任意路径（路径遍历）。
_VALID_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def agg_windows() -> tuple[int, ...]:
    """解析 AGG_WINDOWS_RAW → 升序去重的「当天第几分钟」元组；空元组 = 实时模式。

    容错：全角逗号按半角处理；非法项（如 "25:00"、"abc"）整项忽略——
    配置错误时宁可回到"多跑一个窗口"也不能把全部窗口判丢。
    """
    global _AGG_WINDOWS_MIN
    if _AGG_WINDOWS_MIN is None:
        out: set[int] = set()
        for part in AGG_WINDOWS_RAW.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                hh, mm = part.split(":")[:2]
                v = int(hh) * 60 + int(mm)
            except (ValueError, IndexError):
                continue
            if 0 <= v < 24 * 60:
                out.add(v)
        _AGG_WINDOWS_MIN = tuple(sorted(out))
    return _AGG_WINDOWS_MIN


def in_agg_window(now: datetime | None = None) -> bool:
    """当前时刻是否允许**启动**新的处理管线。

    未启用窗口（AGG_WINDOWS 为空）→ 恒 True（实时模式，旧行为）。
    启用后：窗口时刻起 AGG_SPAN_MIN 分钟内为 True。已开跑的轮次不受影响
    （本函数只在 pipeline.trigger 的入口判一次），跑完为止。
    """
    wins = agg_windows()
    if not wins:
        return True
    now = now or datetime.now()
    n = now.hour * 60 + now.minute
    span = max(1, AGG_SPAN_MIN)
    return any(w <= n < w + span for w in wins)


def batch_due() -> bool:
    """待转写的人声段积压达到 DH_AGG_BATCH_MIN → 允许启动处理（数量触发）。

    AGG_BATCH_MIN <= 0 表示关闭数量触发（只用固定窗口）。
    判据用 `processed=0 AND is_speech=1` 的段数（手机端「待判定」的那批），
    刻意不含「已转写但缺角色」的崩溃残留——否则计数永不归零，会误判成
    「一直有积压」。多租户时任一租户达标即算达标。
    """
    if AGG_BATCH_MIN <= 0:
        return False
    from . import store, tenants   # 延迟导入：store/tenants 依赖本模块，顶层会循环
    return any(store.count_pending_speech(t.id) >= AGG_BATCH_MIN
               for t in tenants.all_tenants())


def agg_active() -> bool:
    """是否允许**启动**新处理管线：固定窗口 **或** 数量触发，任一满足即放行。

    三种用法：
      * 实时（上传即处理）  → DH_AGG_WINDOWS 留空 且 DH_AGG_BATCH_MIN=0（恒 True）
      * 仅固定窗口          → DH_AGG_WINDOWS=13:00,17:45 且 DH_AGG_BATCH_MIN=0
      * 仅数量触发          → DH_AGG_WINDOWS 留空 且 DH_AGG_BATCH_MIN=30
      * 窗口 + 数量同时     → 两者都设（任一满足即触发，默认即是这种）

    关键：**窗口留空 + 启用了数量触发时，只认数量**。因为 in_agg_window()
    在窗口为空时恒 True（「不限制时间」），若不特判，「仅数量触发」会退化成
    实时模式——积压 1 段就开跑，违背「积压到达阈值才处理」的初衷（2026-10-21 实测）。
    """
    if not agg_windows() and AGG_BATCH_MIN > 0:
        return batch_due()
    return in_agg_window() or batch_due()


def is_valid_date(date: str) -> bool:
    """校验 date 既是 yyyy-MM-dd 字面格式，也是真实存在的日期。

    格式校验挡住路径遍历；真实日期校验挡住 2026-99-99 这类会导致
    strftime/分区目录异常的输入。
    """
    if not _VALID_DATE_RE.match(date or ""):
        return False
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, TEXT_DIR, CORPUS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---- 自签 TLS ----
# 手机端 Token 非空即走 https，所以服务端必须始终有证书。与其让用户手工执行
# openssl（还要填 SAN，填错就握手失败），不如首次启动自动生成一张。
# 代价是证书随 DATA_DIR 走：删掉 data/tls 会重新生成，手机端已 pin 的指纹随之失效，
# 需在手机设置页重新点「获取证书指纹」。

def _spki_sha256_base64(cert_pem: bytes) -> str:
    """证书的公钥 SPKI SHA-256，base64 形式 —— 正是 OkHttp CertificatePinner 需要的格式，
    等价于：
      openssl x509 -pubkey -noout | openssl pkey -pubin -outform der |
      openssl dgst -sha256 -binary | openssl enc -base64
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return base64.b64encode(digest.finalize()).decode()


def ensure_tls() -> tuple[str, str]:
    """保证 TLS_CERT / TLS_KEY 存在且配对，返回 (cert, key) 路径。

    已有证书就原样复用（指纹稳定，手机端不必重新 pin）；
    只有证书或只有密钥则视为损坏，两张一起重新生成。
    """
    cert, key = Path(TLS_CERT), Path(TLS_KEY)
    if cert.exists() and key.exists():
        return str(cert), str(key)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime as _dt
    import ipaddress

    cert.parent.mkdir(parents=True, exist_ok=True)
    # 证书损坏/半缺失时重新生成，避免留下一半导致 uvicorn 启动即崩
    for p in (cert, key):
        p.unlink(missing_ok=True)

    key_obj = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "digital-human")])
    now = _dt.datetime.now(_dt.timezone.utc)
    # SAN 里塞上所有本机局域网 IP + localhost。自签证书没有公共 CA 背书，
    # 校验靠的是手机端 pinning，但填上 SAN 能让浏览器/curl 也不报错。
    sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for addr in _lan_ips():
        sans.append(x509.IPAddress(ipaddress.ip_address(addr)))
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key_obj.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=365 * 10))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
    )
    certificate = builder.sign(key_obj, hashes.SHA256())

    key.write_bytes(key_obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    # 私钥只在本地用，收窄权限
    key.chmod(0o600)
    return str(cert), str(key)


def tls_fingerprint() -> str:
    """当前证书的 SPKI SHA-256（base64）。手机端「证书指纹」填的就是这个值。"""
    try:
        return _spki_sha256_base64(Path(TLS_CERT).read_bytes())
    except Exception:
        return ""


def _lan_ips() -> list[str]:
    """本机局域网 IPv4 列表，用于写进证书 SAN 以及启动时打印给用户在手机上填写。"""
    import socket

    found: list[str] = ["127.0.0.1"]
    try:
        # 连一下外部地址即可让内核挑出出口网卡 IP，不产生实际流量（UDP 无连接）
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))
            ip = s.getsockname()[0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    # macOS 常见的多网卡场景：再枚举一遍主机名解析出的地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in found:
                found.append(info[4][0])
    except OSError:
        pass
    return found
