@echo off
title Pi Weather Hub Status
color 0B

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy

echo ============================================
echo   dorothy - Weather Hub Status
echo ============================================
echo.

ssh %PI_USER%@%PI_HOST% "echo '=== DISK ===' && df -h / && echo '' && echo '=== MEMORY ===' && free -h && echo '' && echo '=== TEMPERATURE ===' && vcgencmd measure_temp && echo '' && echo '=== CONTAINERS ===' && docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' && echo '' && echo '=== UPTIME ===' && uptime"

if errorlevel 1 (
    echo [ERROR] Could not connect to %PI_HOST%.
)

echo.
pause
