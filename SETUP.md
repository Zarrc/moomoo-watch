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

用 `run_brief.ps1` 包装（已处理编码与路径问题）：

```powershell
schtasks /Create /TN "moomoo-watch 简报" /SC DAILY /ST 20:30 ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<本仓库的绝对路径>\run_brief.ps1\"" ^
  /F
```

> 把 `<本仓库的绝对路径>` 换成实际路径（在仓库目录里跑 `pwd` 或 `cd` 后看地址栏）。
> 计划任务**只接受绝对路径**，这是 Windows 的限制，绕不开。

> 🚩 **关键约束：任务必须设为「只在用户登录时运行」。**
> 因为 OpenD 需要你交互式登录，且登录态会掉。选「不管用户是否登录都要运行」反而会失败。
> 每次提醒自己：**开机后确认 OpenD 在跑**。
>
> 建议再加一个高频任务（如每 30 分钟）专跑 `--trigger alert`，用于高危事件窗口；
> 日限额与冷却会兜住重复推送。

## 7. 验证清单

- [ ] `telnet 127.0.0.1 11111` 能连
- [ ] `python main.py --source fixture --no-agent` 全绿
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
