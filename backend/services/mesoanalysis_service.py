"""RAP mesoanalysis — objective severe-weather threat assessment on the hourly
RAP analysis (f00).

Ported from TheBattinFront's Raspberry Pi RTMA service. The METEOROLOGY is kept
(multi-pathway ingredients overlap, per-level area thresholds, CIN gating,
connected-component threat clusters, cycle-over-cycle trends); the
INFRASTRUCTURE is replaced:

  * Parameter grids come from the shared RAP field service — AWS byte-range
    reads of the same rap.tHHz.awp130pgrbf00.grib2 the Pi pulled from NOMADS,
    already regridded and cached. We call `_field_grid` (not `get_field`) to get
    NATIVE FLOATS: the packed uint8 wire format quantizes STP to 0.04 and SCP to
    0.2, which would band the threat masks right at the 1.0/4.0 thresholds, and
    its byte 0 conflates "missing" with "below the display floor".
  * The land mask is a rasterized US-states polygon set instead of lat/lon
    boxes. The box mask wrote off Houston, Austin, New Orleans, Mobile, Tampa
    and Orlando as "Gulf of Mexico", and Detroit, Flint and Grand Rapids as
    "Great Lakes" — those areas could never register a threat.
  * Threat zones ship as GeoJSON polygons, not full-grid integer masks.
  * The whole assessment is computed ONCE per RAP cycle and cached. The Pi
    re-ran it, in full, on every /analysis and /threat-zones request.
  * Missing data stays NaN. The Pi wrote NaN → 0 into its cache, and 0 is the
    maximally FAVORABLE value for CIN ("uninhibited") and LCL ("surface-based"),
    so every missing cell silently passed those ingredient tests.

Two pathway inputs changed because they were the Pi's weakest links. Its QLCS
pathways keyed on 0-1 km and 0-3 km bulk shear, both of which it approximated as
a 10 m → 850/700 mb wind difference; pressure levels are not fixed heights AGL,
so those fields were biased and terrain-dependent. RAP publishes no 0-1/0-3 km
shear, but it does publish EFHL (effective-layer SRH) and true 0-3 km CAPE, which
are the better discriminators for organized low-level rotation and QLCS mesovortex
tornadoes anyway. Those replace the approximated shear terms.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from .hrrr_field_service import MODELS, T_N, T_NI, T_NJ, T_RES, T_W, get_hrrr_field_service

logger = logging.getLogger(__name__)

MODEL = "rap"
FHOUR = 0  # f00 = the analysis

# Subsample the 0.035° display grid to ~0.14° for the assessment. The threat math
# is an area-overlap question, not a rendering one — at full resolution it is 28×
# the cells for no extra skill, and 0.14° is about where the Pi ran (0.15°).
STRIDE = 4

# Parameter grids pulled per cycle. Keys are RAP field ids from the field service.
MESO_FIELDS = (
    "sbcape", "mlcape", "mucape", "sbcin", "mlcin", "cape03",
    "srh01", "srh03", "efhl", "shear06",
    "stp", "scp", "ship", "mllcl", "lapse75",
    "pwat", "t2m", "td2m", "mslp", "thetae", "lftx4", "wspd850",
)
# Without these the assessment is meaningless — a cycle missing any of them is
# treated as unavailable and the caller falls back a cycle.
CORE_FIELDS = ("sbcape", "mlcape", "shear06", "srh01", "stp")

CACHE_RUNS = 4          # analyses kept in memory (current + a few for trends)
GRID_CACHE_RUNS = 3

# ── Threat thresholds ───────────────────────────────────────────────────────
# Each level holds a list of PATHWAYS (storm modes). A pathway fires only where
# EVERY one of its ingredients is met at the same grid cell; the level fires when
# the UNION of its qualifying pathways covers enough area. Values follow Thompson
# et al. (2012) parameter spaces, read against SPC-convention STP/SCP/SHIP — which
# is what the field service now computes (its shear terms are capped at 1.5 per
# SPC, and SHIP is the real index, not a CAPE×shear proxy).
#
# "_cape" resolves at runtime to max(MLCAPE, SBCAPE) so warm-sector setups where
# SBCAPE >> MLCAPE are not missed.
# CIN keys test ">= threshold" (less inhibited); LCL keys test "<= threshold".
THREAT_THRESHOLDS: dict[str, dict[str, list[dict]]] = {
    "tornado": {
        "marginal": [
            {"stp": 1.0, "srh01": 100, "_cape": 500, "shear06": 30, "mode": "discrete"},
            # QLCS/HSLC mesovortex: modest buoyancy, but real low-level rotation
            # (0-1 km SRH + effective SRH) and genuine low-level CAPE.
            {"mlcape": 250, "mlcin": -150, "srh01": 75, "efhl": 100,
             "cape03": 25, "shear06": 35, "mode": "qlcs"},
        ],
        "moderate": [
            {"stp": 2.0, "srh01": 150, "_cape": 1000, "shear06": 35, "mllcl": 1500,
             "mode": "discrete"},
            {"mlcape": 500, "mlcin": -100, "srh01": 100, "efhl": 150,
             "cape03": 50, "shear06": 40, "mode": "qlcs"},
        ],
        "high": [
            {"stp": 4.0, "srh01": 200, "_cape": 1500, "shear06": 40, "mllcl": 1000,
             "mode": "discrete"},
        ],
        "extreme": [
            {"stp": 8.0, "srh01": 300, "_cape": 2000, "shear06": 50, "mode": "discrete"},
        ],
    },
    "supercell": {
        "marginal": [{"scp": 1.0, "_cape": 500, "shear06": 30, "mode": "discrete"}],
        "moderate": [{"scp": 4.0, "_cape": 1500, "shear06": 40, "mode": "discrete"}],
        "high": [{"scp": 8.0, "_cape": 2500, "shear06": 50, "mode": "discrete"}],
        "extreme": [{"scp": 12.0, "_cape": 3500, "shear06": 60, "mode": "discrete"}],
    },
    "hail": {
        # SHIP is now the real SPC index, so its published interpretation applies:
        # ~1.0 → 2" potential, 1.5+ → significant, 4+ → giant.
        "marginal": [
            {"ship": 0.5, "_cape": 800, "shear06": 30, "mode": "supercell"},
            {"_cape": 2500, "lapse75": 6.5, "mode": "pulse"},
        ],
        "moderate": [
            {"ship": 1.0, "_cape": 1500, "shear06": 40, "lapse75": 7.0, "mode": "supercell"},
            {"_cape": 3500, "lapse75": 7.0, "mode": "pulse"},
        ],
        "high": [
            {"ship": 2.0, "_cape": 2500, "shear06": 45, "lapse75": 7.5, "mode": "supercell"},
        ],
        "extreme": [
            {"ship": 4.0, "_cape": 3500, "shear06": 55, "lapse75": 7.5, "mode": "supercell"},
        ],
    },
    "damaging_wind": {
        "marginal": [
            {"mlcape": 500, "mlcin": -150, "shear06": 30, "mode": "qlcs"},
            # Cool-season / HSLC bowing segments: little buoyancy, lots of shear.
            {"mlcape": 200, "cape03": 25, "shear06": 45, "mode": "qlcs"},
            {"_cape": 2000, "lapse75": 6.5, "mode": "pulse"},
            # Momentum transfer — mid-level flow mixing down through a deep,
            # well-mixed boundary layer. 700 mb (~10 kft) is a realistic mixing
            # depth; 500 mb is jet level and never reaches the surface.
            {"_cape": 1000, "wspd850": 40, "mode": "any"},
        ],
        "moderate": [
            {"mlcape": 1500, "mlcin": -100, "shear06": 35, "mode": "qlcs"},
            {"mlcape": 500, "cape03": 50, "shear06": 50, "mode": "qlcs"},
            {"_cape": 3000, "lapse75": 7.0, "mode": "pulse"},
        ],
        "high": [
            {"mlcape": 2500, "mlcin": -75, "cape03": 75, "shear06": 45, "mode": "qlcs"},
            {"_cape": 4000, "lapse75": 7.5, "mode": "pulse"},
        ],
    },
    "flash_flood": {
        "marginal": [{"pwat": 1.5, "mlcape": 750, "mlcin": -100, "mode": "any"}],
        "moderate": [{"pwat": 2.0, "mlcape": 1000, "mlcin": -75, "mode": "any"}],
        "high": [{"pwat": 2.5, "mlcape": 1500, "mlcin": -50, "mode": "any"}],
    },
}

# Minimum share of CONUS land a level must cover, cos(lat)-weighted. Deliberately
# LOW and DECREASING with severity: higher-end threats are usually MORE focused
# (a violent discrete supercell covers a few counties; a marginal squall line
# covers several states), so requiring more area for a higher level would
# systematically downgrade exactly the setups that matter most.
THREAT_AREA_THRESHOLDS = {
    "marginal": 0.0015,
    "moderate": 0.0012,
    "high": 0.0009,
    "extreme": 0.0006,
}

THREAT_MESSAGES = {
    "tornado": {
        "marginal": "Isolated tornado threat. Low-end parameters present. Monitor for mesocyclone development.",
        "moderate": "Tornado threat exists. Sufficient CAPE, SRH and shear for supercell tornadoes.",
        "high": "Significant tornado threat. Strong parameters support long-track, potentially violent tornadoes.",
        "extreme": "EXTREME tornado environment. Parameters rival historic outbreaks.",
    },
    "supercell": {
        "marginal": "Isolated supercell potential. Marginal instability and shear present.",
        "moderate": "Supercell storms likely. Sufficient CAPE and deep-layer shear for organized convection.",
        "high": "Intense supercells expected. Strong instability and shear favor long-lived, severe-producing storms.",
        "extreme": "Extreme supercell environment. Discrete, long-lived supercells with all hazards likely.",
    },
    "hail": {
        "marginal": "Isolated large hail possible with any storms that develop.",
        "moderate": "Large hail (1-2 inch) likely with organized convection.",
        "high": "Very large hail (2+ inches) likely. Significant hail threat with supercells.",
        "extreme": "Giant hail (3+ inches) possible. Extreme SHIP values detected.",
    },
    "damaging_wind": {
        "marginal": "Isolated damaging wind gusts possible.",
        "moderate": "Widespread damaging winds possible. Moderate instability with sufficient shear.",
        "high": "Significant wind damage threat. Strong instability and shear support bowing structures.",
    },
    "flash_flood": {
        "marginal": "Locally heavy rain possible. Elevated PWAT detected.",
        "moderate": "Flash flooding possible. High PWAT and CAPE support training thunderstorms.",
        "high": "Significant flash flood threat. Extreme moisture and instability present.",
    },
}

THREAT_TYPES = ("tornado", "supercell", "hail", "damaging_wind", "flash_flood")
LEVELS = ("none", "marginal", "moderate", "high", "extreme")

MODE_LABELS = {"discrete": "Discrete Supercell", "qlcs": "QLCS/Squall Line",
               "pulse": "Pulse Storm", "supercell": "Supercell", "any": ""}

# Trend tracking: floors below which a parameter is not meteorologically
# interesting (avoids "CAPE up 400%" when it went from 5 to 25 J/kg).
TREND_MIN = {
    "sbcape": 500, "mlcape": 500, "mucape": 500, "srh01": 50, "srh03": 75,
    "shear06": 20, "stp": 0.5, "scp": 0.5, "ship": 0.5, "pwat": 0.5,
    "td2m": 40, "mllcl": 500, "cape03": 25, "efhl": 50,
}
TREND_WEIGHTS = {"stp": 2, "scp": 2, "ship": 2}
TREND_PARAMS = tuple(TREND_MIN.keys())

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# backend/data is what the PyInstaller spec bundles, so the frozen build reads
# the copy there. The frontend path is the dev-checkout fallback: backend/data is
# gitignored, so a fresh clone may only have the (tracked) frontend original.
_STATES_CANDIDATES = (
    os.path.join(_BACKEND_DIR, "data", "us_states.json"),
    os.path.join(os.path.dirname(_BACKEND_DIR), "frontend", "src", "data", "us-states.json"),
)


def _states_path() -> Optional[str]:
    for p in _STATES_CANDIDATES:
        if os.path.exists(p):
            return p
    return None
# Territories / non-CONUS states are dropped: outside the RAP CONUS domain and
# they would only add spurious land area to the denominator.
_SKIP_STATES = {"Alaska", "Hawaii", "Puerto Rico"}


def analysis_axes() -> tuple[np.ndarray, np.ndarray]:
    """(lats N→S, lons W→E) cell centres of the coarsened analysis grid."""
    rows = np.arange(0, T_NJ, STRIDE)
    cols = np.arange(0, T_NI, STRIDE)
    return (T_N - (rows + 0.5) * T_RES), (T_W + (cols + 0.5) * T_RES)


class MesoanalysisService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grids: dict[str, dict[str, np.ndarray]] = {}
        self._analyses: dict[str, dict] = {}
        # Threat + watch masks kept alongside each cached analysis so `zones()`
        # is a polygonize of an existing result, not a second full assessment.
        self._masks: dict[str, dict[str, np.ndarray]] = {}
        self._land: Optional[np.ndarray] = None
        self._area_w: Optional[np.ndarray] = None
        self._runs_cache: tuple[float, list[str]] = (0.0, [])

    # ── Grid geometry / land mask ──────────────────────────────────────────
    @property
    def land_mask(self) -> np.ndarray:
        """Boolean CONUS-land mask on the analysis grid (True = land)."""
        if self._land is not None:
            return self._land
        with self._lock:
            if self._land is not None:
                return self._land
            lats, lons = analysis_axes()
            cache = os.path.join(_BACKEND_DIR, "data",
                                 f"_landmask_{lons.size}x{lats.size}.npy")
            mask = None
            if os.path.exists(cache):
                try:
                    m = np.load(cache)
                    if m.shape == (lats.size, lons.size):
                        mask = m.astype(bool)
                except Exception:
                    mask = None
            if mask is None:
                mask = self._rasterize_states(lats, lons)
                try:
                    np.save(cache, mask)
                except Exception:
                    pass  # read-only install → recompute each boot (~1 s)
            self._land = mask
            return mask

    @staticmethod
    def _rasterize_states(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        """Point-in-polygon the US state outlines onto the analysis grid.

        The Great Lakes need no special handling: the state polygons follow their
        shorelines, so lake cells simply fall inside no polygon."""
        from matplotlib.path import Path

        lon_g, lat_g = np.meshgrid(lons, lats)
        pts = np.column_stack([lon_g.ravel(), lat_g.ravel()])
        mask = np.zeros(pts.shape[0], dtype=bool)
        path = _states_path()
        try:
            if path is None:
                raise FileNotFoundError(f"none of {_STATES_CANDIDATES}")
            with open(path, "r", encoding="utf-8") as f:
                gj = json.load(f)
        except Exception as e:
            # Falling back to "everything is land" keeps the service running but
            # skews every coverage fraction (ocean and Canada join the denominator
            # and can host threats), so this is loud on purpose.
            logger.error("mesoanalysis: state outlines unreadable (%s) — "
                         "land mask DISABLED, threat areas will be unreliable", e)
            return np.ones((lats.size, lons.size), dtype=bool)

        for feat in gj.get("features", []):
            if (feat.get("properties") or {}).get("name") in _SKIP_STATES:
                continue
            geom = feat.get("geometry") or {}
            polys = ([geom.get("coordinates")] if geom.get("type") == "Polygon"
                     else geom.get("coordinates") or [])
            for poly in polys:
                if not poly:
                    continue
                ring = np.asarray(poly[0], dtype=float)
                if ring.ndim != 2 or ring.shape[0] < 4:
                    continue
                # Bounding-box prefilter — most cells are outside most states.
                x0, y0 = ring[:, 0].min(), ring[:, 1].min()
                x1, y1 = ring[:, 0].max(), ring[:, 1].max()
                cand = (~mask & (pts[:, 0] >= x0) & (pts[:, 0] <= x1)
                        & (pts[:, 1] >= y0) & (pts[:, 1] <= y1))
                if not cand.any():
                    continue
                inside = Path(ring).contains_points(pts[cand])
                idx = np.flatnonzero(cand)
                mask[idx[inside]] = True
        return mask.reshape(lats.size, lons.size)

    @property
    def area_weights(self) -> np.ndarray:
        """cos(lat) cell weights — a cell at 49°N covers ~30% less ground than one
        at 25°N, so an unweighted cell count skews northern threats large."""
        if self._area_w is None:
            lats, lons = analysis_axes()
            self._area_w = np.cos(np.radians(lats))[:, None] * np.ones((1, lons.size))
        return self._area_w

    def _frac(self, mask: np.ndarray) -> float:
        w = self.area_weights
        denom = float(np.sum(w * self.land_mask))
        return float(np.sum(w * mask)) / denom if denom > 0 else 0.0

    # ── Cycles ─────────────────────────────────────────────────────────────
    def latest_runs(self, limit: int = 4) -> list[str]:
        """Newest RAP cycles that have an analysis available (short TTL cache)."""
        now = time.time()
        if now - self._runs_cache[0] < 120 and self._runs_cache[1]:
            return self._runs_cache[1][:limit]
        svc = get_hrrr_field_service()
        runs = [r["run"] for r in svc.list_runs(MODEL, limit=max(limit, 4))]
        self._runs_cache = (now, runs)
        return runs[:limit]

    @staticmethod
    def _run_iso(run: str) -> str:
        dt = datetime(int(run[:4]), int(run[4:6]), int(run[6:8]), int(run[8:10]),
                      tzinfo=timezone.utc)
        return dt.isoformat()

    # ── Parameter grids ────────────────────────────────────────────────────
    def grids(self, run: str) -> Optional[dict[str, np.ndarray]]:
        """All mesoanalysis parameter grids for a cycle, coarsened. Cached per run."""
        with self._lock:
            if run in self._grids:
                return self._grids[run]
        svc = get_hrrr_field_service()
        # One cheap up-front check: a cycle that is still uploading has no .idx,
        # and without this every one of the fields below would separately fail
        # with NoSuchKey before we gave up on the cycle.
        try:
            key = svc._key(MODEL, run[:8], int(run[8:10]), FHOUR,
                           MODELS[MODEL]["default_file"])
            svc._read_idx(MODEL, key)
        except Exception as e:
            logger.debug("meso: cycle %s not available yet (%s)", run, e)
            return None

        out: dict[str, np.ndarray] = {}
        for fid in MESO_FIELDS:
            try:
                g = svc._field_grid(MODEL, run, fid, FHOUR)
            except Exception as e:
                logger.warning("meso: %s %s failed (%s)", run, fid, e)
                continue
            if g is None:
                logger.debug("meso: %s %s returned no grid", run, fid)
                continue
            out[fid] = np.asarray(g[::STRIDE, ::STRIDE], dtype=np.float32)
        missing = [f for f in CORE_FIELDS if f not in out]
        if missing:
            logger.warning("meso: cycle %s missing core fields %s", run, missing)
            return None
        with self._lock:
            self._grids[run] = out
            while len(self._grids) > GRID_CACHE_RUNS:
                self._grids.pop(next(iter(self._grids)))
        return out

    # ── Threat assessment ──────────────────────────────────────────────────
    @staticmethod
    def _resolve(p: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        r = dict(p)
        if "mlcape" in p and "sbcape" in p:
            r["_cape"] = np.fmax(p["mlcape"], p["sbcape"])
        elif "mlcape" in p:
            r["_cape"] = p["mlcape"]
        elif "sbcape" in p:
            r["_cape"] = p["sbcape"]
        return r

    def _pathway(self, pathway: dict, params: dict[str, np.ndarray]):
        """(mask, mode) for one pathway, or None if any ingredient is unavailable.

        EVERY ingredient must be present. Dropping a missing one and firing on the
        rest lets a threat trigger without its defining ingredient — a flash flood
        with no PWAT, a tornado pathway with no STP."""
        mode = pathway.get("mode", "any")
        mask = None
        for name, thresh in pathway.items():
            if name == "mode":
                continue
            grid = params.get(name)
            if grid is None:
                return None
            if "cin" in name:
                ok = grid >= thresh          # less inhibited is better
            elif "lcl" in name:
                ok = grid <= thresh          # lower cloud base is better
            elif thresh < 0:
                ok = grid <= thresh
            else:
                ok = grid >= thresh
            # NaN compares False everywhere above, so missing data can never
            # satisfy an ingredient — including the "lower is better" ones.
            ok = ok & np.isfinite(grid)
            mask = ok if mask is None else (mask & ok)
        if mask is None:
            return None
        return mask & self.land_mask, mode

    def _evaluate(self, threat: str, params: dict[str, np.ndarray]) -> dict:
        thresholds = THREAT_THRESHOLDS.get(threat, {})
        for level in ("extreme", "high", "moderate", "marginal"):
            if level not in thresholds:
                continue
            union = None
            modes: list[str] = []
            best_frac, best_mode = 0.0, None
            for pathway in thresholds[level]:
                res = self._pathway(pathway, params)
                if res is None:
                    continue
                mask, mode = res
                frac = self._frac(mask)
                if frac <= 0:
                    continue
                # Union every contributing pathway so a discrete-supercell threat
                # in one region and a QLCS threat in another BOTH render, rather
                # than only the single largest.
                union = mask if union is None else (union | mask)
                if mode not in modes:
                    modes.append(mode)
                if frac > best_frac:
                    best_frac, best_mode = frac, mode
            if union is None:
                continue
            frac = self._frac(union)
            if frac >= THREAT_AREA_THRESHOLDS.get(level, 0.01):
                return {
                    "level": level, "mode": best_mode, "modes": modes,
                    "coverage": round(frac, 5),
                    "details": THREAT_MESSAGES.get(threat, {}).get(level, "Threat detected."),
                    "_mask": union,
                }
        return {"level": "none", "mode": None, "modes": [], "coverage": 0.0,
                "details": f"No significant {threat.replace('_', ' ')} threat detected.",
                "_mask": None}

    def _clusters(self, mask: np.ndarray, max_clusters: int = 6, min_cells: int = 4) -> list[dict]:
        """One labelled centre per contiguous threat region.

        A single global-max point dropped every label on one spot (usually the
        Plains, where raw values peak) even when the threat spanned several
        disconnected areas."""
        if mask is None or not mask.any():
            return []
        from scipy import ndimage
        lats, lons = analysis_axes()
        labeled, n = ndimage.label(mask)
        out = []
        for lab in range(1, n + 1):
            cells = labeled == lab
            size = int(cells.sum())
            if size < min_cells:
                continue  # single-cell speckle
            rows, cols = np.where(cells)
            out.append({
                "lat": round(float(lats[int(round(rows.mean()))]), 3),
                "lon": round(float(lons[int(round(cols.mean()))]), 3),
                "cells": size,
            })
        out.sort(key=lambda c: c["cells"], reverse=True)
        return out[:max_clusters]

    def _assess(self, p: dict[str, np.ndarray]) -> tuple[dict, dict[str, np.ndarray]]:
        params = self._resolve(p)
        # Strong surface-based inhibition is a brick wall for SURFACE-BASED
        # convection only. Elevated storms — nocturnal hail producers, training
        # flash-flood convection — routinely thrive over large SBCIN, so gating
        # those would erase real threats.
        capped = None
        if "sbcin" in params:
            capped = np.isfinite(params["sbcin"]) & (params["sbcin"] < -125)
        cin_gated = {"tornado", "damaging_wind"}
        floor = THREAT_AREA_THRESHOLDS["marginal"]

        threats: dict = {}
        masks: dict[str, np.ndarray] = {}
        combined = None
        for t in THREAT_TYPES:
            res = self._evaluate(t, params)
            mask = res.pop("_mask", None)
            if res["level"] != "none" and mask is not None and capped is not None and t in cin_gated:
                mask = mask & (~capped)
                frac = self._frac(mask)
                res["coverage"] = round(frac, 5)
                # Re-validate against the area floor rather than only killing the
                # threat when literally zero cells survive.
                if frac < floor:
                    res.update({"level": "none", "mode": None, "modes": [],
                                "details": "Threat suppressed by strong surface-based "
                                           "convective inhibition (CIN)."})
                    mask = None
            if res["level"] != "none" and mask is not None:
                res["centers"] = self._clusters(mask)
                masks[t] = mask
                combined = mask if combined is None else (combined | mask)
            else:
                res["centers"] = []
            threats[t] = res

        top, primary = 0, None
        for name, t in threats.items():
            i = LEVELS.index(t["level"])
            if i > top:
                top, primary = i, name
        threats["overall"] = {
            "level": LEVELS[top],
            "primary_threat": primary,
            "summary": self._summary(threats, params, combined),
        }
        return threats, masks

    @staticmethod
    def _summary(threats: dict, params: dict, zone: Optional[np.ndarray]) -> str:
        active = []
        for name, d in threats.items():
            if name == "overall" or d.get("level", "none") == "none":
                continue
            label = name.replace("_", " ").title()
            ml = MODE_LABELS.get(d.get("mode"), "")
            active.append(f"{label}: {d['level'].upper()}" + (f" ({ml})" if ml else ""))
        if not active:
            return "No significant severe weather threats detected in the analysis domain."

        def peak(key, agg=np.nanmax):
            g = params.get(key)
            if g is None:
                return None
            sel = g[zone] if (zone is not None and zone.any()) else g
            sel = sel[np.isfinite(sel)]
            return float(agg(sel)) if sel.size else None

        extras = []
        # Reported WITHIN the threat footprint, so the numbers describe the actual
        # threat area rather than whatever corner of CONUS happens to peak.
        for key, fmt, floor in (("mlcape", "Max MLCAPE: {:.0f} J/kg", 100),
                                ("shear06", "Max 0-6 km shear: {:.0f} kt", 20),
                                ("stp", "Max STP: {:.1f}", 0.5),
                                ("scp", "Max SCP: {:.1f}", 1.0),
                                ("ship", "Max SHIP: {:.1f}", 0.5),
                                ("efhl", "Max effective SRH: {:.0f} m²/s²", 50),
                                ("lapse75", "Max 700-500 mb lapse: {:.1f} °C/km", 6.0)):
            v = peak(key)
            if v is not None and v > floor:
                extras.append(fmt.format(v))
        v = peak("mllcl", np.nanmin)
        if v is not None and v < 1500:
            extras.append(f"Min LCL: {v:.0f} m")
        return "Active threats: " + "; ".join(active) + ". " + " | ".join(extras)

    # ── Watch areas ────────────────────────────────────────────────────────
    def _watch(self, p: dict[str, np.ndarray]) -> Optional[np.ndarray]:
        """Where ingredients are COMING TOGETHER but have not reached a threat
        level — instability is mandatory (no CAPE, no storms), plus at least one
        kinematic ingredient."""
        mlcape = p.get("mlcape")
        if mlcape is None:
            return None
        inst = np.isfinite(mlcape) & (mlcape >= 500)
        shear = p.get("shear06")
        if shear is not None:
            # HSLC: accept much lower CAPE when deep-layer shear is extreme —
            # cool-season QLCS events often run 200-400 J/kg under 60+ kt.
            inst = inst | (np.isfinite(shear) & (shear >= 50) & (mlcape >= 200))
        kin = np.zeros_like(inst)
        if shear is not None:
            kin = kin | (np.isfinite(shear) & (shear >= 25))
        for key, thr in (("srh03", 150), ("srh01", 75), ("efhl", 100)):
            g = p.get(key)
            if g is not None:
                kin = kin | (np.isfinite(g) & (g >= thr))
        watch = inst & kin & self.land_mask
        # Suppress anywhere already at a full threat level — that renders as a
        # threat zone, and two overlapping shades read as noise.
        for key, thr in (("stp", 1.0), ("scp", 1.0)):
            g = p.get(key)
            if g is not None:
                watch = watch & ~(np.isfinite(g) & (g >= thr))
        return watch if watch.any() else None

    # ── Trends ─────────────────────────────────────────────────────────────
    def _trends(self, cur: dict, prev: Optional[dict], zone: Optional[np.ndarray]) -> dict:
        if not prev:
            return {"available": False, "message": "No previous cycle for trend comparison."}
        # Trends are computed INSIDE the active threat footprint when there is
        # one. A CONUS-wide percentile answers "is the continent destabilizing",
        # which is not the question an operator is asking.
        sel = zone if (zone is not None and zone.any()) else self.land_mask
        out: dict = {"available": True, "scope": "threat_area" if zone is not None and zone.any() else "conus",
                     "parameters": {}}
        inc = dec = total = 0
        for key in TREND_PARAMS:
            a, b = cur.get(key), prev.get(key)
            if a is None or b is None or a.shape != b.shape:
                continue
            av, bv = a[sel], b[sel]
            av, bv = av[np.isfinite(av)], bv[np.isfinite(bv)]
            if av.size == 0 or bv.size == 0:
                continue
            # 90th percentile, not max — one spurious cell should not define a trend.
            p1, p0 = float(np.percentile(av, 90)), float(np.percentile(bv, 90))
            floor = TREND_MIN.get(key, 0)
            if abs(p1) < floor and abs(p0) < floor:
                out["parameters"][key] = {"direction": "steady", "current": round(p1, 1),
                                          "previous": round(p0, 1), "pct_change": 0.0}
                continue
            if abs(p0) > 0.01:
                pct = (p1 - p0) / abs(p0) * 100.0
            else:
                pct = 100.0 if abs(p1) > 0.01 else 0.0
            direction = "increasing" if pct > 30 else "decreasing" if pct < -30 else "steady"
            out["parameters"][key] = {"direction": direction, "current": round(p1, 1),
                                      "previous": round(p0, 1), "pct_change": round(pct, 1)}
            w = TREND_WEIGHTS.get(key, 1)
            total += w
            inc += w if direction == "increasing" else 0
            dec += w if direction == "decreasing" else 0
        if total and inc > dec * 2:
            out["overall"] = "DESTABILIZING — environment becoming more favorable for severe weather"
        elif total and dec > inc * 2:
            out["overall"] = "STABILIZING — environment becoming less favorable for severe weather"
        else:
            out["overall"] = "STEADY — no significant trend in severe weather parameters"
        return out

    # ── Public API ─────────────────────────────────────────────────────────
    def analysis(self, run: Optional[str] = None) -> Optional[dict]:
        """Threat assessment + trends for a cycle (defaults to the newest that has
        data). Cached per run — the Pi recomputed this on every request."""
        runs = self.latest_runs()
        if not runs:
            return None
        candidates = [run] if run else runs
        for r in candidates:
            with self._lock:
                if r in self._analyses:
                    return self._analyses[r]
            grids = self.grids(r)
            if grids is None:
                continue
            t0 = time.time()
            threats, masks = self._assess(grids)
            combined = None
            for m in masks.values():
                combined = m if combined is None else (combined | m)
            prev = None
            for pr in runs:
                if pr < r:
                    prev = self.grids(pr)
                    break
            watch = self._watch(grids)
            result = {
                "run": r,
                "valid_time": self._run_iso(r),
                "model": "RAP analysis (f00)",
                "threats": threats,
                "trends": self._trends(grids, prev, combined),
                "watch_coverage": round(self._frac(watch), 5) if watch is not None else 0.0,
                "has_zones": bool(masks) or watch is not None,
            }
            logger.info("mesoanalysis %s: %s (%.1fs)", r,
                        threats["overall"]["level"], time.time() - t0)
            zone_masks = dict(masks)
            if watch is not None:
                zone_masks["watch"] = watch
            with self._lock:
                self._analyses[r] = result
                self._masks[r] = zone_masks
                while len(self._analyses) > CACHE_RUNS:
                    old = next(iter(self._analyses))
                    self._analyses.pop(old)
                    self._masks.pop(old, None)
            return result
        return None

    def zones(self, run: Optional[str] = None) -> Optional[dict]:
        """Threat + watch areas as GeoJSON polygons.

        Polygons, not the Pi's full-grid integer masks: a mask was ~120k numbers
        per threat type on the wire, and the client then had to contour it anyway."""
        meta = self.analysis(run)
        if meta is None:
            return None
        with self._lock:
            masks = self._masks.get(meta["run"], {})
        feats = []
        for name, mask in masks.items():
            props = ({"kind": "watch", "threat": "watch", "level": "watch"}
                     if name == "watch" else
                     {"kind": "threat", "threat": name,
                      "level": meta["threats"].get(name, {}).get("level", "marginal")})
            feats += self._polygons(mask, props)
        return {"type": "FeatureCollection", "run": meta["run"],
                "valid_time": meta["valid_time"], "features": feats}

    def _polygons(self, mask: np.ndarray, props: dict) -> list[dict]:
        from contourpy import FillType, contour_generator
        from scipy.ndimage import binary_closing, binary_opening

        raw = mask.astype(bool)
        # Drop lone speckle cells and close 1-cell pinholes so the outlines read as
        # coherent areas instead of confetti. If cleaning erases the threat entirely
        # (a genuinely thin corridor), keep the raw mask — the analysis already said
        # this area qualifies, so it must not silently vanish from the map.
        m = binary_opening(raw, np.ones((2, 2)))
        if not m.any():
            m = raw
        m = binary_closing(m, np.ones((3, 3)))

        lats, lons = analysis_axes()
        # contourpy needs both axes increasing; our rows run N→S.
        z = m.astype(float)[::-1, :]
        ys = lats[::-1]
        cg = contour_generator(lons, ys, z, fill_type=FillType.OuterOffset)
        pts_list, offs_list = cg.filled(0.5, 1.5)
        feats = []
        for pts, offs in zip(pts_list, offs_list):
            rings = []
            for i in range(len(offs) - 1):
                ring = pts[offs[i]:offs[i + 1]]
                if len(ring) < 4:
                    continue
                coords = [[round(float(x), 3), round(float(y), 3)] for x, y in ring]
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                rings.append(coords)
            if not rings:
                continue
            feats.append({"type": "Feature",
                          "geometry": {"type": "Polygon", "coordinates": rings},
                          "properties": dict(props)})
        return feats

    def point(self, lat: float, lon: float, run: Optional[str] = None) -> Optional[dict]:
        """Every mesoanalysis parameter at a point — the 'what is the atmosphere
        doing right here' readout."""
        meta = self.analysis(run)
        if meta is None:
            return None
        grids = self.grids(meta["run"])
        if grids is None:
            return None
        lats, lons = analysis_axes()
        if not (lats.min() <= lat <= lats.max() and lons.min() <= lon <= lons.max()):
            return None
        r = int(np.argmin(np.abs(lats - lat)))
        c = int(np.argmin(np.abs(lons - lon)))
        vals = {}
        for k, g in grids.items():
            v = float(g[r, c])
            if math.isfinite(v):
                vals[k] = round(v, 2)
        # Threats AT THIS CELL, from the zone masks — not the national threat list.
        # Reporting the domain-wide levels here would tell an operator standing in
        # a capped, zero-CAPE airmass that they are under a HIGH supercell threat
        # because Minnesota is.
        with self._lock:
            masks = self._masks.get(meta["run"], {})
        active = []
        for t in THREAT_TYPES:
            m = masks.get(t)
            if m is not None and bool(m[r, c]):
                info = meta["threats"].get(t, {})
                active.append({"threat": t, "level": info.get("level"),
                               "mode": info.get("mode")})
        watch = masks.get("watch")
        return {"run": meta["run"], "valid_time": meta["valid_time"],
                "lat": round(float(lats[r]), 3), "lon": round(float(lons[c]), 3),
                "land": bool(self.land_mask[r, c]),
                "values": vals, "threats": active,
                "watch": bool(watch is not None and watch[r, c])}


_service: Optional[MesoanalysisService] = None


def get_mesoanalysis_service() -> MesoanalysisService:
    global _service
    if _service is None:
        _service = MesoanalysisService()
    return _service
