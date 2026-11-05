<#
  Installs Tally Cloud on a customer's Windows server or PC (next to Tally).

  Run from an unpacked release package: right-click install.cmd -> Run as administrator.
  Optional: -InstallDir C:\TallyCloud -Port 8080 -LicenceServer https://tallycloud.voyager.co.in

  It installs Python 3.12 and PostgreSQL 16 with winget when they are missing, puts the app in
  <InstallDir>\app, makes its own database, and starts Tally Cloud with Windows (scheduled task
  "Tally Cloud"). Then the browser opens the first-run page: licence key and the manager's user ID.
  Running it again repairs an installation and keeps its data and settings.
#>
param(
  [string]$InstallDir = "C:\TallyCloud",
  [int]$Port = 8080,
  [string]$LicenceServer = "https://tallycloud.voyager.co.in"
)
$ErrorActionPreference = "Stop"

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this as administrator: right-click install.cmd and choose 'Run as administrator'."
}
$Package = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path   # <package>\app\deploy\windows -> <package>
$Version = (Get-Content (Join-Path $Package "VERSION") -ErrorAction Stop | Select-Object -First 1).Trim()
Write-Host "Installing Tally Cloud $Version into $InstallDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force $InstallDir, "$InstallDir\logs", "$InstallDir\updates" | Out-Null

function Random-Secret { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ }) }
function Only-Admins($path) {   # the folder holds the database password: SYSTEM and administrators only
  icacls $path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
}
function Winget-Install($id, $extra) {
  if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw "winget is missing. Install 'App Installer' from the Microsoft Store, or install $id by hand, then run this again."
  }
  $wargs = @("install", "-e", "--id", $id, "--silent", "--accept-package-agreements", "--accept-source-agreements") + $extra
  & winget.exe @wargs
  if ($LASTEXITCODE -ne 0) { throw "Installing $id failed (winget exit code $LASTEXITCODE)." }
}

# ------------------------------------------------------------------ Python 3.11+
function Find-Python {
  foreach ($c in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe")) {
    if (Test-Path $c) { return $c }
  }
  return $null
}
$python = Find-Python
if (-not $python) {
  Write-Host "==> Installing Python 3.12"
  Winget-Install "Python.Python.3.12" @("--scope", "machine")
  $python = Find-Python
  if (-not $python) { throw "Python 3.12 was installed but not found in C:\Program Files\Python312." }
}
Write-Host "    Python: $python"

# ------------------------------------------------------------------ PostgreSQL
$secrets = Join-Path $InstallDir "secrets"
New-Item -ItemType Directory -Force $secrets | Out-Null
Only-Admins $secrets
function Find-Psql { Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1 }
$psql = Find-Psql
$superFile = Join-Path $secrets "postgres-superuser.txt"
if (-not $psql) {
  Write-Host "==> Installing PostgreSQL 16"
  $super = Random-Secret
  Set-Content -Path $superFile -Value $super -Encoding ASCII
  Winget-Install "PostgreSQL.PostgreSQL.16" @("--override", "--mode unattended --unattendedmodeui none --superpassword $super --serverport 5432")
  $psql = Find-Psql
  if (-not $psql) { throw "PostgreSQL was installed but psql.exe was not found." }
} elseif (Test-Path $superFile) {
  $super = (Get-Content $superFile -Raw).Trim()
} else {
  $sec = Read-Host -AsSecureString "PostgreSQL is already installed. Password of its 'postgres' user"
  $super = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
$env:PGPASSWORD = $super
function Sql($q) {
  $out = & $psql.FullName -h 127.0.0.1 -U postgres -d postgres -tAc $q
  if ($LASTEXITCODE -ne 0) { throw "PostgreSQL refused: $q" }
  return $out
}

$envFile = Join-Path $secrets "tallycloud.env"
if (Test-Path $envFile) {
  Write-Host "    keeping the existing settings and database ($envFile)"
} else {
  Write-Host "==> Database"
  $dbPass = Random-Secret
  if (Sql "select 1 from pg_roles where rolname='tallycloud'") { Sql "alter role tallycloud with login password '$dbPass'" | Out-Null }
  else { Sql "create role tallycloud with login password '$dbPass'" | Out-Null }
  if (-not (Sql "select 1 from pg_database where datname='tallycloud'")) { Sql "create database tallycloud owner tallycloud" | Out-Null }

  $keyFile = Join-Path $Package "license_public_key.txt"
  if (Test-Path $keyFile) { $publicKey = (Get-Content $keyFile -Raw).Trim() }
  else { $publicKey = (Invoke-RestMethod "$LicenceServer/api/v1/licensing/public-key").public_key }
  @(
    "# Made by install-server.ps1. Kept across updates.",
    "ERP_DATABASE_URL=postgresql+psycopg://tallycloud:$dbPass@127.0.0.1:5432/tallycloud",
    "ERP_LICENSE_MODE=client",
    "ERP_LICENSE_PUBLIC_KEY=$publicKey",
    "ERP_LICENSE_SERVER_URL=$LicenceServer",
    "ERP_INSTALL_DIR=$InstallDir",
    "ERP_AUTO_SYNC=true",
    "ERP_TALLY_TIMEOUT=90"
  ) | Set-Content -Path $envFile -Encoding ASCII
}
Remove-Item Env:\PGPASSWORD

# ------------------------------------------------------------------ the app
Write-Host "==> Application files"
$task = "Tally Cloud"
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $task }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like "$InstallDir\venv\*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
robocopy (Join-Path $Package "app") (Join-Path $InstallDir "app") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying the application failed (robocopy $LASTEXITCODE)." }
Copy-Item $envFile (Join-Path $InstallDir "app\backend\.env") -Force
Set-Content (Join-Path $InstallDir "VERSION") $Version -Encoding ASCII
Set-Content (Join-Path $InstallDir "port.txt") $Port -Encoding ASCII

Write-Host "==> Python packages"
if (-not (Test-Path "$InstallDir\venv\Scripts\python.exe")) { & $python -m venv "$InstallDir\venv" }
& "$InstallDir\venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$InstallDir\venv\Scripts\python.exe" -m pip install -q -r "$InstallDir\app\backend\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "Installing Python packages failed." }

Write-Host "==> Database schema"
Push-Location "$InstallDir\app\backend"
& "$InstallDir\venv\Scripts\python.exe" -m app.cli init-db
$ok = $LASTEXITCODE -eq 0
Pop-Location
if (-not $ok) { throw "Updating the database schema failed." }

# ------------------------------------------------------------------ start with Windows
Write-Host "==> Starting with Windows (task '$task')"
$runner = Join-Path $InstallDir "runner.ps1"
@"
`$ErrorActionPreference = 'Continue'
while (`$true) {
  Set-Location '$InstallDir\app\backend'
  & '$InstallDir\venv\Scripts\python.exe' -m uvicorn app.main:app --host 0.0.0.0 --port $Port --workers 1 *>> '$InstallDir\logs\tallycloud.log'
  Start-Sleep -Seconds 5
}
"@ | Set-Content -Path $runner -Encoding UTF8
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $task

# Other PCs in the office open http://<this computer>:<port>; nothing is opened to the internet.
if (-not (Get-NetFirewallRule -DisplayName "Tally Cloud" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Tally Cloud" -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Domain,Private -Action Allow | Out-Null
}

Write-Host "==> Waiting for Tally Cloud to start"
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
  Start-Sleep -Seconds 2
  try { Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3 | Out-Null; $up = $true } catch { }
}
if (-not $up) { throw "Tally Cloud did not start. See $InstallDir\logs\tallycloud.log" }
Write-Host ""
Write-Host "Tally Cloud $Version is running." -ForegroundColor Green
Write-Host "  This computer:      http://localhost:$Port"
Write-Host "  Other office PCs:   http://$($env:COMPUTERNAME):$Port"
Write-Host "The first time, enter your licence key and create the manager's user ID and password."
Start-Process "http://localhost:$Port"
