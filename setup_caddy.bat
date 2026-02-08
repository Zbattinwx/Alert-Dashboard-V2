@echo off
title Caddy Reverse Proxy Setup
color 0B

echo ============================================
echo     Caddy Reverse Proxy Setup
echo     For Alert Dashboard V2
echo ============================================
echo.

cd /d "%~dp0"

:: Check if caddy.exe already exists
if exist caddy.exe (
    echo [INFO] caddy.exe already exists in this directory.
    echo [INFO] To update, delete caddy.exe and run this script again.
    echo.
    goto :instructions
)

:: Download Caddy for Windows AMD64
echo [INFO] Downloading Caddy for Windows...
echo.

:: Use PowerShell to download
powershell -Command "& { $ProgressPreference = 'SilentlyContinue'; try { Invoke-WebRequest -Uri 'https://caddyserver.com/api/download?os=windows&arch=amd64' -OutFile 'caddy.exe' -UseBasicParsing; Write-Host '[OK] Downloaded caddy.exe successfully' } catch { Write-Host '[ERROR] Download failed:' $_.Exception.Message; exit 1 } }"

if not exist caddy.exe (
    echo [ERROR] Failed to download caddy.exe
    echo [INFO] You can manually download from: https://caddyserver.com/download
    echo [INFO] Select: Platform=Windows, Architecture=amd64
    echo [INFO] Place caddy.exe in: %~dp0
    pause
    exit /b 1
)

echo.
echo [OK] Caddy downloaded successfully!
echo.

:instructions
echo ============================================
echo     PORT FORWARDING INSTRUCTIONS
echo ============================================
echo.
echo You need to forward TWO ports on your router
echo to this computer's local IP address:
echo.
echo   Port 80  (TCP) --^> Your PC's LAN IP : 80
echo   Port 443 (TCP) --^> Your PC's LAN IP : 443
echo.
echo To find your local IP, run: ipconfig
echo Look for "IPv4 Address" under your active adapter.
echo.

:: Show the user's local IP
echo Your local IP addresses:
ipconfig | findstr /i "IPv4"
echo.

echo ============================================
echo     HOW TO RUN
echo ============================================
echo.
echo Option 1: Automatic (recommended)
echo   Set CADDY_ENABLED=true in your .env file
echo   Then run start.bat as usual - Caddy starts automatically.
echo.
echo Option 2: Manual
echo   Open a separate terminal and run:
echo   caddy.exe run --config Caddyfile
echo.
echo Your dashboard will be available at:
echo   https://atmosphericx.ddns.net
echo   https://atmosphericx.ddns.net/chase  (Chase Mode)
echo.
echo ============================================
echo     IMPORTANT NOTES
echo ============================================
echo.
echo - Caddy automatically obtains SSL certificates from Let's Encrypt
echo - First startup may take 30-60 seconds for certificate provisioning
echo - Your DDNS (atmosphericx.ddns.net) must point to your public IP
echo - Windows Firewall may prompt to allow caddy.exe - click Allow
echo - The backend (uvicorn) still runs on port 8000 locally
echo - Caddy handles HTTPS on 443 and proxies to localhost:8000
echo.

pause
