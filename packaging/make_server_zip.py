"""Build + verify the server bundle zip.

Every check here exists because the corresponding mistake shipped or nearly did:

  * PowerShell 5.1's ZipFile::CreateFromDirectory writes entry names with
    BACKSLASHES. apply-update.ps1 finds the payload with
    `Get-ChildItem -Recurse -Directory -Filter dashboard-backend`, which then
    matches nothing and the update aborts -- while the archive's SIZE looks
    completely normal. Hence Python zipfile with forward-slash arcnames, then
    simulating the updater's own lookup before the zip is allowed to ship.
  * Excluding "logs" by NAME also drops _internal/botocore/data/logs, which is
    botocore's AWS Logs SERVICE MODEL -- bundled dependency data the frozen
    backend needs to start. Match exact paths only.
  * The ML payload (sklearn, the trainer, the model seeds) has been silently
    absent from every build until recently, so it is asserted rather than
    assumed.
"""
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Defaults match the Hub deployment; both are overridable so this is usable
# from a checkout rather than only from one machine's layout. It lived in
# f:	mp for months -- outside version control, invisible to anyone reading the
# repo -- which is exactly why a release got built by a different route with
# none of the checks below.
ROOT = Path(os.environ.get("TBF_HUB_DEPLOY", "F:/Apps/tbf/Hub-Deploy"))
OUT = Path(os.environ.get("TBF_SERVER_ZIP", "F:/tmp/release/AlertDashboardV2-Server.zip"))
OUT.parent.mkdir(parents=True, exist_ok=True)

EXCLUDE_DIRS = {"logs", "server/logs", "server/dashboard-backend/logs"}


def excluded(rel: str) -> bool:
    parts = rel.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in EXCLUDE_DIRS:
            return True
    return False


n = 0
if OUT.exists():
    OUT.unlink()
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()      # forward slashes, always
        if excluded(rel):
            continue
        z.write(p, arcname=rel)
        n += 1

print(f"wrote {OUT.name}: {n:,} entries, {OUT.stat().st_size / 1e6:.2f} MB")

with zipfile.ZipFile(OUT) as z:
    names = set(z.namelist())

back = [x for x in names if "\\" in x]
print(f"  backslash entries: {len(back)}  (must be 0)")
assert not back, "backslash entries would break apply-update.ps1"

dirs = {os.path.dirname(x) for x in names}
hit = [d for d in dirs if d.split("/")[-1:] == ["dashboard-backend"]]
assert hit, "apply-update.ps1 would not find dashboard-backend"
src_root = sorted(hit, key=len)[0].rsplit("/dashboard-backend", 1)[0]
print(f"  srcRoot resolves to {src_root!r} (expect 'server')")
assert src_root == "server", src_root

for must in ("app/index.html", "app/login.html", "app/probe.html",
             "server/version.json",
             "server/dashboard-backend/dashboard-backend.exe",
             "server/dashboard-backend/_hub-app-payload/index.html",
             "server/dashboard-backend/_internal/data/rotation_model.joblib",
             "server/dashboard-backend/_internal/data/severe_model.joblib",
             "server/dashboard-backend/_internal/scripts/train_rotation_model.py"):
    assert must in names, f"missing {must}"
print("  critical paths present (incl. the ML payload)")

assert any(x.startswith("server/dashboard-backend/_internal/sklearn/") for x in names), \
    "scikit-learn missing -- the models cannot be unpickled and the backend runs physics-only"
print("  scikit-learn bundled")

# app/ IS THE BROWSER HUB -- the radar app's build, not the dashboard frontend.
# apply-update.ps1 mirrors the zip's top-level app/ straight into the web root
# with robocopy /MIR, so whatever is here becomes what a browser gets at "/".
#
# Staging it from AlertDashboard/frontend/dist instead of RadarApp/dist shipped
# the dashboard's index.html as the Hub in 0.1.29-0.1.31: the page loaded, asked
# for /dash/assets/... at the root, got nothing, and rendered a BLACK SCREEN.
# The dashboard at /dash/ kept working the whole time, which made it look like a
# frontend bug rather than a packaging one.
with zipfile.ZipFile(OUT) as z:
    hub_html = z.read("app/index.html").decode("utf-8", "replace")
assert "TheBattinFront Radar" in hub_html, (
    "app/index.html is not the radar app -- app/ must be staged from "
    "RadarApp/dist (the browser Hub), not from AlertDashboard/frontend/dist")
assert "/dash/assets/" not in hub_html, (
    "app/index.html references /dash/assets -- that is a dashboard build, or a "
    "radar build made with VITE_BASE_PATH=/dash/. The Hub is served at the root")
print("  app/ is the radar app (browser Hub), served at the root")

# The OTHER half of the base-path trap. The DASHBOARD's own frontend, bundled
# inside the frozen backend, must be built with VITE_BASE_PATH=/dash/ for a
# server bundle -- Caddy strips that prefix. Built at the default "/" it serves
# a white page with a module MIME error at /dash/ (2026-09-04).
#
# And it must be set from PowerShell, not Git Bash: MSYS rewrites a bare
# /dash/ into a Windows path, so the build silently comes out asking for
# "/Program Files/Git/dash/assets/..." -- which looks like it worked, right up
# until nothing loads. Hit on 2026-09-09; both halves are asserted here now.
DASH_INDEX = "server/dashboard-backend/_internal/frontend/dist/index.html"
with zipfile.ZipFile(OUT) as z:
    dash_html = z.read(DASH_INDEX).decode("utf-8", "replace")
assert "/dash/assets/" in dash_html, (
    "the bundled dashboard frontend is NOT built at /dash/ -- rebuild it with "
    "VITE_BASE_PATH=/dash/ (from PowerShell) and swap _internal/frontend/dist")
assert "/Program Files" not in dash_html, (
    "MSYS rewrote VITE_BASE_PATH into a Windows path -- set it from PowerShell, "
    "not Git Bash")
print("  bundled dashboard frontend is built at /dash/")

assert not [x for x in names if x.startswith("server/logs/")], "runtime logs leaked in"
assert not [x for x in names if x.startswith("server/dashboard-backend/data/")], \
    "runtime data/ leaked in -- an empty training_data.jsonl would clobber a real archive"
boto = [x for x in names if "botocore/data/logs" in x]
print(f"  botocore logs service model kept: {len(boto)} file(s)  (must be > 0)")
assert boto, "botocore service model excluded -- the frozen backend will not start"

sha = hashlib.sha256(OUT.read_bytes()).hexdigest()
build = json.loads((ROOT / "server/version.json").read_text(encoding="utf-8"))["build"]
def _notes() -> str:
    """Release notes for the update prompt, from notes.md beside this script.

    Falls back to a neutral line rather than a stale one: telling an operator
    they are about to install last month's changes is worse than telling them
    nothing.
    """
    f = OUT.parent / "notes.md"
    if f.exists():
        txt = " ".join(f.read_text(encoding="utf-8").split())
        if txt:
            return txt[:600]
    return "See the release page for what changed in this build."


latest = {
    "build": build,
    "sha256": sha,
    "url": ("https://github.com/Zbattinwx/dashboard-releases/releases/download/"
            f"build-{build}/AlertDashboardV2-Server.zip"),
    # Read from notes.md beside this script rather than a constant. Hardcoding
    # them meant every build since 0.1.26 advertised 0.1.26's changes in the
    # update prompt -- the operator was told they were installing something they
    # had already had for four releases.
    "notes": _notes(),
    "pub_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
(OUT.parent / "latest.json").write_text(json.dumps(latest, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
print(f"  latest.json build={build} sha256={sha[:16]}...")
print("VERIFIED")
