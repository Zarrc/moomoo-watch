# SETUP —— 部署前置

按顺序做。**第 1、2 步只能你自己做**（涉及你的账户与密码，程序代替不了）。

---

## 1. 装 FutuOpenD 并登录（必须，且必须常驻）

`C:\Program Files\moomoo` 那个是**看盘客户端**，不是 OpenD 网关 —— 两个东西。

1. 从 <https://openapi.moomoo.com/> 下载 **FutuOpenD**（Windows 版）
2. 启动后**手动登录**你的 moomoo 账户
   - ⚠️ 官方明确不让 AI 代填交易密码，首次登录还需完成问卷评估 + 确认条款
   - 登录后 OpenD 会监听 `127.0.0.1:11111`（以你自己的 `FutuOpenD.xml` 为准）
3. 验证：`telnet 127.0.0.1 11111` 能连上

> 🔒 **不要把 11111 暴露到公网。** 若必须改监听地址：交易 API 必须配 `rsa_private_key`，
> WebSocket 必须配 SSL。
>
> 🚩 **OpenD 掉了 = 整条管道静默失败。** 程序每次运行都会做端口探活（`market.opend_alive`），
> 连不上会明确报错而不是假装成功。

## 2. 拿 Server酱 SendKey

1. 到 <https://sct.ftqq.com/> 用微信扫码登录
2. 复制你的 **SendKey**

**密钥放哪：`%USERPROFILE%\.moomoo-watch\.env`**（vault **外**）

```ini
SERVERCHAN_SENDKEY=你的SendKey
# 若改用 pushplus：PUSHPLUS_TOKEN=xxx
```

> 🚩 **为什么不放 vault 里**：本 vault 是 **OneDrive 同步目录**且**不是 git 仓库** ——
> 放里面等于把密钥同步上云，而且没有 `.gitignore` 能兜底。
>
> 权限提示：`icacls "%USERPROFILE%\.moomoo-watch\.env" /inheritance:r /grant:r "%USERNAME%:R"`

## 3. Python 环境

需要 **Python 3.10+**。

```bash
# 从仓库根目录执行
python -m pip install -r requirements.txt
```

> 🚩 **`pandas` 是 `futu-api` 的硬依赖，必须一起装。** SDK 的返回值就是 DataFrame，
> 代码里到处 `.iterrows()` / `.to_dict("records")`。`requirements.txt` 现在显式列了它
> （2026-10-02 补）—— 早先漏了，干净环境下安装会缺 pandas，`import futu` 直接失败、
> 整条管道静默降级。
>
> Windows 上若 `python` 打不开，用 `winget install Python.Python.3.12`，
> 或把解释器路径写进环境变量 `MOOMOO_WATCH_PYTHON`（`run_brief.ps1` 会优先用它）。

## 4. 改 `config.yaml`

先复制一份示例：

```bash
cp config.example.yaml config.yaml
```

然后至少改这三处：

```yaml
moomoo:
  markets: ["US"]          # 你实际交易的市场
  watchlist:               # 你真正关心的标的
    - "US.SPY"
mode: live                 # simulate → live（改完才真发推送）
```

## 5. 先跑离线自检（**不要跳**）

```bash
python main.py --source fixture --no-agent
```

这一步不碰 OpenD、不碰密钥。应看到：数据包写入成功、告警若干、无异常。
**跑不通就别往下走。**

然后再连真源：

```bash
python main.py --source futu --no-agent     # 先只出数据包，看看持仓/行情对不对
python main.py --source futu --mode simulate # 加上子代理，但仍不真发
python main.py --source futu --mode live     # 真发
```

## 6. 配计划任务

用 `run_brief.ps1` 包装（已处理编码与路径问题）。

**任务只有两个**（这正是「单一入口」的设计：让程序自己判断该出什么，而不是堆任务）：

| 任务 | 频率 | 命令 | 干什么 |
|------|------|------|--------|
| **主任务** | 每天 **06:30** | `run_brief.ps1 -Rollup` | 常规简报 **+ 多周期回滚**（日评 / 周报 / 月报 / 季报 / 年报，由水位自动判定） |
| **高危预警** | 每 30 分钟 | `run_brief.ps1 -Trigger alert` | 只在事件窗口内报（日限额 + 冷却兜底） |

> ⏰ **为什么主任务选 06:30（+08:00）**：此刻**上一美股交易日的收盘已定**（美股 16:00 ET ≈ 次日 04:00–05:00 +08:00），
> 距当晚 21:30 开盘还有约 15h —— 是「数据完整 + 时间充裕」的唯一窗口。
> 原 20:30 的简报任务**并入**这 06:30 这一次运行（`-Rollup` 会先出简报再出报告）。
>
> 🚫 **`-Rollup` 绝不能挂到 30 分钟那条任务上** —— 报告与简报都以「写 outbox → 按 mtime 取最新」的方式交互，
> 高频重跑会引发取错文件 / 重复推送。日记录水位虽会挡住重复出报告，但没必要冒这个险。

```powershell
# 主任务：每天 06:30
schtasks /Create /TN "moomoo-watch 简报+回滚" /SC DAILY /ST 06:30 ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<本仓库的绝对路径>\run_brief.ps1\" -Rollup" ^
  /F

# 高危预警：每 30 分钟（不含 -Rollup）
schtasks /Create /TN "moomoo-watch 预警" /SC MINUTE /MO 30 ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<本仓库的绝对路径>\run_brief.ps1\" -Trigger alert" ^
  /F
```

> 把 `<本仓库的绝对路径>` 换成实际路径（在仓库目录里跑 `pwd` 或 `cd` 后看地址栏）。
> 计划任务**只接受绝对路径**，这是 Windows 的限制，绕不开。

> 🚩 **关键约束：任务必须设为「只在用户登录时运行」。**
> 因为 OpenD 需要你交互式登录，且登录态会掉。选「不管用户是否登录都要运行」反而会失败。
> 每次提醒自己：**开机后确认 OpenD 在跑**。

> 🥇 **首跑保护**：`config.yaml` 的 `rollup.bootstrap_mode: seed`（默认）会让**第一次运行只登记水位、只出 daily**
> —— 否则首跑会同时触发 5 个周期报告（5×900s + 5 条推送，直接打爆 Server酱 5 条/天）。
> **别把它改成其它值**，除非你清楚在做什么。

## 6.5 离线验收：多周期回滚（**不接 OpenD、不接网**）

多周期回滚（`rollup/`）整套都能离线验证 —— fixture 里**伪造了一整周**的日记录，
所以**不用等一周**就能跑周报。

```bash
# 1) 单测（纯逻辑：打分/周期键/解析/命中率派生，stdlib unittest，无新依赖）
python -m unittest discover -s tests -v

# 2) 造一个「一周后」的时钟，播种 fixture，跑一次回滚（每周只出一次报告）
python main.py --source fixture --mode simulate --no-agent --rollup --seed-fixtures ^
  --now "2026-10-03T06:30:00+08:00"
#   → 应写出 data/report_2026-W40.json，并把命中率写进 Self/投资/预测台账/命中率.md
#   （--no-agent 只跑脚本，不出报告全文；去掉它则由子代理写 Self/投资/周报/…）

# 3) 派生一致性：连跑两次 --rebuild-scorecard，输出应完全一致（除时间戳）
python main.py --source fixture --mode simulate --rebuild-scorecard
```

**验收点**：
- `rollup/daily/` 出现日记录；`rollup/ledger.json` 的预测被**自动打分**（hit/miss/invalidated）
- `rollup/scorecard.json` 与 `Self/投资/预测台账/命中率.md` 的数字**手算可复现**
- 同一 `--now` 连跑两次：**不重复出报告、不重复记台账**（水位是第二道防线）
- 全程**没有对 `rollup/` 之外写任何东西**，且 `rollup/` 在 `.gitignore` 里

> ⚠️ `--now` 是**隐藏测试钩子**（`argparse.SUPPRESS`），只为让「星期几 / 周界 / 打分」可复现；
> 日常计划任务**不要带它**。

## 7. 验证清单

- [ ] `telnet 127.0.0.1 11111` 能连
- [ ] `python main.py --source fixture --no-agent` 全绿
- [ ] `python -m unittest discover -s tests` 全绿（27 个用例）
- [ ] `python main.py --source fixture --no-agent --rollup --seed-fixtures --now "2026-10-03T06:30:00+08:00"` 出 `data/report_2026-W40.json`
- [ ] `python main.py --source futu --no-agent` 里 `data/latest.json` 的持仓与你 App 里一致
- [ ] `.env` 里的 SendKey 正确，且 `mode: live` 时手机真收到一条
- [ ] `logs/watch.log` 有记录、`state.json` 在累计推送次数

## 排障

| 症状 | 多半是 |
|------|--------|
| `OpenD 不可用` | OpenD 没启动/没登录 → 重新登录 |
| 子代理 exit 0 但 outbox 没文件 | `--permission-mode acceptEdits` 被漏掉了（见 `core/summarize.py` 顶部注释） |
| 推送全失败 | `.env` 路径/键名不对；或 `mode` 还是 `simulate` |
| 中文全是乱码 | 控制台编码；`run_brief.ps1` 已设 `PYTHONUTF8=1`，手动跑时也加上 |
| 数据包持仓为空 | 行情权限 ≠ App 权限；也可能是 `filter_trdmarket` 与你实际市场不符 |
| 历史 K 线拿不到 | 订阅/历史 K 线额度用尽 —— 查 `query_subscription.py` |
| 周报/月报一直不出 | 看 `rollup/due.json` 的水位 —— 该期已出过就不重出（幂等）；或 `bootstrap_mode: seed` 首跑只出 daily |
| 命中率永远是「无」 | 预测可能都被 `call_rejected`（不在 `rollup.call_instruments` 白名单）或一直 `pending`（次日无该标的收盘） |
| 报告全文没写进 vault | `--no-agent` 只出报告包；要全文需子代理启用（去掉 `--no-agent`）且 `Self/投资/` 可写 |
