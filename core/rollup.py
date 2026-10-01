"""多周期回滚 + 可证伪预测台账 —— 让「无状态的一次性简报」变成「可回看、可打分的分级汇总」。

三层汇总（青铜 → 白银 → 黄金）：
    L0  data/<时间戳>.json            原始数据包（已有）
            ↓  确定性脚本压（**不是 LLM**）
    L1  rollup/daily/YYYY-MM-DD.json 日记录（本模块）
            ↓
    L2  Self/投资/{日评,周报,月报,季报,年报}/…  报告全文（LLM 写）
        outbox/*.md                              手机短摘要（LLM 写，复用现有推送链路）

🚩 三条不可动摇的分工：
  1. **数字 = 脚本，判断 = LLM。** LLM 只写 `{方向, 关键位, 失效条件, 置信度}`；
     脚本填参考价、做 **100% 的打分**（`score_call` 是纯函数，可单测）。
  2. **机器记录在项目内（gitignore），人读报告在 vault。** 日记录含真实收盘与权重，
     仓库是 **PUBLIC** —— 所以 `rollup/` 必须在 `.gitignore` 里（已在）。
  3. **单一入口自己判断「该出什么」**（`due_periods` + 水位），不靠一堆计划任务。

🥇 高危静默失败（都在本模块）：
  - **水位提前推进**：报告失败却已记水位 = 该期永久丢失且毫无征兆。
    → 水位只在**成功后**、`try/finally` 里最后写。
  - **对无行情标的打分**：`MY.*` 收盘为 None。白名单在**提示词与解析器两处**都拦。
  - **首跑爆炸**：没有 `bootstrap_mode: seed` 会一次触发 5 个 report。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

PERIODS = ("daily", "weekly", "monthly", "quarterly", "yearly")
PERIOD_DIR = {"daily": "日评", "weekly": "周报", "monthly": "月报",
              "quarterly": "季报", "yearly": "年报"}
PERIOD_LABEL = {"daily": "日评", "weekly": "周报", "monthly": "月报",
                "quarterly": "季报", "yearly": "年报"}

# 预测块：<!-- CALL:BEGIN --> … <!-- CALL:END -->，内嵌一个 ```yaml 围栏
_CALL_BLOCK = re.compile(r"<!--\s*CALL:BEGIN\s*-->(.*?)<!--\s*CALL:END\s*-->", re.S)
_YAML_FENCE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)

_VALID_DIRECTIONS = ("up", "down", "flat")
# 🚩 只支持「收盘价」失效条件 —— 日记录只有收盘序列，`touch_*` 需要盘中高低点，
#    当前拿不到。宁可限死取值域，也不写一个永远判不出来的类型（那是静默的空判）。
_VALID_INVALIDATION = ("close_below", "close_above")


# ===========================================================================
# 日期 / 周期工具
# ===========================================================================
def _as_cst(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=CST)
    return now.astimezone(CST)


def _holidays(cfg) -> set:
    return {str(h) for h in (cfg.get("rollup.market_holidays") or [])}


def _is_session_day(cfg, d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _holidays(cfg)


def session_date(cfg, now: Optional[datetime] = None) -> date:
    """按 +08:00 推「最近一个已收盘的美股交易日」。

    美股 16:00 ET 收盘 ≈ 次日 04:00–05:00 (+08:00)，取 **05:00 保守结算时刻**；
    再配合 `session_boundary_hour`（默认 9）—— 该小时之前算「上一交易日」。
    跳过周末与 `rollup.market_holidays`。
    """
    now = _as_cst(now)
    boundary = int(cfg.get("rollup.session_boundary_hour", 9))
    d = now.date()
    if now.hour < boundary:
        d -= timedelta(days=1)
    for _ in range(40):                       # 40 天兜底（连续假期不会这么长）
        settle = datetime.combine(d + timedelta(days=1), time(5, 0), tzinfo=CST)
        if _is_session_day(cfg, d) and now >= settle:
            return d
        d -= timedelta(days=1)
    raise RuntimeError("session_date 回溯超过 40 天 —— 检查 rollup.market_holidays 是否填错")


def period_key(period: str, d: date) -> str:
    period = period.lower()
    if period == "daily":
        return d.isoformat()
    if period == "weekly":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if period == "monthly":
        return f"{d.year:04d}-{d.month:02d}"
    if period == "quarterly":
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    if period == "yearly":
        return f"{d.year:04d}"
    raise ValueError(f"未知周期：{period!r}")


# ===========================================================================
# 打分（纯函数 —— 单独可测，不碰 I/O）
# ===========================================================================
def score_call(call: Dict[str, Any], ref_close: float, next_close: float,
               next_high: Optional[float], next_low: Optional[float], cfg) -> Dict[str, Any]:
    """给一条 call 与「下一个有该标的收盘的日记录」打分。

    返回 `{outcome, direction_hit, invalidated, actual_direction, move_pct, ...}`。
    `outcome ∈ {hit, miss, invalidated}`；判 flat 用 `score_deadband_pct` 死区。
    """
    deadband = float(cfg.get("rollup.ledger.score_deadband_pct", 0.1)) / 100.0
    move = (float(next_close) - float(ref_close)) / float(ref_close)
    actual = "flat" if abs(move) < deadband else ("up" if move > 0 else "down")

    inval = call.get("invalidation") or {}
    itype = str(inval.get("type") or "")
    level = inval.get("level")
    invalidated = False
    if level is not None:
        level = float(level)
        if itype == "close_below" and float(next_close) < level:
            invalidated = True
        elif itype == "close_above" and float(next_close) > level:
            invalidated = True

    direction = str(call.get("direction") or "").lower()
    direction_hit = (direction == actual)
    if invalidated:
        outcome = "invalidated"
    elif direction_hit:
        outcome = "hit"
    else:
        outcome = "miss"

    return {
        "outcome": outcome,
        "direction_hit": direction_hit,
        "invalidated": invalidated,
        "actual_direction": actual,
        "move_pct": round(move * 100, 4),
        "ref_close": round(float(ref_close), 4),
        "next_close": round(float(next_close), 4),
        "next_high": next_high,
        "next_low": next_low,
    }


# ===========================================================================
# 日记录 I/O
# ===========================================================================
def daily_dir(cfg) -> Path:
    return cfg.path_for("rollup") / "daily"


def day_record_path(cfg, d: date) -> Path:
    return daily_dir(cfg) / f"{d.isoformat()}.json"


def load_day_record(cfg, d: date) -> Optional[Dict[str, Any]]:
    p = day_record_path(cfg, d)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("日记录读取失败 %s：%s", p, exc)
        return None


def list_day_records(cfg) -> List[Dict[str, Any]]:
    d = daily_dir(cfg)
    if not d.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("日记录跳过（坏文件）%s：%s", f, exc)
    out.sort(key=lambda r: str(r.get("session_date") or ""))
    return out


def write_day_record(cfg, rec: Dict[str, Any]) -> Path:
    d = daily_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rec['session_date']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    _prune_day_records(cfg)
    return p


def _prune_day_records(cfg) -> None:
    keep = int(cfg.get("rollup.keep_days", 400))
    files = sorted(daily_dir(cfg).glob("*.json"))
    for f in files[:-keep] if keep > 0 else []:
        try:
            f.unlink()
        except OSError:
            pass


# ===========================================================================
# 预测台账 I/O
# ===========================================================================
def ledger_path(cfg) -> Path:
    return cfg.path_for("rollup") / "ledger.json"


def ledger_load(cfg) -> List[Dict[str, Any]]:
    p = ledger_path(cfg)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("台账读取失败（按空继续）：%s", exc)
        return []
    return list(data.get("entries") or []) if isinstance(data, dict) else list(data or [])


def ledger_save(cfg, entries: List[Dict[str, Any]]) -> None:
    p = ledger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ===========================================================================
# 预测块解析 + 校验
# ===========================================================================
def parse_call_block(text: str) -> Dict[str, Any]:
    """从日评 md 里取 `<!-- CALL:BEGIN/END -->` 之间的 yaml。

    返回 `{present, calls, error}`：
      - `present=False`             → 没写预测块（`call_missing`，合格产出，不算错）
      - `error` 非空                → 块损坏（`call_parse_error`，**要大声记日志**）
    """
    m = _CALL_BLOCK.search(text or "")
    if not m:
        return {"present": False, "calls": [], "error": None}
    inner = m.group(1)
    fence = _YAML_FENCE.search(inner)
    body = fence.group(1) if fence else inner
    try:
        data = yaml.safe_load(body) or {}
    except yaml.YAMLError as exc:
        return {"present": True, "calls": [], "error": f"CALL 块 YAML 解析失败：{exc}"}
    if not isinstance(data, dict):
        return {"present": True, "calls": [], "error": "CALL 块不是映射（缺 calls:）"}
    calls = data.get("calls")
    if calls is None:
        return {"present": True, "calls": [], "error": None}      # 空块 = 当天不出预测
    if not isinstance(calls, list):
        return {"present": True, "calls": [], "error": "calls 不是列表"}
    return {"present": True, "calls": calls, "error": None}


def validate_call(cfg, raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """白名单 + 取值域校验。返回 (归一化 call, 拒绝原因)。

    🚩 白名单是**硬约束**：多币种账户的「组合方向」无法定义；`MY.*` 拿不到行情无法打分；
    打分需要干净收盘序列。白名单外的 call **脚本直接丢弃并记 `call_rejected`**。
    """
    if not isinstance(raw, dict):
        return None, "call 不是映射"
    inst = str(raw.get("instrument") or "")
    whitelist = [str(x) for x in (cfg.get("rollup.call_instruments") or [])]
    if inst not in whitelist:
        return None, f"标的 {inst!r} 不在白名单 {whitelist}"

    direction = str(raw.get("direction") or "").lower()
    if direction not in _VALID_DIRECTIONS:
        return None, f"direction 非法：{raw.get('direction')!r}（限 {_VALID_DIRECTIONS}）"

    inval = raw.get("invalidation")
    if not isinstance(inval, dict):
        return None, "invalidation 缺失或不是映射"
    itype = str(inval.get("type") or "")
    if itype not in _VALID_INVALIDATION:
        return None, f"invalidation.type 非法：{itype!r}（限 {_VALID_INVALIDATION}）"
    try:
        level = float(inval.get("level"))
    except (TypeError, ValueError):
        return None, f"invalidation.level 非数值：{inval.get('level')!r}"

    conf = raw.get("confidence")
    if conf is not None:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            return None, f"confidence 非数值：{raw.get('confidence')!r}"
        if not 0.0 <= conf <= 1.0:
            return None, f"confidence 越界：{conf}（限 0–1）"

    key_levels: List[float] = []
    for lv in (raw.get("key_levels") or []):
        try:
            key_levels.append(float(lv))
        except (TypeError, ValueError):
            return None, f"key_levels 含非数值：{lv!r}"

    return {
        "instrument": inst,
        "direction": direction,
        "key_levels": key_levels,
        "invalidation": {"type": itype, "level": level},
        "confidence": conf,
    }, None


def append_calls(cfg, entries: List[Dict[str, Any]], session: date,
                 calls: List[Dict[str, Any]], ref_closes: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]]]:
    """把当日校验过的 call 合并进台账。返回 (新增数, 拒绝明细)。

    去重：`(session_date, instrument)` 已存在且**已打分** → 跳过（防重复计数）；
    未打分且 `refresh_call_same_session=false` → 保留原条目（同日多跑不覆盖）。
    """
    refresh = bool(cfg.get("rollup.refresh_call_same_session", False))
    rejected: List[Dict[str, Any]] = []
    added = 0
    for raw in calls:
        norm, reason = validate_call(cfg, raw)
        if norm is None:
            rejected.append({"instrument": (raw or {}).get("instrument"), "reason": reason})
            log.warning("丢弃一条预测（%s）：%s", reason, (raw or {}).get("instrument"))
            continue
        existing = next((e for e in entries
                         if e.get("session_date") == session.isoformat()
                         and e.get("instrument") == norm["instrument"]), None)
        if existing is not None:
            if not refresh:
                log.info("跳过已存在的预测：%s@%s（refresh_call_same_session=false）",
                         norm["instrument"], session.isoformat())
                continue
            if existing.get("score") is not None:
                log.info("跳过已打分的预测：%s@%s（不覆盖）", norm["instrument"], session.isoformat())
                continue
            entries.remove(existing)
        entries.append({
            "session_date": session.isoformat(),
            "instrument": norm["instrument"],
            "direction": norm["direction"],
            "key_levels": norm["key_levels"],
            "invalidation": norm["invalidation"],
            "confidence": norm["confidence"],
            "ref_close": ref_closes.get(norm["instrument"]),
            "score": None,
            "created_at": datetime.now(CST).isoformat(timespec="seconds"),
        })
        added += 1
    return added, rejected


# ===========================================================================
# 打分
# ===========================================================================
def score_pending(cfg, now: Optional[datetime] = None, *,
                  entries: Optional[List[Dict[str, Any]]] = None,
                  records: Optional[List[Dict[str, Any]]] = None,
                  save: bool = True) -> Dict[str, Any]:
    """给所有未打分的台账条目打分（**纯脚本**，不是 LLM）。

    - 取**下一个有该标的收盘的日记录**（跳过周末/假期/缺口），而非 `date+1`
    - 无下一条记录 → 留在 `pending`，**不编**
    - 超 `max_score_lag_days` → `unscored_stale`（计入 coverage，**绝不静默丢弃**）
    - 已打分的条目（`score != null`）→ skip，防同日多跑重复计数

    `entries` / `records` / `save` 可注入，便于离线单测（不碰文件系统）。
    """
    if entries is None:
        entries = ledger_load(cfg)
    if records is None:
        records = list_day_records(cfg)
    by_day = {str(r.get("session_date")): r for r in records}
    ordered = sorted(by_day)
    lag_limit = int(cfg.get("rollup.ledger.max_score_lag_days", 7))
    sess_today = session_date(cfg, now)
    changed = False
    n_scored = 0

    for e in entries:
        if e.get("score") is not None:
            continue
        inst = str(e.get("instrument") or "")
        sd = str(e.get("session_date") or "")

        # 补参考价（首跑时 ref_close 可能是 None）
        if e.get("ref_close") in (None, 0):
            r0 = by_day.get(sd) or {}
            if (r0.get("closes") or {}).get(inst) is not None:
                e["ref_close"] = r0["closes"][inst]
                changed = True
        ref = e.get("ref_close")

        nxt = next((by_day[d] for d in ordered if d > sd
                    and (by_day[d].get("closes") or {}).get(inst) is not None), None)

        if nxt is not None and ref:
            sc = score_call(e, ref, nxt["closes"][inst],
                            (nxt.get("highs") or {}).get(inst),
                            (nxt.get("lows") or {}).get(inst), cfg)
            sc["scored_by_session"] = nxt["session_date"]
            sc["lag_sessions"] = max(0, ordered.index(nxt["session_date"]) - ordered.index(sd) - 1)
            e["score"] = sc
            changed = True
            n_scored += 1
            log.info("打分 %s %s@%s → %s（%+.2f%%，滞后 %d 个交易日）",
                     inst, e.get("direction"), sd, sc["outcome"], sc["move_pct"], sc["lag_sessions"])
        else:
            try:
                age = (sess_today - date.fromisoformat(sd)).days
            except ValueError:
                age = 0
            if age > lag_limit:
                e["score"] = {"outcome": "unscored_stale",
                              "reason": f"超过 {lag_limit} 天仍无下一条记录", "age_days": age}
                changed = True
                log.warning("预测超 %d 天仍无后续记录 → unscored_stale：%s@%s", lag_limit, inst, sd)

    if changed and save:
        ledger_save(cfg, entries)
    return {"changed": changed, "scored": n_scored, "entries": entries}


# ===========================================================================
# 命中率（完全由台账派生 —— 可重建）
# ===========================================================================
def summarize_scorecard(cfg, entries: Optional[List[Dict[str, Any]]] = None,
                        records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    entries = ledger_load(cfg) if entries is None else entries
    records = list_day_records(cfg) if records is None else records

    scored = [e for e in entries if (e.get("score") or {}).get("outcome") in ("hit", "miss", "invalidated")]
    hit = sum(1 for e in scored if e["score"]["outcome"] == "hit")
    miss = sum(1 for e in scored if e["score"]["outcome"] == "miss")
    inval = sum(1 for e in scored if e["score"]["outcome"] == "invalidated")
    pending = sum(1 for e in entries if e.get("score") is None)
    stale = sum(1 for e in entries if (e.get("score") or {}).get("outcome") == "unscored_stale")
    n = len(scored)

    def _rate(h, tot):
        return round(h / tot, 4) if tot else None       # n==0 → None，不是 0（守「宁可留空」）

    by_inst: Dict[str, Any] = {}
    for e in scored:
        k = str(e.get("instrument"))
        b = by_inst.setdefault(k, {"n": 0, "hit": 0, "miss": 0, "invalidated": 0})
        b["n"] += 1
        b[e["score"]["outcome"]] += 1
    for k, b in by_inst.items():
        b["hit_rate"] = _rate(b["hit"], b["n"])

    # 覆盖只在「实观测日」上统计 —— 回填日根本没有当日 packet，不可能有 call，
    # 把它算进 call_missing 会虚增（那是「没机会出」，不是「该出没出」）。
    live = [r for r in records if not (r.get("provenance") or {}).get("backfilled")]
    backfilled_days = len(records) - len(live)
    missing = sum(1 for r in live
                  if not (r.get("call_stats") or {}).get("present")
                  or (r.get("call_stats") or {}).get("parsed", 0) == 0
                  and not (r.get("call_stats") or {}).get("parse_error"))
    parse_err = sum(1 for r in live if (r.get("call_stats") or {}).get("parse_error"))
    rejected = sum(len((r.get("call_stats") or {}).get("rejected") or []) for r in live)

    return {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "overall": {"n": n, "hit": hit, "miss": miss, "invalidated": inval,
                    "hit_rate": _rate(hit, n)},
        "by_instrument": by_inst,
        "coverage": {
            "days_recorded": len(records),
            "backfilled_days": backfilled_days,
            "pending": pending,
            "unscored_stale": stale,
            "call_missing": missing,
            "call_parse_error": parse_err,
            "call_rejected": rejected,
        },
        "recent": sorted(
            [{"session_date": e["session_date"], "instrument": e["instrument"],
              "direction": e["direction"], "outcome": e["score"]["outcome"],
              "move_pct": e["score"].get("move_pct")} for e in scored],
            key=lambda x: x["session_date"])[-20:],
    }


def scorecard_path(cfg) -> Path:
    return cfg.path_for("rollup") / "scorecard.json"


def write_scorecard(cfg) -> Dict[str, Any]:
    sc = summarize_scorecard(cfg)
    p = scorecard_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    _write_scorecard_note(cfg, sc)
    return sc


def _write_scorecard_note(cfg, sc: Dict[str, Any]) -> None:
    """把命中率写成 vault 里人读的一页 `Self/投资/预测台账/命中率.md`。

    这是**脚本**写的（确定性），不是 LLM —— 预测台账的正确性不该由 LLM 定义。
    """
    try:
        base = cfg.vault_path_for("vault_invest")
        out = base / "预测台账"
        out.mkdir(parents=True, exist_ok=True)
        o, cov = sc["overall"], sc["coverage"]
        rate = "无（尚无已打分预测）" if o["hit_rate"] is None else f"{o['hit_rate'] * 100:.1f}%"
        lines = [
            "# 预测台账 · 命中率",
            "",
            f"> 自动生成（脚本派生自 `rollup/ledger.json`），最后更新 {sc['generated_at']}。",
            "> 预测 = 「次日方向 + 关键位 + 失效条件」，**次日由脚本判对错**，非人工点评。",
            "",
            "## 总命中率",
            "",
            f"- 已打分 **{o['n']}** 条（命中 {o['hit']} / 落空 {o['miss']} / 失效 {o['invalidated']}）",
            f"- **命中率：{rate}**",
            "",
            "## 分标的",
            "",
        ]
        if sc["by_instrument"]:
            for k, b in sc["by_instrument"].items():
                r = "—" if b["hit_rate"] is None else f"{b['hit_rate'] * 100:.1f}%"
                lines.append(f"- `{k}`：{b['n']} 条，命中率 {r}")
        else:
            lines.append("- （暂无）")
        lines += [
            "",
            "## 覆盖（排除项**不计入命中率分母**，也绝不静默丢弃）",
            "",
            f"- 日记录：{cov['days_recorded']}（其中回填 {cov['backfilled_days']}）· "
            f"待打分 pending：{cov['pending']} · 超期未打分：{cov['unscored_stale']}",
            f"- 未出预测 call_missing：{cov['call_missing']} · 解析失败 call_parse_error：{cov['call_parse_error']}"
            f" · 被拒 call_rejected：{cov['call_rejected']}",
            "",
            "## 最近 20 条",
            "",
        ]
        if sc["recent"]:
            lines += ["| 日期 | 标的 | 方向 | 结果 | 当日涨跌 |", "|---|---|---|---|---|"]
            for r in sc["recent"]:
                mv = "—" if r["move_pct"] is None else f"{r['move_pct']:+.2f}%"
                lines.append(f"| {r['session_date']} | {r['instrument']} | {r['direction']} |"
                             f" {r['outcome']} | {mv} |")
        else:
            lines.append("（暂无）")
        lines += ["", "---", "", "本内容由程序汇总公开信息生成，不构成投资建议。", ""]
        (out / "命中率.md").write_text("\n".join(lines), encoding="utf-8")
    except (OSError, KeyError, RuntimeError) as exc:
        log.warning("命中率笔记写入失败（不影响主流程）：%s", exc)


def rebuild_scorecard(cfg) -> Dict[str, Any]:
    """从台账 + 日记录**重算** scorecard —— 便于审计（两次输出应一致）。"""
    return write_scorecard(cfg)


# ===========================================================================
# 水位 / 到期周期
# ===========================================================================
def due_path(cfg) -> Path:
    return cfg.path_for("rollup") / "due.json"


def load_watermark(cfg) -> Dict[str, Any]:
    p = due_path(cfg)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def watermark_map(wm: Dict[str, Any]) -> Dict[str, str]:
    return dict(wm.get("watermark") or {})


def due_periods(cfg, sess: date, wm: Dict[str, str]) -> List[Tuple[str, str]]:
    """本 session 该出哪些周期。daily 也走水位 —— 同日多跑不重复出报告。"""
    periods_cfg = cfg.get("rollup.periods") or {}
    due: List[Tuple[str, str]] = []
    for name in PERIODS:
        if not (periods_cfg.get(name) or {}).get("enabled"):
            continue
        pk = period_key(name, sess)
        last = wm.get(name)
        if last != pk:                     # daily 与其余同规则：变了才出
            due.append((name, pk))
    return due


def _resolve_periods(cfg, sess: date, wm: Dict[str, str]) -> List[Tuple[str, str]]:
    """首次运行保护：`bootstrap_mode: seed` → 只登记水位、只出 daily。

    🥇 不做这一步，首跑会**同时触发 5 个 report**（5×900s + 5 条推送打爆 Server酱 5 条/天）。
    """
    if not wm and str(cfg.get("rollup.bootstrap_mode", "seed")).lower() == "seed":
        log.warning("首次运行（bootstrap_mode=seed）：只登记水位、只出 daily —— 不一次性触发 5 个 report")
        seed = {name: period_key(name, sess) for name in PERIODS}
        save_watermark(cfg, seed, sess)
        return [("daily", seed["daily"])]
    return due_periods(cfg, sess, wm)


def save_watermark(cfg, wm_map: Dict[str, str], sess: Optional[date] = None) -> None:
    p = due_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "watermark": dict(wm_map),
        "last_session": sess.isoformat() if sess else None,
        "_updated_at": datetime.now(CST).isoformat(timespec="seconds"),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ===========================================================================
# 报告包
# ===========================================================================
def _days_in_period(cfg, period: str, pk: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in list_day_records(cfg):
        try:
            d = date.fromisoformat(str(r.get("session_date")))
        except (TypeError, ValueError):
            continue
        if period_key(period, d) == pk:
            out.append(r)
    return out


def _aggregate(cfg, days: List[Dict[str, Any]]) -> Dict[str, Any]:
    """周期内每个标的：起止收盘、区间涨跌、最大/最小、样本数。数字全由脚本算。"""
    codes: List[str] = []
    for r in days:
        for c in (r.get("closes") or {}):
            if c not in codes:
                codes.append(c)
    out: Dict[str, Any] = {}
    for c in codes:
        vals = [((r.get("closes") or {}).get(c)) for r in days]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        first, last = vals[0], vals[-1]
        out[c] = {
            "samples": len(vals),
            "first_close": first,
            "last_close": last,
            "change_pct": round((last - first) / first * 100, 4) if first else None,
            "max_close": max(vals),
            "min_close": min(vals),
        }
    return out


def build_report_bundle(cfg, period: str, pk: str,
                        days: List[Dict[str, Any]], macro: Dict[str, Any],
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    scorecard = summarize_scorecard(cfg)
    degraded = sorted({f for r in days for f in (r.get("provenance") or {}).get("degraded_fields") or []})
    backfilled = [r["session_date"] for r in days if (r.get("provenance") or {}).get("backfilled")]
    warnings: List[str] = []
    if backfilled:
        warnings.append(f"以下交易日为**回填**（只有日 K 收盘，缺新闻/日历/告警）：{backfilled} —— "
                        f"报告里这些字段一律写「无」，不要让回填的一周看起来像完整观测的一周")
    if degraded:
        warnings.append(f"这些字段有缺失：{degraded}")
    if (macro or {}).get("_fixture"):
        warnings.append("🚩 本包宏观数据为**样例（fixture）**，不是真实数据 —— 任何输出必须标注「样例数据」")

    return {
        "period": period,
        "period_key": pk,
        "label": PERIOD_LABEL.get(period, period),
        "generated_at": (now or datetime.now(CST)).isoformat(timespec="seconds"),
        "session_dates": [r["session_date"] for r in days],
        "day_records": days,
        "aggregates": _aggregate(cfg, days),
        "macro": macro,
        "scorecard": scorecard,
        "call_instruments": list(cfg.get("rollup.call_instruments") or []),
        "call_contract": {
            "direction": list(_VALID_DIRECTIONS),
            "invalidation_type": list(_VALID_INVALIDATION),
            "confidence": "0–1 的小数",
            "note": "call 只能点 call_instruments 内、且当日收盘取得到的标的；"
                    "只能 close_below / close_above 两种失效条件",
        },
        "warnings": warnings,
    }


def write_bundle(cfg, pk: str, bundle: Dict[str, Any]) -> Path:
    d = cfg.path_for("data")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"report_{pk}.json"
    p.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("报告包已写入 %s（%d 条日记录）", p, len(bundle.get("session_dates") or []))
    return p


# ===========================================================================
# 日记录组装
# ===========================================================================
def _condense_macro(macro: Dict[str, Any]) -> Dict[str, Any]:
    if not macro:
        return {}
    fw = macro.get("fed_watch")
    dp = macro.get("dot_plot")
    return {
        "_fixture": bool(macro.get("_fixture")),
        "ok": macro.get("ok"),
        "asof": macro.get("asof"),
        "fed_watch": ({"target_rate": fw.get("target_rate"), "next_meeting": fw.get("next_meeting"),
                       "probabilities": fw.get("probabilities")} if fw else None),
        "dot_plot": ({"points": dp.get("points")} if dp else None),
        "indicators": macro.get("indicators") or {},
    }


def build_day_record(cfg, sess: date, packet: Dict[str, Any], macro: Dict[str, Any]) -> Dict[str, Any]:
    mkt = packet or {}
    closes: Dict[str, Any] = {}
    changes: Dict[str, Any] = {}
    highs: Dict[str, Any] = {}
    lows: Dict[str, Any] = {}
    for row in list(mkt.get("positions") or []) + list(mkt.get("watchlist") or []):
        code = str(row.get("code") or "")
        if not code:
            continue
        closes[code] = row.get("last")
        changes[code] = row.get("change_pct")
        highs[code] = row.get("high")
        lows[code] = row.get("low")

    weights = {str(p.get("code")): p.get("weight") for p in (mkt.get("positions") or [])}
    indicators: Dict[str, Any] = {}
    for row in list(mkt.get("positions") or []) + list(mkt.get("watchlist") or []):
        code = str(row.get("code") or "")
        if code:
            indicators[code] = row.get("indicators")

    events = mkt.get("calendar") or []
    macro_actuals = [{"name": e.get("name"), "at": e.get("at"), "actual": e.get("actual"),
                      "forecast": e.get("forecast"), "prior": e.get("prior")}
                     for e in events if (e.get("minutes_until") or 0) <= 0]
    events_ahead = [e for e in events if (e.get("minutes_until") or 0) > 0]
    acct = mkt.get("account") or {}
    status = mkt.get("source_status") or {}

    return {
        "session_date": sess.isoformat(),
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "closes": closes,
        "changes_pct": changes,
        "highs": highs,
        "lows": lows,
        "account": {k: acct.get(k) for k in (
            "equity", "currency", "cash", "market_value_by_currency", "pnl_by_currency",
            "mixed_currency", "totals_complete", "position_count", "unquoted_position_count")},
        "weights": weights,
        "indicators": indicators,
        "macro_actuals": macro_actuals,
        "macro_snapshot": _condense_macro(macro),
        "events_ahead": events_ahead[:20],
        "alerts": mkt.get("alerts") or [],
        "news_top": (mkt.get("news") or [])[:8],
        "calls": [],
        "call_stats": {"present": False, "parsed": 0, "rejected": [], "parse_error": None},
        "scores": [],
        "provenance": {
            "source": status.get("source"),
            "backfilled": False,
            "degraded_fields": [],
            "macro_fixture": bool((macro or {}).get("_fixture")),
        },
    }


# ===========================================================================
# fixtures 播种 / 找报告文件
# ===========================================================================
def seed_from_fixtures(cfg) -> int:
    """把 `fixtures/rollup/*` 复制进 `rollup/` —— 不用等一周就能跑周报。"""
    src = cfg.path_for("fixtures") / "rollup"
    dst = cfg.path_for("rollup", create=True)
    if not src.is_dir():
        log.warning("没有 fixtures/rollup 目录，跳过播种：%s", src)
        return 0
    n = 0
    (dst / "daily").mkdir(parents=True, exist_ok=True)
    for f in (src / "daily").glob("*.json"):
        shutil.copyfile(f, dst / "daily" / f.name)
        n += 1
    for extra in ("due.json", "ledger.json"):
        s = src / extra
        if s.is_file():
            shutil.copyfile(s, dst / extra)
            log.info("播种 %s → %s", extra, dst / extra)
    log.info("已播种 %d 条 fixture 日记录 → %s", n, dst / "daily")
    return n


def find_vault_report(cfg, period: str, pk: str) -> Optional[Path]:
    base = cfg.vault_path_for("vault_invest") / PERIOD_DIR.get(period, period)
    if not base.is_dir():
        return None
    cands = [f for f in base.glob(f"{pk}*.md")]
    if not cands:
        return None
    return max(cands, key=lambda f: f.stat().st_mtime)


# ===========================================================================
# 缺口回填（M2）—— 只有「日 K 收盘」能回填；当日 packet 无法回填
# ===========================================================================
def _load_daily_klines(cfg, code: str) -> List[Dict[str, Any]]:
    """取某标的的日 K。fixture → 本地文件；futu → K_DAY（失败返回空）。"""
    if cfg.source == "fixture":
        p = cfg.path_for("fixtures") / "klines_daily" / f"{code}.json"
        if not p.is_file():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    try:
        from futu import AuType, KLType, OpenQuoteContext
    except ImportError:
        return []
    from .market import opend_alive
    if not opend_alive(cfg)["alive"]:
        return []
    ctx = None
    try:
        ctx = OpenQuoteContext(host=cfg.get("moomoo.host", "127.0.0.1"),
                               port=int(cfg.get("moomoo.port", 11111)))
        end = datetime.now(CST).strftime("%Y-%m-%d")
        start = (datetime.now(CST) - timedelta(days=400)).strftime("%Y-%m-%d")
        all_bars: List[Dict[str, Any]] = []
        page = None
        for _ in range(5):
            ret, kl, page = ctx.request_history_kline(          # type: ignore[attr-defined]
                code, start=start, end=end, ktype=KLType.K_DAY,
                max_count=1000, autype=AuType.QFQ, page_req_key=page)
            if ret != 0:
                return []
            all_bars.extend({"time": str(r["time_key"]), "close": float(r["close"])}
                            for _, r in kl.iterrows())
            if not page:
                break
        return all_bars
    except Exception as exc:                                    # noqa: BLE001
        log.warning("日 K 回填取数失败 %s：%s", code, exc)
        return []
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:                                   # noqa: BLE001
                pass


def _close_by_date(bars: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for b in bars:
        d = str(b.get("time") or "")[:10]
        c = b.get("close")
        if d and c is not None:
            try:
                out[d] = float(c)
            except (TypeError, ValueError):
                continue
    return out


def backfill(cfg, sess: date) -> int:
    """把缺失的交易日补成「**只有收盘**」的日记录。

    🚩 回填的日记录一律带 `provenance.backfilled=true` + `degraded_fields`
    （当日 packet 里的新闻 / 日历 / 告警**无法回填**）→ 周报必须据此把这些字段标「无」，
    **不许让回填的一周看起来像完整观测的一周**。返回补写条数。
    """
    max_days = int(cfg.get("rollup.max_backfill_days", 10))
    if max_days <= 0:
        return 0
    existing = {str(r.get("session_date")) for r in list_day_records(cfg)}
    codes = list(cfg.get("rollup.call_instruments") or [])
    for r in list_day_records(cfg):
        for c in (r.get("closes") or {}):
            if c not in codes:
                codes.append(c)
    if not codes:
        return 0

    # 只补「最新一段**连续**缺口」——遇到已有记录就停。
    # 🚩 不这样做的话，每次运行都会继续往更早处多补一段（10 天变 20 天变 30 天…），
    #    既不幂等，也会把早已无意义的陈年交易日灌进来。
    wanted: List[date] = []
    d = sess - timedelta(days=1)
    while len(wanted) < max_days and (sess - d).days < 60:
        if d.isoformat() in existing:
            break
        if _is_session_day(cfg, d):
            wanted.append(d)
        d -= timedelta(days=1)
    if not wanted:
        return 0

    series = {c: _close_by_date(_load_daily_klines(cfg, c)) for c in codes}
    n = 0
    for day in sorted(wanted):
        ds = day.isoformat()
        closes = {c: s[ds] for c, s in series.items() if s.get(ds) is not None}
        if not closes:
            continue
        rec = {
            "session_date": ds,
            "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
            "closes": closes, "changes_pct": {}, "highs": {}, "lows": {},
            "account": {}, "weights": {}, "indicators": {},
            "macro_actuals": [], "macro_snapshot": {}, "events_ahead": [],
            "alerts": [], "news_top": [], "calls": [],
            "call_stats": {"present": False, "parsed": 0, "rejected": [], "parse_error": None},
            "scores": [],
            "provenance": {"source": cfg.source, "backfilled": True,
                           "degraded_fields": ["news", "calendar", "alerts", "macro_actuals",
                                               "account", "indicators"],
                           "macro_fixture": False},
        }
        write_day_record(cfg, rec)
        n += 1
    if n:
        log.warning("回填了 %d 个交易日（仅收盘，缺新闻/日历/告警）—— 报告须标注「回填」", n)
    return n


def _read_latest_packet(cfg) -> Dict[str, Any]:
    p = cfg.path_for("data") / "latest.json"
    if not p.is_file():
        log.warning("没有 data/latest.json —— rollup 拿不到数据包（先跑取数）")
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("latest.json 读取失败：%s", exc)
        return {}


# ===========================================================================
# 编排
# ===========================================================================
def run(cfg, *, packet: Optional[Dict[str, Any]] = None,
        macro: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None,
        seed_fixtures: bool = False) -> Dict[str, Any]:
    """一次 rollup：日记录 → 打分 → 到期周期出报告 → 台账/命中率/水位。

    返回 `{drafts, periods, day_record, scorecard, errors}`；**任何失败都不抛**——
    但失败时水位绝不推进（否则该期永久丢失且毫无征兆）。
    """
    from . import summarize

    result: Dict[str, Any] = {"drafts": [], "periods": [], "day_record": None,
                              "scorecard": None, "errors": []}
    if not cfg.get("rollup.enabled", True):
        log.info("rollup.enabled = false，跳过")
        return result

    now = _as_cst(now)
    if seed_fixtures:
        seed_from_fixtures(cfg)

    sess = session_date(cfg, now)
    log.info("rollup：session_date = %s（now=%s）", sess, now.isoformat(timespec="seconds"))

    if packet is None:
        packet = _read_latest_packet(cfg)
    if macro is None:
        macro = {}

    # 0.5) 缺口回填（只有日 K 收盘能回填；标 backfilled，见 backfill()）
    try:
        backfill(cfg, sess)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("回填失败（跳过，不影响主流程）：%s", exc)

    # 1) 打分（用已存在的旧记录；今天的记录在最后统一写）
    score_pending(cfg, now)

    # 2) 到期周期（含首跑保护）
    wm = watermark_map(load_watermark(cfg))
    due = _resolve_periods(cfg, sess, wm)
    log.info("rollup：到期周期 = %s", [f"{p}:{k}" for p, k in due])

    rec = build_day_record(cfg, sess, packet, macro)
    drafts: List[Dict[str, Any]] = []
    call_stats = {"present": False, "parsed": 0, "rejected": [], "parse_error": None}
    new_calls: List[Dict[str, Any]] = []
    entries = ledger_load(cfg)

    try:
        for period, pk in due:
            days = _days_in_period(cfg, period, pk)
            if period == "daily":
                days = [rec]                       # 今日记录尚未落盘，直接用内存版
            bundle = build_report_bundle(cfg, period, pk, days, macro, now=now)
            bundle_path = write_bundle(cfg, pk, bundle)

            agent_on = bool(cfg.get("agent.enabled", True))
            if agent_on:
                res = summarize.run(cfg, trigger="report", period=period,
                                    period_key=pk, report_bundle=bundle_path, now=now)
                if not res.get("ok"):
                    result["errors"].append(f"{pk}: {res.get('error')}")
                    log.error("周期 %s 报告失败：%s —— 水位**不推进**，下次重试", pk, res.get("error"))
                    continue
                draft = dict(res["draft"])
                draft["period"] = period
                draft["period_key"] = pk
                drafts.append(draft)
                result["periods"].append({"period": period, "period_key": pk,
                                          "push": bool((cfg.get(f"rollup.periods.{period}") or {}).get("push", True))})
            else:
                log.info("agent 未启用 → 只出报告包，不出报告全文（%s）", pk)

            # 只从「日评」里取预测块（CALL 只出现在 daily）。
            # 🚩 故意**放在 agent 判断之外**：只要该日评文件在，就解析 —— 不依赖「本次 agent 是否刚跑过」。
            if period == "daily":
                vf = find_vault_report(cfg, period, pk)
                if vf is None:
                    if agent_on:
                        # agent 自称成功却没写全文 → 是真错误（不许静默）
                        log.warning("找不到日评全文文件（CALL 块无法解析）：%s", pk)
                        call_stats["parse_error"] = "日评文件缺失"
                    # 否则（agent 关）：无报告 → 落进 call_missing，不算错
                else:
                    parsed = parse_call_block(vf.read_text(encoding="utf-8"))
                    call_stats["present"] = parsed["present"]
                    call_stats["parse_error"] = parsed["error"]
                    if parsed["error"]:
                        log.error("🚩 CALL 块损坏（记 call_parse_error，**不计入 miss**）：%s", parsed["error"])
                    call_stats["parsed"] = len(parsed["calls"])      # 块里解析出的条数（≠ 采纳数）
                    added, rejected = append_calls(
                        cfg, entries,
                        date.fromisoformat(pk), parsed["calls"], rec.get("closes") or {})
                    call_stats["added"] = added
                    call_stats["rejected"] = rejected
                    new_calls = [e for e in entries if e.get("session_date") == pk]
    finally:
        # 3) 一律写日记录（带 calls / stats / scores）——即使某周期报告失败
        daily_due = any(p == "daily" for p, _ in due)
        existing = load_day_record(cfg, sess)
        if existing is not None and not daily_due:
            # 本次没重出日评（daily 未到期）→ 保留既有 call 记录，别把已观测的历史清空
            rec["calls"] = existing.get("calls") or rec["calls"]
            rec["call_stats"] = existing.get("call_stats") or rec["call_stats"]
        else:
            rec["calls"] = new_calls
            rec["call_stats"] = call_stats
        rec["scores"] = [
            {"instrument": e["instrument"], "session_date": e["session_date"],
             "outcome": (e.get("score") or {}).get("outcome"),
             "move_pct": (e.get("score") or {}).get("move_pct")}
            for e in entries
            if (e.get("score") or {}).get("scored_by_session") == pk_daily(due, sess)
        ]
        result["day_record"] = str(write_day_record(cfg, rec))
        ledger_save(cfg, entries)

        # 4) 命中率（由台账派生）+ 水位（**成功后**才推进 —— 失败的周期不进水位）
        sc = write_scorecard(cfg)
        result["scorecard"] = sc
        wm_map = watermark_map(load_watermark(cfg))
        for period, pk in due:
            if any(p["period_key"] == pk and p["period"] == period for p in result["periods"]):
                wm_map[period] = pk
            elif period == "daily" and not cfg.get("agent.enabled", True):
                wm_map[period] = pk          # no-agent 也推进 daily（只为验证链路）
        save_watermark(cfg, wm_map, sess)

    log.info("rollup 完成：周期 %s，草稿 %d 条", result["periods"], len(drafts))
    return result


def pk_daily(due: List[Tuple[str, str]], sess: date) -> str:
    for period, pk in due:
        if period == "daily":
            return pk
    return period_key("daily", sess)
