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

### ⚠️ 「大脑」那一层需要你自己准备

本仓库**只含「身体」**。程序通过 `claude --agent <名字>` 调起一个子代理，
**那个子代理的定义文件不在本仓库里** —— 你需要自备，或者把它关掉。

- 自备：写一个 `.claude/agents/<名字>.md`，约定见下方「数据包契约」
- 关掉：`config.yaml` 里设 `agent.enabled: false` —— 但那样流水线会停在数据包，
  **不会产生推送**（推送内容由子代理写）

调起命令里 **`--permission-mode acceptEdits` 不能省**：子代理文件里的
`permissionMode` 字段在 `--agent` 这条路径下**静默失效**，省了就写不出文件
（文件照样加载、exit 0，只是什么都没产出）。

## 快速开始

```bash
# 1) 装依赖（Python 3.10+）
python -m pip install -r requirements.txt

# 2) 复制配置
cp config.example.yaml config.yaml

# 3) 跑一次离线自检 —— 不连 OpenD、不需要密钥，应该全绿
python main.py --source fixture --no-agent

# 4) 密钥（放仓库外，别提交）
#    %USERPROFILE%\.moomoo-watch\.env  ← 详见 SETUP.md

# 5) 连真 OpenD
python main.py --source futu --no-agent    # 先只出数据包，核对数字
python main.py --source futu --mode live   # 真发
```

## 常用命令

| 命令 | 作用 |
|------|------|
| `python main.py` | 按 `config.yaml` 跑一次 |
| `python main.py --source fixture` | 用离线样例跑（不连 OpenD） |
| `python main.py --mode live` | 真发推送（默认 `simulate` 只写日志） |
| `python main.py --no-agent` | 只出数据包，不调子代理 |
| `python main.py --no-push` | 跑到出稿为止，不推送 |
| `python main.py --ask "GLD 今天怎么样"` | 交互问答 |
| `python main.py --trigger alert` | 强制走高危预警模式 |

退出码：`0` = 正常（**含「没重要事不发」**）；`1` = 硬失败。

## 目录

```
├── main.py              # 入口：编排整条流水线
├── config.example.yaml  # 配置模板（复制成 config.yaml）
├── requirements.txt
├── run_brief.ps1        # Windows 计划任务包装（处理编码与路径）
├── core/
│   ├── config.py        # 配置 + 密钥注入（密钥从仓库外读）
│   ├── market.py        # 行情/持仓/账户（fixture 与 futu 两个源）
│   ├── indicators.py    # MA / ATR / RSI / 量比 —— 纯 Python，不引入 pandas
│   ├── calendar.py      # 宏观日历（翻页取全，含未来事件）
│   ├── news.py          # moomoo 资讯 HTTP（无需 key）
│   ├── packet.py        # 组装数据包 —— 程序与子代理的唯一接口
│   ├── state.py         # 去重 / 冷却 / 分渠道日限额
│   ├── notifier.py      # Telegram / Server酱 / pushplus（非阻塞、失败降级、脱敏）
│   └── summarize.py     # 调起子代理
├── fixtures/            # 离线样例（由 make_fixtures.py 生成）
├── data/                # ← 程序写，子代理读   ⚠️gitignore
├── outbox/              # ← 子代理写，程序读   ⚠️gitignore
└── logs/                # 按天滚动，留 30 天   ⚠️gitignore
```

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

程序与子代理之间**只通过 `data/latest.json` 交互**。字段：

| 字段 | 内容 |
|------|------|
| `trigger` | `brief` / `alert` / `ask` |
| `generated_at` · `data_asof` · `data_age_minutes` | 生成时间 / 数据时间 / 数据年龄 |
| `account` | 净值、现金、**分币种**市值与盈亏、集中度、`totals_complete` |
| `positions` | 代码 · 方向 · 数量 · 成本 · 现价 · 浮盈亏 · 权重 · `price_source` · `realtime_quote` |
| `watchlist` | 现价 · 涨跌幅 · 量比 |
| `calendar` | 名称 · 时间 · `minutes_until` · `high_risk` |
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

## 部署

见 [`SETUP.md`](SETUP.md)。
