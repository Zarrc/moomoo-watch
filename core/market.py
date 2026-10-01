"""行情 / 持仓 / 账户 —— 两个数据源：fixture（离线样例）与 futu（OpenD）。

⚠️ 诚实声明：**futu 分支在本机未经验证** —— 写这份代码时本机没有安装 FutuOpenD，
也没有可登录的账户。fixture 分支是跑通过的。futu 分支按官方接口签名编写并做了
防御式降级，但**首次真机联调时请预期需要修**。

两条已知的坑（来自 skill §6）：
- OpenD 必须**已经在跑且已登录**，否则整条管道静默失败 → 所以每个入口都带连通性自检。
- 行情权限 ≠ App 权限；账户查询在 SDK 直用场景下**通常**不需要 unlock_trade，但按
  「可能需解锁」预留了开关。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 连通性自检
# ---------------------------------------------------------------------------
def opend_alive(cfg) -> Dict[str, Any]:
    """探一下 OpenD 是否可达。返回 {alive, detail}。不抛异常。"""
    import socket

    host = cfg.get("moomoo.host", "127.0.0.1")
    port = int(cfg.get("moomoo.port", 11111))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect((host, port))
        return {"alive": True, "detail": f"{host}:{port} 可连接"}
    except OSError as exc:
        return {"alive": False, "detail": f"{host}:{port} 连不上（{exc}）—— OpenD 是否在跑且已登录？"}
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# fixture 源（离线，跑得通）
# ---------------------------------------------------------------------------
def _load_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("fixture 读取失败 %s：%s", path, exc)
        return None


def _from_fixture(cfg, now: datetime | None = None) -> Dict[str, Any]:
    fx = cfg.path_for("fixtures")
    account = _load_json(fx / "account.json") or {}
    positions = _load_json(fx / "positions.json") or []
    watchlist = _load_json(fx / "watchlist.json") or []

    indicators: Dict[str, Any] = {}
    kdir = fx / "klines"
    if kdir.is_dir():
        for f in sorted(kdir.glob("*.json")):
            bars = _load_json(f) or []
            indicators[f.stem] = bars

    return {
        "account": account,
        "positions": positions,
        "watchlist": watchlist,
        "klines": indicators,
        "asof": (now or datetime.now(CST)).isoformat(timespec="seconds"),
        "status": {"source": "fixture", "ok": True, "detail": "离线样例数据"},
    }


# ---------------------------------------------------------------------------
# 账户定位 —— 这是最容易错的一步
# ---------------------------------------------------------------------------
# 🚩 2026-09-30 实踩（Moomoo MY 账户）：`security_firm` **决定了你能看到哪些账户**。
#    传 FUTUSECURITIES（moomoo 默认/多数示例里的值）→ **只看到模拟盘**；
#    传 FUTUMY → 才看到实盘。硬编码任何一个都是错的。
#    症状极具误导性：position_list_query 报 "Nonexisting acc_id"，
#    看着像账户不存在，实际是 security_firm / trd_env 不匹配。
CANDIDATE_FIRMS = ["FUTUMY", "FUTUSECURITIES", "FUTUINC", "FUTUSG",
                   "FUTUJP", "FUTUAU", "FUTUCA"]


def _f(v) -> Any:
    """安全转 float；None / 空 / 'N/A' 一律返回 None（futu 会拿 'N/A' 当缺失值）。"""
    if v is None or v == "" or (isinstance(v, str) and v.strip().upper() in ("N/A", "NAN")):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _find_account(cfg, host: str, port: int, market_enum):
    """遍历 security_firm 候选，返回 (acc_row, firm_name, ctx)。**优先实盘**。

    调用方负责在结束后 ctx.close()。找不到任何账户返回 (None, None, None)。
    """
    from futu import OpenSecTradeContext, SecurityFirm

    configured = cfg.get("moomoo.security_firm")
    firms = ([configured] if configured else []) + [f for f in CANDIDATE_FIRMS if f != configured]

    fallback = None            # 只有模拟盘时的兜底
    for fname in firms:
        if not fname or not hasattr(SecurityFirm, fname):
            continue
        try:
            ctx = OpenSecTradeContext(
                filter_trdmarket=market_enum, host=host, port=port,
                security_firm=getattr(SecurityFirm, fname),
            )
        except Exception as exc:                            # noqa: BLE001
            log.debug("security_firm=%s 建连失败：%s", fname, exc)
            continue
        try:
            ret, acc = ctx.get_acc_list()
        except Exception as exc:                            # noqa: BLE001
            log.debug("security_firm=%s get_acc_list 异常：%s", fname, exc)
            ctx.close()
            continue
        if ret != 0 or not len(acc):
            ctx.close()
            continue
        for _, row in acc.iterrows():
            if str(row.get("trd_env")).upper() == "REAL":
                log.info("账户定位：security_firm=%s → 实盘账户 acc_id=%s", fname, row.get("acc_id"))
                return row, fname, ctx
        if fallback is None:
            fallback = (acc.iloc[0], fname, ctx)
        else:
            ctx.close()

    if fallback:
        log.warning("账户定位：未找到实盘账户，退回模拟盘（security_firm=%s）。"
                    "**下列数字不是真实持仓**。", fallback[1])
    return fallback or (None, None, None)


# ---------------------------------------------------------------------------
# futu 源（2026-09-30 首次真机验证；Moomoo MY + 美股行情 + 模拟盘账户）
# ---------------------------------------------------------------------------
def _from_futu(cfg, now: datetime | None = None) -> Dict[str, Any]:
    status: Dict[str, Any] = {"source": "futu", "ok": False, "detail": ""}
    try:
        from futu import (AuType, KLType, OpenQuoteContext, OpenSecTradeContext,
                          SecurityFirm, TrdEnv, TrdMarket)
    except ImportError:
        status["detail"] = "futu-api 未安装（pip install futu-api）"
        return {"account": {}, "positions": [], "watchlist": [], "klines": {}, "status": status}

    host = cfg.get("moomoo.host", "127.0.0.1")
    port = int(cfg.get("moomoo.port", 11111))

    probe = opend_alive(cfg)
    if not probe["alive"]:
        status["detail"] = probe["detail"]
        return {"account": {}, "positions": [], "watchlist": [], "klines": {}, "status": status}

    watch_codes = list(cfg.get("moomoo.watchlist") or [])
    ktype = getattr(KLType, str(cfg.get("indicators.kline", "K_60M")), KLType.K_60M)
    max_count = int(cfg.get("indicators.max_count", 200))

    account: Dict[str, Any] = {}
    positions: List[Dict[str, Any]] = []
    watchlist: List[Dict[str, Any]] = []
    klines: Dict[str, List[Dict[str, Any]]] = {}

    # ---- 行情 ----
    try:
        qctx = OpenQuoteContext(host=host, port=port)
    except Exception as exc:                                   # noqa: BLE001
        status["detail"] = f"OpenQuoteContext 初始化失败：{exc}"
        return {"account": {}, "positions": [], "watchlist": [], "klines": {}, "status": status}

    try:
        # 持仓 + 自选一起取快照（单次上限 400 个标的，限频 60 次/30 秒）
        tctx = None
        all_codes = list(watch_codes)
        try:
            # 账户所在的 trdmarket 由配置决定（默认 US）——注意这**不是**你的账户归属地，
            # 而是你要看哪个市场的账户。MY 客户的实盘账户在 US 过滤下也能找到。
            mkt_enum = getattr(TrdMarket, str(cfg.get("moomoo.account_market", "US")).upper(),
                               TrdMarket.US)
            picked, firm_used, tctx = _find_account(cfg, host, port, mkt_enum)
            if picked is not None:
                status["security_firm"] = firm_used
                acc_id = int(picked["acc_id"])
                acc_env = TrdEnv.REAL if str(picked.get("trd_env")).upper() == "REAL" else TrdEnv.SIMULATE
                status["acc_env"] = "REAL" if acc_env == TrdEnv.REAL else "SIMULATE"

                # 账户资产（需求 ④）
                ret, info = tctx.accinfo_query(trd_env=acc_env, acc_id=acc_id)
                if ret == 0 and len(info):
                    row = info.iloc[0]
                    for src, dst in (("total_assets", "equity"), ("cash", "cash"),
                                     ("securities_assets", "securities_value"),
                                     ("market_val", "market_value")):
                        if src in info.columns:
                            val = _f(row[src])
                            if val is not None:
                                account[dst] = val
                    # 🚩 账户计价币种 —— 2026-09-30 实踩：Moomoo MY 客户的账户是 **HKD 计价**，
                    #    但持仓是 USD / MYR 混合。直接拿持仓市值跟 equity 比会得出
                    #    「差了一大截」的假结论（当时差 10 倍）。**必须先看币种。**
                    account["currency"] = row.get("currency")
                    # 分币种资产快照，用于对账（值是各币种原值，未换算）
                    # 只认**币种**列：`total_assets` / `securities_assets` / `fund_assets` /
                    # `bond_assets` 也以 `_assets` 结尾，但它们不是币种，混进来会污染对账。
                    _not_ccy = {"total_assets", "securities_assets", "fund_assets", "bond_assets"}
                    by_ccy = {}
                    for c in info.columns:
                        if c.endswith("_assets") and c not in _not_ccy:
                            v = _f(row[c])
                            if v:
                                by_ccy[c[:-7].upper()] = v
                    if by_ccy:
                        account["assets_by_currency"] = by_ccy
                else:
                    log.warning("accinfo_query 失败：%s", info)

                ret, pos = tctx.position_list_query(trd_env=acc_env, acc_id=acc_id)
                if ret == 0:
                    for _, r in pos.iterrows():
                        code = str(r.get("code"))
                        all_codes.append(code)
                        positions.append({
                            "code": code,
                            "name": r.get("stock_name"),
                            "direction": str(r.get("position_side")),
                            "qty": float(r.get("qty") or 0),
                            "cost": round(float(r.get("cost_price") or 0), 4),
                            "currency": r.get("currency"),
                            # 🚩 持仓接口**自带价格/市值**，是实时行情接口没权限时的兜底。
                            #    2026-09-30 实踩：MY 股票在 get_market_snapshot 报无权限，
                            #    但这里 nominal_price / market_val 一直都是有值的 ——
                            #    只认快照价等于把手上已有的数据白扔了。
                            "pos_price": _f(r.get("nominal_price")),
                            "pos_market_val": _f(r.get("market_val")),
                            "pos_pl_val": _f(r.get("pl_val")),
                            "price_source": "position_api",
                        })
                else:
                    log.warning("position_list_query 失败：%s", pos)
            else:
                log.warning("遍历全部 security_firm 候选后仍未找到任何账户 → 跳过持仓，仅出行情")
        except Exception as exc:                               # noqa: BLE001
            log.warning("账户/持仓查询失败（继续，仅出行情）：%s", exc)
        finally:
            if tctx is not None:
                try:
                    tctx.close()
                except Exception:                              # noqa: BLE001
                    pass

        # 用快照补现价
        # 🚩 2026-09-30 实踩：批量快照是**全有或全无** —— 只要 chunk 里有一个标的没行情权限，
        #    整个 chunk 返回 ret!=0，**所有标的的报价一起丢**。
        #    （实例：持仓含 MY.1155 但 MY 行情无权限 → 连 US 持仓的价格也拿不到。）
        #    所以批量失败必须**逐标的回退**，让没权限的那些单独失败。
        snap_rows: List[Dict[str, Any]] = []
        no_quote: List[str] = []
        for i in range(0, len(all_codes), 400):
            chunk = all_codes[i:i + 400]
            ret, snap = qctx.get_market_snapshot(chunk)
            if ret == 0:
                snap_rows.extend(snap.to_dict("records"))
                continue
            log.warning("批量快照失败（%s）→ 逐标的回退重试", str(snap)[:90])
            for code in chunk:
                ret1, snap1 = qctx.get_market_snapshot([code])
                if ret1 == 0:
                    snap_rows.extend(snap1.to_dict("records"))
                else:
                    no_quote.append(code)
                    log.warning("  %s 取不到行情：%s", code, str(snap1)[:90])

        if no_quote:
            status["no_quote"] = no_quote
            log.warning("以下 %d 个标的无行情权限，将只显示成本/数量、不显示现价：%s",
                        len(no_quote), no_quote)

        for r in snap_rows:
            code = str(r["code"])
            row = {
                "code": code,
                "name": r.get("name"),
                "last": float(r.get("last_price") or 0),
                "prev_close": float(r.get("prev_close_price") or 0),
                "volume": float(r.get("volume") or 0),
            }
            pc = row["prev_close"]
            row["change_pct"] = round((row["last"] - pc) / pc * 100, 3) if pc else None
            if code in watch_codes:
                watchlist.append(row)
            for p in positions:
                if p["code"] == code:
                    p.update({"last": row["last"], "change_pct": row["change_pct"],
                              "price_source": "snapshot"})

        # 快照拿不到价的持仓 → 退回持仓接口自带的 nominal_price。
        # 这不是实时行情（可能是延迟/收盘价），所以**必须打标**，让下游知道口径不同。
        for p in positions:
            if p.get("last") is None and p.get("pos_price"):
                p["last"] = p["pos_price"]
                p["price_source"] = "position_api"
                log.info("  %s 用持仓接口价格兜底：%s（非实时行情）", p["code"], p["pos_price"])

        # 逐标的取 K 线（用于算指标）—— 注意会消耗历史 K 线额度
        #
        # 🚩🚩 必须显式传 start / end！2026-09-30 实踩：
        #   只给 max_count 不传日期时，OpenD 返回的是**窗口里最旧的一段**，
        #   不是最新的。当时拿到 2025-09-30 ~ 2025-11-07 的数据（滞后 11 个月），
        #   于是 MA50 / ATR / RSI 全在陈年数据上算出 —— 指标全废，而且**不报错**。
        #   （这条是被子代理「K线数据停在2025-11-07」发现的，见 outbox 记录。）
        #
        # 🚩🚩 而且**单页不够**：`request_history_kline` 是**从 start 往后**返回
        #   `max_count` 根，不是「取最近 max_count 根」。
        #   200 根 60M 线只覆盖约 44 天 → 只取一页的话数据会停在 44 天前（又是一种静默过期）。
        #   所以要么 start 设得很近，要么**翻页取到最后**。这里用翻页，稳妥。
        today = datetime.now(CST)
        end_date = today.strftime("%Y-%m-%d")
        start_date = (today - timedelta(days=180)).strftime("%Y-%m-%d")
        page_limit = 1000          # 单次上限
        for code in all_codes:
            if code in no_quote:          # 没行情权限的标的，K 线必然也拿不到，省一次调用
                continue
            all_bars: List[Dict[str, Any]] = []
            page_key = None
            for _ in range(10):           # 最多翻 10 页，防跑飞
                ret, kl, page_key = qctx.request_history_kline(
                    code, start=start_date, end=end_date, ktype=ktype,
                    max_count=page_limit, autype=AuType.QFQ, page_req_key=page_key,
                )
                if ret != 0:
                    log.warning("K 线获取失败 %s：%s", code, str(kl)[:90])
                    break
                all_bars.extend(
                    {
                        "time": str(r["time_key"]),
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r["volume"]),
                    }
                    for _, r in kl.iterrows()
                )
                if not page_key:
                    break
            if all_bars:
                # 只保留最后 max_count 根（最靠近今天的）
                klines[code] = all_bars[-max_count:]
                log.debug("  %s K线 %d 根，末端 %s", code, len(klines[code]),
                          klines[code][-1]["time"])

        status.update({"ok": True, "detail": f"取到 {len(positions)} 个持仓 / {len(watchlist)} 个自选"})
    except Exception as exc:                                   # noqa: BLE001
        status["detail"] = f"行情获取异常：{exc}"
    finally:
        try:
            qctx.close()
        except Exception:                                      # noqa: BLE001
            pass

    return {
        "account": account, "positions": positions, "watchlist": watchlist,
        "klines": klines, "asof": (now or datetime.now(CST)).isoformat(timespec="seconds"),
        "status": status,
    }


# ---------------------------------------------------------------------------
def collect(cfg, now: datetime | None = None) -> Dict[str, Any]:
    """取数。`now` 可选 —— 只影响 `asof` 时间戳（测试用可控时钟）。"""
    if cfg.source == "futu":
        return _from_futu(cfg, now)
    return _from_fixture(cfg, now)


def enrich(cfg, raw: Dict[str, Any]) -> Dict[str, Any]:
    """对持仓/自选补算浮盈亏、权重、集中度，并对每个标的算指标。"""
    from . import indicators as ind

    account = dict(raw.get("account") or {})
    positions = list(raw.get("positions") or [])
    watchlist = list(raw.get("watchlist") or [])
    klines = raw.get("klines") or {}

    equity = float(account.get("equity") or cfg.get("risk.equity", 0) or 0)

    # 涨跌幅：统一在这里补算，两个数据源口径一致
    # （futu 源在快照阶段已算过，这里只兜 fixture 源与缺值时的情况）
    for row in list(positions) + list(watchlist):
        if row.get("change_pct") is None:
            pc = row.get("prev_close")
            last = row.get("last")
            if pc and last:
                row["change_pct"] = round((float(last) - float(pc)) / float(pc) * 100, 3)

    # 持仓逐条补算
    # 🚩🚩 现价缺失时必须**如实留空**，绝不能拿 0 当价格。
    #    2026-09-30 实踩：MY 持仓无行情权限导致 last=None，当时按 0 处理 →
    #    凭空造出「亏损 = 成本 × 数量」的假浮亏（US.O 被报成 -233.54）。
    #    在这个场景里，报个假数字比报「没有」危险得多。宁可留空并说明。
    mv_total = 0.0
    quoted = 0
    for p in positions:
        qty = float(p.get("qty") or 0)
        cost = float(p.get("cost") or 0)
        raw_last = p.get("last")
        last = float(raw_last) if raw_last not in (None, "") else 0.0
        if last <= 0:
            p.update({"market_value": None, "pnl": None, "pnl_pct": None,
                      "weight": None, "quote_available": False})
            continue
        quoted += 1
        mv_total += abs(last * qty)
        p["market_value"] = round(abs(last * qty), 2)
        p["pnl"] = round((last - cost) * qty, 2)
        p["pnl_pct"] = round((last - cost) / cost * 100, 3) if cost else None
        p["quote_available"] = True
        # ⚠️ 与 `quote_available` 区分开：有价 ≠ 有**实时行情**。
        # 价可能来自持仓接口（延迟/收盘）。子代理实指过这两处口径打架，故拆成两个字段。
        p["realtime_quote"] = p.get("price_source") == "snapshot"

    for p in positions:
        if p.get("market_value") is None:
            p["concentration_breach"] = False
            continue
        p["weight"] = round(p["market_value"] / equity, 4) if equity else None
        p["concentration_breach"] = bool(
            p["weight"] is not None and p["weight"] > float(cfg.get("risk.max_position_pct", 0.35))
        )

    # 指标
    ind_map: Dict[str, Any] = {}
    for code, bars in klines.items():
        ind_map[code] = ind.sanitize(ind.compute(bars, cfg))

    # K 线陈旧检测 —— 脚本必须自己兜住。
    # 理由：K 线取错区间时**不报错**，指标照样算得出来，只是全是陈年数据。
    # 2026-09-30 那次是子代理读出来的；不能假设每次都这么走运。
    stale_klines: List[Dict[str, Any]] = []
    limit_h = float(cfg.get("risk.stale_kline_hours", 72))
    for code, bars in klines.items():
        if not bars:
            continue
        try:
            dt = datetime.strptime(str(bars[-1].get("time"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except (ValueError, TypeError):
            continue
        age_h = (datetime.now(CST) - dt).total_seconds() / 3600.0
        if age_h > limit_h:
            stale_klines.append({"code": code, "last_bar": str(bars[-1].get("time")),
                                 "age_hours": round(age_h, 1)})
    if stale_klines:
        log.warning("⚠️ %d 个标的的 K 线已过期（阈值 %sh）：%s",
                    len(stale_klines), limit_h,
                    [(s["code"], s["last_bar"][:10]) for s in stale_klines])
    for p in positions:
        p["indicators"] = ind_map.get(str(p.get("code")))
    for w in watchlist:
        w["indicators"] = ind_map.get(str(w.get("code")))

    unquoted = len(positions) - quoted

    # 🚩 跨币种不能相加！持仓可能是 USD / MYR 混合，直接求和得的数字没有意义。
    #    2026-09-30 实踩：把 1,493 USD 与 999 MYR 相加，还拿去跟 **HKD 计价**的 equity 比，
    #    得出「差了一大截」的假结论。**先看币种，再谈合计。**
    mv_by_ccy: Dict[str, float] = {}
    pnl_by_ccy: Dict[str, float] = {}
    for p in positions:
        c = str(p.get("currency") or "?")
        if p.get("market_value") is not None:
            mv_by_ccy[c] = round(mv_by_ccy.get(c, 0.0) + p["market_value"], 2)
        if p.get("pnl") is not None:
            pnl_by_ccy[c] = round(pnl_by_ccy.get(c, 0.0) + p["pnl"], 2)
    single_ccy = len(mv_by_ccy) <= 1

    account.update({
        "equity": equity,
        "currency": account.get("currency"),
        "market_value_by_currency": mv_by_ccy,
        "pnl_by_currency": pnl_by_ccy,
        # 只有单一币种时才给标量合计；多币种一律 None，免得下游拿去用（或相加）
        "total_market_value": round(mv_total, 2) if single_ccy else None,
        "total_pnl": (round(sum(p["pnl"] for p in positions if p.get("pnl") is not None), 2)
                      if single_ccy else None),
        "mixed_currency": not single_ccy,
        "position_count": len(positions),
        "quoted_position_count": quoted,
        "unquoted_position_count": unquoted,
        # 有标的没行情权限时，下面的合计**只覆盖有报价的部分** —— 必须显式标出来，
        # 否则子代理会把「部分合计」当成「全部」，报出一个偏小的仓位。
        "totals_complete": unquoted == 0,
        "equity_note": "equity 用账户计价币种（见 currency）；持仓按各自币种分列，两者不可直接相减",
    })
    if single_ccy and equity and account["total_pnl"] is not None and account["totals_complete"]:
        account["total_pnl_pct"] = round(account["total_pnl"] / equity * 100, 3)

    status = dict(raw.get("status") or {})
    if stale_klines:
        status["stale_klines"] = stale_klines

    return {
        "account": account, "positions": positions, "watchlist": watchlist,
        "klines": klines, "asof": raw.get("asof"), "status": status,
        "stale_klines": stale_klines,
    }
