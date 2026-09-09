"""The background environment a storm cell is sitting in, sampled per cell.

WHY THIS EXISTS
---------------
The storm classifier has been radar-only: reflectivity structure, dual-pol,
rotation, trends. Nothing about the atmosphere around the storm. So a 55 dBZ
core with a 20 m/s couplet looked identical to the model whether it sat in
0 m2/s2 of storm-relative helicity or 300, and whether the air above it could
support a supercell or not.

That is the single largest gap between this and how the question is actually
answered in the literature. Thompson et al. (2012) find that convective mode
and tornado potential come from environment AND storm structure together --
"a diagnostic recipe that combines storm environment (i.e., large values of
effective-layer STP) and convective mode (RM) with mesocyclone strength" -- and
the operational systems built on that idea (ProbSevere and its descendants)
feed near-storm environment fields alongside the radar object.

The environment grids already exist in this process, hourly, on a background
now valid to within minutes (see mesoanalysis_service). This module is the
join: cell lat/lon -> the parameter values at that point.

TWO THINGS TO KNOW BEFORE USING THE VALUES
------------------------------------------
1. ABSENT IS NaN, NEVER 0.  Zero CAPE is a real, meaningful atmosphere; "the
   RAP grid had not downloaded" is not. The trainer's HistGradientBoosting
   learns a default branch direction per split, so NaN is representable and
   costs nothing -- but a 0.0 standing in for "unknown" is confidently wrong
   data, which is worse than missing. This is the same mistake that once told
   the model 391,471 ordinary storms had debris-ball correlation coefficients.

2. THE HISTORICAL ARCHIVE HAS NONE OF THIS.  The mesoanalysis service holds
   only recent cycles, so rows collected before this module existed carry NaN
   for every environment feature and always will, unless someone backfills
   archived RAP hour by hour. That is fine and expected -- the model uses these
   where present -- but it means the environment features earn their keep only
   as new data accumulates, and an ablation run today will show them doing
   almost nothing.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# The parameters sampled per cell, in the order they appear in the feature
# vector. Chosen as the ones that actually discriminate in the literature
# rather than everything the analysis happens to carry:
#
#   mlcape/mucape  buoyancy, the fuel
#   mlcin          the lid -- the difference between a threat and a nice day
#   shear06        deep-layer shear: organisation, supercell vs pulse
#   srh01/efhl     low-level and effective storm-relative helicity: rotation
#   mllcl          cloud base -- low LCLs favour tornadoes (Thompson 2003)
#   stp/scp        the composites, i.e. the published combinations
#   ship           significant hail parameter
#   lapse75        mid-level lapse rate: hail growth and downdraft strength
#   pwat           moisture depth: heavy rain / flash flood, and hail melting
ENV_FIELDS: tuple[str, ...] = (
    "mlcape", "mucape", "mlcin", "shear06", "srh01", "efhl",
    "mllcl", "stp", "scp", "ship", "lapse75", "pwat",
)

# Feature names as the model sees them. Prefixed so they can never collide with
# a radar feature and so their provenance is obvious in an importance table.
ENV_FEATURE_NAMES: tuple[str, ...] = tuple(f"env_{f}" for f in ENV_FIELDS)

# How stale an analysis may be before its values stop describing the air the
# storm is in. The background is hourly and now typically <30 min old; three
# hours is generous but still bounded, and past that NaN is the honest answer.
MAX_ANALYSIS_AGE_S = 3 * 3600.0

# Re-resolving which cycle is current, and re-sampling the grids, is wasted work
# inside one radar volume -- every cell in a scan shares the same environment.
_CACHE_TTL_S = 120.0


class _EnvCache:
    """One resolved analysis grid set, reused across the cells of a scan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._grids: Optional[dict] = None
        self._lats: Optional[np.ndarray] = None
        self._lons: Optional[np.ndarray] = None
        self._run: Optional[str] = None
        self._valid_iso: Optional[str] = None
        self._failed_logged = False

    def get(self):
        now = time.time()
        with self._lock:
            if self._grids is not None and (now - self._at) < _CACHE_TTL_S:
                return self._grids, self._lats, self._lons, self._run, self._valid_iso
        grids = lats = lons = run = valid = None
        try:
            from .mesoanalysis_service import get_mesoanalysis_service, analysis_axes
            svc = get_mesoanalysis_service()
            meta = svc.analysis()
            if meta:
                run = meta.get("run")
                valid = meta.get("valid_time")
                fresh = True
                if valid:
                    from datetime import datetime, timezone
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(valid)).total_seconds()
                    fresh = age <= MAX_ANALYSIS_AGE_S
                    if not fresh:
                        logger.info(
                            "storm environment: newest analysis is %.0f min old "
                            "(> %.0f), leaving environment features unset",
                            age / 60.0, MAX_ANALYSIS_AGE_S / 60.0)
                if fresh:
                    g = svc.grids(run)
                    if g:
                        grids = svc._resolve(g) if hasattr(svc, "_resolve") else g
                        lats, lons = analysis_axes()
        except Exception as e:
            # Never let the environment take the tracker down: a storm cell with
            # no environment is degraded, a crashed scan is an outage.
            if not self._failed_logged:
                self._failed_logged = True
                logger.warning(
                    "storm environment unavailable (%s: %s) - env_* features "
                    "will be NaN. Logged once per process.",
                    type(e).__name__, e)
        with self._lock:
            self._at = now
            self._grids, self._lats, self._lons = grids, lats, lons
            self._run, self._valid_iso = run, valid
        return grids, lats, lons, run, valid


_cache = _EnvCache()


def _sample(grid: np.ndarray, lats: np.ndarray, lons: np.ndarray,
            lat: float, lon: float) -> float:
    """Nearest-cell value, or NaN when off-grid or not finite.

    Nearest rather than bilinear on purpose. The analysis grid is ~15 km after
    striding and these fields are already an objective analysis -- interpolating
    between cells would imply a precision the source does not have, and the
    storm itself is several cells across.
    """
    if grid is None or lats is None or lons is None:
        return float("nan")
    r = int(np.argmin(np.abs(lats - lat)))
    c = int(np.argmin(np.abs(lons - lon)))
    # argmin always returns something, so verify the point is actually ON the
    # grid rather than nearest to an edge from far outside it.
    if abs(float(lats[r]) - lat) > 1.0 or abs(float(lons[c]) - lon) > 1.0:
        return float("nan")
    try:
        v = float(grid[r, c])
    except (IndexError, TypeError, ValueError):
        return float("nan")
    return v if math.isfinite(v) else float("nan")


def environment_at(lat: float, lon: float) -> dict[str, float]:
    """Environment parameters at a point, keyed by ENV_FEATURE_NAMES.

    Always returns every key. Missing values are NaN, never 0.0 -- see the
    module docstring for why that distinction is load-bearing.
    """
    out = {name: float("nan") for name in ENV_FEATURE_NAMES}
    if lat is None or lon is None:
        return out
    grids, lats, lons, _run, _valid = _cache.get()
    if not grids:
        return out
    for field, name in zip(ENV_FIELDS, ENV_FEATURE_NAMES):
        out[name] = _sample(grids.get(field), lats, lons, float(lat), float(lon))
    return out


def environment_source() -> dict:
    """Which analysis the values came from -- for the health panel and the QA log."""
    _grids, _lats, _lons, run, valid = _cache.get()
    return {"run": run, "valid_time": valid, "available": _grids is not None}
