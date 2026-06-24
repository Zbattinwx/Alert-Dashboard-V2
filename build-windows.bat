@echo off
REM ============================================================
REM  Build the Alert Dashboard V2 Windows server bundle.
REM
REM  Produces:  dist-windows\AlertDashboardV2-Server.zip
REM  Copy that to the ONW PC and extract (see packaging\windows\README-DEPLOY.txt).
REM
REM  Run from the repo root in a normal Command Prompt:
REM      build-windows.bat
REM  (Use cmd.exe, NOT git-bash -- git-bash mangles the /v2/ base path.)
REM ============================================================
title Build Alert Dashboard V2 - Windows Server Bundle
color 0B
cd /d "%~dp0"

set BUNDLE=dist-windows\AlertDashboardV2-Server

if not exist ".venv-build\Scripts\pyinstaller.exe" (
    echo [ERROR] .venv-build\Scripts\pyinstaller.exe not found.
    echo         The build virtualenv with PyInstaller + radar deps is required.
    pause
    exit /b 1
)

echo.
echo [1/4] Building frontend with base path /v2/ ...
REM cmd.exe 'set' does NOT path-convert the leading slash (git-bash does).
pushd frontend
set VITE_BASE_PATH=/v2/
call npm run build
popd
if not exist "frontend\dist\index.html" (
    echo [ERROR] Frontend build failed - frontend\dist\index.html was not produced.
    pause
    exit /b 1
)

echo.
echo [2/4] Freezing backend EXE with PyInstaller (this takes a few minutes)...
.venv-build\Scripts\pyinstaller.exe --noconfirm --clean --onedir --name dashboard-backend ^
  --distpath packaging\dist --workpath packaging\build --specpath packaging ^
  --paths . ^
  --collect-all uvicorn --collect-all sounderpy --collect-all metpy --collect-all cartopy ^
  --collect-all pyproj --collect-all shapely --collect-all netCDF4 --collect-all matplotlib ^
  --collect-all slixmpp --collect-all pyart --collect-all pint --collect-all xradar ^
  --collect-all cmweather --collect-all open_radar_data --collect-all xarray ^
  --collect-all eccodes --collect-all findlibs ^
  --collect-submodules backend ^
  --add-data "%CD%\backend\data;backend\data" ^
  --add-data "%CD%\frontend\dist;frontend\dist" ^
  --add-data "%CD%\widgets;widgets" ^
  --add-data "%CD%\config\brands;config\brands" ^
  packaging\run_backend.py
if not exist "packaging\dist\dashboard-backend\dashboard-backend.exe" (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo [3/4] Assembling bundle...
if exist "%BUNDLE%" rmdir /s /q "%BUNDLE%"
mkdir "%BUNDLE%"
robocopy "packaging\dist\dashboard-backend" "%BUNDLE%\dashboard-backend" /MIR /njh /njs /ndl /nc /ns >nul
if exist caddy.exe ( copy /y caddy.exe "%BUNDLE%\caddy.exe" >nul ) else ( echo [WARN] caddy.exe missing - run setup_caddy.bat to fetch it. )
copy /y "packaging\windows\Caddyfile"         "%BUNDLE%\Caddyfile" >nul
copy /y "packaging\windows\start-server.bat"  "%BUNDLE%\start-server.bat" >nul
copy /y "packaging\windows\update.bat"        "%BUNDLE%\update.bat" >nul
copy /y "packaging\windows\.env.example"      "%BUNDLE%\.env.example" >nul
copy /y "packaging\windows\README-DEPLOY.txt" "%BUNDLE%\README-DEPLOY.txt" >nul

echo.
echo [4/4] Zipping bundle...
powershell -NoProfile -Command "Compress-Archive -Path '%BUNDLE%\*' -DestinationPath 'dist-windows\AlertDashboardV2-Server.zip' -Force"

echo.
echo ============================================================
echo  DONE:  dist-windows\AlertDashboardV2-Server.zip
echo  Copy it to the ONW PC, extract, set up .env, run start-server.bat.
echo  (Full steps: packaging\windows\README-DEPLOY.txt)
echo ============================================================
pause
