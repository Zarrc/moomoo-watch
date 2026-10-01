"""宏观数据 —— 美联储利率预期（主刻度）+ 指标历史（实际值）。

为什么单独一个模块：
- `calendar.py` 只给「未来事件」，**不给已公布事件的实际值**，也不给利率概率。
- 多周期报告（周/月/季/年）恰恰要「上期实际 vs 预期」和「加息/降息概率在往哪走」。
- 这两项来自 OpenD 另外几个接口（见 skill §四）：
    `get_fed_watch_target_rate` · `get_fed_watch_dot_plot` · `get_macro_indicator_history`

🚩 **降级阶梯（本模块永不抛异常）**：
  1) `import futu` 失败            → `ok:false`，其余全 None/{}
  2) `opend_alive()` 为假          → **不开 context**，其余全 None/{}（省一次连接）
  3) 单个 API 失败                 → 该子键置 None 并记 WARNING，其余照常
  4) fixture 源                    → 读 `fixtures/macro.json`，且**必须透传 `_fixture:true`**
绝不编数：取不到就是 None / 空字典。

🚩🚩 **全设计里最危险的一个数**：fixture 里合成的「加息概率」。若 `_fixture` 标记没有
  透传出去，它可能被当成真实数据写进手机推送 —— 读者无从分辨。所以 `_fixture` 一路
  带到数据包，并由子代理契约强制在任何输出里标注「样例数据」。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


def _empty(ok: bool = False, errors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "ok": ok, "asof": None, "_fixture": False,
        "fed_watch": None, "dot_plot": None,
        "indicators": {}, "series": {}, "errors": errors or [],
    }


def _records(obj: Any) -> Optional[List[Dict[str, Any]]]:
    """把 DataFrame / list / dict 统一成 list[dict]；拿不到返回 None。"""
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        try:
            return list(obj.to_dict("records"))
        except Exception:                                   # noqa: BLE001
            pass
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        return [obj]
    return None


def _f(v: Any) -> Optional[float]:
    if v is None or v == "" or (isinstance(v, str) and v.strip().upper() in ("N/A", "NAN", "暂无")):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(row: Dict[str, Any], *names: str) -> Any:
    """按候选键名（大小写不敏感）在一条记录里取值。"""
    low = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return None


# ---------------------------------------------------------------------------
# fed watch（主刻度）
# ---------------------------------------------------------------------------
def _norm_fed_watch(obj: Any) -> Optional[Dict[str, Any]]:
    """把 get_fed_watch_target_rate 的返回规整成 {target_rate, probabilities, ...}。

    ⚠️ 该接口的字段名**未在真机核对过**（OpenD 当前未开）—— 故这里做「按键名模糊匹配 +
    找不到就留 None」的处理，**绝不猜数值**。首次真机联调时按实际列名收紧。
    """
    recs = _records(obj)
    if not recs:
        return None
    row = recs[0]
    probs: Dict[str, Any] = {"hike": None, "hold": None, "cut": None}
    for r in recs:
        for key, val in r.items():
            k = str(key).lower()
            f = _f(val)
            if f is None:
                continue
            if probs["hike"] is None and any(t in k for t in ("hike", "raise", "increase", "up")):
                probs["hike"] = f
            if probs["cut"] is None and any(t in k for t in ("cut", "lower", "decrease", "down", "ease")):
                probs["cut"] = f
            if probs["hold"] is None and any(t in k for t in ("hold", "unchanged", "no_change", "same")):
                probs["hold"] = f
    target = _pick(row, "target_rate", "target", "rate", "fed_rate", "current_rate")
    meeting = _pick(row, "next_meeting", "meeting_date", "date", "time")
    return {
        "target_rate": _f(target),
        "next_meeting": str(meeting) if meeting is not None else None,
        "probabilities": probs,
        "raw": recs,
    }


def _norm_dot_plot(obj: Any) -> Optional[Dict[str, Any]]:
    recs = _records(obj)
    if not recs:
        return None
    points: List[Dict[str, Any]] = []
    for r in recs:
        year = _pick(r, "year", "period", "time", "date")
        rate = _f(_pick(r, "rate", "value", "target_rate", "median"))
        if year is None and rate is None:
            continue
        points.append({"year": str(year) if year is not None else None, "rate": rate})
    return {"points": points, "raw": recs} if points else {"points": [], "raw": recs}


# ---------------------------------------------------------------------------
# 指标历史
# ---------------------------------------------------------------------------
def _norm_series(obj: Any) -> List[Dict[str, Any]]:
    recs = _records(obj) or []
    out: List[Dict[str, Any]] = []
    for r in recs:
        period = _pick(r, "time", "period", "date", "timestamp", "month")
        val = _f(_pick(r, "value", "val", "actual", "close", "price"))
        if period is None or val is None:
            continue
        out.append({"period": str(period)[:19], "value": val})
    out.sort(key=lambda x: x["period"])
    return out


# ---------------------------------------------------------------------------
# fixture 源
# ---------------------------------------------------------------------------
def _from_fixture(cfg, now: datetime) -> Dict[str, Any]:
    path = cfg.path_for("fixtures") / "macro.json"
    if not path.is_file():
        log.warning("fixture 宏观数据不存在：%s", path)
        return _empty(False, [{"api": "fixture", "detail": f"缺少 {path.name}"}])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("fixture 宏观数据读取失败：%s", exc)
        return _empty(False, [{"api": "fixture", "detail": str(exc)}])

    out = _empty(True)
    out["asof"] = now.isoformat(timespec="seconds")
    # 🚩 透传 _fixture 标记 —— 缺了它，合成的加息概率会被当成真数据推出去
    out["_fixture"] = bool(data.get("_fixture", True))
    out["fed_watch"] = data.get("fed_watch")
    out["dot_plot"] = data.get("dot_plot")
    out["indicators"] = data.get("indicators") or {}
    out["series"] = data.get("series") or {}
    log.info("宏观数据（fixture%s）：指标 %d 项", "，样例" if out["_fixture"] else "",
             len(out["indicators"]))
    return out


# ---------------------------------------------------------------------------
# futu 源
# ---------------------------------------------------------------------------
def _from_futu(cfg, now: datetime) -> Dict[str, Any]:
    try:
        from futu import OpenQuoteContext
    except ImportError:
        log.warning("futu-api 未安装，宏观数据降级为空")
        return _empty(False, [{"api": "import", "detail": "futu-api 未安装"}])

    from .market import opend_alive
    if not opend_alive(cfg)["alive"]:
        log.warning("OpenD 不可达，宏观数据降级为空（不开连接）")
        return _empty(False, [{"api": "opend", "detail": "OpenD 不可达"}])

    out = _empty(True)
    out["asof"] = now.isoformat(timespec="seconds")
    errors: List[Dict[str, Any]] = []

    host = cfg.get("moomoo.host", "127.0.0.1")
    port = int(cfg.get("moomoo.port", 11111))
    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)

        if cfg.get("macro.fed_watch", True):
            try:
                res = ctx.get_fed_watch_target_rate()          # type: ignore[attr-defined]
                ret, data = _split(res)
                out["fed_watch"] = _norm_fed_watch(data) if ret == 0 else None
                if ret != 0:
                    raise RuntimeError(str(data)[:120])
            except Exception as exc:                           # noqa: BLE001
                errors.append({"api": "fed_watch", "detail": str(exc)[:120]})
                log.warning("get_fed_watch_target_rate 失败（置空）：%s", exc)

        if cfg.get("macro.dot_plot", True):
            try:
                res = ctx.get_fed_watch_dot_plot()             # type: ignore[attr-defined]
                ret, data = _split(res)
                out["dot_plot"] = _norm_dot_plot(data) if ret == 0 else None
                if ret != 0:
                    raise RuntimeError(str(data)[:120])
            except Exception as exc:                           # noqa: BLE001
                errors.append({"api": "dot_plot", "detail": str(exc)[:120]})
                log.warning("get_fed_watch_dot_plot 失败（置空）：%s", exc)

        n = int(cfg.get("macro.history_periods", 24))
        for ind_id in (cfg.get("macro.indicators") or []):
            try:
                res = ctx.get_macro_indicator_history(          # type: ignore[attr-defined]
                    str(ind_id), max_count=n,
                )
                ret, data = _split(res)
                if ret != 0:
                    raise RuntimeError(str(data)[:120])
                series = _norm_series(data)
                if series:
                    out["series"][str(ind_id)] = series
                    out["indicators"][str(ind_id)] = {
                        "value": series[-1]["value"], "period": series[-1]["period"],
                    }
            except Exception as exc:                           # noqa: BLE001
                errors.append({"api": f"indicator:{ind_id}", "detail": str(exc)[:120]})
                log.warning("指标 %s 获取失败（跳过）：%s", ind_id, exc)
    except Exception as exc:                                   # noqa: BLE001
        errors.append({"api": "context", "detail": str(exc)[:120]})
        log.warning("宏观数据 context 异常：%s", exc)
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:                                  # noqa: BLE001
                pass

    out["errors"] = errors
    out["ok"] = bool(out["fed_watch"] or out["indicators"])
    log.info("宏观数据（futu）：fed_watch=%s 指标 %d 项（失败 %d）",
             bool(out["fed_watch"]), len(out["indicators"]), len(errors))
    return out


def _split(res):
    """OpenD 返回 (ret, data[, page…])；防御式取值。"""
    if isinstance(res, tuple) and len(res) >= 2:
        return res[0], res[1]
    if isinstance(res, tuple) and len(res) == 1:
        return 0, res[0]
    return 0, res


# ---------------------------------------------------------------------------
def snapshot(cfg, now: datetime | None = None) -> Dict[str, Any]:
    """取一版宏观快照。任何失败都降级为空壳，永不抛。"""
    now = now or datetime.now(CST)
    if not cfg.get("macro.enabled", True):
        return _empty(False, [{"api": "config", "detail": "macro.enabled = false"}])
    return _from_futu(cfg, now) if cfg.source == "futu" else _from_fixture(cfg, now)
