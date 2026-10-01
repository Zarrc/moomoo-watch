"""生成 rollup 的离线样例：伪造一整周（2026-09-28 … 10-02）的日记录 + 台账 + 水位。

用途：**不用等一周**就能跑周报与命中率。生成是确定性的（无随机、无 Date.now）。
跑法：python fixtures/make_rollup_fixtures.py

产出（都在 fixtures/rollup/）：
  daily/2026-09-28.json … daily/2026-10-02.json
  ledger.json   —— 7 条已可打分的预测 + 1 条 pending
  due.json      —— 水位：只有 weekly 与当前不匹配 → 只出周报（其余周期都已对齐）
"""

from __future__ import annotations

import json
import math
import random
import sys
import zlib
from datetime import date, timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

HERE = Path(__file__).resolve().parent
OUT = HERE / "rollup"

# 伪造一周的收盘（US.SPY / US.GLD 是白名单标的；额外放 US.AAPL 以贴近真实持仓）
CLOSES = {
    "2026-09-28": {"US.SPY": 640.00, "US.GLD": 260.50, "US.AAPL": 210.00},
    "2026-09-29": {"US.SPY": 645.50, "US.GLD": 262.10, "US.AAPL": 214.20},
    "2026-09-30": {"US.SPY": 638.20, "US.GLD": 258.40, "US.AAPL": 208.40},
    "2026-10-01": {"US.SPY": 647.90, "US.GLD": 263.70, "US.AAPL": 213.50},
    "2026-10-02": {"US.SPY": 644.31, "US.GLD": 264.21, "US.AAPL": 212.81},
}
SESSIONS = sorted(CLOSES)


def _changes(d: str) -> dict:
    i = SESSIONS.index(d)
    if i == 0:
        return {k: None for k in CLOSES[d]}
    prev = CLOSES[SESSIONS[i - 1]]
    return {k: round((v - prev[k]) / prev[k] * 100, 3) for k, v in CLOSES[d].items()}


def day_record(d: str) -> dict:
    closes = CLOSES[d]
    return {
        "session_date": d,
        "generated_at": f"{d}T06:30:00+08:00",
        "closes": closes,
        "changes_pct": _changes(d),
        "highs": {k: round(v * 1.004, 4) for k, v in closes.items()},
        "lows": {k: round(v * 0.996, 4) for k, v in closes.items()},
        "account": {"equity": 10000.0, "currency": "USD", "cash": 3120.55,
                    "market_value_by_currency": {"USD": 5110.0}, "pnl_by_currency": {"USD": 210.0},
                    "mixed_currency": False, "totals_complete": True,
                    "position_count": 3, "unquoted_position_count": 0},
        "weights": {"US.GLD": 0.317, "US.AAPL": 0.106, "US.SPY": 0.064},
        "indicators": {k: {"last_close": v, "ma50": round(v * 0.99, 4)} for k, v in closes.items()},
        "macro_actuals": [],
        "macro_snapshot": {"_fixture": True, "ok": True},
        "events_ahead": [],
        "alerts": [],
        "news_top": [],
        "calls": [],
        "call_stats": {"present": True, "parsed": 0, "rejected": [], "parse_error": None},
        "scores": [],
        "provenance": {"source": "fixture", "backfilled": False,
                       "degraded_fields": [], "macro_fixture": True},
    }


def ledger_entry(session: str, instrument: str, direction: str, inv_type: str,
                 level: float, conf: float, ref: float) -> dict:
    return {
        "session_date": session, "instrument": instrument, "direction": direction,
        "key_levels": [ref], "invalidation": {"type": inv_type, "level": level},
        "confidence": conf, "ref_close": ref, "score": None,
        "created_at": f"{session}T06:30:00+08:00",
    }


# 日 K：回填用（~300 根业务日，末端 5 根与伪造周对齐，保证「回填」与「周报」同口径）
KLINE_CODES = ["US.SPY", "US.GLD", "US.AAPL"]
KLINE_START = {"US.SPY": 560.0, "US.GLD": 238.0, "US.AAPL": 226.0}
SEED = 20260930


def _business_days(end: date, n: int):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def gen_daily_klines(code: str, end: date = date(2026, 10, 2), n: int = 300):
    rng = random.Random(SEED + zlib.crc32(code.encode("utf-8")) % 10_000)
    days = _business_days(end, n)
    bars, price = [], KLINE_START[code]
    for d in days:
        price *= math.exp(0.0004 + rng.gauss(0, 0.008))
        bars.append({"time": d.isoformat() + " 00:00:00",
                     "open": round(price, 4), "high": round(price * 1.006, 4),
                     "low": round(price * 0.994, 4), "close": round(price, 4),
                     "volume": int(9e8 * (0.6 + rng.random()))})
    # 缩放，让走线落在伪造周附近；再把最后 5 根强制对齐伪造周
    scale = CLOSES[SESSIONS[-1]][code] / bars[-1]["close"]
    for b in bars:
        for k in ("open", "high", "low", "close"):
            b[k] = round(b[k] * scale, 4)
    for d in SESSIONS:
        for b in bars:
            if b["time"][:10] == d:
                b["close"] = CLOSES[d][code]
                b["high"] = round(CLOSES[d][code] * 1.006, 4)
                b["low"] = round(CLOSES[d][code] * 0.994, 4)
    return bars


def main() -> None:
    (OUT / "daily").mkdir(parents=True, exist_ok=True)

    for d in SESSIONS:
        (OUT / "daily" / f"{d}.json").write_text(
            json.dumps(day_record(d), ensure_ascii=False, indent=2), encoding="utf-8")

    kdir = HERE / "klines_daily"
    kdir.mkdir(parents=True, exist_ok=True)
    for code in KLINE_CODES:
        (kdir / f"{code}.json").write_text(
            json.dumps(gen_daily_klines(code), ensure_ascii=False), encoding="utf-8")

    # 7 条「次日可打分」的预测 + 1 条 pending。
    # 预期结果：hit 4 / miss 2 / invalidated 1 → n=7，命中率 4/7≈0.5714；另 pending 1。
    ledger = [
        ledger_entry("2026-09-28", "US.SPY", "up",   "close_below", 630.0, 0.55, 640.00),  # → 645.50 ↑ hit
        ledger_entry("2026-09-28", "US.GLD", "up",   "close_below", 252.0, 0.50, 260.50),  # → 262.10 ↑ hit
        ledger_entry("2026-09-29", "US.SPY", "down", "close_above", 655.0, 0.45, 645.50),  # → 638.20 ↓ hit
        ledger_entry("2026-09-29", "US.GLD", "up",   "close_below", 259.0, 0.40, 262.10),  # → 258.40 触发失效
        ledger_entry("2026-09-30", "US.GLD", "flat", "close_above", 270.0, 0.35, 258.40),  # → 263.70 ↑ miss
        ledger_entry("2026-10-01", "US.SPY", "up",   "close_below", 635.0, 0.55, 647.90),  # → 644.31 ↓ miss
        ledger_entry("2026-10-01", "US.GLD", "up",   "close_below", 255.0, 0.60, 263.70),  # → 264.21 ↑ hit
        ledger_entry("2026-10-02", "US.SPY", "up",   "close_below", 630.0, 0.52, 644.31),  # → 无下一条 → pending
    ]
    (OUT / "ledger.json").write_text(
        json.dumps({"entries": ledger}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 水位：weekly 停在上一周（W39）→ W40 到期；其余周期都对齐到当前 → 不出。
    (OUT / "due.json").write_text(json.dumps({
        "watermark": {"daily": "2026-10-02", "weekly": "2026-W39",
                      "monthly": "2026-10", "quarterly": "2026-Q4", "yearly": "2026"},
        "last_session": "2026-10-02", "_updated_at": "2026-10-02T06:30:00+08:00",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已生成 rollup fixture 到 {OUT}")
    print(f"  日记录：{len(SESSIONS)} 条（{SESSIONS[0]} … {SESSIONS[-1]}）")
    print(f"  台账：{len(ledger)} 条（7 可打分 + 1 pending）")
    print(f"  日 K：{len(KLINE_CODES)} 个标的 × 300 根 → {kdir}")


if __name__ == "__main__":
    main()
