"""补跑扫描器（2026-09-08 新增）：日报处理不能只靠手机端事件。

背景（真实故障）：手机补传历史日期的音频后只给「当天」发 /api/done，服务端没有任何
兜底，于是 09-05 的 7 段、09-06 的 3 段永远停在未转写，09-06 连日报都没有；Mac 关机
重启也不会接着处理。用户要求：数据持久化、不受网络与关机影响，开机就该自动续跑。

职责（全部幂等，重复跑无害）：
  ① 启动补跑：服务起来后先把积压的未处理段按日期升序补跑增量（final=0）。
  ② 周期扫描：每 SWEEP_MIN 分钟扫一次未处理段，手机不发 done 也能兜住。
  ③ 日终兜底：到 DAILY_FINAL_TIME 由服务端自己对今天跑 final=1；历史日期
     若从没出过终稿（final_at=0）也补跑一次——关机错过 17:30 不再永久缺失。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from . import config, llm, pipeline, reclaim, store, tenants

log = logging.getLogger(__name__)

_STARTUP_DELAY_SEC = 8.0      # 等服务与 LLM 后端就绪再开跑
_TICK_POLL_SEC = 20.0         # 轮询间隔（决定是否到点/是否该扫）

# 窗口外的「跳过」日志每半小时最多一条：扫描 10 分钟一轮，窗口外整天都跳，
# 不节流的话这条 INFO 会刷满日志（窗口模式是常态运行方式，跳过不是异常）。
_SKIP_LOG_EVERY_SEC = 1800.0
_last_window_skip_log = 0.0


def _catchup_since() -> str:
    """补跑的起始日期（今天往前 CATCHUP_DAYS 天）。"""
    return (datetime.now() - timedelta(days=config.CATCHUP_DAYS)).strftime("%Y-%m-%d")


def _final_due(date: str) -> bool:
    """该日期是否已过日终时间（今天看时刻，历史日期一律算已过）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if date < today:
        return True
    try:
        hh, mm = config.DAILY_FINAL_TIME.split(":")[:2]
        due_at = datetime.now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        log.warning("invalid DH_DAILY_FINAL_TIME=%r, fallback 17:30", config.DAILY_FINAL_TIME)
        due_at = datetime.now().replace(hour=17, minute=30, second=0, microsecond=0)
    return datetime.now() >= due_at


_MAX_WAIT_SEC = 900.0   # 单个日期最长等待（15 分钟），超时交给下一轮


def _stale_report_dates(tenant: str) -> list[str]:
    """「段都已入库、日报却还停在旧内容」的日期（2026-09-14 补）。

    兜底场景：管线在 ②（聚类）之后、③（落库）之前被中断——崩溃，或用户
    `kill -9` 重启服务。此时段的 text 与 speaker_label 都已写好，而
    `dates_with_pending` 找的是 processed=0 / 没角色，**不会**把这一天算进来，
    于是没人再碰它，日报就永久停在旧内容上（要等有新段才顺带修复）。

    判据用 `pipeline.render_raw()`，与管线建稿共用同一份公式——两边同源才不会
    假报落后、每轮空转。只看最近 CATCHUP_DAYS 天：逐日重算一遍正文再比对，
    每天一次段表扫描（几百行），放在 10 分钟一轮的扫描里可以忽略。
    """
    today = datetime.now()
    out: list[str] = []
    for i in range(config.CATCHUP_DAYS + 1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        expected = pipeline.render_raw(d, tenant)
        stored = (store.get_report(d, tenant) or {}).get("raw") or ""
        if expected and stored != expected:
            out.append(d)
    return out


def _pending_dates(tenant: str) -> list[str]:
    return sorted(set(store.dates_with_pending(tenant))
                  | set(store.dates_without_report(tenant))
                  | set(_stale_report_dates(tenant)))


def _final_dates(tenant: str) -> list[str]:
    """终稿只补「已过日终时间且没出过终稿」的日期，且限最近 CATCHUP_DAYS 天。"""
    pending = set(store.dates_with_pending(tenant))
    return [d for d in store.dates_missing_final(_catchup_since(), tenant=tenant)
            if _final_due(d) and d not in pending]


async def sweep_async() -> int:
    """扫一轮：先补跑待处理段，再补缺的终稿。**严格串行**。

    串行不是保守，是踩出来的（2026-09-08）：一次把 4 个日期一起触发，
    mlx-lm 被并发请求打爆（RuntimeError: metal::malloc Resource limit exceeded），
    当天日报全线卡死。补跑是后台兜底，慢一点没关系，压垮 LLM 才是事故。
    """
    # 处理触发门槛（2026-09-27 窗口 / 2026-10-21 数量）：固定窗口与数量触发
    # 二者都没满足时整轮跳过——落库照常、处理延后，这是「服务常驻但大部分时间
    # 不烧 CPU」的另一半保障（另一半在 pipeline.trigger）。
    global _last_window_skip_log
    if not config.agg_active():
        now = asyncio.get_running_loop().time()
        if now - _last_window_skip_log >= _SKIP_LOG_EVERY_SEC:
            _last_window_skip_log = now
            log.info("sweep skipped: outside agg window %r (span %d min) and "
                     "batch(%d) not due; segments stay queued",
                     config.AGG_WINDOWS_RAW, config.AGG_SPAN_MIN,
                     config.AGG_BATCH_MIN)
        return 0

    if pipeline.any_running():
        log.info("sweep skipped: pipeline already running for %s",
                 sorted(pipeline._running) if hasattr(pipeline, "_running") else "?")
        return 0

    fired = 0
    # 多租户（2026-09-09）：逐租户串行扫描；租户之间也串行（_gate 兜底，
    # 但主动串行能把「等谁跑完」的日志语义保持清楚）。
    for t in tenants.all_tenants():
        # 窗口刚开时 mlx 可能还在加载（keepalive 30s 轮询 + 模型加载几十秒）。
        # 先等就绪再触发：否则第一轮会以「LLM 不可用」落 raw-only 日报，
        # pending 清空后没人补算，干净稿/总结要等下个窗口（实测教训的镜像场景）。
        if not await _llm_ready():
            log.warning("sweep: LLM not ready inside window, skip this tick")
            return 0
        for d in _pending_dates(t.id):
            if not config.is_valid_date(d):
                continue
            if pipeline.trigger(d, with_llm=True, final_pass=False, tenant=t.id):
                fired += 1
                log.info("catchup[%s]: incremental pipeline started for %s", t.id, d)
                await _wait_idle(d, t.id)
        for d in _final_dates(t.id):
            if pipeline.trigger(d, with_llm=True, final_pass=True, tenant=t.id):
                fired += 1
                log.info("catchup[%s]: final pass started for %s", t.id, d)
                await _wait_idle(d, t.id)
    # 音频回收（1.0.3）：每天至多一次，删已 final / 超龄 / 低信息密度的 wav。
    # 只删有转写文本的 wav，文本与语料 jsonl 永久保留——细节见 reclaim.py 顶部。
    for t in tenants.all_tenants():
        try:
            r = reclaim.maybe_sweep(t.id)
            if r:
                log.info("reclaim %s: 删 %d 个 wav / %.1f MB",
                         t.id, r.get("deleted_files", 0), r.get("freed_bytes", 0) / 1048576)
        except Exception:
            log.exception("reclaim %s failed", t.id)
    return fired


async def _llm_ready(timeout: float = 240.0) -> bool:
    """等 LLM 后端就绪（阻塞调用放线程池，别卡事件循环）。

    实时模式（窗口关闭）下 LLM 常驻，首次探测即过；窗口模式下 mlx 由
    keepalive.sh 按窗口拉起，这里最多等 timeout 秒。
    """
    try:
        return await asyncio.to_thread(llm.wait_ready, timeout)
    except Exception:
        log.exception("llm.wait_ready failed")
        return False


async def _wait_idle(date: str, tenant: str = tenants.DEFAULT_TENANT) -> None:
    """等这个日期跑完（或超时），再处理下一个。"""
    waited = 0.0
    while pipeline.is_running(date, tenant) and waited < _MAX_WAIT_SEC:
        await asyncio.sleep(3.0)
        waited += 3.0
    if waited >= _MAX_WAIT_SEC:
        log.warning("sweep: %s still running after %.0fs, leave it to next round",
                    date, waited)


def sweep_once() -> int:
    """同步封装：仅供测试/手工调用（真实运行走 sweep_async）。"""
    fired = 0
    for t in tenants.all_tenants():
        for d in _pending_dates(t.id):
            if config.is_valid_date(d) and pipeline.trigger(
                    d, with_llm=True, final_pass=False, tenant=t.id):
                fired += 1
        for d in _final_dates(t.id):
            if pipeline.trigger(d, with_llm=True, final_pass=True, tenant=t.id):
                fired += 1
    return fired


async def run_forever() -> None:
    """后台常驻：启动补跑一次 → 之后每 SWEEP_MIN 分钟扫一次。"""
    await asyncio.sleep(_STARTUP_DELAY_SEC)
    try:
        # 必须在事件循环里调用：pipeline.trigger 内部要 get_running_loop() 建任务，
        # 丢进 to_thread 会抛 "no running event loop"（2026-09-08 实踩）。
        # 扫描本身只有几条索引查询，毫秒级，不会卡住循环。
        n = await sweep_async()
        log.info("catchup sweep at startup fired %d pipeline(s)", n)
    except Exception:
        log.exception("catchup sweep at startup failed")

    if config.SWEEP_MIN <= 0:
        log.info("periodic sweep disabled (DH_SWEEP_MIN=0)")
        return

    interval = max(1, config.SWEEP_MIN) * 60.0
    next_at = asyncio.get_running_loop().time() + interval
    while True:
        await asyncio.sleep(min(_TICK_POLL_SEC, max(1.0, interval)))
        now = asyncio.get_running_loop().time()
        if now < next_at:
            continue
        next_at = now + interval
        try:
            n = await sweep_async()
            if n:
                log.info("periodic sweep fired %d pipeline(s)", n)
        except Exception:
            log.exception("periodic sweep failed")


def start() -> asyncio.Task | None:
    """在事件循环里启动扫描任务（lifespan 内调用）。"""
    try:
        task = asyncio.get_running_loop().create_task(run_forever())
    except RuntimeError:
        log.warning("sweeper not started: no running event loop")
        return None
    return task
