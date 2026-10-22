"""处理管线编排（每天一次，上传完成事件触发）：

① ASR 批量转写上传段（手机端已是 Silero VAD 切好的 ≤30s 语音段，无需二次切段）
② 声纹 isUser 标记
③ LLM 去口气 → ④ LLM 每日总结
⑤ 文字三件套写库 + JSON/MD 备份
⑥ 语料导出（JSONL + GPT-SoVITS 目录，本人或未判定段）
⑦ 删除原始音频

幂等：任一步骤失败可整体重跑；全部成功才删原始音频。

关于二次 VAD：手机端已用 Silero VAD 切好 ≤30s 语音段，Mac 端不需要再做切段。
历史上曾实现过（sherpa-onnx Python API，参数 th=0.5 / min_sil=0.1 / min_speech=0.25），
但实测 Python API 不稳定且无调用方，已按 YAGNI 移除。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import asr, audio_tagging, config, llm, store, tenants, voiceprint

log = logging.getLogger(__name__)

# 同一租户的同一日期只允许一个管线在跑；键为 "tenant/date"
_running: set[str] = set()
# 全局串行门（2026-09-08）：mlx-lm 本地模型一次只能稳稳服务一个请求，
# 补跑扫描与手机实时触发并发不同日期（或不同租户）时，所有请求都在 mlx 内部排队，
# 每个都变慢（实测 09-07 终稿被 09-08 的增量抢占，从几十秒拖到 14 分钟）。
# asyncio.Lock 的等待队列是 FIFO：先到先跑，后来者排队，杜绝并发抖动。
# sweeper 的 any_running() 整轮跳过逻辑不受影响（排队中的任务也在 _running 里）。
# 多租户（2026-09-09）后依然全局一把门：LLM 后端是所有租户共享的，串行保护不变。
_gate = asyncio.Lock()
# 进入去语气阶段前，LLM 不可用时的最长等待（秒）。
# 数量触发模式下 mlx 由 backend_window_manager 按需拉起（30s 轮询 + 4B 模型加载
# 几十秒），而 /api/done 直连 trigger 的路径不经过 sweeper 的 _llm_ready 预等。
# 不等就会落一个 raw-only 日报，且 pending 已清空没人补算（2026-10-21 实测丢过一次总结）。
_LLM_WAIT_SEC = 180.0
# 后台任务的强引用集合：asyncio 只持有 Task 的弱引用，若不保存，
# 任务可能在 GC 时被静默回收（实测表现为管线跑一半消失且无任何日志）。
_tasks: set[asyncio.Task] = set()
# 轮内在跑的日期收到的「再跑一轮」请求：key -> (with_llm, final_pass)。
# 见 trigger() 里的说明。
_pending: dict[str, tuple[bool, bool]] = {}
# 单个 runner 连续自动补跑的上限。防止客户端高频重发 /api/done 时，
# 一个 runner 长时间占住全局 FIFO 门（_gate），把其他日期的补跑饿死。
_MAX_AUTO_RERUNS = 3

# 单轮最多转写多少段（2026-09-21 新增）。
#
# 为什么要有上限：一轮 = 转写 → 聚类 → raw 落库 → LLM 清洗，而**聚类在①之后**。
# 积压上百段时若一轮把全部转写完，手机要等十几分钟才第一次看到说话人 ——
# 实测积压 540 段时 11:43:23 起跑，直到 12:00:38 才打出
# "speaker labeling: 514/514"，其间 /api/report 返回的 segment_flags 里
# speaker 全是空，手机端一直显示「待判定」，用户观感就是「上传成功但手机没反应」。
# 限制每轮段数后，每轮都能在几分钟内闭环，手机在下次拉取（准实时 15-20 分钟）
# 就能看到进展；剩余段由后续轮次与 sweeper 继续消化。
#
# 只切**转写批次**，不切 roleless —— 聚类仍覆盖当天全部无角色段，
# 所以已转写却漏标的段照样能在本轮补上。整体幂等，不影响正确性。
_MAX_SEGS_PER_ROUND = 120


def trigger(date: str, with_llm: bool = True, final_pass: bool = False,
            tenant: str = tenants.DEFAULT_TENANT) -> bool:
    """事件触发入口（/api/done）。返回 False 表示该租户该日期已在处理中。

    2026-09-14 改：已在处理中时**不再把这次触发丢掉**，而是记一笔待补跑
    （`_pending`），本轮结束后立刻接着跑一轮。

    改之前是裸 `return False`。实测代价：19:56:09 的 /api/done 落进 19:52 那轮
    11 分钟里被静默吞掉，19:53 / 19:56 上传的 3 段就只能等下一次触发
    （20:04:26）才被处理——用户看到的现象正是「刚讲完的没处理」。

    记账是有界且自清的：每轮开始时先清账，只有真正在轮内到达的触发才会引发
    下一轮；且连续自动补跑不超过 `_MAX_AUTO_RERUNS`。没有新段时管线走 skip
    分支毫秒级返回，不会空转。
    """
    if not config.is_valid_date(date):
        log.error("pipeline.trigger rejected bad date: %r", date)
        return False
    # key 必须在这里先算出来：下面的窗口判定日志要用它。
    # （2026-09-27 实测踩过：先写在窗口判定之后 → 窗口外每次 /api/done 都
    #  NameError 抛 500，而窗口内的测试路径永远走不到那一行，测不出来。）
    key = f"{tenant}/{date}"
    if not config.agg_active():
        # 处理触发门槛：固定窗口（2026-09-27）+ 数量触发（2026-10-21）二者都没满足时，
        # 一律不**启动**新管线——段照常落库，处理延后到窗口内或积压达标时由 sweeper
        # 兜底消化。已在跑的轮次不受影响（本判定只在启动新轮次时做一次）。
        # DH_AGG_WINDOWS 留空且 DH_AGG_BATCH_MIN=0 = 实时模式，这里恒放行。
        # 注意：这正是「run.py 常驻但大部分时间不烧 CPU」的关键 —— /api/done 高频到达
        # 也只会打一行 INFO，不会触发转写/聚类/LLM。
        log.info("pipeline.trigger %s outside agg window %r and batch(%d) not due; "
                 "deferred", key, config.AGG_WINDOWS_RAW, config.AGG_BATCH_MIN)
        return False
    if key in _running:
        old = _pending.get(key)
        # final 只升不降：轮内来了 final=1，就必须补一次终稿轮
        _pending[key] = (True, bool(final_pass or (old[1] if old else False)))
        log.info("pipeline for %s already running; queued for another round (final=%d)",
                 key, _pending[key][1])
        return False
    _running.add(key)
    _pending[key] = (with_llm, final_pass)

    async def runner():
        try:
            for round_no in range(_MAX_AUTO_RERUNS + 1):
                params = _pending.pop(key, None)
                run_llm, run_final = params if params else (with_llm, final_pass)
                for attempt in range(config.PIPELINE_MAX_RETRIES + 1):
                    async with _gate:
                        ok = await asyncio.to_thread(
                            run_for_date, date, run_llm, run_final, tenant)
                    if ok:
                        break
                    log.warning("pipeline for %s failed (attempt %d)", key, attempt + 1)
                    await asyncio.sleep(60)
                if key not in _pending:
                    break
                if round_no >= _MAX_AUTO_RERUNS:
                    log.warning("pipeline for %s hit the auto-rerun cap (%d); "
                                "drop the queued request, let the sweeper pick it up",
                                key, _MAX_AUTO_RERUNS)
                    break
                log.info("pipeline for %s re-triggered while running; running another round",
                         key)
        finally:
            _running.discard(key)
            _pending.pop(key, None)

    task = asyncio.get_running_loop().create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def is_running(date: str, tenant: str = tenants.DEFAULT_TENANT) -> bool:
    """该租户该日期的管线是否正在跑（补跑扫描靠它串起来，避免并发压垮 LLM）。"""
    return f"{tenant}/{date}" in _running


def any_running() -> bool:
    """是否有任意任务在跑。扫描器在别人干活时整轮跳过，不抢 LLM 显存。"""
    return bool(_running)


def _line(s: dict) -> str:
    """日报正文的一行：[HH:MM] [角色] 文本。

    每句都带 [HH:MM] 时间戳（2026-09-03 按用户要求改）：此前只在间隔 >10min 时
    插入「— HH:MM —」块头，块内几十分钟的内容无法定位时间段，用户明确要求
    三版（原始/干净/总结）都能区分时间段。
    """
    ts = datetime.fromtimestamp(s["start_offset_ms"] / 1000).strftime("%H:%M")
    body = f"[{s['speaker_label']}] {s['text']}" if s["speaker_label"] else s["text"]
    # 重复幻觉兜底（2026-09-17）：ASR 出口（asr.transcribe）已压过一道，但**库里
    # 早于该修复入库的旧 text 不会被重转**——实测 final rewrite 一轮 229 段只重转
    # 18 段（仅 asr_final_rewrite 相关的），其余沿用旧脏 text，所以历史数据必须在
    # 建稿出口再压一道，否则「昨天那些重复语句」永远留着。
    # 放在 _line() 而不是调用方，是为了让**落库内容与判据同源**：sweeper 判
    # 「报告是否落后于库」走的也是 render_raw()，若一边压一边不压，判据会永远
    # 认为报告落后 → 每轮空转重跑。collapse_repeats 是纯函数且幂等，重复调用安全。
    return asr.collapse_repeats(f"[{ts}] {body}")


# 跨段重复的起始时间容差（毫秒）。手机端 VAD 偶发把同一语音输出两次时，
# 两段 start_offset 只差十几毫秒；正常连续说话不会这么近。
_CROSS_DUP_START_MS = 100


def _dedupe_overlapping(segs: list[dict]) -> list[dict]:
    """丢掉「同一语音被 VAD 重复输出」的冗余段（2026-10-21）。

    实测形态：后段 duration ≈ 前段 × 2 + 320ms（对应手机端合并公式
    prev + gap + samples），文本是前段的重复，`start_offset_ms` 只差十几毫秒。

    判据刻意保守——三个条件同时满足才算重复：
      ① 相邻两段 start 相差 ≤ 100ms；
      ② 两段文本都不为空；
      ③ 其中一段是另一段的子串（含完全相同）。
    只保留信息更全的那段（文本更长的），避免把「又补了一句」误删。
    """
    out: list[dict] = []
    for s in segs:
        if out:
            prev = out[-1]
            dt = abs(int(s["start_offset_ms"]) - int(prev["start_offset_ms"]))
            a = (prev.get("text") or "").strip()
            b = (s.get("text") or "").strip()
            if dt <= _CROSS_DUP_START_MS and a and b and (a in b or b in a):
                if len(b) > len(a):
                    out[-1] = s
                continue
        out.append(s)
    return out


def render_raw(date: str, tenant: str = tenants.DEFAULT_TENANT) -> str:
    """当天全部「人声且有文本」的段拼成的正文（含历史已处理段）。

    建稿条件就是 `is_speech and text`：非人声、以及已判定为人声但还没转写出
    文本的段都不进稿；`speaker_label` 只决定行首有没有 `[角色]` 前缀，
    不影响整行是否入稿。
    提成模块级是为了让「报告是否落后于库」也能用同一个公式算——
    两边必须同源，否则会假报落后、每轮空转。
    """
    segs = [s for s in store.segments_of_date(date, tenant)
            if s["is_speech"] and s["text"]]
    return "\n".join(_line(s) for s in _dedupe_overlapping(segs))


def run_for_date(date: str, with_llm: bool = True, final_pass: bool = False,
                 tenant: str = tenants.DEFAULT_TENANT) -> bool:
    """同步执行整条管线（per-tenant：读写该租户的库与目录）。返回 True = 全部成功。

    2026-09-04 改版：干净稿/总结在**增量轮也重算**（本地 4B 模型一轮十几秒，
    上传已是准实时小批量，代价可接受）——否则原始转写实时更新、干净稿/总结
    却停留在上一次日终，三版内容对不上，用户会认为「没有处理」。

    两级开关：
    - with_llm：是否跑 LLM 干净稿+总结（增量/日终都开）
    - final_pass：日终独占——Fun-ASR-Nano+热词全段重转 + 处理完删音频
      （重转是全天音频 O(n)，Nano 10x 实时要几分钟，只在 17:30 做一次）
    """
    if not config.is_valid_date(date):
        log.error("run_for_date rejected bad date: %r", date)
        return False
    segs = [s for s in store.segments_of_date(date, tenant) if not s["processed"]]
    # 只取前 _MAX_SEGS_PER_ROUND 段，让「转写→聚类→落库」尽快闭环一轮
    # （理由见模块级 _MAX_SEGS_PER_ROUND 的说明）。
    # ⚠️ 日终终稿轮**不能切**：那一轮要对当天全部人声段重转，切了会漏段。
    if not final_pass:
        segs = segs[:_MAX_SEGS_PER_ROUND]
    final_rewrite = final_pass and config.ASR_FINAL_REWRITE
    # 当天「已转写但没角色、且音频还在」的段。既决定要不要跳过整轮，也是 ② 的补标范围。
    #
    # 2026-09-14 实测漏洞：只凭 segs 判断会漏掉这类段，而它们恰恰出现在
    # 「那天崩过、之后又没有新段」的情形里。此时若在这里 return，② 永远执行不到，
    # 「崩溃后补角色」的补救就形同虚设 —— 更糟的是不收敛：
    # `dates_with_pending()` 一直把该日期判为待补，sweeper 每轮排队重跑，
    # 每次都在这里被跳过，既不补角色也不出队。
    # 音频已被终稿轮删掉的段（path 空或文件不在）不在此列：它们补不了，
    # 也不该让这一天被反复排队。
    roleless = [
        s for s in store.segments_of_date(date, tenant)
        if s["is_speech"] and not (s["speaker_label"] or "").strip()
        and (s["path"] or "").strip() and Path((s["path"] or "").strip()).is_file()
    ]
    if not segs and not final_rewrite and not roleless:
        # 没有新段、也没有待补角色：还要再看一眼「报告是否落后于库」，都正常才跳过。
        #
        # 2026-09-14 补：管线可能在 ② 之后、③ 落库之前被中断（崩溃，或用户
        # `kill -9` 重启服务）。此时段的 text 与 speaker_label 都已写好，
        # `segs`（processed=0）与 `roleless`（无角色）双双为空 —— 旧判据会直接跳过，
        # 而 sweeper 的 `dates_with_pending` 也不会把这一天列为待处理，日报就
        # **永久**停在被中断前的内容上（要等有新段才顺带修复）。
        # 今天 20:04 那一轮恰好就处在这个窗口里（20:04:48 完成聚类，20:13 仍未落库）。
        # 期望正文为空时一律不算「落后」——绝不冒清空已有日报的风险。
        expected = render_raw(date, tenant)
        if not expected or ((store.get_report(date, tenant) or {}).get("raw") or "") == expected:
            # 绝不能走到下面的重建逻辑——否则会用空内容覆盖已有日报
            # （实测踩坑：/api/done 被重复触发后日报被清空）。
            # 例外：日终重写开启时必须继续——增量轮已把全部段置 processed，
            # 日终轮的核心工作（终稿后端重转 + LLM）恰恰发生在「无新段」时，
            # 在这里 return 会让日报永远停留在增量后端的转写质量。
            log.info("no new segments for %s, skip (existing report preserved)", date)
            return True
        log.info("report for %s lags behind the segments (%d chars expected, no new "
                 "segment to transcribe); rebuild the report", date, len(expected))

    try:
        # ① 逐段：非人声检测 + ASR 转写
        #    音乐/噪声/静默 → is_speech=0，跳过转写（不进日报/语料）
        #    人声（本人+他人）→ 全部转写，说话人标签稍后由聚类统一标注
        speech: list[tuple[int, str, str]] = []   # (seg_id, path, text)
        for seg in segs:
            sid = seg["seg_id"]
            p = (seg["path"] or "").strip()
            src = Path(p) if p else None
            if src is None or not src.is_file():
                store.mark_segment_processed(sid, "", None, tenant=tenant)
                continue
            if not audio_tagging.is_speech(src):
                log.info("segment %d classified NON-SPEECH, skipped", sid)
                store.mark_segment_processed(sid, "", None, is_speech=0, speaker_label="", tenant=tenant)
                continue
            text = asr.transcribe(
                src,
                stage="final" if final_pass else "incremental",
            )
            text, fixed = llm.apply_industry_fixes(text)
            if fixed:
                log.info("segment %d industry fixes: %s", sid, ", ".join(fixed))
            # 语言守卫：识别成日/韩等其他语言的段不进日报（2026-09-04 用户要求
            # 只处理中文和英文）。whisper 兜底后仍含假名/谚文 → 按非人声同等处理。
            if asr.looks_non_zh_en(text):
                log.warning("segment %d transcribed as non zh/en (%r), excluded",
                            sid, text[:60])
                store.mark_segment_processed(sid, "", None, is_speech=0, speaker_label="")
                continue
            store.mark_segment_processed(sid, text, None, is_speech=1, speaker_label="", tenant=tenant)
            speech.append((sid, seg["path"], text))

        # ①.5 日终重转写：增量轮用快速后端（SenseVoice）先出稿，日终轮才把当天
        # 全部人声段用终稿后端（Fun-ASR-Nano + 热词）重转一遍——否则增量的
        # processed=1 会让日报永远停留在增量后端的转写质量。全天音频仅几分钟，
        # Nano 约 10x 实时，代价可接受。失败段（文本为空）保留增量结果不覆盖。
        # 2026-09-08 修正：此步必须只在 final_pass 跑。曾经条件写成 with_llm
        # （增量轮也是 True），导致每次增量触发都全段重转——47 段要 2.5 分钟、
        # 94 段要 5 分钟，加上全天 clean 的 LLM 时间，单轮增量拖到 5 分钟以上，
        # 这是「日报不及时/看起来卡住」的最大单一根因。
        if final_pass and config.ASR_FINAL_REWRITE:
            rewritten = 0
            dropped = 0
            for s in store.segments_of_date(date, tenant):
                if not s["is_speech"]:
                    continue
                # 历史数据里有 path 为空/为 '.' 的段（旧版本写入）：
                # Path('').exists() 与 Path('.').is_file() 都会把当前目录当有效文件，
                # wave.open 会直接抛 IsADirectoryError——必须用 is_file() 严格过滤。
                p = (s["path"] or "").strip()
                src = Path(p) if p else None
                if src is None or not src.is_file():
                    continue
                text = asr.transcribe(src, stage="final")
                if not text:
                    continue
                text, fixed = llm.apply_industry_fixes(text)
                if fixed:
                    log.info("final rewrite seg %d industry fixes: %s",
                             s["seg_id"], ", ".join(fixed))
                # 语言守卫：日终重转把中文误判成日/韩语（实测「もうすぐ ここを…」）
                # 时保留增量轮 SenseVoice 的中文结果，绝不能覆盖进日报。
                if asr.looks_non_zh_en(text):
                    # 逐段落 WARNING 会在实时采集里刷屏（实测 8 行/分钟），且与
                    # asr.py 里那条 retry 告警一比一成对。这里只计数，按批汇总。
                    dropped += 1
                    log.debug("final rewrite for seg %d produced non zh/en text, "
                              "keep incremental result", s["seg_id"])
                    continue
                store.mark_segment_processed(
                    s["seg_id"], text, s["is_user"],
                    is_speech=1, speaker_label=s["speaker_label"], tenant=tenant,
                )
                rewritten += 1
            if rewritten or dropped:
                log.info("final rewrite: %d segments re-transcribed for %s "
                         "(dropped %d non zh/en)", rewritten, date, dropped)

        # ② 说话人聚类：本人/甲乙丙丁（持久化说话人库，跨批/跨天身份一致）
        #
        # 范围 = 本轮新转写段 ∪ 当天仍无角色的段。
        #
        # 为什么要带上后者（2026-09-14 实测故障）：转写在 ① 里逐段把 processed 置 1、
        # 角色留空，聚类要到本步才做。一旦中间崩溃（当时是 CED 对 42.9s 长音频抛
        # Reshape），先转好的段就永久停在「processed=1 + 角色空」——sweeper 的
        # dates_with_pending 只找 processed=0，这天再也不会被重跑，手机端一直显示
        # 「待判定」。补齐后，只要这天还能跑一次，漏标的段就自动补上。
        to_label: dict[int, str] = {sid: p for sid, p, _ in speech}
        # 再补上当天「已转写但没角色」的段（roleless 在函数开头已算好，
        # 并已用 is_file() 挡掉 path 为空/为 '.' 的段——Path('') 与 Path('.')
        # 都会把当前目录当有效文件）。
        for s in roleless:
            to_label.setdefault(s["seg_id"], (s["path"] or "").strip())

        labels: dict[int, str] = {}
        if to_label:
            sids_order = list(to_label.keys())
            labels = voiceprint.cluster_speakers(
                [Path(to_label[i]) for i in sids_order],
                sids_order,
                tenant,
            )
            relabeled = 0
            for sid in sids_order:
                label = labels.get(sid, "")
                # 本人别名（2026-09-08）：昵称注册的具名锚点（如「大锅」）也是本人，
                # 否则本人发言会被标成 is_user=0、总结稿拆进他人栏。
                if label in config.SELF_ALIASES:
                    label = config.SELF_LABEL  # 写库时统一规范成「本人」
                is_user = 1 if label == config.SELF_LABEL else (0 if label else None)
                store.update_segment_speaker(sid, label, is_user, tenant)
                if label:
                    relabeled += 1
            log.info("speaker labeling: %d/%d segments got a role for %s",
                     relabeled, len(sids_order), date)

        # ③ 日报聚合：当天全部【人声】段（带说话人标注），含历史已处理段。
        #    任一次重跑/增量触发产出的都是完整日报，而不是只有本轮新段。
        #    公式见模块级 `render_raw()`——与「报告是否落后」判据共用同一份，
        #    两边同源才不会假报落后、每轮空转。
        #    必须在此刻重算：① 刚转写完、② 刚补完角色，早先那份不含它们。
        raw = render_raw(date, tenant)
        # 增量轮次沿用上一轮的干净稿/总结，本轮只刷新原始转写。
        # 不能清空重算：那会让用户在 17:30 之前看到的干净稿反复变成空白。
        prev = store.get_report(date, tenant)
        prev_clean = (prev["clean"] if prev else "") or ""
        prev_summary = (prev["summary"] if prev else "") or ""
        prev_summary_ts = float((prev or {}).get("summary_generated_at") or 0)

        # 原始稿先落库（2026-09-14）：raw 在 ② 之后就已就绪，而下面的全天干净稿
        # 才是单轮耗时的大头——实测一轮增量 11 分钟，干净稿 33 块里未命中缓存的
        # 约 6 块 × ~90s 就吃掉了 9 分钟。此前 raw 要等 LLM 全部算完（文末那次
        # upsert）才写库，用户在这 10 分钟里刷新手机看到的还是上一轮正文，
        # 观感就是「新讲的没处理」——而这期间原始稿其实早就好了。
        # 这里先按「沿用上一轮干净稿/总结」落一次（与下面 with_llm=False 分支
        # 同一策略，不破坏既有的三版一致性约定），LLM 算完后由文末的 upsert
        # 覆盖成最新三件套。
        # 安全性：upsert_report 不触碰 final_at；本行只在「本轮确实有事可做」
        # 的分支之后执行，不会碰到「跳过时走到重建逻辑、空内容覆盖日报」那个老坑。
        store.upsert_report(date, raw, prev_clean, prev_summary, ready=True,
                            summary_generated_at=prev_summary_ts, tenant=tenant)

        summary_ts = 0.0  # 本次落库的 summary_generated_at
        if not with_llm:
            log.info("incremental run for %s: ASR only, keep existing clean/summary", date)
            clean, summary = prev_clean, prev_summary
            summary_ts = prev_summary_ts
        elif llm.available() or llm.wait_ready(_LLM_WAIT_SEC):
            # 数量触发模式下 mlx 可能还没被拉起：先等它就绪（最多 _LLM_WAIT_SEC 秒）。
            # 不等就会落 raw-only 日报，且 pending 已清空、没人补算（2026-10-21 实测）。
            # 窗口模式 / 实时模式下 mlx 已常驻，available() 首次即过，无额外等待。
            # 兜底不可省：_chat 在超时/后端异常时返回空串，若直接采信，
            # 日报的干净稿会被写成空——原始转写还在这里，丢了就是永久丢失。
            # 但兜底必须留下 ERROR 日志：静默回落等于把「去口气失效」藏起来，
            # 用户只会看到干净稿和原始稿一模一样，却查不到原因（2026-09-03 修复）。
            clean = llm.clean_transcript(raw)
            if not clean.strip():
                log.error("clean_transcript returned EMPTY for %s (raw=%d chars); "
                          "falling back to raw. 排查：LLM 后端是否只返回 reasoning_content",
                          date, len(raw))
                clean = raw
            # 总结稿节流：要通读全天内容，4B 模型一轮要几分钟，没必要每段一算。
            # 攒 SUMMARY_MIN_INTERVAL 秒以上的新数据再重算（干净稿/原始稿照常刷新）。
            # 日终 final_pass 永远重算，且旧总结为空也必须先补上一次。
            due = final_pass or not prev_summary.strip() or \
                (time.time() - prev_summary_ts) >= config.SUMMARY_MIN_INTERVAL
            if due:
                summary = llm.daily_summary(raw)
                if summary.strip():
                    summary_ts = time.time()
                else:
                    log.error("daily_summary returned EMPTY for %s (raw=%d chars); "
                              "该日报将没有总结", date, len(raw))
                    # 绝不用空总结覆盖旧总结（实测 2026-09-08：服务重启窗口期
                    # mlx-lm 连接拒绝 → 空总结把 3164 字的好总结冲掉了）。
                    # 有旧总结就沿用，下一轮 due 时自然重算自愈。
                    if prev_summary.strip():
                        summary, summary_ts = prev_summary, prev_summary_ts
                        log.warning("kept previous summary for %s (%d chars) "
                                    "instead of overwriting with empty", date, len(prev_summary))
            else:
                summary = prev_summary
                summary_ts = prev_summary_ts
                log.info("summary throttled for %s (last generated %ds ago, interval %ds); "
                         "keep prev summary, clean refreshed to latest",
                         date, int(time.time() - prev_summary_ts),
                         config.SUMMARY_MIN_INTERVAL)
        else:
            log.warning("llm unavailable for %s; report keeps raw transcript only", date)
            clean = raw
            summary = prev_summary
            summary_ts = prev_summary_ts

        store.upsert_report(date, raw, clean, summary, ready=True,
                            summary_generated_at=summary_ts, tenant=tenant)
        _backup_text(date, raw, clean, summary, tenant)

        # ④ 语料导出：本轮「本人」人声段（GPT-SoVITS 训练本人音色）
        corpus_files = [
            (Path(p), text) for sid, p, text in speech
            if labels.get(sid) in config.SELF_ALIASES and text
        ]
        if corpus_files:
            _export_corpus(date, corpus_files, tenant)

        # ⑤ 全部成功 → 删除原始音频文件（仅日终轮）。
        # 增量轮就删的话，17:30 日终重写没有音频可用——终稿后端永远没机会重转。
        # 代价是音频保留到 17:30（一天约几十 MB），换终稿质量，值得。
        if final_pass:
            store.cleanup_audio_files(date, tenant)
            store.mark_final(date, tenant=tenant)
        log.info("pipeline for %s done: %d speech segments, report covers %d chars",
                 date, len(speech), len(raw))
        return True
    except Exception:
        log.exception("pipeline for %s crashed", date)
        return False


def _backup_text(date: str, raw: str, clean: str, summary: str,
                 tenant: str = tenants.DEFAULT_TENANT) -> None:
    """文字三件套双格式备份（JSON + Markdown）。per-tenant 目录。"""
    markers = store.markers_of_date(date, tenant)
    text_dir = tenants.resolve(tenant).text_dir
    payload = {"date": date, "raw_transcript": raw, "clean_transcript": clean,
               "summary": summary, "markers": markers,
               "generated_at": datetime.now().isoformat()}
    (text_dir / f"{date}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# 日报 {date}", "", "## 原始转写", raw, "", "## 去口气干净稿", clean,
          "", "## 总结", summary, ""]
    if markers:
        md += ["## 重要时刻", ""]
        md += [f"- {m['t']}{(' ' + m['note']) if m.get('note') else ''}" for m in markers]
        md.append("")
    (text_dir / f"{date}.md").write_text("\n".join(md), encoding="utf-8")


def _export_corpus(date: str, corpus_files: list[tuple[Path, str]],
                   tenant: str = tenants.DEFAULT_TENANT) -> None:
    """GPT-SoVITS 语料目录：wavs/ + list.wav.txt（格式：路径|说话人|语言|文本）。

    合并模式：保留既有条目，只追加本轮新段（增量触发时不能清掉先前导出的语料）。
    """
    out = tenants.resolve(tenant).corpus_dir / date
    (out / "wavs").mkdir(parents=True, exist_ok=True)
    list_file = out / "list.wav.txt"
    existing: dict[str, str] = {}
    if list_file.exists():
        for line in list_file.read_text(encoding="utf-8").splitlines():
            parts = line.split("|")
            if len(parts) == 4:
                key, spk, lang, txt = parts
                # 重复幻觉兜底（2026-09-17）：既有条目是在建稿修复**之前**写的，
                # 文本里带着 ASR 循环幻觉。这里顺带压一遍，历史条目下次导出即自愈；
                # 幂等，重复导出不会漂移。
                existing[key] = f"{key}|{spk}|{lang}|{asr.collapse_repeats(txt)}"
    new_names: set[str] = set()
    for src, text in corpus_files:
        shutil.copy2(src, out / "wavs" / src.name)
        key = f"wavs/{src.name}"
        existing[key] = f"{key}|user|zh|{text}"
        new_names.add(key)
    (list_file).write_text(
        "\n".join(existing[k] for k in sorted(existing)) + "\n", encoding="utf-8")
    # 文本样本 JSONL（全量重建，来自数据库归档；必须限定日期，否则会混入他日语料）
    store.export_corpus_jsonl(out / "corpus.jsonl", date, tenant=tenant)
