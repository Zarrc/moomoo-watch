"""盘中快照 —— 零 token 线：取数、记数据、判异动、收盘汇总，全程不调 LLM。

契约见 intraday-plan.md。运行形态（计划任务每 30 分钟调一次本脚本）：

    窗口外（周末/假日/盘前/夜盘）→ 记日志即退（exit 0）
    窗口内（美东 09:30–20:00）  → 批量快照「持仓+自选」→ 追加当日 xlsx
                                   → 越阈值才推送（每标的每日只报首次越线）→ exit 0
    窗口后第一跳（≥20:00）      → 读当日 xlsx 汇总 → 推一条收盘汇总 → 打标记 → exit 0

与主管道（main.py）互不相干：不写 data/、不写 outbox/、不碰 state.json；
运行态（异动去重标记 .alerts_<日期>.json、EOD 标记 .eod_<日期>.json）落在
{数据仓根}（config.yaml 的 intraday.store_root），随 Desktop 数据仓天然不进 git。

用法：
    python intraday/snapshot.py               # 计划任务调这个（每 30 分钟一次）
    python intraday/snapshot.py --force       # 无视窗口强跑一轮快照（联调用；照常写当日 xlsx）
    python intraday/snapshot.py --no-push     # 只记数据不推送，也不写去重标记（联调用）
    python intraday/snapshot.py --mode simulate

退出码：0 = 正常（含「窗口外，本轮无事」）；1 = 硬失败（配置/OpenD/写文件）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, time as dtime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 cp1252，中文日志会 UnicodeEncodeError 崩掉（与 main.py 同款处理）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from core import market as market_mod     # noqa: E402
from core import notifier                 # noqa: E402
from core.config import Config            # noqa: E402

log = logging.getLogger("moomoo-watch.intraday")

# 数据契约（与 etl_weekly.py 对齐的唯一定义；xlsx 首行 = 本列序，sheet 名 ticks）
COLUMNS = ["ts", "code", "name", "open", "high", "low",
           "last", "prev_close", "change_pct", "volume", "turnover"]
SHEET = "ticks"


# ===========================================================================
# 纯函数区（可单测；不碰网络 / 不碰 OpenD / 不碰文件系统以外的状态）
# ===========================================================================
def et_zone(cfg) -> "Any":
    """交易所时区。⚠️ 勿写死本地时间——夏/冬令时会漂移。"""
    from zoneinfo import ZoneInfo
    return ZoneInfo(str(cfg.get("intraday.window.tz", "America/New_York")))


def _hhmm(s: Any, fallback: dtime) -> dtime:
    try:
        h, m = str(s).split(":")
        return dtime(int(h), int(m))
    except (ValueError, TypeError, AttributeError):
        return fallback


def classify(et: datetime, cfg, xlsx_exists: bool, eod_sent: bool) -> str:
    """本轮动作：'skip' | 'snapshot' | 'eod'。

    - 周末 / rollup.market_holidays（复用回滚段的美股休市日表）→ skip
    - [start, end) → snapshot；>= end 且当日有数据、未发过汇总 → eod（错过则顺延到下一跳补发）
    """
    if et.weekday() >= 5:
        return "skip"
    if et.strftime("%Y-%m-%d") in set(cfg.get("rollup.market_holidays") or []):
        return "skip"
    t = et.time()
    if t < _hhmm(cfg.get("intraday.window.start", "09:30"), dtime(9, 30)):
        return "skip"
    if t < _hhmm(cfg.get("intraday.window.end", "20:00"), dtime(20, 0)):
        return "snapshot"
    return "eod" if (xlsx_exists and not eod_sent) else "skip"


def build_rows(records: List[Dict[str, Any]], ts: str) -> List[Dict[str, Any]]:
    """futu 快照 records → ticks 行。v1 只收 US.*（窗口按美东判）。

    缺 prev_close / last 的行整行丢弃（change_pct 造不出来，宁缺毋假）。
    """
    rows: List[Dict[str, Any]] = []
    for r in records:
        code = str(r.get("code") or "")
        if not code.startswith("US."):
            log.info("跳过非 US 标的（v1 窗口按美东判）：%s", code)
            continue
        pc = market_mod._f(r.get("prev_close_price"))
        last = market_mod._f(r.get("last_price"))
        if not pc or not last:
            log.warning("跳过 %s：prev_close/last 缺失", code)
            continue
        rows.append({
            "ts": ts, "code": code, "name": str(r.get("name") or ""),
            "open": market_mod._f(r.get("open_price")),
            "high": market_mod._f(r.get("high_price")),
            "low": market_mod._f(r.get("low_price")),
            "last": last, "prev_close": pc,
            "change_pct": round((last - pc) / pc * 100, 3),
            "volume": market_mod._f(r.get("volume")),
            "turnover": market_mod._f(r.get("turnover")),
        })
    return rows


def pick_movers(rows: List[Dict[str, Any]], pos_codes: Set[str],
                thr_pos: float, thr_watch: float) -> List[Dict[str, Any]]:
    """异动 = |change_pct| ≥ 所属组阈值（绝对涨跌幅，不区分多空方向）。"""
    out = []
    for r in rows:
        cp = r.get("change_pct")
        if cp is None:
            continue
        is_pos = r["code"] in pos_codes
        thr = float(thr_pos if is_pos else thr_watch)
        if abs(cp) >= thr:
            out.append({"row": r, "threshold": thr,
                        "group": "持仓" if is_pos else "自选"})
    return out


def _sym(code: str) -> str:
    return code.split(".")[-1]


def _sign_pct(p: float) -> str:
    return f"{'+' if p >= 0 else ''}{p:.1f}%"


def make_title(prefix: str, items: List[tuple], maxlen: int = 40) -> str:
    """标题 ≤40 字且自包含（Server酱只显标题）；放不下就丢尾部标的保省略号。"""
    parts = [f"{_sym(c)} {_sign_pct(p)}" for c, p in items]
    title = prefix + "·".join(parts)
    if len(title) <= maxlen:
        return title
    while parts and len(prefix + "·".join(parts) + "…") > maxlen:
        parts.pop()
    return prefix + "·".join(parts) + "…"


# ===========================================================================
# OpenD 取数（复用 core/market.py 的已验证模式）
# ===========================================================================
def fetch_position_codes(cfg, host: str, port: int) -> Set[str]:
    """实时持仓代码（qty>0）。失败抛异常由调用方降级（只影响持仓组阈值，不致命）。"""
    from futu import TrdEnv, TrdMarket

    mkt = getattr(TrdMarket, str(cfg.get("moomoo.account_market", "US")).upper(), TrdMarket.US)
    picked, _firm, tctx = market_mod._find_account(cfg, host, port, mkt)
    if picked is None:
        return set()
    try:
        acc_id = int(picked["acc_id"])
        env = TrdEnv.REAL if str(picked.get("trd_env")).upper() == "REAL" else TrdEnv.SIMULATE
        ret, pos = tctx.position_list_query(trd_env=env, acc_id=acc_id)
        if ret != 0:
            log.warning("position_list_query 失败：%s", str(pos)[:120])
            return set()
        return {str(r["code"]) for _, r in pos.iterrows() if float(r.get("qty") or 0) > 0}
    finally:
        try:
            tctx.close()
        except Exception:                                          # noqa: BLE001
            pass


def fetch_snapshot_records(cfg, host: str, port: int, codes: List[str]) -> List[Dict[str, Any]]:
    """批量快照。🚩 批量是全有或全无：chunk 内一个无权限 → 整 chunk 失败 → 逐标的回退。"""
    from futu import OpenQuoteContext

    qctx = OpenQuoteContext(host=host, port=port)
    rows: List[Dict[str, Any]] = []
    try:
        no_quote: List[str] = []
        for i in range(0, len(codes), 400):
            chunk = codes[i:i + 400]
            ret, snap = qctx.get_market_snapshot(chunk)
            if ret == 0:
                rows.extend(snap.to_dict("records"))
                continue
            log.warning("批量快照失败（%s）→ 逐标的回退", str(snap)[:90])
            for code in chunk:
                ret1, snap1 = qctx.get_market_snapshot([code])
                if ret1 == 0:
                    rows.extend(snap1.to_dict("records"))
                else:
                    no_quote.append(code)
        if no_quote:
            log.warning("以下标的取不到行情：%s", no_quote)
        return rows
    finally:
        try:
            qctx.close()
        except Exception:                                          # noqa: BLE001
            pass


# ===========================================================================
# 数据仓 IO（Desktop；即写即关句柄 —— OneDrive 红线）
# ===========================================================================
def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def append_rows(xlsx_path: Path, rows: List[Dict[str, Any]]) -> bool:
    """追加写当日 xlsx，写完即关句柄。OneDrive 同步锁 → 重试一次。"""
    from openpyxl import Workbook, load_workbook

    for attempt in (1, 2):
        try:
            if xlsx_path.exists():
                wb = load_workbook(xlsx_path)
            else:
                wb = Workbook()
                wb.active.title = SHEET
                wb.active.append(COLUMNS)
            if SHEET not in wb.sheetnames:
                wb.create_sheet(SHEET).append(COLUMNS)
            ws = wb[SHEET]
            if ws.max_row < 1 or ws["A1"].value != COLUMNS[0]:
                log.error("%s 表头不是 ticks 契约（A1=%r），拒写防串位", xlsx_path.name, ws["A1"].value)
                return False
            for r in rows:
                ws.append([r[c] for c in COLUMNS])
            wb.save(xlsx_path)
            wb.close()
            return True
        except Exception as exc:                                   # noqa: BLE001
            log.warning("xlsx 写入第 %d 次失败：%s", attempt, exc)
            time.sleep(2)
    log.error("xlsx 两次写入均失败：%s（本轮数据丢失，exit 1 让调度器留痕）", xlsx_path)
    return False


def summarize_day(xlsx_path: Path) -> List[Dict[str, Any]]:
    """读回当日 xlsx，聚合出每股终值 / 日高低 / 轮数。"""
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb[SHEET]
        it = ws.iter_rows(values_only=True)
        header = list(next(it, ()) or ())
        if header[: len(COLUMNS)] != COLUMNS:
            raise ValueError(f"表头与 ticks 契约不符：{header}")
        per: Dict[str, Dict[str, Any]] = {}
        for vals in it:
            d = dict(zip(header, vals))
            c = str(d.get("code"))
            a = per.setdefault(c, {"code": c, "name": str(d.get("name") or ""),
                                   "last": None, "change_pct": None,
                                   "high": None, "low": None, "rounds": 0})
            a["rounds"] += 1
            a["last"] = d.get("last")
            a["change_pct"] = d.get("change_pct")
            hi, lo = d.get("high"), d.get("low")
            if hi is not None:
                a["high"] = hi if a["high"] is None else max(a["high"], hi)
            if lo is not None:
                a["low"] = lo if a["low"] is None else min(a["low"], lo)
        return list(per.values())
    finally:
        wb.close()


# ===========================================================================
# 推送文案
# ===========================================================================
def _alert_line(m: Dict[str, Any]) -> str:
    r = m["row"]
    return (f"{_sym(r['code'])} {r['name']} {_sign_pct(r['change_pct'])}"
            f"（{m['group']}阈值 ±{m['threshold']:g}%）现价 {r['last']}")


def _eod_body(agg: List[Dict[str, Any]]) -> str:
    lines = []
    for a in sorted(agg, key=lambda x: abs(x["change_pct"] or 0), reverse=True):
        hi = f"{a['high']:g}" if a["high"] is not None else "无"
        lo = f"{a['low']:g}" if a["low"] is not None else "无"
        lines.append(f"{_sym(a['code'])} 收 {a['last']}，{_sign_pct(a['change_pct'] or 0)}"
                     f"（日高 {hi} / 日低 {lo}）")
    rounds = max((a["rounds"] for a in agg), default=0)
    lines.append(f"共 {len(agg)} 标的 · {rounds} 轮快照 · 数据截止收盘")
    return "\n".join(lines)


# ===========================================================================
# 主流程
# ===========================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="moomoo-watch 盘中快照（零 token 线）")
    p.add_argument("--config", default=None, help="配置文件路径（默认项目内 config.yaml）")
    p.add_argument("--mode", choices=["simulate", "live"], default=None)
    p.add_argument("--no-push", action="store_true", help="不推送、不写去重标记（联调用）")
    p.add_argument("--force", action="store_true", help="无视交易窗口强跑一轮快照（联调用）")
    return p.parse_args()


def setup_logging(cfg) -> None:
    logdir = cfg.path_for("logs")
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = TimedRotatingFileHandler(logdir / "intraday.log", when="midnight",
                                  backupCount=14, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]


def main() -> int:
    args = parse_args()
    try:
        cfg = Config(Path(args.config) if args.config else None)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[致命] 配置加载失败：{exc}", file=sys.stderr)
        return 1
    if args.mode:
        cfg.raw["mode"] = args.mode

    setup_logging(cfg)
    log.info("=" * 58)
    log.info("盘中快照启动 | mode=%s force=%s no_push=%s", cfg.mode, args.force, args.no_push)

    if not cfg.get("intraday.enabled", True):
        log.info("intraday.enabled = false → 本轮退出")
        return 0
    root_str = str(cfg.get("intraday.store_root") or "").strip()
    if not root_str:
        log.error("config.yaml 缺 intraday.store_root（数据仓根）——见 intraday-plan.md 第 4 步")
        return 1
    store = Path(root_str)
    store.mkdir(parents=True, exist_ok=True)

    et = datetime.now(et_zone(cfg))
    day = et.strftime("%Y-%m-%d")
    xlsx_path = store / f"stocks_{day}.xlsx"
    alerts_path = store / f".alerts_{day}.json"
    eod_path = store / f".eod_{day}.json"

    kind = "snapshot" if args.force else classify(et, cfg, xlsx_path.exists(), eod_path.exists())
    log.info("美东 %s → 本轮动作：%s", et.strftime("%Y-%m-%d %H:%M:%S %Z"), kind)
    if kind == "skip":
        return 0
    if kind == "eod" and not cfg.get("intraday.eod_summary", True):
        log.info("intraday.eod_summary = false → 不发收盘汇总")
        return 0

    probe = market_mod.opend_alive(cfg)
    if not probe["alive"]:
        log.error("OpenD 不可用：%s", probe["detail"])
        return 1
    host = str(cfg.get("moomoo.host", "127.0.0.1"))
    port = int(cfg.get("moomoo.port", 11111))

    # ---- EOD：只读当日 xlsx 汇总，不新增快照行 -------------------------------
    if kind == "eod":
        try:
            agg = summarize_day(xlsx_path)
        except Exception as exc:                                   # noqa: BLE001
            log.error("当日 xlsx 汇总失败（下轮重试）：%s", exc)
            return 0
        if not agg:
            log.warning("当日 xlsx 为空 → 写 EOD 标记防止反复重试")
            _write_json(eod_path, {"at": et.isoformat(timespec="seconds"), "rows": 0})
            return 0
        top = [(a["code"], a["change_pct"] or 0) for a in
               sorted(agg, key=lambda x: abs(x["change_pct"] or 0), reverse=True)[:3]]
        title = make_title("📊收盘·", top)
        if args.no_push:
            log.info("[no-push] 本会发收盘汇总：%s", title)
            return 0
        res = notifier.send(cfg, title, _eod_body(agg), "normal")
        if res.get("ok"):
            _write_json(eod_path, {"at": et.isoformat(timespec="seconds"),
                                   "rows": len(agg),
                                   "simulated": bool(res.get("simulated"))})
            log.info("收盘汇总已发：%s%s", title,
                     "（simulate，标记照写）" if res.get("simulated") else "")
        else:
            log.error("收盘汇总推送失败（标记未写，下轮重试）：%s", res.get("error"))
        return 0

    # ---- snapshot：持仓代码实时取（避免 config 双份漂移），自选用 config -------
    try:
        pos_codes = fetch_position_codes(cfg, host, port)
        log.info("持仓代码 %d 个：%s", len(pos_codes), sorted(pos_codes))
    except Exception as exc:                                       # noqa: BLE001
        log.warning("持仓代码获取失败（退化为全部按自选组阈值）：%s", exc)
        pos_codes = set()

    watch_codes = [str(c) for c in (cfg.get("moomoo.watchlist") or [])]
    codes = list(dict.fromkeys([*sorted(pos_codes), *watch_codes]))
    us_codes = [c for c in codes if c.startswith("US.")]
    non_us = [c for c in codes if not c.startswith("US.")]
    if non_us:
        log.info("非 US 标的不记（v1 窗口按美东判）：%s", non_us)
    if not us_codes:
        log.warning("没有可记的 US 标的，本轮退出")
        return 0

    records = fetch_snapshot_records(cfg, host, port, us_codes)
    ts = et.strftime("%Y-%m-%d %H:%M:%S")
    rows = build_rows(records, ts)
    if not rows:
        log.warning("本轮快照 0 行（行情权限？）——不写文件、不判异动")
        return 0
    if not append_rows(xlsx_path, rows):
        return 1
    log.info("已写 %s：%d 行", xlsx_path.name, len(rows))

    movers = pick_movers(rows, pos_codes,
                         float(cfg.get("intraday.thresholds.positions_pct", 3.0)),
                         float(cfg.get("intraday.thresholds.watchlist_pct", 5.0)))
    markers = _read_json(alerts_path)
    fresh = [m for m in movers if m["row"]["code"] not in markers]
    if not fresh:
        log.info("无异动推送（越线 %d 个均为已报过/未越线）", len(movers))
        return 0
    log.info("异动 %d 个（新 %d）：%s", len(movers), len(fresh),
             [(m["row"]["code"], m["row"]["change_pct"]) for m in fresh])
    if args.no_push:
        log.info("[no-push] 本会推送，去重标记不写")
        return 0

    title = make_title("⚡", [(m["row"]["code"], m["row"]["change_pct"]) for m in fresh])
    body = "\n".join(_alert_line(m) for m in fresh)
    body += "\n（盘中快照·零 token；每标的每日只报首次越线）"
    res = notifier.send(cfg, title, body, "high")
    if res.get("ok"):
        for m in fresh:
            markers[m["row"]["code"]] = {"pct": m["row"]["change_pct"], "at": ts}
        _write_json(alerts_path, markers)
        log.info("异动推送成功：%s%s", title,
                 "（simulate，标记照写）" if res.get("simulated") else "")
    else:
        log.error("异动推送失败（标记未写，下轮重试）：%s", res.get("error"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                                        # noqa: BLE001 —— 绝不让调度器看到崩溃栈
        logging.getLogger("moomoo-watch.intraday").exception("未捕获异常：%s", exc)
        sys.exit(1)
