<#
    run_research.ps1 —— 「市场观察」计划任务调这个，不直接调 python。
    克隆自 run_brief.ps1 的三件防坑事：UTF-8 强制 / cwd 显式 / 退出码透传
    （含 WindowsApps python.exe 商店存根陷阱的排除，详见 run_brief.ps1 注释）。

    用法：
      run_research.ps1                  # 计划任务模式：自动判断 daily/weekly/event
      run_research.ps1 -DryRun          # 只打印将做什么
      run_research.ps1 -Force -Modes daily,weekly   # 手动指定
#>
param(
    [switch]$DryRun,
    [switch]$Force,
    [string]$Modes = '',
    [string]$Date = ''
)

$ErrorActionPreference = 'Stop'

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $proj

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
    Write-Error "找不到可用的 Python 3.10+。（WindowsApps 里那个是商店存根。）设 MOOMOO_WATCH_PYTHON 指向真解释器。"
    exit 1
}
Write-Host "使用 Python: $py"

$pyArgs = @('research/run_research.py')
if ($DryRun)  { $pyArgs += '--dry-run' }
if ($Force)   { $pyArgs += '--force' }
if ($Modes)   { $pyArgs += @('--modes', $Modes) }
if ($Date)    { $pyArgs += @('--date', $Date) }

& $py @pyArgs
exit $LASTEXITCODE
