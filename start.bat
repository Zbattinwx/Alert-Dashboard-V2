@echo off
title Alert Dashboard V2
color 0A

echo ============================================
echo        Alert Dashboard V2 - Startup
echo ============================================
echo.

:: Navigate to project directory
cd /d "%~dp0"

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ and add it to PATH.
    pause
    exit /b 1
)

:: Check if Ollama should be started
findstr /i "LLM_ENABLED=true" .env >nul 2>&1
if %errorlevel%==0 (
    echo [INFO] LLM is enabled - checking Ollama...
    where ollama >nul 2>&1
    if %errorlevel%==0 (
        :: Check if Ollama is already running
        curl -s http://localhost:11434/api/tags >nul 2>&1
        if errorlevel 1 (
            echo [INFO] Starting Ollama in background...
            start /min "Ollama" ollama serve
            timeout /t 3 /nobreak >nul
        ) else (
            echo [INFO] Ollama is already running.
        )
    ) else (
        echo [WARN] Ollama not found - LLM chat features will be unavailable.
    )
) else (
    echo [INFO] LLM is disabled in .env - skipping Ollama.
)

:: Check if Caddy reverse proxy should be started
findstr /i "CADDY_ENABLED=true" .env >nul 2>&1
if %errorlevel%==0 (
    if exist caddy.exe (
        echo [INFO] Starting Caddy reverse proxy...
        start /min "Caddy" caddy.exe run --config Caddyfile
        timeout /t 2 /nobreak >nul
        echo [INFO] Caddy started - HTTPS available at https://atmosphericx.ddns.net
    ) else (
        echo [WARN] CADDY_ENABLED=true but caddy.exe not found.
        echo [WARN] Run setup_caddy.bat to download and configure Caddy.
    )
) else (
    echo [INFO] Caddy is disabled in .env - local access only.
)

echo.
echo [INFO] Starting Alert Dashboard backend on port 3074...
echo [INFO] Dashboard will be available at http://localhost:3074
findstr /i "CADDY_ENABLED=true" .env >nul 2>&1
if %errorlevel%==0 (
    echo [INFO] Remote access: https://atmosphericx.ddns.net
    echo [INFO] Chase Mode:    https://atmosphericx.ddns.net/chase
)
echo [INFO] Docker: docker compose up -d  (for Raspberry Pi deployment)
echo [INFO] Press Ctrl+C to stop.
echo.

python backend/main.py

pause
