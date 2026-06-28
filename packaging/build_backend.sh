#!/usr/bin/env bash
# Freeze the Alert Dashboard backend into a standalone (onedir) executable.
# Run from the repo root with the build venv active/available:
#   bash packaging/build_backend.sh
#
# Produces packaging/dist/dashboard-backend/dashboard-backend(.exe).
# onedir (not onefile): a large scientific bundle extracts slowly on every
# launch with onefile + trips antivirus — onedir starts fast and Tauri can ship
# the folder as an app resource.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv-build"
PYI="$VENV/Scripts/pyinstaller.exe"
[ -x "$PYI" ] || PYI="$VENV/bin/pyinstaller"

cd "$ROOT"

# Rebuild the dashboard frontend so the bundled dist always matches source.
# PyInstaller below only copies frontend/dist (--add-data) — it never builds it,
# so without this the bundled dashboard quietly drifts behind the frontend code.
echo "Building dashboard frontend (frontend/dist)..."
( cd "$ROOT/frontend" && { [ -d node_modules ] || npm ci; } && npm run build )

# PyInstaller is Windows Python — it needs a native path for --add-data, not the
# MSYS /c/... form git-bash's pwd returns (that mangles to C:\c\...).
ROOT_WIN="$(pwd -W 2>/dev/null || pwd)"
"$PYI" --noconfirm --clean --onedir --name dashboard-backend \
  --distpath packaging/dist \
  --workpath packaging/build \
  --specpath packaging \
  --paths . \
  --collect-all uvicorn \
  --collect-all sounderpy \
  --collect-all metpy \
  --collect-all cartopy \
  --collect-all pyproj \
  --collect-all shapely \
  --collect-all netCDF4 \
  --collect-all matplotlib \
  --collect-all slixmpp \
  --collect-all pyart \
  --collect-all pint \
  --collect-all xradar \
  --collect-all cmweather \
  --collect-all open_radar_data \
  --collect-all xarray \
  --collect-all h5netcdf \
  --collect-all h5py \
  --collect-all boto3 \
  --collect-all botocore \
  --collect-all PIL \
  --collect-all eccodes \
  --collect-all findlibs \
  --collect-submodules backend \
  --add-data "$ROOT_WIN/backend/data;backend/data" \
  --add-data "$ROOT_WIN/frontend/dist;frontend/dist" \
  --add-data "$ROOT_WIN/widgets;widgets" \
  --add-data "$ROOT_WIN/config/brands;config/brands" \
  packaging/run_backend.py

echo "Built: packaging/dist/dashboard-backend/"
