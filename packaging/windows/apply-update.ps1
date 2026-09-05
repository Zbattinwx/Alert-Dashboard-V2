<#
  apply-update.ps1 -- in-place self-updater for the standalone AlertDashboardV2
  Windows server. Spawned DETACHED by the backend (POST /api/update/apply) so it
  survives the backend being killed during the swap.

  It downloads the new bundle from GitHub Releases, verifies its SHA-256, then
  stops -> backs up -> swaps -> restarts the server. Your Caddyfile, .env, and data\
  are never touched. The previous app is kept in _backup\ for rollback.

  Params come from the backend's update manifest:
    -Url        direct download URL of AlertDashboardV2-Server.zip
    -Sha256     expected SHA-256 of that zip
    -DeployRoot the folder holding start-server.bat / version.json (this app)
    -Build      the new build id (for logging + version.json)
#>
param(
  [Parameter(Mandatory = $true)][string]$Url,
  [Parameter(Mandatory = $true)][string]$Sha256,
  [Parameter(Mandatory = $true)][string]$DeployRoot,
  [string]$Build = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $DeployRoot
$log = Join-Path $DeployRoot "update.log"
$flag = Join-Path $DeployRoot ".updating"

function Log($m) {
  $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
  Write-Output $line
  try { Add-Content -LiteralPath $log -Value $line -Encoding utf8 } catch {}
}

# install-services.ps1 registers these with -RestartCount 999 -RestartInterval
# 1min, so Windows resurrects the backend WHILE we are swapping files and the
# robocopy /MIR then fails on a locked dashboard-backend.exe. taskkill alone
# cannot win that race -- the tasks have to be disabled for the duration. The
# .updating flag only ever spoke to start-server.bat's own restart loop.
$script:TaskNames  = @('TBF Dashboard Backend', 'TBF Caddy')
$script:HeldTasks  = @()

function Stop-ServiceTasks {
  foreach ($t in $script:TaskNames) {
    if (-not (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue)) { continue }
    try {
      Disable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
      Stop-ScheduledTask    -TaskName $t -ErrorAction SilentlyContinue
      $script:HeldTasks += $t
      Log "disabled scheduled task '$t' for the swap"
    } catch { Log "WARN could not disable task '$t': $($_.Exception.Message)" }
  }
}

function Restore-ServiceTasks {
  foreach ($t in $script:HeldTasks) {
    try {
      Enable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
      Start-ScheduledTask  -TaskName $t -ErrorAction SilentlyContinue
      Log "re-enabled and started scheduled task '$t'"
    } catch { Log "WARN could not restore task '$t': $($_.Exception.Message)" }
  }
  return $script:HeldTasks.Count -gt 0
}

function Start-Server {
  # When this deployment runs under the scheduled tasks, THEY are the server --
  # relaunching start-server.bat as well would put a second backend on port 3074.
  if (Restore-ServiceTasks) { return }
  $ss = Join-Path $DeployRoot "start-server.bat"
  if (Test-Path -LiteralPath $ss) {
    Start-Process -FilePath $ss -WorkingDirectory $DeployRoot
    Log "relaunched start-server.bat"
  } else {
    Log "WARN start-server.bat missing - server NOT relaunched"
  }
}

try {
  Log "===== auto-update start (build $Build) ====="

  # 1. Download to a temp workspace
  $work = Join-Path $env:TEMP ("adv2-update-" + $Build)
  if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
  New-Item -ItemType Directory -Path $work | Out-Null
  $zip = Join-Path $work "bundle.zip"
  Log "downloading $Url"
  $old = $ProgressPreference; $ProgressPreference = "SilentlyContinue"
  Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
  $ProgressPreference = $old

  # 2. Verify checksum BEFORE touching the running install
  $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
  if ($got -ne $Sha256.ToLower()) {
    Log "ABORT checksum mismatch got=$got want=$($Sha256.ToLower())"
    throw "SHA-256 mismatch -- refusing to install"
  }
  Log "sha256 verified"

  # 3. Extract and locate the new dashboard-backend folder
  $ex = Join-Path $work "x"
  Expand-Archive -LiteralPath $zip -DestinationPath $ex -Force
  $srcApp = Get-ChildItem -LiteralPath $ex -Recurse -Directory -Filter "dashboard-backend" | Select-Object -First 1
  if (-not $srcApp) { throw "dashboard-backend not found inside the downloaded bundle" }
  $srcRoot = $srcApp.Parent.FullName
  Log "extracted; new app at $($srcApp.FullName)"

  # 4. Signal the restart loop to stand down, then stop the server
  New-Item -ItemType File -Path $flag -Force | Out-Null
  Log "stopping server"
  Stop-ServiceTasks
  cmd /c "taskkill /f /im dashboard-backend.exe >nul 2>&1"
  cmd /c "taskkill /f /im caddy.exe >nul 2>&1"
  Start-Sleep -Seconds 3

  # 5. Back up the current app, then install the new one
  $appDst = Join-Path $DeployRoot "dashboard-backend"
  $backup = Join-Path $DeployRoot "_backup"
  if (Test-Path -LiteralPath $appDst) {
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
    Move-Item -LiteralPath $appDst -Destination $backup
    Log "backed up current app to _backup"
  }
  Log "installing new app"
  # robocopy exit codes 0-7 are success; 8+ are failures
  cmd /c "robocopy `"$(Join-Path $srcRoot 'dashboard-backend')`" `"$appDst`" /MIR /NJH /NJS /NDL /NC /NS >nul"
  if ($LASTEXITCODE -ge 8) { throw "robocopy failed (code $LASTEXITCODE)" }

  # 6. Refresh SAFE support files only. Never overwrite Caddyfile / .env / data\,
  #    and never overwrite this running script (apply-update.ps1) or update.bat.
  foreach ($f in @("start-server.bat", "run-backend.cmd", "version.json")) {
    $s = Join-Path $srcRoot $f
    if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination (Join-Path $DeployRoot $f) -Force }
  }
  # A newer apply-update.ps1 / update.bat can't replace itself while running --
  # stage it as .new so start-server.bat can promote it on next launch.
  foreach ($f in @("apply-update.ps1", "update.bat")) {
    $s = Join-Path $srcRoot $f
    if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination (Join-Path $DeployRoot ($f + ".new")) -Force }
  }
  $newCaddy = Join-Path $srcRoot "caddy.exe"
  if (Test-Path -LiteralPath $newCaddy) { Copy-Item -LiteralPath $newCaddy -Destination (Join-Path $DeployRoot "caddy.exe") -Force }

  # 6b. The Hub's browser frontend, when this bundle carries one AND this
  #     deployment actually serves it. The updater only ever swapped
  #     dashboard-backend, so on a Hub server (C:\TBF\server + C:\TBF\app) the
  #     radar app itself never updated -- the whole point of most releases.
  #     Guarded on the target already existing so a plain dashboard server (no
  #     app\ folder) is untouched.
  $srcWeb = Join-Path (Split-Path -Parent $srcRoot) "app"
  $dstWeb = Join-Path (Split-Path -Parent $DeployRoot) "app"
  if ((Test-Path -LiteralPath (Join-Path $srcWeb "index.html")) -and
      (Test-Path -LiteralPath (Join-Path $dstWeb "index.html"))) {
    Log "updating Hub frontend at $dstWeb"
    cmd /c "robocopy `"$srcWeb`" `"$dstWeb`" /MIR /NJH /NJS /NDL /NC /NS >nul"
    if ($LASTEXITCODE -ge 8) { throw "frontend robocopy failed (code $LASTEXITCODE)" }
    Log "Hub frontend updated"
  }

  # 7. Done -- clear the flag and relaunch
  Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  Start-Server
  Log "===== auto-update complete (build $Build). Previous app in _backup\ ====="
}
catch {
  Log "ERROR $($_.Exception.Message)"
  Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
  # Don't leave the server down if we'd already stopped it
  try { Start-Server } catch {}
  exit 1
}
