# TradingAgents + Indian market data (Kite / Investing.com / Yahoo) - one-shot Windows installer
# Usage (PowerShell, no download needed):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ujjwal83star/TradingAgents/india-data/install_windows.ps1)))
# Keys can be passed as -AnthropicKey / -KiteApiKey / -KiteApiSecret when run as a file,
# or filled into .env afterwards (opened in Notepad automatically if missing).
# Re-running is safe: it pulls the latest india-data branch from ujjwal83star/TradingAgents.
param(
    [string]$Dir = "$env:USERPROFILE\TradingAgents",
    [string]$AnthropicKey = $env:ANTHROPIC_API_KEY,
    [string]$KiteApiKey = $env:KITE_API_KEY,
    [string]$KiteApiSecret = $env:KITE_API_SECRET,
    [string]$KiteLoginTime = "08:45",          # weekday browser sign-in reminder (local time)
    [switch]$NoKiteTask
)
$ErrorActionPreference = "Stop"
$Repo = "https://github.com/ujjwal83star/TradingAgents.git"
$Branch = "india-data"

# 1. git + uv
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
    $env:Path += ";C:\Program Files\Git\cmd"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 2. clone / update the fork's india-data branch
if (-not (Test-Path "$Dir\.git")) {
    git clone -b $Branch $Repo $Dir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
    Set-Location $Dir
} else {
    Set-Location $Dir
    if (-not (git remote | Select-String -Quiet "^fork$")) { git remote add fork $Repo }
    git fetch -q fork $Branch
    git checkout -q -B $Branch "fork/$Branch"
    if ($LASTEXITCODE -ne 0) { throw "could not check out $Branch (local changes?)" }
}

# 3. venv + install with Kite extra
if (-not (Test-Path .venv\Scripts\python.exe)) { uv venv -q --python 3.13 .venv }
uv pip install -q --python .venv\Scripts\python.exe -e ".[india]"

# 4. .env (existing values kept; given keys written in)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
function Set-EnvLine($name, $value) {
    if (-not $value) { return }
    $lines = Get-Content .env
    $hit = $false
    $lines = $lines | ForEach-Object {
        if ($_ -match "^#?\s*$name=") { $hit = $true; "$name=$value" } else { $_ }
    }
    if (-not $hit) { $lines += "$name=$value" }
    Set-Content .env $lines
}
foreach ($k in "ANTHROPIC_API_KEY","KITE_API_KEY","KITE_API_SECRET") {
    if (-not (Select-String -Path .env -Pattern "^$k=" -Quiet)) { Set-EnvLine $k " " ; (Get-Content .env) -replace "^$k= $", "$k=" | Set-Content .env }
}
Set-EnvLine "ANTHROPIC_API_KEY" $AnthropicKey
Set-EnvLine "KITE_API_KEY" $KiteApiKey
Set-EnvLine "KITE_API_SECRET" $KiteApiSecret

# 5. launchers
"@echo off`r`ncd /d `"$Dir`"`r`ncall .venv\Scripts\activate.bat`r`ntradingagents %*" |
    Set-Content -Encoding ASCII "$Dir\ta.bat"
@"
@echo off
cd /d "$Dir"
schtasks /Query /TN "TradingAgents Kite Login" >nul 2>&1 || schtasks /Create /F /TN "TradingAgents Kite Login" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST $KiteLoginTime /TR "\"$Dir\kite-login.bat\"" >nul
call .venv\Scripts\activate.bat
tradingagents kite-login
if errorlevel 1 pause
"@ | Set-Content -Encoding ASCII "$Dir\kite-login.bat"

# 6. weekday Kite sign-in task (opens the Kite login page; token is captured automatically)
if ($KiteApiKey -and -not $NoKiteTask) {
    $action  = New-ScheduledTaskAction -Execute "$Dir\kite-login.bat"
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $KiteLoginTime
    Register-ScheduledTask -TaskName "TradingAgents Kite Login" -Action $action -Trigger $trigger `
        -Description "Daily Kite Connect sign-in for TradingAgents" -Force | Out-Null
}

# 7. smoke test (offline: resolver + vendor wiring)
& .venv\Scripts\python.exe -c @"
from tradingagents.dataflows.vendors.india.instruments import resolve
from tradingagents.dataflows.router import VENDOR_METHODS
assert 'india' in VENDOR_METHODS['get_stock_data']
for s in ('^NSEI','BANKNIFTY','GOLD.MCX','RELIANCE.NS','BANKNIFTY.NFO'):
    print(f'{s:15} -> {resolve(s)}')
print('TradingAgents + india vendor OK')
"@

Write-Host ""
Write-Host "Installed to $Dir" -ForegroundColor Green
Write-Host " 1. Kite app (developers.kite.trade) redirect URL: http://127.0.0.1:5000/"
Write-Host " 2. Sign in for today:  $Dir\kite-login.bat"
Write-Host " 3. Run:                $Dir\ta.bat   (tickers: NIFTY, BANKNIFTY, GOLD.MCX, RELIANCE.NS ...)"

# 8. open .env if keys are still missing
$envText = Get-Content .env -Raw
if ($envText -notmatch "(?m)^(ANTHROPIC|OPENAI|GOOGLE)_API_KEY=\S" -or $envText -notmatch "(?m)^KITE_API_KEY=\S") {
    Write-Host " Fill in ANTHROPIC_API_KEY, KITE_API_KEY, KITE_API_SECRET in .env (opening it now)" -ForegroundColor Yellow
    Start-Process notepad.exe "$Dir\.env"
}
