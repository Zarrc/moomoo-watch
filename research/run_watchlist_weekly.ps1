<#
    run_watchlist_weekly.ps1 -- weekly (Monday 09:30) 13F hot-watchlist refresh.

    Steps:
      1) deterministic: python research/fetch_research.py 13f   (incremental, EDGAR)
      2) judgment:      headless claude (main agent, VAULT ROOT cwd) recomputes
                        institutional consensus and MAY update the hot block in
                        config.yaml. Prompt: research/weekly_watchlist_prompt.md

    Design notes (v4, lessons from the first scheduled run 2026-10-13):
      - DO NOT wrap claude in Start-Job: jobs do NOT inherit the current
        location, so claude would run with cwd = user home and every
        vault-relative path in the prompt would miss. Call it directly.
      - Everything claude prints is appended to research/watchlist_weekly.log,
        otherwise scheduled stdout vanishes and failures look like success.
      - claude's exit code is propagated (the v1 bug: unconditional exit 0).
      - Timeout: rely on the task's ExecutionTimeLimit (1h) instead of an
        in-script guard.
    ASCII-only on purpose (PowerShell 5.1 reads no-BOM UTF-8 as ANSI).
#>
$ErrorActionPreference = 'Stop'

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$proj  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)  # repo root
# vault root = nearest ancestor of the repo containing CLAUDE.md
# (2026-10-13 bug: a fixed Split-Path count landed one level short in project\,
#  so headless claude ran with the wrong cwd and could not touch vault paths)
$vault = $null
$dir = $proj
while ($dir -and -not (Test-Path (Join-Path $dir 'CLAUDE.md'))) { $dir = Split-Path -Parent $dir }
if ($dir) { $vault = $dir } else { Write-Error 'vault root not found (no CLAUDE.md in ancestors of repo)'; exit 1 }
$log   = Join-Path $proj 'research\watchlist_weekly.log'
Set-Location $proj

function Log([string]$line) {
    $stamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Add-Content -Path $log -Value "[$stamp] $line" -Encoding UTF8
    Write-Host $line
}

# --- locate a REAL python (WindowsApps stub trap, see run_brief.ps1) ----------
function Test-RealPython([string]$exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe)) { return $false }
    if ($exe -match '\\WindowsApps\\') { return $false }
    try {
        $v = & $exe --version 2>&1
        if ("$v" -notmatch '^Python 3') { Log ('Test-RealPython: ' + $exe + ' version output = [' + $v + ']') }
        return ("$v" -match '^Python 3')
    } catch {
        Log ('Test-RealPython: ' + $exe + ' THREW: ' + $_.Exception.Message)
        return $false
    }
}
$py = $env:MOOMOO_WATCH_PYTHON
if (-not (Test-RealPython $py)) {
    $py = $null
    $candidates = @()
    # known-good absolute path FIRST (diagnosed 2026-10-13: real python lives here;
    # relying on Get-Command ordering was flaky in the scheduled context)
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )
    foreach ($cand in @('python', 'python3', 'py')) {
        $c = Get-Command $cand -ErrorAction SilentlyContinue
        if ($c) { $candidates += $c.Source }
    }
    foreach ($cand in $candidates) { if (Test-RealPython $cand) { $py = $cand; break } }
}
if (-not $py) {
    Log 'FATAL no real Python found -- diagnostics follow'
    Log ('LOCALAPPDATA=' + $env:LOCALAPPDATA)
    foreach ($cand in @('python', 'python3', 'py')) {
        $c = Get-Command $cand -ErrorAction SilentlyContinue
        Log ('Get-Command ' + $cand + ' -> ' + $(if ($c) { $c.Source } else { '<none>' }))
    }
    foreach ($p2 in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        'C:\Python312\python.exe', 'C:\Python311\python.exe')) {
        Log ('candidate ' + $p2 + ' exists=' + (Test-Path $p2))
    }
    exit 1
}

Log ('--- 1/2 incremental 13F fetch (python: ' + $py + ') ---')
& $py 'research/fetch_research.py' '13f' 2>&1 | ForEach-Object { Log ("fetch| " + $_) }
if ($LASTEXITCODE -ne 0) { Log ('FATAL fetch exit ' + $LASTEXITCODE); exit 1 }

Log '--- 2/2 headless claude: recompute consensus, maybe update watchlist ---'
$claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe'   # keep in sync with config.yaml agent.claude_exe
if (-not (Test-Path $claude)) { Log ('FATAL claude.exe missing: ' + $claude); exit 1 }

Set-Location $vault    # claude MUST run at vault root (CLAUDE.md + relative paths)
Log ('claude invoking with cwd = ' + (Get-Location).Path)
# bare `claude -p` gets NO file tools in this environment (diagnosed 2026-10-13:
# only ToolSearch + deferred extras; --allowedTools does NOT resurrect them).
# The ONLY working pattern is --agent, where the agent md's `tools:` line grants
# them (same mechanism summarize.py uses daily). --permission-mode is MANDATORY
# with --agent (frontmatter permissionMode is silently ignored on this path).
& $claude --agent watchlist-weekly --permission-mode acceptEdits -p 'execute weekly watchlist refresh' 2>&1 | ForEach-Object { Log ("claude| " + $_) }
$code = $LASTEXITCODE
Log ('claude exit code: ' + $code)
if ($code -ne 0) { Log 'FATAL claude step failed'; exit $code }
Log 'weekly watchlist refresh done.'
exit 0
