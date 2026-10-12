#!/usr/bin/env python3
"""research/fetch_research.py —— investment-research 的取数管道（纯标准库 + PyYAML）。

产出（契约见 .claude/skills/research-data-sources/SKILL.md §四，与 agent 锚点一致）：
  data/research/13f_<YYYYQn>.json   SEC 13F 机构持仓包（每家带 filing date）
  data/research/macro.json          FRED 宏观序列包（每条带最后观测日期）
  data/research/.state.json         增量水位（cik → 已取 accession，跳过重复下载）

用法（仓库根目录）：
  python research/fetch_research.py all      # 13F + 宏观
  python research/fetch_research.py 13f
  python research/fetch_research.py macro

密钥：FRED_API_KEY 从环境变量或 %USERPROFILE%\\.moomoo-watch\\.env 读取（绝不进仓库）。
护栏：EDGAR 限频 ~10 req/s（本管道守 ~7.7）；User-Agent 必须带联系方式（research.yaml 可改）。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Windows 控制台 cp1252 打不出中文 → 统一 utf-8（今天实踩两次的坑）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = Path(__file__).resolve().parent / "research.yaml"
DATA_DIR = REPO_ROOT / "data" / "research"
STATE_PATH = DATA_DIR / ".state.json"

UA = ""            # from config
MIN_INTERVAL = 0.13
_last_req = 0.0


# ---------------------------------------------------------------- HTTP ------
def http_get(url: str, accept: str = "application/json") -> bytes:
    """带 UA / 限频 / 重试的 GET。返回原始 bytes（自动解 gzip）。"""
    global _last_req
    for attempt in range(3):
        wait = MIN_INTERVAL - (time.monotonic() - _last_req)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Encoding": "gzip",
        })
        try:
            _last_req = time.monotonic()
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            if attempt == 2:
                raise
            print(f"    retry {attempt + 1}/2 after error: {exc}")
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def http_json(url: str) -> dict:
    return json.loads(http_get(url).decode("utf-8"))


# ---------------------------------------------------------------- utils -----
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(st: dict) -> None:
    write_json_atomic(STATE_PATH, st)


def quarter_of(date_str: str) -> str:
    y, m = int(date_str[:4]), int(date_str[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def load_env_key(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if val:
        return val
    envf = Path(os.environ.get("MOOMOO_WATCH_ENV", Path.home() / ".moomoo-watch" / ".env"))
    if envf.exists():
        for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return ""


# ---------------------------------------------------------------- FRED ------
def _obs_at_least(obs: list, months_back: int | None = None, obs_back: int | None = None):
    """取 months_back 个月前（按日期匹配年月）或 obs_back 条之前的观测。"""
    if not obs:
        return None
    if obs_back is not None:
        i = len(obs) - 1 - obs_back
        return obs[i] if i >= 0 else obs[0]
    if months_back is not None:
        last = obs[-1]
        y, m = int(last["date"][:4]), int(last["date"][5:7])
        target = (y * 12 + m - 1) - months_back
        for o in obs:
            if (int(o["date"][:4]) * 12 + int(o["date"][5:7]) - 1) <= target:
                return o
        return obs[0]
    return None


def fetch_macro(cfg: dict) -> None:
    key = load_env_key("FRED_API_KEY")
    if not key:
        print("[macro] 缺 FRED_API_KEY（环境变量或 ~/.moomoo-watch/.env）——跳过宏观，只跑 13F 可用 all 除外")
        return
    series_cfg = cfg["fred"]["series"]
    limit = int(cfg["fetch"].get("fred_obs_limit", 60))
    out_series, data_asof = {}, ""
    for s in series_cfg:
        sid = s["id"]
        url = ("https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={sid}&api_key={key}&file_type=json"
               f"&sort_order=desc&limit={limit}")
        payload = http_json(url)
        rows = [o for o in payload.get("observations", []) if o.get("value", ".") not in (".", "")]
        rows = [{"date": o["date"], "value": float(o["value"])} for o in rows][::-1]
        if not rows:
            print(f"[macro] {sid}: 无有效观测（序列停更或 id 错）——如实记录缺失")
        out_series[sid] = {
            "label": s["label"], "frequency": s["frequency"],
            "last_observation": rows[-1]["date"] if rows else None,
            "observations": rows,
        }
        if rows and rows[-1]["date"] > data_asof:
            data_asof = rows[-1]["date"]
        print(f"[macro] {sid}: {len(rows)} obs, latest {out_series[sid]['last_observation']}")

    # derived —— 确定性算术归脚本（数字=脚本、判断=LLM）；方向判断留给 agent
    def _obs_months_back(obs, months_back):
        if not obs:
            return None

        def ym(o):
            return int(o["date"][:4]) * 12 + int(o["date"][5:7]) - 1

        target = ym(obs[-1]) - months_back
        res = None
        for o in obs:            # 取「≤ 目标年月」的**最后**一条（首踩版本取了第一条——5 年前的点）
            if ym(o) <= target:
                res = o
        return res or obs[0]

    def val(sid, months_back=None, obs_back=None):
        obs = out_series[sid]["observations"]
        if not obs:
            return None
        if obs_back is not None:
            i = max(len(obs) - 1 - obs_back, 0)
            return obs[i]["value"]
        if months_back is not None:
            return _obs_months_back(obs, months_back)["value"]
        return obs[-1]["value"]

    def yoy(sid):
        obs = out_series[sid]["observations"]
        if len(obs) < 2:
            return None
        prev = _obs_months_back(obs, 12)
        if not prev["value"]:
            return None
        return round((obs[-1]["value"] / prev["value"] - 1) * 100, 2)

    derived = {
        "cpi_yoy_pct":            yoy("CPIAUCSL"),
        "core_cpi_yoy_pct":       yoy("CPILFESL"),
        "unrate_latest_pct":      val("UNRATE"),
        "unrate_6m_ago_pct":      val("UNRATE", months_back=6),
        "payems_latest_k":        val("PAYEMS"),
        "payems_6m_change_k":     (lambda a, b: round(a - b, 1) if a is not None and b is not None else None)(
            val("PAYEMS"), val("PAYEMS", months_back=6)),
        "fedfunds_latest_pct":    val("FEDFUNDS"),
        "dtwexbgs_latest":        val("DTWEXBGS"),
        "dtwexbgs_change_30obs":  (lambda a, b: round(a / b - 1, 4) if a and b else None)(
            val("DTWEXBGS"), val("DTWEXBGS", obs_back=30)),
        "dexmaus_latest":         val("DEXMAUS"),
    }

    pkg = {
        "generated_at": now_iso(),
        "data_asof": data_asof,
        "source": "FRED (api.stlouisfed.org), key from env",
        "series": out_series,
        "derived": derived,
        "_schema": {
            "package": "macro —— 美林投资时钟用的宏观数据包",
            "series": "每条: label/frequency/last_observation/observations[{date,value}]（FRED 原始观测，升序）",
            "derived": {
                "cpi_yoy_pct": "CPI 同比 %（管道算，口径=最近值对 12 个月前）",
                "core_cpi_yoy_pct": "核心 CPI 同比 %",
                "unrate_latest_pct / unrate_6m_ago_pct": "失业率最新 / 6 个月前（增长轴方向用）",
                "payems_latest_k / payems_6m_change_k": "非农就业(千人) 最新 / 6 个月净增",
                "fedfunds_latest_pct": "联邦基金有效利率(月)",
                "dtwexbgs_latest / dtwexbgs_change_30obs": "美元广义名义指数 最新 / 相对 30 个观测前(≈6周) 变化",
                "dexmaus_latest": "MYR/USD 汇率",
            },
            "notes": "DTWEXBGS 非 ICE DXY（口径不同）；PMI 无免费序列（SKILL.md §二）——两处都须在报告写明口径",
        },
    }
    write_json_atomic(DATA_DIR / "macro.json", pkg)
    print(f"[macro] 写入 data/research/macro.json（asof {data_asof}）")


# ---------------------------------------------------------------- 13F -------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_infotable(xml_bytes: bytes) -> list[dict]:
    """解析 13F information table（namespace 不敏感，新 schema `infoTable`/`nameOfIssuer`
    与旧式 `entry`/`issuerName` 双兼容）。value 单位 = 千美元。"""
    root = ET.fromstring(xml_bytes)
    entries = []
    for el in root.iter():
        if _local(el.tag) not in ("infoTable", "entry"):
            continue
        row = {}
        for ch in el:
            k = _local(ch.tag)
            if k in ("nameOfIssuer", "issuerName", "cusip", "value", "sshPrnamt",
                     "sshPrnamtType", "putCall", "investmentDiscretion", "shrsOrPrn", "otherManager"):
                row[k] = (ch.text or "").strip()
        row["issuerName"] = row.get("nameOfIssuer") or row.get("issuerName") or ""
        entries.append(row)
    return entries


def fetch_institution(cik: int, name_hint: str, cfg: dict, state: dict) -> tuple[list[dict], str]:
    """返回 (该机构最近 K 个季度的 filing 结果列表, EDGAR 登记名)。"""
    qb = int(cfg["fetch"].get("quarters_back", 2))
    top_n = int(cfg["fetch"].get("top_n", 50))
    sub = http_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    edgar_name = sub.get("name", name_hint)
    rec = sub.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    rows = [
        {
            "form": forms[i], "filing_date": rec["filingDate"][i],
            "report_date": rec["reportDate"][i], "accession": rec["accessionNumber"][i],
            "primary": rec.get("primaryDocument", [""] * len(forms))[i] or "",
        }
        for i in range(len(forms))
        if forms[i] in ("13F-HR", "13F-HR/A")
    ]
    rows.sort(key=lambda r: r["report_date"], reverse=True)
    seen_quarters, picked = set(), []
    for r in rows:
        q = quarter_of(r["report_date"])
        if q in seen_quarters:
            continue
        seen_quarters.add(q)
        picked.append(r)
        if len(picked) >= qb:
            break
    if not picked:
        return [], edgar_name

    results = []
    for r in picked:
        acc_dir = r["accession"].replace("-", "")
        st_key = f"{cik}:{r['report_date']}"
        cached = state.get("13f", {}).get(st_key)
        entries, from_cache = None, False
        if cached and cached.get("accession") == r["accession"] and cached.get("n_entries"):
            entries = cached["entries"]
            from_cache = True
        if entries is None:
            idx = http_json(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_dir}/index.json")
            names = [it["name"] for it in idx.get("directory", {}).get("item", [])]
            # 🚩 infotable 文件名毫无规律（实测有纯数字如 56757.xml）——名字猜不中，
            # 唯一可靠判据 = XML 根元素是 <informationTable>。按「名字含 infotable/13f 优先」
            # 排序后逐个验根，最多试 4 个。
            cands = [n for n in names if n.lower().endswith(".xml")]
            cands.sort(key=lambda n: not any(k in n.lower() for k in ("infotable", "13f")))
            got = False
            for n in cands[:4]:
                xml_b = http_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_dir}/{n}")
                try:
                    root_tag = _local(ET.fromstring(xml_b).tag)
                except ET.ParseError:
                    continue
                if root_tag == "informationTable":
                    entries = parse_infotable(xml_b)
                    got = True
                    break
            if not got:
                print(f"    [13F] {edgar_name} {r['report_date']}: 目录里找不到 infotable ——跳过该季")
                continue
            state.setdefault("13f", {})[st_key] = {
                "accession": r["accession"], "n_entries": len(entries), "entries": entries,
            }
        # 🚩 单位实测校准（2026-10-12，对 Berkshire 全组合核对）：现代 13F XML 的
        # <value> 是**美元**（不是旧纸质规范的千美元）——按千美元读会放大 1000 倍。
        total_usd = sum(float(e.get("value") or 0) for e in entries)
        top = sorted(entries, key=lambda e: float(e.get("value") or 0), reverse=True)[:top_n]
        holdings = [
            {
                "issuer": e.get("issuerName", ""),
                "cusip": e.get("cusip", ""),
                "value_usd": int(float(e.get("value") or 0)),
                "shares": int(float(e.get("sshPrnamt") or 0)) if e.get("sshPrnamt") else None,
                "pct_of_portfolio": round(float(e.get("value") or 0) / total_usd * 100, 3) if total_usd else None,
                **({"put_call": e["putCall"]} if e.get("putCall") else {}),
            }
            for e in top
        ]
        results.append({
            "name": edgar_name, "cik": cik, "form": r["form"], "amended": r["form"] == "13F-HR/A",
            "filing_date": r["filing_date"], "report_date": r["report_date"],
            "quarter": quarter_of(r["report_date"]), "accession": r["accession"],
            "n_entries": len(entries), "total_value_usd": int(total_usd),
            "top_holdings": holdings,
        })
        tag = "cache" if from_cache else "fetch"
        print(f"    [13F] {edgar_name} {r['report_date']} ({tag}): {len(entries)} 持仓, 总值 {total_usd/1e9:.1f} B$")
    return results, edgar_name


def fetch_13f(cfg: dict) -> None:
    state = load_state()
    all_results, missing = [], []
    for inst in cfg["institutions"]:
        cik, hint = int(inst["cik"]), inst["name"]
        try:
            results, edgar_name = fetch_institution(cik, hint, cfg, state)
        except Exception as exc:
            missing.append({"name": hint, "cik": cik, "reason": f"fetch error: {exc}"})
            print(f"    [13F] {hint}: 取数失败 {exc}")
            continue
        if not results:
            missing.append({"name": hint, "cik": cik, "reason": "submissions 近期无 13F-HR"})
            continue
        if edgar_name != hint:
            print(f"    [13F] 名单名 '{hint}' vs EDGAR 登记名 '{edgar_name}'（以 EDGAR 名写进包）")
        all_results.extend(results)
    save_state(state)

    by_quarter: dict[str, dict] = {}
    for res in all_results:
        by_quarter.setdefault(res["quarter"], []).append(res)
    for q, insts in sorted(by_quarter.items(), reverse=True):
        path = DATA_DIR / f"13f_{q}.json"
        existing = {}
        if path.exists():
            try:
                existing = {i["cik"]: i for i in json.loads(path.read_text(encoding="utf-8")).get("institutions", [])}
            except Exception:
                existing = {}
        for i in insts:
            existing[i["cik"]] = i
        final_insts = sorted(existing.values(), key=lambda i: i["total_value_usd"], reverse=True)
        pkg = {
            "generated_at": now_iso(),
            "data_asof": max(i["filing_date"] for i in final_insts),
            "quarter": q,
            "config": {
                "institutions_expected": len(cfg["institutions"]),
                "top_n_per_institution": int(cfg["fetch"].get("top_n", 50)),
                "coverage_note": "13F 只覆盖 13(f) 证券多头（不含债券/现金/空头）；45 天窗口内陆续披露，未交≠清仓",
            },
            "filed_count": len({i["cik"] for i in final_insts}),
            "institutions": final_insts,
            "missing": [m for m in missing],
            "_schema": {
                "package": f"13f_{q} —— SEC 13F 机构持仓包",
                "institutions[]": "name/cik/filing_date/report_date/quarter/accession/n_entries/"
                                  "total_value_usd(美元)/top_holdings[]（按市值前 N，含 pct_of_portfolio）",
                "top_holdings[]": "issuer/cusip/value_usd(美元)/shares/pct_of_portfolio/put_call?"
                                  "；同一 issuer 可能多行（不同 CUSIP/类别/期权），解读侧自行归并",
                "value 单位": "美元（现代 13F XML 口径，2026-10-12 对 Berkshire 实测校准）",
                "filed_count vs config.institutions_expected": "供「已交 N / 名单 M 家」标注",
                "missing[]": "名单内未取到的机构与原因（不许当清仓解读）",
            },
        }
        write_json_atomic(path, pkg)
        print(f"[13F] 写入 data/research/13f_{q}.json（已交 {pkg['filed_count']}/{len(cfg['institutions'])} 家, asof {pkg['data_asof']}）")


# ---------------------------------------------------------------- main ------
def main() -> int:
    global UA, MIN_INTERVAL
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    # SEC Fair Access：UA 必须是「名字 邮箱@域名」——实踩结论（2026-10-12）：
    #   ✅ "Sample Company admin@example.com" → 200
    #   ❌ 仓库 URL 式 UA → 403；❌ *@users.noreply.github.com → 403（该模式被 SEC 拉黑）
    # 联系邮箱走 .env（EDGAR_CONTACT_EMAIL），不进公开仓库。
    ua_name = cfg["edgar"].get("user_agent_name", "moomoo-watch research pipeline")
    email = load_env_key("EDGAR_CONTACT_EMAIL") or cfg["edgar"].get("contact_email", "")
    if not email:
        email = "admin@example.com"
        print("[edgar] ⚠️ 未配置联系方式（.env 里加 EDGAR_CONTACT_EMAIL=你的邮箱），"
              "暂用占位符 admin@example.com —— SEC 政策要求真实联系方式")
    UA = f"{ua_name} {email}"
    MIN_INTERVAL = float(cfg["fetch"].get("min_interval_sec", 0.13))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("all", "macro"):
        fetch_macro(cfg)
    if cmd in ("all", "13f"):
        fetch_13f(cfg)
    if cmd not in ("all", "macro", "13f"):
        print(f"未知子命令: {cmd}（可用: all / 13f / macro）")
        return 2
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
