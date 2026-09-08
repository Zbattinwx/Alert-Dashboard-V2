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
# Invoke through the interpreter, not the console-script exe. Those .exe shims bake an
# ABSOLUTE python path at install time, so moving the venv leaves them pointing at a path
# that no longer exists - and they then fail with exit 1 and NO output, which under
# `set -e` kills this script silently. (Bit us on 2026-09-01 after the F:\Apps move.)
VENVPY="$VENV/Scripts/python.exe"
[ -x "$VENVPY" ] || VENVPY="$VENV/bin/python"
PYI="$VENVPY -m PyInstaller"

cd "$ROOT"

# Fail fast and legibly if the model seeds are missing: --add-data on a path
# that does not exist fails deep inside PyInstaller with a much worse message.
"$VENVPY" -c "import sklearn, joblib" 2>/dev/null || {
  echo "ERROR: .venv-build lacks scikit-learn/joblib - the frozen backend"
  echo "       would load no models. Fix with:"
  echo "       $VENVPY -m pip install scikit-learn joblib threadpoolctl"
  exit 1
}

for m in data/rotation_model.joblib data/severe_model.joblib; do
  [ -f "$m" ] || { echo "ERROR: $m is missing - train it before freezing"; exit 1; }
done

# Rebuild the dashboard frontend so the bundled dist always matches source.
# PyInstaller below only copies frontend/dist (--add-data) — it never builds it,
# so without this the bundled dashboard quietly drifts behind the frontend code.
#
# VITE_BASE_PATH MATTERS AND IS NOT OPTIONAL FOR A HUB SERVER BUNDLE.
# The dashboard's index.html hard-codes its asset URLs at build time. Caddy
# serves it behind `handle_path /dash/*`, which STRIPS the prefix, so a bundle
# built at the default base "/" asks the browser for /assets/... -- that lands
# on the Hub's radar app at the site root, gets index.html back from its SPA
# fallback, and the dashboard is a white screen with a MIME-type error.
#
# The DESKTOP app is the opposite case: it serves this same dashboard from
# localhost:3074 at the ROOT and wants the default. One frozen backend cannot
# satisfy both, so pick the base for the artefact you are building:
#
#   VITE_BASE_PATH=/dash/ bash packaging/build_backend.sh   # Hub server bundle
#   bash packaging/build_backend.sh                         # desktop app
echo "Building dashboard frontend (frontend/dist, base=${VITE_BASE_PATH:-/})..."
( cd "$ROOT/frontend" && { [ -d node_modules ] || npm ci; } && npm run build )

# PyInstaller is Windows Python — it needs a native path for --add-data, not the
# MSYS /c/... form git-bash's pwd returns (that mangles to C:\c\...).
ROOT_WIN="$(pwd -W 2>/dev/null || pwd)"
$PYI --noconfirm --clean --onedir --name dashboard-backend \
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
  `# The models are pickled sklearn estimators. PyInstaller cannot see that` \
  `# through a pickle -- the import is data, not code -- so collect it` \
  `# explicitly or the exe fails with "No module named sklearn" and drops` \
  `# to physics-only. Keep .venv-build on the sklearn version that TRAINED` \
  `# the models; unpickling across a major version can score differently.` \
  --collect-all sklearn \
  --collect-all joblib \
  --collect-all threadpoolctl \
  --collect-submodules backend \
  `# scripts/ carries FEATURE_NAMES and the trainer the retrain loop calls.` \
  `# WITHOUT THIS the tracker's "from scripts.train_rotation_model import ..."` \
  `# raises ImportError inside the exe and the backend silently drops to` \
  `# physics-only -- which is what every shipped build did until 2026-09-07.` \
  --collect-submodules scripts \
  --add-data "$ROOT_WIN/scripts;scripts" \
  `# Trained models as a read-only SEED so a fresh install classifies on day` \
  `# one. backend/services/model_paths.py prefers a retrained copy written` \
  `# beside the .exe, so shipping these cannot pin anyone to a stale model.` \
  --add-data "$ROOT_WIN/data/rotation_model.joblib;data" \
  --add-data "$ROOT_WIN/data/severe_model.joblib;data" \
  --add-data "$ROOT_WIN/backend/data;backend/data" \
  --add-data "$ROOT_WIN/frontend/dist;frontend/dist" \
  `# US state outlines — the mesoanalysis land mask rasterizes these. Shipped` \
  `# from frontend/src because backend/data is gitignored, so a fresh clone` \
  `# has only this (tracked) copy and the freeze would otherwise get nothing.` \
  --add-data "$ROOT_WIN/frontend/src/data;frontend/src/data" \
  --add-data "$ROOT_WIN/widgets;widgets" \
  --add-data "$ROOT_WIN/config/brands;config/brands" \
  packaging/run_backend.py

echo "Built: packaging/dist/dashboard-backend/"
