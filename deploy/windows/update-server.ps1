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
$new = $null
# The page shows these steps as a progress bar (GET /licence/update/progress reads this file, also after the restart).
function Step($percent, $text, $state = "installing", $message = $null) {
  try {
    @{ state = $state; percent = $percent; step = $text; version = $new; previous = $old; message = $message;
       at = (Get-Date -Format s) } | ConvertTo-Json -Compress |
      Set-Content -Path (Join-Path $InstallDir "updates\progress.json") -Encoding UTF8
  } catch { }
}
try {
  Step 10 "Unpacking the new version"
  Write-Host "$(Get-Date -Format s)  update from $old using $Package"
  if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
  Expand-Archive -Path $Package -DestinationPath $staging
  $new = (Get-Content (Join-Path $staging "VERSION") | Select-Object -First 1).Trim()
  if (-not (Test-Path (Join-Path $staging "app\backend\app\main.py"))) { throw "This is not a Tally Cloud package" }

  Step 25 "Stopping Tally Cloud"
  Stop-App
  Step 35 "Replacing the application files"
  if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }
  Move-Item $app $previous
  Move-Item (Join-Path $staging "app") $app
  Step 50 "Installing Python packages and updating the database"
  Prepare
  Set-Content (Join-Path $InstallDir "VERSION") $new -Encoding ASCII
  Step 85 "Starting Tally Cloud $new"
  Start-ScheduledTask -TaskName $task
  if (-not (Healthy)) { throw "Version $new did not start" }
  Write-Host "Updated to $new."
  Step 100 "Updated to $new" "done"
  # start.bat runs this copy: keep it the new version's
  $runPs = Join-Path $app "deploy\windows\run.ps1"
  if (Test-Path $runPs) { Copy-Item $runPs (Join-Path $InstallDir "run.ps1") -Force }
} catch {
  $why = $_.Exception.Message
  Write-Host "!! $why. Going back to $old."
  Step 90 "Going back to version $old" "rolling_back" $why
  Stop-App
  if (Test-Path $previous) {
    if (Test-Path $app) { Remove-Item $app -Recurse -Force }
    Move-Item $previous $app
    try { Prepare } catch { Write-Host "!! $($_.Exception.Message)" }
    Set-Content (Join-Path $InstallDir "VERSION") $old -Encoding ASCII
  }
  Start-ScheduledTask -TaskName $task
  Step 100 "Not updated: version $old is running again" "failed" $why
} finally {
  if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
  Stop-Transcript | Out-Null
}
