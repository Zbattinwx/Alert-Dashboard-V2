@echo off
title Deploy Alert Dashboard V2 (LITE - no NEXRAD) to Raspberry Pi
color 0E

echo ============================================
echo   Deploy V2 LITE (no NEXRAD) to Pi (dorothy)
echo ============================================
echo.
echo   This build EXCLUDES the heavy NEXRAD radar
echo   dependencies (pyart, scipy, boto3, etc.)
echo   to save ~1GB+ of disk space.
echo.

:: Configuration
set PI_USER=beltzer
set PI_HOST=dorothy
set PI_DIR=/home/beltzer/alert-dashboard-v2
set ARCHIVE=alert-dashboard-v2.tar.gz

:: Navigate to project directory
cd /d "%~dp0"

:: ========================================
:: Step 0: Clean up failed build on Pi
:: ========================================
echo [0/5] Cleaning up failed Docker build on Pi...
echo       (removing dangling images, build cache, stopped containers)
echo.

ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% 2>/dev/null && docker compose down --remove-orphans 2>/dev/null; docker system prune -af 2>/dev/null; docker builder prune -af 2>/dev/null; echo 'Cleanup done. Free space:'; df -h / | tail -1"
if errorlevel 1 (
    echo [WARN] SSH cleanup had issues, continuing anyway...
)
echo.

:: ========================================
:: Step 1: Create temporary lite files
:: ========================================
echo [1/5] Creating lite Dockerfile and requirements...

:: Create a requirements.txt without NEXRAD deps
echo # Alert Dashboard V2 Dependencies (LITE - no NEXRAD)> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Web Framework (FastAPI)>> _requirements_lite.txt
echo fastapi^>=0.104.0>> _requirements_lite.txt
echo uvicorn[standard]^>=0.24.0>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Core async and web>> _requirements_lite.txt
echo aiohttp^>=3.9.0>> _requirements_lite.txt
echo websockets^>=12.0>> _requirements_lite.txt
echo uvloop^>=0.19.0; sys_platform != 'win32'>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # XMPP for NWWS-OI>> _requirements_lite.txt
echo slixmpp^>=1.8.5>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Date/time>> _requirements_lite.txt
echo python-dateutil^>=2.8.2>> _requirements_lite.txt
echo pytz^>=2024.1>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Configuration>> _requirements_lite.txt
echo python-dotenv^>=1.0.0>> _requirements_lite.txt
echo pydantic^>=2.5.0>> _requirements_lite.txt
echo pydantic-settings^>=2.1.0>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Logging>> _requirements_lite.txt
echo structlog^>=24.1.0>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Serialization>> _requirements_lite.txt
echo orjson^>=3.9.0>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # HTTP client>> _requirements_lite.txt
echo httpx^>=0.26.0>> _requirements_lite.txt
echo tenacity^>=8.2.0>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Geospatial>> _requirements_lite.txt
echo shapely^>=2.0.0>> _requirements_lite.txt
echo.>> _requirements_lite.txt
echo # Image processing>> _requirements_lite.txt
echo Pillow^>=10.0.0>> _requirements_lite.txt

:: Create a Dockerfile without HDF5/NetCDF runtime libs
(
echo # Alert Dashboard V2 - LITE build ^(no NEXRAD^)
echo FROM node:20-alpine AS frontend-build
echo WORKDIR /app/frontend
echo COPY frontend/package*.json ./
echo RUN npm ci --no-audit
echo COPY frontend/ ./
echo ENV VITE_BASE_PATH=/v2/
echo RUN npm run build
echo.
echo FROM python:3.12-slim AS python-build
echo WORKDIR /build
echo RUN apt-get update ^&^& apt-get install -y --no-install-recommends \
echo     gcc g++ python3-dev libgeos-dev libjpeg62-turbo-dev \
echo     zlib1g-dev libffi-dev curl \
echo     ^&^& rm -rf /var/lib/apt/lists/*
echo RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs ^| sh -s -- -y
echo ENV PATH="/root/.cargo/bin:${PATH}"
echo RUN pip install --upgrade pip
echo COPY requirements.txt .
echo RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
echo.
echo FROM python:3.12-slim
echo WORKDIR /app
echo RUN apt-get update ^&^& apt-get install -y --no-install-recommends \
echo     libgeos-c1v5 libjpeg62-turbo zlib1g libffi8 \
echo     ^&^& rm -rf /var/lib/apt/lists/*
echo COPY --from=python-build /install /usr/local
echo COPY backend/ ./backend/
echo COPY --from=frontend-build /app/frontend/dist ./frontend/dist
echo COPY widgets/ ./widgets/
echo RUN mkdir -p /app/data/chase_logs/radar
echo ENV HOST=0.0.0.0
echo ENV PORT=3074
echo ENV DEBUG=false
echo EXPOSE 3074
echo CMD ["python", "backend/main.py"]
) > _Dockerfile_lite

echo       Done.
echo.

:: ========================================
:: Step 2: Create deployment archive
:: ========================================
echo [2/5] Creating deployment archive...

:: Build file list
(
    echo _Dockerfile_lite
    echo docker-compose.yml
    echo Caddyfile
    echo _requirements_lite.txt
    echo .env
    echo .dockerignore
) > _deploy_files.txt

:: Add backend (excluding __pycache__)
for /r backend %%F in (*.py *.json) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Add frontend source files
for /r frontend\src %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Add frontend config files
if exist frontend\package.json echo frontend\package.json>> _deploy_files.txt
if exist frontend\package-lock.json echo frontend\package-lock.json>> _deploy_files.txt
if exist frontend\tsconfig.json echo frontend\tsconfig.json>> _deploy_files.txt
if exist frontend\vite.config.ts echo frontend\vite.config.ts>> _deploy_files.txt
if exist frontend\index.html echo frontend\index.html>> _deploy_files.txt

:: Add frontend public assets
for /r frontend\public %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Add widgets
for /r widgets %%F in (*) do (
    set "fp=%%F"
    setlocal enabledelayedexpansion
    set "rel=!fp:%cd%\=!"
    echo !rel!>> _deploy_files.txt
    endlocal
)

:: Create archive
tar -czf %ARCHIVE% -T _deploy_files.txt
if errorlevel 1 (
    echo [ERROR] Failed to create archive.
    del _deploy_files.txt _Dockerfile_lite _requirements_lite.txt 2>nul
    pause
    exit /b 1
)

del _deploy_files.txt 2>nul
for %%A in (%ARCHIVE%) do echo       Archive size: %%~zA bytes
echo.

:: ========================================
:: Step 3: Transfer to Pi
:: ========================================
echo [3/5] Transferring to %PI_USER%@%PI_HOST%...
echo.

ssh %PI_USER%@%PI_HOST% "docker network create proxy 2>/dev/null || true && mkdir -p %PI_DIR%"
if errorlevel 1 (
    echo [ERROR] SSH connection failed.
    del %ARCHIVE% _Dockerfile_lite _requirements_lite.txt 2>nul
    pause
    exit /b 1
)

scp %ARCHIVE% %PI_USER%@%PI_HOST%:%PI_DIR%/
if errorlevel 1 (
    echo [ERROR] File transfer failed.
    del %ARCHIVE% _Dockerfile_lite _requirements_lite.txt 2>nul
    pause
    exit /b 1
)
echo.

:: ========================================
:: Step 4: Extract, swap files, build
:: ========================================
echo [4/5] Building Docker containers on Pi (LITE)...
echo       (no NEXRAD deps = much faster + smaller)
echo.

ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && tar -xzf %ARCHIVE% && rm %ARCHIVE% && mv _Dockerfile_lite Dockerfile && mv _requirements_lite.txt requirements.txt && docker compose up -d --build && docker image prune -f"
if errorlevel 1 (
    echo [WARN] Docker build may have had issues. Check manually:
    echo        ssh %PI_USER%@%PI_HOST%
    echo        cd %PI_DIR% ^&^& docker compose logs
)
echo.

:: Step 5: Clean up local temp files
del %ARCHIVE% _Dockerfile_lite _requirements_lite.txt 2>nul

:: Step 6: Verify
echo [5/5] Checking container status...
ssh %PI_USER%@%PI_HOST% "cd %PI_DIR% && docker compose ps && echo '' && echo 'Disk usage:' && df -h / | tail -1"
echo.

echo ============================================
echo   LITE Deployment complete!
echo ============================================
echo.
echo   V2 Dashboard:  https://atmosphericx.ddns.net/v2/
echo   V2 Chase Mode: https://atmosphericx.ddns.net/v2/chase
echo   LAN Access:    http://192.168.0.10/v2/
echo.
echo   NOTE: NEXRAD radar is DISABLED in this build.
echo         Upgrade Pi storage and use deploy.bat for
echo         the full build with radar + storm tracking.
echo ============================================

pause
