"""宏观事件日历 —— 覆盖参考模板里「爬 BLS + 硬编码 2026 日历」那一整块。

参考模板（黄金交易信号系统）自己写爬虫爬 BLS 官网、再硬编码一份全年日历兜底 ——
那是它最脆的部分。moomoo 官方 OpenAPI 直接有 get_economic_calendar，
所以本项目**不写爬虫、不硬编码日历**，只做「取 → 标记 → 算距今」。

fixture 源提供一个离线样例，保证不连 OpenD 也能跑通全链路。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


def _kw_hit(name: str, kws: List[str]) -> bool:
    low = (name or "").lower()
    return any(k.lower() in low for k in kws)


def _normalize(raw: Dict[str, Any], kws: List[str], now: datetime) -> Dict[str, Any] | None:
    """把 OpenD 返回的一条事件规整成我们的契约。返回 None 表示该条不可用。"""
    name = str(raw.get("title") or raw.get("event_name") or raw.get("name") or "").strip()
    if not name:
        return None
    ts = raw.get("timestamp") or raw.get("publish_time") or raw.get("date")
    when: datetime | None = None
    if isinstance(ts, (int, float)):
        when = datetime.fromtimestamp(float(ts), tz=CST)
    elif isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                when = datetime.strptime(ts, fmt).replace(tzinfo=CST)
                break
            except ValueError:
                continue
    if when is None:
        return None

    minutes = int((when - now).total_seconds() // 60)
    return {
        "name": name,
        "at": when.isoformat(timespec="minutes"),
        "minutes_until": minutes,
        "high_risk": _kw_hit(name, kws),
        "country": raw.get("country") or raw.get("region"),
        "importance": raw.get("importance") or raw.get("star"),
    }


def _from_fixture(cfg, now: datetime) -> List[Dict[str, Any]]:
    path = cfg.path_for("fixtures") / "economic_calendar.json"
    if not path.is_file():
        log.warning("fixture 日历不存在：%s", path)
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("fixture 日历读取失败：%s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        offset = row.get("minutes_from_now")
        if offset is None:
            continue
        out.append({
            "name": row.get("name", ""),
            "at": (now + timedelta(minutes=int(offset))).isoformat(timespec="minutes"),
            "minutes_until": int(offset),
            "high_risk": bool(row.get("high_risk")),
            "country": row.get("country", "US"),
            "importance": row.get("importance"),
        })
    return out


def _from_futu(cfg, now: datetime) -> List[Dict[str, Any]]:
    """从 OpenD 取经济日历。任何连接问题都降级为空列表，不抛。"""
    try:
        from futu import OpenQuoteContext
    except ImportError:
        log.warning("futu-api 未安装，日历降级为空")
        return []

    kws = cfg.get("calendar.high_risk_keywords") or []
    ctx = None
    try:
        ctx = OpenQuoteContext(
            host=cfg.get("moomoo.host", "127.0.0.1"),
            port=int(cfg.get("moomoo.port", 11111)),
        )
        # ⚠️ 三个反直觉点，全在 2026-09-30 实踩过：
        #   ① 签名 (begin_date, end_date=None, market_list=None, importance=None, count=None, next_page=None)
        #      —— `begin_date` **必填**，不传直接 TypeError。
        #   ② 返回值是 **4 元组**，不是 2 个。写 `ret, data = ...` 会 "too many values to unpack"。
        #   ③ 🚩 **必须翻页**：单次固定返回 50 条，**按重要性排序**，不是按时间。
        #      只取第一页的话，会被当天最重要的那批占满 —— 未来的事件全被挤出去，
        #      于是「事件前 30-60 分钟预警」永远不会触发，而**看不出任何异常**。
        #      （实踩：第一页 50 条全在当天 04:30–22:30；第 2 页才出现 10-02 的 NFP。）
        #      下一页游标在元组第 3 位，是**毫秒时间戳字符串**。
        begin = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=int(cfg.get("calendar.lookahead_days", 10)))).strftime("%Y-%m-%d")

        rows: List[Dict[str, Any]] = []
        page_cursor = None
        for _ in range(int(cfg.get("calendar.max_pages", 6))):
            res = ctx.get_economic_calendar(          # type: ignore[attr-defined]
                begin_date=begin, end_date=end, next_page=page_cursor,
            )
            ret, data = (res[0], res[1]) if isinstance(res, tuple) and len(res) >= 2 else (res, None)
            page_cursor = res[2] if isinstance(res, tuple) and len(res) > 2 else None
            if ret != 0 or data is None:
                log.warning("get_economic_calendar 返回错误：%s", str(data)[:120])
                break
            rows.extend(data.to_dict("records") if hasattr(data, "to_dict") else list(data))
            if not page_cursor:
                break
        log.info("宏观日历：翻页取到 %d 条原始记录", len(rows))
    except Exception as exc:                          # noqa: BLE001
        log.warning("经济日历获取失败（降级为空）：%s", exc)
        return []
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:                          # noqa: BLE001
                pass

    out = [x for x in (_normalize(r, kws, now) for r in rows) if x]
    return [x for x in out if x["minutes_until"] >= -60]


def upcoming(cfg, now: datetime | None = None) -> List[Dict[str, Any]]:
    now = now or datetime.now(CST)
    if not cfg.get("calendar.enabled", True):
        return []

    events = _from_futu(cfg, now) if cfg.source == "futu" else _from_fixture(cfg, now)

    # 保留窗：刚过去的 session 仍留一点（便于复盘），未来按 lookahead 截断
    lookback = int(cfg.get("calendar.past_minutes", 120))
    horizon = int(cfg.get("calendar.lookahead_days", 10)) * 1440
    events = [e for e in events if -lookback <= e["minutes_until"] <= horizon]
    events.sort(key=lambda e: e["minutes_until"])

    # 翻页后可能有几百条 —— 截断时**优先保住未来的高险事件**，
    # 否则大量的低价值条目会把真正要预警的挤掉（这正是翻页之前踩的同一个坑）。
    cap = int(cfg.get("calendar.max_items", 40))
    if len(events) > cap:
        priority = [e for e in events if e["high_risk"] and e["minutes_until"] >= 0][:cap]
        taken = {id(e) for e in priority}
        rest = [e for e in events if id(e) not in taken]
        events = sorted(priority + rest[: max(0, cap - len(priority))],
                        key=lambda e: e["minutes_until"])
        log.info("宏观日历：截断至 %d 条（优先保留未来高险事件）", len(events))

    log.info("宏观日历：%d 条（其中高险 %d 条，未来高险 %d 条）", len(events),
             sum(1 for e in events if e["high_risk"]),
             sum(1 for e in events if e["high_risk"] and e["minutes_until"] >= 0))
    return events


def in_alert_window(events: List[Dict[str, Any]], cfg) -> List[Dict[str, Any]]:
    """筛出落在预警窗口 [min_lead, max_lead] 内的**高险**事件。

    ⚠️ **必须有下限**。只写 `0 <= minutes_until <= 60` 的话，事件已经到点（=0 分钟）时
    仍会触发 alert —— 那时推「即将发布」毫无意义，只是在烧推送额度。
    （2026-09-30 实踩：子代理产出稿里明确点了这条「触发条件与场景不匹配」。）
    """
    max_lead = int(cfg.get("calendar.alert_window_minutes", 60))
    min_lead = int(cfg.get("calendar.alert_min_lead_minutes", 5))
    return [e for e in events
            if e["high_risk"] and min_lead <= e["minutes_until"] <= max_lead]
