<#
    run_brief.ps1 —— 计划任务调这个，不直接调 python。

    为什么需要这层包装：
      1. Windows 控制台默认 cp1252，中文日志会 UnicodeEncodeError 崩掉 → 强制 UTF-8
      2. 计划任务的 cwd 不一定是项目目录 → 显式 Set-Location
      3. 把退出码如实透出去，便于在「任务计划程序」里看历史

    用法：
      run_brief.ps1                 # 按 config.yaml 跑（默认 brief）
      run_brief.ps1 -Trigger alert  # 高危预警模式
      run_brief.ps1 -Ask "GLD 怎么样"
      run_brief.ps1 -Rollup         # ⑥:30 那次：简报 + 多周期回滚（日评/周/月/季/年）
      run_brief.ps1 -Rollup -SeedFixtures   # 离线验收（播种 fixture 日记录，不接 OpenD）
#>
param(
    [ValidateSet('brief', 'alert', 'ask')]
    [string]$Trigger = 'brief',
    [string]$Ask = '',
    [switch]$NoPush,
    [switch]$Rollup,
    [switch]$SeedFixtures
)

$ErrorActionPreference = 'Stop'

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'          # Python 3.7+ 的全局 UTF-8 模式
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $proj

# --- Python 解释器定位 -------------------------------------------------------
# 🚩🚩 这里有个**静默失败陷阱**（2026-09-30 实踩，代价：退出码 0 但一行输出都没有）：
#   Windows 自带的 `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` 是**应用商店存根** ——
#   它运行时不报错、不输出、**退出码 0**。`Get-Command python` 会优先命中它，
#   于是计划任务每次都「成功」地什么都没干，而且从不报错。
#   所以：**必须显式排除 WindowsApps，并真的问一句 --version 确认它是真 Python。**

function Test-RealPython([string]$exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe)) { return $false }
    if ($exe -match '\\WindowsApps\\') { return $false }   # 商店存根，直接毙掉
    try {
        $v = & $exe --version 2>&1
        return ("$v" -match '^Python 3')
    } catch { return $false }
}

$py = $env:MOOMOO_WATCH_PYTHON
if (-not (Test-RealPython $py)) {
    $py = $null
    $candidates = @()
    foreach ($cand in @('python', 'python3', 'py')) {
        $c = Get-Command $cand -ErrorAction SilentlyContinue
        if ($c) { $candidates += $c.Source }
    }
    # 再兜几个常见安装位置（用户级 winget 装法就是第一个）
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        'C:\Python312\python.exe',
        'C:\Python311\python.exe'
    )
    foreach ($cand in $candidates) {
        if (Test-RealPython $cand) { $py = $cand; break }
    }
}
if (-not $py) {
    Write-Error "找不到可用的 Python 3.10+。（注意：WindowsApps 里那个是商店存根，不是真 Python。）请安装 Python，或把解释器绝对路径写进环境变量 MOOMOO_WATCH_PYTHON。"
    exit 1
}
Write-Host "使用 Python: $py"

# ⚠️ 不能叫 $args —— 那是 PowerShell 自动变量，赋值会直接报错
$pyArgs = @('main.py', '--trigger', $Trigger)
if ($Ask)          { $pyArgs += @('--ask', $Ask) }
if ($NoPush)       { $pyArgs += '--no-push' }
if ($Rollup)       { $pyArgs += '--rollup' }
if ($SeedFixtures) { $pyArgs += '--seed-fixtures' }

& $py @pyArgs
exit $LASTEXITCODE
