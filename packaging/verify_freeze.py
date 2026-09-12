"""Did the freeze actually produce what it promised?

    python packaging/verify_freeze.py [dist_dir] [--base /dash/]

Exits non-zero and says what is missing. Both freeze scripts call it.

WHY THIS EXISTS
---------------
On 2026-09-11 a server bundle shipped to production with NO MACHINE-LEARNING
MODELS. Every storm cell lost its rotation, severe and hail probabilities --
the SVR / TOR% / HAIL% columns simply went blank -- and nothing anywhere said
so. It ran like that for hours.

The freeze scripts already checked that the model FILES EXIST IN THE REPO
before starting. That check passed. Nothing checked the freeze's OUTPUT, so a
build that silently omitted its payload was indistinguishable from one that
worked: same exe, same folder, ~4 MB lighter out of 45.

There are two freeze paths and they did not agree:

    packaging/build_backend.sh   --add-data data/*_model.joblib  (three of them)
    build-windows.bat            --add-data backend\\data         (which is EMPTY)

build-windows.bat is ONW's bundle builder -- it defaults to VITE_BASE_PATH=/v2/
and never needed the Hub's models. Reusing it for the Hub is what shipped the
broken bundle, and the category error was invisible because both scripts
produce a directory of the same name in the same place.

So this verifies the ARTEFACT, not the recipe. It does not care which script
ran, whether PyInstaller half-finished, whether the disk filled mid-build, or
whether someone hand-rolled it. If the payload is not in the output, it fails.

WHAT IT CHECKS
--------------
  * the exe exists and is not absurdly small
  * the three shipped models are present and non-trivial in size
  * sklearn is collected -- without it the models cannot be unpickled and the
    backend silently drops to physics-only, which looks identical to having
    no models at all
  * scripts/ is collected -- the tracker imports FEATURE_NAMES from it, and
    without it every build before 2026-09-07 ran physics-only
  * the dashboard frontend is present, and built at the expected base path
    when one is given (wrong base = a white screen at /dash/)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# A model is ~1.1 MB. A zero-byte or few-hundred-byte file means a truncated
# copy, which unpickles into an exception rather than a classifier.
MIN_MODEL_BYTES = 100_000
MIN_EXE_BYTES = 10_000_000

REQUIRED_MODELS = (
    "rotation_model.joblib",
    "severe_model.joblib",
    "hail_1in_model.joblib",
)


def check(dist: Path, base: str | None) -> list[str]:
    """Return a list of problems; empty means the freeze is complete."""
    bad: list[str] = []
    internal = dist / "_internal"

    exe = dist / "dashboard-backend.exe"
    if not exe.is_file():
        return [f"no dashboard-backend.exe at {exe}"]
    if exe.stat().st_size < MIN_EXE_BYTES:
        bad.append(f"dashboard-backend.exe is only {exe.stat().st_size:,} bytes")

    if not internal.is_dir():
        return bad + [f"no _internal/ at {internal} -- the freeze collected nothing"]

    data = internal / "data"
    if not data.is_dir():
        bad.append("_internal/data/ does not exist -- NO MODELS SHIPPED "
                   "(every probability column will be blank)")
    else:
        for name in REQUIRED_MODELS:
            f = data / name
            if not f.is_file():
                bad.append(f"missing model: _internal/data/{name}")
            elif f.stat().st_size < MIN_MODEL_BYTES:
                bad.append(f"truncated model: _internal/data/{name} "
                           f"({f.stat().st_size:,} bytes)")

    for pkg, why in (
        ("sklearn", "the models cannot be unpickled; the backend runs physics-only"),
        ("scripts", "the tracker's FEATURE_NAMES import fails; physics-only"),
    ):
        if not (internal / pkg).is_dir():
            bad.append(f"missing _internal/{pkg}/ -- {why}")

    index = internal / "frontend" / "dist" / "index.html"
    if not index.is_file():
        bad.append("missing _internal/frontend/dist/index.html -- no dashboard UI")
    elif base:
        html = index.read_text(encoding="utf-8", errors="replace")
        srcs = [s for s in re.findall(r'src="([^"]+\.js)"', html)
                if not s.startswith(("http://", "https://"))]
        if not srcs:
            bad.append("no local script tag in the bundled index.html")
        elif not srcs[0].startswith(base):
            bad.append(f"dashboard frontend is at '{srcs[0]}' but must start "
                       f"with '{base}' -- this is the white screen")
        if "/Program Files" in html:
            bad.append("bundled index.html carries an MSYS-mangled path "
                       "(VITE_BASE_PATH was set from Git Bash, not PowerShell)")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dist", nargs="?",
                    default=str(Path(__file__).resolve().parent / "dist" / "dashboard-backend"),
                    help="the frozen dashboard-backend directory")
    ap.add_argument("--base", default=None,
                    help="expected asset base for the bundled dashboard, e.g. /dash/ or /v2/")
    args = ap.parse_args()

    # MSYS REWRITES A LEADING-SLASH ARGUMENT into a Windows path, so `--base /`
    # arrives as "C:/Program Files/Git/" and `--base /dash/` as
    # ".../Git/dash/". That is the same conversion that produces the mangled
    # bundles this script checks for -- it caught its own invocation the first
    # time it ran from Git Bash. Say so plainly instead of reporting a
    # nonsensical expected base.
    base = args.base
    if base and ("/Git/" in base or base.rstrip("/").endswith("/Git")):
        print(f"refusing a path-converted --base: {base!r}\n"
              "  Git Bash rewrote the leading slash. Call this with\n"
              "  MSYS_NO_PATHCONV=1, or pass the base via TBF_EXPECT_BASE.",
              file=sys.stderr)
        return 2
    if not base:
        base = os.environ.get("TBF_EXPECT_BASE") or None

    dist = Path(args.dist)
    problems = check(dist, base)

    print(f"verifying freeze: {dist}")
    if problems:
        print("\nFREEZE IS INCOMPLETE -- do not ship it:")
        for p in problems:
            print(f"  !! {p}")
        print("\nRe-run the freeze. If it keeps happening, check free disk on the")
        print("TEMP drive: PyInstaller stages tens of thousands of files there.")
        return 1

    data = dist / "_internal" / "data"
    print(f"  exe            {(dist / 'dashboard-backend.exe').stat().st_size:,} bytes")
    print(f"  models         {len(REQUIRED_MODELS)} of {len(REQUIRED_MODELS)} present")
    for n in REQUIRED_MODELS:
        print(f"                 {(data / n).stat().st_size:>9,}  {n}")
    print("  sklearn        collected")
    print("  scripts        collected")
    if args.base:
        print(f"  dashboard base {args.base}")
    print("VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
