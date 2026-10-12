#!/usr/bin/env python3
"""research/run_research.py —— 市场观察生产线的调度大脑（计划任务每天 09:10 (+08) 调）。

一条任务、聪明 runner：自己判断该干什么，不该干的直接空转退出。
  1. 刷新 FRED 宏观（周六顺带刷 13F——季度数据没必要天天碰）
  2. OpenD 可达才做市场线：判「上一美股交易日 P」
       P=普通交易日  → market_daily（盘前→盘后当日总结）
       P=周五        → + market_weekly（本周板块走向）
       P=月末交易日或 event_dates 登记日 → + market_event（总结+情绪推演）
  3. 每个要出的报告：抓数落盘 → headless 调 investment-research 子代理
  4. 一切写 data/research/run_research.log（追加）

用法：
  python research/run_research.py            # 计划任务入口（自动判断）
  python research/run_research.py --dry-run  # 只打印将做什么，不动任何东西
  python research/run_research.py --force --modes daily,weekly   # 手动指定
退出码：0=正常（含空转）；1=OpenD 不可达（市场线跳过）；2=参数/数据错误
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data" / "research"
LOG_PATH = DATA_DIR / "run_research.log"
CFG = yaml.safe_load((RESEARCH_DIR / "research.yaml").read_text(encoding="utf-8"))
PROD_CFG = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8")) if (REPO_ROOT / "config.yaml").exists() else {}

MODE_DIR = {"daily": "daily", "weekly": "weekly", "event": "event"}
MODE_TITLE = {
    "daily": "市场观察（盘前→盘后）",
    "weekly": "周度板块走向",
    "event": "事件/月末观察与情绪推演",
}


def log(msg: str) -> None:
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def vault_root() -> Path:
    p = REPO_ROOT
    while p != p.parent:
        if (p / ".claude").is_dir():
            return p
        p = p.parent
    raise RuntimeError("向上找不到 .claude（vault 根）——本 runner 必须住在 vault 内运行")


def run(cmd: list[str], timeout: int = 240) -> int:
    log(f"$ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    try:
        p = subprocess.run([str(c) for c in cmd], cwd=str(REPO_ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        tail = (p.stdout or "").strip().splitlines()[-3:]
        for t in tail:
            log(f"  | {t}")
        if p.returncode != 0:
            log(f"  退出码 {p.returncode}" + (f"；stderr: {(p.stderr or '').strip()[-200:]}" if p.stderr else ""))
        return p.returncode
    except subprocess.TimeoutExpired:
        log(f"  超时（{timeout}s）")
        return -1


def invoke_agent(mode: str, date: str, extra: str = "") -> None:
    exe = PROD_CFG.get("agent", {}).get("claude_exe") or ""
    if not exe:
        import shutil
        exe = shutil.which("claude") or ""
    if not Path(exe).is_file():
        log(f"⚠️ claude 可执行不可用（{exe or '未找到'}）——{mode} 报告跳过，数据包已落盘可手动跑")
        return
    prompt = (
        f"模式：market_{mode}（{MODE_TITLE[mode]}）。今天 {datetime.now().strftime('%Y-%m-%d')}，"
        f"目标交易日 {date}。\n"
        f"数据包（vault 根为工作目录，只读）：Self/To-do's/project/moomoo-watch/data/research/"
        f"{'market_week_' + date + '.json' if mode == 'weekly' else 'market_' + date + '.json'}"
        + (f" 与 Self/To-do's/project/moomoo-watch/data/research/macro.json" if mode == "event" else "")
        + f"。\n"
        f"输出文件（固定）：Self/投资/研究/market/{MODE_DIR[mode]}/{date} {MODE_TITLE[mode]}.md\n"
        f"{extra}"
        f"按你的定义执行该模式的全部输出规范（含数据截止标注与沙盘声明）。"
        f"完成后输出一行报告路径即可。"
    )
    cmd = [exe, "--agent", "investment-research", "--permission-mode", "acceptEdits", "-p", prompt]
    log(f"调起 investment-research（{mode}，超时 1500s）")
    run(cmd, timeout=1500)


def decide_modes(date: str, days: list[str], cfg: dict, forced: str | None) -> list[str]:
    if forced:
        return [m.strip() for m in forced.split(",") if m.strip() in MODE_DIR]
    modes = ["daily"]
    if datetime.strptime(date, "%Y-%m-%d").weekday() == 4:
        modes.append("weekly")
    idx = days.index(date) if date in days else -1
    nxt = days[idx + 1] if 0 <= idx < len(days) - 1 else None
    month_end = nxt is not None and nxt[:7] != date[:7]
    event_listed = date in [str(d)[:10] for d in cfg.get("market", {}).get("event_dates", [])]
    if month_end or event_listed:
        modes.append("event")
    return modes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="跳过交易日判定，直接以 --date（缺省=最近推断）执行")
    ap.add_argument("--date", default=None)
    ap.add_argument("--modes", default=None, help="daily,weekly,event 手动指定（配 --force 用）")
    args = ap.parse_args()

    log("=== run_research 启动 ===")

    # 1) 宏观刷新（不依赖 OpenD）；周六顺带 13F
    if not args.dry_run:
        run([sys.executable, RESEARCH_DIR / "fetch_research.py", "macro"])
        if datetime.now().weekday() == 5:  # 周六
            run([sys.executable, RESEARCH_DIR / "fetch_research.py", "13f"], timeout=600)

    # 2) OpenD 探测（dry-run 容忍离线：用工作日启发演示决策）
    from fetch_market import probe_opend, prev_trading_day, open_ctx
    mk = CFG["market"]
    opend_ok = probe_opend(mk["opend_host"], int(mk["opend_port"]))
    if not opend_ok and not args.dry_run:
        log("OpenD 不可达 —— 市场线跳过（宏观已刷）。开 moomoo 后可手动补跑。")
        return 1

    ctx = None
    try:
        if opend_ok:
            ctx = open_ctx(mk["opend_host"], int(mk["opend_port"]))
        if args.date:
            date, days = args.date, []
        elif ctx:
            date, days = prev_trading_day(ctx, CFG)
        else:  # dry-run 且 OpenD 离线：工作日启发（与 fetch_market.prev_trading_day 同语义）
            from datetime import timedelta
            from fetch_market import ET as _ET
            et_now = datetime.now(_ET)
            d, days = et_now, []
            while len(days) < 15:
                if d.weekday() < 5:
                    days.append(d.date().isoformat())
                d -= timedelta(days=1)
            # 与 fetch_market.prev_trading_day 同语义：ET≥20:05 且今天为交易日 → 今天
            if et_now.time().strftime("%H:%M") >= "20:05":
                date = et_now.date().isoformat()
            else:
                cand = [x for x in days if x < et_now.date().isoformat()]
                if not cand:
                    log("dry-run：非交易日，空转。")
                    return 0
                date = cand[0]
        modes = decide_modes(date, days or [date], CFG, args.modes if args.force else None)
        log(f"目标交易日 {date}，将产出：{'+'.join(modes)}")
        if args.dry_run:
            log("dry-run：到此为止，未抓数、未调代理。")
            return 0
        run([sys.executable, RESEARCH_DIR / "fetch_market.py", "--date", date]
            + (["--weekly"] if "weekly" in modes else []), timeout=300)
        for m in modes:
            extra = ("注意：事件/月末模式要联动宏观包（fedfunds/CPI 方向）与 VIX；"
                     "情绪只做证据链推演（fed_watch 若在 latest.json 且非 fixture 才可用），禁预测式断言。\n"
                     if m == "event" else "")
            invoke_agent(m, date, extra)
    finally:
        if ctx:
            ctx.close()
    log("=== run_research 结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
