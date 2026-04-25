@echo off
title Full Weather Hub Deployment
color 0B

echo ============================================
echo   Full Weather Hub Deployment to dorothy
echo ============================================
echo.
echo   This script deploys ALL services to a
echo   fresh Raspberry Pi in the correct order.
echo.
echo   Prerequisites:
echo     1. Pi is booted with Raspberry Pi OS Lite
echo     2. Docker is installed (curl -fsSL https://get.docker.com ^| sh)
echo     3. SSH key auth is configured
echo     4. Docker network 'proxy' is created
echo.
echo   Services deployed (in order):
echo     1. Infrastructure (Portainer, Uptime Kuma, Homepage)
echo     2. Alert Dashboard V2 + Caddy
echo     3. RTMA Mesoanalysis
echo     4. SevereRecap API
echo.

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy

set ALERT_DIR=%~dp0
set TBF_DIR=C:\Users\troja\Documents\TheBattinFront

:: Confirm before proceeding
echo Press any key to start full deployment, or Ctrl+C to cancel...
pause > nul
echo.

:: ========================================
:: Step 1: Ensure Docker network exists
:: ========================================
echo [Step 1/5] Ensuring Docker network 'proxy' exists...
ssh %PI_USER%@%PI_HOST% "docker network create proxy 2>/dev/null || echo 'Network proxy already exists'"
echo.

:: ========================================
:: Step 2: Deploy Infrastructure
:: ========================================
echo [Step 2/5] Deploying Infrastructure Stack...
echo ----------------------------------------
call "%ALERT_DIR%deploy-infra.bat"
echo.

:: ========================================
:: Step 3: Deploy Alert Dashboard V2
:: ========================================
echo [Step 3/5] Deploying Alert Dashboard V2...
echo ----------------------------------------
call "%ALERT_DIR%deploy.bat"
echo.

:: ========================================
:: Step 4: Deploy RTMA
:: ========================================
echo [Step 4/5] Deploying RTMA Mesoanalysis...
echo ----------------------------------------
if exist "%TBF_DIR%\deploy-rtma.bat" (
    call "%TBF_DIR%\deploy-rtma.bat" full
) else (
    echo [SKIP] deploy-rtma.bat not found at %TBF_DIR%
)
echo.

:: ========================================
:: Step 5: Deploy SevereRecap
:: ========================================
echo [Step 5/5] Deploying SevereRecap API...
echo ----------------------------------------
if exist "%TBF_DIR%\deploy-recap.bat" (
    call "%TBF_DIR%\deploy-recap.bat"
) else (
    echo [SKIP] deploy-recap.bat not found at %TBF_DIR%
)
echo.

:: ========================================
:: Final Status
:: ========================================
echo ============================================
echo   Deployment Complete - Final Status
echo ============================================
echo.
call "%ALERT_DIR%pi-status.bat"
