@echo off
title Clean Docker on Pi
color 0E

echo ============================================
echo   Clean Docker Build Cache on Pi (dorothy)
echo ============================================
echo.
echo   WARNING: This clears the Docker build cache.
echo   The next deploy will do a FULL rebuild
echo   (Rust + Python deps, ~10-15 min on Pi).
echo.
echo   Only run this when disk space is low.
echo.

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy
set PI_DIR=/home/beltzer/alert-dashboard-v2

:: Show current disk usage
echo Current disk usage on Pi:
ssh %PI_USER%@%PI_HOST% "df -h / && echo. && echo Docker disk usage: && docker system df"
echo.

set /p CONFIRM="Proceed with cleanup? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo Cleaning up...
ssh %PI_USER%@%PI_HOST% "docker builder prune -af && docker image prune -af && echo. && echo Disk usage after cleanup: && df -h /"
echo.

echo ============================================
echo   Cleanup complete!
echo ============================================
echo.
echo   Next deploy will be a full rebuild.
echo   Subsequent deploys will be fast again
echo   (cached Python/Rust layers).
echo ============================================

pause
