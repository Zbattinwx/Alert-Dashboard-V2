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
set ARCHIVE=_quick_deploy.tar.gz

:: Navigate to project directory
cd /d "%~dp0"

:: Step 1: Build file list for backend + widgets
echo [1/3] Creating archive...

:: Build file list (same approach as deploy.bat)
type nul > _quick_files.txt
:: Backend files (py + json)
for /r backend %%F in (*.py *.json) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _quick_files.txt
    endlocal
)
:: Widget files
for /r widgets %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _quick_files.txt
    endlocal
)

tar -czf %ARCHIVE% -T _quick_files.txt
if errorlevel 1 (
    echo [ERROR] Failed to create archive.
    del _quick_files.txt 2>nul
    pause
    exit /b 1
)
del _quick_files.txt 2>nul
for %%A in (%ARCHIVE%) do echo       Archive size: %%~zA bytes
echo.

:: Step 2: Transfer to Pi
echo [2/3] Transferring to %PI_USER%@%PI_HOST%...
scp %ARCHIVE% %PI_USER%@%PI_HOST%:%PI_DIR%/
if errorlevel 1 (
    echo [ERROR] Transfer failed.
    del %ARCHIVE% 2>nul
    pause
    exit /b 1
)
del %ARCHIVE% 2>nul
echo.

:: Step 3: Extract on Pi and restart
echo [3/3] Extracting and restarting V2...
ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && tar -xzf %ARCHIVE% && rm %ARCHIVE% && docker restart alert-dashboard-v2"
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
