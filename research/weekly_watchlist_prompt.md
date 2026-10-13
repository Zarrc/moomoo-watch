# 任务：周更 13F 热门自选（每周一由计划任务 headless 调起）

你是主代理，**无人值守**执行。只做本文件写明的事，做完一句话收尾。

## 步骤

1. 读 `Self/01 To-do's/project/Agents/moomoo-watch/data/research/` 下的 `13f_*.json`
   （每包顶层有 `quarter` / `filed_count` / `data_asof`；取数脚本刚跑过，盘上即最新）。
2. 判断有没有**比 `config.yaml` 热门股段注释里登记的季度更新的、且 `filed_count ≥ 1` 的季度包**。
   - 没有（新季度 0 家已交 = 窗口开着但没人交）→ **不改自选**，只写日志一条「无新申报」，结束。
   - 有 → 走第 3 步。
3. 重算共识（只在已落盘的包上算，**不联网取数**）：
   - 每家机构取「市值变动前 10 + 新进」作为增持信号；注意三个打折项：市值口径含价格效应、
     top-50 切片（退出≠清仓）、期权行混入（GS/Citadel/JPM 尤甚）；
   - 跨机构交集：被 **≥3 家**同向增持/新进的美国上市个股入候选；
   - 排除：当前持仓（O / NVDA / JPM / AVGO / MY.5296 / MY.1155）与已在自选的标的；
   - issuer 名 → 行情代码映射**保守**：只加确定在美交所交易的普通股（如 ALPHABET→US.GOOGL），
     ETF/期权/ADR 不确定的一律不加。宁缺毋滥。
4. 改 `Self/01 To-do's/project/Agents/moomoo-watch/config.yaml` 的 `moomoo.watchlist`：
   - **永不动前三只**（US.GLD / US.SPY / US.AAPL）；
   - 只改「热门股」注释段及以下（保持 3–5 只），每只一行注释：依据（哪家机构、什么方向、哪个季度）；
   - 段首注释更新日期与数据季度。
5. 在 `Self/投资/研究/ibanker/自选周更日志.md` **顶部追加**（新的在上）：
   日期、各季度包 filed 情况、改了什么 / 为什么没改。文件不存在就建。

## 硬护栏

- 只允许写两个文件：上述 `config.yaml` 与日志。其他任何文件不动，不取数、不推送、不 commit。
- 数据包里没有的标的绝不加；判定不确定时选择「不加」。
- 改完 config.yaml 后用 `python -c "import yaml;yaml.safe_load(open(...))"` 口径自查语法（或等效检查）。
