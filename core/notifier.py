"""推送 —— 多渠道 + 按优先级分流。

设计要点（每条都对应一个真实故障模式）：
- **非阻塞**：发送在单独线程里跑，绝不阻塞主流程。
- **超时**：15 秒封顶，防对端挂起。
- **失败只记日志**：认证错误 / 网络超时都不得抛出去让主程序退出。
- **密钥不进代码、不进 config.yaml**：只从 vault 外的 .env / 环境变量读。
- **simulate 模式不发**：只写日志，用于联调。
- **限额必须分渠道**：Server酱 5 条/天，Telegram 不限。用全局限额会把分流彻底卡死。

渠道特性对照（决定了为什么这么分流）：
- Server酱：**卡片只显示标题、不显示正文**，且每天仅 5 条 → 稀缺
- Telegram：免费无上限、正文完整、支持 Markdown → 快渠道
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

import requests

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push")

# Telegram 单条消息上限 4096 字符
_TG_LIMIT = 4000

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)


def _md_to_plain(text: str) -> str:
    """把简报的 Markdown 抹成可读的纯文本。

    为什么需要：真实简报里 URL 带下划线（`news_search_searchnews`），
    下划线在 Telegram Markdown 里是斜体标记且不配对 → **必然 400** →
    每次都走降级路径 → 于是 `**粗体**`、`## 标题` 会原样显示成符号，很难看。

    ⚠️ 刻意**不处理下划线**：它在纯文本里本来就是正常字符，
    而且 URL 里大量存在，动它会把链接改坏。
    """
    t = _MD_LINK.sub(lambda m: f"{m.group(1)}\n{m.group(2)}", text)  # [文字](url) → 文字 换行 url
    t = _MD_HEAD.sub("", t)                                          # 去掉 ## 标题井号
    t = _MD_BULLET.sub(r"\1· ", t)                                   # - 列表 → · 列表
    t = t.replace("**", "").replace("`", "")                         # 去粗体与反引号
    return t.strip()


# ---------------------------------------------------------------------------
# 各渠道实现
# ---------------------------------------------------------------------------
def _send_serverchan(cfg, title: str, body: str, timeout: int) -> Dict[str, Any]:
    key = cfg.secret(cfg.get("push.channels.serverchan.env_key", "SERVERCHAN_SENDKEY"))
    if not key:
        return {"ok": False, "error": "缺少 SendKey（未找到环境变量或 .env 条目）"}
    resp = requests.post(
        f"https://sctapi.ftqq.com/{key}.send",
        data={"title": title, "desp": body}, timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    ok = data.get("code") == 0
    return {"ok": ok, "error": None if ok else data.get("message"), "raw_code": data.get("code")}


def _send_pushplus(cfg, title: str, body: str, timeout: int) -> Dict[str, Any]:
    token = cfg.secret(cfg.get("push.channels.pushplus.env_key", "PUSHPLUS_TOKEN"))
    if not token:
        return {"ok": False, "error": "缺少 Token（未找到环境变量或 .env 条目）"}
    resp = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": token, "title": title, "content": body, "template": "markdown"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    ok = data.get("code") == 200
    return {"ok": ok, "error": None if ok else data.get("msg"), "raw_code": data.get("code")}


def _send_telegram(cfg, title: str, body: str, timeout: int) -> Dict[str, Any]:
    token = cfg.secret(cfg.get("push.channels.telegram.env_token", "TELEGRAM_BOT_TOKEN"))
    chat = cfg.secret(cfg.get("push.channels.telegram.env_chat", "TELEGRAM_CHAT_ID"))
    if not token or not chat:
        return {"ok": False, "error": "缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID"}

    text = f"*{title}*\n\n{body}"
    if len(text) > _TG_LIMIT:
        text = text[:_TG_LIMIT] + "\n…（已截断，完整内容见 outbox）"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # 🚩 Markdown 会因标题里的 `_` `*` `[` 等未转义字符直接 400
    #    （报 "can't parse entities"）。所以先试 Markdown，被拒就退回纯文本 ——
    #    简报的**送达**比样式重要。
    #
    # 🚩🚩 **绝不能在这里调 resp.raise_for_status()**（2026-09-30 实踩）：
    #    Telegram 把错误详情放在 **400 的响应体**里，raise_for_status 会先抛异常，
    #    导致下面的降级逻辑**永远跑不到** —— 表现就是「明知该退纯文本却报 400」。
    last_err = None
    for parse_mode in ("Markdown", None):
        payload: Dict[str, Any] = {
            "chat_id": chat, "text": text, "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        else:
            payload["text"] = _md_to_plain(text)   # 退回纯文本时顺手抹掉 Markdown 符号
        resp = requests.post(url, json=payload, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "error": f"Telegram 响应不是合法 JSON（HTTP {resp.status_code}）"}
        if data.get("ok"):
            return {"ok": True, "error": None, "parse_mode": parse_mode}
        last_err = data.get("description")
        if resp.status_code != 400 or "parse" not in str(last_err).lower():
            break                      # 不是格式问题 → 没必要重试
    return {"ok": False, "error": last_err, "raw_code": 400}


_CHANNELS = {
    "serverchan": _send_serverchan,
    "pushplus": _send_pushplus,
    "telegram": _send_telegram,
}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def declared_channels(cfg) -> List[str]:
    """config 里声明了的渠道，按声明顺序（= 降级顺序）。"""
    chans = cfg.get("push.channels") or {}
    return [c for c in chans if c in _CHANNELS]


def resolve_channel(cfg, priority: str) -> str | None:
    """按优先级选渠道。routing 没配就退回 push.channel，再退回第一个声明的渠道。"""
    routing = cfg.get("push.routing") or {}
    picked = routing.get(priority) or routing.get("normal") or cfg.get("push.channel")
    if picked:
        return str(picked).lower()
    chans = declared_channels(cfg)
    return chans[0] if chans else None


def missing_secrets(cfg) -> List[str]:
    """列出所有渠道缺失的密钥名（不打印值），供启动自检。"""
    env_cfg = cfg.get("push.channels") or {}
    names: List[str] = []
    for name, spec in env_cfg.items():
        for k, v in (spec or {}).items():
            if k.startswith("env_") and v and not cfg.secret(v):
                names.append(f"{name}:{v}")
    return names


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def _redact(text: Any, cfg) -> str:
    """把出现在文本里的密钥擦掉。

    🚩 为什么必须有：`requests` 的异常信息**带完整 URL**，而 Telegram 的 URL 里
    就嵌着 bot token（`/bot<TOKEN>/sendMessage`）。这些字符串会进 `logs/watch.log`，
    **而日志目录在 OneDrive 同步的 vault 里** —— 等于把 token 同步上云。
    2026-09-30 实踩：第一次 400 的错误信息里就明晃晃带着 token。
    """
    s = str(text)
    for spec in (cfg.get("push.channels") or {}).values():
        for key, env_name in (spec or {}).items():
            if key.startswith("env_") and env_name:
                val = cfg.secret(env_name)
                if val and len(val) > 8:
                    s = s.replace(val, "***REDACTED***")
    return s


def send(cfg, title: str, body: str, priority: str = "normal") -> Dict[str, Any]:
    """按优先级分流发送；路由渠道失败时按声明顺序降级重试。"""
    if not cfg.live_push:
        chan = resolve_channel(cfg, priority)
        log.info("[simulate] 不真发推送 | priority=%s → 渠道=%s | 标题=%s", priority, chan, title)
        return {"ok": True, "simulated": True, "channel": chan, "error": None}

    primary = resolve_channel(cfg, priority)
    order = [primary] if primary else []
    if cfg.get("push.fallback", True):
        order += [c for c in declared_channels(cfg) if c not in order]

    timeout = int(cfg.get("push.timeout_seconds", 15))
    last: Dict[str, Any] = {"ok": False, "error": "没有可用渠道"}
    tried: List[str] = []
    for chan in order:
        fn = _CHANNELS.get(chan)
        if fn is None:
            continue
        tried.append(chan)
        try:
            res = fn(cfg, title, body, timeout)
        except requests.Timeout:
            res = {"ok": False, "error": f"超时（{timeout}s）"}
        except requests.RequestException as exc:
            res = {"ok": False, "error": f"网络错误：{exc}"}
        except ValueError as exc:
            res = {"ok": False, "error": f"响应不是合法 JSON：{exc}"}
        except Exception as exc:                       # noqa: BLE001 —— 绝不能搞崩主程序
            res = {"ok": False, "error": f"未预期错误：{exc!r}"}

        res["channel"] = chan
        # 所有对外文字都过一遍脱敏 —— 异常信息里可能嵌着 token，而日志在 OneDrive 里
        res["error"] = _redact(res.get("error"), cfg)
        if res.get("ok"):
            if len(tried) > 1:
                log.warning("降级成功：%s 失败后改由 %s 送达", tried[0], chan)
            return res
        log.warning("渠道 %s 发送失败：%s", chan, res.get("error"))
        last = res

    last["tried"] = tried
    return last


def send_async(cfg, title: str, body: str, priority: str = "normal"):
    """非阻塞发送，返回 Future。结果里同样不会抛异常。"""
    return _executor.submit(send, cfg, title, body, priority)
