"""账户计价币种 → 展示币种（MYR）的汇率换算 —— 「数字 = 脚本」原则的一环。

为什么存在：futu 10.11.7108 的 OpenQuoteContext / OpenSecTradeContext **都没有汇率接口**，
OpenD 也拿不到外汇行情（2026-10-13 实测 `Forex.*` 报代码格式错、`MY.USDMYR`/`US.USDHKD` 报 Unknown stock）。
账户计价是 HKD，用户日常看 MYR → 走免费无 key 的 open.er-api.com。

铁律：
- **取不到汇率 = 没有换算值**（`fx=None`，不写 `equity_myr` 等），绝不编、绝不用记忆里的汇率。
- fixture 源用固定汇率并标 `_fixture: true`，离线自检保持确定性（与 `macro._fixture` 同一套约定）。
- 换算由脚本做完（`equity_myr` / `cash_myr`），子代理只读现成数字、**不做算术**。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

# fixture 用的固定展示汇率（约 2026-10 水平，只为离线链路可复现，不代表真实行情）
FIXTURE_RATE = 0.574


def fetch_rates(cfg, base: str = "HKD", quote: str = "MYR",
                timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """取 `base→quote` 汇率。返回 `{f"{base}_{quote}": 0.5xx, "source":…, "asof":…}`；
    关闭、同币种、网络失败、返回异常 → **None（调用方降级，不抛）**。"""
    if cfg.source == "fixture":
        return {f"{base.lower()}_{quote.lower()}": FIXTURE_RATE,
                "source": "fixture", "_fixture": True}
    if not cfg.get("fx.enabled", True):
        return None
    if str(base).upper() == str(quote).upper():
        return None
    url = cfg.get("fx.url") or f"https://open.er-api.com/v6/latest/{base}"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        rate = (data.get("rates") or {}).get(str(quote).upper())
        if not isinstance(rate, (int, float)) or rate <= 0:
            return None
        return {f"{base.lower()}_{quote.lower()}": float(rate),
                "source": "open.er-api.com",
                "asof": data.get("time_last_update_utc")}
    except Exception:
        return None  # 网络失败不崩（设计原则：异常一律只降级）


def attach_display(cfg, account: Dict[str, Any], quote: str = "MYR") -> Dict[str, Any]:
    """给 `account` 原地补：`fx`（汇率块或 None）+ `equity_myr` / `cash_myr`（换算值）。

    计价币种取 `account.currency`（拿不到按 HKD 兜底）。换算值只写**取得到原值**的项。
    """
    base = str(account.get("currency") or "HKD")
    rates = fetch_rates(cfg, base=base, quote=quote)
    account["fx"] = rates
    if not rates:
        return account
    rate = next((v for k, v in rates.items()
                 if k.endswith(f"_{quote.lower()}") and isinstance(v, (int, float))), None)
    if rate is None:
        return account
    for src, dst in (("equity", f"equity_{quote.lower()}"), ("cash", f"cash_{quote.lower()}")):
        val = account.get(src)
        if isinstance(val, (int, float)):
            account[dst] = round(val * rate, 2)
    return account
