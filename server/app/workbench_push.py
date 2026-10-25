"""把采集结果推进「智能家居 AI 工作台」（`/api/content/extractions/transcript`）。

## 定位

采集端只管「把语音变成文字」，工作台管「把文字变成结构化的产品资料」。
本模块是两者之间那条**可选**的窄缝：采集端把一段口述物料描述 POST 给工作台，
工作台跑 LLM 结构化抽取，判定为产品类且无缺项时直接落库成产品。

## 失败处理原则

**任何失败都抛 `WorkbenchError`，绝不抛裸异常、绝不让调用方 500。**
对接是可选能力：工作台挂了、没配好、租户配错，都不该把采集服务带崩。
路由层拿到 `WorkbenchError` 后翻译成 503 或 502，并带上「缺哪一项」或
「工作台原话说了什么」——排障时最怕的就是一个没有信息量的 500。

两种状态码的分工：
- **503**：这边还没准备好（缺配置、连不上、超时）——去配 env.local 或看网络。
- **502**：工作台明确回了错误（4xx/5xx，比如租户不匹配）——是我们发过去的请求有问题。

## 为什么用标准库 urllib 而不是 httpx / requests

与 hermes_client.py 保持一致：依赖表里没有 HTTP 客户端库，而这里是
「一次请求拿一个 JSON」的简单场景。若将来要做 SSE 流式，再引 httpx 不迟。

## 代理

本机回环调用必须显式禁用代理：run.py 虽然清了 HTTP(S)_PROXY，但如果用户在
「设置 → 云端代理」里填了 DH_HTTPS_PROXY，某些库会把它当通用代理用，导致
127.0.0.1 的请求被发到外网代理上去（表现为莫名超时）。ProxyHandler({}) 一次解决。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config
from . import hermes_client

log = logging.getLogger(__name__)

# 显式禁代理的 opener（见模块 docstring）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 工作台侧的取件口。注意是 **transcript**（口述转写）那条：
# upload 要传文件、url 要传网址，只有 transcript 是「给一段文字就抽取」。
TRANSCRIPT_PATH = "/api/content/extractions/transcript"

# 工作台的 transcript 入参规格（docType 默认 meeting、title ≤100 字、
# text 20~200000 字）。这里按同一份约束在本地先挡一道：出错时给出的是
# 「text 至少 20 字」这种能直接改的提示，而不是工作台回的一句 zod 报错。
DOC_TYPES = ("product", "meeting", "generic")
TEXT_MIN = 20
TEXT_MAX = 200_000
TITLE_MAX = 100


class WorkbenchError(RuntimeError):
    """对接失败。

    `status` 是给路由层的 HTTP 状态码建议（503/502），
    `detail` 是直接回给调用方的中文说明——必须说清「缺哪一项」或「对方说了什么」，
    否则用户只能对着一个 503 干瞪眼。
    """

    def __init__(self, detail: str, status: int = 503):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def tenant_map() -> dict[str, str]:
    """解析 `DH_WORKBENCH_TENANT_MAP`（采集端租户 → 工作台租户 id 的 JSON 串）。

    解析失败只记日志并按「没有映射」处理：这份配置写错了不该让整个服务起不来，
    但必须留一条 warning，否则表现是「明明配了映射却不生效」。
    """
    raw = (config.WORKBENCH_TENANT_MAP_RAW or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        log.warning("DH_WORKBENCH_TENANT_MAP 不是合法 JSON，已忽略：%s", e)
        return {}
    if not isinstance(data, dict):
        log.warning("DH_WORKBENCH_TENANT_MAP 必须是 JSON 对象，已忽略")
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        k, v = str(k).strip(), str(v).strip()
        if k and v:
            out[k] = v
    return out


def missing_config() -> list[str]:
    """列出还缺哪些配置项（供路由层一次性说清，而不是逐个试错）。"""
    miss: list[str] = []
    if not (config.WORKBENCH_BASE_URL or "").strip():
        miss.append("DH_WORKBENCH_BASE_URL")
    if not (config.WORKBENCH_API_KEY or "").strip():
        miss.append("DH_WORKBENCH_API_KEY")
    # 租户可以由默认项提供，也可以由映射表提供；两者都没有才是真的缺。
    if not (config.WORKBENCH_TENANT_ID or "").strip() and not tenant_map():
        miss.append("DH_WORKBENCH_TENANT_ID")
    return miss


def resolve_workbench_tenant(tenant: str = "") -> str:
    """采集端租户 → 工作台租户 id。

    映射表优先（多空间时用），否则退回 `DH_WORKBENCH_TENANT_ID`。
    两者都没有就是没配好——抛 503，不猜。
    """
    tid = (tenant or "").strip()
    mapped = tenant_map().get(tid)
    if mapped:
        return mapped
    fallback = (config.WORKBENCH_TENANT_ID or "").strip()
    if fallback:
        return fallback
    raise WorkbenchError(
        f"未配置工作台租户：采集租户 {tid or '(默认)'} 在 DH_WORKBENCH_TENANT_MAP "
        f"里没有映射，且 DH_WORKBENCH_TENANT_ID 为空", 503)


def push_transcript(text: str, title: str = "", doc_type: str = "meeting",
                    tenant: str = "") -> dict:
    """把一段物料描述推进工作台，返回工作台那边的原始 JSON 响应。

    失败一律抛 `WorkbenchError`（503 = 没配好/连不上，502 = 工作台明确拒绝）。
    这里不做任何静默降级：调用方需要知道「到底推进成功没有」，
    静默吞掉失败只会让素材在无人察觉的情况下永远停在采集端。
    """
    base = (config.WORKBENCH_BASE_URL or "").rstrip("/")
    if not base:
        raise WorkbenchError("未配置 DH_WORKBENCH_BASE_URL（工作台基址）", 503)
    if not (config.WORKBENCH_API_KEY or "").strip():
        raise WorkbenchError("未配置 DH_WORKBENCH_API_KEY（机器凭证）", 503)
    wb_tenant = resolve_workbench_tenant(tenant)

    payload: dict = {"text": text, "docType": doc_type}
    if title:
        # 工作台的 title 上限 100 字，超了会被 zod 拒掉；这里先截断，
        # 免得一个长标题把整条推送废掉。
        payload["title"] = title[:TITLE_MAX]
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base}{TRANSCRIPT_PATH}", data=data, method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {config.WORKBENCH_API_KEY}",
            # 机器凭证只能操作它自己那个租户：这个头必须指向该凭证所属的租户，
            # 指向别的租户会被工作台拒成 403 API_KEY_TENANT_MISMATCH。
            "X-Tenant-Id": wb_tenant,
        },
    )
    try:
        with _opener.open(req, timeout=config.WORKBENCH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise WorkbenchError(
            f"工作台返回 HTTP {e.code}：{body or '(空响应体)'}", 502) from e
    except TimeoutError as e:
        raise WorkbenchError(
            f"工作台响应超时（{config.WORKBENCH_TIMEOUT:.0f}s）：{base}", 503) from e
    except urllib.error.URLError as e:
        raise WorkbenchError(f"连不上工作台 {base}：{e.reason}", 503) from e
    except Exception as e:
        raise WorkbenchError(f"推送工作台失败：{e}", 503) from e

    try:
        return json.loads(raw)
    except ValueError as e:
        raise WorkbenchError(f"工作台返回的不是 JSON：{raw[:200]}", 502) from e


def truthy(v) -> bool:
    """把请求里可能出现的多种写法归一成布尔。

    为什么不能直接 `bool(v)`：JSON 里 `false` 是布尔，但表单或查询串传进来
    的是字符串 `"false"` —— 而 `bool("false")` 是 **True**。
    「显式说不要走 hermes」会被当成「要走」，方向正好反了。
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def hermes_understand(text: str, tenant: str = "", ref: str = "") -> dict:
    """把这段物料描述先送 Hermes 过一遍，返回它的应答摘要。

    ## 这一步在链路里的位置

    需求是「hermes 把采集数据传给工作台做方案」。查过 Hermes 的接口：
    它只有语音 invoke/ack（一条「理解 + 应答」链路），**没有对外投递数据的通道**。
    所以分工是：**Hermes 负责理解与判定，采集端负责执行投递** ——
    文本先过 Hermes 一遍，它做意图归一、查实时设备数据、给出一句应答，
    这句应答与后续工作台建档结果一起回到调用方，整条链路才闭环。

    ## 失败处理

    `WorkbenchError`（503）：Hermes 不可达或调用失败。
    **绝不静默直投** —— 调用方以为经过了 Hermes，其实没有，
    那比直接报错更坏（数据可能带着没被理解的语义进了工作台）。
    """

    if not hermes_client.available(force=True):
        raise WorkbenchError(
            f"Hermes 不可达（{config.HERMES_BASE_URL}）：中转投递要求先经 Hermes 理解，"
            f"不做静默直投", 503)
    # session_id 带 ref：同一段素材的多次推送共享上下文，Hermes 能看出是同一件事。
    # ref 为空时用空串（invoke 的默认值），不编造一个会话 id。
    res = hermes_client.invoke(
        text, user_aliases=[], tenant=tenant or "default",
        session_id=(f"workbench-{ref}" if ref else ""),
        device="workbench-push")
    if res is None:
        raise WorkbenchError(
            f"Hermes 调用失败（已重试 {config.HERMES_RETRY} 次）：{config.HERMES_BASE_URL}", 503)
    return {
        "replyText": str(res.get("reply_text") or ""),
        "replyShort": str(res.get("reply_short") or ""),
        "intentType": str(res.get("intent_type") or ""),
        # skip 不算失败：Hermes 判为「不需要应答」也是一种判定结果
        # （它照常记录了，只是没有生成播报文本）。只有「压根没调通」才算失败。
        "skip": bool(res.get("skip")),
        "reason": str(res.get("reason") or ""),
        "durationMs": int(res.get("duration_ms") or 0),
    }


def push_via_hermes(text: str, title: str = "", doc_type: str = "meeting",
                    tenant: str = "", ref: str = "") -> dict:
    """经 Hermes 中转后投递工作台，返回 `{hermes, workbench}`。

    Hermes 不可达 → 抛 `WorkbenchError`（503），**不投递**。
    Hermes 通了但工作台拒了 → 抛 `WorkbenchError`（502），同样不静默。
    """
    hermes = hermes_understand(text, tenant=tenant, ref=ref)
    resp = push_transcript(text, title=title, doc_type=doc_type, tenant=tenant)
    return {"hermes": hermes, "workbench": resp}


def summarize(resp: dict) -> dict:
    """把工作台的响应压成一段摘要。

    工作台的响应体很大（整篇文档 + 每个字段 + 每条缺项），
    采集端只回摘要：排障要的是「作业 id / 文档类型 / 缺项数」，
    把全文塞进响应只会让日志与调用方都读不下去。
    """
    doc = resp.get("document") or {}
    deficiencies = resp.get("deficiencies") or []
    fields = doc.get("fields") or []
    return {
        "jobId": resp.get("id") or "",
        "status": resp.get("status") or "",
        "score": resp.get("score"),
        "docType": doc.get("docType") or "",
        "title": doc.get("title") or "",
        "confirmed": bool(doc.get("confirmed")),
        "fieldCount": len(fields),
        "deficiencyCount": len(deficiencies),
        "deficiencies": [
            str(d.get("label") or d.get("fieldKey") or "") for d in deficiencies
        ][:20],
    }
