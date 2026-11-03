"""核心逻辑回归测试（stdlib unittest，无第三方依赖）。

只测「一旦坏了会静默丢数据或算错」的点，不追求覆盖率：

1. `store.upsert_segment` 重传时必须保留已有转写
   —— 2026-10-23 事故：重传 419 段后 263 段 text 被清，日报 23404→7938 字。
2. `insights.is_low_value` 的判定边界
   —— reclaim 用它决定「口气类音频」能否提前到 3 天回收；
      判错会误删有效音频，或让无效音频占满硬盘。
3. `redact` 的掩码不留原值
   —— 隐私样本会进入 /summary 与手机洞察卡（会被截屏外发）。

运行（用 bundle 自带解释器，py3.13）：
    PYTHONPATH=server /Applications/数字人服务.app/Contents/Resources/python/bin/python3 \
        -m unittest discover -s server/tests -v
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

# ---- 必须在 import app.* 之前把数据目录指到临时位置 ------------------------
# config 在 import 时求值 DATA_DIR；晚一步就会读写真实生产库。
_TMP = tempfile.mkdtemp(prefix="dh-test-")
os.environ["DH_DATA_DIR"] = str(pathlib.Path(_TMP) / "data")
os.environ["DH_ENV_FILE"] = str(pathlib.Path(_TMP) / "env.local")
os.environ["DH_RECLAIM"] = "0"          # 测试里绝不触发真实删除
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("https_proxy", None)

_SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVER_ROOT))

from app import insights, redact, store, tenants  # noqa: E402


class UpsertKeepsTranscript(unittest.TestCase):
    """同一 seg_id 重传时，已有文本 / processed / is_user 必须保留。"""

    TID = "utest"

    @classmethod
    def setUpClass(cls):
        # tenants.add 对已存在的租户抛 ValueError（不是幂等接口），这里忽略即可
        try:
            tenants.add(cls.TID, "单测租户")
        except ValueError:
            pass

    def _upsert(self, seg_id: int, text_hint: str = ""):
        store.upsert_segment(
            seg_id=seg_id, session_id=1, date="2026-10-22",
            start_offset_ms=1792666887000, duration_ms=6000,
            speech_ratio=0.9, path=f"/tmp/{seg_id}.wav", tenant=self.TID,
        )
        if text_hint:
            store.mark_segment_processed(seg_id, text_hint, True, tenant=self.TID)

    def _row(self, seg_id: int) -> dict:
        for r in store.segments_of_date("2026-10-22", tenant=self.TID):
            if int(r["seg_id"]) == seg_id:
                return r
        self.fail(f"seg {seg_id} 不在库里")

    def test_first_upload_has_empty_text(self):
        self._upsert(9001)
        row = self._row(9001)
        self.assertEqual((row["text"] or ""), "")
        self.assertEqual(int(row["processed"] or 0), 0)

    def test_reupload_keeps_text_and_processed(self):
        """1.0.4 事故回归：第二次 upsert 不得把 text/processed 清空。"""
        self._upsert(9002, "今天聊了网关和调光方案")
        store.upsert_segment(
            seg_id=9002, session_id=1, date="2026-10-22",
            start_offset_ms=1792666887000, duration_ms=6000,
            speech_ratio=0.9, path="/tmp/9002-again.wav", tenant=self.TID,
        )
        row = self._row(9002)
        self.assertEqual(row["text"], "今天聊了网关和调光方案")
        self.assertEqual(int(row["processed"] or 0), 1)
        # 路径要跟着最新上传更新（音频文件确实被覆盖）
        self.assertTrue(row["path"].endswith("9002-again.wav"))

    def test_reupload_sets_processed_when_no_text(self):
        """没有文本的段重传后仍应待转写（不能误标成已处理）。"""
        self._upsert(9003)
        store.upsert_segment(
            seg_id=9003, session_id=1, date="2026-10-22",
            start_offset_ms=1792666887000, duration_ms=6000,
            speech_ratio=0.9, path="/tmp/9003b.wav", tenant=self.TID,
        )
        row = self._row(9003)
        self.assertEqual((row["text"] or ""), "")
        self.assertEqual(int(row["processed"] or 0), 0)


class LowValueBoundary(unittest.TestCase):
    """reclaim 的低价值判定：宁可漏判，不可误判有效内容。"""

    def test_empty_is_low(self):
        self.assertTrue(insights.is_low_value(""))
        self.assertTrue(insights.is_low_value("   "))

    def test_pure_response_is_low(self):
        self.assertTrue(insights.is_low_value("嗯，对，好的。"))
        self.assertTrue(insights.is_low_value("行，可以。"))

    def test_real_content_is_not_low(self):
        self.assertFalse(insights.is_low_value("客户要三个房间都做调光，预算两万左右。"))
        # 含数字/型号等实体信号
        self.assertFalse(insights.is_low_value("这个 KNX 网关报价 1280 元。"))
        self.assertFalse(insights.is_low_value("明天上午十点去现场量尺。"))


class RedactMasksSecrets(unittest.TestCase):
    """隐私样本必须掩码，且原值不得出现在 classify/redact 的输出里。

    这两处的返回值会进日报、/summary 与手机洞察卡（会被截屏外发）。
    """

    def _samples(self, text: str) -> str:
        return "".join(s for c in redact.classify(text) for s in c["samples"])

    def test_phone_masked(self):
        joined = self._samples("我的手机是 13812345678")
        self.assertNotIn("13812345678", joined)
        self.assertIn("*", joined, "必须打码")

    def test_id_card_masked(self):
        joined = self._samples("身份证 320102199001011234")
        self.assertNotIn("320102199001011234", joined)

    def test_redact_removes_original(self):
        out, n = redact.redact("我的手机是 13812345678，密码是 abc123")
        self.assertGreater(n, 0)
        self.assertNotIn("13812345678", out)
        self.assertNotIn("abc123", out)

    def test_clean_text_untouched(self):
        self.assertEqual(redact.classify("好的"), [])


class ReclaimPlanSafety(unittest.TestCase):
    """回收计划的安全边界：没有转写文本的段绝不进入删除清单。"""

    TID = "utest"

    @classmethod
    def setUpClass(cls):
        # tenants.add 对已存在的租户抛 ValueError（不是幂等接口），这里忽略即可
        try:
            tenants.add(cls.TID, "单测租户")
        except ValueError:
            pass   # 幂等：已存在时 add 会覆盖同名显示名

    def test_plan_skips_segments_without_text(self):
        from app import reclaim
        # 造一个 60 天前、无文本的段 + 对应 wav；plan 不应把它列进删除清单
        old_ms = 1758000000000
        t = tenants.resolve(self.TID)
        d = t.audio_dir / "2026-09-16"
        d.mkdir(parents=True, exist_ok=True)
        wav = d / "9101.wav"
        wav.write_bytes(b"RIFF0000WAVE")
        store.upsert_segment(
            seg_id=9101, session_id=1, date="2026-09-16",
            start_offset_ms=old_ms, duration_ms=6000, speech_ratio=0.9,
            path=str(wav), tenant=self.TID,
        )
        plan = reclaim.plan(self.TID)
        paths = [str(p) for p, _, _ in plan["files"]]
        self.assertNotIn(str(wav), paths, "无转写文本的 wav 绝不能被回收")

    def test_plan_includes_aged_with_text(self):
        from app import reclaim
        old_ms = 1758000000000
        t = tenants.resolve(self.TID)
        d = t.audio_dir / "2026-09-16"
        d.mkdir(parents=True, exist_ok=True)
        wav = d / "9102.wav"
        wav.write_bytes(b"RIFF0000WAVE")
        store.upsert_segment(
            seg_id=9102, session_id=1, date="2026-09-16",
            start_offset_ms=old_ms, duration_ms=6000, speech_ratio=0.9,
            path=str(wav), tenant=self.TID,
        )
        store.mark_segment_processed(9102, "这段有转写，可以回收音频", None, tenant=self.TID)
        plan = reclaim.plan(self.TID)
        paths = [str(p) for p, _, _ in plan["files"]]
        self.assertIn(str(wav), paths, "有文本且超龄的 wav 应进入回收清单")


if __name__ == "__main__":
    unittest.main(verbosity=2)
