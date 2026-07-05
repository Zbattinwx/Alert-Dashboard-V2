#!/usr/bin/env bash
# Publish the standalone dashboard bundle to GitHub Releases so remote servers
# can self-update. Run AFTER build-windows.bat has produced the zip.
#
#   bash packaging/windows/publish-dashboard-bundle.sh
#
# Reads the build id from the freshly built bundle's version.json, computes the
# zip's SHA-256, writes a latest.json manifest, and creates a GitHub release on
# the dashboard-releases repo with the zip + latest.json attached. The deployed
# server's updater polls .../releases/latest/download/latest.json.
#
# Env: DASHBOARD_RELEASES_REPO (default Zbattinwx/dashboard-releases),
#      RELEASE_NOTES (manifest + release body).
set -euo pipefail

REPO="${DASHBOARD_RELEASES_REPO:-Zbattinwx/dashboard-releases}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUNDLE_DIR="$ROOT/dist-windows/AlertDashboardV2-Server"
ZIP="$ROOT/dist-windows/AlertDashboardV2-Server.zip"

[ -f "$ZIP" ] || { echo "ERROR: $ZIP not found — run build-windows.bat first."; exit 1; }
[ -f "$BUNDLE_DIR/version.json" ] || { echo "ERROR: $BUNDLE_DIR/version.json not found (old build?)."; exit 1; }

BUILD="$(python -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8-sig'))['build'])" "$BUNDLE_DIR/version.json")"
[ -n "$BUILD" ] || { echo "ERROR: could not read build id from version.json"; exit 1; }

echo "==> Computing SHA-256 of the bundle ..."
SHA="$(python - "$ZIP" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"

TAG="build-$BUILD"
ASSET="AlertDashboardV2-Server.zip"
URL="https://github.com/$REPO/releases/download/$TAG/$ASSET"
NOTES="${RELEASE_NOTES:-Standalone dashboard build $BUILD}"
PUB_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

MANIFEST="$ROOT/dist-windows/latest.json"
cat > "$MANIFEST" <<EOF
{
  "app": "2.0.0",
  "build": "$BUILD",
  "sha256": "$SHA",
  "url": "$URL",
  "notes": $(python -c "import json,sys;print(json.dumps(sys.argv[1]))" "$NOTES"),
  "pub_date": "$PUB_DATE"
}
EOF
echo "==> Manifest:"; cat "$MANIFEST"

# Ensure the (public) releases repo exists — the updater downloads without auth.
if ! gh repo view "$REPO" >/dev/null 2>&1; then
  echo "==> Creating public repo $REPO ..."
  gh repo create "$REPO" --public --description "AlertDashboardV2 standalone server release channel" >/dev/null
fi

echo "==> Creating release $TAG on $REPO ..."
gh release create "$TAG" -R "$REPO" \
  --title "Dashboard build $BUILD" \
  --notes "$NOTES" \
  "$ZIP" "$MANIFEST"

echo "==> Published. Updater endpoint:"
echo "    https://github.com/$REPO/releases/latest/download/latest.json"
