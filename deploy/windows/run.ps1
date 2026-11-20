<#
  One script behind run.bat (in the downloaded package) and start.bat (in the installation folder).

  From the unpacked package (run.bat): the first time it installs Tally Cloud (install-server.ps1),
  and a package newer than the installation is installed over it (data and settings are kept).
  Otherwise, and always from start.bat: it starts Tally Cloud if it is not running and opens the browser.
  It asks Windows for administrator rights by itself only when it has to install or start the service.
#>
param(
  [string]$InstallDir = "C:\TallyCloud",
  [switch]$Elevated,
  [ValidateSet("", "install", "start")][string]$Action = ""
)
$ErrorActionPreference = "Stop"

function Is-Admin {
  ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Port {
  $f = Join-Path $InstallDir "port.txt"
  if (Test-Path $f) { return [int]((Get-Content $f | Select-Object -First 1).Trim()) }
  return 8080
}
function Is-Up {
  try { Invoke-RestMethod "http://127.0.0.1:$(Port)/health" -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}
function Version-Of($file) {
  if (-not (Test-Path $file)) { return $null }
  try { return [version]((Get-Content $file | Select-Object -First 1).Trim()) } catch { return $null }
}
function Run-Elevated($what) {
  # A second window, as administrator; Windows asks the user to allow it.
  $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated -Action $what -InstallDir `"$InstallDir`""
  try { Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait }
  catch { throw "Tally Cloud needs administrator rights to $what. Run it again and choose 'Yes' when Windows asks." }
}
function Start-TallyCloud {
  if (-not (Is-Up)) {
    Write-Host "Starting Tally Cloud ..." -ForegroundColor Cyan
    if (Is-Admin) { Start-ScheduledTask -TaskName "Tally Cloud" } else { Run-Elevated "start" }
    $up = $false
    for ($i = 0; $i -lt 60 -and -not ($up = Is-Up); $i++) { Start-Sleep -Seconds 2 }
    if (-not $up) { throw "Tally Cloud did not start. See $InstallDir\logs\tallycloud.log" }
  }
}

# The package this runs from (run.bat), if any: <package>\app\deploy\windows\run.ps1
$package = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..") -ErrorAction SilentlyContinue).Path
$fromPackage = $package -and (Test-Path (Join-Path $package "VERSION")) -and (Test-Path (Join-Path $package "app\backend"))
$installed = (Test-Path "$InstallDir\venv\Scripts\python.exe") -and (Version-Of "$InstallDir\VERSION")
if (-not $Action) {
  $Action = "start"
  if ($fromPackage -and (-not $installed -or (Version-Of "$package\VERSION") -gt (Version-Of "$InstallDir\VERSION"))) {
    $Action = "install"
  }
}

try {
  if ($Action -eq "install") {
    if (-not $fromPackage) { throw "Tally Cloud is not installed yet. Run run.bat from the downloaded package." }
    if (Is-Admin) {
      & (Join-Path $PSScriptRoot "install-server.ps1") -InstallDir $InstallDir   # starts it and opens the browser
    } else {
      Write-Host "Installing Tally Cloud $((Get-Content "$package\VERSION" | Select-Object -First 1).Trim()) - allow it when Windows asks ..." -ForegroundColor Cyan
      Run-Elevated "install"          # the install opens the browser when Tally Cloud is up
    }
    return
  }
  if (-not $installed) { throw "Tally Cloud is not installed on this computer. Run run.bat from the downloaded package." }
  Start-TallyCloud
  if (-not $Elevated) {
    Write-Host "Tally Cloud is running: http://localhost:$(Port)" -ForegroundColor Green
    Start-Process "http://localhost:$(Port)"
  }
} catch {
  Write-Host ""
  Write-Host $_.Exception.Message -ForegroundColor Red
  Read-Host "Press Enter to close"
  exit 1
}
