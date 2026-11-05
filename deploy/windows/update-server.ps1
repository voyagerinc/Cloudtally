<#
  Installs a downloaded Tally Cloud update on a customer's Windows server. Started by the app
  (Licence tab -> Install update) after it checked the package's SHA-256 and Voyager's signature.

  Keeps the settings and data, swaps the app folder, installs Python packages, updates the database
  and restarts. If the new version does not start, it puts the previous version back.
#>
param(
  [Parameter(Mandatory)] [string]$Package,
  [Parameter(Mandatory)] [string]$InstallDir
)
$ErrorActionPreference = "Stop"
Start-Transcript -Path (Join-Path $InstallDir "logs\update.log") -Append | Out-Null
$task = "Tally Cloud"
$port = (Get-Content (Join-Path $InstallDir "port.txt") -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $port) { $port = 8080 }
$py = Join-Path $InstallDir "venv\Scripts\python.exe"
$app = Join-Path $InstallDir "app"
$previous = Join-Path $InstallDir "app.previous"
$staging = Join-Path $InstallDir "updates\staging"

function Stop-App {
  Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like "$InstallDir\venv\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
}
function Healthy {
  for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    try { Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 3 | Out-Null; return $true } catch { }
  }
  return $false
}
function Prepare {   # packages and schema for the code now in $app
  Copy-Item (Join-Path $InstallDir "secrets\tallycloud.env") (Join-Path $app "backend\.env") -Force
  & $py -m pip install -q -r (Join-Path $app "backend\requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "Installing Python packages failed" }
  Push-Location (Join-Path $app "backend")
  try { & $py -m app.cli init-db; if ($LASTEXITCODE -ne 0) { throw "Updating the database schema failed" } }
  finally { Pop-Location }
}

$old = (Get-Content (Join-Path $InstallDir "VERSION") -ErrorAction SilentlyContinue | Select-Object -First 1)
try {
  Write-Host "$(Get-Date -Format s)  update from $old using $Package"
  if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
  Expand-Archive -Path $Package -DestinationPath $staging
  $new = (Get-Content (Join-Path $staging "VERSION") | Select-Object -First 1).Trim()
  if (-not (Test-Path (Join-Path $staging "app\backend\app\main.py"))) { throw "This is not a Tally Cloud package" }

  Stop-App
  if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }
  Move-Item $app $previous
  Move-Item (Join-Path $staging "app") $app
  Prepare
  Set-Content (Join-Path $InstallDir "VERSION") $new -Encoding ASCII
  Start-ScheduledTask -TaskName $task
  if (-not (Healthy)) { throw "Version $new did not start" }
  Write-Host "Updated to $new."
} catch {
  Write-Host "!! $($_.Exception.Message). Going back to $old."
  Stop-App
  if (Test-Path $previous) {
    if (Test-Path $app) { Remove-Item $app -Recurse -Force }
    Move-Item $previous $app
    try { Prepare } catch { Write-Host "!! $($_.Exception.Message)" }
    Set-Content (Join-Path $InstallDir "VERSION") $old -Encoding ASCII
  }
  Start-ScheduledTask -TaskName $task
} finally {
  if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
  Stop-Transcript | Out-Null
}
