# research/ —— investment-research 取数管道（可选模块）

给 Claude Code 的 `investment-research` 子代理（四模式研究沙盘：ibanker / stock / analyst / professional）
供数的 Python「身体」。与核心守望管道（`main.py` / `core/`）**零耦合**，不装任何新依赖
（纯标准库 + PyYAML）。不想用研究功能的，整个目录可以无视。

## 跑（仓库根目录）

```bash
python research/fetch_research.py all     # 13F + 宏观，一次全量
python research/fetch_research.py 13f     # 只刷 SEC 13F 机构持仓
python research/fetch_research.py macro   # 只刷 FRED 宏观序列
```

**产物**（`data/research/`，已被 `.gitignore` 的 `data/` 覆盖，不进仓库）：

| 文件 | 内容 |
|---|---|
| `13f_<YYYYQn>.json` | 各机构该季 13F 持仓（每家带 filing date、市值前 N、占组合 %） |
| `macro.json` | FRED 七条序列 + 派生指标（CPI 同比、失业率方向、非农 6 月净增…） |
| `.state.json` | 增量水位（accession 未变不重复下载 infotable） |

包内均带 `_schema` 自描述字段——消费方（agent）「读不到 schema 就停」依赖它。

## 密钥与联系方式（`%USERPROFILE%\.moomoo-watch\.env`，不进仓库）

```
FRED_API_KEY=...            # fred.stlouisfed.org 免费注册
EDGAR_CONTACT_EMAIL=你@邮箱  # SEC Fair Access 要求 User-Agent 带联系方式
```

不配 EDGAR_CONTACT_EMAIL 会退占位符 `admin@example.com` 并打警告——能跑，但请换成真实邮箱。

## 实测坑（2026-10-12，详录见 `.claude/skills/research-data-sources/SKILL.md`）

1. **UA 格式必须是「名字 邮箱@域名」**：仓库 URL 式 → 403；`*@users.noreply.github.com` 也 403（被 SEC 拉黑）
2. **现代 13F XML 的 `<value>` 单位是美元**（非旧规范的千美元）——按千美元读会放大 1000 倍
3. **infotable 文件名毫无规律**（实测见过纯数字 `56757.xml`）——只能按 XML 根元素 `<informationTable>` 识别
4. **机构 CIK 别凭记忆**：以 `data.sec.gov/submissions/CIK##########.json` 返回的登记名核对（RenTech = 1037389）

## 与 agent 的契约（两处不能漂移）

数据包路径与字段契约 = `.claude/agents/investment-research.md` 的路径锚点 =
`.claude/skills/research-data-sources/SKILL.md` §四。改任何一边，先改这两处。
