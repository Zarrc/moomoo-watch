"""intraday/snapshot 单测 —— 只测纯函数区，不碰 OpenD / 网络 / 数据仓。

跑法（在仓库根）：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intraday import snapshot as sn   # noqa: E402

ET = ZoneInfo("America/New_York")


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


def cfg_stub():
    return _Cfg({
        "intraday": {
            "window": {"tz": "America/New_York", "start": "09:30", "end": "20:00"},
            "thresholds": {"positions_pct": 3.0, "watchlist_pct": 5.0},
        },
        "rollup": {"market_holidays": ["2026-11-26"]},
    })


class TestClassify(unittest.TestCase):
    def _et(self, y, mo, d, h, mi=0):
        return datetime(y, mo, d, h, mi, tzinfo=ET)

    def test_weekday_in_window_is_snapshot(self):
        # 2026-10-27 是周二
        self.assertEqual(sn.classify(self._et(2026, 10, 27, 12, 0), cfg_stub(), True, True), "snapshot")

    def test_before_open_skips(self):
        self.assertEqual(sn.classify(self._et(2026, 10, 27, 8, 0), cfg_stub(), True, True), "skip")

    def test_open_boundary_included(self):
        self.assertEqual(sn.classify(self._et(2026, 10, 27, 9, 30), cfg_stub(), True, True), "snapshot")

    def test_end_boundary_is_eod(self):
        self.assertEqual(sn.classify(self._et(2026, 10, 27, 20, 0), cfg_stub(), True, False), "eod")

    def test_eod_marker_prevents_repeat(self):
        self.assertEqual(sn.classify(self._et(2026, 10, 27, 20, 30), cfg_stub(), True, True), "skip")

    def test_eod_without_xlsx_skips(self):
        self.assertEqual(sn.classify(self._et(2026, 10, 27, 21, 0), cfg_stub(), False, False), "skip")

    def test_weekend_skips_even_in_window(self):
        # 2026-10-24 是周六
        self.assertEqual(sn.classify(self._et(2026, 10, 24, 12, 0), cfg_stub(), True, False), "skip")

    def test_holiday_skips(self):
        # 2026-11-26 感恩节（周四，落在 rollup.market_holidays 表里）
        self.assertEqual(sn.classify(self._et(2026, 11, 26, 12, 0), cfg_stub(), True, False), "skip")


class TestBuildRows(unittest.TestCase):
    def _rec(self, code="US.NVDA", last="210.55", pc="203.60", name="NVIDIA"):
        return {"code": code, "name": name, "last_price": last, "prev_close_price": pc,
                "open_price": "205.0", "high_price": "211.0", "low_price": "204.0",
                "volume": "1234567", "turnover": "2.6e8"}

    def test_change_pct_and_columns(self):
        rows = sn.build_rows([self._rec()], "2026-10-27 12:00:00")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(list(r.keys()), sn.COLUMNS)
        self.assertEqual(r["change_pct"], round((210.55 - 203.60) / 203.60 * 100, 3))
        self.assertEqual(r["ts"], "2026-10-27 12:00:00")

    def test_non_us_skipped(self):
        rows = sn.build_rows([self._rec(code="MY.1155")], "2026-10-27 12:00:00")
        self.assertEqual(rows, [])

    def test_missing_prev_close_skipped(self):
        rows = sn.build_rows([self._rec(pc="N/A")], "2026-10-27 12:00:00")
        self.assertEqual(rows, [])


class TestMoversAndTitle(unittest.TestCase):
    def _row(self, code, cp):
        return {"code": code, "name": "x", "last": 1.0, "change_pct": cp}

    def test_position_threshold_vs_watchlist(self):
        rows = [self._row("US.NVDA", 3.0), self._row("US.MU", 4.9), self._row("US.GLD", -5.0)]
        pos = {"US.NVDA"}
        movers = sn.pick_movers(rows, pos, 3.0, 5.0)
        got = {m["row"]["code"] for m in movers}
        self.assertEqual(got, {"US.NVDA", "US.GLD"})   # 持仓恰 3.0 触发；自选 4.9 不够、-5.0 触发

    def test_once_per_day_filter(self):
        movers = sn.pick_movers([self._row("US.NVDA", 3.4)], {"US.NVDA"}, 3.0, 5.0)
        fresh = [m for m in movers if m["row"]["code"] not in {"US.NVDA": 1}]
        self.assertEqual(fresh, [])

    def test_title_within_40(self):
        items = [(f"US.CODE{i}", 3.3) for i in range(12)]
        t = sn.make_title("⚡", items)
        self.assertLessEqual(len(t), 40)
        self.assertTrue(t.endswith("…"))

    def test_title_short_kept_whole(self):
        t = sn.make_title("⚡", [("US.NVDA", 3.42), ("US.MU", -5.1)])
        self.assertEqual(t, "⚡NVDA +3.4%·MU -5.1%")


if __name__ == "__main__":
    unittest.main()
