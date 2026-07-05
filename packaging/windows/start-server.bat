@echo off
title Alert Dashboard V2 - Server
color 0A
cd /d "%~dp0"

REM Promote updater scripts staged by a previous auto-update (a running script
REM can't overwrite itself, so apply-update.ps1 leaves them as *.new).
if exist "apply-update.ps1.new" ( move /y "apply-update.ps1.new" "apply-update.ps1" >nul )
if exist "update.bat.new" ( move /y "update.bat.new" "update.bat" >nul )
if exist ".updating" ( del /q ".updating" >nul 2>&1 )

echo ============================================
echo    Alert Dashboard V2 - Server
echo ============================================
echo.

REM Backend binds loopback only; Caddy exposes it publicly via /v2.
REM (For direct LAN access to the backend at http://this-pc:3074, set HOST=0.0.0.0)
set HOST=127.0.0.1
set PORT=3074

if not exist "dashboard-backend\dashboard-backend.exe" (
    echo [ERROR] dashboard-backend\dashboard-backend.exe not found.
    echo         Make sure you extracted the full bundle into this folder.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARN] No .env found next to this script.
    echo        Copy your .env here, or rename .env.example, before going live;
    echo        otherwise NWWS / social / brand settings use defaults only.
    echo.
)

REM --- Optional: start Ollama if installed AND enabled (LLM / AI agent) ---
REM Flat goto flow avoids cmd's parse-time %errorlevel% trap inside ( ) blocks.
findstr /i "LLM_ENABLED=true" .env >nul 2>&1 || goto :skip_ollama
where ollama >nul 2>&1 || goto :skip_ollama
curl -s http://localhost:11434/api/tags >nul 2>&1 && goto :skip_ollama
echo [INFO] Starting Ollama in background...
start /min "Ollama" ollama serve
timeout /t 3 /nobreak >nul
:skip_ollama

REM --- Start Caddy reverse proxy (HTTPS 443 + HTTP 80, routes /v2 -> backend) ---
if exist caddy.exe (
    if exist Caddyfile (
        echo [INFO] Starting Caddy reverse proxy...
        start /min "Caddy" caddy.exe run --config Caddyfile
        timeout /t 2 /nobreak >nul
        echo [INFO] Public: https://atmosphericx.ddns.net/v2/
    ) else (
        echo [WARN] Caddyfile missing - starting backend without the reverse proxy.
    )
) else (
    echo [WARN] caddy.exe missing - /v2 public access will not work.
    echo        Run setup_caddy or place caddy.exe next to this script.
)

echo.
echo [INFO] Backend: http://127.0.0.1:%PORT%
echo [INFO] Close this window to stop the server (Caddy keeps its own window).
echo.

:loop
dashboard-backend\dashboard-backend.exe
if exist ".updating" (
    echo.
    echo [INFO] Update in progress - the updater will relaunch the server. Closing this window.
    exit /b 0
)
echo.
echo [WARN] Backend exited. Restarting in 5s...  (close window to stop)
timeout /t 5 /nobreak >nul
goto loop
