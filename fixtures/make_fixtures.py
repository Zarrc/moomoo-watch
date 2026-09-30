"""生成离线样例数据（fixture）。

用途：让整条流水线在**没有 OpenD、没有账户**的情况下也能真跑一遍 ——
这是本机唯一可验证的路径。生成是确定性的（固定种子），所以结果可复现。

跑法：python fixtures/make_fixtures.py
"""

from __future__ import annotations

import json
import math
import random
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 同 main.py：Windows 控制台 cp1252 会让中文 print 崩掉
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

HERE = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
SEED = 20260930


def gen_klines(code: str, start: float, n: int = 220, drift: float = 0.0006,
               vol: float = 0.006, seed: int = SEED) -> list[dict]:
    # ⚠️ 不能用 hash(code)：Python 的字符串 hash 每进程加盐（PYTHONHASHSEED），
    # 会让「固定种子」失效 → 每次生成的数据都不同。crc32 是稳定的。
    rng = random.Random(seed + zlib.crc32(code.encode("utf-8")) % 10_000)
    bars, price = [], start
    t0 = datetime.now(CST) - timedelta(hours=n)
    for i in range(n):
        price *= math.exp(drift + rng.gauss(0, vol))
        high = price * (1 + abs(rng.gauss(0, vol / 2)))
        low = price * (1 - abs(rng.gauss(0, vol / 2)))
        openp = low + (high - low) * rng.random()
        vol_ = int(800_000 * (0.6 + rng.random() * 1.2))
        bars.append({
            "time": (t0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(openp, 4), "high": round(high, 4),
            "low": round(low, 4), "close": round(price, 4), "volume": vol_,
        })
    return bars


def main() -> None:
    kdir = HERE / "klines"
    kdir.mkdir(parents=True, exist_ok=True)

    specs = {"US.GLD": (238.0, 0.0007), "US.SPY": (548.0, 0.0004), "US.AAPL": (226.0, -0.0002)}
    closes: dict[str, float] = {}
    for code, (start, drift) in specs.items():
        bars = gen_klines(code, start, drift=drift)
        closes[code] = bars[-1]["close"]
        (kdir / f"{code}.json").write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")

    account = {
        "equity": 10000.0, "cash": 3120.55,
        "currency": "USD", "note": "fixture 样例账户",
    }
    prev = {"US.GLD": closes["US.GLD"] / 1.008, "US.SPY": closes["US.SPY"] / 0.996,
            "US.AAPL": closes["US.AAPL"] / 1.021}

    positions = [
        {"code": "US.GLD", "name": "SPDR Gold Shares", "direction": "LONG",
         "qty": 12, "cost": 231.40, "last": closes["US.GLD"],
         "prev_close": round(prev["US.GLD"], 4), "currency": "USD"},
        {"code": "US.AAPL", "name": "Apple Inc.", "direction": "LONG",
         "qty": 5, "cost": 233.10, "last": closes["US.AAPL"],
         "prev_close": round(prev["US.AAPL"], 4), "currency": "USD"},
        {"code": "US.SPY", "name": "SPDR S&P 500 ETF", "direction": "LONG",
         "qty": 1, "cost": 540.00, "last": closes["US.SPY"],
         "prev_close": round(prev["US.SPY"], 4), "currency": "USD"},
    ]
    watchlist = [
        {"code": c, "name": n, "last": closes[c], "prev_close": round(prev[c], 4)}
        for c, n in [("US.GLD", "SPDR Gold Shares"), ("US.SPY", "SPDR S&P 500 ETF"),
                     ("US.AAPL", "Apple Inc.")]
    ]
    calendar = [
        {"name": "US CPI (Consumer Price Index) YoY", "minutes_from_now": 45,
         "high_risk": True, "country": "US", "importance": 3},
        {"name": "US Initial Jobless Claims", "minutes_from_now": 300,
         "high_risk": False, "country": "US", "importance": 2},
        {"name": "US Nonfarm Payrolls (NFP)", "minutes_from_now": 2880,
         "high_risk": True, "country": "US", "importance": 3},
        {"name": "FOMC Interest Rate Decision", "minutes_from_now": 5040,
         "high_risk": True, "country": "US", "importance": 3},
    ]

    for name, obj in [("account", account), ("positions", positions),
                      ("watchlist", watchlist), ("economic_calendar", calendar)]:
        (HERE / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已生成 fixture 到 {HERE}")
    print(f"  收盘价：{ {k: round(v, 2) for k, v in closes.items()} }")
    print(f"  K 线：{len(specs)} 个标的 × 220 根")


if __name__ == "__main__":
    main()
