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

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_PROMPT = (
    "读 {data} 这个数据包，按你的 {trigger} 模式产出一份手机推送稿，"
    "写到 {outbox} 下。严格遵守你自己的输出契约（send / priority / title 三个字段 + 正文），"
    "title 必须 ≤40 字且自包含（Server酱免费版只显示标题）。"
    "数据包里没有的数字一律写「无」，不许估算。"
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
    """从本文件往上找含 .claude 的目录 = vault 根。

    不用数 parent 层数：路径深度改一次就得重新数一遍，数错是静默的
    （子代理 cwd 错了 → 相对路径全错 → 还会 exit 0）。往上找锚点更稳。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".claude").is_dir():
            return parent
    raise RuntimeError("找不到 vault 根（往上没有 .claude 目录）—— 项目是否被移动了？")


def run(cfg, *, trigger: str = "brief", question: str | None = None) -> Dict[str, Any]:
    """调子代理 → 读回推送稿。返回 {ok, draft, error}。任何失败都不抛。"""
    if not cfg.get("agent.enabled", True):
        return {"ok": False, "draft": None, "error": "agent.enabled = false"}

    exe = cfg.get("agent.claude_exe")
    agent = cfg.get("agent.agent_name", "portfolio-watch")
    perm = cfg.get("agent.permission_mode", "acceptEdits")
    timeout = int(cfg.get("agent.timeout_seconds", 300))
    data_path = cfg.path_for("data") / "latest.json"
    outbox = cfg.path_for("outbox", create=True)
    vault_root = _vault_root()

    if not Path(exe).is_file():
        return {"ok": False, "draft": None, "error": f"claude 可执行文件不存在：{exe}"}

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
    return {"ok": True, "draft": draft, "error": None}
