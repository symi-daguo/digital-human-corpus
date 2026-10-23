"""LLM 适配层：OpenAI 兼容本地 API（mlx-lm / LM Studio / Ollama 均提供 /v1 端点）。

两个任务：去口气 + 每日总结。
- mlx-lm：   python -m mlx_lm.server --model <模型目录> --port 1234
- LM Studio：lms server start → http://127.0.0.1:1234/v1
- Ollama：   ollama serve     → http://127.0.0.1:11434/v1
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import config
from . import insights
from .asr import collapse_repeats

log = logging.getLogger(__name__)

# 本地 LLM 一律直连，绝不能走 HTTP_PROXY。
#
# urllib 默认会读取环境的 HTTP_PROXY/http_proxy（公司网络、科学上网工具都会注入）。
# 一旦读到了，发往 127.0.0.1:1235 的请求也会被丢给代理，代理回 502，
# 而 _chat 只打一行 "llm chat failed: HTTP Error 502" 就返回空串 ——
# 管线照常跑完，日报的干净稿与总结双双空白，日志里没有任何「配置错误」的提示。
# 2026-09-03 实测踩到：curl 加 --noproxy 正常，Python 进程却全 502。
_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener_for(proxy: str = ""):
    """按代理串建 opener。本地一律传空串（禁代理），云端传 config.LLM_PROXY。"""
    proxy = (proxy or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return _no_proxy_opener


def _opener():
    """按本地 / 云端选择 opener。

    本地必须禁用代理（理由见上）。

    云端相反：国内厂商直连即可；国外厂商（OpenAI / Claude / Gemini）在境内
    往往需要代理。而 run.py 为了本地调用已经把 HTTPS_PROXY 清掉了，
    所以云端只认显式配置的 DH_HTTPS_PROXY，由用户在设置里填。
    """
    return _opener_for(config.LLM_PROXY if config.llm_is_cloud() else "")


def _headers_for(protocol: str, api_key: str) -> dict:
    """请求头。云端鉴权全靠这里——没有它，所有厂商一律 401。"""
    h = {"Content-Type": "application/json"}
    protocol = (protocol or "openai").strip().lower()
    if protocol == "anthropic":
        # Anthropic 原生协议用 x-api-key + 版本头，不是 Bearer
        if api_key:
            h["x-api-key"] = api_key
        h["anthropic-version"] = "2023-06-01"
    elif api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _headers() -> dict:
    return _headers_for(config.LLM_PROTOCOL, config.LLM_API_KEY)


def _model_id() -> str:
    """请求体里的 model 字段。

    本地 mlx-lm 要**模型目录绝对路径**（LLM_SERVED_ID），云端要**模型名**
    （LLM_MODEL）。把本地路径发给云端必然 404，而表现只是「总结空白」，
    极难定位，所以这里按本地 / 云端分开取，绝不混用。
    """
    if config.llm_is_cloud():
        return config.LLM_MODEL
    return config.LLM_SERVED_ID or config.LLM_MODEL


def _chat(system: str, user: str,
          history: list[tuple[str, str]] | None = None,
          soft: bool = False) -> str:
    """单轮或**多轮**对话。

    `history` 是 [(role, content)] 的旧轮次，role 取 `user` / `assistant`，
    按时间升序排列（最早的在前）。不传即等价于原来的单轮行为。

    为什么要它：实时对话降到纯 AI（Hermes 不可用）时，如果不带历史，
    用户说「那第二个呢」这种指代就完全无从回答——4B 模型只能硬编。
    多轮连续性靠这里，不靠调用方自己拼 prompt。

    `soft`（2026-10-21）：失败只打 WARNING 而不是 ERROR。
    专供**实时对话**这类「本来就有降级路径」的调用——窗口外 mlx 已按设计停掉，
    此时对话退到「只回文字」是预期行为，不该用 ERROR 污染日志
    （实测 14:31-15:45 空档期 84 次对话刷了 4 条 ERROR，全是正常的降级）。
    批量管线（去口气/总结）不支持 soft：那里失败是真问题，必须 ERROR 可见。
    """
    if config.LLM_PROTOCOL == "anthropic":
        return _chat_anthropic(system, user, history, soft=soft)
    return _chat_openai(system, user, history, soft=soft)


def _chat_openai(system: str, user: str,
                 history: list[tuple[str, str]] | None = None,
                 soft: bool = False) -> str:
    messages: list[dict] = [{"role": "system", "content": system}]
    for role, content in (history or []):
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user})
    body = {
        "model": _model_id(),
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 4096,
        "messages": messages,
    }
    if config.LLM_SERVED_ID and not config.llm_is_cloud():
        # mlx-lm（Qwen3.5 思考型模型）：关闭思考，直接出正文。
        # 云端厂商不认识这个字段，发了可能被拒，所以只在本地加。
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{config.llm_base_url()}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(),
    )
    # 单段去口气 / 单日总结都是短任务，超时只会在后端挂死时拖住整条管线。
    # 本地 4B 模型出长正文需要时间，默认 300s；失败由管线重试兜底。
    try:
        with _opener().open(req, timeout=config.LLM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content and msg.get("reasoning"):
                # 兜底：思考未关干净时取正文，避免把思考链当结果
                log.warning("llm returned empty content with reasoning")
            return content
    except Exception as e:
        (log.warning if soft else log.error)("llm chat failed: %s", e)
        return ""


def _chat_anthropic(system: str, user: str,
                    history: list[tuple[str, str]] | None = None,
                    soft: bool = False) -> str:
    """Anthropic 原生协议：system 是独立字段，响应体是 content[] 块列表。

    多轮时 messages 必须**以 user 开头且 user/assistant 交替**，否则 400。
    调用方保证 history 是成对的 (user, assistant)，这里只做一次防御性过滤：
    丢掉开头不是 user 的项，避免上游少存了一轮 assistant 就整轮失败。
    """
    msgs = [{"role": r, "content": c} for r, c in (history or [])]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    msgs.append({"role": "user", "content": user})
    body = {
        "model": _model_id(),
        "system": system,
        "messages": msgs,
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        f"{config.llm_base_url()}/messages",
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(),
    )
    try:
        with _opener().open(req, timeout=config.LLM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
            blocks = data.get("content") or []
            return "".join(b.get("text", "") for b in blocks).strip()
    except Exception as e:
        (log.warning if soft else log.error)("llm chat(anthropic) failed: %s", e)
        return ""


CLEAN_SYSTEM = (
    "你是逐字稿整理助手。在严格保留原意与信息量的前提下，做这些事："
    "1) 删除语气词（嗯、啊、呃、那个）、口头禅、无意义重复；"
    "2) 补齐标点符号；"
    "3) 删除脏话与粗口（如「他妈」「我靠」「屌」等），该词直接删去或换成中性表述，"
    "不要留下孤立的空标点；"
    "4) 清理转写机器留下的标记残留（如 [noise]、<|...|> 这类方括号/尖括号标记）；"
    "5) 只修正**确定无疑**的同音错字（例如把「亖鸡」改成「SYMI」），"
    "不确定的一律保留原样——宁可留着错字，也绝不臆造改写。"
    "不增删事实信息，不改写句式，不加解释。"
    "每一行开头形如「[HH:MM]」的时间戳和「[说话人]」标签必须原样保留在行首，"
    "一行不少：不同时间戳的行绝不能合并，同一时间戳内连续几句可以合为一句，"
    "但合并后要放在最晚那句的时间戳行里（或保留各自时间戳，取更细者）。"
    "绝对禁止删除、改动或挪动任何时间戳。直接输出整理后的逐字稿。"
    "只用简体中文和英文输出；严禁出现日语、韩语等其它语言文字。"
)

# 分时段要点概括（daily_summary 每块调用）。
PERBLOCK_SYSTEM = (
    "你是纪要助手。用两到四句话概括这段对话的内容要点，保留关键原话，"
    "按对话原文的先后顺序概括，不得打乱或倒叙。"
    "原文行首方括号内是说话人标签（如[大锅]、[甲]）：概括时必须保留说话人归属，"
    "用「我：」「某某：」注明每条要点是谁说的；多人发言可以合并在同一时段概括，"
    "但每个人的话必须分开表述，绝不把不同人说的话混写成一个人的。"
    "只用简体中文和英文；严禁输出日语、韩语等其它语言文字——"
    "输入中若出现其它语言片段，一律忽略或用简体中文概括其含义。"
    "直接输出要点本身，不要解释你的处理过程，不要提及语言问题。"
)

# 总结尾部四栏（【我的动态】【他人动态】由代码按说话人确定性生成，不依赖模型——
# 2026-09-04 用户要求总结与原始稿/干净稿同时间轴同顺序；2026-09-08 用户硬要求
# 本人发言必须与他人分开总结，他人可合并——拆分靠声纹标签在代码层完成，模型只概括）。
SUMMARY_SYSTEM = (
    "你是日记助手。输入是已按时间顺序整理的当日要点，其中【我的动态】是机主本人的发言，"
    "【他人动态】是其他人的发言，两者绝不混合。"
    "必须按下面四个小标题输出，每个标题单独一行、以【】包裹，顺序固定：\n"
    "【关键原话】三到五条当日最有信息量的原话，每条一行，行首用「- 说话人：」开头"
    "（机主本人写「我：」，他人写其标签名），引用原句并标注它的 [HH:MM] 时间戳\n"
    "【待办】承诺与待办事项，每条一行、行首「- 」，行首注明是谁的待办"
    "（「我：」或具体人名）；没有就写「无」\n"
    "【金句】有意思的想法/金句，每条一行、行首「- 说话人：」；没有就写「无」\n"
    "【状态】情绪与状态：先用一句写「我：…」，再用一句写他人的状态（多人可合并）；"
    "某一方没有内容就只写另一方\n"
    "严禁输出【我的动态】【他人动态】【回顾】——时间轴由系统生成，你只输出以上四栏。"
    "时间只允许取自输入里已有的 [HH:MM] 或【HH:MM-HH:MM】，严禁编造。"
    "不要输出标题以外的小标题或空行分隔。"
    "只用简体中文和英文；严禁出现日语、韩语等其它语言文字。简洁平实。"
)

def _industry_hint() -> str:
    """行业术语 + 同音错字示范（2026-09-05）：把当前行业词表喂给 LLM。

    解决的是「ASR 同音错字在文本层救不回来」的问题——转写会把「二维码」写成
    「奥维码」、「吊顶」写成「杯盖」。转写阶段已放弃用热词偏置（实测有害，见
    config.HOTWORD_MAX 注释），文本层是主要的补救位置。

    关键实测结论：4B 模型**不会**自己判断"哪个词可疑"。同一批 4 个真实错字：
      只给术语表 + "遇到可疑词就改"   → 纠正 1/4
      术语表 + "逐词扫描同音词"       → 纠正 1/4，还臆造出"力量器→调光器"
      术语表 + 几条「错写→正写」示范  → 纠正 4/4
    所以这里把 `错写=正写` 映射直接展开成 few-shot 示范：小模型照着例子改，
    比让它自己推理可靠得多，而且映射本身也是约束——没列出来的不乱改。

    用函数而不是模块级常量拼接：行业可以在运行时由手机端切换，
    常量拼接会导致「UI 已切换、提示词还是旧行业」的假切换。
    未配置行业时返回空串，提示词保持原样（不引入无用噪声）。
    """
    terms = getattr(config, "INDUSTRY_TERMS", None) or []
    fixes = getattr(config, "INDUSTRY_FIXES", None) or {}
    if not terms:
        return ""
    # 术语表只给前 80 个：再长小模型也用不上，还挤占真实文本的空间
    hint = f"（当前行业：{config.INDUSTRY}。行业术语表：{'、'.join(terms[:80])}。）"
    if fixes:
        # 映射过多会稀释示范效果，取前 12 条
        demo = "、".join(f"{w}→{r}" for w, r in list(fixes.items())[:12])
        hint += (
            f"转写常有同音错字，必须改正，例如：{demo}。"
            "只改这几类和它们明显同音的错写，其余一律原样保留，不要臆造。"
        )
    else:
        hint += "不要臆造术语表里没有的词。"
    return hint


# 纠偏表的「错写」是否以拉丁字母开头。决定要不要给替换加「前面不能还是字母」
# 的守卫 —— 见 apply_industry_fixes 里的方言说明。
_LATIN_HEAD_RE = re.compile(r"^[A-Za-z]")


def apply_industry_fixes(text: str) -> tuple[str, list[str]]:
    """同音错字**确定性**替换，返回 (新文本, 命中记录)。

    为什么不继续只靠提示词让模型改：4B 模型在这件事上不稳定。同一批 4 个真实
    错字，提示词写法最优时测到过 4/4，但换一批输入（多行 + 说话人标签，即真实
    日报的形态）复测就是 0/3 —— 小模型的指令遵循随输入形态漂移，不能当依赖。

    而 `错写=正写` 这张表本来就是人工从真实转写里挑出来的高置信映射，
    既然确定，就该用确定的方式落地：字符串替换，100% 命中、零延迟、可预测。
    提示词那份保留作为补充（偶尔能改到表里没有的漏网词），但主线靠这里。

    误伤控制：只替换当前行业词表明确列出的错写；词表按行业隔离，讲智能家居时
    不会去动「杯盖」。收录纪律见 industries/*.txt —— 只写真实见过的错字。
    """
    fixes = getattr(config, "INDUSTRY_FIXES", None) or {}
    if not fixes or not text:
        return text, []
    hits: list[str] = []
    for wrong, right in fixes.items():
        if not wrong:
            continue
        if _LATIN_HEAD_RE.match(wrong):
            # 方言类错写（2026-09-17）：六合/南京话的「去」被 ASR 写成拉丁字母 K，
            # 于是错写是「K啊」这种**字母 + 汉字**的形态。朴素 replace 在这里
            # 会误伤：「OK啊」里就含子串「K啊」，真被替换就产出「O去啊」——
            # 实测 2026-09-14 的原始稿里确有「OK啊」。加一条「前面不能还是字母」
            # 的守卫即可：`K啊` 命中，`OK啊` 里的 K 前面是 O，不命中。
            # 只对字母开头的错写加守卫，纯汉字错写（奥维码=二维码）行为不变。
            pat = re.compile(r"(?<![A-Za-z])" + re.escape(wrong))
            n = len(pat.findall(text))
            if n:
                text = pat.sub(right, text)
        else:
            n = text.count(wrong)
            if n:
                text = text.replace(wrong, right)
        if n:
            hits.append(f"{wrong}→{right}×{n}")
    return text, hits


def clean_system() -> str:
    return CLEAN_SYSTEM + _industry_hint()


def perblock_system() -> str:
    return PERBLOCK_SYSTEM + _industry_hint()


def summary_system() -> str:
    return SUMMARY_SYSTEM + _industry_hint()


# 总结卡片：固定栏目（顺序即卡片顺序）。解析失败（模型不按格式输出）时由调用方
# 回退成整段文本，绝不因为格式问题丢内容。
# 「我的动态/他人动态」（2026-09-08）：本人发言与他人分开总结，由代码拆流生成；
# 「回顾」保留在解析列表里只为兼容旧版总结（新版不再输出该栏目）。
CARD_SPECS = [
    ("我的动态", "我的动态"),
    ("他人动态", "他人动态"),
    ("回顾", "今日回顾"),
    ("关键原话", "关键原话"),
    ("待办", "待办与承诺"),
    ("金句", "灵感与金句"),
    ("状态", "情绪与状态"),
]


def parse_summary_cards(text: str) -> list[dict[str, str]]:
    """把带【栏目】标记的总结文本解析成卡片列表 [{type,title,body}]。

    返回空列表 = 模型没按格式输出（或内容为空），调用方应回退为整段文本展示。
    刻意不做「尽力修」的模糊匹配：栏目是固定协议，模型不遵守就该在日志里暴露，
    而不是靠猜把内容塞进错误的卡片。
    """
    if not text:
        return []
    markers = [m for m, _ in CARD_SPECS]
    pattern = "【(" + "|".join(re.escape(m) for m in markers) + ")】"
    parts = re.split(pattern, text)
    if len(parts) < 3:      # 至少需要 1 个栏目：["", marker, body...]
        return []
    cards: list[dict[str, str]] = []
    title_of = dict(CARD_SPECS)
    seen: set[str] = set()
    for i in range(1, len(parts), 2):
        marker = parts[i]
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if not body or body == "无":
            continue
        if marker in seen:
            # 模型违规重复输出同名栏目（如无视禁令又写了【回顾】）时保留第一个
            continue
        seen.add(marker)
        cards.append({"type": marker, "title": title_of.get(marker, marker), "body": body})
    return cards


# 单次去口气请求的输入上限（字符）。
#
# 不能省：去口气是「等长输出」任务——输入 N 个 token 就要吐回约 N 个 token。
# max_tokens=4096 是输入+输出共享的预算，整篇塞进去时，超过约 3500 字符的
# 逐字稿会在中途被硬截断，干净稿只剩前半段，而后半段被静默丢弃（无报错、
# 无异常，只有「今天说得少」的错觉）。按段边界分块可根治。
_CLEAN_CHUNK_CHARS = 1200

# 总结是「压缩输出」任务，输入可以长得多。合并后的时间轴要点绝不允许在块中间
# 硬截断——2026-09-07 实测：全天密集对话 merged 超过 6000 字，旧的硬切把 16:31
# 之后的时段整段砍掉，总结稿时间轴断尾而干净稿却同步到了 16:51（用户当场发现）。
# 4B 模型上下文以万 token 计，12000 字（约 8k token）的汇总输入完全放得下；
# 超过上限时按「整块」丢弃并打 ERROR 日志，绝不切半句、绝不静默。
_SUMMARY_DIRECT_CHARS = 12000
_SUMMARY_CHUNK_CHARS = 3000


def _split_keep_lines(text: str, limit: int) -> list[str]:
    """按行边界切块，单块尽量不超过 limit 字符。

    必须按行（= 一个语音段）切，不能按固定字符硬切：硬切会把一句话劈成两半，
    两块各自补标点后拼起来，语义会被割裂。单行本身超限时才退化为硬切。
    """
    lines = text.split("\n")
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        # 单行超过 limit：先结算已有内容，再把长行按 limit 硬切（极端情况兜底）
        if len(ln) > limit:
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            for i in range(0, len(ln), limit):
                chunks.append(ln[i:i + limit])
            continue
        if cur_len + len(ln) + 1 > limit and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _ts_set(text: str) -> set[str]:
    """提取文中全部 [HH:MM] 时间戳。用于「去口气后时间戳零丢失」的确定性校验。"""
    return set(re.findall(r"\[\d{2}:\d{2}\]", text or ""))


def _clean_one(chunk: str, idx: int, total: int) -> str:
    """清理单块，带时间戳完整性校验：重试一次，仍丢则回退原文。

    时间戳零丢失（2026-09-03 用户硬要求）：清理后用正则比对输入/输出的
    [HH:MM] 集合，缺了就加强指令重试一次，仍缺则整块回退原文——
    宁可这块没去口气，也不能丢时间段信息（4B 模型合并相邻行时会吞时间戳）。
    """
    r = _chat(clean_system(), chunk)
    if r.strip() and _ts_set(chunk) <= _ts_set(r):
        # 第二道防线（2026-09-17）：模型在**高度重复的输入**上会失控——
        # prompt 里明明写了「删除无意义重复」，它却把重复串越写越长。
        # 实测 2026-09-16：超长行 raw 7499 字 → clean 17902 字（2.4 倍），
        # 单行 1039 → 8180（7.9 倍）。ASR 出口已经掐过一道，这里是兜底：
        # 万一还有漏网的，也不能让它把日报灌成一片重复。
        return collapse_repeats(r)
    if r.strip():
        r2 = _chat(
            clean_system()
            + "（硬性要求：输入中出现的每一个 [HH:MM] 时间戳都必须原样出现在输出里，一个都不能少、不能改、不能挪。）",
            chunk,
        )
        if r2.strip() and _ts_set(chunk) <= _ts_set(r2):
            return r2
        log.warning("clean chunk %d/%d 丢失时间戳，该块回退原文", idx, total)
        return chunk
    log.warning("clean chunk %d/%d 返回空，该块保留原文", idx, total)
    return chunk


# ---- 粗口确定性清理（2026-10-20）----------------------------------------
# LLM（4B）对「删脏话」执行很不稳定：实测同一条 prompt，单行输入 4/4 全清，
# 换成 133 行的真实日报只清掉个位数（11 处「他妈」只清 1 处）。
# 粗口是封闭集合，用正则确定性命中，比反复调模型可靠得多——LLM 负责
# 去语气 / 补标点这类需要语义的活，粗口删除交给代码。
# 只收「明确粗口、不会误伤正常词」的组合，刻意不碰单字：
# 「靠」在「靠在桌上」里是正常动词，「逼」在「逼我」里是逼迫。
_PROFANITY_RE = re.compile(
    r"他妈|尼玛|尼美|马勒戈壁|妈的|"
    r"[马妈]了?个?逼|"          # 妈了个逼 / 妈个逼 / 马了个逼 / 妈逼 等变体
    r"我靠|我操|我草|"
    r"傻[逼比]|煞笔|"
    r"狗屎|狗屁|"
    r"屌(?:丝|用|样|货|东西)?"
)


def _strip_profanity(text: str) -> str:
    """删去明确粗口，并规整删词后留下的空标点 / 空格堆叠。"""
    if not text:
        return text
    out = _PROFANITY_RE.sub("", text)
    # 删词后常留下空标点堆叠或「[标签] ，文本」这类残留，规整掉
    out = re.sub(r"[，。、！？；：]{2,}", "。", out)   # 连续标点压成一个句号
    out = re.sub(r"\]\s*[，、；：]", "]", out)         # 「[标签] ，文本」→「[标签] 文本」
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def _clean_blocks(text: str) -> str:
    """把一段文本走完「分块 → 逐块清洗（带缓存）→ 拼接」的既有链路。

    从 clean_transcript 里原样抽出：价值门要对「高价值部分」复用这条链路，
    低价值部分则完全不过 LLM。
    """
    if not text.strip():
        return ""
    chunks = _split_keep_lines(text, _CLEAN_CHUNK_CHARS)
    if len(chunks) == 1:
        out = _clean_one_cached(chunks[0], 1, 1)
        _clean_cache_flush()   # 单块也要落盘，否则当天短稿的缓存永远写不进去
        return out
    hits = 0
    out = []
    for i, ck in enumerate(chunks):
        before = _clean_cache_get(ck)
        if before is not None:
            hits += 1
            out.append(before)
        else:
            out.append(_clean_one_cached(ck, i + 1, len(chunks)))
    _clean_cache_flush()
    log.info("clean_transcript 分块处理：%d 字符 → %d 块（缓存命中 %d）",
             len(text), len(chunks), hits)
    return "\n".join(out)


def clean_transcript(raw: str) -> str:
    """去口气。长文本分块处理，避免 max_tokens 截断导致后半段静默丢失。

    任一块失败（返回空）时不整篇失败：该块原样保留，其余块照常整理。
    这样即使 LLM 中途抖动，用户拿到的也是「部分整理过的稿子」而不是空稿。

    块级缓存（2026-09-08）：原始稿只在尾部追加、前面的块永不变化，而增量轮
    一天会触发十几次——不缓存就是全天 O(n²) 重算（4B 模型 13 块要 3 分钟，
    到傍晚 50+ 块要十几分钟，这是「日报不及时」的另一半根因）。缓存后增量轮
    只算新增/变化的块，通常 1-2 块、十几秒出稿。

    缓存落盘（2026-09-14）：见 _CLEAN_CACHE_FILE 的说明——进程内缓存扛不住
    「每次开机都重启服务」，冷启动会白算 4~5 分钟。

    价值门（2026-10-23，DH_VALUE_GATE，默认开）：实测全天原始稿里
    「无实体信号 + 有效字 <8」的低信息密度行约占 1/3 字符量，它们进 LLM
    清洗产出为零。门按行打分（行业词/数字英文实体/有效长度），低价值行
    只做确定性清理（去重复+去粗口），**跳过 LLM**，追加在稿尾并显式标注。
    高价值行照旧走 LLM。时间戳完整性不受影响：低价值行的 [HH:MM] 原样保留。
    """
    if not raw.strip():
        return ""
    # 粗口在 LLM 之前先删：让模型看不到粗口，也就不会在「补标点」时
    # 围着它造出新的语气词（实测模型会保留甚至复述粗口）。
    raw = _strip_profanity(raw)

    # ---- 价值门 -----------------------------------------------------------
    if insights.VALUE_GATE and "\n" in raw:
        try:
            runs, kinds, low_chars = insights.partition_lines(raw)
        except Exception as e:   # 门坏了绝不能拖垮清洗，退回全量路径
            log.warning("价值门评分异常，本轮退回全量 LLM 清洗：%s", e)
            runs, kinds, low_chars = [], [], 0
        hi = "\n".join(r for r, k in zip(runs, kinds) if k == "hi")
        lo = "\n".join(r for r, k in zip(runs, kinds) if k == "lo")
        if low_chars > 0 and hi.strip():
            core = _clean_blocks(hi)
            lo_clean = collapse_repeats(lo)
            total = len(hi) + len(lo)
            log.info("价值门：低信息密度 %d/%d 字符（%.0f%%）跳过 LLM 清洗，"
                     "LLM 输入 %d→%d 字符",
                     low_chars, total, low_chars * 100 / max(total, 1),
                     total, len(hi))
            return _strip_profanity(
                core
                + "\n\n—— 以下为低信息密度片段（仅去重复与粗口，未做语义清洗）——\n"
                + lo_clean
            )
        # 没有低价值行 / 全是低价值行 → 走原路径（全低价值时 LLM 还是过一遍，
        # 因为整段都闲聊说明当天语义分层不可靠，宁多勿缺）

    chunks = _split_keep_lines(raw, _CLEAN_CHUNK_CHARS)
    if len(chunks) == 1:
        out = _clean_one_cached(chunks[0], 1, 1)
        _clean_cache_flush()   # 单块也要落盘，否则当天短稿的缓存永远写不进去
        return _strip_profanity(out)

    out = _clean_blocks(raw)
    # 出口再兜底一道（幂等）：防 LLM 复述粗口，或缓存里历史上含粗口的旧结果
    return _strip_profanity(out)


# 块级缓存：key=sha256(提示词 + 块文本)，value=清理结果。只缓存成功结果
# （_clean_one 失败会回退原文，原文与 LLM 输出不同，不缓存失败块）。
#
# 2026-09-14 落盘（原为进程内 dict，重启即丢）：
#   菜单栏 App 每次开机都会重启服务，进程内缓存必然清空 → 冷启动必须把当天
#   全部块重算一遍。实测 09-14 那天 2.2 万字符的干净稿重算花了 **4 分 36 秒**
#   （日志「分块处理：21994 字符 → 20 块（缓存命中 0）」，12:12:02 → 12:16:38），
#   而这 20 块里有 19 块在开机前就算过一次（原始稿只尾部追加，前面的块不变）。
#   落盘后同样的块直接命中，冷启动只算当天新增的尾巴。
#
# 键里带 clean_system() 的哈希：CLEAN_SYSTEM 或行业词表（data/industries/）改了，
# 旧键自动失效，不会把旧提示词产出的稿子当成新结果用——这是唯一需要担心的
# 缓存正确性问题，用「把提示词揉进键」根治，比维护一个手写的版本号可靠。
_CLEAN_CACHE_FILE = config.DATA_DIR / "llm-clean-cache.json"
_CLEAN_CACHE_MAX = 800
_clean_cache: dict[str, str] = {}
_clean_cache_loaded = False
_clean_cache_dirty = False


def _clean_key(chunk: str) -> str:
    return hashlib.sha256((clean_system() + "\x00" + chunk).encode()).hexdigest()


def _clean_cache_load() -> None:
    """首次用到时读盘。文件缺失/损坏一律当空缓存，绝不因此中断处理。"""
    global _clean_cache_loaded
    if _clean_cache_loaded:
        return
    _clean_cache_loaded = True
    try:
        data = json.loads(_CLEAN_CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as e:  # 损坏的 JSON / 权限问题：降级成空缓存
        log.warning("干净稿块缓存读取失败，按空缓存继续（本轮会慢一些）：%s", e)
        return
    if isinstance(data, dict):
        _clean_cache.update({k: v for k, v in data.items() if isinstance(v, str)})
        log.info("干净稿块缓存载入 %d 条", len(_clean_cache))


def _clean_cache_flush() -> None:
    """落盘（一次 clean_transcript 只写一次，避免每块都写几百 KB）。

    写临时文件再 replace：中途掉电/被杀不会留下半截 JSON 把缓存读坏。
    """
    global _clean_cache_dirty
    if not _clean_cache_dirty:
        return
    try:
        tmp = _CLEAN_CACHE_FILE.with_name(_CLEAN_CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(_clean_cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CLEAN_CACHE_FILE)
        _clean_cache_dirty = False
    except OSError as e:
        # 缓存写不进去不该影响本轮结果——只是下一轮还要重算
        log.warning("干净稿块缓存落盘失败（不影响本轮结果）：%s", e)


def _clean_cache_get(chunk: str) -> str | None:
    _clean_cache_load()
    return _clean_cache.get(_clean_key(chunk))


def _clean_one_cached(chunk: str, idx: int, total: int) -> str:
    global _clean_cache_dirty
    out = _clean_one(chunk, idx, total)
    # 回退原文（=chunk）说明本轮清理失败，不能缓存，否则失败被永久固化
    if out != chunk and out.strip():
        if len(_clean_cache) >= _CLEAN_CACHE_MAX:
            _clean_cache.clear()  # 简单防膨胀：清空重来，代价只是下轮慢一次
        _clean_cache[_clean_key(chunk)] = out
        _clean_cache_dirty = True
    return out


def _blocks_with_range(raw: str, gap_min: int = 10, span_max_min: int = 30) -> list[tuple[str, str]]:
    """把带 [HH:MM] 行前缀的原始稿切成时间段块。

    确定性时间标注（2026-09-03）：4B 模型自己给要点标时间不可靠（实测全天
    要点全标成 10:29），时间范围必须由代码按行前缀计算，LLM 只负责概括内容。
    相邻行时间差 > gap_min 分钟即切块；另外单块跨度超过 span_max_min 分钟也
    强制切块（2026-09-07）：连续说话一小时会合出一个「16:31-17:24」巨块，
    头部起点离最新内容太远，用户会误以为总结停在 16:31 没更新。
    返回 [(「HH:MM-HH:MM」, 块文本)]。
    """
    blocks: list[tuple[str | None, list[str]]] = []
    cur_start: str | None = None
    cur_start_min: int | None = None
    cur: list[str] = []
    prev_min: int | None = None
    for ln in raw.split("\n"):
        m = re.match(r"\[(\d{2}:\d{2})\]", ln)
        if m:
            hh, mm = m.group(1).split(":")
            t = int(hh) * 60 + int(mm)
            if cur and prev_min is not None and (
                t - prev_min > gap_min or t - cur_start_min > span_max_min
            ):
                blocks.append((cur_start, cur))
                cur, cur_start, cur_start_min = [], None, None
            if cur_start is None:
                cur_start = m.group(1)
                cur_start_min = t
            prev_min = t
        cur.append(ln)
    if cur:
        blocks.append((cur_start, cur))

    out: list[tuple[str, str]] = []
    for start, lines in blocks:
        times = re.findall(r"\[(\d{2}:\d{2})\]", "\n".join(lines))
        rng = f"{times[0]}-{times[-1]}" if times else (start or "?")
        out.append((rng, "\n".join(lines)))
    return out


def _split_by_speaker(raw: str) -> tuple[str, str]:
    """把原始稿按说话人拆成（本人流, 他人流）。

    用户硬要求（2026-09-08）：总结稿里本人发言必须单独成栏，绝不与他人混合；
    他人可以合并。拆分由代码按声纹标签确定性完成，不靠模型判断——
    4B 模型自己归类说话人不可靠（与「时间头由代码注入」同一原则）。
    标签为空（声纹未判定）的行归入他人流：宁可让未知归属不污染本人栏，
    精确归属以干净稿/原始稿的行级标签为准。
    """
    self_lines: list[str] = []
    other_lines: list[str] = []
    for ln in raw.split("\n"):
        m = re.match(r"\[\d{2}:\d{2}\]\s*\[([^\]]+)\]", ln)
        if m and m.group(1).strip() in config.SELF_ALIASES:
            self_lines.append(ln)
        else:
            other_lines.append(ln)
    return "\n".join(self_lines), "\n".join(other_lines)


def _timeline_summary(stream: str) -> str:
    """把一条说话人流按时间段概括成【HH:MM-HH:MM】要点时间轴（时间头代码注入）。"""
    blocks = _blocks_with_range(stream)
    partial: list[str] = []
    for rng, text in blocks:
        # 单块过长仍按行切块逐段概括（极端全天连续说话场景兜底）
        parts = _split_keep_lines(text, _SUMMARY_CHUNK_CHARS)
        for j, p in enumerate(parts):
            sub = f"（{rng}）" if len(parts) == 1 else f"（{rng} 第{j + 1}部分）"
            r = _chat(perblock_system(), f"对话片段{sub}：\n{p}")
            if r.strip():
                partial.append(f"【{rng}】{r}" if j == 0 else r)
    if not partial:
        return ""
    merged = "\n".join(partial)
    # 超限时按整块保留（块以【HH:MM 开头），绝不硬切半句断时间轴
    if len(merged) > _SUMMARY_DIRECT_CHARS:
        lines = merged.split("\n")
        kept: list[str] = []
        total = 0
        first_dropped = ""
        for ln in lines:
            w = len(ln) + 1
            if ln.startswith("【") and total + w > _SUMMARY_DIRECT_CHARS and kept:
                first_dropped = ln[:20]
                break
            kept.append(ln)
            total += w
        if first_dropped:
            log.error("daily_summary merged %d chars over limit %d; timeline blocks from "
                      "%s... onward dropped（应调大 _SUMMARY_DIRECT_CHARS 或缩短单段概括）",
                      len(merged), _SUMMARY_DIRECT_CHARS, first_dropped)
        merged = "\n".join(kept)
    return merged


def daily_summary(raw: str) -> str:
    """每日总结：按说话人拆成本人/他人两流，各自按时间段分块概括，汇总成栏目卡片。

    输出结构（2026-09-08 本人/他人分离版，与原始稿/干净稿同时间轴同顺序）：
      【我的动态】
      【HH:MM-HH:MM】本人时间段要点…
      【他人动态】
      【HH:MM-HH:MM】他人时间段要点（多人可合并，各自注明谁说的）…
      【关键原话】【待办】【金句】【状态】（LLM 生成，每条标注说话人）

    时间头与说话人拆分都由代码完成，模型只负责概括内容（防幻觉/防混淆）。
    栏目卡由 parse_summary_cards 解析后同步手机端；旧版【回顾】格式仍可解析。
    """
    if not raw.strip():
        return ""
    mine, others = _split_by_speaker(raw)
    sections: list[str] = []
    for title, stream in (("我的动态", mine), ("他人动态", others)):
        if not stream.strip():
            continue
        merged = _timeline_summary(stream)
        if merged.strip():
            sections.append(f"【{title}】\n{merged}")
    if not sections:
        return ""
    body = "\n\n".join(sections)
    tail = _chat(
        summary_system(),
        f"以下是当日要点（已按说话人分开）：\n{body}\n\n"
        "请严格按系统提示里的四个【栏目】输出（不要输出【我的动态】【他人动态】）。",
    )
    if not tail.strip():
        log.error("daily_summary 四栏汇总返回空，仅保留说话人时间轴")
        return body
    return body + "\n\n" + tail


# --------------------------------------------------------------------------
# 实时对话（Hermes 对接，2026-09-10）
# --------------------------------------------------------------------------
TALK_SYSTEM = (
    "你是 SYMI 智能家居管家的语音助手。用户通过语音跟你说话。\n"
    "要求：\n"
    "1. 直接给结论，不要复述用户的话，不要说「好的我明白了」这类客套开场。\n"
    "2. 回复必须是**适合朗读**的口语短句，30 字以内，不要 markdown、不要列表、不要括号注释。\n"
    "3. 不确定的事就说不知道，绝不编造设备状态。\n"
    "4. 涉及具体设备开关/状态时，若上下文没有真实数据，明确告知「我这边查不到实时状态」。"
)


def quick_answer(text: str, context: str = "",
                 history: list[tuple[str, str]] | None = None) -> str:
    """实时对话的单轮问答（talk.py 在 Hermes 不可用时降级调用）。

    与 clean_transcript / daily_summary 的区别：那两个是**批量管线**用的长 prompt
    （通读全天语料、要严格输出结构），单轮对话要的是 30 字内的口语短句，
    套用总结 prompt 会让 4B 模型输出一堆结构化标题，TTS 念出来很怪。

    `history`：同一 session 的既往轮次，用来接住「那第二个呢」这类指代。
    由 talk.py 按 session_id 维护并传入——本函数**自己不存状态**，
    保持无副作用，方便从任意线程池调用。

    失败返回空串（调用方据此判断「AI 没回应」），不抛异常。
    """
    user = (text or "").strip()
    if not user:
        return ""
    if context:
        user = f"{user}\n\n[可用上下文]\n{context}"
    try:
        # soft=True：窗口外 mlx 按设计已停，对话退到「只回文字」是预期行为，
        # 用 WARNING 留痕即可（用 ERROR 会让日志被正常降级刷满，掩盖真故障）。
        return (_chat(TALK_SYSTEM, user, history, soft=True) or "").strip()
    except Exception as e:
        log.warning("quick_answer 失败：%s", e)
        return ""


def _cloud_of(provider: str, base_url: str) -> bool:
    """显式参数版的「是否云端」，用于设置页的连通性测试。

    不能直接调 config.llm_is_cloud()：测试用的是**还没保存**的参数，
    读全局会拿旧配置去测新地址，测出来的结论是假的。
    """
    p = (provider or "").strip().lower()
    if p in ("cloud", "remote", "api", "online"):
        return True
    if p in ("local", "mlx", "offline"):
        return False
    return not any(h in (base_url or "").lower()
                   for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))


def list_models(base_url: str, api_key: str, protocol: str = "openai",
                proxy: str = "", timeout: int = 20) -> dict:
    """拉取厂商的模型列表，免得用户手抄模型名（抄错一个字符就是 404）。

    返回 {"ok":bool,"message":str,"models":[...]}。失败不抛异常——
    设置页要的是一句人话，不是 traceback。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "message": "接口地址为空", "models": []}
    req = urllib.request.Request(
        f"{base}/models", headers=_headers_for(protocol, api_key))
    try:
        with _opener_for(proxy).open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = {401: "API Key 无效（401）", 403: "没有权限（403）"}.get(
            e.code, f"请求失败（HTTP {e.code}）")
        return {"ok": False, "message": msg, "models": []}
    except Exception as e:
        return {"ok": False, "message": f"连不上：{e}", "models": []}
    ids = sorted({str(m.get("id", "")) for m in (data.get("data") or [])} - {""})
    if not ids:
        return {"ok": False, "message": "厂商返回了空模型列表（该 Key 可能无权限）",
                "models": []}
    return {"ok": True, "message": f"共 {len(ids)} 个模型", "models": ids}


def probe(base_url=None, model=None, api_key=None, protocol=None,
          proxy=None, provider=None, served_id=None, timeout: int = 60) -> dict:
    """连通性测试：一次真实对话，确认「地址 + 密钥 + 模型名」三者都对。

    刻意不复用 available()：那读的是全局配置，而这里测的是**用户刚填还没保存**
    的参数。参数留 None 表示「沿用已保存值」——设置页里密钥是掩码显示的，
    用户没改就不会回传，此时必须拿已保存的 Key 去测，否则永远报「缺少 API Key」。

    返回 {"ok","message","latency_ms","url","model","protocol"}。
    """
    base = (base_url or config.llm_base_url()).rstrip("/")
    proto = (protocol or config.LLM_PROTOCOL or "openai").strip().lower()
    key = api_key if api_key is not None else config.LLM_API_KEY
    pxy = proxy if proxy is not None else config.LLM_PROXY
    prov = provider if provider is not None else config.LLM_PROVIDER
    sid = served_id if served_id is not None else config.LLM_SERVED_ID
    cloud = _cloud_of(prov, base)
    mid = model or config.LLM_MODEL
    if not cloud and sid:
        mid = sid
    if cloud and not (key or "").strip():
        return {"ok": False, "message": "云端接口缺少 API Key", "latency_ms": 0,
                "url": base, "model": mid, "protocol": proto}

    anthropic = proto == "anthropic"
    url = f"{base}/messages" if anthropic else f"{base}/chat/completions"
    if anthropic:
        body = {"model": mid, "system": "", "max_tokens": 16, "temperature": 0.0,
                "messages": [{"role": "user", "content": "回复一个字：好"}]}
    else:
        body = {"model": mid, "stream": False, "temperature": 0.0,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "回复一个字：好"}]}
        if sid and not cloud:
            body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=_headers_for(proto, key or ""))

    t0 = time.monotonic()
    try:
        with _opener_for(pxy).open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (e.read().decode("utf-8", "ignore") or "")[:200]
        except Exception:
            pass
        msg = {
            401: "API Key 无效或已过期（401）",
            403: "Key 没有该模型的权限（403）",
            404: f"模型名「{mid}」不存在（404）——点「拉取模型列表」选一个",
            429: "触发限流或余额不足（429）",
        }.get(e.code, f"请求失败（HTTP {e.code}）")
        return {"ok": False, "message": msg + (f"：{detail}" if detail else ""),
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "url": url, "model": mid, "protocol": proto}
    except Exception as e:
        # 境外厂商才提示代理：本地地址连不上是「服务没起」，提示代理会误导。
        remote = not any(h in base.lower()
                         for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))
        hint = "（境外厂商可能需要代理，在「云端代理」里填）" if cloud and remote and not pxy else ""
        return {"ok": False, "message": f"连不上：{e}{hint}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "url": url, "model": mid, "protocol": proto}

    ms = int((time.monotonic() - t0) * 1000)
    if anthropic:
        text = "".join(b.get("text", "") for b in (data.get("content") or []))
    else:
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        return {"ok": False, "message": "接口通了但返回空内容（模型名可能不对，或思考型模型未关 thinking）",
                "latency_ms": ms, "url": url, "model": mid, "protocol": proto}
    return {"ok": True, "message": f"连通正常，{ms} ms，返回：{text.strip()[:20]}",
            "latency_ms": ms, "url": url, "model": mid, "protocol": proto}


def _probe_ok(model: str) -> bool:
    """真实 chat 探测：确认后端能返回非空正文，而不只是「列得出模型」。

    这一层不能省。2026-09-03 实测事故：LM Studio 加载 Qwen3.5（思考型）时，
    /v1/models 正常返回模型名，available() 判定为可用，但 chat 的全部 token 预算
    被 reasoning_content 吃掉，content 恒为空 —— 管线照常跑完，日报的干净稿与
    总结双双空白，而日志里没有任何报错。列表探测挡不住这类「静默空输出」。
    """
    anthropic = config.LLM_PROTOCOL == "anthropic"
    body = {
        "model": model,
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "回复一个字：好"}],
    }
    if config.LLM_SERVED_ID and not config.llm_is_cloud() and not anthropic:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{config.llm_base_url()}{'/messages' if anthropic else '/chat/completions'}",
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(),
    )
    try:
        with _opener().open(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.error("llm probe failed (model=%r): %s", model, e)
        return False

    if anthropic:
        text = "".join(b.get("text", "") for b in (data.get("content") or []))
        if text.strip():
            return True
        log.error("llm probe returned EMPTY content (anthropic protocol)")
        return False

    msg = data["choices"][0]["message"]
    if (msg.get("content") or "").strip():
        return True
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    log.error(
        "llm probe returned EMPTY content (reasoning=%d chars). "
        "典型原因：思考型模型未关闭 thinking（需后端支持 chat_template_kwargs/"
        "enable_thinking，LM Studio 不支持），或请求体 model 字段与 /v1/models 返回的 id 不一致。"
        "此时去口气与总结会全部为空。",
        len(reasoning),
    )
    return False


def available() -> bool:
    """探测 LLM 服务可达且目标模型在位，且真能产出非空正文。

    匹配必须同时考虑 LLM_SERVED_ID 与 LLM_MODEL，原因：
    - mlx-lm 的 /models 返回的是**模型目录绝对路径**（如 /Users/.../Qwen3.5-4B-4bit）
    - LM Studio / Ollama 返回的是**模型名**（如 qwen3.5-4b）
    只拿 LLM_MODEL 做子串匹配会在 mlx-lm 下永远失配，导致管线误判「LLM 不可用」
    而整条跳过——去口气与总结全部丢失，且日志无任何报错。
    """
    try:
        req = urllib.request.Request(f"{config.llm_base_url()}/models", headers=_headers())
        with _opener().open(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
            ids = [str(m.get("id", "")) for m in data.get("data", [])]
    except Exception:
        return False

    if not ids or config.llm_is_cloud():
        # 服务可达但没列模型（部分后端不实现 /models 列表）→ 用配置里的 id 直接探测。
        # 云端同理：厂商的 /models 常常不列全部别名（DashScope 兼容模式下尤其明显），
        # 拿名字去匹配会永远失配。云端只要鉴权通过即可，最终以一次真实 chat 为准。
        return _probe_ok(_model_id())

    wanted = [config.LLM_SERVED_ID, config.LLM_MODEL]
    for w in wanted:
        w = (w or "").rstrip("/").lower()
        if not w:
            continue
        for i in ids:
            il = i.rstrip("/").lower()
            if w in il or il in w:
                # 命中模型名后必须再验证一次真实输出，只查列表挡不住「静默空正文」
                return _probe_ok(i)
    log.warning("llm model not found: served=%r model=%r, ids=%s",
                config.LLM_SERVED_ID, config.LLM_MODEL, ids)
    return False


def wait_ready(timeout: float = 240.0, interval: float = 3.0) -> bool:
    """等待 LLM 就绪（最多 timeout 秒），窗口模式下必经的一步。

    窗口模式（DH_AGG_WINDOWS 非空）时 mlx-lm 由 keepalive.sh 按窗口拉起：
    30s 轮询一次 + 4B 模型加载约几十秒。sweeper 在窗口内触发带 LLM 的轮次前
    必须先等它就绪——否则第一轮会以「LLM 不可用」落一个 raw-only 的日报，
    且该轮把 pending 清空后没人再补算，干净稿/总结要等到下个窗口才恢复。
    实时模式（窗口关闭）下 LLM 常驻，本函数第一次探测即通过，等价于直接用。
    """
    import time as _time
    deadline = _time.monotonic() + max(0.0, timeout)
    while True:
        if available():
            return True
        if _time.monotonic() >= deadline:
            log.warning("llm.wait_ready: not ready after %.0fs (base=%s)",
                        timeout, config.llm_base_url())
            return False
        _time.sleep(interval)


# ── 本地后端按窗口启停（2026-09-27，资源优化任务书·任务 A）──────────────
#
# 生产链路里 mlx-lm 由 Swift 外壳在「服务启动时」无条件拉起并 24h 常驻（≈3.6GB，
# 本机最大的单项内存占用）。窗口化后生命周期归服务端管：
#   窗口内 → 进程不在就拉起（与外壳/keepalive 同一条命令行）
#   窗口外 → 进程还在就停掉（外壳只 reconcile 一次，不会跟我们抢）
# 实时模式（DH_AGG_WINDOWS 为空）下 in_agg_window() 恒 True ⇒ 行为退化为
# 「保证常驻」，与旧 keepalive 完全一致，不需要任何配置迁移。
# 云端模式 / 未配 DH_LLM_SERVED_ID（LM Studio、Ollama 等外部后端）一律不碰——
# 我们只管自己拉起过的 mlx。
#
# 注意：本函数绝不能停一个正在跑管线的后端——窗口结束但轮次未完时等下一轮再停。

import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

_backend_state_logged = ""


def _managed_backend() -> bool:
    """是否由本模块接管本地后端生命周期。

    只接管「mlx 型」配置：非云端 且 显式配了 DH_LLM_SERVED_ID（模型目录）。
    LM Studio / Ollama 是用户自己起的进程，我们既不该拉也不该杀。
    """
    return (not config.llm_is_cloud()) and bool((config.LLM_SERVED_ID or "").strip())


def _backend_port() -> int:
    try:
        return urlparse(config.llm_base_url()).port or 1234
    except Exception:
        return 1234


def _backend_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "mlx_lm.server"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        log.exception("pgrep mlx failed")
        return False


def _model_dir() -> str:
    return (config.LLM_SERVED_ID or "").strip()


def _spawn_backend() -> bool:
    """拉起 mlx-lm。优先用 run-llm.sh（bundle 内用它才能带上内嵌解释器），
    仓库/开发环境退回 sys.executable（run.py 跑在哪个解释器，mlx 就用哪个）。"""
    model = _model_dir()
    port = _backend_port()
    here = Path(__file__).resolve().parent.parent
    script = here / "run-llm.sh"
    bundled_py = here.parent / "python" / "bin" / "python3"
    log_path = config.LOGS_DIR / "llm.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lf = open(log_path, "ab")
        if script.is_file() and bundled_py.is_file():
            cmd = [str(script), model, str(port)]
        else:
            cmd = [sys.executable, "-m", "mlx_lm.server",
                   "--model", model, "--port", str(port), "--host", "127.0.0.1"]
        subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
        log.info("llm backend spawning: %s (port %d)", " ".join(cmd), port)
        return True
    except Exception:
        log.exception("failed to spawn llm backend")
        return False


def _stop_backend() -> None:
    try:
        subprocess.run(["pkill", "-f", "mlx_lm.server"], capture_output=True, timeout=10)
    except Exception:
        log.exception("pkill mlx failed")


def reconcile_backend() -> None:
    """按「固定窗口 ∪ 数量触发 ∪ 管线在跑」对齐本地后端进程（60s 一轮）。

    第三项「管线在跑」是 2026-10-21 补的关键一条：数量触发模式下，管线会先把
    段转写并置 processed=1（积压瞬间清零），此后才进入去口气/总结阶段。
    若只按积压判断，reconcile 会认为「没活了」而不拉 mlx，等去口气时
    LLM 不可用 → 日报只剩原始稿（实测 10-21 就是这么丢了一次总结）。
    所以只要管线还在跑，就必须保住 mlx；等它跑完再按窗口/积压决定停不停。
    """
    global _backend_state_logged
    if not _managed_backend():
        return
    from . import pipeline   # 延迟导入：pipeline 依赖本模块，顶层会循环
    running = _backend_running()
    busy = pipeline.any_running()
    if config.agg_active() or busy:
        if not running:
            _spawn_backend()
        if _backend_state_logged != "in":
            log.info("llm backend guard: processing active (window %r / batch %d / "
                     "pipeline running=%s), ensure running",
                     config.AGG_WINDOWS_RAW, config.AGG_BATCH_MIN, busy)
            _backend_state_logged = "in"
    else:
        if running:
            _stop_backend()
            log.info("llm backend stopped (outside window %r, batch %d not due, "
                     "pipeline idle), memory released",
                     config.AGG_WINDOWS_RAW, config.AGG_BATCH_MIN)
        if _backend_state_logged != "out":
            log.info("llm backend guard: outside window %r and batch %d not due, "
                     "will stop when idle", config.AGG_WINDOWS_RAW, config.AGG_BATCH_MIN)
            _backend_state_logged = "out"


async def backend_window_manager() -> None:
    """常驻循环：每 60s 对齐一次本地后端与窗口状态。"""
    await asyncio.sleep(20)   # 等外壳自己的第一次 reconcile 完成，避免启动期抢跑
    while True:
        try:
            reconcile_backend()
        except Exception:
            log.exception("backend window manager tick failed")
        # TTS 常驻模型的空闲回收（2026-10-22）：Ming 占 1.9GB，
        # 只在合成时才检查空闲是不够的——不播报就永远不会触发。
        try:
            from . import tts
            tts.reap_idle()
        except Exception:
            log.exception("tts idle reap failed")
        await asyncio.sleep(60)


def start_backend_manager() -> asyncio.Task | None:
    """在事件循环里启动后端窗口守护（lifespan 内调用）。"""
    try:
        return asyncio.get_running_loop().create_task(backend_window_manager())
    except RuntimeError:
        log.warning("llm backend manager not started: no running event loop")
        return None
