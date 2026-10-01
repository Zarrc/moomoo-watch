"""调起 portfolio-watch 子代理，把数据包变成一份手机推送稿。

⚠️ 这条链路依赖 `claude -p`（用户已明确接受该依赖）。若拒绝该依赖，
   本模块应整体停用，简报改由模板 + 规则生成 —— 那是正当选择，不是降级。

⚠️ 实测得出的关键约束（2026-09-30，v2.1.201）：
   **`--permission-mode acceptEdits` 不能省。**
   子代理文件里的 `permissionMode` 字段在 `--agent` 这条路径下**静默失效** ——
   文件照样加载、exit 0，但 Write 被权限门挡下，什么都不会产出。
   详见 .claude/agents/portfolio-watch.md 的四臂实测矩阵。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from . import config as _config

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_PROMPT = (
    "读 {data} 这个数据包，按你的 {trigger} 模式产出一份手机推送稿，"
    "写到 {outbox} 下。严格遵守你自己的输出契约（send / priority / title 三个字段 + 正文），"
    "title 必须 ≤40 字且自包含（Server酱免费版只显示标题）。"
    "数据包里没有的数字一律写「无」，不许估算。"
)

# report 模式（日评/周报/月报/季报/年报）：两文件输出 —— 全文落 vault，短摘要落 outbox
_PERIOD_DIR = {"daily": "日评", "weekly": "周报", "monthly": "月报",
               "quarterly": "季报", "yearly": "年报"}
_PROMPT_REPORT = (
    "现在是 {now}。这是**{label}**（周期 {period_key}）的汇总任务，按你的 `report` 模式做。\n"
    "1) 先读**报告包** {bundle} —— 含本期日记录、脚本算好的聚合数字、宏观快照、当前命中率；\n"
    "2) 再读**数据包** {data} —— 最新一版原始数据，供细节核对；\n"
    "3) 把**报告全文**写到 `{invest}/{subdir}/{period_key} <标题>.md`（长文；手机只读摘要）；\n"
    "4) 若周期是 daily：全文里必须带一个 `<!-- CALL:BEGIN -->` … `<!-- CALL:END -->` 预测块"
    "（内嵌 ```yaml，字段见你的契约）—— 这是**次日被脚本打分的可证伪预测**；\n"
    "5) 把**短摘要**写到 {outbox} 下（frontmatter 仍扁平：send/priority/title）。\n"
    "数字一律取自报告包，不要自己算、不要估算；报告包里没有的写「无」。"
    "`macro._fixture=true` 时，任何输出都必须标注「样例数据」。"
)

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def _parse_outbox(path: Path) -> Dict[str, Any]:
    """解析子代理写出的推送稿：YAML frontmatter（send/priority/title）+ 正文。"""
    text = path.read_text(encoding="utf-8")
    m = _FM.match(text)
    if not m:
        return {"send": False, "priority": "normal", "title": path.stem, "body": text,
                "parse_error": "没有 frontmatter，已按 send:false 处理（保守）"}

    head, body = m.group(1), m.group(2).strip()
    meta: Dict[str, Any] = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")

    def _bool(v, default=False):
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "yes", "1", "on") if v is not None else default

    return {
        "send": _bool(meta.get("send"), False),
        "priority": (meta.get("priority") or "normal").lower(),
        "title": meta.get("title") or path.stem,
        "body": body,
        "raw_meta": meta,
    }


def _newest_outbox(outbox: Path, since: datetime) -> Optional[Path]:
    if not outbox.is_dir():
        return None
    candidates = []
    for f in outbox.glob("*.md"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, tz=CST) >= since:
                candidates.append(f)
        except OSError:
            continue
    return max(candidates, key=lambda f: f.stat().st_mtime) if candidates else None


def _vault_root() -> Path:
    """vault 根 —— **唯一实现在 `core.config.find_vault_root()`**，这里只做转发。

    为什么要收敛成一份：向上找 `.claude` 的逻辑散在多处时，「数错 parent 层数」那个
    静默 bug（子代理 cwd 错了 → 相对路径全错 → 还 exit 0）会以不同面目回来。
    """
    return _config.find_vault_root()


def run(cfg, *, trigger: str = "brief", question: str | None = None,
        data_path: Optional[Path] = None, period: Optional[str] = None,
        period_key: Optional[str] = None, report_bundle: Optional[Path] = None,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """调子代理 → 读回推送稿。返回 {ok, draft, error}。任何失败都不抛。

    覆盖参数（默认不变，旧调用点无感）：
      - `data_path`   —— 覆盖默认的 `data/latest.json`
      - `trigger="report"` —— 走 report 模式（`period` / `period_key` / `report_bundle`）
    """
    if not cfg.get("agent.enabled", True):
        return {"ok": False, "draft": None, "error": "agent.enabled = false"}

    exe = cfg.get("agent.claude_exe")
    agent = cfg.get("agent.agent_name", "portfolio-watch")
    perm = cfg.get("agent.permission_mode", "acceptEdits")
    is_report = (trigger == "report")
    timeout = int(cfg.get("agent.report_timeout_seconds", 900) if is_report
                  else cfg.get("agent.timeout_seconds", 300))
    data_path = data_path or (cfg.path_for("data") / "latest.json")
    outbox = cfg.path_for("outbox", create=True)
    vault_root = _vault_root()

    if not Path(exe).is_file():
        return {"ok": False, "draft": None, "error": f"claude 可执行文件不存在：{exe}"}

    if is_report:
        period = period or "daily"
        period_key = period_key or "?"
        bundle = Path(report_bundle) if report_bundle else (cfg.path_for("data") / "latest.json")
        invest_rel = (Path(cfg.get("paths.vault_invest", "Self/投资"))).as_posix()
        prompt = _PROMPT_REPORT.format(
            now=(now or datetime.now(CST)).isoformat(timespec="seconds"),
            label=_PERIOD_DIR.get(period, period), period_key=period_key,
            bundle=bundle.relative_to(vault_root).as_posix(),
            data=data_path.relative_to(vault_root).as_posix(),
            invest=invest_rel, subdir=_PERIOD_DIR.get(period, period),
            outbox=outbox.relative_to(vault_root).as_posix(),
        )
    else:
        prompt = _PROMPT.format(
            data=data_path.relative_to(vault_root).as_posix(),
            outbox=outbox.relative_to(vault_root).as_posix(),
            trigger=trigger,
        )
    if question:
        prompt += f"\n\n用户的问题是：「{question}」—— 先读数据包再回答，数据不够就明说。"
        prompt += "\n（这是用户在终端等的实时答复：正文写清楚，标题仍按契约 ≤40 字。）"

    cmd = [exe, "--agent", agent, "--permission-mode", perm, "-p", prompt]
    model = cfg.get("agent.model")
    if model:
        cmd[3:3] = ["--model", str(model)]

    started = datetime.now(CST)
    log.info("调起子代理：%s（trigger=%s，超时 %ss）", agent, trigger, timeout)
    try:
        proc = subprocess.run(cmd, cwd=str(vault_root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "draft": None, "error": f"子代理超时（{timeout}s）"}
    except OSError as exc:
        return {"ok": False, "draft": None, "error": f"子代理启动失败：{exc}"}

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {"ok": False, "draft": None,
                "error": f"子代理 exit={proc.returncode}：{' / '.join(tail)}"}

    draft_file = _newest_outbox(outbox, started)
    if draft_file is None:
        return {"ok": False, "draft": None,
                "error": "子代理退出码 0 但 outbox 没有新文件（写权限被拒？确认 --permission-mode 未遗漏）"}

    try:
        draft = _parse_outbox(draft_file)
    except OSError as exc:
        return {"ok": False, "draft": None, "error": f"推送稿读取失败：{exc}"}

    draft["file"] = str(draft_file)
    log.info("子代理产出：%s（send=%s, priority=%s）", draft_file.name, draft["send"], draft["priority"])
    return {"ok": True, "draft": draft, "error": None,
            "period": period if is_report else None,
            "period_key": period_key if is_report else None}
