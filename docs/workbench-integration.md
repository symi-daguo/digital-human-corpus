# 工作台对接（智能家居 AI 工作台）

采集端（这台 Mac）把一段**口述物料描述**送进「智能家居 AI 工作台」，
由工作台跑 LLM 做结构化抽取，判为产品类且无缺项时**直接落库成产品**；
工作台再基于这个产品出报价方案（PPTX）与配套讲解音频。

本文是这条链路的**使用与排障手册**：怎么配、怎么调、出错怎么查。

---

## 零、这条链路长什么样

```
手机端（录音 / 切段 / 上传）
        ↓  PUT /api/upload/{segId}
采集服务端（本机，FastAPI）
        ↓  两级 ASR → 说话人聚类 → LLM 清洗 → 日报
        │
        │  ← 新增：把一段物料描述推进工作台
        ↓
   POST /api/push/workbench
        │
        │  可选：先经 Hermes 理解与判定（不直接直投）
        ↓
Hermes（127.0.0.1:9876，voice invoke）
        ↓
智能家居 AI 工作台  POST /api/content/extractions/transcript
        ↓
  LLM 结构化抽取 → 产品落库 → 报价方案 → 讲解音频
```

**分工**：采集端只管「把语音变成文字」与「把东西送过去」；
工作台管「把文字变成结构化的产品资料」与后续方案。
本模块是两者之间那条**可选**的窄缝 —— 没配好时采集主链路完全不受影响。

---

## 一、前置条件

### 1.1 工作台侧

工作台要开一个**机器账号**并签发 API Key（JWT）：

```bash
# 管理员 token（工作台自己的登录接口拿）
TOK=<管理员 JWT>

# 1) 建机器账号（roleKey 取可用的角色键之一：
#    delivery/designer/finance/marketing/operator/purchase/sales/service/super_admin/tenant_admin/viewer）
curl -s -X POST http://127.0.0.1:3080/api/agent/service-accounts \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"name":"采集服务","roleKey":"operator"}'
# → {"id":"<服务账号 id>", ...}

# 2) 签发 Key（ttlDays 365；Key 只显示一次，当场存好）
curl -s -X POST http://127.0.0.1:3080/api/agent/keys \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"serviceAccountId":"<服务账号 id>","name":"采集服务入站","ttlDays":365}'
```

机器凭证**只能操作它自己那个租户**：请求必须带 `X-Tenant-Id` 指向该凭证所属租户，
指到别的租户会被工作台拒成 `403 API_KEY_TENANT_MISMATCH`。

> 采集侧需要的是**工作台租户 id**，不是服务账号 id —— 两者不是一回事，
> 填错的表现是 403 租户不匹配。

### 1.2 采集侧

在 `server/env.local` 里补齐四个配置（模板见 `server/env.local.example`）：

```bash
export DH_WORKBENCH_BASE_URL="http://127.0.0.1:3080"
export DH_WORKBENCH_API_KEY="<上一步签发的 JWT>"
export DH_WORKBENCH_TENANT_ID="<工作台租户 id>"
```

**改完必须重启服务**：env 的加载是 `setdefault` 语义（已存在的环境变量优先），
不重启就还在用旧值。

---

## 二、接口

### 2.1 `POST /api/push/workbench` —— 把物料描述推进工作台

鉴权用采集服务自己的配对 Token（`Authorization: Bearer <DH_TOKEN>`），
与手机端连服务端用的是同一个。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | **必填**，正文，20 ~ 200000 字 |
| `title` | string | 标题，≤100 字 |
| `docType` | string | `product` \| `meeting` \| `generic`，缺省 `meeting` |
| `kind` | string | 目前只有 `transcript` |
| `tenant` | string | 采集端租户 id，缺省取 `X-Tenant` 头，再缺省 `default` |
| `ref` | string | 来源标识（通常是录音段的 `seg_id`）。**给了就幂等** |
| `viaHermes` | bool/string | 是否经 Hermes 中转，**优先于**部署默认值 |

响应体是摘要（不是工作台的全量响应 —— 全量含整篇文档与每个字段，读不下去）：

```json
{
  "ok": true,
  "duplicate": false,
  "status": "pushed",
  "ref": "seg-20261025-001",
  "tenant": "default",
  "workbenchTenant": "<工作台租户 id>",
  "jobId": "<抽取作业 id>",
  "jobStatus": "CONFIRMED",
  "docType": "product",
  "score": 97,
  "deficiencyCount": 0,
  "deficiencies": [],
  "fieldCount": 5,
  "confirmed": true,
  "viaHermes": false,
  "hermes": null
}
```

- `duplicate=true` 表示这个 `ref` 之前推过，本次**没有**再打工作台，返回的是上次的结果；
- `viaHermes=true` 时多一个 `hermes` 段，带 Hermes 的判定结果（见第四章）。

调一把：

```bash
curl -s -X POST http://127.0.0.1:8777/api/push/workbench \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer digital-human' \
  -d '{"kind":"transcript","title":"真hermes中转自检","docType":"product",
       "text":"现场勘察：客厅装一个智能面板，型号 i7，白色，86 面板，单价 899 元，含安装调试服务。",
       "ref":"real-hermes-108d","viaHermes":true}'
```

### 2.2 `POST /api/tts/synthesize` —— 任意文本合成语音

工作台的讲解音频由采集服务合成，这端点就是它的对外形态。

请求体：`{"text": "<要合成的文本>", "format": "wav"}`

- 文本上限 **20000 字符**（`config.TTS_SYNTH_MAX_CHARS`），超了返回 400 并说明；
- 只支持 `wav`：**16kHz 单声道**（手机端与 ASR 输入链路的格式）；
- 播报内容经 `X-TTS-Text` 头回传（URL 编码，只回显前若干字），
  调用方能确认「念的到底是哪段」。

与 `/tts/preview` 共用**同一个** `tts.synthesize` 与同一套后端探测：
预览能响，正式合成就必须能响。两边各写一套探测逻辑，迟早会漂移成
「预览正常、正式合成没声音」，而这种故障在设置页完全看不出来。

---

## 三、配置项

| 配置项 | 默认 | 作用 |
| --- | --- | --- |
| `DH_WORKBENCH_BASE_URL` | `http://127.0.0.1:3080` | 工作台基址 |
| `DH_WORKBENCH_API_KEY` | 空 | 机器凭证（JWT）。**只放 env.local，绝不入库** |
| `DH_WORKBENCH_TENANT_ID` | 空 | 默认工作台租户 id |
| `DH_WORKBENCH_TENANT_MAP` | 空 | 采集租户 → 工作台租户的一对一映射（JSON 串） |
| `DH_WORKBENCH_TIMEOUT` | `120` | 单次推送超时（秒）。工作台侧要走 LLM 抽取，比普通 API 慢一个量级 |
| `DH_WORKBENCH_PUSH_VIA_HERMES` | `0` | 是否**强制**经 Hermes 中转投递 |

多工作台空间时才需要 `DH_WORKBENCH_TENANT_MAP`：

```bash
export DH_WORKBENCH_TENANT_MAP='{"default":"<租户 A>","store-2":"<租户 B>"}'
```

映射表**优先于**默认租户；两者都没有 → 503。

> 注意一个坑：映射表非空但**不含**当前采集租户时，
> 「缺配置检查」是查不出来的（它只看有没有映射），只有真正解析租户时才发现。
> 所以端点里必须显式做这一步解析。

---

## 四、经 Hermes 中转投递

### 4.1 为什么不是「让 Hermes 自己去投」

需求是「Hermes 把采集数据传给工作台做方案」。先查清 Hermes **实际有什么**：

本机 Hermes 暴露 `/hermes/voice/invoke` 与 `/hermes/voice/ack`，
是一条**语音大脑**链路：去重 → 意图归一 → 联网查询快路径 →
确定性设备控制快路径 → 大模型兜底。它**没有**「往外部业务系统投递数据」的通道。

所以「让 Hermes 发那个 POST」不能靠喊话达成。可选路径两条：

| 路径 | 做法 | 取舍 |
| --- | --- | --- |
| 改 Hermes 本机脚本 | 给接收器加一条确定性「工作台投递快路径」，照抄 `try_device_control` 的模式 | 能真正做到「Hermes 发起」，但那是**本机运行态脚本**（不在版本控制、随 Hermes 更新被覆盖），且动的是用户每天在用的服务 |
| **采集服务侧经 Hermes 中转**（采用） | 采集服务在投递前**先经过 Hermes 一次**：Hermes 作为必经关卡做判定（不可达即 503、不投递），再由采集服务执行投递 | 采集服务与工作台**都在版本控制内**，Hermes 是链路的必经环节而不只是旁路 |

### 4.2 Hermes 在这条链路上实际做了什么（实测）

真 Hermes（`127.0.0.1:9876`）实测，推一段物料描述，它回：

```json
{"ok": true, "skip": true, "reason": "not_conversation",
 "note_reason": "require_alias", "intent_type": "note_only",
 "reply_text": "", "reply_short": "", "ha_used": false, "duration_ms": 0}
```

即：**Hermes 记录了这一轮，但没有生成应答**。原因是它自己的硬门
`REQUIRE_ALIAS_TO_REPLY`（默认开）—— 文本里没有唤醒别名就一律只记录。

这是**正确行为不是缺陷**：采集端送来的是现场勘察记录，
本来就不是「在跟管家说话」，Hermes 不该对着它播报。

所以「经 Hermes 理解」的准确含义是：**它是投递链路的必经关卡与判定环节**，
而不是「Hermes 会把物料改写成一段话再交给工作台」。

失败处理与别处一致：**任何失败都明确报错，不静默降级**。
Hermes 不可达时返回 503 并说清「不做静默直投」——
否则调用方以为经过了 Hermes，其实没有，那比直接报错更坏。

### 4.3 刻意不做的事

不给物料描述硬套唤醒词外壳去「骗」Hermes 走对话分支。理由：

1. 会让 Hermes 每次推送都播报一句，打扰用户；
2. Hermes 的改写结果当前不进工作台，不产生业务价值；
3. 让 LLM 改写原始素材会**降低**建档准确率 —— 实测原文直投评分 97、0 缺项、CONFIRMED；
4. YAGNI。

若将来确实要让 Hermes 的语义理解并进工作台抽取，正确做法是做成**显式可选的增强**
（配置开关 + 理解结果作为独立上下文字段传给工作台，而不是改写原文），
而不是去改 Hermes 或硬造外壳。

### 4.4 一个容易踩空的地方

`hermes_client.invoke` 的 `user_aliases` 参数在当前 Hermes 版本里**完全没被读取**
（接收器脚本里 grep 零命中）。Hermes 只认「文本里的别名」，
以及调用方传的 `has_alias` 线索 —— 而 `has_alias` 在 2026-09-14 收紧后
只作对比参考、不能覆盖文本判定。

原来的 `user_aliases=[]` 传了等于没传。这不改变结论，但**别再把它当成开关用**。

---

## 五、幂等与失败处理

**幂等占坑在失败时必须撤销**，否则一次网络抖动就把素材永久标记成「已推送」。

- 占坑与判重是**同一条原子语句**（`store.claim_push` 用 `INSERT OR IGNORE`）：
  「先查后插」在并发下会双双判定为新 `ref`，同一段素材被推进两次；
- 推送失败 → 撤销占坑 → 同一个 `ref` 还能重推；
- 推送成功 → 保留占坑 → 第二次推同一个 `ref` 返回 `duplicate=true` 与上次的结果，
  **不再打工作台一次**。

---

## 六、故障排查

### `401 {"message":"USER_NOT_FOUND"}`

**最常见的一个**，而且极具误导性：HTTP 状态码是 401，但含义不是「Token 无效」，
而是「Token 解开了、但对应的用户不存在」。

根因几乎总是：**采集侧手里拿的是一把旧 Key**。

核对凭证**不能只看前 30 字符** —— JWT 的头部对所有 Key 都一模一样，
比对了等于没比对。必须解到 payload 的 `sub`：

```bash
node -e '
const fs=require("fs");
const tok=fs.readFileSync("/tmp/new_wb_key.txt","utf8").trim();
console.log(JSON.parse(Buffer.from(tok.split(".")[1]
  .replace(/-/g,"+").replace(/_/g,"/"),"base64").toString("utf8")));
'
# 看 sub 是不是当前那个服务账号 id
```

第二个坑：**改完 `env.local` 必须重启进程**。env 的加载是 `setdefault` 语义
（已存在的环境变量优先），候选顺序为 `DH_ENV_FILE` → 代码目录 `env.local`
→ 数据目录上一级。不重启就还在用旧值。

### `403 API_KEY_TENANT_MISMATCH`

`X-Tenant-Id` 指向的租户与机器凭证所属租户不一致。
凭证是哪个租户签的，就只能往哪个租户推。

### `503 工作台对接未配置，缺少：DH_WORKBENCH_API_KEY`

配置没读到。按第三章配齐并**重启服务**。

### `503 Hermes 不可达`

中转模式下 Hermes 不可达。这是**故意的**：宁可报错也不静默直投。
确认 Hermes 在跑、`DH_HERMES_BASE_URL` 指对了。

### `502 工作台返回 HTTP 4xx/5xx`

工作台明确拒了请求 —— 是我们发过去的东西有问题。
响应体会带上工作台的原话（如 zod 的字段校验详情），照着改。

---

## 七、自检

仓库自带一个自检脚本，**自带假 Hermes 与假工作台**（不依赖本机真的那两个在跑）：

```bash
cd server
"$APPPY" tools/check_push_and_tts.py
# → 通过 37 项，失败 0 项
```

它覆盖：正常推送、幂等重复、Hermes 不可达、Hermes 判定为 skip、
字符串 `"false"` 的解析方向、缺配置、基址不可达、租户不匹配等分支。

### 门禁必须与生产库隔离

自检**绝不打真实工作台**。早期版本用 `env.local` 里的真地址与真 Key，
结果跑一次门禁就在生产库里留下一批抽取作业与审计记录 ——
**门禁本身成了数据污染源**，跑一次脏一次库，只能靠人工清理兜着。

现在脚本自带一个最小假工作台（`FAKE_WORKBENCH_SRC`，只用标准库 `http.server`），
并把 `DH_WORKBENCH_BASE_URL` / `DH_WORKBENCH_API_KEY` / `DH_WORKBENCH_TENANT_ID`
三项**强制覆盖**成假值：凭据是假的、租户是假的，只有**代码路径**是真的。

判据落在**数据**上，不是「我以为打的是假的」：假工作台把收到的每个请求写进
`received.jsonl`，脚本跑完读它 —— 收不到请求就说明采集端打去了别处。
第 [8] 组四条断言就是查这个：请求是否全进了假工作台、是否带了机器凭证、
是否带了租户头、配置指向的是不是假地址。

**反向测试**（确认断言可证伪，脚本本身也要能被验）：

| 注入的坏法 | 变红的断言 |
| --- | --- |
| Hermes 不可达时静默直投 | 「Hermes 不可达：返回 503」「提示里点名了 Hermes」「明说没有静默直投」「失败后同一 ref 还能重推」 |
| `truthy()` 改用 `bool()` | 「viaHermes 传字符串 "false" 时确实不走中转」「没走中转时没有 hermes 段」 |
| 门禁改打真实工作台（真地址 + 真凭据 + 真租户） | **精确 1 条**：「门禁配置指向假工作台」 |

第三行那个坏法是精确的：推送**完全成功**（真工作台正常响应），
其余 36 条断言依然全绿，只有隔离那一条变红 ——
说明这条断言在「别的都正常、只是打错了地方」时也能抓住。
跑完该坏法后，生产库的抽取作业数确实 +4，反过来证实了旧版确实在污染。

---

## 八、已知边界

1. **只支持 `kind=transcript`**：目前只有「给一段文字就抽取」这一种入口。
   工作台的 `upload`（传文件）与 `url`（传网址）两条入口采集侧没接。
2. **Hermes 不做语义理解**（只记录不播报），见 4.2 / 4.3。
3. **`docType` 缺省是 `meeting`**：要落成产品必须显式传 `product`，
   否则工作台按会议纪要抽取，不会落成产品。
4. **工作台侧的方案与讲解音频由工作台负责**：采集端只送到「建档」这一步，
   后面的报价 PPT 与讲解音频合成在工作台那边完成。
5. **本模块是可选的**：不配置任何 `DH_WORKBENCH_*` 时，采集主链路
   （录音 → ASR → 日报）完全不受影响。
