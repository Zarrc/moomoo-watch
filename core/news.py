"""moomoo 资讯 / 公告 / 研报抓取。

来源：公开 HTTP 接口，**无需 API key**（2026-09-30 实测 HTTP 200 / code:0）。
注意：新闻**不在** OpenD 里 —— 别去 futu-api 找 get_news，没有那个接口。

字段契约（实测）：
  {"code":0,"data":[{"news_id","news_type","title","publish_time","url","img_url"}],...}
  publish_time 是 **Unix 秒的时间戳字符串**，不是 ISO 字符串。
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """接口返回的标题里带 <em> 高亮标签，去掉并反转义。"""
    return html.unescape(_TAG.sub("", text or "")).strip()


def _ts_to_iso(raw: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(raw), tz=CST).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def fetch(cfg, keywords: List[str]) -> List[Dict[str, Any]]:
    """按关键词抓新闻，按 news_id 去重，返回统一格式的条目列表。"""
    if not cfg.get("news.enabled", True) or not keywords:
        return []

    endpoint = cfg.get("news.endpoint")
    lang = cfg.get("news.lang", "en")
    news_type = int(cfg.get("news.news_type", 1))
    per_kw = int(cfg.get("news.per_keyword", 8))
    max_items = int(cfg.get("news.max_items", 12))

    seen: Dict[str, Dict[str, Any]] = {}
    for kw in keywords:
        params = {
            "keyword": kw, "size": per_kw, "news_type": news_type,
            "lang": lang, "sort_type": 2,   # 2 = 按时间
        }
        try:
            resp = requests.get(
                endpoint, params=params, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (moomoo-watch)"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("新闻抓取失败（关键词 %s，跳过）：%s", kw, exc)
            continue

        if payload.get("code") != 0:
            log.warning("新闻接口返回 code=%s（关键词 %s）", payload.get("code"), kw)
            continue

        for item in payload.get("data") or []:
            nid = str(item.get("news_id") or "")
            if not nid or nid in seen:
                continue
            seen[nid] = {
                "id": nid,
                "title": _clean(item.get("title", "")),
                "url": item.get("url"),
                "published_at": _ts_to_iso(item.get("publish_time")),
                "news_type": item.get("news_type"),
                "matched_keyword": kw,
            }

    items = sorted(seen.values(), key=lambda x: x.get("published_at") or "", reverse=True)
    log.info("新闻：%d 个关键词 → 去重后 %d 条，保留 %d 条", len(keywords), len(items), min(max_items, len(items)))
    return items[:max_items]


def keywords_for(positions: List[Dict[str, Any]], watchlist: List[Dict[str, Any]], cfg) -> List[str]:
    """关键词 = 配置指定；留空则用持仓/自选股的代码自动生成。"""
    explicit = cfg.get("news.keywords") or []
    if explicit:
        return list(explicit)
    codes: List[str] = []
    for row in list(positions or []) + list(watchlist or []):
        code = str(row.get("code") or "")
        if not code:
            continue
        sym = code.split(".")[-1]          # US.GLD -> GLD
        if sym and sym not in codes:
            codes.append(sym)
    return codes
