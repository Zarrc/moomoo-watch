"""周更入库 —— 把 {数据仓根} 的 stocks_YYYY-MM-DD.xlsx 幂等灌进同目录 history.db。

契约见 intraday-plan.md：

    ticks(ts, code, name, open, high, low, last, prev_close, change_pct, volume, turnover)
    PRIMARY KEY (ts, code)；INSERT OR REPLACE → 可重跑、天然幂等
    单写者：只有本脚本写 history.db；连接用完即关（OneDrive 红线）

进度用 {数据仓根}/.etl_watermark.json（文件名 → 已入库行数）跟踪：
行数没变的文件跳过；变了（或新文件）整文件重灌 —— REPLACE 兜底，重复无害。
--rebuild 无视水位全量重灌（仍幂等）。

计划任务：每周六 09:00（+08:00 = 美东周五 21:00，快照窗口已收、无并发写）。
不需要 OpenD、不联网、不推送 —— 纯本地 ETL。history.db 即 trading-agent
「数据海 L0」的地基，列序唯一真源 = intraday/snapshot.py 的 COLUMNS。

用法：
    python intraday/etl_weekly.py                # 计划任务调这个
    python intraday/etl_weekly.py --dry-run      # 只报告会灌什么，不写库不写水位
    python intraday/etl_weekly.py --rebuild      # 无视水位全量重灌

退出码：0 = 正常（含「没有新东西」）；1 = 硬失败（配置/数据仓/库）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 cp1252，中文日志会 UnicodeEncodeError 崩掉（与 main.py 同款处理）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from core.config import Config                        # noqa: E402
from intraday.snapshot import COLUMNS, setup_logging  # noqa: E402

log = logging.getLogger("moomoo-watch.intraday")

CST = timezone(timedelta(hours=8))
DB_NAME = "history.db"
WM_NAME = ".etl_watermark.json"

_DDL = f"""
CREATE TABLE IF NOT EXISTS ticks (
  ts TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT,
  open REAL, high REAL, low REAL, last REAL, prev_close REAL,
  change_pct REAL, volume REAL, turnover REAL,
  PRIMARY KEY (ts, code)
);
CREATE INDEX IF NOT EXISTS idx_ticks_code_ts ON ticks (code, ts);
"""


# ---------------------------------------------------------------------------
# 纯 IO（无网络；全部即开即关）
# ---------------------------------------------------------------------------
def find_files(store: Path) -> List[Path]:
    """数据仓里的当日 xlsx，按文件名（=日期）升序。"""
    return sorted(store.glob("stocks_*.xlsx"))


def read_rows(xlsx: Path) -> List[Tuple]:
    """xlsx → COLUMNS 序元组列表。表头不符抛 ValueError（整文件跳过，不串位）。"""
    from openpyxl import load_workbook

    wb = load_workbook(xlsx, read_only=True)
    try:
        if "ticks" not in wb.sheetnames:
            raise ValueError(f"无 ticks sheet")
        it = wb["ticks"].iter_rows(values_only=True)
        header = list(next(it, ()) or ())
        if header[: len(COLUMNS)] != COLUMNS:
            raise ValueError(f"表头与 ticks 契约不符：{header}")
        rows = []
        for v in it:
            if not v or v[0] is None:            # 空行/尾部残行
                continue
            rows.append(tuple(v[: len(COLUMNS)]))
        return rows
    finally:
        wb.close()


def load_watermark(store: Path) -> Dict[str, Any]:
    try:
        return json.loads((store / WM_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_watermark(store: Path, wm: Dict[str, Any]) -> None:
    p = store / WM_NAME
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(wm, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def upsert(conn: sqlite3.Connection, rows: List[Tuple]) -> int:
    ph = ",".join("?" * len(COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO ticks ({','.join(COLUMNS)}) VALUES ({ph})", rows)
    return len(rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(cfg, dry_run: bool = False, rebuild: bool = False) -> Dict[str, Any]:
    """灌库。返回摘要 dict。sqlite 异常向上抛（main 兜底 exit 1）。"""
    store = Path(str(cfg.get("intraday.store_root") or "").strip())
    wm: Dict[str, Any] = {} if rebuild else load_watermark(store)
    files = find_files(store)
    s: Dict[str, Any] = {"files": len(files), "ingested_files": 0, "rows": 0,
                         "unchanged": 0, "bad": [], "db_rows": None}

    pending: List[Tuple[Path, List[Tuple]]] = []
    for f in files:
        try:
            rows = read_rows(f)
        except ValueError as exc:
            log.warning("跳过 %s：%s", f.name, exc)
            s["bad"].append(f.name)
            continue
        if wm.get(f.name, {}).get("rows") == len(rows):
            s["unchanged"] += 1
            continue
        pending.append((f, rows))
        s["rows"] += len(rows)

    if dry_run:
        log.info("[dry-run] 会灌 %d 文件 / %d 行：%s",
                 len(pending), s["rows"], [f.name for f, _ in pending])
        s["ingested_files"] = len(pending)
        s["dry_run"] = True
        return s

    conn = sqlite3.connect(store / DB_NAME)
    try:
        conn.executescript(_DDL)
        for f, rows in pending:
            upsert(conn, rows)
            wm[f.name] = {"rows": len(rows),
                          "ingested_at": datetime.now(CST).isoformat(timespec="seconds")}
            s["ingested_files"] += 1
        conn.commit()
        s["db_rows"] = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    finally:
        conn.close()                      # 🔴 OneDrive 红线：用完即关，让同步看见完整文件

    if s["ingested_files"] or s["bad"]:
        save_watermark(store, wm)
    return s


def main() -> int:
    p = argparse.ArgumentParser(description="moomoo-watch 周更入库（xlsx → history.db）")
    p.add_argument("--config", default=None, help="配置文件路径（默认项目内 config.yaml）")
    p.add_argument("--dry-run", action="store_true", help="只报告，不写库不写水位")
    p.add_argument("--rebuild", action="store_true", help="无视水位全量重灌（仍幂等）")
    args = p.parse_args()

    try:
        cfg = Config(Path(args.config) if args.config else None)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[致命] 配置加载失败：{exc}", file=sys.stderr)
        return 1

    setup_logging(cfg)
    log.info("=" * 58)
    log.info("周更入库启动 | dry_run=%s rebuild=%s", args.dry_run, args.rebuild)

    if not cfg.get("intraday.enabled", True):
        log.info("intraday.enabled = false → 本轮退出")
        return 0
    root_str = str(cfg.get("intraday.store_root") or "").strip()
    if not root_str:
        log.error("config.yaml 缺 intraday.store_root（数据仓根）——见 intraday-plan.md")
        return 1
    Path(root_str).mkdir(parents=True, exist_ok=True)

    s = run(cfg, dry_run=args.dry_run, rebuild=args.rebuild)
    log.info("周更入库完成：文件 %d（灌 %d / 未变 %d / 坏 %d）· 本批 %d 行 · 库内共 %s 行",
             s["files"], s["ingested_files"], s["unchanged"], len(s["bad"]),
             s["rows"], s["db_rows"] if s["db_rows"] is not None else "-")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                                        # noqa: BLE001 —— 绝不让调度器看到崩溃栈
        logging.getLogger("moomoo-watch.intraday").exception("未捕获异常：%s", exc)
        sys.exit(1)
