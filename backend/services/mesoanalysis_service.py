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
FHOUR = 0  # legacy default, used only when a caller passes a bare cycle string

# ── Where the analysis background comes from ───────────────────────────────
# THE SHORT VERSION: we do not wait for an F00.
#
# SPC's mesoanalysis is on screen at :15 past the hour and ours was ~90 minutes
# old, and the reason is architectural rather than a bug.  SPC does not use a
# model analysis at all: it uses the PREVIOUS cycle's short-range RAP FORECAST
# valid at the hour as a first guess, then objectively analyses surface
# observations onto it (Bothwell, Hart & Thompson 2002; Thompson et al. 2012
# restate it: "The RUC analyses at the lowest model level are used as a
# first-guess field in an objective analysis of the hourly surface
# observations").  A forecast valid at the top of the hour exists BEFORE the
# hour, so SPC never waits on a file.
#
# Measured on AWS, four consecutive cycles, 2026-09-08:
#   RAP F00 posts ~HH:49, valid HH:00  ->  age 49-109 min, mean ~79
#   RAP F01 posts ~HH:48, valid HH+1:00 -> age  0-60 min,  mean ~30
# The F01 of a cycle lands about twelve minutes BEFORE its own valid time.
# That single fact is the whole freshness fix.
#
# Not RRFS: measured the same day, its F00 posts ~75 min after valid AND the
# operational AWS bucket publishes a CONUS F00 only at 00/03/06/09/12/15/18/21Z
# -- 8 a day against RAP's 24 -- so the mean age of the newest RRFS analysis is
# ~165 min.  It would roughly double the staleness it was meant to fix.  Worth
# re-measuring after the 2026-10-06 operational implementation.
#
# Not HRRR (yet): HRRR F01 posts ~6 min before valid and would work just as
# well on freshness, but 8 of the fields below (mlcin, cape03, efhl, ship,
# mllcl, lapse75, mslp, thetae) are registered only for RAP in
# hrrr_field_service.  Seven of their idx strings match live HRRR exactly and
# only `thetae` (EPOT:surface) is genuinely absent -- but registering them in
# HRRR_FIELDS also adds 8 entries to the app's HRRR field picker, and at
# STRIDE=4 the assessment grid is ~15 km, so 3 km resolution buys this
# calculation nothing.  Left as a deliberate follow-up, not an oversight.
#
# Order is preference.  Every entry is (model, forecast hour).
MESO_SOURCES: tuple[tuple[str, int], ...] = (("rap", 1), ("rap", 0))

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
    # Simulated composite reflectivity — the initiation term. See _initiation.
    "refc",
    # The TRIGGER terms. See the block below.
    "omega700", "z500",
)

# ── Trigger: the term Johns & Doswell name and CAPE omits ──────────────────
# The gate above answers "is a storm here". This answers the other half of the
# same question -- "is anything going to MAKE one" -- and until now nothing did.
#
# It is the third ingredient quoted at the top of the initiation block: deep
# convection needs moisture, a steep enough lapse rate, AND "sufficient lifting
# of a parcel from the moist layer to allow it to reach its level of free
# convection" (Johns & Doswell 1992). CAPE folds the first two into one number.
# Nothing in this service supplied the third, which is why a loaded warm sector
# with no forcing and a loaded warm sector under a shortwave read identically.
#
# SPC's own reasoning is written in these terms. For the 2026-09-09 Slight over
# northern Ohio: "a fast-moving shortwave trough ... will track eastward", and
# "the primary baroclinic [zone] ... will move eastward and BE THE FOCUS for
# scattered afternoon thunderstorms". Neither was represented here at all.
#
# Three terms, OR'd, each measured on that case (RAP 18Z F01, valid 19Z; the
# NE Ohio box is 40.6-42.0N, 83.6-80.8W, where the warned line was):
#
#   omega700   large-scale ascent, Pa/s, NEGATIVE is up. The lift itself,
#              whatever causes it. CONUS p5 -0.34; NE Ohio reached -1.28.
#   |grad thetae|  the surface baroclinic zone -- a boundary, i.e. a focus,
#              even where the ascent has not started. CONUS p90 13.0 K/100 km;
#              NE Ohio reached 37.3, a sharp front exactly where SPC put it.
#   z500 fall  1-hourly height falls: the shortwave ARRIVING. CONUS p5 -9.98 m.
#              NE Ohio was only -3.85 at 19Z, which is correct and worth saying:
#              the trough was still upstream over the Lakes. This term is the
#              predictive one -- it fires before the other two do.
#
# Thresholds are the measured CONUS percentiles, not round numbers, for the
# same reason the reflectivity gate had to stop being 40 dBZ: a threshold means
# nothing until you know the distribution of the field you are applying it to.
# How much of a threat area must have a trigger before the wording changes.
# Two steps rather than one because "a front clips the corner" and "the whole
# area is under ascent" are different forecasts and should not read alike.
TRIGGER_PRESENT_FRAC = 0.15
TRIGGER_LIKELY_FRAC = 0.50
OMEGA_ASCENT_PA_S = -0.30          # ~CONUS p5: significant large-scale ascent
THETAE_GRAD_K_PER_100KM = 12.0     # ~CONUS p90: a frontal-strength gradient
Z500_FALL_M_PER_HOUR = -10.0       # ~CONUS p5: heights falling, trough inbound

# ── Initiation gate ────────────────────────────────────────────────────────
# THE MINNESOTA BUG, AND WHY IT WAS NEVER A THRESHOLD PROBLEM.
#
# STP, SCP, SHIP and EHI are CONDITIONAL discriminators. Every one of them was
# derived from a storm-only sample: Thompson et al. 2003 fitted STP to 413
# proximity soundings of which all 413 were storms (54 significantly tornadic,
# 144 weakly tornadic, 215 nontornadic supercells, 75 nonsupercell storms), and
# Thompson et al. 2012 used 22,901 severe events and say plainly that "the
# exclusion of these weaker events precludes a complete assessment of null
# cases". A parameter fitted to separate storm A from storm B can only answer
# "GIVEN a storm, which kind" — it carries no information about whether a storm
# exists. SPC says so on its own help page: "The majority of the parameters
# displayed have not been tested as prognostic tools."
#
# So grading a storm-free grid box on STP is not a tuning error, it is asking a
# question the parameter cannot answer, and it will happily return HIGH over
# clear skies. Craven/Brooks/Hart's climatology — the one that DOES include
# nulls, 60,090 soundings of which 45,508 had no thunder — found 39% of the
# no-thunder soundings still had non-zero CAPE. Ingredients without a trigger
# are the normal state of a warm-sector afternoon, not a severe threat.
#
# Johns & Doswell 1992 name the missing term: deep convection needs moisture, a
# steep enough lapse rate, AND "sufficient lifting of a parcel from the moist
# layer to allow it to reach its level of free convection". CAPE folds the
# first two into one number and omits the third entirely.
#
# The gate below supplies it. Storm presence comes from OBSERVED reflectivity
# (MRMS merged composite, matched to the analysis's own valid time), falling
# back to the model's simulated field only when no observation is close
# enough — see the OBS_STORM_DBZ block for why that order is not a
# preference but a correctness requirement. Everything outside the storm
# mask is only ever reported as conditional.
MUCAPE_FLOOR = 100.0        # SPC effective-inflow base needs CAPE >= 100 J/kg
MUCAPE_REFC_FLOOR = 50.0    # SPC's HREF screens reflectivity on MUCAPE > 50
EFFECTIVE_CIN_LIMIT = -250.0  # effective inflow layer: CIN > -250 J/kg
MLCIN_HARD_CAP = -200.0     # STP's own zero point for its MLCIN term
MLCIN_WEAK_CAP = -50.0      # STP's MLCIN term is 1.0 above this
# ── What answers "are there storms here" ───────────────────────────────────
# THIS GATE WAS UNSATISFIABLE, AND IT WAS A UNIT TRAP, NOT A TUNING ERROR.
#
# The threshold used to be a flat 40 dBZ against the RAP's SIMULATED composite
# reflectivity, documented as "SPC HREF probability threshold".  HREF is a
# CONVECTION-ALLOWING 3 km ensemble.  RAP is 13 km with PARAMETERIZED
# convection and physically cannot represent a convective core.  Measured
# 2026-09-09 19Z -- a Slight-risk afternoon with a warned line across northern
# Ohio, 82 tracked cells, 16 severe, 59 dBZ on radar:
#
#     RAP  18Z F01   >=30 dBZ  1.196% of grid    max anywhere in CONUS 45.1 dBZ
#                    >=35 dBZ  0.280%            >=50 dBZ: 0 cells, nationwide
#                    >=40 dBZ  0.028%  <- the gate
#     HRRR 18Z F01   >=40 dBZ  0.086%            max anywhere in CONUS 69.4 dBZ
#
# Over the warned line the RAP peaked at 39.1 dBZ, so `storms` was EMPTY there
# and every threat reported "if storms form" while the storms were on screen.
# The threshold sat near the 99.97th percentile of the model's own
# distribution.  This is the same error as comparing a km-scale Vrot against
# the TDA's gate-to-gate dV: a number carried across to an instrument it was
# not derived on.
#
# The fix is a different INSTRUMENT, not a different number.  Whether a storm
# exists is an observation, and MRMS merged composite reflectivity is already
# on this dashboard.  Order of preference:
#
#   1. MRMS observed reflectivity at the analysis's own valid time.
#   2. The model's simulated field, at a model-calibrated threshold, when no
#      observation is close enough to that valid time.
#
# 40 dBZ is right for OBSERVED reflectivity -- it is a real convective core and
# the value SPC's neighbourhood probabilities are calibrated to.
OBS_STORM_DBZ = 40.0
# The RAP fallback. Deliberately below the 0.078%-frequency match with HRRR
# (~38 dBZ on the day measured) because this path only runs when observations
# are unavailable, and there a false "storms present" merely reports an active
# threat an hour early, while a false negative reports nothing at all -- which
# is the failure this whole block exists to end. 35 dBZ caught the Ohio line
# with margin (206 cells in the box, against 0 at 40).
MODEL_STORM_DBZ = 35.0
# How far the MRMS frame may sit from the analysis's valid time. The analysis
# describes one hour; gating it on radar from a different hour would staple
# current storms onto a past environment -- the same mistake the near-storm
# environment lookup had to fix when it learned to resolve its own hour.
MRMS_MATCH_MIN = 20.0
# SPC computes neighbourhood reflectivity probabilities on a 40 km radius. The
# assessment grid is ~0.14 deg (~15 km) after STRIDE, so +/-2 cells is ~40 km.
REFC_NEIGHBORHOOD_CELLS = 2
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

# Plain-language names for the internal level keys.  Deliberately describing
# how FAVOURABLE the environment is, because that is the only thing these
# parameters measure — not how likely severe weather is at a point, which is
# what SPC's outlook categories mean and what our old wording implied.
LEVEL_LABELS = {
    "marginal": "Marginally favorable",
    "moderate": "Favorable",
    "high": "Very favorable",
    "extreme": "Extremely favorable",
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
    @staticmethod
    def _parse_run(token: str) -> tuple[str, str, int]:
        """Split an analysis token into (model, cycle, forecast hour).

        A token is "<model>:<YYYYMMDDHH>:<fhour>".  A bare 10-digit cycle is
        still accepted so an existing ?run= link, or anything stored before
        this change, keeps resolving.
        """
        parts = token.split(":")
        if len(parts) == 3:
            return parts[0], parts[1], int(parts[2])
        return MODEL, token, FHOUR

    @classmethod
    def _run_token(cls, model: str, cycle: str, fhour: int) -> str:
        return f"{model}:{cycle}:{fhour}"

    def latest_runs(self, limit: int = 4) -> list[str]:
        """Newest available analyses, freshest valid time first.

        Candidates come from every source in MESO_SOURCES; when two sources
        offer the same valid time the earlier (preferred) one wins.

        The `<= now` filter is not incidental.  An F01 is published about
        twelve minutes BEFORE its valid time, and presenting a field labelled
        with a future hour as the current analysis would be wrong on air --
        it is a forecast until the clock reaches it.  Holding it until then is
        exactly what yields the 0-60 minute age band.
        """
        now = time.time()
        if now - self._runs_cache[0] < 120 and self._runs_cache[1]:
            return self._runs_cache[1][:limit]
        svc = get_hrrr_field_service()
        now_dt = datetime.now(timezone.utc)

        by_valid: dict[str, str] = {}
        for model, fhour in MESO_SOURCES:
            try:
                cycles = [r["run"] for r in svc.list_runs(model, limit=max(limit, 4) + 2)]
            except Exception as e:
                logger.debug("meso: cannot list %s runs (%s)", model, e)
                continue
            for cycle in cycles:
                token = self._run_token(model, cycle, fhour)
                try:
                    iso = self._run_iso(token)
                except Exception:
                    continue
                if datetime.fromisoformat(iso) > now_dt:
                    continue  # valid in the future — not an analysis yet
                by_valid.setdefault(iso, token)

        runs = [by_valid[k] for k in sorted(by_valid, reverse=True)]
        self._runs_cache = (now, runs)
        return runs[:limit]

    @staticmethod
    def _run_iso(run: str) -> str:
        """VALID time of an analysis token — the cycle plus its forecast hour.

        Everything user-facing is labelled from this, not from the cycle: a
        22Z F01 is valid at 23Z and saying "22Z" would misreport it by an hour.
        """
        model, cycle, fhour = MesoanalysisService._parse_run(run)
        dt = datetime(int(cycle[:4]), int(cycle[4:6]), int(cycle[6:8]), int(cycle[8:10]),
                      tzinfo=timezone.utc) + timedelta(hours=fhour)
        return dt.isoformat()

    # ── Parameter grids ────────────────────────────────────────────────────
    def grids(self, run: str) -> Optional[dict[str, np.ndarray]]:
        """All mesoanalysis parameter grids for a cycle, coarsened. Cached per run."""
        with self._lock:
            if run in self._grids:
                return self._grids[run]
        svc = get_hrrr_field_service()
        model, cycle, fhour = self._parse_run(run)
        # One cheap up-front check: a cycle that is still uploading has no .idx,
        # and without this every one of the fields below would separately fail
        # with NoSuchKey before we gave up on the cycle.
        try:
            key = svc._key(model, cycle[:8], int(cycle[8:10]), fhour,
                           MODELS[model]["default_file"])
            svc._read_idx(model, key)
        except Exception as e:
            logger.debug("meso: cycle %s not available yet (%s)", run, e)
            return None

        out: dict[str, np.ndarray] = {}
        for fid in MESO_FIELDS:
            try:
                g = svc._field_grid(model, cycle, fid, fhour)
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

    def _trigger(self, params: dict[str, np.ndarray],
                 prev: Optional[dict[str, np.ndarray]] = None) -> dict:
        """Where something could lift a parcel to its LFC.

        Returns {"mask": bool grid or None, "parts": {name: bool grid}}. The
        terms are OR'd because they are alternative mechanisms, not a checklist:
        a storm needs one of them, not all three.

        This is reported ALONGSIDE the threat, never used to shrink it. A
        destabilising area with no forcing is a real and useful thing to see --
        it is most of what this product is for on a quiet afternoon -- it just
        should not read the same as an area with a front sitting on it.
        """
        shape = next((np.shape(g) for g in params.values() if g is not None), None)
        if shape is None:
            return {"mask": None, "parts": {}}

        def grid(name):
            g = params.get(name)
            return g if g is not None and np.shape(g) == shape else None

        parts: dict[str, np.ndarray] = {}

        om = grid("omega700")
        if om is not None:
            parts["ascent"] = np.isfinite(om) & (om <= OMEGA_ASCENT_PA_S)

        th = grid("thetae")
        if th is not None:
            # |grad theta-e| in K per 100 km. The cell is ~15 km N-S and narrows
            # with latitude, so the two axes get their own spacing -- using one
            # for both would report a front in Texas and miss the same front in
            # Minnesota.
            lats, _ = analysis_axes()
            if len(lats) == shape[0]:
                dy_km = T_RES * STRIDE * 110.57
                dx_km = (T_RES * STRIDE * 111.32) * np.cos(np.radians(lats))[:, None]
                with np.errstate(invalid="ignore"):
                    gy, gx = np.gradient(np.nan_to_num(th, nan=np.nanmean(th)))
                    mag = np.hypot(gy / dy_km, gx / np.maximum(1e-6, dx_km)) * 100.0
                parts["boundary"] = np.isfinite(mag) & (mag >= THETAE_GRAD_K_PER_100KM)

        z = grid("z500")
        zp = prev.get("z500") if prev else None
        if z is not None and zp is not None and np.shape(zp) == shape:
            with np.errstate(invalid="ignore"):
                fall = z - zp
            parts["height_falls"] = np.isfinite(fall) & (fall <= Z500_FALL_M_PER_HOUR)

        if not parts:
            return {"mask": None, "parts": {}}
        mask = np.zeros(shape, dtype=bool)
        for m in parts.values():
            mask |= m
        return {"mask": mask, "parts": parts}

    def _observed_storms(self, valid_iso: Optional[str], shape) -> Optional[np.ndarray]:
        """Observed convection on the assessment grid, or None if unavailable.

        Boolean: True where MRMS merged composite reflectivity reaches
        OBS_STORM_DBZ anywhere inside the assessment cell. The MAX over the
        footprint is the point -- a mean would average a convective core away
        against the surrounding light echo and reproduce, in the resampling,
        exactly the smoothing that made the model field unusable.

        The frame is chosen by the analysis's VALID TIME, not by "now", and a
        frame further than MRMS_MATCH_MIN from it is refused rather than used:
        an analysis valid at 19Z gated on 20Z radar would be two different
        moments presented as one. That is also what makes this safe for the
        older cycles `analysis()` keeps for trends -- they get no observation
        and fall back to the model, instead of being told about storms that
        formed after they were valid.

        Reads the service's packed display frame rather than re-decoding GRIB:
        it is already cached, and at (80 - -20)/255 the quantisation is 0.39 dBZ
        against a 40 dBZ threshold.
        """
        if not valid_iso:
            return None
        try:
            from .mrms_service import get_mrms_service
            svc = get_mrms_service()
        except Exception as e:
            logger.debug("meso: MRMS service unavailable (%s)", e)
            return None
        if svc is None or not svc.available():
            return None

        try:
            want = datetime.fromisoformat(valid_iso)
            if want.tzinfo is None:
                want = want.replace(tzinfo=timezone.utc)
            frames = svc.get_frame_list() or []
            best, best_gap = None, None
            for f in frames:
                ts = f.get("ts")
                if not ts:
                    continue
                try:
                    t = datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                gap = abs((t - want).total_seconds()) / 60.0
                if best_gap is None or gap < best_gap:
                    best, best_gap = ts, gap
            if best is None or best_gap > MRMS_MATCH_MIN:
                logger.debug("meso: no MRMS frame within %.0f min of %s (best %s)",
                             MRMS_MATCH_MIN, valid_iso,
                             f"{best_gap:.0f} min" if best_gap is not None else "none")
                return None
            raw = svc.get_frame_binary(best)
            if not raw:
                return None
            grid = self._unpack_mrms(raw, shape)
            if grid is not None:
                logger.info("meso: initiation gated on OBSERVED MRMS %s (%.0f min from %s)",
                            best, best_gap, valid_iso)
            return grid
        except Exception as e:
            logger.warning("meso: observed-reflectivity gate failed (%s) -- "
                           "falling back to the model field", e)
            return None

    @staticmethod
    def _unpack_mrms(raw: bytes, shape) -> Optional[np.ndarray]:
        """Packed MRMS frame -> boolean storm mask on the assessment grid."""
        import struct

        if len(raw) < 52 or raw[:4] != b"MRMS":
            return None
        ni, nj = struct.unpack_from("<II", raw, 4)
        north, south, west, east = struct.unpack_from("<dddd", raw, 12)
        vmin, vmax = struct.unpack_from("<ff", raw, 44)
        body = np.frombuffer(raw, dtype=np.uint8, count=ni * nj, offset=52)
        if body.size != ni * nj:
            return None
        gate = body.reshape(nj, ni)

        # Threshold in BYTE space: no float conversion of 6M cells needed, and
        # byte 0 is the no-data sentinel so it can never clear the threshold.
        span = (float(vmax) - float(vmin)) or 1.0
        cut = int(round((OBS_STORM_DBZ - float(vmin)) / span * 255.0))
        hit = gate >= max(1, cut)

        # Max over the assessment cell's footprint, then sample. Nearest-index
        # sampling of a max-filtered field is a max over the footprint without
        # building the index ranges by hand.
        lats, lons = analysis_axes()
        if (len(lats), len(lons)) != tuple(shape):
            return None
        dlat = (north - south) / nj
        dlon = (east - west) / ni
        cell_deg = T_RES * STRIDE
        try:
            from scipy import ndimage
            fy = max(1, int(round(cell_deg / max(1e-9, dlat))))
            fx = max(1, int(round(cell_deg / max(1e-9, dlon))))
            hit = ndimage.maximum_filter(hit, size=(fy, fx), mode="constant", cval=False)
        except Exception as e:
            logger.debug("meso: MRMS footprint max unavailable (%s)", e)

        rows = np.clip(((north - lats) / dlat).astype(int), 0, nj - 1)
        cols = np.clip(((lons - west) / dlon).astype(int), 0, ni - 1)
        out = hit[np.ix_(rows, cols)]
        # Entirely outside the MRMS domain, or an empty frame: not an answer.
        return out if out.any() else None

    def _initiation(self, params: dict[str, np.ndarray],
                    valid_iso: Optional[str] = None) -> dict[str, np.ndarray]:
        """Where storms are, and where the atmosphere could support them.

        Returns two masks:
          `capable` — enough buoyancy, and not sealed by a cap.  Necessary, but
                      on its own it is only "if something sets it off".
          `storms`  — capable AND convection actually present in the model's
                      reflectivity field within a ~40 km neighbourhood.

        Nothing is rated outside `capable`, and nothing is rated as an ACTIVE
        threat outside `storms`.  See the constants above for why.
        """
        shape = next((g.shape for g in params.values() if g is not None), None)
        if shape is None:
            return {}

        def grid(name):
            g = params.get(name)
            return g if g is not None and np.shape(g) == shape else None

        mucape = grid("mucape")
        if mucape is None:
            mucape = grid("mlcape")
        mlcin = grid("mlcin")
        if mlcin is None:
            mlcin = grid("sbcin")

        capable = np.ones(shape, dtype=bool)
        if mucape is not None:
            capable &= np.isfinite(mucape) & (mucape >= MUCAPE_FLOOR)
        if mlcin is not None:
            # CIN is negative. A cap stronger than STP's own zero point seals
            # surface-based convection; NaN means unknown, not uncapped, but
            # treating unknown as capped would erase real threats when the
            # field simply failed to download, so it stays permissive.
            capable &= ~(np.isfinite(mlcin) & (mlcin < MLCIN_HARD_CAP))
        land = self.land_mask
        if land is not None and np.shape(land) == shape:
            capable &= land

        storms = np.zeros(shape, dtype=bool)
        source = "none"
        # OBSERVATION FIRST. See the OBS_STORM_DBZ block: the model's simulated
        # field cannot answer this question on a 13 km parameterized-convection
        # grid, and it is the wrong instrument rather than the wrong number.
        hit = self._observed_storms(valid_iso, shape)
        if hit is not None:
            source = "observed"
        else:
            refc = grid("refc")
            if refc is not None:
                hit = np.isfinite(refc) & (refc >= MODEL_STORM_DBZ)
                source = "model"
        if hit is not None:
            if mucape is not None:
                hit = hit & np.isfinite(mucape) & (mucape > MUCAPE_REFC_FLOOR)
            try:
                from scipy import ndimage
                n = REFC_NEIGHBORHOOD_CELLS * 2 + 1
                hit = ndimage.maximum_filter(hit, size=n, mode="constant", cval=False)
            except Exception as e:
                logger.debug("meso: reflectivity neighbourhood unavailable (%s)", e)
            storms = hit & capable

        # Weakly inhibited and buoyant, but nothing going yet: reportable only
        # as conditional.
        conditional = capable & ~storms
        if mlcin is not None:
            conditional &= ~(np.isfinite(mlcin) & (mlcin < MLCIN_WEAK_CAP))

        return {"capable": capable, "storms": storms, "conditional": conditional,
                "source": source}

    def _assess(self, p: dict[str, np.ndarray], valid_iso: Optional[str] = None,
                prev: Optional[dict[str, np.ndarray]] = None,
                ) -> tuple[dict, dict[str, np.ndarray]]:
        params = self._resolve(p)
        gate = self._initiation(params, valid_iso)
        trig = self._trigger(params, self._resolve(prev) if prev else None)
        trigger = trig.get("mask")
        storms = gate.get("storms")
        capable = gate.get("capable")
        conditional = gate.get("conditional")
        # Whether the gate had ANY way to answer "are there storms". Without one
        # every threat is honestly conditional; with one, "conditional" means
        # the ingredients are there and the convection is not -- which is the
        # forecast half of this product and worth being able to say precisely.
        have_refc = gate.get("source", "none") != "none"

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

            # ── Initiation gate ────────────────────────────────────────────
            # A parameter space is a statement about what storms WOULD do here.
            # Whether any storm exists is a separate question, and answering it
            # is what stops a warm, sheared, storm-free afternoon from being
            # rendered as a severe threat.
            res["basis"] = "environment"
            res["conditional"] = True
            if res["level"] != "none" and mask is not None and capable is not None:
                mask = mask & capable
                active = mask & storms if storms is not None else None
                any_storms = active is not None and bool(active.any())
                if any_storms and self._frac(active) >= floor:
                    # Storms present over a reportable area: rate on them.
                    mask = active
                    res["basis"] = "storms" if have_refc else "environment"
                    res["conditional"] = not have_refc
                elif any_storms:
                    # Storms present but covering less than the area floor —
                    # an isolated cell, or convection still going up.
                    #
                    # This used to fall through to the branch below, which does
                    # `mask &= conditional`, and `conditional` is
                    # `capable & ~storms`: it removed precisely the cells that
                    # had the storms in them and drew the threat on the
                    # surrounding storm-free air. At ~181 km² per assessment
                    # cell the marginal floor is 12,120 km² (67 cells), which a
                    # dilated single storm (4,531) or small cluster (6,525)
                    # never reaches — so the zone was displaced off the
                    # convection exactly during initiation, the moment it
                    # matters most. The floor decides whether a threat is worth
                    # reporting, never where it is.
                    mask = active
                    res["basis"] = "storms" if have_refc else "environment"
                    res["conditional"] = not have_refc
                    res["below_area_floor"] = True
                else:
                    # Nothing going yet. The parameters still describe what
                    # would happen IF storms formed — the forecast half of this
                    # product — so keep the threat and label it conditional.
                    if conditional is not None:
                        mask = mask & conditional
                    res["basis"] = "conditional"
                    res["conditional"] = True
                frac = self._frac(mask)
                res["coverage"] = round(frac, 5)
                # The floor exists so a handful of noisy cells cannot set a
                # national threat level over an empty map. It must not silence a
                # threat sitting on OBSERVED convection: one severe storm is a
                # small share of CONUS and still the thing the operator is
                # looking at. Suppress on area only when there is no storm.
                if frac < floor and not res.get("below_area_floor"):
                    res.update({
                        "level": "none", "mode": None, "modes": [],
                        "basis": "none", "conditional": True,
                        "details": "Ingredients present but no convection and no "
                                   "trigger — nothing to be severe.",
                    })
                    mask = None

            # How much of this threat's area has something to set it off. Kept
            # as a FRACTION rather than a boolean because "a front clips the
            # corner" and "the whole area is under ascent" are different
            # forecasts, and because a boolean would have to pick a threshold
            # here as well as in _trigger.
            if res["level"] != "none" and mask is not None and trigger is not None:
                n = int(mask.sum())
                res["trigger"] = round(float((mask & trigger).sum()) / n, 3) if n else 0.0
                res["trigger_terms"] = sorted(
                    k for k, m in trig["parts"].items() if bool((mask & m).any()))
            res["label"] = self._level_label(res["level"], res.get("conditional", True),
                                             res.get("trigger"))
            if res["level"] != "none":
                res["details"] = self._threat_details(t, res)

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
    def _level_label(level: str, conditional: bool,
                     trigger: Optional[float] = None) -> str:
        """Plain-language wording for a threat level.

        The level KEYS stay as they are — map layers and saved views are keyed
        on them — but nothing user-facing should say MARGINAL / MODERATE /
        HIGH again.  Those are the names of SPC's categorical outlook
        categories, defined by specific probabilities of severe weather within
        25 miles of a point.  Printing one of them over a storm-free Minnesota
        does not just overstate our own confidence, it reads as though SPC had
        issued that outlook.  On air that is a misrepresentation of an official
        product, which is a different and worse problem than being wrong.
        """
        if level == "none":
            return "No threat"
        base = LEVEL_LABELS.get(level, level.title())
        if not conditional:
            return base
        # "if storms form" is honest but it was the ONLY thing this ever said,
        # and a phrase that never varies stops being read. When something is
        # there to set them off, say so — that is the difference between a
        # warm sector and a warm sector with a front in it.
        if trigger is not None and trigger >= TRIGGER_LIKELY_FRAC:
            return f"{base}, storms expected to develop"
        if trigger is not None and trigger >= TRIGGER_PRESENT_FRAC:
            return f"{base} where storms develop"
        return f"{base} if storms form"

    @staticmethod
    def _threat_details(threat: str, res: dict) -> str:
        """One sentence saying what this means, and on what basis.

        Every composite here answers "given a storm, which kind" — so the
        wording says "given a storm" whenever that is what we actually know.
        """
        level = res.get("level", "none")
        msg = THREAT_MESSAGES.get(threat, {}).get(level, "Threat detected.")
        if res.get("basis") == "storms":
            lead = "Storms present."
            if res.get("below_area_floor"):
                lead = "Storms present (isolated)."
            return f"{lead} {msg}"
        trig = res.get("trigger")
        terms = res.get("trigger_terms") or []
        if trig is not None and trig >= TRIGGER_PRESENT_FRAC and terms:
            named = {"ascent": "large-scale ascent",
                     "boundary": "a surface boundary",
                     "height_falls": "falling heights aloft"}
            what = ", ".join(named.get(k, k) for k in terms)
            share = "Most of this area has" if trig >= TRIGGER_LIKELY_FRAC else "Part of this area has"
            return (f"No convection yet, but {what} — {share.lower()} something to "
                    f"set storms off. {msg}")
        return ("No convection, and nothing lifting parcels to their LFC — "
                f"ingredients only. {msg}")

    @staticmethod
    def _summary(threats: dict, params: dict, zone: Optional[np.ndarray]) -> str:
        active = []
        for name, d in threats.items():
            if name == "overall" or d.get("level", "none") == "none":
                continue
            label = name.replace("_", " ").title()
            ml = MODE_LABELS.get(d.get("mode"), "")
            wording = d.get("label") or d["level"].title()
            active.append(f"{label}: {wording}" + (f" ({ml})" if ml else ""))
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
            prev = None
            for pr in runs:
                if pr < r:
                    prev = self.grids(pr)
                    break
            threats, masks = self._assess(grids, self._run_iso(r), prev)
            combined = None
            for m in masks.values():
                combined = m if combined is None else (combined | m)
            watch = self._watch(grids)
            result = {
                "run": r,
                "valid_time": self._run_iso(r),
                # The source is the analysis token, not a hardcoded f00: since
                # MESO_SOURCES learned to prefer the previous cycle's F01 this
                # is usually an F01, and saying "f00" misreported it.
                "model": "RAP analysis (f%02d)" % self._parse_run(r)[2],
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
