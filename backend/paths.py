"""
Filesystem locations shared across the app.

These lived at module scope in main.py, where seven route groups reached for
FRONTEND_DIR and four for _GRAPHICS_DIR. That made them the last thing blocking
those groups from being split out: a router cannot import them from main.py,
because main.py imports the router.

Frozen builds are the reason these are computed rather than written as literals.
`Path(__file__).parent.parent` resolves inside the PyInstaller bundle, which is
correct for read-only assets shipped WITH the app (the frontend build, the
widgets) and wrong for anything written at runtime -- the bundle's internal tree
is replaced wholesale on update. Runtime data therefore resolves against the
process working directory instead, matching settings.data_dir and the one
directory the updater preserves. Getting that backwards once already cost a
near-miss where retrained models would have been deleted on every update.
"""

from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent

# ── Shipped with the build: read-only, safe inside a bundle ────────────────
FRONTEND_DIR = _REPO / "frontend" / "dist"
WIDGETS_DIR = _REPO / "widgets"

# ── Written at runtime: must NOT resolve into a bundle's internal tree ─────
# Relative to the working directory, which start-server.bat sets to the deploy
# root -- the same place settings.data_dir resolves to.
DATA_DIR = Path("data")
SOUNDS_DIR = DATA_DIR / "sounds"
GRAPHICS_DIR = _REPO / "data" / "alert_graphics"

# Back-compat alias: main.py and several route handlers spell it with the
# leading underscore. Kept so the extraction did not have to rewrite call sites
# at the same time as moving them -- one change at a time.
_GRAPHICS_DIR = GRAPHICS_DIR


def ensure_runtime_dirs() -> None:
    """Create the runtime directories. Called at startup, not at import.

    Import-time mkdir means merely importing this module writes to disk, which
    surprises tests and any tool that imports the app to inspect it.
    """
    for d in (SOUNDS_DIR, GRAPHICS_DIR):
        d.mkdir(parents=True, exist_ok=True)
