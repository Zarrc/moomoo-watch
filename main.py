"""moomoo-watch 主入口 —— 定时任务调的就是这个。

流水线：
    取数 → 补算 → 日历 → 新闻 → 组装数据包 → 子代理出稿 → 去重/冷却 → 推送

用法示例：
    python main.py                          # 按 config.yaml 跑一次（默认 brief）
    python main.py --trigger alert          # 高危事件预警模式
    python main.py --source futu            # 连真 OpenD
    python main.py --mode live              # 真发推送（否则 simulate，只写日志）
    python main.py --no-agent               # 不调子代理，只出数据包
    python main.py --ask "GLD 今天怎么样"    # 交互问答

退出码：0 = 正常（含「没重要事不发」）；1 = 硬失败（配置/环境错误）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 cp1252，中文日志会直接 UnicodeEncodeError 崩掉。
# 必须在任何输出之前把 stdout/stderr 掰成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from core import calendar as cal_mod          # noqa: E402
from core import macro as macro_mod           # noqa: E402
from core import market, news, notifier, packet, rollup, summarize   # noqa: E402
from core.config import Config                # noqa: E402
from core.state import StateManager           # noqa: E402

from datetime import datetime, timezone, timedelta  # noqa: E402
CST = timezone(timedelta(hours=8))            # noqa: E402

log = logging.getLogger("moomoo-watch")


def setup_logging(cfg) -> None:
    logdir = cfg.path_for("logs")
    logdir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = TimedRotatingFileHandler(logdir / "watch.log", when="midnight", backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="moomoo-watch：持仓与市场风险守望")
    p.add_argument("--trigger", choices=["brief", "alert", "ask"], default="brief")
    p.add_argument("--source", choices=["fixture", "futu"], default=None)
    p.add_argument("--mode", choices=["simulate", "live"], default=None)
    p.add_argument("--ask", dest="question", default=None, help="交互提问（隐含 --trigger ask）")
    p.add_argument("--no-agent", action="store_true", help="不调子代理，只产出数据包")
    p.add_argument("--no-push", action="store_true", help="不推送，只跑到出稿为止")
    p.add_argument("--config", default=None, help="配置文件路径（默认项目内 config.yaml）")
    p.add_argument("--rollup", action="store_true",
                   help="跑多周期回滚：日记录 + 预测评分 + 到期周期报告（并入常规简报）")
    p.add_argument("--seed-fixtures", action="store_true",
                   help="跑 rollup 前，把 fixtures/rollup/* 播种进 rollup/（离线验证用）")
    p.add_argument("--rebuild-scorecard", action="store_true",
                   help="只从台账重算命中率（scorecard.json + Self/投资/预测台账/命中率.md）")
    # 隐藏测试钩子：可控时钟，让 session_date / 周界 / 打分可复现（仅测试用）
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    return p.parse_args()


def _push_draft(cfg, draft, no_push: bool) -> bool:
    """把一份推送稿过闸门并发送。返回是否真发成功。所有失败只记日志、不抛。"""
    if not draft.get("send"):
        log.info("子代理判定「没重要事」→ 不推送（合格产出）。")
        return False
    if no_push:
        log.info("--no-push：不推送")
        return False
    channel = notifier.resolve_channel(cfg, draft["priority"])
    state = StateManager(cfg.path_for("state"))
    allowed, reason = state.can_push(draft["title"], draft["priority"], cfg, channel)
    if not allowed:
        log.warning("拦截推送：%s", reason)
        return False
    log.info("推送闸门：%s（priority=%s → 渠道 %s，该渠道今日已用 %d 条）",
             reason, draft["priority"], channel, state.daily_count(channel))
    res = notifier.send(cfg, draft["title"], draft["body"], draft["priority"])
    state.record_push(draft["title"], draft["priority"],
                      str(res.get("channel") or channel), bool(res.get("ok")),
                      simulated=bool(res.get("simulated")))
    if res.get("ok"):
        log.info("推送成功%s", "（simulate，未真发）" if res.get("simulated") else "")
    else:
        log.error("推送失败（不影响主流程）：%s", res.get("error"))
    return bool(res.get("ok"))


def main() -> int:
    args = parse_args()
    try:
        cfg = Config(Path(args.config) if args.config else None)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[致命] 配置加载失败：{exc}", file=sys.stderr)
        return 1

    if args.source:
        cfg.raw["source"] = args.source
    if args.mode:
        cfg.raw["mode"] = args.mode
    if args.question:
        args.trigger = "ask"
    if args.no_agent:
        # 一路贯穿到 rollup（它自己也读 agent.enabled）——否则 --no-agent 只挡简报、挡不住报告
        cfg.raw.setdefault("agent", {})["enabled"] = False

    setup_logging(cfg)
    log.info("=" * 62)
    log.info("启动 | trigger=%s source=%s mode=%s rollup=%s",
             args.trigger, cfg.source, cfg.mode, args.rollup)

    # 可控时钟（仅测试钩子）—— 让 session_date / 周界 / 打分可复现
    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            print(f"[致命] --now 不是合法 ISO 时间：{args.now}", file=sys.stderr)
            return 1
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
    # 只重算命中率 → 从台账派生，不取数、不推送（便于审计 / 幂等验证）
    if args.rebuild_scorecard:
        sc = rollup.rebuild_scorecard(cfg)
        o = sc["overall"]
        log.info("命中率已重算：n=%s hit=%s miss=%s invalidated=%s hit_rate=%s",
                 o["n"], o["hit"], o["miss"], o["invalidated"], o["hit_rate"])
        return 0

    # 启动自检：缺密钥只警告，不拦（simulate 模式不需要密钥）
    if cfg.live_push:
        missing = cfg.missing_secrets()
        ready = [c for c in notifier.declared_channels(cfg) if notifier.channel_ready(cfg, c)]
        if not ready:
            # 一个都没配好 → 真会全失败
            log.error("live 模式：没有任何渠道配好密钥，推送会全部失败。缺失 %s", missing)
        elif missing:
            # 有渠道缺密钥，但有别的可用 → 会降级，不是错误
            log.warning("以下渠道缺密钥，会被降级跳过：%s；本次可用渠道：%s", missing, ready)

    # 1) 取数 + 补算
    raw = market.collect(cfg, now)
    log.info("数据源状态：%s", raw.get("status"))
    if cfg.source == "futu" and not (raw.get("status") or {}).get("ok"):
        log.error("OpenD 不可用：%s", (raw.get("status") or {}).get("detail"))
        log.error("（若只想验证链路，用 --source fixture）")
    enriched = market.enrich(cfg, raw)

    # 2) 日历
    events = cal_mod.upcoming(cfg, now)
    window = cal_mod.in_alert_window(events, cfg)
    if window:
        log.info("⚠️ 有 %d 个高危事件落在预警窗口内", len(window))
        if args.trigger == "brief":
            args.trigger = "alert"      # 高危窗口优先走 alert 模式
            log.info("已自动切换到 alert 模式")

    # 3) 新闻
    kws = news.keywords_for(enriched["positions"], enriched["watchlist"], cfg)
    news_items = news.fetch(cfg, kws)

    # 3.5) 宏观快照（主刻度 = 美联储利率预期；永不抛，取不到就给空壳）
    macro_snapshot = macro_mod.snapshot(cfg, now)

    # 4) 数据包
    pkt = packet.build(cfg, trigger=args.trigger, market=enriched,
                       events=events, news_items=news_items,
                       macro=macro_snapshot, now=now)
    packet.write(cfg, pkt)

    if args.no_agent and not args.rollup:
        log.info("--no-agent：停在数据包，未调子代理")
        return 0

    # 5a) rollup 模式：先出常规简报，再出各周期报告（**同进程内串行**，避免 outbox mtime 竞态）
    if args.rollup:
        if not args.no_agent:
            brief = summarize.run(cfg, trigger=args.trigger, question=args.question,
                                  data_path=cfg.path_for("data") / "latest.json", now=now)
            if brief["ok"]:
                d = brief["draft"]
                log.info("简报稿：%s | send=%s priority=%s | 标题：%s",
                         d.get("file"), d["send"], d["priority"], d["title"])
                _push_draft(cfg, d, args.no_push)
            else:
                log.error("简报子代理未产出（不阻断 rollup）：%s", brief["error"])

        rr = rollup.run(cfg, packet=pkt, macro=macro_snapshot, now=now,
                        seed_fixtures=args.seed_fixtures)
        if rr["errors"]:
            log.error("rollup 有周期失败（水位未推进，下次重试）：%s", rr["errors"])
        for d in rr["drafts"]:
            period = d.get("period")
            if period and not (cfg.get(f"rollup.periods.{period}") or {}).get("push", True):
                log.info("周期 %s 配置为不推送 → 只写全文", period)
                continue
            log.info("周期报告稿：%s | 标题：%s", d.get("file"), d.get("title"))
            _push_draft(cfg, d, args.no_push)
        return 0

    # 5b) 常规单稿流程
    result = summarize.run(cfg, trigger=args.trigger, question=args.question, now=now)
    if not result["ok"]:
        log.error("子代理未产出：%s", result["error"])
        return 0 if "超时" in str(result["error"]) else 1

    draft = result["draft"]
    log.info("推送稿：%s | send=%s priority=%s", draft.get("file"), draft["send"], draft["priority"])
    log.info("标题：%s", draft["title"])
    _push_draft(cfg, draft, args.no_push)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                        # noqa: BLE001 —— 兜底：绝不让调度器看到崩溃栈
        logging.getLogger("moomoo-watch").exception("未捕获异常：%s", exc)
        sys.exit(1)
