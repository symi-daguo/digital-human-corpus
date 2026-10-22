"""声纹标记：sherpa-onnx speaker embedding（3D-Speaker ECAPA ONNX）+ 余弦相似度。

比对注册声纹判定 is_user；未注册声纹或引擎未配置时返回 None（未判定）。
"""
from __future__ import annotations

import logging
import math
import struct
import wave
from pathlib import Path

import numpy as np

from . import config, store, tenants

log = logging.getLogger(__name__)

_extractor = None

# 声纹体检的判据（2026-09-10 实测标定，不是拍脑袋的数）：
# 本人自己的历史簇与注册锚点余弦落在 0.47~0.62，他人簇普遍 < 0.35，
# 但「超大簇」（如 670 段的「乙」）可能混了多个人，不能只看余弦。
_AUDIT_GRAY_BAND = 0.12      # 距阈值 0.12 以内算灰区，交给人判断

# 段数 ≥ 200 的簇很可能是多人混合，单独警示。
#
# ⚠️ 这个数同时被三处使用，其中两处**互相打架**（2026-09-21 实测绘出）：
#   ① `audit()` 的 `huge` 标记：cnt >= 200 → 标红（纯观测）
#   ② `_load_usable_clusters()`：cnt >= 200 → **摘出匹配池**（安全边界）
#   ③ `cluster_speakers()` 的漂移冻结：`n + 1 >= 200` → **停止 upsert**，于是
#      被冻结的簇 count 永久钉在 **199**，永远达不到 ② 的 200。
# 结果是：**冻结恰恰让 ② 对最该拦的那批簇变成死代码** —— 它们既不再更新质心，
# 又永远留在匹配池里当「谁都能命中」的吸附点。
#
# 2026-09-21 实测（default 租户 388 簇）：说话人27 / 说话人54 / 说话人338 /
# 说话人382 的 count 全 = 199（冻结态）。用「与其余全部簇的平均余弦（亲和度）」
# 判别谁是混合簇，全局 mean=0.2205 / 中位=0.2266：
#   说话人54  亲和度 0.4195 → 排名 **1/388**，当日内容只有「喂喂喂。」「具。」
#   说话人27  亲和度 0.3985 → 排名 **2/388**
#   说话人338 亲和度 0.2429 → 排名 146/388（离群 = 真·独立说话人，当日在授课）
#   说话人382 亲和度 0.1546 → 排名 333/388（离群 = 真·独立说话人）
# ⇒ **「体积大」不是「是混合簇」的好判据**：338/382 当日各承载 225 / 217 段、
#    却是纯说话人；27/54 是真混合簇，体积上反而看不出来。
# ⇒ 所以这里**故意不改成「按体积提前排除」**：那会把 338/382 这类真说话人一起
#    踢出匹配池，让它们的后续段裂成一堆新碎簇（库里已有 266 个单样本碎簇）。
#    正确的判据是「质心在空间中是否居中」（亲和度），需要单独一轮标定，
#    见 `.workbuddy/memory/pitfalls.md` P-91。
_AUDIT_HUGE_COUNT = 200      # 段数 ≥ 200 的簇很可能是多人混合，单独警示

# 实时对话（identify）的最短判定时长。
# 声纹 embedding 靠的是音色统计量，句子越短信息越少。2026-09-11 受控实测
# （把已确认本人的录音按时长截断，再算与本人锚点的 max 余弦，4 段平均）：
#   0.3s→0.483  0.5s→0.546  0.8s→0.610  1.2s→0.647  2.0s→0.711  3.0s→0.728
# 也就是说 0.8s 以下根本过不了 0.6 的具名阈值，判定会掉进第③步匿名簇，
# 而那里命中哪个簇基本是随机的 —— 表现为「嗯。」「哼。」被硬判成某个「他人」
# （实测出现过 说话人95 / 说话人66）。
# 所以极短句宁可返回「未判定」（None）也不硬判：talk 的门控是 lenient，
# None 会被放行，不影响正常对话；乱认一个名字则会误触门控且污染日报标注。
_IDENTIFY_MIN_SEC = 1.0

# ---------------------------------------------------------------- 注册样本
#
# 具名声纹的注册样本时长与朗读脚本。
#
# 为什么必须让用户「读固定的一段文字」而不是随便说话：注册样本要拿去做
# 锚点比对，embedding 的稳定性受内容影响很大 —— 同一段文字、同一个人的
# 声学特征才可比；各读各的文本时，样本间差异里混进了「内容不同」这一项，
# 锚点会偏。文本本身刻意覆盖日常对话高频的声韵母、数字与常用词。
#
# 30 秒是「够长」与「肯配合」的折中：更短则音色统计量不足（实测见上
# _IDENTIFY_MIN_SEC 的标定，秒级片段余弦值能差 0.2），更长则没人愿意读完。
#
# ⚠️ Android 采集端有一份同样的文本（app/.../data/Constants.kt 的
# ENROLL_SCRIPT）：那边必须能离线录音（注册样本走 EnrollQueue 补交，
# 断网时也要录），不能依赖服务端下发，所以本地必须留一份。
# **改这里就要同步改那边**，两处不一致会导致「手机录的样本」与
# 「电脑上传的样本」内容不同，锚点可比性下降。
ENROLL_SECONDS = 30

ENROLL_SCRIPT = (
    "今天是九月四号，星期四。我现在读一段固定的文字，用来登记我的声音。\n"
    "早晨出门的时候，天有点阴，风从窗口吹进来，桌上的纸轻轻翻动。\n"
    "我喜欢在安静的房间里工作，偶尔喝一口温水，看看窗外的树。\n"
    "下午要和同事讨论设备的安装位置，先准备好工具，再确认一遍清单。\n"
    "这段话不长，但包含了日常说话里常用的字词，读完刚好三十秒。"
)


def _get_extractor():
    global _extractor
    if _extractor is not None:
        return _extractor
    if not config.SPEAKER_MODEL or not Path(config.SPEAKER_MODEL).exists():
        log.warning("speaker model not configured; voiceprint disabled")
        return None
    from sherpa_onnx import SpeakerEmbeddingExtractor, SpeakerEmbeddingExtractorConfig

    cfg = SpeakerEmbeddingExtractorConfig(
        model=config.SPEAKER_MODEL, num_threads=1, provider="cpu"
    )
    _extractor = SpeakerEmbeddingExtractor(cfg)
    return _extractor


def _embed(wav_path: Path) -> list[float] | None:
    """提取一段音频的说话人 embedding。失败一律返回 None（绝不抛异常）。

    这里刻意不用 assert：cluster_speakers 会对整批几十段循环调用本函数，
    任何一段格式异常（非 16k、非单声道、文件损坏、时长为 0）若抛 AssertionError
    就会让整批管线失败并触发全量重试。单段失败应当只影响该段，降级为「未判定」。
    """
    ex = _get_extractor()
    if ex is None:
        return None
    try:
        with wave.open(str(wav_path), "rb") as w:
            rate, ch, nframes = w.getframerate(), w.getnchannels(), w.getnframes()
            if rate != 16000 or ch != 1:
                log.warning("skip %s: expect 16kHz mono, got %dHz %dch",
                            wav_path.name, rate, ch)
                return None
            if nframes < 1600:      # < 0.1s：embedding 无意义
                log.warning("skip %s: too short (%d frames)", wav_path.name, nframes)
                return None
            frames = w.readframes(nframes)
        n = len(frames) // 2
        samples = struct.unpack(f"<{n}h", frames[: n * 2])
        stream = ex.create_stream()
        stream.accept_waveform(16000, [s / 32768.0 for s in samples])
        stream.input_finished()
        v = ex.compute(stream)
    except Exception as e:
        log.warning("embedding failed for %s: %s", wav_path.name, e)
        return None
    return list(v) if v else None


def register_user(wav_path: Path) -> bool:
    """配对：用一段本人录音注册声纹，并同步覆盖说话人库中的「本人」centroid。

    只写 voiceprint 表是不够的：未配对时聚类已经把「第一个出现的人」写成了
    speaker.本人。若注册不覆盖它，历史标签会一直是错的那个人，且配对形同无效。
    """
    return register_named(config.SELF_LABEL, wav_path)


def register_named(name: str, wav_path: Path, tenant: str = tenants.DEFAULT_TENANT) -> bool:
    """注册一个具名角色声纹（本人 / 张三 / 李四…）。per-tenant：写入该租户自己的声纹库。

    三处写入缺一不可：
    1) named_speaker：具名锚点表，聚类时优先级最高，命中即直接用该名字；
    2) speaker 表同名 centroid：保证走聚类路径时也能命中同一个标签，
       而不是新建一个「说话人N」把同一个人分成两摊；
    3) 名字等于本人时额外写 voiceprint：老逻辑以 voiceprint 行作为本人锚点。

    2026-09-10 增补：还要写 `named_anchor`（source=`register`）。
    重注册时**只覆盖 source='register' 那一条**，归并进来的历史锚点必须保留 ——
    那些是用户逐条确认过的证据，重录一次声纹就丢掉等于把判定质量打回原形。
    """
    name = (name or "").strip()
    if not name:
        return False
    emb = _embed(wav_path)
    if emb is None:
        return False
    blob = struct.pack(f"{len(emb)}f", *emb)
    store.upsert_named_speaker(name, blob, tenant)
    store.replace_speaker_embedding(name, blob, tenant)
    store.clear_named_anchors(name, "register", tenant)
    store.add_named_anchor(name, blob, "register", 1, tenant)
    if name == config.SELF_LABEL:
        store.set_voiceprint(blob, tenant)
    return True


def _load_named_anchors(tenant: str) -> dict[str, list[list[float]]]:
    """具名角色的全部锚点：`{角色名: [embedding, ...]}`。

    2026-09-10 由单锚点升级为多锚点。理由（实测数据，非推断）：同一人的
    手机近场与远场录音是两套声学特征，本人自己的历史簇与注册锚点余弦只有
    0.55~0.62 —— 刚好卡在 0.6 阈值上下，单锚点必然时而判得出、时而判不出。

    多锚点取 **max** 而不是平均：平均会把两套特征糊成一个「谁都不像」的中间点，
    反而更容易漏判。

    老库自愈：`named_anchor` 为空时自动从 `named_speaker` 迁移一次（幂等）。
    """
    try:
        rows = store.list_named_anchors(tenant)
        if not rows:
            moved = store.seed_named_anchors(tenant)
            if moved:
                log.info("声纹锚点升级：named_speaker → named_anchor 迁移 %d 条", moved)
            rows = store.list_named_anchors(tenant)
    except Exception as e:
        log.warning("读取具名锚点失败：%s", e)
        return {}
    out: dict[str, list[list[float]]] = {}
    for r in rows:
        raw = r["embedding"]
        try:
            v = list(struct.unpack(f"{len(raw) // 4}f", raw))
        except Exception as e:
            log.warning("锚点 %s#%s 损坏：%s", r.get("name"), r.get("id"), e)
            continue
        out.setdefault(r["name"], []).append(v)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _load_usable_clusters(tenant: str) -> list[tuple[str, np.ndarray, int]]:
    """加载**允许参与匹配**的匿名簇，返回 [(label, centroid, count)]。

    体积异常大的簇（>= _AUDIT_HUGE_COUNT）很可能是多个人混在一起：它的质心被大量
    异质样本平均后，会漂成「谁都能过阈值」的中间态，然后不断吸收新段，把正确判定吞掉。
    2026-09-11 实测：「乙」累计 775 段、跨 9 天 30 个 session，当天下午吞掉本人 86%；
    实时对话链路也把单句命中成「乙」。

    所以 `cluster_speakers` 与 `identify` **两条链路都必须摘掉它们**，只在一条上
    加保护会留下「另一条继续吞」的缺口——这个函数就是那条共享的安全边界。

    ⚠️ **这条边界只拦得住「长胖到 200」的簇，拦不住「被冻结在 199」的簇**
    （2026-09-21 实测）：漂移冻结发生在 `cluster_speakers()` 的 `n + 1 >= 200`
    分支，它**不 upsert**，所以 count 永远停 199 < 200 —— 冻结当天起，
    这个簇就变成了「质心不再更新、却永久留在匹配池」的吸附点，
    正是本函数想防的东西。default 租户实测：说话人27 / 54 / 338 / 382 全在 199。
    **但不要用「提前到 199 就摘」来补**：338/382 是当日各承载 225/217 段的
    真·独立说话人（亲和度排名 146/333，离群），摘掉它们只会让后续段裂成新碎簇。
    判「是不是混合簇」要看质心在空间里是否居中，见 `_AUDIT_HUGE_COUNT` 处的实测。
    """
    out: list[tuple[str, np.ndarray, int]] = []
    for sp in store.list_speakers(tenant):
        cnt = int(sp["count"] or 0)
        if cnt >= _AUDIT_HUGE_COUNT:
            log.warning("跳过体积异常簇 %s（%d 段，疑似多人混合，不参与匹配）",
                        sp["label"], cnt)
            continue
        raw = sp["embedding"]
        try:
            emb = np.asarray(struct.unpack(f"{len(raw) // 4}f", raw), dtype=np.float32)
        except Exception as e:
            log.warning("skip corrupt speaker '%s': %s", sp["label"], e)
            continue
        out.append((sp["label"], emb, cnt))
    return out


_MIXTURE_TOP_N = 5           # 每轮提示最居中的几个簇（混合簇候选）
_MIXTURE_MIN_CLUSTERS = 30   # 簇太少时统计不可靠，不提示

# 「质心已冻结」提示的去重集合，键 = (tenant, 簇名)。
# 用**模块级**而不是每次调用局部：冻结后 cnt 恒停在 _AUDIT_HUGE_COUNT-1，
# 于是 `n + 1 >= 上限` 对每一个命中该簇的段、每一轮都成立；局部去重只能压到
# 「每轮每簇一条」，而这条信息只在刚冻结那一刻有意义。详见 cluster_speakers
# 里冻结分支的注释。
_frozen_warned: set[tuple[str, str]] = set()


def _log_mixture_candidates(clusters: list[tuple[str, np.ndarray, int]]) -> None:
    """把「质心最居中」的若干簇打成一行 INFO，供人工归并。

    为什么需要（2026-09-21）：`_load_usable_clusters()` 的「cnt >= 200 就摘出
    匹配池」这条边界，对**被漂移冻结在 199 的簇**完全失效（见 `_AUDIT_HUGE_COUNT`
    处的实测），而「体积大」本身也不是「是混合簇」的好判据。于是真混合簇会长期
    静默地留在匹配池里吸附各家的话音，从日志上完全看不出来。

    判据：混合簇的质心是多个人的平均，所以它与**其余所有簇**的平均余弦
    （下称亲和度）会显著高于全局。2026-09-21 实测 388 簇、全局 mean=0.2205：
      说话人54  0.4195（排名   1/388）→ 真混合簇（当日内容仅「喂喂喂。」）
      说话人27  0.3985（排名   2/388）→ 真混合簇
      说话人338 0.2429（排名 146/388）→ 纯说话人（当日授课 225 段）
      说话人382 0.1546（排名 333/388）→ 纯说话人（当日 217 段）
    四个簇的库内 count 都是冻结态 199，**只看体积分不出这两组，亲和度能分出**。

    **只读**：不写库、不改任何判定，纯粹让这个问题可观测。任何异常都只吞掉
    这一行日志，绝不允许拖垮管线。
    """
    if len(clusters) < _MIXTURE_MIN_CLUSTERS:
        return
    try:
        mat = np.stack([emb for _label, emb, _cnt in clusters]).astype(np.float32)
        norm = np.linalg.norm(mat, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        unit = mat / norm
        sim = unit @ unit.T
        np.fill_diagonal(sim, np.nan)
        aff = np.nanmean(sim, axis=1)
    except Exception as e:
        log.warning("混合簇体检失败（已跳过，不影响判定）：%s", e)
        return
    if not np.all(np.isfinite(aff)):
        return
    order = np.argsort(-aff)[:_MIXTURE_TOP_N]
    top = "、".join(f"{clusters[i][0]}({aff[i]:.4f}/库内{clusters[i][2]}段)"
                    for i in order)
    log.info("混合簇体检：%d 簇，全局平均亲和度 %.4f；最居中 = %s"
             "（居中说明质心是多人的平均，最可能是混合簇；"
             "要归并请在声纹审计页操作，本行不改任何判定）",
             len(clusters), float(np.nanmean(aff)), top)


def cluster_speakers(wav_paths: list[Path], seg_ids: list[int],
                     tenant: str = tenants.DEFAULT_TENANT) -> dict[int, str]:
    """对一组音频段做说话人聚类，返回 {seg_id: 标签} 映射。per-tenant：读写该租户自己的声纹库。

    两级判定（2026-09-04 引入具名角色）：
    ① **具名锚点优先**：先与 named_speaker 里用户注册的每个角色逐一比对余弦，
       最高分且过阈值就直接用该角色名（本人/张三/李四…）。工作环境里大部分
       对话者都能被注册掉，甲乙丙丁这类匿名标签自然就消失了。
    ② **聚类兜底**：具名锚点都没命中，才走 sherpa-onnx 增量贪心聚类，
       按甲乙丙丁顺序命名，并持久化到 speaker 表保持跨批/跨天身份一致。
    - 引擎不可用/单段提取失败：对应 seg_id 返回空串（未判定）。
    """
    ex = _get_extractor()
    if ex is None:
        log.warning("speaker embedding engine unavailable; cannot cluster")
        return {sid: "" for sid in seg_ids}

    from sherpa_onnx import SpeakerEmbeddingManager

    manager = SpeakerEmbeddingManager(ex.dim)
    centroid: dict[str, tuple[np.ndarray, int]] = {}   # label -> (centroid, count)

    # 加载持久化说话人库（历史 centroid）
    # 体积异常的簇见 _load_usable_clusters 的说明：摘出判定链路，不参与匹配，
    # 也不再更新质心。
    _usable = _load_usable_clusters(tenant)
    for _label, _emb, _cnt in _usable:
        manager.add(_label, _emb)
        centroid[_label] = (_emb, _cnt)
    # 混合簇体检：每轮一行 INFO，让「质心居中的吸附簇」可观测。
    # **纯只读**，不参与下面的任何判定（判据与实测见该函数说明）。
    _log_mixture_candidates(_usable)

    # 注册声纹覆盖「本人」锚点
    ref_raw = store.get_voiceprint(tenant)
    if ref_raw is not None:
        try:
            ref_emb = np.asarray(struct.unpack(f"{len(ref_raw) // 4}f", ref_raw), dtype=np.float32)
            manager.add("本人", ref_emb)
            centroid["本人"] = (ref_emb, 1)
        except Exception as e:
            log.warning("failed to load registered voiceprint: %s", e)

    # 具名角色锚点（用户显式注册 + 用户确认过的历史归并，多锚点）
    anchors = _load_named_anchors(tenant)
    named_labels = set(anchors)

    # 库里**全部**标签，含被 _load_usable_clusters 摘掉的超大簇。
    # 新簇命名必须避开它们，理由见 _next_label 的说明。
    _taken_labels: set[str] = {sp["label"] for sp in store.list_speakers(tenant)}

    def _match_named(arr: np.ndarray) -> str:
        """具名锚点匹配：**每个角色先对全部锚点取最高分**，再在角色间取最高。
        返回过阈值的角色名，否则空串。"""
        vals = [float(x) for x in arr]
        best, best_sim = "", -1.0
        for name, embs in anchors.items():
            sim = max((_cosine(vals, e) for e in embs), default=-1.0)
            if sim > best_sim:
                best, best_sim = name, sim
        return best if best_sim >= config.NAMED_VOICEPRINT_THRESHOLD else ""

    def _next_label() -> str:
        """给新簇取一个**确实未占用**的名字。

        ⚠️ 不能用 `len(centroid) + 1` 生成编号（2026-09-21 修）：
        超大簇会被 `_load_usable_clusters` 摘掉、不进 `centroid`，但它的编号
        仍然占着 —— 此时 `len(centroid) + 1` 可能恰好等于那个已存在大簇的编号，
        而调用处紧接着就是 `store.upsert_speaker(name, arr, 1)`，会把这条已有
        记录**整条覆盖**：质心退回单样本（噪声极大、阈值判定失准）、count 退回 1，
        该簇从此反复被重建。改成「已用最大编号 + 1」，编号单调递增、永不撞名。
        """
        for lab in [config.SELF_LABEL] + list(config.SPEAKER_OTHER_LABELS):
            if lab not in centroid and lab not in _taken_labels:
                _taken_labels.add(lab)
                return lab
        mx = 0
        for lab in _taken_labels | set(centroid):
            if lab.startswith("说话人"):
                try:
                    mx = max(mx, int(lab[len("说话人"):]))
                except ValueError:
                    pass
        name = f"说话人{mx + 1}"
        _taken_labels.add(name)
        return name

    result: dict[int, str] = {}
    for sid, path in zip(seg_ids, wav_paths):
        emb = _embed(path)
        if emb is None:
            result[sid] = ""
            continue
        arr = np.asarray(emb, dtype=np.float32)
        # ① 具名角色优先（注册过的同事/本人 → 直接给名字）
        name = _match_named(arr)
        if name:
            result[sid] = name
            continue
        # ② 具名未命中 → 走聚类库
        name = manager.search(arr, threshold=config.VOICEPRINT_THRESHOLD)
        if not name:
            name = _next_label()
            manager.add(name, arr)
            centroid[name] = (arr, 1)
            store.upsert_speaker(name, struct.pack(f"{len(emb)}f", *emb), 1, tenant)
        else:
            # 命中已有簇：滑动平均刷新 centroid。否则 centroid 永远停留在
            # 该簇的第一个样本（往往只有 2 秒、噪声极大），同一人后续段
            # 会不断低于阈值被拆成新簇（实测一天裂出 57 个碎簇）。
            # 「本人」与全部具名角色除外：注册锚点必须保持不动，不能被日常段漂移。
            # （具名角色同时也写进了 speaker 聚类库，走本分支时同样不能动它的 centroid）
            if name != config.SELF_LABEL and name not in named_labels:
                c, n = centroid[name]
                if n + 1 >= _AUDIT_HUGE_COUNT:
                    # 已到体积上限：冻结质心，不再继续漂移。再漂下去它就会变成
                    # 「谁都能过阈值」的黑洞簇（见上方加载处说明）。
                    #
                    # 提示按「**每进程每簇一次**」去重（2026-09-21 二次修）：
                    # 冻结后 count 恒停在 _AUDIT_HUGE_COUNT-1（实测说话人27/54/
                    # 338/382 都是 199），所以 `n + 1 >= 上限` 对**每一个命中该簇
                    # 的段、每一轮**都成立。先按「每轮每簇一次」去重只是把刷屏
                    # 从「每段一条」压到「每轮每簇一条」，仍然会每轮重复；
                    # 而该信息只在「刚冻结那一刻」有意义 —— 改成模块级集合，
                    # 每簇一辈子只提示一次。后续的可见性由 `_log_mixture_candidates`
                    # 每轮那一行负责（它才带得出「是不是混合簇」的判断依据）。
                    #
                    # ⚠️ 冻结**不会**让该簇退出匹配池：摘出匹配池的判据是
                    # `cnt >= _AUDIT_HUGE_COUNT`，而冻结把 cnt 钉在 199。
                    # 所以这条提示必须说清「它还在吸附」，否则会被读成「已隔离」。
                    key = (tenant, name)
                    if key not in _frozen_warned:
                        _frozen_warned.add(key)
                        log.warning(
                            "簇 %s 已达 %d 段上限，质心冻结不再漂移；"
                            "⚠️ 但它仍留在匹配池里继续吸附（摘出边界按 cnt>=%d 判，"
                            "冻结把 cnt 钉在 %d，永远够不到）—— 是否为混合簇请见"
                            "「混合簇体检」那一行",
                            name, _AUDIT_HUGE_COUNT, _AUDIT_HUGE_COUNT,
                            _AUDIT_HUGE_COUNT - 1)
                else:
                    new_c = (c * n + arr) / (n + 1)
                    norm = float(np.linalg.norm(new_c))
                    if norm > 0:
                        new_c = (new_c / norm).astype(np.float32)
                    centroid[name] = (new_c, n + 1)
                    try:
                        manager.remove(name)
                        manager.add(name, new_c)
                    except Exception as e:
                        log.warning("refresh centroid for %s failed: %s", name, e)
                    store.upsert_speaker(
                        name, struct.pack(f"{len(new_c)}f", *[float(x) for x in new_c]),
                        n + 1, tenant)
        result[sid] = name

    return result


def _wav_seconds(wav_path: Path) -> float:
    """WAV 时长（秒）。读不了返回 -1，交给调用方按「未知」处理。"""
    import wave

    try:
        with wave.open(str(wav_path)) as f:
            rate = f.getframerate()
            return f.getnframes() / rate if rate > 0 else -1.0
    except Exception:
        return -1.0


def identify(wav_path: Path, tenant: str = tenants.DEFAULT_TENANT) -> tuple[bool | None, str]:
    """单段说话人判定（实时对话用，2026-09-10）。

    返回 `(is_user, label)`：
    - `is_user`：True 本人 / False 他人 / None 未判定（引擎不可用或音频不合格）
    - `label`：具名角色名或已有簇标签；未判定时为空串

    与 cluster_speakers 的区别：**本函数绝不新建簇、绝不写库**。
    对话是一问一答的短促场景，每句话都往 speaker 表里塞一个新簇，
    一天下来会把聚类库污染成几百个碎簇（这正是 cluster_speakers 注释里
    提到的「一天裂出 57 个碎簇」问题的放大版）。所以这里只做**只读匹配**：
    命中具名锚点或已注册的本人声纹就返回，命中不了就老实说未判定。

    说话人判定失败**不影响对话**——只是 is_user 为 None，talk.py 照常转写与调用。
    """
    if not config.SPEAKER_MODEL:
        return None, ""
    emb = _embed(wav_path)
    if emb is None:
        return None, ""

    # ① 本人锚点：voiceprint 表（register_user 写入）与具名锚点里叫「本人」的
    try:
        ref_raw = store.get_voiceprint(tenant)
    except Exception:
        ref_raw = None
    if ref_raw is not None:
        try:
            ref = list(struct.unpack(f"{len(ref_raw) // 4}f", ref_raw))
            if _cosine(emb, ref) >= config.NAMED_VOICEPRINT_THRESHOLD:
                return True, config.SELF_LABEL
        except Exception as e:
            log.warning("本人声纹比对失败：%s", e)

    # ② 具名角色锚点（多锚点：每个角色取与全部锚点的最高分，角色间再取最高）
    anchors = _load_named_anchors(tenant)
    best, best_sim = "", -1.0
    for name, embs in anchors.items():
        sim = max((_cosine(emb, e) for e in embs), default=-1.0)
        if sim > best_sim:
            best, best_sim = name, sim
    if best and best_sim >= config.NAMED_VOICEPRINT_THRESHOLD:
        # 具名锚点命中：名字属于 SELF_ALIASES（本人用昵称「大锅」注册的）也算本人
        return (best in config.SELF_ALIASES), best

    # ③ 已有匿名簇：只查不建。查不到就未判定。
    #    与批量链路共用同一份安全边界（见 _load_usable_clusters）：实时对话是
    #    一问一答的短音频，信噪比本就比成段录音差，再放进一个「谁都能过阈值」的
    #    黑洞簇，等于必然误判成他人（2026-09-11 实测命中过「乙」）。
    #
    #    极短句额外加一道门槛：0.8s 以下与本人锚点的余弦实测只有 0.48~0.61
    #    （见 _IDENTIFY_MIN_SEC），具名匹配必然不过 → 掉到这里 → 随机命中一个簇
    #    并「自信地」报成他人。与其乱认，不如老实说未判定。
    if _wav_seconds(wav_path) < _IDENTIFY_MIN_SEC:
        log.info("identify: %s 时长不足 %.1fs，不判定说话人", wav_path.name,
                 _IDENTIFY_MIN_SEC)
        return None, ""
    ex = _get_extractor()
    if ex is not None:
        try:
            from sherpa_onnx import SpeakerEmbeddingManager

            manager = SpeakerEmbeddingManager(ex.dim)
            for label, arr, _cnt in _load_usable_clusters(tenant):
                manager.add(label, arr)
            hit = manager.search(np.asarray(emb, dtype=np.float32),
                                 threshold=config.VOICEPRINT_THRESHOLD)
            if hit:
                return (hit in config.SELF_ALIASES), hit
        except Exception as e:
            log.warning("匿名簇匹配失败：%s", e)

    return None, ""


# --------------------------------------------------------------------------
# 声纹体检与归并（2026-09-10）
#
# 为什么需要人工确认：`identify()` 第③步命中匿名簇后返回 `hit in SELF_ALIASES`。
# 「说话人18」不在 SELF_ALIASES 里 → is_user=False（判为**他人**）。但实测
# 说话人18 与本人锚点余弦 0.592、与「大锅」0.578 —— 它就是本人。这就是误判源头。
#
# 修法不是降阈值（会把客人也放进来），而是把「本人自己的历史大簇」并入具名角色。
# 归并动作**必须由用户确认**：把客人误并进「大锅」，声纹门控就会放客人进来。
# --------------------------------------------------------------------------
def audit(tenant: str = tenants.DEFAULT_TENANT) -> dict:
    """只读声纹体检：算出每个持久簇与每个锚点的余弦，给出归并建议。

    **绝不修改任何数据**，用户看完再决定归并哪些。

    ## 为什么建议要看「可信锚点」而不是「全部锚点的最高分」

    实测（2026-09-10）：把「乙」（670 段）当锚点加进去后，与本人无关的
    「说话人86」余弦会从 0.108 飙到 0.473 —— 说明「乙」是**混了多个人的超大簇**，
    它的 centroid 落在人群中间，谁看它都「有点像」。
    若建议按「全部锚点取 max」算，就会形成正反馈：并一个脏簇 → 建议更多簇 →
    越并越脏，最后门控形同虚设。

    所以建议只依据**最可信的锚点**（用户亲手注册的 + voiceprint 本人锚点），
    并单列 `borderline` 灰区交给人判断。
    """
    named = _load_named_anchors(tenant)

    # 可信锚点：注册锚点（source='register'）+ voiceprint 本人锚点
    trusted: dict[str, list[list[float]]] = {}
    register_dim = 0
    for r in store.list_named_anchors(tenant):
        if (r.get("source") or "") != "register":
            continue
        try:
            v = list(struct.unpack(f"{len(r['embedding']) // 4}f", r["embedding"]))
        except Exception:
            continue
        trusted.setdefault(r["name"], []).append(v)
        register_dim += 1
    try:
        raw = store.get_voiceprint(tenant)
        if raw:
            trusted.setdefault(config.SELF_LABEL, []).append(
                list(struct.unpack(f"{len(raw) // 4}f", raw)))
    except Exception as e:
        log.warning("读取 voiceprint 锚点失败：%s", e)

    anchors_all = [{"name": r["name"], "source": r.get("source", ""),
                    "count": r.get("count", 1), "dim": len(r["embedding"]) // 4}
                   for r in store.list_named_anchors(tenant)]

    thr = config.NAMED_VOICEPRINT_THRESHOLD
    gray = max(0.0, thr - _AUDIT_GRAY_BAND)

    clusters: list[dict] = []
    suggestions: list[dict] = []
    borderline: list[dict] = []
    for sp in store.list_speakers(tenant):
        raw = sp["embedding"]
        try:
            v = list(struct.unpack(f"{len(raw) // 4}f", raw))
        except Exception as e:
            log.warning("簇 %s 损坏：%s", sp["label"], e)
            continue
        sims_all = {name: max((_cosine(v, e) for e in embs), default=-1.0)
                    for name, embs in named.items()}
        sims_trusted = {name: max((_cosine(v, e) for e in embs), default=-1.0)
                        for name, embs in trusted.items()}
        b_all = max(sims_all.items(), key=lambda kv: kv[1]) if sims_all else ("", -1.0)
        b_tru = max(sims_trusted.items(), key=lambda kv: kv[1]) if sims_trusted else ("", -1.0)
        # 角色名自身也在 speaker 表里（register_named 会写同名 centroid 以防聚类拆成两摊）。
        # 它不该出现在「建议归并」里 —— 自己并自己是空操作，只会干扰判断。
        is_role_self = (sp["label"] in named) or (sp["label"] in config.SELF_ALIASES)
        item = {
            "label": sp["label"],
            "count": int(sp["count"]),
            "is_self_alias": sp["label"] in config.SELF_ALIASES,
            "is_role_self": is_role_self,
            # 体积异常大的簇很可能是多个人混在一起，单独标出来提醒
            "huge": int(sp["count"]) >= _AUDIT_HUGE_COUNT,
            "sims": {k: round(v2, 4) for k, v2 in sims_all.items()},
            "trusted_name": b_tru[0],
            "trusted_sim": round(b_tru[1], 4),
            "now_name": b_all[0],          # 当前会被判成谁
            "now_sim": round(b_all[1], 4),
            "suggest": (not is_role_self) and b_tru[1] >= thr,
            "borderline": (not is_role_self) and b_tru[1] < thr and b_tru[1] >= gray,
        }
        clusters.append(item)
        if item["suggest"]:
            suggestions.append(item)
        elif item["borderline"]:
            borderline.append(item)

    clusters.sort(key=lambda x: (-x["trusted_sim"], -x["count"]))
    suggestions.sort(key=lambda x: (-x["trusted_sim"], -x["count"]))
    borderline.sort(key=lambda x: (-x["trusted_sim"], -x["count"]))
    return {
        "threshold": thr,
        "cluster_threshold": config.VOICEPRINT_THRESHOLD,
        "gray_band": _AUDIT_GRAY_BAND,
        "huge_count": _AUDIT_HUGE_COUNT,
        "self_aliases": sorted(config.SELF_ALIASES),
        "trusted_anchors": sorted(trusted),
        "anchors": anchors_all,
        "cluster_total": len(clusters),
        "clusters": clusters,
        "suggestions": suggestions,
        "borderline": borderline,
    }


def merge_cluster(label: str, name: str,
                  tenant: str = tenants.DEFAULT_TENANT) -> dict:
    """把匿名簇 `label` 归并进具名角色 `name`（用户确认后调用）。

    `is_user` 由 `SELF_ALIASES` 决定，**不由调用方传入** —— 能否触发本人的管家
    是安全判定，不能让上层随手给一个 True。
    """
    label = (label or "").strip()
    name = (name or "").strip()
    if not label or not name:
        return {"ok": False, "reason": "empty_argument"}
    if name not in _load_named_anchors(tenant):
        return {"ok": False, "reason": "role_not_found", "name": name}
    res = store.merge_speaker_into_named(label, name, name in config.SELF_ALIASES, tenant)
    if res.get("ok"):
        log.info("声纹归并：簇 %r（%d 段）→ 角色 %r，改写历史段 %d 条",
                 label, res.get("count", 0), name, res.get("relabeled_segments", 0))
    return res
