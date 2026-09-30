"""状态持久化 —— 程序重启能续上：今天推了几条、上次推这条是什么时候。

为什么必须有它：Server酱免费版每天只有 5 条。没有去重和冷却，上午就会烧光，
真正的高危预警反而发不出去。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


class StateManager:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {"pushes": []}
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("state.json 读取失败（按空状态继续）：%s", exc)
                self.data = {"pushes": []}
        self.data.setdefault("pushes", [])

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("state.json 写入失败（不影响主流程）：%s", exc)

    # -- 查询 ----------------------------------------------------------------
    @staticmethod
    def fingerprint(title: str) -> str:
        return hashlib.sha1(title.strip().encode("utf-8")).hexdigest()[:12]

    def _today(self) -> str:
        return datetime.now(CST).strftime("%Y-%m-%d")

    def daily_count(self, channel: str | None = None, day: str | None = None) -> int:
        """今日成功推送数。传 channel 则只数该渠道。

        ⚠️ **必须按渠道分开数**：Server酱 5 条/天、Telegram 不限。
        用全局计数会让 Server酱 的限额卡死 Telegram 的推送。
        另外只数成功的 —— 失败的那次没送达，不该消耗额度。
        """
        day = day or self._today()
        return sum(
            1 for p in self.data["pushes"]
            if p.get("at", "").startswith(day)
            and p.get("ok")
            # 🚩 simulate 的推送**不算额度** —— 它根本没送达。
            #    不排除的话，联调几次就把 Server酱 的 5 条/天烧光了。
            and not p.get("simulated")
            and (channel is None or p.get("channel") == channel)
        )

    def last_push_at(self, fp: str) -> datetime | None:
        for p in reversed(self.data["pushes"]):
            if p.get("fp") == fp:
                try:
                    return datetime.fromisoformat(p["at"])
                except (KeyError, ValueError):
                    return None
        return None

    # -- 决策 ----------------------------------------------------------------
    def can_push(self, title: str, priority: str, cfg, channel: str | None = None) -> Tuple[bool, str]:
        """返回 (能否推送, 原因)。所有拦截都记原因，便于事后复盘。

        限额是**按渠道**查的：Server酱 5 条/天、Telegram 0（不限）。
        """
        # 该渠道的日限额；0 或缺省 = 不限
        max_day = 0
        if channel:
            max_day = int(cfg.get(f"push.channels.{channel}.max_per_day", 0) or 0)
        cooldown = int(cfg.get("push.cooldown_minutes", 30))
        bypass = bool(cfg.get("push.high_priority_bypass", True))

        if max_day:
            used = self.daily_count(channel)
            if used >= max_day:
                return False, f"渠道 {channel} 今日已达上限 {used}/{max_day}（额度稀缺，留给真正的高危预警）"

        fp = self.fingerprint(title)
        last = self.last_push_at(fp)
        if last is not None:
            elapsed = (datetime.now(CST) - last).total_seconds() / 60.0
            if elapsed < cooldown and not (bypass and priority == "high"):
                return False, f"同类内容 {elapsed:.1f} 分钟前刚推过（冷却 {cooldown} 分钟）"

        return True, "允许推送"

    def record_push(self, title: str, priority: str, channel: str, ok: bool,
                    simulated: bool = False) -> None:
        self.data["pushes"].append({
            "at": datetime.now(CST).isoformat(timespec="seconds"),
            "fp": self.fingerprint(title),
            "title": title[:80],
            "priority": priority,
            "channel": channel,
            "ok": ok,
            "simulated": simulated,
        })
        # 只保留最近 30 天，避免 state.json 无限膨胀（它在 OneDrive 里）
        cutoff = datetime.now(CST) - timedelta(days=30)
        kept: List[Dict[str, Any]] = []
        for p in self.data["pushes"]:
            try:
                if datetime.fromisoformat(p["at"]) >= cutoff:
                    kept.append(p)
            except (KeyError, ValueError):
                continue
        self.data["pushes"] = kept
        self.save()
