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
#>
param(
    [ValidateSet('brief', 'alert', 'ask')]
    [string]$Trigger = 'brief',
    [string]$Ask = '',
    [switch]$NoPush
)

$ErrorActionPreference = 'Stop'

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'          # Python 3.7+ 的全局 UTF-8 模式
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $proj

# Python 解释器：优先用环境变量 MOOMOO_WATCH_PYTHON，否则从 PATH 里找。
# （写死绝对路径的话，换台机器就跑不了 —— 本脚本要能跟着仓库走。）
$py = $env:MOOMOO_WATCH_PYTHON
if (-not $py -or -not (Test-Path $py)) {
    foreach ($cand in @('python', 'python3', 'py')) {
        $c = Get-Command $cand -ErrorAction SilentlyContinue
        if ($c) { $py = $c.Source; break }
    }
}
if (-not $py -or -not (Test-Path $py)) {
    Write-Error "找不到 Python 解释器。请装 Python 3.10+，或设置环境变量 MOOMOO_WATCH_PYTHON。"
    exit 1
}

# ⚠️ 不能叫 $args —— 那是 PowerShell 自动变量，赋值会直接报错
$pyArgs = @('main.py', '--trigger', $Trigger)
if ($Ask)    { $pyArgs += @('--ask', $Ask) }
if ($NoPush) { $pyArgs += '--no-push' }

& $py @pyArgs
exit $LASTEXITCODE
