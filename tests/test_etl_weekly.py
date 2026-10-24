"""intraday/etl_weekly 单测 —— 用临时目录造数据仓，验证幂等/水位/坏表头跳过。

跑法（在仓库根）：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intraday import etl_weekly as etl     # noqa: E402
from intraday import snapshot as sn        # noqa: E402


class _Cfg:
    def __init__(self, raw):
        self.raw = raw

    def get(self, dotted, default=None):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _row(ts, code, last, pc):
    return {"ts": ts, "code": code, "name": code[-1], "open": 1.0, "high": 2.0,
            "low": 0.5, "last": last, "prev_close": pc,
            "change_pct": round((last - pc) / pc * 100, 3), "volume": 100, "turnover": 1000.0}


class TestEtl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.cfg = _Cfg({"intraday": {"store_root": str(self.store)}})
        # 两天、每天 2 标的 × 1 轮
        sn.append_rows(self.store / "stocks_2026-10-20.xlsx",
                       [_row("2026-10-20 10:00:00", "US.A", 10.0, 9.0),
                        _row("2026-10-20 10:00:00", "US.B", 5.0, 5.0)])
        sn.append_rows(self.store / "stocks_2026-10-23.xlsx",
                       [_row("2026-10-23 11:00:00", "US.A", 11.0, 10.0),
                        _row("2026-10-23 11:00:00", "US.B", 5.5, 5.0)])

    def tearDown(self):
        self._tmp.cleanup()

    def _db_count(self):
        conn = sqlite3.connect(self.store / etl.DB_NAME)
        try:
            return conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        finally:
            conn.close()

    def test_ingest_then_idempotent(self):
        s1 = etl.run(self.cfg)
        self.assertEqual(s1["ingested_files"], 2)
        self.assertEqual(s1["rows"], 4)
        self.assertEqual(self._db_count(), 4)
        # 第二次：水位命中，全部跳过，行数不变
        s2 = etl.run(self.cfg)
        self.assertEqual(s2["ingested_files"], 0)
        self.assertEqual(s2["unchanged"], 2)
        self.assertEqual(self._db_count(), 4)

    def test_rebuild_reruns_without_duplicates(self):
        etl.run(self.cfg)
        s = etl.run(self.cfg, rebuild=True)
        self.assertEqual(s["ingested_files"], 2)
        self.assertEqual(self._db_count(), 4)      # REPLACE 兜底：重灌不翻倍

    def test_bad_header_file_skipped_others_ingested(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.title = "ticks"
        wb.active.append(["not", "the", "contract"])
        wb.save(self.store / "stocks_2026-10-21.xlsx")
        wb.close()
        s = etl.run(self.cfg)
        self.assertIn("stocks_2026-10-21.xlsx", s["bad"])
        self.assertEqual(s["ingested_files"], 2)
        self.assertEqual(self._db_count(), 4)

    def test_dry_run_writes_nothing(self):
        s = etl.run(self.cfg, dry_run=True)
        self.assertTrue(s.get("dry_run"))
        self.assertFalse((self.store / etl.DB_NAME).exists())
        self.assertFalse((self.store / etl.WM_NAME).exists())


if __name__ == "__main__":
    unittest.main()
