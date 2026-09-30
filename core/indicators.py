"""技术指标 —— 纯 Python 实现，不依赖 pandas。

为什么要自己算：契约规定「指标由脚本算好，子代理不算数」。
为什么不用 pandas：少一个重依赖 = 少一类环境故障。futu-api 给的 DataFrame
在本模块入口处统一转成 list[dict]，之后全程纯 Python。

K 线统一格式：[{"time": "...", "open": f, "high": f, "low": f, "close": f, "volume": f}, ...]
按时间**升序**（旧 → 新）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _closes(bars: List[Dict[str, Any]]) -> List[float]:
    return [float(b["close"]) for b in bars]


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    cur = sum(values[:period]) / period
    for v in values[period:]:
        cur = v * k + cur * (1 - k)
    return cur


def ma_series(values: List[float], period: int) -> List[float]:
    """滚动 SMA，长度与 values 对齐的前段用 None 占位。"""
    out: List[Optional[float]] = []
    for i in range(len(values)):
        out.append(sum(values[i - period + 1: i + 1]) / period if i + 1 >= period else None)
    return [v for v in out if v is not None]


def true_ranges(bars: List[Dict[str, Any]]) -> List[float]:
    trs: List[float] = []
    for i, b in enumerate(bars):
        h, l = float(b["high"]), float(b["low"])
        if i == 0:
            trs.append(h - l)
        else:
            pc = float(bars[i - 1]["close"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Wilder 平滑 ATR。"""
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    cur = sum(trs[:period]) / period
    for tr in trs[period:]:
        cur = (cur * (period - 1) + tr) / period
    return cur


def rsi(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Wilder 平滑 RSI。"""
    closes = _closes(bars)
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def volume_ratio(bars: List[Dict[str, Any]], period: int = 20) -> Optional[float]:
    """最新一根成交量 / 前 period 根均量。"""
    if len(bars) < period + 1:
        return None
    prev = [float(b["volume"]) for b in bars[-period - 1: -1]]
    avg = sum(prev) / len(prev)
    if avg == 0:
        return None
    return float(bars[-1]["volume"]) / avg


def ma_slope(bars: List[Dict[str, Any]], period: int) -> Optional[str]:
    """MA 斜率：up / down / flat（用最近两根 MA 比较，阈值取 ATR 的 2%）。"""
    closes = _closes(bars)
    if len(closes) < period + 2:
        return None
    m_now = sum(closes[-period:]) / period
    m_prev = sum(closes[-period - 1: -1]) / period
    a = atr(bars, 14)
    tol = (a * 0.02) if a else (abs(m_now) * 0.0005)
    if m_now - m_prev > tol:
        return "up"
    if m_prev - m_now > tol:
        return "down"
    return "flat"


def compute(bars: List[Dict[str, Any]], cfg) -> Dict[str, Any]:
    """按 config.yaml 的 indicators 段算全套指标。"""
    if not bars:
        return {"error": "无 K 线数据"}

    p_ma = int(cfg.get("indicators.ma_period", 50))
    p_atr = int(cfg.get("indicators.atr_period", 14))
    p_rsi = int(cfg.get("indicators.rsi_period", 14))
    p_vol = int(cfg.get("indicators.volume_avg_period", 20))

    closes = _closes(bars)
    last = float(bars[-1]["close"])
    m = sma(closes, p_ma)
    a = atr(bars, p_atr)

    out: Dict[str, Any] = {
        "bars": len(bars),
        "last_close": round(last, 4),
        "last_time": bars[-1].get("time"),
        f"ma{p_ma}": round(m, 4) if m else None,
        "ma_slope": ma_slope(bars, p_ma),
        f"atr{p_atr}": round(a, 4) if a else None,
        f"rsi{p_rsi}": round(r, 4) if (r := rsi(bars, p_rsi)) is not None else None,
        "volume_ratio": round(v, 3) if (v := volume_ratio(bars, p_vol)) is not None else None,
    }
    if m:
        out["price_vs_ma"] = "above" if last > m else ("below" if last < m else "equal")
        out["distance_from_ma"] = round(last - m, 4)
        out["distance_in_atr"] = round((last - m) / a, 3) if a else None
    return out


def sanitize(obj: Any) -> Any:
    """把 NaN / inf 换成 None，保证写出的 JSON 合法。"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj
