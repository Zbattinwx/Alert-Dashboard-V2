@echo off
title Deploy Infrastructure Stack to Raspberry Pi
color 0B

echo ============================================
echo   Deploy Infra Stack to Pi (dorothy)
echo   Portainer + Uptime Kuma + Homepage
echo ============================================
echo.

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy
set PI_DIR=/home/beltzer/infra

:: Navigate to project directory
cd /d "%~dp0"

:: Step 1: Transfer compose file
echo [1/3] Transferring docker-compose.yml...
ssh %PI_USER%@%PI_HOST% "mkdir -p %PI_DIR%"
if errorlevel 1 (
    echo [ERROR] SSH connection failed. Make sure the Pi is reachable.
    pause
    exit /b 1
)

scp infra\docker-compose.yml %PI_USER%@%PI_HOST%:%PI_DIR%/
if errorlevel 1 (
    echo [ERROR] File transfer failed.
    pause
    exit /b 1
)
echo.

:: Step 2: Pull images and start containers
echo [2/3] Pulling images and starting containers...
echo       (first pull may take a few minutes on Pi)
echo.
ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && docker compose pull && docker compose up -d"
if errorlevel 1 (
    echo [WARN] Docker may have had issues. Check manually:
    echo        ssh %PI_USER%@%PI_HOST%
    echo        cd %PI_DIR% ^&^& docker compose logs
)
echo.

:: Step 3: Verify
echo [3/3] Checking container status...
ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && docker compose ps"
echo.

echo ============================================
echo   Infrastructure Deployment Complete!
echo ============================================
echo.
echo   Portainer:  https://%PI_HOST%:9443
echo   Uptime Kuma: http://%PI_HOST%:3001
echo   Homepage:    http://%PI_HOST%:3000
echo.
echo   NOTE: These services are LAN-only.
echo         Do NOT port-forward 9443, 3001, or 3000.
echo ============================================

pause
