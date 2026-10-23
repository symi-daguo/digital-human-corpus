"""日报洞察层：价值评分 / 关键词提取 / 隐私汇总。

为什么要有这一层（2026-10-23，用户需求原话）：
「每天讲话会有很多无效的白话，要让智能体优先提取每天真实有用的数据，
 然后再针对关键有效关键词语进行处理，这样更高效、节约大量数据提取浪费 token」。

实测（2026-10-22 全天 416 段 / 17811 字符）纠正了一个直觉：
纯语气词白话在 VAD 之后几乎为 0（<0.2%），真正的 token 浪费来自
**低信息密度的闲聊长句**——它们和实质内容一起进 LLM 清洗与总结，
LLM 读得懂但产出为零。这一层用**零 LLM 成本**的确定性规则先把
「值得 LLM 花 token 的行」和「不值得的行」分开：

  - line_value()      单行价值分（实体信号 + 有效长度）
  - partition_lines() 把原始稿切成「高价值run / 低价值run」交替序列，
                       高价值 run 才进 LLM 清洗，低价值 run 只做确定性清理
  - extract_keywords() 全天关键词 Top-N（行业词命中 + 数字/英文实体 + 高频词）
  - privacy_overview() 隐私分类汇总（redact.classify 的全天聚合）

全部是 O(n) 正则与计数，2 万字符 <10ms，对处理管线无感。
"""
from __future__ import annotations

import os
import re
from collections import Counter

from . import redact

# ---- 配置（环境变量可直接覆盖，无需改代码） --------------------------------
# 价值门总开关：0=关（所有行照旧进 LLM），1=开
VALUE_GATE = os.environ.get("DH_VALUE_GATE", "1") not in ("0", "false", "no")
# 低价值判定：有效字符（去标点）少于此阈值的行，且无实体信号，才算低价值
VALUE_MIN_CHARS = int(os.environ.get("DH_VALUE_MIN_CHARS", "8"))
# 关键词 Top-N
KEYWORDS_TOP_N = int(os.environ.get("DH_KEYWORDS_TOP_N", "12"))

# 行业词由 config 在启动时按行业加载；这里延迟引用，避免循环导入
_industry_terms: list[str] | None = None


def set_industry_terms(terms: list[str]) -> None:
    """由 config.reload() 注入当前行业词表（供价值评分与关键词提取加权）。"""
    global _industry_terms
    _industry_terms = [t for t in (terms or []) if len(t) >= 2]


def _get_industry_terms() -> list[str]:
    if _industry_terms is None:
        try:
            from . import config
            set_industry_terms(list(getattr(config, "INDUSTRY_TERMS", []) or []))
        except Exception:
            set_industry_terms([])
    return _industry_terms or []


# 实体信号：数字串（价格/型号/尺寸/时间）、英文词（品牌/型号/接口名）
_ENTITY_RE = re.compile(r"\d{2,}|[A-Za-z]{2,}")
# 有效字符：去标点空白后剩下的（中文按单字计）
_PUNCT_RE = re.compile(r"[\s，。！？、；：\"'（）()【】\[\]{}…·,._!?;:<>-]+")
# 纯应答/语气（整行只有这些字，任意组合重复）
_FILLER_RE = re.compile(r"^(?:[嗯啊呃哦唉嘿哈嘛呗呀啦喔噢诶好行对是的没不用谢]|[\W_])+$")


def eff_len(text: str) -> int:
    """有效字符数：去掉标点空白后的长度。"""
    return len(_PUNCT_RE.sub("", text))


def line_value(text: str) -> int:
    """单行价值分 0-100。只回答「这行值不值得让 LLM 花 token」。

    评分（按对日报的贡献加权，行业词命中最重）：
      行业词命中     ×30（命中即 30 分，命中多个取前二）
      数字/英文实体   ×25（有即 25）
      有效长度        ≤40（每 4 个有效字 1 分，封顶）
    实体信号是硬通货：日报里「200×300 的玻璃」「KNX 总线」「3 月 15 号交」
    这类行几乎必含数字或英文，闲聊几乎必不含——这个区分度实测非常稳。
    """
    t = text.strip()
    if not t:
        return 0
    body = _PUNCT_RE.sub("", t)
    score = 0
    terms = _get_industry_terms()
    hit_terms = sum(1 for term in terms[:200] if term in t)   # 词表大时限前 200，防 O(n·m) 爆炸
    score += min(hit_terms, 2) * 30
    if _ENTITY_RE.search(t):
        score += 25
    score += min(eff_len(t) // 4, 40)
    return min(score, 100)


def is_low_value(text: str) -> bool:
    """低价值判定：无实体信号、非行业词、有效字少，或整行纯应答。"""
    body = _PUNCT_RE.sub("", text)
    if not body or _FILLER_RE.match(body):
        return True
    if line_value(text) < 30 and eff_len(text) < VALUE_MIN_CHARS:
        return True
    return False


def partition_lines(raw: str) -> tuple[list[str], list[str], int]:
    """把原始稿按行切成「高价值 run / 低价值 run」交替序列。

    返回 (runs, kinds, low_chars)：
      runs  = [文本块, ...]（保持原顺序，每块内同类行）
      kinds = ["hi"/"lo", ...]（与 runs 一一对应）
      low_chars = 低价值块的总字符数（省下的 LLM 输入量）

    保序是硬约束：原始稿按 [HH:MM] 时间排列，打乱顺序等于毁了时间轴。
    相邻同类行合成一个 run，避免一行一调 LLM。
    """
    runs: list[str] = []
    kinds: list[str] = []
    low_chars = 0
    cur_kind: str | None = None
    cur: list[str] = []
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            # 空行跟随当前 run，避免把排版切开
            if cur:
                cur.append(line)
            continue
        kind = "lo" if is_low_value(line) else "hi"
        if kind != cur_kind and cur:
            runs.append("".join(cur))
            kinds.append(cur_kind or "hi")
            cur = []
        cur_kind = kind
        cur.append(line)
        if kind == "lo":
            low_chars += len(line)
    if cur:
        runs.append("".join(cur))
        kinds.append(cur_kind or "hi")
    return runs, kinds, low_chars


_STOPWORDS = {
    "这个", "那个", "我们", "你们", "他们", "自己", "什么", "怎么", "现在",
    "可以", "就是", "然后", "还是", "因为", "所以", "但是", "如果", "一个",
    "一下", "没有", "不是", "时候", "这里", "那里", "东西", "地方", "问题",
    "今天", "明天", "昨天", "知道", "觉得", "应该", "可能", "出来", "起来",
    "的时候", "这一块", "哪些", "哪些东西",
}

# 行首时间戳前缀（[HH:MM] /【HH:MM-HH:MM】）。原始稿每行都带，不清掉会把
# 「19」「10」这种小时数当 Top 实体（2026-10-23 实测：Top10 全是小时数）。
_TS_PREFIX_RE = re.compile(r"^\[[\d:]+\]\s*|^[【\[][\d:]+[-–—~][\d:]+[】\]]\s*")


def _strip_ts(text: str) -> str:
    return _TS_PREFIX_RE.sub("", text)


def extract_keywords(texts: list[str], top_n: int = KEYWORDS_TOP_N) -> list[dict]:
    """全天关键词 Top-N：行业词 > 数字/英文实体 > 高频中文字段。

    零依赖实现（不引 jieba）：行业词和实体是精确串，直接计数；中文关键词
    用「2-4 字滑窗 + 停用词过滤 + 频次阈值」。
    滑窗的两个纪律（实测教训，第一版产出过 'X总'/'成2' 这类跨词碎屑）：
      1. 窗口只在**标点切开的词段内**滑动，绝不跨标点——跨了就是把
         「换成」「2.5」缝合成 '成2' 这种不存在于任何人嘴里的词；
      2. freq 类只收**纯中文**片段——混拉丁字母的归 entity 类管。
    返回 [{kw, count, kind}]，kind ∈ term/entity/freq。
    """
    term_cnt: Counter = Counter()
    entity_cnt: Counter = Counter()
    frag_cnt: Counter = Counter()
    entity_re = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,}|\d+(?:\.\d+)?(?:[×xX*]\d+(?:\.\d+)?)*")
    han_only = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
    for raw_t in texts:
        t = _strip_ts(raw_t or "")
        if not t:
            continue
        for term in _get_industry_terms():
            if term and term in t:
                term_cnt[term] += 1
        for m in entity_re.findall(t):
            tok = m.strip(".-")
            if len(tok) >= 2:
                entity_cnt[tok] += 1
        # 2-4 字滑窗：按标点切段后在段内滑，且只收纯中文（实体另算）
        for seg in re.split(r"[^\u4e00-\u9fff]+", t):
            if len(seg) < 2:
                continue
            for n in (2, 3, 4):
                for i in range(len(seg) - n + 1):
                    frag = seg[i:i + n]
                    if frag in _STOPWORDS or _FILLER_RE.match(frag):
                        continue
                    frag_cnt[frag] += 1
    out: list[dict] = []
    for kw, c in term_cnt.most_common(top_n):
        out.append({"kw": kw, "count": c, "kind": "term"})
    for kw, c in entity_cnt.most_common(top_n):
        if c >= 2 and kw not in {x["kw"] for x in out}:
            out.append({"kw": kw, "count": c, "kind": "entity"})
    # 高频片段阈值按长度分档：2 字碎片天然易撞（跨词边界），要求 ≥5 次；
    # 3-4 字 ≥3 次即可。且不能是已选关键词的子串（去重）。
    chosen = [x["kw"] for x in out]
    for kw, c in frag_cnt.most_common(top_n * 6):
        if len(out) >= top_n:
            break
        need = 5 if len(kw) == 2 else 3
        if c < need or any(kw in ch or ch in kw for ch in chosen):
            continue
        out.append({"kw": kw, "count": c, "kind": "freq"})
        chosen.append(kw)
    return out[:top_n]


def privacy_overview(texts: list[str]) -> list[dict]:
    """全天隐私分类汇总：按类别计数 + 掩码样本（不给原值）。

    语义与 redact.classify 相同，聚合到天级。汇总里**只出现掩码样本**——
    这个结构的消费方是日报与智能体输出，按出口脱敏同一标准自律。
    """
    merged: dict[str, dict] = {}
    for raw_t in texts:
        t = _strip_ts(raw_t or "")
        for item in redact.classify(t):
            cur = merged.setdefault(item["category"], {
                "category": item["category"],
                "label": item["label"],
                "count": 0,
                "samples": [],
            })
            cur["count"] += item["count"]
            for smp in item["samples"]:
                if smp and smp not in cur["samples"] and len(cur["samples"]) < 3:
                    cur["samples"].append(smp)
    return sorted(merged.values(), key=lambda d: -d["count"])


def day_insights(raw: str) -> dict:
    """一天一次的洞察总包：给 pipeline / agent_api 直接用。"""
    texts = [ln for ln in raw.splitlines() if ln.strip()]
    body = [re.sub(r"^\[[\d:]+\]\s*", "", ln) for ln in texts]   # 去时间戳前缀再统计
    return {
        "keywords": extract_keywords(body),
        "privacy": privacy_overview(body),
        "low_value_skipped_chars": 0,   # 由 value gate 回填，这里给缺省
    }
