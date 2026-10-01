# moomoo-watch

基于**本机 moomoo / 富途 OpenD 网关**的持仓与市场风险守望。

一条流水线：取行情/持仓/账户 → 算技术指标 → 抓宏观日历与新闻 → 组装数据包 →
交给一个 LLM 子代理写成**手机可读的简报** → 按优先级推送到你手机。

只出**分析**，**不下单**。所有交易由你自己操作。

---

## 架构：身体 + 大脑

| 层 | 干什么 | 谁来做 |
|----|--------|--------|
| **身体** | 取数 · 算指标 · 判日历 · 去重 · 冷却 · 推送 | 本仓库的 Python 程序，由计划任务定时跑 |
| **大脑** | 选什么值得说 · 怎么解读 · 怎么措辞 | 一个 Claude Code 子代理（headless 调起） |

```
计划任务 ──▶ main.py ──▶ data/latest.json ──▶ claude --agent <名字>
                                                     │
                                                     ▼
                        outbox/*.md ◀── 推送稿（send / priority / title）
                              │
                              ▼
                   去重 / 冷却 / 分渠道限额 ──▶ Telegram / Server酱 ──▶ 手机
```

### 🧠 「大脑」那一层：带一份参考实现

程序通过 `claude --agent <名字>` 调起一个子代理来完成「怎么写、写什么」这部分。

**本仓库带一份参考版**：[`agents/portfolio-watch.md`](agents/portfolio-watch.md)
—— 那是可直接用（也可直接改）的子代理定义，含数据包契约、四种模式
（`brief` / `alert` / `ask` / `report`）、候选筛选规则与硬性护栏。**把它当模板读，别当黑箱用。**

- **装它**：复制到你的 Claude Code 项目（一般是 vault 根，即含 `.claude/` 的那层）的
  `.claude/agents/portfolio-watch.md`；名字要与 `config.yaml` 的 `agent.agent_name` 一致。
  详见 [`SETUP.md`](SETUP.md) 第 5 步。
- **不用它**：`config.yaml` 里设 `agent.enabled: false` —— 但那样流水线会停在数据包，
  **不会产生推送**（推送内容由子代理写）。

> ⚠️ 参考版里凡是 🔧 标了的地方（报告区路径、`claude` 可执行文件位置等）都要按你的实际情况改。

调起命令里 **`--permission-mode acceptEdits` 不能省**：子代理文件里的
`permissionMode` 字段在 `--agent` 这条路径下**静默失效**，省了就写不出文件
（文件照样加载、exit 0，只是什么都没产出）。

## 快速开始

```bash
# 1) 装依赖（Python 3.10+）
python -m pip install -r requirements.txt

# 2) 复制配置
cp config.example.yaml config.yaml

# 3) 装上「大脑」：把参考子代理复制进你的 Claude Code 项目
#    <你的 vault 根>/.claude/agents/portfolio-watch.md ← from agents/portfolio-watch.md

# 4) 跑一次离线自检 —— 不连 OpenD、不需要密钥，应该全绿
python main.py --source fixture --no-agent

# 5) 密钥（放仓库外，别提交）
#    %USERPROFILE%\.moomoo-watch\.env  ← 详见 SETUP.md

# 6) 连真 OpenD
python main.py --source futu --no-agent    # 先只出数据包，核对数字
python main.py --source futu --mode live   # 真发
```

## 常用命令

| 命令 | 作用 |
|------|------|
| `python main.py` | 按 `config.yaml` 跑一次 |
| `python main.py --source fixture` | 用离线样例跑（不连 OpenD） |
| `python main.py --mode live` | 真发推送（默认 `simulate` 只写日志） |
| `python main.py --no-agent` | 只出数据包/报告包，不调子代理 |
| `python main.py --no-push` | 跑到出稿为止，不推送 |
| `python main.py --ask "GLD 今天怎么样"` | 交互问答 |
| `python main.py --trigger alert` | 强制走高危预警模式 |
| `python main.py --rollup` | **多周期回滚**：日记录 + 预测评分 + 到期周期报告（并入常规简报） |
| `python main.py --rebuild-scorecard` | 只从台账重算命中率（审计 / 幂等验证） |
| `python main.py --rollup --seed-fixtures` | 离线验收：播种 fixture 日记录（不用等一周就能跑周报） |
| `python -m unittest discover -s tests` | 跑单测（stdlib，无新依赖） |

退出码：`0` = 正常（**含「没重要事不发」**）；`1` = 硬失败。

## 目录

```
├── main.py              # 入口：编排整条流水线
├── config.example.yaml  # 配置模板（复制成 config.yaml）
├── requirements.txt
├── run_brief.ps1        # Windows 计划任务包装（处理编码与路径）
├── core/
│   ├── config.py        # 配置 + 密钥注入（密钥从仓库外读）+ vault 根解析
│   ├── market.py        # 行情/持仓/账户（fixture 与 futu 两个源）
│   ├── indicators.py    # MA / ATR / RSI / 量比 —— 纯 Python，不引入 pandas
│   ├── calendar.py      # 宏观日历（翻页取全，含 future 事件 + 已公布实际值）
│   ├── macro.py         # 宏观快照：美联储利率预期（主刻度）+ 指标历史
│   ├── news.py          # moomoo 资讯 HTTP（无需 key；fixture 源读本地）
│   ├── packet.py        # 组装数据包 —— 程序与子代理的唯一接口
│   ├── rollup.py        # 多周期回滚：日记录 / 预测台账 / 打分 / 命中率 / 水位
│   ├── state.py         # 去重 / 冷却 / 分渠道日限额
│   ├── notifier.py      # Telegram / Server酱 / pushplus（非阻塞、失败降级、脱敏）
│   └── summarize.py     # 调起子代理（brief / alert / ask / report）
├── agents/              # 🧠 「大脑」：子代理定义（参考版，复制进你的 .claude/agents/）
│   └── portfolio-watch.md
├── docs/                # 分析框架等参考文档
├── tests/               # 单测（stdlib unittest）
├── fixtures/            # 离线样例（make_fixtures.py / make_rollup_fixtures.py 生成）
│   ├── klines/          #  60M K 线（指标用）
│   └── klines_daily/    #  日 K（回填用，~300 根）
├── data/                # ← 程序写，子代理读：数据包 + 报告包  ⚠️gitignore
├── outbox/              # ← 子代理写，程序读：手机推送稿       ⚠️gitignore
├── rollup/              # ← 程序写：日记录 / 台账 / 命中率 / 水位 ⚠️gitignore
└── logs/                # 按天滚动，留 30 天   ⚠️gitignore
```

> 📄 **报告全文不在本仓库** —— 落在 vault 的 `Self/投资/{日评,周报,月报,季报,年报}/`（人读，可被 Obsidian 索引）。
> 本仓库只保留**机器记录**（`rollup/`）与**手机短摘要**（`outbox/`）。

## 推送：按优先级分流

不同渠道特性差很多，所以**按优先级走不同渠道**（`config.yaml` 的 `push.routing`）：

| 优先级 | 默认走 | 理由 |
|--------|--------|------|
| `high`（高危预警） | Telegram | 免费无上限、正文完整 |
| `normal`（常规简报） | Server酱 → 微信 | 每天仅 5 条，省着用 |

- **限额按渠道各算** —— 全局限额会让 Server酱 的 5 条/天卡死 Telegram
- **失败降级**：路由渠道失败时按 `push.channels` 声明顺序自动重试下一个
- **simulate 不计额度** —— 否则联调几次就把日限额烧光
- **错误信息脱敏** —— `requests` 的异常里会带完整 URL（Telegram 的 URL 含 bot token），
  而日志会落盘，所以所有对外文字都过一遍替换

## 数据包契约

程序与子代理之间**只通过 `data/` 下的 JSON 交互**：常规简报读 `data/latest.json`，
周期报告另读 `data/report_<周期键>.json`（报告包）。字段：

| 字段 | 内容 |
|------|------|
| `trigger` | `brief` / `alert` / `ask` |
| `generated_at` · `data_asof` · `data_age_minutes` | 生成时间 / 数据时间 / 数据年龄 |
| `account` | 净值、现金、**分币种**市值与盈亏、集中度、`totals_complete` |
| `positions` | 代码 · 方向 · 数量 · 成本 · 现价 · 浮盈亏 · 权重 · `price_source` · `realtime_quote` |
| `watchlist` | 现价 · 涨跌幅 · 量比 |
| `calendar` | 名称 · 时间 · `minutes_until` · `high_risk` · **`actual` / `forecast` / `prior`**（已公布数据的实际/预期/前值，缺则 `null`） |
| `macro` | **宏观快照**：`fed_watch`（目标利率 + 加息/按兵/降息概率，**主刻度**）· `dot_plot` · `indicators` · `series`。`macro._fixture=true` = 样例数据 |
| `news` | 标题 · 来源 · 时间 · URL |
| `alerts` | 程序已判定的**确定性**告警（阈值触发、数据过期、无行情权限…） |

子代理写回 `outbox/*.md`，带 YAML frontmatter：

```yaml
---
send: true          # false = 不必推送（「今天没什么值得说的」是合格产出）
priority: high      # high 允许突破冷却
title: ≤40 字，必须自包含
---
正文……
```

## 多周期回滚与预测台账

原来每次运行都是**无状态**的：取数 → 写一份推送 → 忘掉。带时间戳的历史包没人回头读，
于是只能回答「此刻怎么样」，回答不了「这一周 / 在这一月往哪走」。

**三层汇总**（青铜 → 白银 → 黄金）：

```
L0  原始层   data/<时间戳>.json              已有：每次跑落一个
                 ↓  确定性脚本压（不是 LLM）
L1  日汇总   rollup/daily/YYYY-MM-DD.json    新建：日记录
                 ↓
L2  周期汇总 报告全文 → Self/投资/{日评,周报,月报,季报,年报}/
             手机短摘要 → outbox/*.md        复用现有推送链路
```

**三条不可动摇的分工**：

1. **数字 = 脚本，判断 = LLM。** LLM 只写 `{方向, 关键位, 失效条件, 置信度}`；
   脚本填参考价、做 **100% 的打分**（`score_call` 是纯函数，见 `tests/test_rollup.py`）。
2. **机器记录在项目内（`rollup/`，gitignore），人读报告在 vault（`Self/投资/`）。**
   —— `rollup/daily/*.json` 含真实收盘与权重，本仓库是**公开**的，绝不能提交。
3. **单一入口自己判断「该出什么」**（`due_periods` + 水位 `rollup/due.json`），不堆计划任务。

### 可证伪预测（为什么预测没被「禁止」）

本仓库立身之本是「只陈述状态、不编数字、不下指令」，预测本是被禁止的模式。**解法**：
预测可以存在，但必须**可证伪、且由脚本自动打分**。

- 每个交易日，子代理在 `Self/投资/日评/YYYY-MM-DD*.md` 里放一段
  `<!-- CALL:BEGIN -->…<!-- CALL:END -->`，内嵌一个 `yaml` 的预测块。
- **次日由脚本**（不是 LLM）取「下一个有该标的收盘的日记录」判对错，写进 `rollup/ledger.json`。
- 累积成 `rollup/scorecard.json` 与 vault 里的 `Self/投资/预测台账/命中率.md`。

**硬约束**（提示词**与**解析器两处都拦）：

| 约束 | 为什么 |
|------|--------|
| 只能点 `rollup.call_instruments` 白名单（默认 `US.SPY` / `US.GLD`） | 多币种账户的「组合方向」无法定义；`MY.*` 拿不到行情无法打分；打分需干净收盘序列 |
| `direction ∈ {up, down, flat}` · `invalidation.type ∈ {close_below, close_above}` · `confidence ∈ [0,1]` | 取值域限定，否则无法机判 |
| 白名单外 / 越界的 call → **脚本丢弃**并记 `call_rejected` | 提示词级约束不够，解析器也要拦 |

**边界情形（都要诚实处理，绝不静默丢弃）**：

- 无下一条记录 → 留在 `pending`，**不编**；超 `max_score_lag_days` → `unscored_stale`
- 未出预测 → `call_missing`；块损坏 → `call_parse_error`（**大声记日志**）
- 以上排除项**计入 `coverage` 但不计入命中率分母**；`hit_rate` 在 `n==0` 时是 `null` 不是 `0`
- **`scorecard` 完全由 `ledger.json` 派生** → `--rebuild-scorecard` 可重建，便于审计

### 调度（单一入口）

- **06:30 一次 `run_brief.ps1 -Rollup`**：常规简报 + 回滚汇总（哪个周期到期由水位判断）。
- 30 分钟的高危预警任务**保持不动**（`-Trigger alert`，**绝不带 `-Rollup`**）。
- 首跑保护：`rollup.bootstrap_mode: seed`（默认）→ 第一次只登记水位、只出 daily，
  否则首跑会同时触发 5 个报告（5×900s + 打爆 Server酱 5 条/天）。

## 设计原则（踩过坑才写下的）

**宁可留空，不要报假数字。** 所有「取不到」的情况都如实标注，绝不拿 0 或估算顶上：

- 取不到现价 → `pnl: null` + 告警说明，**不用 0 当价格**（否则会凭空造出一笔假亏损）
- 账户多币种 → 拒绝给出「总市值」，只给分币种（否则是跨币种相加的假数）
- K 线过期 → `high` 级告警说明「该标的 MA/ATR/RSI 不可信」
- 部分持仓无行情权限 → `totals_complete: false`，明确合计只是部分

**异常一律不崩。** 网络超时、数据缺失、推送失败都只记 WARNING，主循环继续。

**额度是稀缺资源。** 没重要事就不发 —— 这是为了保住真正的高危预警。

## 已知限制

- **马来西亚市场（Bursa）的实时行情接口无权限。** 实测 `get_market_snapshot`
  对 `MY.*` 报 `No permission`。持仓接口自带的 `nominal_price` 可作兜底，
  但口径是延迟/收盘价，程序会给这类价格打 `price_source: position_api` 标记。
- **`security_firm` 因账户地区而异。** 默认留空自动遍历候选并优先实盘账户；
  Moomoo MY 客户实测需 `FUTUMY` 才看得到实盘。
- **交易默认走模拟环境**，切实际盘需二次确认 + 手动输密码（官方限制，无法全自动）。
- **OpenD 必须常驻且已登录。** 登录态掉了 = 整条管道静默失败，程序每次会做端口探活。
- **信用风险：无数据。** 分析框架（见 `docs/分析框架-传导链.md`）的**副刻度 = 信用风险**，
  但**没有任何 API 来源** —— 一律写「无数据」，**绝不用代理指标（信用利差 / CDS / VIX）替代**。
- **宏观接口字段名未在真机核对。** `macro.py` 的 `get_fed_watch_*` / `get_macro_indicator_history`
  取值按「键名模糊匹配、找不到就留 `None`」处理（本机 OpenD 当时未开）→ 首次真机联调需按其实际列名收紧。
- **回填的日记录只有收盘。** 当日 packet 里的新闻 / 日历 / 告警**无法回填** ——
  回填记录带 `provenance.backfilled=true`，报告必须把那些字段标「无」。

## 部署

见 [`SETUP.md`](SETUP.md)。
