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

REM Build id = UTC timestamp (sortable / monotonic). Stamped into the bundle so
REM the in-dashboard self-updater can compare the deployed build to the published one.
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')"') do set BUILD=%%i

if not exist ".venv-build\Scripts\pyinstaller.exe" (
    echo [ERROR] .venv-build\Scripts\pyinstaller.exe not found.
    echo         The build virtualenv with PyInstaller + radar deps is required.
    pause
    exit /b 1
)

echo.
REM Base path defaults to /v2/ (the ONW deployment). Pre-set VITE_BASE_PATH to
REM build a bundle for a different mount point -- TheBattinFront's Hub serves its
REM own dashboard at /dash/ on its own hostname, and the frozen frontend has to
REM agree or every asset 404s.
if not defined VITE_BASE_PATH set VITE_BASE_PATH=/v2/
echo [1/4] Building frontend with base path %VITE_BASE_PATH% ...
REM cmd.exe 'set' does NOT path-convert the leading slash (git-bash does).
pushd frontend
call npm run build
popd
if not exist "frontend\dist\index.html" (
    echo [ERROR] Frontend build failed - frontend\dist\index.html was not produced.
    pause
    exit /b 1
)

echo.
REM The models are pickled sklearn estimators. PyInstaller cannot see that import
REM through a pickle, so sklearn/joblib/threadpoolctl are collected explicitly or
REM the exe drops to physics-only; scripts\ carries FEATURE_NAMES, which the
REM tracker imports at runtime.
REM
REM THIS SCRIPT USED TO ADD ONLY backend\data, WHICH IS EMPTY -- the models live
REM at repo-root data\ -- so it could never ship a classifier. On 2026-09-11 a
REM bundle built this way reached production with every storm-cell probability
REM column blank, and nothing said so: same exe, same layout, 4 MB lighter of 45.
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
  --collect-all sklearn --collect-all joblib --collect-all threadpoolctl ^
  --collect-submodules scripts ^
  --add-data "%CD%\scripts;scripts" ^
  --add-data "%CD%\data\rotation_model.joblib;data" ^
  --add-data "%CD%\data\severe_model.joblib;data" ^
  --add-data "%CD%\data\hail_1in_model.joblib;data" ^
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

REM Verify the OUTPUT, not just that an exe appeared. "The exe exists" was the
REM only post-condition this script had, and a freeze that writes the exe and
REM then collects nothing satisfies it.
python packaging\verify_freeze.py "packaging\dist\dashboard-backend" --base %VITE_BASE_PATH%
if errorlevel 1 (
    echo [ERROR] The freeze is incomplete - see above. Refusing to build a bundle from it.
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
copy /y "packaging\windows\apply-update.ps1"  "%BUNDLE%\apply-update.ps1" >nul
copy /y "packaging\windows\.env.example"      "%BUNDLE%\.env.example" >nul
copy /y "packaging\windows\README-DEPLOY.txt" "%BUNDLE%\README-DEPLOY.txt" >nul

REM Stamp the build id so the deployed server knows its own version.
powershell -NoProfile -Command "[ordered]@{app='2.0.0';build='%BUILD%';built_at=(Get-Date).ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -Encoding utf8 '%BUNDLE%\version.json'"
echo    build id: %BUILD%

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
