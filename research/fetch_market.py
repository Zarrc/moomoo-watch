#!/usr/bin/env python3
"""research/fetch_market.py —— 市场观察数据抓取（OpenD 行情 → data/research/）。

给 investment-research 的 market_daily / market_weekly / market_event 模式供数：
  market_<YYYY-MM-DD>.json       盘前/常规/盘后三段行情（5 分钟棒，extended_time）
  market_week_<YYYY-MM-DD>.json  周度日线聚合（板块走向用）

用法：
  python research/fetch_market.py --date 2026-10-09            # 指定交易日
  python research/fetch_market.py --auto                       # 自动判「上一美股交易日」
  python research/fetch_market.py --auto --weekly              # 同时出周度包
  python research/fetch_market.py --probe                       # 只探测 OpenD/VIX 代码可用性

依赖：futu-api（moomoo-watch 生产既有依赖）。OpenD 不可达 → 打日志退出码 3。
时间口径：futu 的 time_key 按 +08:00 解析（与 core/market.py:457 同约定），
时段切分转 America/New_York（stdlib zoneinfo，自动处理夏令时）。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = Path(__file__).resolve().parent / "research.yaml"
DATA_DIR = REPO_ROOT / "data" / "research"

CST = timezone(timedelta(hours=8))
ET = ZoneInfo("America/New_York")


def log(msg: str) -> None:
    print(f"[market {datetime.now().astimezone().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_not_needed() -> None:  # 占位说明：本模块只走 OpenD，不碰 HTTP
    pass


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def probe_opend(host: str, port: int, timeout: float = 2.5) -> bool:
    import socket
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ futu -----
def open_ctx(host: str, port: int):
    from futu import OpenQuoteContext
    return OpenQuoteContext(host=host, port=port)


def _rows_of(data):
    """futu 返回可能是 DataFrame 或 list[dict] —— 统一成 list[dict]。"""
    try:
        return data.to_dict(orient="records")  # pandas DataFrame
    except AttributeError:
        return list(data) if data else []


def _get(ret_data, key, default=None):
    ret, data = ret_data
    if ret != 0:
        raise RuntimeError(f"futu ret={ret}")
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def prev_trading_day(ctx, cfg: dict) -> tuple[str, list[str]]:
    """返回（上一交易日, 近 15 个交易日列表）。优先 futu 交易日历，失败退工作日启发。"""
    today_et = datetime.now(ET)
    end = today_et.date().isoformat()
    start = (today_et - timedelta(days=25)).date().isoformat()
    days: list[str] = []
    try:
        from futu import Market
        raw = _get(ctx.request_trading_days(Market.US, start=start, end=end), "trade_date_list")
        days = [str(d)[:10] for d in (raw or [])]
        log(f"交易日历（futu）：{len(days)} 天")
    except Exception as exc:
        log(f"交易日历退化到工作日启发（futu 失败：{str(exc)[:80]}）")
    if not days:
        d = today_et.date()
        while len(days) < 15:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d -= timedelta(days=1)
    days = sorted(set(days))          # 统一升序（futu/启发式两个来源方向不同，防大小月判断出错）
    # 「上一交易日」= 最新的、盘后(20:00 ET)已结束的交易日：
    #   ET 已过 20:05 且今天是交易日 → 今天；否则 → 今天之前最近一个交易日。
    #   （ET 周一早盘跑也应得上周五——盘后早已结束，不是无脑再退一天。）
    now_et = datetime.now(ET)
    if now_et.time().strftime("%H:%M") >= "20:05" and today_et.date().isoformat() in days:
        return today_et.date().isoformat(), days
    cand = [d for d in days if d < today_et.date().isoformat()]
    if not cand:
        raise RuntimeError("上一交易日判定为空（节假日连续）")
    return cand[0], days


def _bars_to_et_sessions(rows: list[dict], sessions_cfg: dict) -> dict:
    """5 分钟棒 → ET 时段聚合。futu time_key 按 +08:00 解析（core 同约定）。"""
    buckets: dict[str, list[dict]] = {k: [] for k in ("premarket", "regular", "postmarket")}
    for r in rows:
        try:
            t = datetime.strptime(str(r.get("time_key", ""))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except ValueError:
            continue
        t_et = t.astimezone(ET)
        hm = t_et.strftime("%H:%M")
        for name, (a, b) in sessions_cfg.items():
            if a <= hm < b:
                buckets[name].append(r)
                break
    out = {}
    for name, bars in buckets.items():
        if not bars:
            out[name] = None
            continue
        first_open = float(bars[0]["open_key"] if "open_key" in bars[0] else bars[0]["open"])
        last_close = float(bars[-1]["close"])
        out[name] = {
            "chg_pct": round((last_close / first_open - 1) * 100, 3) if first_open else None,
            "open": first_open, "close": last_close,
            "high": max(float(b.get("high", 0)) for b in bars),
            "low": min(float(b.get("low", 1e18)) for b in bars),
            "volume": int(sum(float(b.get("volume", 0) or 0) for b in bars)),
            "bars": len(bars),
        }
    return out


def fetch_symbol_sessions(ctx, code: str, label: str, date: str, sessions_cfg: dict) -> dict:
    from futu import KLType
    ret, data = ctx.request_history_kline(
        code=code, start=f"{date} 00:00:00", end=f"{date} 23:59:59",
        ktype=KLType.K_5M, max_count=1000, extended_time=True)
    if ret != 0:
        return {"code": code, "label": label, "error": "kline ret!=0"}
    rows = _rows_of(data)
    if not rows:
        return {"code": code, "label": label, "error": "no bars"}
    sess = _bars_to_et_sessions(rows, sessions_cfg)
    reg = sess.get("regular")
    day_close = (reg or {}).get("close") or float(rows[-1]["close"])
    day_open = (sess.get("premarket") or {}).get("open") or float(rows[0]["open"])
    all_bars = [b for v in sess.values() if v for b in []]  # 占位（高低点直接重扫）
    highs = [float(r.get("high", 0)) for r in rows]
    lows = [float(r.get("low", 1e18)) for r in rows]
    return {
        "code": code, "label": label,
        "sessions": sess,
        "day_open": day_open, "day_close": day_close,
        "day_high": max(highs), "day_low": min(lows),
    }


def fetch_daily_bars(ctx, code: str, n: int) -> list[dict]:
    from futu import KLType
    end = datetime.now(ET).strftime("%Y-%m-%d")
    start = (datetime.now(ET) - timedelta(days=n * 2 + 10)).strftime("%Y-%m-%d")
    ret, data = ctx.request_history_kline(
        code=code, start=f"{start} 00:00:00", end=f"{end} 23:59:59",
        ktype=KLType.K_DAY, max_count=1000)
    if ret != 0:
        return []
    rows = _rows_of(data)
    return [
        {"date": str(r.get("time_key", ""))[:10],
         "close": float(r.get("close", 0)), "open": float(r.get("open_key", r.get("open", 0)) or 0),
         "chg_pct": round((float(r.get("change_rate", 0)) or 0), 3)}
        for r in rows
    ]


# ------------------------------------------------------------------ main -----
def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD")
    ap.add_argument("--auto", action="store_true", help="自动判上一美股交易日")
    ap.add_argument("--weekly", action="store_true", help="同时产出周度聚合包")
    ap.add_argument("--probe", action="store_true", help="只探测 OpenD/VIX 可用性")
    args = ap.parse_args()

    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    mk = cfg["market"]
    host, port = mk["opend_host"], int(mk["opend_port"])

    if not probe_opend(host, port):
        log(f"OpenD 不可达（{host}:{port}）——市场观察跳过。请确认 moomoo/OpenD 已启动。")
        return 3

    ctx = open_ctx(host, port)
    try:
        if args.probe:
            ok, bad = [], []
            for s in mk["symbols"]:
                (ok if fetch_daily_bars(ctx, s["code"], 2) else bad).append(s["code"])
            vix_code = None
            for c in (mk["vix"]["code"], mk["vix"].get("fallback")):
                if c and fetch_daily_bars(ctx, c, 2):
                    vix_code = c
                    break
            log(f"行情可用 {len(ok)}/{len(mk['symbols'])}；VIX 用 {vix_code or '不可用'}")
            if bad:
                log(f"不可用：{bad}")
            return 0

        date = args.date
        if not date:
            date, _days = prev_trading_day(ctx, cfg)
            log(f"自动判定上一交易日：{date}")

        # --- 日包：三段行情 ---
        symbols_out = {}
        for s in mk["symbols"]:
            r = fetch_symbol_sessions(ctx, s["code"], s["label"], date, mk["sessions"])
            if "error" in r:
                log(f"  {s['code']}: {r['error']}")
            symbols_out[s["code"]] = r
            time.sleep(0.15)

        # prev_close（日线倒数第二根）→ day_chg
        for code, r in symbols_out.items():
            if "error" in r:
                continue
            bars = fetch_daily_bars(ctx, code, 5)
            closes = [b for b in bars if b["date"] <= date]
            if len(closes) >= 2 and closes[-1]["date"] == date:
                prev = closes[-2]["close"]
                r["prev_close"] = prev
                r["day_chg_pct"] = round((r["day_close"] / prev - 1) * 100, 3) if prev else None
            time.sleep(0.1)

        # VIX
        vix_out = {"requested": mk["vix"]["code"]}
        for c in (mk["vix"]["code"], mk["vix"].get("fallback")):
            if not c:
                continue
            bars = fetch_daily_bars(ctx, c, 5)
            closes = [b for b in bars if b["date"] <= date]
            if closes:
                vix_out = {"code": c, "note": ("指数本体" if c == mk["vix"]["code"] else "ETN 代理口径"),
                           "close": closes[-1]["close"], "date": closes[-1]["date"],
                           "chg_1d_pct": closes[-2]["chg_pct"] if len(closes) >= 2 else None}
                break
        if "code" not in vix_out:
            vix_out["error"] = "VIX 及代理均不可得（行情权限？）"

        valid = [r for r in symbols_out.values() if "day_chg_pct" in r]
        ranked = sorted(valid, key=lambda r: r["day_chg_pct"], reverse=True)
        pkg = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "date": date,
            "symbols": symbols_out,
            "vix": vix_out,
            "movers": {
                "best": [{"code": r["code"], "label": r["label"], "day_chg_pct": r["day_chg_pct"]} for r in ranked[:3]],
                "worst": [{"code": r["code"], "label": r["label"], "day_chg_pct": r["day_chg_pct"]} for r in ranked[-3:]],
            },
            "_schema": {
                "package": f"market_{date} —— 美股交易日三段行情包（盘前/常规/盘后）",
                "symbols.{code}": "label / sessions.{premarket|regular|postmarket}（各段 chg_pct/open/close/high/low/volume/bars；"
                                  "段内无数据=null，ETF 盘前常见空）/ day_chg_pct（常规收盘 vs 前日收盘，extended 口径下以日线为准）",
                "vix": "情绪锚；note 字段标口径（指数本体 vs VIXY ETN 代理）",
                "movers": "管道排好序的涨跌榜（agent 直接引用，不重算）",
            },
        }
        write_json_atomic(DATA_DIR / f"market_{date}.json", pkg)
        log(f"日包写入 market_{date}.json（{len(valid)}/{len(symbols_out)} 有效，VIX={vix_out.get('code', '缺')}）")

        # --- 周包 ---
        if args.weekly:
            nbars = int(mk.get("weekly_bars", 10))
            wk = {}
            for s in mk["symbols"]:
                bars = [b for b in fetch_daily_bars(ctx, s["code"], nbars) if b["date"] <= date][-5:]
                if len(bars) >= 2:
                    wk[s["code"]] = {"label": s["label"], "closes": bars,
                                     "week_chg_pct": round((bars[-1]["close"] / bars[0]["open"] - 1) * 100, 3)}
                time.sleep(0.1)
            vbars = []
            for c in (mk["vix"]["code"], mk["vix"].get("fallback")):
                vbars = [b for b in fetch_daily_bars(ctx, c, nbars) if b["date"] <= date][-5:]
                if vbars:
                    wk["_vix"] = {"code": c, "closes": vbars,
                                  "week_chg_pct": round((vbars[-1]["close"] / vbars[0]["open"] - 1) * 100, 3)}
                    break
            rank = sorted(((c, v) for c, v in wk.items() if not c.startswith("_")),
                          key=lambda kv: kv[1]["week_chg_pct"], reverse=True)
            wpkg = {
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "week_end": date,
                "symbols": wk,
                "ranking": [{"code": c, "label": v["label"], "week_chg_pct": v["week_chg_pct"]} for c, v in rank],
                "_schema": {
                    "package": f"market_week_{date} —— 周度板块走向包",
                    "symbols.{code}": "label / closes（近 5 交易日日线 date+close+chg_pct）/ week_chg_pct（首日开盘→末日收盘）",
                    "ranking": "管道排好序的周度板块榜",
                },
            }
            write_json_atomic(DATA_DIR / f"market_week_{date}.json", wpkg)
            log(f"周包写入 market_week_{date}.json（{len(wk)} 标的）")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
