"""敏感信息脱敏（移植自 DeskCap privacy.rs 的 emit-time 思路）。

用途：/api/agent/* 知识端点输出前对文本强制扫描，两类都要抹掉：
  1. 凭证 —— 语音里口述的密码 / API key / 私钥等「说了会泄密」的；
  2. PII  —— 手机号 / 身份证 / 银行卡 / 邮箱 / 车牌等「说了会伤隐私」的
             （2026-10-16 补：此前只覆盖凭证，PII 六类实测全部漏网）。
采集侧（转写管线）保持原样不脱敏（原始稿是用户的私有记录），
只在 agent 出口这一层做「出口脱敏」。

模式非穷尽（与 DeskCap 相同的诚实立场）：agent 侧仍须按 SKILL.md 再扫一遍。
已知未覆盖 —— 自由文本住址（如「住在世纪大道 88 号」）无法可靠正则，
需要结合上下文语义判断，交给 agent 侧自律，不进本模块。
"""
from __future__ import annotations

import re

# 边界一律用 ASCII-only 的 (?<![A-Za-z0-9_])，**不要用 \b**：Python re 默认 Unicode
# 模式把中文汉字也算作 \w，于是「邮箱zhang@ex.com」「key是sk-xxx」这类「中文紧邻
# 英文串」下 \b 不成立 → 整条规则静默失效。2026-10-16 实测：用 \b 的 7 条规则在
# 中文紧邻场景下 7/7 全部漏网（这是中文语料库的常态，不是边角）。
_ASCII_L = r"(?<![A-Za-z0-9_])"

# (名称, 编译好的模式)。命中即整段匹配文本替换为 [REDACTED]。
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Authorization / Bearer 凭据
    ("bearer", re.compile(r"(?i)" + _ASCII_L + r"bearer\s+[A-Za-z0-9\-._~+/]+=*")),
    # OpenAI / Anthropic 风格 key
    ("openai_key", re.compile(_ASCII_L + r"sk-[A-Za-z0-9_-]{16,}")),
    # GitHub token
    ("github_token", re.compile(_ASCII_L + r"(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{16,}")),
    # AWS access key
    ("aws_key", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])")),
    # JWT
    ("jwt", re.compile(_ASCII_L + r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    # 通用赋值形态：password=xxx / token:xxx / api_key:xxx / 密码：xxx
    ("kv_secret", re.compile(
        r"(?i)" + _ASCII_L + r"(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)"
        r"\s*[=:：]\s*\S{4,}")),
    ("kv_secret_cn", re.compile(r"(?:密码|口令|密钥|令牌)\s*[:：=]\s*\S{4,}")),
    # 口语形态：密码是 xxx / 密钥为 xxx（语音转写里最常见的口述方式）
    ("kv_secret_cn_spoken", re.compile(r"(?:密码|口令|密钥|令牌)\s*[是为]\s*\S{4,}")),
    # PEM 私钥块
    ("pem_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    # 长 hex/随机串（32 位以上，弱误报可接受：宁可多脱敏）
    ("hex_secret", re.compile(r"(?<![a-fA-F0-9])[a-f0-9]{32,}(?![a-fA-F0-9])")),

    # ---- 个人身份信息（PII）----
    # 与上面「凭证」分开一节：凭证是「说了会泄密」，PII 是「说了会伤隐私」，
    # 两者都不该进 agent 知识库。PII 边界取宽——误报的代价（多抹一段数字）
    # 远低于漏报（真手机号/身份证进了第三方云）。2026-10-16 补：
    # 此前只覆盖凭证，实测手机号/身份证/银行卡/邮箱/车牌 6 类全部漏网。
    # 顺序：身份证(18) 先于银行卡(16-19)，否则身份证会被当银行卡匹配。
    ("id_card_cn", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    # 中国大陆手机号：11 位、1 开头、次位 3-9（前后不能再接数字，避免误切长串）
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("email", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # 中国大陆车牌：省份简称 + 字母 + 5-6 位字母数字（含挂/学/警/港澳后缀）
    ("plate_cn", re.compile(
        r"[京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新使领]"
        r"[A-Z][A-Z0-9]{4,5}[A-Z0-9挂学警港澳]")),
]

REDACTED = "[REDACTED]"

# classify() 输出的类别中文标签（日报/智能体展示用）
CATEGORY_LABELS = {
    "bearer": "网络凭据",
    "openai_key": "API 密钥",
    "github_token": "代码仓库令牌",
    "aws_key": "云服务密钥",
    "jwt": "登录令牌",
    "kv_secret": "口述密码",
    "kv_secret_cn": "口述密码",
    "kv_secret_cn_spoken": "口述密码",
    "pem_key": "私钥文件",
    "hex_secret": "随机密钥串",
    "id_card_cn": "身份证号",
    "bank_card": "银行卡号",
    "phone_cn": "手机号",
    "email": "邮箱",
    "plate_cn": "车牌",
}


def _mask(sample: str) -> str:
    """掩码样本：留首尾各 2 字符证明「确实有这个东西」，中间全遮。"""
    s = sample.strip()
    if len(s) <= 6:
        return s[0] + "***" if s else ""
    return s[:2] + "***" + s[-2:]


def classify(text: str) -> list[dict]:
    """敏感信息**分类**（与 redact 的「全抹」互补）。

    redact() 回答「这段能不能给别人看」；classify() 回答「这段里有什么类型的
    敏感内容、有多少」——给日报做隐私高亮与汇总时按类处理用。

    返回 [{category, label, count, samples}]，按命中次数降序。
    samples 是掩码后的片段（不留原值——本函数常与原文同域调用，但返回值
    会进日报/智能体输出，按出口脱敏的同一标准处理）。
    """
    if not text:
        return []
    found: dict[str, dict] = {}
    for name, pat in _PATTERNS:
        hits = pat.findall(text)
        if not hits:
            continue
        samples = []
        for h in hits[:3]:
            sample = h if isinstance(h, str) else h[0]
            samples.append(_mask(sample))
        found[name] = {
            "category": name,
            "label": CATEGORY_LABELS.get(name, name),
            "count": len(hits),
            "samples": samples,
        }
    return sorted(found.values(), key=lambda d: -d["count"])


def redact(text: str) -> tuple[str, int]:
    """扫描并替换秘密。返回 (脱敏后文本, 命中次数)。"""
    if not text:
        return text, 0
    hits = 0
    out = text
    for _, pat in _PATTERNS:
        out, n = pat.subn(REDACTED, out)
        hits += n
    return out, hits
