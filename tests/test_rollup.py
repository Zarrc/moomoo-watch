"""rollup 单测 —— stdlib `unittest`，不引入任何新依赖。

跑法（在仓库根）：
    python -m unittest discover -s tests -v
    # 或
    python tests/test_rollup.py

覆盖：命中 / 死区判 flat / 触发失效 / 跳 N 天打分 / 无下一条留 pending /
      超期 unscored_stale / 解析块（好·缺·坏）/ 校验与拒绝 / 台账去重 / 命中率派生一致性。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import rollup  # noqa: E402

CST = rollup.CST


class _Cfg:
    """极简 cfg 替身：只用 `get("a.b.c", default)`，与 core.config.Config 同签名。"""

    def __init__(self, raw):
        self.raw = raw

    def get(self, dotted, default=None):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _cfg(**over):
    base = {
        "rollup": {
            "session_boundary_hour": 9,
            "market_holidays": [],
            "call_instruments": ["US.SPY", "US.GLD"],
            "ledger": {"score_deadband_pct": 0.1, "max_score_lag_days": 7},
        },
        "macro": {"enabled": True},
    }
    base["rollup"].update(over)
    return _Cfg(base)


def _rec(session, closes, **extra):
    r = {"session_date": session, "closes": closes, "highs": {}, "lows": {},
         "call_stats": {"present": True, "parsed": 1, "rejected": [], "parse_error": None}}
    r.update(extra)
    return r


def _entry(session, inst, direction, ref, inv_type="close_below", level=0.0):
    return {"session_date": session, "instrument": inst, "direction": direction,
            "key_levels": [], "invalidation": {"type": inv_type, "level": level},
            "confidence": 0.5, "ref_close": ref, "score": None}


class TestPeriodKey(unittest.TestCase):
    def test_formats(self):
        d = date(2026, 10, 2)
        self.assertEqual(rollup.period_key("daily", d), "2026-10-02")
        self.assertEqual(rollup.period_key("weekly", d), "2026-W40")
        self.assertEqual(rollup.period_key("monthly", d), "2026-10")
        self.assertEqual(rollup.period_key("quarterly", d), "2026-Q4")
        self.assertEqual(rollup.period_key("yearly", d), "2026")


class TestSessionDate(unittest.TestCase):
    def test_saturday_morning_is_friday(self):
        now = datetime(2026, 10, 3, 6, 30, tzinfo=CST)      # 周六
        self.assertEqual(rollup.session_date(_cfg(), now), date(2026, 10, 2))

    def test_monday_morning_skips_weekend(self):
        now = datetime(2026, 10, 5, 6, 30, tzinfo=CST)      # 周一
        self.assertEqual(rollup.session_date(_cfg(), now), date(2026, 10, 2))

    def test_tuesday_morning_is_monday(self):
        now = datetime(2026, 10, 6, 6, 30, tzinfo=CST)
        self.assertEqual(rollup.session_date(_cfg(), now), date(2026, 10, 5))

    def test_holiday_skipped(self):
        cfg = _cfg(market_holidays=["2026-10-02"])
        now = datetime(2026, 10, 3, 6, 30, tzinfo=CST)
        self.assertEqual(rollup.session_date(cfg, now), date(2026, 10, 1))

    def test_before_close_settle_steps_back(self):
        # 周二 03:00 —— 周一的收盘（周二 05:00 才结算）还没出来 → 应回退到上周五
        now = datetime(2026, 10, 6, 3, 0, tzinfo=CST)
        self.assertEqual(rollup.session_date(_cfg(), now), date(2026, 10, 2))


class TestScoreCall(unittest.TestCase):
    def test_hit_up(self):
        sc = rollup.score_call({"direction": "up", "invalidation": {"type": "close_below", "level": 90}},
                               100.0, 101.0, None, None, _cfg())
        self.assertEqual(sc["outcome"], "hit")
        self.assertEqual(sc["actual_direction"], "up")
        self.assertAlmostEqual(sc["move_pct"], 1.0, places=3)

    def test_deadband_judged_flat(self):
        # 100 → 100.05 = +0.05% < 0.1% 死区 → actual flat
        sc = rollup.score_call({"direction": "flat", "invalidation": {"type": "close_below", "level": 90}},
                               100.0, 100.05, None, None, _cfg())
        self.assertEqual(sc["actual_direction"], "flat")
        self.assertEqual(sc["outcome"], "hit")
        # 同一次波动下，押「up」应判 miss
        sc2 = rollup.score_call({"direction": "up", "invalidation": {"type": "close_below", "level": 90}},
                                100.0, 100.05, None, None, _cfg())
        self.assertEqual(sc2["outcome"], "miss")

    def test_direction_right_but_invalidated(self):
        sc = rollup.score_call({"direction": "down", "invalidation": {"type": "close_below", "level": 99}},
                               100.0, 98.0, None, None, _cfg())
        self.assertEqual(sc["outcome"], "invalidated")
        self.assertTrue(sc["direction_hit"])       # 方向其实是对的，但失效条件优先 → invalidated

    def test_close_above_invalidation(self):
        sc = rollup.score_call({"direction": "up", "invalidation": {"type": "close_above", "level": 105}},
                               100.0, 106.0, None, None, _cfg())
        self.assertEqual(sc["outcome"], "invalidated")


class TestScorePending(unittest.TestCase):
    def test_jump_three_sessions_records_lag(self):
        records = [
            _rec("2026-09-28", {"US.SPY": 100.0}),
            _rec("2026-09-29", {"US.GLD": 50.0}),          # 无 SPY
            _rec("2026-09-30", {"US.GLD": 51.0}),
            _rec("2026-10-01", {"US.GLD": 52.0}),
            _rec("2026-10-02", {"US.SPY": 110.0}),
        ]
        entries = [_entry("2026-09-28", "US.SPY", "up", 100.0)]
        rollup.score_pending(_cfg(), datetime(2026, 10, 3, 6, 30, tzinfo=CST),
                             entries=entries, records=records, save=False)
        self.assertEqual(entries[0]["score"]["outcome"], "hit")
        self.assertEqual(entries[0]["score"]["lag_sessions"], 3)
        self.assertEqual(entries[0]["score"]["scored_by_session"], "2026-10-02")

    def test_no_next_record_stays_pending(self):
        records = [_rec("2026-10-02", {"US.SPY": 100.0})]
        entries = [_entry("2026-10-02", "US.SPY", "up", 100.0)]
        rollup.score_pending(_cfg(), datetime(2026, 10, 3, 6, 30, tzinfo=CST),
                             entries=entries, records=records, save=False)
        self.assertIsNone(entries[0]["score"])           # 不编、留在 pending

    def test_stale_when_no_later_record_and_too_old(self):
        records = [_rec("2026-10-02", {"US.GLD": 50.0})]   # 之后永远没有 SPY
        entries = [_entry("2026-09-01", "US.SPY", "up", 100.0)]
        rollup.score_pending(_cfg(), datetime(2026, 10, 3, 6, 30, tzinfo=CST),
                             entries=entries, records=records, save=False)
        self.assertEqual(entries[0]["score"]["outcome"], "unscored_stale")

    def test_already_scored_is_skipped(self):
        records = [_rec("2026-09-28", {"US.SPY": 100.0}), _rec("2026-09-29", {"US.SPY": 99.0})]
        e = _entry("2026-09-28", "US.SPY", "up", 100.0)
        e["score"] = {"outcome": "hit"}                     # 已打分
        rollup.score_pending(_cfg(), datetime(2026, 9, 30, 6, 30, tzinfo=CST),
                             entries=[e], records=records, save=False)
        self.assertEqual(e["score"], {"outcome": "hit"})    # 未被覆盖


class TestParseCallBlock(unittest.TestCase):
    GOOD = ("# 日评\n\n<!-- CALL:BEGIN -->\n```yaml\n"
            "calls:\n  - instrument: US.SPY\n    direction: up\n"
            "    key_levels: [770.0, 780.0]\n    invalidation: {type: close_below, level: 755.0}\n"
            "    confidence: 0.55\n```\n<!-- CALL:END -->\n\n正文")
    BAD = "<!-- CALL:BEGIN -->\n```yaml\ncalls:\n  - instrument: US.SPY\n    direction: [up\n```\n<!-- CALL:END -->"

    def test_good(self):
        r = rollup.parse_call_block(self.GOOD)
        self.assertTrue(r["present"])
        self.assertIsNone(r["error"])
        self.assertEqual(len(r["calls"]), 1)
        self.assertEqual(r["calls"][0]["instrument"], "US.SPY")

    def test_missing_block_is_not_an_error(self):
        r = rollup.parse_call_block("# 今天只观察，不出预测")
        self.assertFalse(r["present"])
        self.assertIsNone(r["error"])

    def test_broken_yaml_records_error(self):
        r = rollup.parse_call_block(self.BAD)
        self.assertTrue(r["present"])
        self.assertIsNotNone(r["error"])
        self.assertEqual(r["calls"], [])


class TestValidateCall(unittest.TestCase):
    def test_whitelist_rejects(self):
        norm, reason = rollup.validate_call(
            _cfg(), {"instrument": "MY.2429", "direction": "up",
                     "invalidation": {"type": "close_below", "level": 1.0}})
        self.assertIsNone(norm)
        self.assertIn("白名单", reason)

    def test_bad_direction(self):
        norm, reason = rollup.validate_call(
            _cfg(), {"instrument": "US.SPY", "direction": "moon",
                     "invalidation": {"type": "close_below", "level": 1.0}})
        self.assertIsNone(norm)
        self.assertIn("direction", reason)

    def test_bad_invalidation_type(self):
        norm, reason = rollup.validate_call(
            _cfg(), {"instrument": "US.SPY", "direction": "up",
                     "invalidation": {"type": "touch_below", "level": 1.0}})
        self.assertIsNone(norm)

    def test_confidence_out_of_range(self):
        norm, reason = rollup.validate_call(
            _cfg(), {"instrument": "US.SPY", "direction": "up",
                     "invalidation": {"type": "close_below", "level": 1.0}, "confidence": 1.7})
        self.assertIsNone(norm)

    def test_ok(self):
        norm, reason = rollup.validate_call(
            _cfg(), {"instrument": "US.GLD", "direction": "flat",
                     "key_levels": [260, "261.5"],
                     "invalidation": {"type": "close_above", "level": 270}, "confidence": 0.4})
        self.assertIsNone(reason)
        self.assertEqual(norm["key_levels"], [260.0, 261.5])


class TestAppendCalls(unittest.TestCase):
    def test_dedupe_scored_entry(self):
        entries = [_entry("2026-10-01", "US.SPY", "up", 100.0)]
        entries[0]["score"] = {"outcome": "hit"}
        added, rejected = rollup.append_calls(
            _cfg(), entries, date(2026, 10, 1),
            [{"instrument": "US.SPY", "direction": "down",
              "invalidation": {"type": "close_below", "level": 1.0}}], {})
        self.assertEqual(added, 0)
        self.assertEqual(len(entries), 1)

    def test_rejects_and_counts(self):
        entries = []
        added, rejected = rollup.append_calls(
            _cfg(), entries, date(2026, 10, 1),
            [{"instrument": "US.MY", "direction": "up",
              "invalidation": {"type": "close_below", "level": 1.0}}], {})
        self.assertEqual(added, 0)
        self.assertEqual(len(rejected), 1)


class TestScorecard(unittest.TestCase):
    def _records(self):
        return [
            _rec("2026-09-28", {"US.SPY": 100.0}, call_stats={"present": True, "parsed": 1,
                                                              "rejected": [], "parse_error": None}),
            _rec("2026-09-29", {"US.SPY": 101.0}, call_stats={"present": False, "parsed": 0,
                                                              "rejected": [], "parse_error": None}),
            _rec("2026-09-30", {"US.SPY": 99.0}, call_stats={"present": True, "parsed": 0,
                                                             "rejected": [{"instrument": "US.MY"}],
                                                             "parse_error": None}),
            _rec("2026-10-01", {"US.SPY": 100.0}, call_stats={"present": True, "parsed": 1,
                                                              "rejected": [], "parse_error": "坏块"}),
        ]

    def _entries(self):
        a = _entry("2026-09-28", "US.SPY", "up", 100.0)
        a["score"] = {"outcome": "hit", "move_pct": 1.0}
        b = _entry("2026-09-28", "US.GLD", "down", 50.0)
        b["score"] = {"outcome": "miss", "move_pct": 0.5}
        c = _entry("2026-09-29", "US.SPY", "up", 100.0)
        c["score"] = {"outcome": "invalidated", "move_pct": -1.0}
        d = _entry("2026-10-02", "US.SPY", "up", 100.0)   # pending
        return [a, b, c, d]

    def test_counts_and_rate(self):
        sc = rollup.summarize_scorecard(_cfg(), entries=self._entries(), records=self._records())
        o = sc["overall"]
        self.assertEqual((o["n"], o["hit"], o["miss"], o["invalidated"]), (3, 1, 1, 1))
        self.assertAlmostEqual(o["hit_rate"], 1 / 3, places=4)
        cov = sc["coverage"]
        self.assertEqual(cov["pending"], 1)
        self.assertEqual(cov["call_parse_error"], 1)
        self.assertEqual(cov["call_rejected"], 1)
        self.assertGreaterEqual(cov["call_missing"], 2)

    def test_deterministic_apart_from_timestamp(self):
        a = rollup.summarize_scorecard(_cfg(), entries=self._entries(), records=self._records())
        b = rollup.summarize_scorecard(_cfg(), entries=self._entries(), records=self._records())
        a.pop("generated_at"); b.pop("generated_at")
        self.assertEqual(a, b)

    def test_empty_gives_null_rate_not_zero(self):
        sc = rollup.summarize_scorecard(_cfg(), entries=[], records=[])
        self.assertIsNone(sc["overall"]["hit_rate"])
        self.assertEqual(sc["overall"]["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
