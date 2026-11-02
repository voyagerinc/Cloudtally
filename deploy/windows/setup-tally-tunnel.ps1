<#
  Connects this Tally PC to Tally Cloud with a reverse SSH tunnel, so the cloud server can reach this
  PC's Tally on its own 127.0.0.1:<RemotePort>. Nothing is opened on this PC or this office's router:
  the PC dials out to the server over SSH (port 22).

  Run once in PowerShell on the PC where TallyPrime runs (no admin rights needed):
      powershell -ExecutionPolicy Bypass -File setup-tally-tunnel.ps1
  Optional: -Server tallycloud.voyager.co.in -RemotePort 9000 -TallyPort 9000

  1st run: creates a key and prints the line to add on the server (deploy/add-tally-pc.sh).
  It then installs a scheduled task that keeps the tunnel up whenever this user is logged on,
  reconnecting by itself after network drops or a server restart.
#>
param(
  [string]$Server = "tallycloud.voyager.co.in",
  [int]$RemotePort = 9000,
  [int]$TallyPort = 9000
)
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:USERPROFILE ".tallycloud"
$key = Join-Path $dir "tunnel_key"
New-Item -ItemType Directory -Force $dir | Out-Null

if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
  throw "OpenSSH client is missing. Settings > Apps > Optional features > Add 'OpenSSH Client', then run this again."
}
if (-not (Test-Path $key)) {
  ssh-keygen.exe -t ed25519 -N '""' -C "tally-$env:COMPUTERNAME" -f $key | Out-Null
}

# The loop the scheduled task runs: reconnect 10 s after any drop.
$runner = Join-Path $dir "tunnel.ps1"
@"
`$ErrorActionPreference = 'Continue'
while (`$true) {
  ssh.exe -N -i "$key" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 ``
    -o StrictHostKeyChecking=accept-new -o BatchMode=yes ``
    -R 127.0.0.1:${RemotePort}:127.0.0.1:${TallyPort} tallytunnel@$Server 2>> "$dir\tunnel.log"
  Start-Sleep -Seconds 10
}
"@ | Set-Content -Encoding UTF8 $runner

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Tally Cloud tunnel" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "Tally Cloud tunnel"

Write-Host ""
Write-Host "Tunnel task installed and started ('Tally Cloud tunnel' in Task Scheduler)." -ForegroundColor Green
Write-Host "If this is the first time, run this on the server (one line):" -ForegroundColor Yellow
Write-Host ""
Write-Host ("  sudo bash /root/projects/tallycloud/deploy/add-tally-pc.sh `"" + (Get-Content "$key.pub").Trim() + "`" $RemotePort")
Write-Host ""
Write-Host "Then, in the Tally Cloud web app: Connector tab, host 127.0.0.1, port $RemotePort, and Check Tally."
Write-Host "Problems: see $dir\tunnel.log"
