# =============================================================================
# Build the LITE arm64 image on THIS PC and deploy it to the Raspberry Pi.
#
# Flow:  buildx (arm64) -> docker save -> scp to Pi -> docker load -> compose up
# No Docker registry / login required - the image is transferred as a tarball.
#
# Prereqers (on this PC):
#   - Docker Desktop running, with buildx + QEMU (default on Docker Desktop)
#   - OpenSSH client (ssh/scp) - same one deploy.bat already uses
#
# Run from the project root:
#   powershell -ExecutionPolicy Bypass -File .\build-pi-image.ps1
# =============================================================================

$ErrorActionPreference = 'Stop'

# --- Config (matches deploy.bat) ---
$PiUser   = 'beltzer'
$PiHost   = 'dorothy'
$PiDir    = '/home/beltzer/alert-dashboard-v2'
$ImageTag = 'alert-dashboard-v2:lite'
$TarName  = 'adv2-lite-arm64.tar'

Set-Location -Path $PSScriptRoot

Write-Host '============================================' -ForegroundColor Cyan
Write-Host '  Build LITE arm64 image -> deploy to Pi'      -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan
Write-Host ''

# --- 0. Ensure arm64 emulation is available (no-op if already installed) ---
Write-Host '[0/6] Ensuring arm64 emulation (binfmt)...' -ForegroundColor Yellow
docker run --privileged --rm tonistiigi/binfmt --install arm64 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host '      (binfmt step skipped/failed - continuing; usually already set)' -ForegroundColor DarkGray }

# --- 1. Build the arm64 lite image and load it into local Docker ---
Write-Host '[1/6] Building arm64 lite image (this is where the heavy lifting happens)...' -ForegroundColor Yellow
docker buildx build --platform linux/arm64 -f Dockerfile.lite -t $ImageTag --load .
if ($LASTEXITCODE -ne 0) { throw 'buildx build failed' }

# --- 2. Save the image to a tarball ---
Write-Host '[2/6] Saving image to tarball...' -ForegroundColor Yellow
docker save -o $TarName $ImageTag
if ($LASTEXITCODE -ne 0) { throw 'docker save failed' }
$sizeMB = [math]::Round((Get-Item $TarName).Length / 1MB, 1)
Write-Host "      $TarName = $sizeMB MB" -ForegroundColor DarkGray

# --- 3. Make sure the Pi has the proxy network + project dir ---
Write-Host "[3/6] Preparing $PiUser@$PiHost (enter password if prompted)..." -ForegroundColor Yellow
ssh "$PiUser@$PiHost" "docker network create proxy 2>/dev/null || true; mkdir -p $PiDir"
if ($LASTEXITCODE -ne 0) { throw "SSH to $PiHost failed - is the Pi reachable?" }

# --- 4. Copy the image + stack files to the Pi ---
Write-Host '[4/6] Transferring image + compose + Caddyfile + .env...' -ForegroundColor Yellow
scp $TarName "$PiUser@${PiHost}:$PiDir/"
if ($LASTEXITCODE -ne 0) { throw 'scp of image failed' }
scp docker-compose.pi.yml Caddyfile .env "$PiUser@${PiHost}:$PiDir/"
if ($LASTEXITCODE -ne 0) { throw 'scp of stack files failed' }

# --- 5. Load the image and (re)start the stack on the Pi ---
Write-Host '[5/6] Loading image and starting stack on the Pi...' -ForegroundColor Yellow
$remote = "cd $PiDir && docker load -i $TarName && rm $TarName && " +
          "docker compose -f docker-compose.pi.yml up -d && docker image prune -f"
ssh "$PiUser@$PiHost" $remote
if ($LASTEXITCODE -ne 0) { Write-Host '[WARN] Remote load/up reported an issue - check: ssh '"$PiUser@$PiHost"' then docker compose -f docker-compose.pi.yml logs' -ForegroundColor Red }

# --- 6. Clean up local tarball + show status ---
Write-Host '[6/6] Cleaning up and checking status...' -ForegroundColor Yellow
Remove-Item $TarName -ErrorAction SilentlyContinue
ssh "$PiUser@$PiHost" "cd $PiDir && docker compose -f docker-compose.pi.yml ps && echo '' && echo 'Disk:' && df -h / | tail -1"

Write-Host ''
Write-Host '============================================' -ForegroundColor Green
Write-Host '  Pi (LITE) deployment complete'              -ForegroundColor Green
Write-Host '============================================' -ForegroundColor Green
Write-Host '  Remote:  https://atmosphericx.ddns.net/v2/'
Write-Host '  Ticker:  https://atmosphericx.ddns.net/v2/widgets/ticker.html'
Write-Host '  Radar:   run the FULL dashboard on this PC when you need it'
Write-Host ''
Write-Host '  Tip: add a daily image prune on the Pi to keep the SD card clean:'
Write-Host "    ssh $PiUser@$PiHost ""(crontab -l 2>/dev/null; echo '0 4 * * * docker image prune -f') | crontab -"""
