@echo off
title Quick Deploy - Alert Dashboard V2
color 0B

echo ============================================
echo   Quick Deploy to Pi (source files only)
echo ============================================
echo.
echo   Use this when ONLY code files changed.
echo   For dependency or frontend changes, use
echo   deploy.bat instead (full rebuild).
echo.

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy
set PI_DIR=/home/beltzer/alert-dashboard-v2

:: Navigate to project directory
cd /d "%~dp0"

:: Step 1: Sync backend Python files
echo [1/3] Syncing backend files...
tar -czf _quick_backend.tar.gz backend/
scp _quick_backend.tar.gz %PI_USER%@%PI_HOST%:%PI_DIR%/
if errorlevel 1 (
    echo [ERROR] Transfer failed.
    del _quick_backend.tar.gz 2>nul
    pause
    exit /b 1
)
del _quick_backend.tar.gz 2>nul

:: Step 2: Sync widget files
echo [2/3] Syncing widget files...
tar -czf _quick_widgets.tar.gz widgets/
scp _quick_widgets.tar.gz %PI_USER%@%PI_HOST%:%PI_DIR%/
if errorlevel 1 (
    echo [ERROR] Transfer failed.
    del _quick_widgets.tar.gz 2>nul
    pause
    exit /b 1
)
del _quick_widgets.tar.gz 2>nul

:: Step 3: Extract on Pi and restart
echo [3/3] Extracting and restarting V2...
ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && tar -xzf _quick_backend.tar.gz && rm _quick_backend.tar.gz && tar -xzf _quick_widgets.tar.gz && rm _quick_widgets.tar.gz && docker restart alert-dashboard-v2"
echo.

echo ============================================
echo   Quick deploy complete!
echo ============================================
echo.
echo   Backend + widgets updated. No rebuild needed.
echo   Widget changes are live immediately.
echo   Backend changes take effect after restart.
echo.
echo   If you changed frontend code, package.json,
echo   or requirements.txt, use deploy.bat instead.
echo ============================================

pause
