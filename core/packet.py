"""组装数据包 —— 这是**脚本与子代理之间唯一的接口**。

契约见 .claude/agents/portfolio-watch.md 的「数据包契约」节。改这里必须同步改那里，
否则子代理会在字段上踩空（它被明确要求：读不到 schema 就停下来报告，不许猜）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

SCHEMA_VERSION = "1.0"

# 塞进数据包、供子代理自解释的 schema 摘要（它读不到就别猜）
_SCHEMA = {
    "version": SCHEMA_VERSION,
    "enums": {
        "trigger": ["brief", "alert", "ask"],
        "ma_slope": ["up", "down", "flat", None],
        "price_vs_ma": ["above", "below", "equal", None],
        # 预测块取值域（见 rollup.py / 子代理契约）——写超出域的值会被脚本拒收
        "call_direction": ["up", "down", "flat"],
        "call_invalidation_type": ["close_below", "close_above"],
    },
    "units": {
        "weight": "市值 / 净值，小数（0.38 = 38%）",
        "pnl_pct": "百分比数值（-2.5 表示 -2.5%）",
        "distance_in_atr": "价格距 MA 的 ATR 倍数",
        "minutes_until": "距今分钟数，负值 = 已过去",
        "volume_ratio": "最新量 / 前 20 根均量",
        "fed_probability": "0–1 的小数（加息/按兵/降息概率）",
    },
    "calendar_fields": ["name", "at", "minutes_until", "high_risk", "country", "importance",
                        "actual", "forecast", "prior"],
    "macro_note": "macro 为宏观快照：fed_watch（目标利率 + 加息/按兵/降息概率，主刻度）· "
                  "dot_plot · indicators · series。`macro._fixture=true` 表示**样例数据**，"
                  "任何输出必须据此标注「样例数据」，绝不当作真实数据。取不到的子键为 null 或 {}。",
    "note": "所有数字均由脚本算好。字段缺失即为「脚本没取到」，不要推测、不要估算。",
}


def _build_alerts(cfg, enriched: Dict[str, Any], events: List[Dict[str, Any]],
                  stale: bool, stale_detail: str) -> List[Dict[str, Any]]:
    """确定性告警 —— 阈值判断，全部由脚本做，子代理只负责解读。"""
    alerts: List[Dict[str, Any]] = []

    if stale:
        alerts.append({"level": "warn", "kind": "stale_data", "message": stale_detail})

    # K 线过期 → 基于它算的指标全部不可信，必须让子代理知道（否则它会把陈年 MA 当实时用）
    for s in enriched.get("stale_klines") or []:
        alerts.append({
            "level": "high", "kind": "stale_kline", "code": s["code"],
            "message": f"{s['code']} 的 K 线停在 {str(s['last_bar'])[:10]}"
                       f"（约 {s['age_hours']:.0f} 小时前）→ 该标的的 MA/ATR/RSI **不可信**，不要据此判断",
        })

    acct = enriched.get("account") or {}

    # 多币种账户：任何「总市值 / 总盈亏」都是跨币种相加的假数 —— 必须显式禁止使用
    if acct.get("mixed_currency"):
        alerts.append({
            "level": "warn", "kind": "mixed_currency",
            "message": f"账户为多币种（{acct.get('market_value_by_currency')}），"
                       f"计价币种 {acct.get('currency')} → **不存在单一的总市值/总盈亏**，"
                       f"请按币种分列，不要相加、也不要与 equity 相减",
        })

    # 用非实时价兜底的持仓 —— 口径不同，必须标出来
    for p in enriched.get("positions") or []:
        if p.get("price_source") == "position_api" and p.get("last"):
            alerts.append({
                "level": "info", "kind": "delayed_price", "code": p["code"],
                "message": f"{p['code']} 的价格取自持仓接口（非实时行情接口）—— 口径为延迟/收盘价，非盘中实时",
            })

    # 有持仓取不到行情时，合计是「部分合计」—— 必须显式告诉子代理，别让它当全量报
    if acct and acct.get("totals_complete") is False:
        alerts.append({
            "level": "warn", "kind": "missing_quotes",
            "message": f"{acct.get('unquoted_position_count')} 个持仓取不到行情"
                       f"（多为该市场无 API 行情权限）→ 账户合计**只覆盖有报价的部分**，不是全量",
        })

    for e in events:
        if e["high_risk"] and 0 <= e["minutes_until"] <= int(cfg.get("calendar.alert_window_minutes", 60)):
            alerts.append({
                "level": "high", "kind": "event_window",
                "message": f"{e['name']} 将在 {e['minutes_until']} 分钟后发布",
            })

    for p in enriched.get("positions") or []:
        if p.get("concentration_breach"):
            alerts.append({
                "level": "high", "kind": "concentration",
                "code": p["code"],
                "message": f"{p['code']} 占净值 {p['weight']:.1%}，超过阈值 "
                           f"{float(cfg.get('risk.max_position_pct', 0.35)):.0%}",
            })
        chg = p.get("change_pct")
        if chg is not None and abs(chg) >= 5:
            alerts.append({
                "level": "warn", "kind": "big_move", "code": p["code"],
                "message": f"{p['code']} 当日 {chg:+.2f}%",
            })
        ind = p.get("indicators") or {}
        if ind.get("price_vs_ma") and ind.get("ma_slope"):
            cross = "上方" if ind["price_vs_ma"] == "above" else "下方"
            alerts.append({
                "level": "info", "kind": "trend", "code": p["code"],
                "message": f"{p['code']} 收于 MA 线{cross}，MA 斜率 {ind['ma_slope']}"
                           + (f"，距 MA {ind['distance_in_atr']}×ATR" if ind.get("distance_in_atr") is not None else ""),
            })

    for w in enriched.get("watchlist") or []:
        ind = w.get("indicators") or {}
        vr = ind.get("volume_ratio")
        if vr is not None and vr >= 1.5:
            alerts.append({
                "level": "info", "kind": "volume", "code": w["code"],
                "message": f"{w['code']} 成交量放大至均量的 {vr}×",
            })

    order = {"high": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a.get("level"), 9))
    return alerts


def build(cfg, *, trigger: str, market: Dict[str, Any], events: List[Dict[str, Any]],
          news_items: List[Dict[str, Any]], macro: Optional[Dict[str, Any]] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(CST)
    generated = now.isoformat(timespec="seconds")
    data_asof = market.get("asof") or generated

    # 数据新鲜度
    stale, stale_detail = False, ""
    try:
        age_min = (now - datetime.fromisoformat(data_asof)).total_seconds() / 60.0
    except (TypeError, ValueError):
        age_min = 0.0
    limit = int(cfg.get("risk.stale_data_minutes", 30))
    if age_min > limit:
        stale = True
        stale_detail = f"数据时间 {data_asof}，距生成时间已 {age_min:.0f} 分钟（阈值 {limit} 分钟）"

    alerts = _build_alerts(cfg, market, events, stale, stale_detail)

    return {
        "_schema": _SCHEMA,
        "trigger": trigger,
        "generated_at": generated,
        "data_asof": data_asof,
        "data_age_minutes": round(age_min, 1),
        "source_status": market.get("status"),
        "account": market.get("account") or {},
        "positions": market.get("positions") or [],
        "watchlist": market.get("watchlist") or [],
        "calendar": events,
        "news": news_items,
        # 宏观快照（附加式，旧契仍有效）：fed_watch 为「主刻度」
        "macro": macro or {},
        "alerts": alerts,
    }


def write(cfg, packet: Dict[str, Any]) -> Path:
    """写 data/latest.json + 一份带时间戳的历史存档。"""
    ddir = cfg.path_for("data")
    ddir.mkdir(parents=True, exist_ok=True)

    latest = ddir / "latest.json"
    latest.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = packet["generated_at"].replace(":", "").replace("-", "")[:15]
    hist = ddir / f"{stamp}.json"
    try:
        hist.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("历史数据包写入失败：%s", exc)

    log.info("数据包已写入 %s（告警 %d 条）", latest, len(packet.get("alerts") or []))
    return latest
