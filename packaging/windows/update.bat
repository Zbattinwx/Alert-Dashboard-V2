@echo off
title Alert Dashboard V2 - Update
color 0E
cd /d "%~dp0"

echo ============================================
echo    Alert Dashboard V2 - Update
echo ============================================
echo.
echo This refreshes the app (backend + bundled frontend + widgets) from a new
echo build placed in a "_update" folder here. Your .env, data\, and Caddyfile
echo are NOT touched.
echo.

if not exist "_update\dashboard-backend\dashboard-backend.exe" (
    echo [ERROR] _update\dashboard-backend not found.
    echo.
    echo   1. Copy the new AlertDashboardV2-Server zip to this PC.
    echo   2. Extract it, then move/copy its "dashboard-backend" folder into
    echo      a folder named "_update" right here next to this script
    echo      ^(i.e. _update\dashboard-backend\dashboard-backend.exe^).
    echo   3. Run this script again.
    echo.
    pause
    exit /b 1
)

echo [1/4] Stopping running server...
taskkill /f /im dashboard-backend.exe >nul 2>&1
taskkill /f /im caddy.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/4] Backing up current app to _backup ...
if exist dashboard-backend (
    if exist _backup rmdir /s /q _backup
    move dashboard-backend _backup >nul
)

echo [3/4] Installing new app version...
robocopy "_update\dashboard-backend" "dashboard-backend" /MIR /njh /njs /ndl /nc /ns >nul
if exist "_update\caddy.exe" copy /y "_update\caddy.exe" caddy.exe >nul
rmdir /s /q _update

echo [4/4] Restarting server...
start "" start-server.bat

echo.
echo [OK] Update complete. Previous version saved in _backup\ (delete when happy).
echo.
echo      ROLLBACK: stop the server, delete dashboard-backend, rename _backup
echo      back to dashboard-backend, then run start-server.bat.
echo.
echo      NOTE: this update does NOT change Caddyfile or .env. If a release
echo      says the Caddyfile changed, copy the new one over by hand.
echo.
pause
