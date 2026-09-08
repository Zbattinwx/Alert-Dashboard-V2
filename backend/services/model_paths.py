"""
Where the ML models live, from source AND inside the frozen backend.

This exists because the classifiers have never once run in the bundled backend.
Three separate reasons, all silent:

  1. `storm_tracking_service.load_rotation_model` reaches the model through
     `from scripts.train_rotation_model import ...`, and the PyInstaller spec
     collects submodules of `backend` only -- `scripts/` is not in the bundle at
     all, so that import raises ImportError inside the exe;
  2. the import failure is caught and logged at INFO as "running pure physics",
     which is indistinguishable from "no model has been trained yet";
  3. even had it imported, the path is `Path(__file__).parents[2] / "data"`,
     which inside a onedir bundle points into the unpacked `_internal` tree
     rather than at anything a retrain could have written.

So every Hub user has been running physics-only while the UI had a field for a
probability that was always None.

The fix has to satisfy two competing needs. Models must SHIP (a fresh install
should classify on day one) and models must be REPLACEABLE (the auto-retrain
loop writes a new one and a baked-in read-only copy could never be updated).
Hence two directories and a strict order:

  runtime  the deploy root's data/ when frozen (the CWD start-server.bat sets,
           and the one directory apply-update.ps1 preserves), else <repo>/data.
           Writable, survives an update, and a retrain writes here; wins if present.
  bundled  sys._MEIPASS/data, the read-only seed shipped in the exe. Used
           only until a retrain produces something better.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_NAMES = ("rotation_model.joblib", "severe_model.joblib")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_data_dir() -> Path:
    """Writable data directory that SURVIVES an update.

    This must agree with `settings.data_dir`, which is the relative Path("data")
    and therefore resolves against the process CWD. start-server.bat does
    `cd /d "%~dp0"`, so that is the deploy root -- the one place apply-update.ps1
    promises not to touch ("Never overwrite Caddyfile / .env / data\\").

    Do NOT use `sys.executable.parent / "data"`. That is
    server\\dashboard-backend\\data, and the updater mirrors that whole folder
    with robocopy /MIR, which DELETES anything not in the incoming bundle -- so
    the training archive and every retrained model would be destroyed on each
    update. An earlier version of this function did exactly that.
    """
    env = os.environ.get("TBF_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        try:
            from backend.config.settings import get_settings
            d = Path(get_settings().data_dir)
            return d if d.is_absolute() else (Path.cwd() / d).resolve()
        except Exception:  # noqa: BLE001 - settings unavailable this early
            return (Path.cwd() / "data").resolve()
    return Path(__file__).resolve().parents[2] / "data"


def bundled_data_dir() -> Optional[Path]:
    """Read-only seed directory inside the bundle, or None when running from source."""
    if not is_frozen():
        return None
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    return Path(base) / "data"


def find_model(name: str) -> Optional[Path]:
    """Locate one model file: runtime copy first, bundled seed second.

    Returns None when neither exists, which is a legitimate state -- the tracker
    runs pure physics and says so.
    """
    rt = runtime_data_dir() / name
    if rt.exists():
        return rt
    seed = bundled_data_dir()
    if seed is not None:
        cand = seed / name
        if cand.exists():
            return cand
    return None


def describe() -> dict:
    """Diagnostic snapshot for the health endpoint / startup log.

    The whole point of this module is that a missing model used to be invisible,
    so make the resolution inspectable rather than something to infer from
    behaviour.
    """
    out = {
        "frozen": is_frozen(),
        "runtime_dir": str(runtime_data_dir()),
        "bundled_dir": str(bundled_data_dir()) if bundled_data_dir() else None,
        "models": {},
    }
    for n in MODEL_NAMES:
        p = find_model(n)
        out["models"][n] = {
            "found": p is not None,
            "path": str(p) if p else None,
            "source": (
                None if p is None
                else "runtime" if p.parent == runtime_data_dir() else "bundled"
            ),
        }
    return out
