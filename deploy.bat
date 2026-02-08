@echo off
title Deploy Alert Dashboard V2 to Raspberry Pi
color 0B

echo ============================================
echo   Deploy Alert Dashboard V2 to Pi (dorothy)
echo ============================================
echo.

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy
set PI_DIR=/home/beltzer/alert-dashboard-v2
set ARCHIVE=alert-dashboard-v2.tar.gz

:: Navigate to project directory
cd /d "%~dp0"

:: Step 1: Create file list for archive
echo [1/4] Creating deployment archive...
echo       (excluding node_modules, .git, data, logs)

:: Build a file list to avoid Windows tar glob issues
(
    echo Dockerfile
    echo docker-compose.yml
    echo Caddyfile
    echo requirements.txt
    echo .env
    echo .dockerignore
) > _deploy_files.txt

:: Add backend (excluding __pycache__)
for /r backend %%F in (*.py) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Add frontend source files
for /r frontend\src %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Add frontend config files individually
if exist frontend\package.json echo frontend\package.json>> _deploy_files.txt
if exist frontend\package-lock.json echo frontend\package-lock.json>> _deploy_files.txt
if exist frontend\tsconfig.json echo frontend\tsconfig.json>> _deploy_files.txt
if exist frontend\vite.config.ts echo frontend\vite.config.ts>> _deploy_files.txt
if exist frontend\index.html echo frontend\index.html>> _deploy_files.txt

:: Add frontend public assets
for /r frontend\public %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Add widgets (ticker overlays for OBS/streaming)
for /r widgets %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Create the archive from file list
tar -czf %ARCHIVE% -T _deploy_files.txt
if errorlevel 1 (
    echo [ERROR] Failed to create archive.
    del _deploy_files.txt 2>nul
    pause
    exit /b 1
)

del _deploy_files.txt 2>nul
for %%A in (%ARCHIVE%) do echo       Archive size: %%~zA bytes
echo.

:: Step 2: Transfer to Pi
echo [2/4] Transferring to %PI_USER%@%PI_HOST%...
echo       (enter password when prompted)
echo.

:: Create directory on Pi and transfer archive in one scp
ssh %PI_USER%@%PI_HOST% "mkdir -p %PI_DIR%"
if errorlevel 1 (
    echo [ERROR] SSH connection failed. Make sure the Pi is reachable.
    echo         Try: ssh %PI_USER%@%PI_HOST%
    del %ARCHIVE% 2>nul
    pause
    exit /b 1
)

scp %ARCHIVE% %PI_USER%@%PI_HOST%:%PI_DIR%/
if errorlevel 1 (
    echo [ERROR] File transfer failed.
    del %ARCHIVE% 2>nul
    pause
    exit /b 1
)
echo.

:: Step 3: Extract and build on Pi
echo [3/4] Building Docker containers on Pi...
echo       (this may take several minutes on first build)
echo.

ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && tar -xzf %ARCHIVE% && rm %ARCHIVE% && docker compose up -d --build && docker builder prune -af && docker image prune -f"
if errorlevel 1 (
    echo [WARN] Docker build may have had issues. Check manually:
    echo        ssh %PI_USER%@%PI_HOST%
    echo        cd %PI_DIR% ^&^& docker compose logs
)
echo.

:: Step 4: Clean up local archive
del %ARCHIVE% 2>nul

:: Step 5: Verify
echo [4/4] Checking container status...
ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && docker compose ps"
echo.

echo ============================================
echo   Deployment complete!
echo ============================================
echo.
echo   V2 Dashboard:  https://atmosphericx.ddns.net/v2/
echo   V2 Chase Mode: https://atmosphericx.ddns.net/v2/chase
echo   V2 Ticker:     https://atmosphericx.ddns.net/v2/widgets/ticker.html
echo   LAN Access:    http://192.168.0.10/v2/
echo.
echo   Portainer: Check the 'alert-dashboard-v2' stack
echo.
echo   Useful commands (SSH into Pi first):
echo     docker compose logs -f v2      View V2 logs
echo     docker compose logs -f caddy   View Caddy logs
echo     docker compose restart v2      Restart V2
echo     docker compose down            Stop everything
echo     docker compose up -d --build   Rebuild and start
echo.
echo   NOTE: Forward ports 80 + 443 on your router
echo         to dorothy's LAN IP for HTTPS to work.
echo ============================================

pause
