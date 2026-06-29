"""
Storm cell identification, tracking, and scoring service.
Analyzes each NEXRAD volume scan to identify storm cells, track their
movement, detect rotation/hail/debris signatures, and assign severity scores.
"""

import asyncio
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

THREAT_LEVELS = {
    "minimal": {"min": 0, "max": 25, "color": "#888888"},
    "moderate": {"min": 26, "max": 50, "color": "#FFD700"},
    "significant": {"min": 51, "max": 70, "color": "#FF8C00"},
    "severe": {"min": 71, "max": 85, "color": "#FF0000"},
    "extreme": {"min": 86, "max": 100, "color": "#FF00FF"},
}

# Scoring weights — 11 factors, must sum to 100.
# Phase 5 restructure: LLSD (the single most predictive feature for
# tornadogenesis per published research) now outweighs the noisier velocity-
# couplet rotation factor.  Grid-based rotation is Barnes2-blurred and the
# multi-tilt profile measures column rotation that may not reach the surface;
# LLSD directly measures near-surface azimuthal shear on raw polar data.
SCORE_WEIGHTS = {
    "reflectivity":    15,  # SCIT core dBZ level
    "growth_trend":    10,  # Quantitative intensification (LLSD/rot/echo-top/VIL slopes)
    "rotation":        10,  # Velocity couplet (grid + multi-tilt profile)
    "llsd":            16,  # Low-level azimuthal shear (polar, lowest tilt)
    "hail":            13,  # Dual-pol strict core + TBSS confirmation + MESH
    "downburst_marc":   8,  # Downburst ΔV + MARC mid-level convergence
    "straight_line":    4,  # Broad severe outflow swath + RIJ
    "debris":          14,  # TDS with multi-tilt depth confirmation
    "vil":              4,  # Cell-based VIL from multi-tilt integration
    "cell_top":         3,  # Echo top height (overshooting top severity)
    "lightning":        3,  # GLM flash rate near cell
}

# Detection thresholds
# SCIT thresholds (Johnson et al. 1998, WAF) — processed high→low so that
# distinct high-reflectivity cores are found before lower-threshold halos are tested.
SCIT_THRESHOLDS = [60, 55, 50, 45, 40, 35, 30]  # dBZ
CELL_DETECT_DBZ = 35  # Used for MCS classification filter (not identification)
CELL_MIN_AREA_KM2 = 5  # Minimum component area to reject noise pixels
MAX_MATCH_DISTANCE_KM = 20  # Max distance for cell matching across scans

# ── Volumetric SCIT identification (Johnson et al. 1998 + WSR-88D ROC specs) ──
# Per-tilt 2D component extraction on the polar reflectivity of every elevation,
# then vertical association across tilts into 3D cells.  Defaults are the ROC
# algorithm's adaptable parameters.
SCIT_SEG_MIN_LEN_KM = 1.9        # min along-radial run length for a segment
SCIT_SEG_DROPOUT_GATES = 2       # gates below threshold bridged within a segment
SCIT_COMP_MIN_AREA_KM2 = 10.0    # min 2D component area (per tilt)
SCIT_COMP_MIN_SEGMENTS = 2       # min radials spanned by a component
SCIT_VERT_RADII_KM = (5.0, 7.5, 10.0)   # escalating vertical-association search radii
SCIT_CELL_MIN_COMPONENTS = 2     # min components (tilts) for a valid 3D cell
SCIT_MERGE_HORIZ_KM = 10.0       # merge cells whose centroids are within this
SCIT_DECROWD_KM = 5.0            # of two cells closer than this, keep the stronger
SCIT_MAX_ELEV_DEG = 20.0         # ignore tilts above this (no convective core info)
# Minimum time between motion vector updates.  When two active sites
# produce scans only seconds apart, cross-site parallax (3–5 km) divided
# by a tiny Δt produces physically impossible speeds (300–600 kph).
# Below this threshold we keep the previous motion vector unchanged.
MIN_MOTION_DT_SECONDS = 120.0   # 2 minutes
MAX_STORM_SPEED_KPH   = 175.0   # Hard physical cap (extreme derecho upper bound)
MESO_VELOCITY_THRESHOLD_MS = 13  # Minimum rotational velocity for mesocyclone
                                  # (lowered from 15 to catch borderline LP/QLCS
                                  # mesos where forecasters issue TOR warnings on
                                  # 12–15 m/s couplets; couplet-diameter and
                                  # straddling guards still reject noise)
TVS_VELOCITY_THRESHOLD_MS = 25  # Tornado vortex signature threshold
MESO_MAX_DIAMETER_KM = 10  # Max diameter for mesocyclone couplet
HAIL_REFLECTIVITY_DBZ = 55  # Reflectivity threshold for hail check
HAIL_CC_RANGE = (0.70, 0.95)  # CC range indicating hail
HAIL_ZDR_THRESHOLD = 0.5  # ZDR near 0 indicates hail (within ±this)
DEBRIS_CC_THRESHOLD = 0.80      # CC below this (combined with strong rotation) = TDS
# TDS requires STRONG rotation, not just the meso threshold.  A rain cell
# can produce a weak 15 m/s couplet; a real tornado produces 20+ m/s.
TDS_MIN_ROTATION_MS = 20.0
# NWS dual-pol TDS criteria require Z ≥ 20 dBZ in the low-CC region.
TDS_MIN_REFL_DBZ = 20.0
# Beam height ceiling for TDS: above this the beam samples ice/mixed-phase,
# not debris — low CC is normal and should NOT trigger a debris signature.
TDS_MAX_BEAM_HEIGHT_KM = 1.5
VIL_HIGH_THRESHOLD = 40  # kg/m² considered high VIL

# QLCS / MCS detection thresholds
QLCS_MESO_VELOCITY_MS = 10    # Lower velocity threshold for embedded QLCS mesos
QLCS_MAX_DIAMETER_KM = 5      # QLCS mesos are smaller than supercell mesos
QLCS_SEARCH_RADIUS_KM = 3.0   # Tighter search around each QLCS cell
MCS_MIN_CELLS = 5             # Minimum cells to classify as a linear system
MCS_MIN_LENGTH_KM = 50        # Minimum line length (km)
MCS_ASPECT_RATIO_MIN = 4.0    # Primary:secondary extent ratio for "linear"
MCS_MAX_WIDTH_KM = 25.0       # Max perpendicular extent — lines are narrow
MCS_SPACING_CV_MAX = 1.0      # Coefficient of variation on along-line spacing (reject clumped)
MCS_BOW_MIN_CELLS = 6         # Bow echoes need more cells than a bare squall line
BOW_ECHO_MIN_BULGE_KM = 15.0  # Min leading-edge bulge to call it a bow echo
RNI_BULGE_KM = 25.0           # Rear-inflow notch: bow bulge ≥ this

# LLSD (Low-Level Shear Detection) — azimuthal shear on the lowest polar sweep
# Units: /s (inverse seconds). Derived from ∂V/∂azimuth over a small kernel.
LLSD_KERNEL_RAYS = 2           # Half-width in rays for shear stencil (±2 rays ≈ 2°)
LLSD_KERNEL_GATES = 3          # Half-width in gates for local max search
LLSD_WEAK_SHEAR = 0.005        # /s — noticeable shear
LLSD_MESO_SHEAR = 0.010        # /s — weak meso-class rotation
LLSD_STRONG_SHEAR = 0.020      # /s — strong low-level rotation
LLSD_TORNADIC_SHEAR = 0.025    # /s — tornadic-class shear
LLSD_MAX_ELEVATION_DEG = 1.2   # Only consider sweeps at or below this tilt for LLSD
LLSD_CELL_SEARCH_KM = 4.0      # Half-width of the window around each cell
# Multi-tilt rotation profile — run the same shear analysis at every tilt to
# catch mid-level mesos that may not extend to the surface.
ROTATION_PROFILE_MAX_ELEV_DEG = 8.0   # Don't bother above this (mesos below here)
# Mesocyclone height constraints — prevents anvil/outflow divergence from being
# misclassified.  Real supercell mesos live at 2–8 km AGL.  Rotation detected
# only above 8 km is upper-level wind shear, not a low-level vortex.
MESO_MAX_CONFIRM_HEIGHT_KM = 8.0
# Minimum vertical depth (km) across tilts for a CONFIRMED mesocyclone.
# 0.0 km means only ONE tilt exceeded the velocity threshold — not enough.
# LLSD confirmation can override this requirement for single-tilt low-level rotation.
MESO_MIN_DEPTH_FOR_CONFIRM_KM = 0.1  # > 0 requires at least 2 tilts

# Altitude bands for operational rotation classification.
# Low-level mesos (≤ 2 km AGL OR LLSD-confirmed surface shear) are the
# primary tornadogenesis precursor.  Mid-level rotation (2–6 km) is a
# normal supercell signature without imminent tornado risk; rotation
# above 6 km AGL with no lower extent is anvil-level shear.
LOW_LEVEL_MESO_MAX_HEIGHT_KM = 2.0
MID_LEVEL_MESO_MAX_HEIGHT_KM = 6.0
# Depth bonus only applies when rotation reaches the boundary layer.
LOW_LEVEL_ROT_DEPTH_BONUS_BASE_KM = 1.5

# Trend detection — slope computed over this many consecutive scans.
# Research (TorNet/WAF 2023) shows 5 scans ≈ 25 minutes is the optimal
# window: long enough to filter noise, short enough to catch rapid onset.
TREND_WINDOW_SCANS  = 5
TREND_HISTORY_MAX   = 12   # keep at most this many snapshots per cell

# ---------------------------------------------------------------------------
# Phase 2: Kinematic wind signatures
# ---------------------------------------------------------------------------

# Downburst / microburst — lowest-tilt radial divergence
# NWS defines microburst as ΔV ≥ 10 m/s; severe microburst ≥ 25 m/s.
# We use 20 m/s as the flag threshold (operationally significant).
DOWNBURST_DELTA_V_MS      = 20.0    # Minimum ΔV across divergent signature (m/s)
DOWNBURST_MAX_DIAMETER_KM = 10.0    # Maximum compact diameter; wider → storm-scale motion
DOWNBURST_INBOUND_FLOOR_MS  = -10.0 # Near-side must be at least this inbound (m/s)
DOWNBURST_OUTBOUND_FLOOR_MS =  10.0 # Far-side must at least be this outbound (m/s)

# MARC (Mid-Altitude Radial Convergence) — strong updraft inflow at 3–9 km AGL
# Operationally used as a supercell hail/tornado precursor by NWS forecasters.
MARC_MIN_HEIGHT_KM      = 3.0       # Bottom of the MARC search layer (km AGL)
MARC_MAX_HEIGHT_KM      = 9.0       # Top of the MARC search layer (km AGL)
MARC_CONVERGENCE_MS     = -12.0     # Peak inbound required (m/s; negative = inbound)
MARC_REFL_FLOOR_DBZ     = 45.0      # Require this reflectivity at low levels beneath MARC

# Straight-line winds — broad swath of damaging outflow
SLW_SEVERE_MS           = 25.7      # 50 knots — NWS severe wind threshold (m/s)
SLW_SEARCH_RADIUS_KM    = 40.0      # Broad window radius for swath measurement
SLW_MIN_SWATH_KM2       = 30.0      # Minimum severe-velocity coverage area (km²)

# Sub-severe but operationally notable: ~35 kt (18 m/s).  Catches the
# strong-but-not-yet-severe wind regime in developing squall lines and
# bow echoes — gust front pushes, intensifying QLCS outflow, etc.  Smaller
# swath threshold because the strong tier is meant to flag concern earlier
# in the life cycle, when the high-wind footprint is still building.
SLW_STRONG_MS           = 18.0      # 35 knots (sub-severe but damaging)
SLW_STRONG_MIN_SWATH_KM2 = 15.0     # Smaller area threshold for the strong tier

# Rear-Inflow Jet (RIJ) — mid-level inbound channel behind a bow echo
RIJ_INBOUND_MS          = -20.0     # Strong inbound threshold for RIJ channel (m/s)
RIJ_HEIGHT_MIN_KM       = 1.0       # Lower AGL bound of the RIJ search layer
RIJ_HEIGHT_MAX_KM       = 4.0       # Upper AGL bound of the RIJ search layer

# ---------------------------------------------------------------------------
# Phase 3: Enhanced dual-pol signatures
# ---------------------------------------------------------------------------

# Strict hail core: pixel-level co-location required (Z > 55, ZDR ≈ 0, CC 0.85-0.95).
# The 0.85 lower bound is the key upgrade — the old HAIL_CC_RANGE started at 0.70,
# which admits large-drop rain and even debris.  True large hail tumbles, producing
# ZDR ≈ 0 and CC 0.85-0.95 (mixed-phase ice/water coating).
HAIL_STRICT_CC_MIN    = 0.85    # Lower CC bound for confirmed large-hail core
HAIL_STRICT_CC_MAX    = 0.95    # Upper CC bound (>0.95 is rain, not hail)
HAIL_STRICT_ZDR_MAX   = 0.5     # |ZDR| ≤ this value confirms tumbling hailstones
HAIL_STRICT_MIN_PX    = 3       # Minimum co-located pixels to confirm hail core

# Three-Body Scatter Spike (TBSS): range ghost behind hail core at same azimuth.
# Radar energy hits hail, scatters to ground, reflects back, then hail re-scatters
# it to the radar — arriving at ~1.3–2x the actual hail range (same azimuth).
TBSS_MIN_RANGE_KM         = 40.0   # Minimum hail core range for TBSS to be detectable
TBSS_SPIKE_DBZ_MIN        = 10.0   # Minimum reflectivity in the spike region
TBSS_SPIKE_DBZ_MAX        = 40.0   # Maximum (spike is always weaker than the hail core)
TBSS_SPIKE_CC_MAX         = 0.85   # TBSS returns have anomalously low CC
TBSS_RANGE_FACTOR_MIN     = 1.2    # Spike begins at this fraction of hail core range
TBSS_RANGE_FACTOR_MAX     = 2.0    # Spike ends at this fraction
TBSS_MIN_SPIKE_PIXELS     = 3      # Minimum spike gates to confirm

# TDS multi-tilt continuity.  A true debris signature is a deep column of mixed
# scatterers visible across multiple low-level tilts.  Single-tilt detection can
# be ground clutter, roost scatter, or AP — requiring TWO tilts eliminates >90 %
# of false positives while preserving true TDS detection (ops experience, NSSL).
TDS_MIN_TILT_COUNT        = 2      # Minimum confirming tilts for TDS (was 1, implicitly)
TDS_MAX_TILT_HEIGHT_KM    = 2.0    # Only count tilts whose beam height ≤ this AGL

# ---------------------------------------------------------------------------
# Phase 5: BWER + MESH
# ---------------------------------------------------------------------------

# Bounded Weak Echo Region — column signature of an intense, tilted supercell
# updraft.  Weak echo (≤ BWER_WEAK_DBZ) bounded above by strong echo overhang
# (≥ BWER_OVERHANG_DBZ) and below by moderate echo.  Classic indicator of an
# updraft strong enough to suspend large hail and support tornadogenesis.
BWER_WEAK_DBZ           = 25.0   # Reflectivity ceiling for the weak region
BWER_OVERHANG_DBZ       = 50.0   # Strong echo required ABOVE the weak region
BWER_BELOW_DBZ          = 35.0   # Moderate echo required BELOW (bounded)
BWER_MIN_WEAK_HEIGHT_KM = 3.0    # Weak region must be at or above this AGL
BWER_MAX_WEAK_HEIGHT_KM = 8.0    # ...and below this (anvil starts above 8–10 km)
BWER_MIN_OVERHANG_KM    = 1.0    # Vertical separation between weak and overhang

# MESH (Maximum Estimated Size of Hail) — NSSL formula derived from the
# Severe Hail Index (Witt et al. 1998).  Requires the environmental 0°C and
# −20°C heights; we use sensible defaults for warm-season convection and
# allow site configuration via the freezing_level_km setting.  Implementation
# integrates per-tilt reflectivity through the column and weights by the
# temperature-based hail kinetic energy function.
MESH_FREEZING_LEVEL_KM_DEFAULT = 3.5   # Warm-season default (~July US plains)
MESH_MINUS_20C_DELTA_KM        = 2.5   # −20°C ≈ freezing level + 2.5 km
MESH_Z_LOWER_DBZ               = 40.0  # Lower Z cutoff for hail kinetic energy
MESH_Z_UPPER_DBZ               = 50.0  # Upper Z for full kinetic energy weight
MESH_SHI_TO_MM_COEFF           = 2.54  # NSSL MESH (mm) = coeff × SHI^0.5
MESH_SIG_HAIL_MM               = 19.0  # 3/4" — severe threshold
MESH_LARGE_HAIL_MM             = 44.0  # 1.75" — significant severe
MESH_GIANT_HAIL_MM             = 76.0  # 3" — giant hail


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TrackedStormCell:
    """A tracked storm cell with full analysis."""
    cell_id: str
    lat: float
    lon: float
    max_reflectivity_dbz: float
    area_km2: float
    severity_score: int  # 0-100 composite
    threat_level: str
    motion_direction_deg: float  # Direction cell is moving (meteorological)
    motion_speed_kph: float
    rotation_detected: bool
    rotation_velocity_ms: Optional[float]
    tvs_detected: bool
    qlcs_meso_detected: bool    # Embedded QLCS mesocyclone (smaller, weaker than supercell meso)
    qlcs_meso_velocity_ms: Optional[float]
    hail_indicated: bool
    hail_max_dbz: Optional[float]
    debris_signature: bool
    vil_kg_m2: Optional[float]
    cell_top_km: Optional[float]
    track_history: list[dict]  # [{lat, lon, timestamp}]
    forecast_track: list[dict]  # [{lat, lon, minutes_ahead}]
    score_breakdown: dict  # {factor: score}
    first_detected: str
    last_updated: str
    trend: str  # strengthening / steady / weakening
    scan_count: int  # Number of scans this cell has been tracked
    mcs_system_id: Optional[str] = None  # Set if part of a linear MCS/QLCS system
    llsd_rotation_detected: bool = False   # Low-level azimuthal shear exceeds weak-meso threshold
    llsd_max_shear: Optional[float] = None # Peak |∂V/∂az| (/s) on the lowest tilt near this cell
    llsd_elevation_deg: Optional[float] = None  # Elevation angle of the sweep used
    llsd_diagnostic: Optional[str] = None  # Skip-reason when LLSD bailed; None on success
    # Multi-tilt rotation profile
    max_rot_velocity_ms: Optional[float] = None  # Peak rotational velocity anywhere in column
    max_rot_height_km: Optional[float] = None    # Height where peak rotation occurs
    rotation_profile: list = field(default_factory=list)  # [{height_km, shear, rot_ms}]
    rotation_depth_km: Optional[float] = None    # Vertical extent of rotation ≥ meso threshold
    rotation_base_km: Optional[float] = None     # Lowest tilt with ≥ meso-class rotation
    # Operational altitude classification — set in _reconcile_rotation_flags.
    # `low_level_meso_detected` is the primary tornado precursor; mid-level
    # alone is just a supercell signature.  A deep meso can be both.
    low_level_meso_detected: bool = False
    mid_level_meso_detected: bool = False
    cell_base_km: Optional[float] = None       # Lowest 18 dBZ echo height (AGL)
    max_ref_height_km: Optional[float] = None  # Height at which max reflectivity occurs
    centroid_height_km: Optional[float] = None # Reflectivity-weighted mean height
    depth_km: Optional[float] = None           # cell_top - cell_base

    # ── Trend fields (rate of change per scan, positive = increasing) ──────
    # Computed by _compute_trends() over the last TREND_WINDOW_SCANS scans.
    # These are the most important features for pre-alert detection because
    # a rapidly intensifying storm is far more dangerous than a steady one.
    llsd_trend: Optional[float] = None        # LLSD shear /s per scan
    rot_vel_trend: Optional[float] = None     # rotational velocity m/s per scan
    vil_trend: Optional[float] = None         # VIL kg/m² per scan
    echo_top_trend: Optional[float] = None    # echo top km per scan
    dbz_trend: Optional[float] = None         # max reflectivity dBZ per scan

    # ── Phase 2: Kinematic wind signatures (set each scan by Phase 2 detectors) ──
    downburst_detected: bool = False
    downburst_delta_v_ms: Optional[float] = None    # ΔV across divergent signature (m/s)
    marc_signature_detected: bool = False
    marc_convergence_ms: Optional[float] = None     # Peak inbound at mid-level (m/s, negative)
    straight_line_wind_detected: bool = False
    strong_wind_detected: bool = False               # Sub-severe (≥35 kt) broad outflow swath
    strong_wind_swath_km2: Optional[float] = None    # Area of strong-tier coverage (km²)
    max_wind_velocity_ms: Optional[float] = None    # Peak broad outbound velocity (m/s)
    rij_detected: bool = False                       # Rear-Inflow Jet inside a bow echo

    # ── Phase 3: Enhanced dual-pol ───────────────────────────────────────────
    tbss_detected: bool = False          # Three-Body Scatter Spike confirmed behind hail core
    tds_tilt_count: int = 0             # Number of low tilts confirming TDS (≥2 = genuine)

    # ── Phase 5: BWER + MESH ─────────────────────────────────────────────────
    bwer_detected: bool = False          # Bounded weak echo region (strong updraft signature)
    bwer_overhang_dbz: Optional[float] = None  # Peak Z above the weak region
    mesh_mm: Optional[float] = None       # Maximum Estimated Size of Hail (mm)
    shi_value: Optional[float] = None     # Raw Severe Hail Index integral
    # ML rotation classifier output (None if no model loaded or features unavailable)
    p_rotation_model: Optional[float] = None

    # ── Internal scan-history for trend computation (not sent to frontend) ──
    # Stores the last TREND_HISTORY_MAX snapshots of key numeric fields.
    feature_history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("feature_history", None)   # internal only — strip before WebSocket
        return d


@dataclass
class MCSSystem:
    """
    A Mesoscale Convective System or quasi-linear convective system (QLCS)
    identified from the spatial arrangement of tracked storm cells.
    """
    system_id: str
    system_type: str           # 'squall_line', 'bow_echo', 'mcs'
    cell_ids: list[str]        # Member cell IDs
    centroid_lat: float
    centroid_lon: float
    orientation_deg: float     # Line angle from north (0=N–S, 90=E–W)
    length_km: float
    bow_echo_detected: bool
    rear_inflow_notch: bool
    book_end_vortices: bool
    embedded_qlcs_mesos: int   # Count of embedded QLCS mesos in the system
    max_severity_score: int
    threat_level: str
    motion_direction_deg: float
    motion_speed_kph: float
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _InternalCell:
    """Internal representation during identification (before tracking)."""
    centroid_y: int  # Grid index
    centroid_x: int
    lat: float
    lon: float
    max_dbz: float
    area_km2: float
    pixel_mask: object  # numpy boolean array
    bbox: tuple  # (y_min, y_max, x_min, x_max)
    # Dual-pol analysis results
    mean_cc: Optional[float] = None
    min_cc: Optional[float] = None
    mean_zdr: Optional[float] = None
    # Multi-threshold structure
    dbz_50_area_km2: float = 0.0
    dbz_55_area_km2: float = 0.0
    dbz_60_area_km2: float = 0.0
    # Phase 3: strict pixel-level dual-pol hail confirmation
    hail_core_pixels: int = 0   # pixels where Z>55, |ZDR|≤0.5, CC∈[0.85,0.95] co-locate


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class StormTrackingService:
    """Identifies, tracks, and scores storm cells from NEXRAD volume scans."""

    def __init__(self):
        self._tracked_cells: dict[str, TrackedStormCell] = {}
        self._tracked_systems: dict[str, MCSSystem] = {}
        self._previous_cells: list[_InternalCell] = []
        self._history_max_scans = 20  # Keep up to 20 scans of track history

        # Grid metadata from last scan
        self._grid_lat: Optional[np.ndarray] = None
        self._grid_lon: Optional[np.ndarray] = None
        self._grid_res_km: float = 1.0
        self._grid_shape: int = 460  # N×N grid size (derived from grid)
        self._range_m: float = 230_000.0  # Radar max range in metres
        # Image overlay bounds (same dict stored in RadarFrame / VolumeScanData)
        self._bounds: Optional[dict] = None  # {south, north, west, east}

        # Radar site locations {site_id: (lat, lon)} — used for Voronoi
        # partitioning so each cell/pixel is analysed by its closest radar.
        self._radar_locations: dict[str, tuple[float, float]] = {}

        # Environmental freezing level (km AGL) — used by MESH/SHI hail sizing.
        # Defaults to warm-season convection; overridable via set_freezing_level()
        # so callers can wire HRRR/RAP sounding data when available.
        self._freezing_level_km: float = MESH_FREEZING_LEVEL_KM_DEFAULT

        # Optional ML rotation classifier (loaded on service start if a trained
        # model exists at data/rotation_model.joblib).  Inert until trained.
        self._rotation_model = None
        self._rotation_model_features: list[str] = []

        # GLM lightning service reference (optional)
        self._glm_service = None

        # Callbacks
        self.on_cells_updated: Optional[Callable] = None   # (cells: list[TrackedStormCell]) -> None
        self.on_systems_updated: Optional[Callable] = None  # (systems: list[MCSSystem]) -> None
        # (direction_deg, speed_kph) — pushed after each scan so the radar
        # renderer can compute Storm-Relative Velocity using the latest mean
        # storm motion across all tracked cells.
        self.on_motion_update: Optional[Callable] = None

        self._running = False

    def set_glm_service(self, glm_svc):
        """Wire in the GLM lightning service for flash-rate scoring."""
        self._glm_service = glm_svc

    def set_freezing_level(self, freezing_level_km: float) -> None:
        """Override the environmental 0°C height used for MESH/SHI calculation.

        Callers (e.g. an HRRR/RAP sounding ingester) can update this between
        scans; the default is warm-season convection.
        """
        if freezing_level_km > 0:
            self._freezing_level_km = float(freezing_level_km)

    def load_rotation_model(self, model_path: Optional[str] = None) -> bool:
        """Attempt to load a trained ML rotation classifier from disk.

        Returns True on success.  Safe to call when no model exists — the
        service stays in pure physics mode in that case.
        """
        try:
            from pathlib import Path
            import sys
            project_root = Path(__file__).resolve().parents[2]
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            from scripts.train_rotation_model import (
                load_rotation_model as _load,
                FEATURE_NAMES,
            )
            model = _load() if model_path is None else None
            if model_path is not None:
                import joblib
                model = joblib.load(model_path)
            if model is None:
                logger.info("No rotation ML model found — running pure physics")
                return False
            self._rotation_model = model
            self._rotation_model_features = list(FEATURE_NAMES)
            logger.info(
                f"Loaded ML rotation model with {len(FEATURE_NAMES)} features"
            )
            return True
        except Exception as e:
            logger.warning(f"Could not load ML rotation model: {e}")
            return False

    @staticmethod
    def _cell_to_feature_vector(
        cell: "TrackedStormCell", feature_names: list[str]
    ) -> list[float]:
        """Build the feature vector consumed by the trained classifier.

        Mirrors the schema in live_qa.extract_features.  Unknown features
        default to 0.0 so trained models with a superset of fields still work.
        """
        peak_profile_vel = 0.0
        if cell.rotation_profile:
            peak_profile_vel = max(
                (p.get("rot_velocity_ms", 0.0) for p in cell.rotation_profile),
                default=0.0,
            )

        # MRMS multi-radar rotation features — sampled at the cell's lat/lon
        # from the cached MRMS rotation service.  Both default to 0.0 when
        # MRMS is unavailable so the model handles missing data gracefully.
        mrms_rot = 0.0
        mrms_azshear = 0.0
        try:
            from backend.services.mrms_rotation_service import get_mrms_rotation_service
            svc = get_mrms_rotation_service()
            if svc is not None and svc.available:
                rt = svc.get_rotation_track_at(float(cell.lat), float(cell.lon))
                if rt is not None:
                    mrms_rot = float(rt)
                az = svc.get_azshear_at(float(cell.lat), float(cell.lon))
                if az is not None:
                    mrms_azshear = float(az)
        except Exception:
            pass

        feat: dict[str, float] = {
            "max_dbz":              float(cell.max_reflectivity_dbz or 0),
            "area_km2":             float(cell.area_km2 or 0),
            "vil_kg_m2":            float(cell.vil_kg_m2 or 0),
            "cell_top_km":          float(cell.cell_top_km or 0),
            "cell_base_km":         float(cell.cell_base_km or 0),
            "depth_km":             float(cell.depth_km or 0),
            "max_ref_height_km":    float(cell.max_ref_height_km or 0),
            "centroid_height_km":   float(cell.centroid_height_km or 0),
            "mean_cc":              0.0,  # not currently stored on the cell
            "min_cc":               0.0,
            "mean_zdr":             0.0,
            "rot_velocity_ms":      float(cell.rotation_velocity_ms or 0),
            "llsd_max_shear":       float(cell.llsd_max_shear or 0),
            "llsd_elevation_deg":   float(cell.llsd_elevation_deg or 0),
            "max_rot_vel_profile_ms": float(
                cell.max_rot_velocity_ms or peak_profile_vel
            ),
            "max_rot_height_km":    float(cell.max_rot_height_km or 0),
            "rotation_depth_km":    float(cell.rotation_depth_km or 0),
            "motion_speed_kph":     float(cell.motion_speed_kph or 0),
            "motion_dir_deg":       float(cell.motion_direction_deg or 0),
            "score_rotation":       float((cell.score_breakdown or {}).get("rotation", 0)),
            "llsd_trend":           float(cell.llsd_trend or 0),
            "rot_vel_trend":        float(cell.rot_vel_trend or 0),
            "vil_trend":            float(cell.vil_trend or 0),
            "echo_top_trend":       float(cell.echo_top_trend or 0),
            "dbz_trend":            float(cell.dbz_trend or 0),
            "mrms_rotation_track_30min": mrms_rot,
            "mrms_azshear_0_2km":        mrms_azshear,
        }
        return [feat.get(name, 0.0) for name in feature_names]

    def _apply_rotation_model(self, cells: list["TrackedStormCell"]) -> None:
        """Run the loaded ML classifier on each cell and store probabilities.

        Probability is exposed as `p_rotation_model` for downstream consumers.
        Score adjustment: scales with how strongly the model disagrees with
        the physics detectors.  Calibrated for the current ROC-AUC ~0.81
        ensemble vote rather than transformative classification — the model
        learns severe-storm proxies (VIL, area, dBZ) more than rotation
        per se, so impact is intentionally small.
        """
        if self._rotation_model is None or not self._rotation_model_features:
            return
        import numpy as np
        for cell in cells:
            if cell.scan_count < 0:
                cell.p_rotation_model = None
                continue
            try:
                row = np.array(
                    self._cell_to_feature_vector(cell, self._rotation_model_features),
                    dtype=float,
                ).reshape(1, -1)
                p = float(self._rotation_model.predict_proba(row)[0, 1])
                cell.p_rotation_model = round(p, 3)
            except Exception:
                cell.p_rotation_model = None
                continue

            # Conservative ensemble vote — only highly-confident model
            # predictions nudge the score, and the magnitude is bounded
            # tightly (±2) so the physics-driven detectors remain the
            # primary signal.  At AUC 0.81 the model is most useful as a
            # corroboration signal, not as a co-equal classifier.
            adj = 0
            if cell.p_rotation_model >= 0.80 and not cell.rotation_detected:
                adj = 2
            elif cell.p_rotation_model < 0.10 and cell.rotation_detected:
                adj = -2
            if adj:
                cell.severity_score = max(0, min(100, cell.severity_score + adj))
                cell.threat_level   = self._score_to_threat(cell.severity_score)

    @property
    def tracked_cells(self) -> list[TrackedStormCell]:
        return list(self._tracked_cells.values())

    @property
    def tracked_systems(self) -> list[MCSSystem]:
        return list(self._tracked_systems.values())

    def get_cell(self, cell_id: str) -> Optional[TrackedStormCell]:
        return self._tracked_cells.get(cell_id)

    async def process_volume(self, volume_data) -> list[TrackedStormCell]:
        """
        Process a new volume scan. Called by NexradService via callback.
        Runs cell ID + tracking + scoring in executor to avoid blocking.
        """
        loop = asyncio.get_event_loop()
        cells = await loop.run_in_executor(
            None, self._process_sync, volume_data
        )

        if cells is not None:
            if self.on_cells_updated:
                await self.on_cells_updated(cells)
            if self.on_systems_updated:
                await self.on_systems_updated(self.tracked_systems)
            if self.on_motion_update:
                motion = self._compute_mean_storm_motion(cells)
                if motion is not None:
                    await self.on_motion_update(motion[0], motion[1])
        return cells or []

    def _compute_mean_storm_motion(
        self, cells: list["TrackedStormCell"]
    ) -> Optional[tuple[float, float]]:
        """Mean storm motion (direction_deg, speed_kph) across active cells.

        Used to push a single SRV reference vector to the radar renderer.
        Weighted by severity score so a notable supercell dominates the
        average over weak embedded cells; falls back to unweighted mean
        if all scores are zero.  Returns None if no cells have motion yet.
        """
        moving = [
            c for c in cells
            if c.scan_count > 0
            and c.motion_speed_kph > 0
            and c.motion_speed_kph < MAX_STORM_SPEED_KPH
        ]
        if not moving:
            return None
        weights = [max(1.0, float(c.severity_score)) for c in moving]
        avg_speed = float(
            sum(w * c.motion_speed_kph for w, c in zip(weights, moving))
            / sum(weights)
        )
        avg_dir = self._circular_mean(
            [c.motion_direction_deg for c in moving], weights
        )
        return float(avg_dir), avg_speed

    def _process_sync(self, volume_data) -> Optional[list[TrackedStormCell]]:
        """Synchronous processing pipeline (runs in thread)."""
        try:
            grid = volume_data.grid
            radar = volume_data.radar_object
            timestamp = volume_data.timestamp
            site = volume_data.site

            # Register this radar's location for Voronoi analysis
            rad_lat, rad_lon = self._get_radar_latlon(radar)
            if rad_lat is not None:
                self._radar_locations[site] = (rad_lat, rad_lon)

            # Store bounds for image-aligned marker placement
            self._bounds = volume_data.bounds

            # Extract grid coordinate arrays
            self._extract_grid_coords(grid)

            # Step 1: Identify cells from reflectivity (volumetric SCIT; 2D fallback)
            raw_cells = self._identify_cells(grid, radar)
            if not raw_cells:
                # No cells found — only expire cells owned by this site so that
                # storms tracked by a different active radar are not wiped out.
                self._expire_cells_by_site(site, timestamp)
                self._tracked_systems = {}
                return self.tracked_cells

            # Step 2: Analyze dual-pol for each cell
            self._analyze_dual_pol(grid, radar, raw_cells)

            # Step 3: Match to existing tracked cells
            matched = self._match_cells(raw_cells, timestamp)

            # Step 3b: Phase 3 — TBSS check (hail_indicated is set inside _match_cells)
            self._check_tbss_signatures(radar, matched)

            # Step 4: Detect supercell mesocyclone / TVS rotation
            self._detect_rotation(grid, radar, matched)

            # Step 4b: Detect QLCS embedded mesos (smaller couplets along squall lines)
            self._detect_qlcs_rotation(grid, radar, matched)

            # Step 4c: Low-Level Shear Detection on the lowest polar tilt (0.5°)
            self._detect_llsd_rotation(radar, matched)

            # Step 4c2: Multi-tilt rotation profile — catches mid-level mesocyclones
            self._compute_rotation_profile(radar, matched)

            # Step 4d: Compute vertical structure (top/base/VIL/etc.) from all tilts
            self._compute_cell_structure(radar, matched)

            # Step 4e: Phase 2 — kinematic wind signatures.
            # Must run after vertical-structure so reflectivity flags are populated,
            # but before trend computation so these readings enter the snapshot.
            self._detect_downburst_signatures(radar, matched)
            self._detect_marc_signatures(radar, matched)
            self._detect_straight_line_winds(radar, matched)

            # Step 4f: Compute per-scan trend features BEFORE reconciling flags,
            # so trend data is available to scoring (and to the training JSONL).
            self._compute_trends(matched, timestamp)

            # Step 4g: Reconcile rotation flags using the best available signal.
            # Priority (highest → lowest accuracy):
            #   1. Multi-tilt profile (polar data, multiple elevation angles)
            #   2. LLSD azimuthal shear (polar data, lowest tilt, direct derivative)
            #   3. Grid-based couplet (Barnes2-blurred, used only when polar unavailable)
            # The grid-based detection sets the initial flag but the polar signals
            # override it here, after they have run.
            self._reconcile_rotation_flags(matched)

            # Step 5: Score each cell (uses both rotation + qlcs_meso flags)
            self._score_cells(matched, timestamp)

            # Step 5b: ML rotation classifier ensemble vote (inert if no model)
            self._apply_rotation_model(matched)

            # Step 6: Classify linear systems (MCS/QLCS/bow echo)
            systems = self._detect_mcs_systems(matched, timestamp)
            self._tracked_systems = {s.system_id: s for s in systems}

            # Step 6b: Phase 2 — Rear-Inflow Jet (requires bow echo from Step 6)
            self._detect_rear_inflow_jet(radar, matched, systems)

            # Step 7: Generate forecast tracks
            self._generate_forecasts(matched)

            # Update previous cells for next scan
            self._previous_cells = raw_cells

            return self.tracked_cells

        except Exception as e:
            logger.error(f"Storm tracking error: {e}", exc_info=True)
            return None

    def _get_radar_latlon(self, radar) -> tuple[Optional[float], Optional[float]]:
        """Extract the radar's latitude and longitude from a Py-ART radar object."""
        try:
            return float(radar.latitude["data"][0]), float(radar.longitude["data"][0])
        except Exception:
            return None, None

    @staticmethod
    def _get_nyquist(radar) -> float:
        """Return the radar's Nyquist velocity in m/s.

        Used to sanity-gate non-dealiased velocity: a single folded gate (e.g.
        +30 m/s wrapping to -30) creates an apparent 60 m/s ΔV that masquerades
        as extreme rotation.  Reject any Vrot > NYQ * 0.95 from raw velocity.
        Falls back to a conservative WSR-88D default if instrument parameters
        are unavailable.
        """
        try:
            nv = radar.instrument_parameters.get("nyquist_velocity")
            if nv is not None:
                data = nv.get("data") if isinstance(nv, dict) else nv["data"]
                arr = np.asarray(data, dtype=float)
                arr = arr[~np.isnan(arr)]
                if arr.size > 0:
                    return float(np.nanmean(arr))
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        return 28.0  # Conservative WSR-88D legacy VCP default

    def _is_primary_radar_for_cell(
        self, rad_lat: float, rad_lon: float, cell: "TrackedStormCell"
    ) -> bool:
        """Return True if (rad_lat, rad_lon) is the closest registered radar to cell.

        Voronoi rule: each cell is owned by its nearest radar, which has the
        lowest beam height and the most accurate dual-pol data for that location.
        """
        if len(self._radar_locations) <= 1:
            return True
        dist_self, _ = self._latlon_to_polar(rad_lat, rad_lon, cell.lat, cell.lon)
        for o_lat, o_lon in self._radar_locations.values():
            if abs(o_lat - rad_lat) < 0.01 and abs(o_lon - rad_lon) < 0.01:
                continue  # same radar
            dist_other, _ = self._latlon_to_polar(o_lat, o_lon, cell.lat, cell.lon)
            if dist_other < dist_self:
                return False
        return True

    def _expire_cells_by_site(self, site: str, timestamp: str):
        """Expire only the cells owned by *site* (Voronoi-nearest to it).

        When one of several active radars scans a quiet area, we must not
        clear cells being tracked by a different radar.
        """
        site_loc = self._radar_locations.get(site)
        if not site_loc or len(self._radar_locations) <= 1:
            self._expire_all_cells(timestamp)
            return

        rad_lat, rad_lon = site_loc
        expired = []
        for cell_id, cell in self._tracked_cells.items():
            # Only touch cells whose primary radar is this site
            cell_lat, cell_lon = cell.lat, cell.lon
            dist_self, _ = self._latlon_to_polar(rad_lat, rad_lon, cell_lat, cell_lon)
            is_primary = True
            for o_lat, o_lon in self._radar_locations.values():
                if abs(o_lat - rad_lat) < 0.01 and abs(o_lon - rad_lon) < 0.01:
                    continue
                dist_other, _ = self._latlon_to_polar(o_lat, o_lon, cell_lat, cell_lon)
                if dist_other < dist_self:
                    is_primary = False
                    break
            if not is_primary:
                continue
            if cell.scan_count > 0:
                cell.scan_count = -1
                cell.trend = "weakening"
                cell.last_updated = timestamp
            else:
                expired.append(cell_id)

        for cell_id in expired:
            del self._tracked_cells[cell_id]

    def _extract_grid_coords(self, grid):
        """Extract lat/lon arrays and geometry from the grid."""
        try:
            self._grid_lat = grid.point_latitude["data"][0]  # 2D array
            self._grid_lon = grid.point_longitude["data"][0]
            shape = self._grid_lat.shape
            self._grid_shape = shape[0]  # assume square
            # Estimate grid resolution
            if shape[0] > 1:
                dlat = abs(self._grid_lat[1, 0] - self._grid_lat[0, 0])
                self._grid_res_km = dlat * 111.0  # approx km per degree lat
            # Derive range from grid: each cell is res_km wide, N cells total
            self._range_m = (self._grid_shape / 2.0) * self._grid_res_km * 1000.0
        except Exception as e:
            logger.warning(f"Could not extract grid coords: {e}")

    def _identify_cells(self, grid, radar=None) -> list[_InternalCell]:
        """SCIT cell identification.

        Prefers the canonical **volumetric** path (per-tilt 2D components on the
        polar reflectivity of every elevation → 3D vertical association); falls
        back to the single-level 2D Cartesian method if no radar is supplied or
        the volumetric pass fails, so the pipeline never goes dark.
        """
        if radar is not None:
            try:
                cells = self._identify_cells_volumetric(grid, radar)
                if cells is not None:
                    return cells
            except Exception as e:  # never break tracking on an identification edge case
                logger.warning(f"Volumetric SCIT identification failed ({e}); using 2D fallback", exc_info=True)
        return self._identify_cells_2d(grid)

    def _identify_cells_2d(self, grid) -> list[_InternalCell]:
        """
        SCIT multi-threshold feature core extraction (Johnson et al. 1998, WAF §2b).

        The WSR-88D storm series algorithm used a single 30 dBZ threshold, causing
        all cells in a squall line or cluster to merge into one large component.
        SCIT fixes this by processing seven thresholds from 60→30 dBZ and applying
        the core extraction rule:

            If the centroid of a higher-threshold component falls within the area
            of a lower-threshold component, the lower component is discarded.

        This ensures that each distinct high-dBZ core (e.g., three 50 dBZ cores in
        a squall line that all merge into one 35 dBZ blob) is tracked individually.
        Only cells with no higher-threshold "parent" centroid survive each pass.

        Implementation note: we work on the 2D Cartesian grid (single altitude level
        from pyart.map.grid_from_radars), which is the Cartesian equivalent of SCIT's
        per-elevation-angle 2D component step.  The 3D vertical association across
        tilts is handled separately by _compute_cell_structure and _compute_rotation_profile.
        """
        from scipy import ndimage

        if "reflectivity" not in grid.fields:
            return []

        refl = grid.fields["reflectivity"]["data"][0]
        refl_filled = np.ma.filled(refl, -999.0).astype(np.float32)

        pixel_area_km2 = self._grid_res_km ** 2

        # Centroids (y, x) identified at higher thresholds — used for core extraction.
        # Each entry is the geometric centroid of a component that survived its pass.
        identified_centroids: list[tuple[int, int]] = []
        final_cells: list[_InternalCell] = []

        for threshold in SCIT_THRESHOLDS:
            binary = refl_filled >= threshold
            if not np.any(binary):
                continue

            labeled, num_features = ndimage.label(binary)
            if num_features == 0:
                continue

            for label_id in range(1, num_features + 1):
                mask = labeled == label_id

                # Area filter — rejects sub-pixel noise and AP clutter specks
                n_pixels = int(np.count_nonzero(mask))
                area_km2 = n_pixels * pixel_area_km2
                if area_km2 < CELL_MIN_AREA_KM2:
                    continue

                ys, xs = np.where(mask)
                cell_refl = refl_filled[ys, xs]
                max_dbz = float(np.nanmax(cell_refl))
                # Reflectivity-mass-weighted centroid (SCIT spec): weight by linear
                # reflectivity factor so the centroid sits on the core, not the
                # geometric middle of the thresholded blob.
                w = np.power(10.0, np.clip(cell_refl, 0.0, 75.0) / 10.0)
                wsum = float(w.sum())
                if wsum > 0:
                    cy = int(round(float((w * ys).sum() / wsum)))
                    cx = int(round(float((w * xs).sum() / wsum)))
                else:
                    cy = int(round(float(ys.mean())))
                    cx = int(round(float(xs.mean())))

                # ── Feature core extraction ───────────────────────────────────
                # "If the centroid of a higher-reflectivity thresholded component
                #  falls within the area of a lower-reflectivity thresholded
                #  component, the latter component is discarded." (Johnson 1998)
                #
                # Because we process high→low, `identified_centroids` holds only
                # centroids from thresholds ABOVE the current one.  A centroid
                # from a 50 dBZ component will always lie inside the 35 dBZ blob
                # that surrounds it, so the 35 dBZ blob is correctly discarded.
                redundant = any(mask[pcy, pcx] for pcy, pcx in identified_centroids)
                if redundant:
                    continue

                # This component is a new unique cell at this threshold (max_dbz +
                # cell_refl computed above for the mass-weighted centroid).
                try:
                    lat, lon = self._grid_to_image_latlon(cy, cx)
                except (IndexError, TypeError, ValueError):
                    continue

                bbox = (int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max()))

                # Sub-area statistics used downstream for hail scoring
                dbz_50_area = float(np.count_nonzero(cell_refl >= 50)) * pixel_area_km2
                dbz_55_area = float(np.count_nonzero(cell_refl >= 55)) * pixel_area_km2
                dbz_60_area = float(np.count_nonzero(cell_refl >= 60)) * pixel_area_km2

                final_cells.append(_InternalCell(
                    centroid_y=cy,
                    centroid_x=cx,
                    lat=lat,
                    lon=lon,
                    max_dbz=max_dbz,
                    area_km2=area_km2,
                    pixel_mask=mask,
                    bbox=bbox,
                    dbz_50_area_km2=dbz_50_area,
                    dbz_55_area_km2=dbz_55_area,
                    dbz_60_area_km2=dbz_60_area,
                ))
                identified_centroids.append((cy, cx))

        logger.debug(
            f"SCIT identified {len(final_cells)} storm cells "
            f"across {len(SCIT_THRESHOLDS)} thresholds"
        )
        return final_cells

    # ──────────────────────────────────────────────────────────────────────
    # Volumetric SCIT identification (Johnson et al. 1998 + WSR-88D ROC specs)
    # ──────────────────────────────────────────────────────────────────────
    def _identify_cells_volumetric(self, grid, radar) -> Optional[list[_InternalCell]]:
        """Canonical volumetric SCIT.

        Extracts 2D reflectivity components per elevation from the *polar* radar
        (7 thresholds high→low, mass-weighted centroids), associates them
        vertically into 3D cells (escalating search radii + vertical coherence),
        de-crowds, then maps each cell onto the Cartesian grid and synthesises a
        per-cell pixel mask so the existing dual-pol / rotation / hail / scoring
        pipeline runs unchanged.
        """
        from scipy import ndimage

        if "reflectivity" not in radar.fields:
            return None
        if self._grid_shape is None or self._range_m is None:
            return None

        rng_km = np.asarray(radar.range["data"], dtype=np.float32) / 1000.0
        ngates = rng_km.size
        if ngates < 2:
            return None
        gate_km = float(rng_km[1] - rng_km[0])

        # ── Step 1: per-tilt 2D components on the polar reflectivity ───────────
        tilt_comps: list[dict] = []
        try:
            fixed = list(radar.fixed_angle["data"])
        except Exception:
            fixed = [0.0] * radar.nsweeps

        seen_elev: list[float] = []
        layer = 0
        for sweep in range(radar.nsweeps):
            try:
                elev = float(fixed[sweep])
            except (TypeError, ValueError, IndexError):
                elev = 0.0
            if elev > SCIT_MAX_ELEV_DEG or elev < 0:
                continue
            if any(abs(elev - e) < 0.15 for e in seen_elev):
                continue  # skip duplicate split-cut elevations
            try:
                refl = radar.get_field(sweep, "reflectivity")
                az = radar.get_azimuth(sweep)
            except Exception:
                continue
            if refl is None or refl.shape[1] != ngates:
                continue
            comps = self._tilt_components(ndimage, refl, az, rng_km, gate_km, elev, layer)
            if comps:
                tilt_comps.extend(comps)
                seen_elev.append(elev)
                layer += 1

        if not tilt_comps:
            return []

        # ── Step 2: vertical association into 3D cells, then de-crowd ──────────
        cells_xy = self._associate_3d(tilt_comps)
        if not cells_xy:
            return []

        # ── Step 3: build grid-compatible _InternalCell list ──────────────────
        return self._cells_from_xy(ndimage, grid, cells_xy)

    def _tilt_components(self, ndimage, refl, az, rng_km, gate_km, elev, layer) -> list[dict]:
        """Extract 2D reflectivity components for one elevation (polar, vectorized).

        Returns dicts with mass-weighted (x, y) centroids in km east/north of the
        radar, plus max dBZ, footprint area, the elevation and a vertical layer index.
        """
        filled = np.ma.filled(refl, -999.0).astype(np.float32)   # (nray, ngate)
        nray = filled.shape[0]
        if nray < SCIT_COMP_MIN_SEGMENTS:
            return []
        order = np.argsort(az)
        filled = filled[order]
        azs = np.asarray(az, dtype=np.float32)[order]
        az_step_rad = math.radians(360.0 / max(nray, 1))
        azr = np.radians(azs)                                      # (nray,)

        struct_close = np.ones((1, 2 * SCIT_SEG_DROPOUT_GATES + 1), dtype=bool)
        struct_lbl = np.ones((3, 3), dtype=bool)                  # 8-connectivity

        identified: list[tuple[float, float]] = []                # higher-thr centroids
        comps: list[dict] = []
        for T in SCIT_THRESHOLDS:
            binary = filled >= T
            if not binary.any():
                continue
            binary = ndimage.binary_closing(binary, structure=struct_close)
            labeled, n = ndimage.label(binary, structure=struct_lbl)
            if n == 0:
                continue
            # Per-label stats in one vectorized pass (bincount) — no per-label rescans.
            rr, gg = np.nonzero(labeled)
            if rr.size == 0:
                continue
            labs = labeled[rr, gg].astype(np.int64)
            vals = filled[rr, gg]
            rk = rng_km[gg]
            a = azr[rr]
            w = np.power(10.0, np.clip(vals, 0.0, 75.0) / 10.0)
            garea = gate_km * (rk * az_step_rad)
            nlab = n + 1
            sw = np.bincount(labs, weights=w, minlength=nlab)
            swx = np.bincount(labs, weights=w * (rk * np.sin(a)), minlength=nlab)
            swy = np.bincount(labs, weights=w * (rk * np.cos(a)), minlength=nlab)
            sar = np.bincount(labs, weights=garea, minlength=nlab)
            smax = np.full(nlab, -999.0)
            np.maximum.at(smax, labs, vals)
            # distinct radials per label (segment count)
            ukey = np.unique(labs * nray + rr.astype(np.int64))
            nrad = np.bincount((ukey // nray).astype(np.int64), minlength=nlab)
            for lid in range(1, nlab):
                if nrad[lid] < SCIT_COMP_MIN_SEGMENTS:
                    continue
                area = float(sar[lid])
                if area < SCIT_COMP_MIN_AREA_KM2 or sw[lid] <= 0:
                    continue
                cx = float(swx[lid] / sw[lid])
                cy = float(swy[lid] / sw[lid])
                eff_r = math.sqrt(area / math.pi)
                # Nested-threshold core extraction: discard this (lower) component if
                # a higher-threshold centroid lies within its footprint.
                if any((px - cx) ** 2 + (py - cy) ** 2 <= eff_r * eff_r for (px, py) in identified):
                    continue
                comps.append({
                    "x": cx, "y": cy, "maxdbz": float(smax[lid]),
                    "area": area, "elev": elev, "layer": layer,
                })
                identified.append((cx, cy))
        return comps

    def _associate_3d(self, comps: list[dict]) -> list[dict]:
        """Associate per-tilt components into 3D cells (union-find on horizontal
        proximity across elevations, escalating radii), apply the vertical-coherence
        gate, mass-weight the centroid, and de-crowd."""
        n = len(comps)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        # Bind each component to its nearest neighbour on a *different* tilt within
        # the escalating search radius (tight first → stacked cores bind before sheared).
        for radius in SCIT_VERT_RADII_KM:
            r2 = radius * radius
            for i in range(n):
                ci = comps[i]
                best_j, best_d = -1, r2
                for j in range(n):
                    cj = comps[j]
                    if i == j or cj["layer"] == ci["layer"] or find(i) == find(j):
                        continue
                    d = (ci["x"] - cj["x"]) ** 2 + (ci["y"] - cj["y"]) ** 2
                    if d <= best_d:
                        best_d, best_j = d, j
                if best_j >= 0:
                    union(i, best_j)

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        cells: list[dict] = []
        for members in groups.values():
            layers = {comps[m]["layer"] for m in members}
            maxd = max(comps[m]["maxdbz"] for m in members)
            # Vertical coherence: need ≥2 tilts, unless it's a strong core only the
            # lowest tilt can see (distant storms the higher beams overshoot).
            if len(layers) < SCIT_CELL_MIN_COMPONENTS and maxd < 40.0:
                continue
            wsum = sx = sy = 0.0
            for m in members:
                c = comps[m]
                wt = c["area"] * (10.0 ** (min(c["maxdbz"], 75.0) / 10.0))
                wsum += wt; sx += wt * c["x"]; sy += wt * c["y"]
            if wsum <= 0:
                continue
            low_layer = min(comps[m]["layer"] for m in members)
            area = max(comps[m]["area"] for m in members if comps[m]["layer"] == low_layer)
            cells.append({"x": sx / wsum, "y": sy / wsum, "maxdbz": maxd,
                          "area": area, "ntilts": len(layers)})

        # De-crowd: keep the stronger of any two cells closer than SCIT_DECROWD_KM.
        cells.sort(key=lambda c: (c["maxdbz"], c["area"]), reverse=True)
        kept: list[dict] = []
        dc2 = SCIT_DECROWD_KM * SCIT_DECROWD_KM
        for c in cells:
            if all((c["x"] - k["x"]) ** 2 + (c["y"] - k["y"]) ** 2 > dc2 for k in kept):
                kept.append(c)
        return kept

    def _cells_from_xy(self, ndimage, grid, cells_xy: list[dict]) -> list[_InternalCell]:
        """Map 3D cells (x,y km from radar) onto the grid + synthesise pixel masks
        (grid ≥30 dBZ blob, Voronoi-split when cells share a blob)."""
        from collections import defaultdict

        if "reflectivity" not in grid.fields:
            return []
        grid_refl = np.ma.filled(grid.fields["reflectivity"]["data"][0], -999.0).astype(np.float32)
        gshape = grid_refl.shape[0]
        res_m = self._grid_res_km * 1000.0
        range_m = self._range_m
        pixel_area = self._grid_res_km ** 2

        def to_grid(c):
            gx = int(round((c["x"] * 1000.0 + range_m) / res_m))
            gy = int(round((c["y"] * 1000.0 + range_m) / res_m))
            return (max(0, min(gshape - 1, gy)), max(0, min(gshape - 1, gx)))

        blobs, _ = ndimage.label(grid_refl >= 30.0)
        cell_pos = [to_grid(c) for c in cells_xy]
        blob_of = [int(blobs[gy, gx]) for (gy, gx) in cell_pos]
        shared: dict[int, list[int]] = defaultdict(list)
        for idx, b in enumerate(blob_of):
            if b > 0:
                shared[b].append(idx)

        cells: list[_InternalCell] = []
        for idx, c in enumerate(cells_xy):
            gy, gx = cell_pos[idx]
            b = blob_of[idx]
            if b > 0 and len(shared[b]) == 1:
                mask = blobs == b
            elif b > 0:
                bys, bxs = np.where(blobs == b)
                members = shared[b]
                cyx = np.array([cell_pos[m] for m in members])         # (k, 2) = (y, x)
                d = (bys[:, None] - cyx[None, :, 0]) ** 2 + (bxs[:, None] - cyx[None, :, 1]) ** 2
                mine = np.argmin(d, axis=1) == members.index(idx)
                mask = np.zeros((gshape, gshape), dtype=bool)
                mask[bys[mine], bxs[mine]] = True
            else:
                eff_px = max(2, int(round(math.sqrt(max(c["area"], 1.0) / math.pi) / self._grid_res_km)))
                yy, xx = np.ogrid[:gshape, :gshape]
                mask = (yy - gy) ** 2 + (xx - gx) ** 2 <= eff_px * eff_px

            if not mask.any():
                mask = np.zeros((gshape, gshape), dtype=bool)
                mask[gy, gx] = True

            ys, xs = np.where(mask)
            cell_refl = grid_refl[ys, xs]
            try:
                lat, lon = self._grid_to_image_latlon(gy, gx)
            except (IndexError, TypeError, ValueError):
                continue
            cells.append(_InternalCell(
                centroid_y=gy, centroid_x=gx, lat=lat, lon=lon,
                max_dbz=float(c["maxdbz"]),
                area_km2=float(mask.sum()) * pixel_area,
                pixel_mask=mask,
                bbox=(int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())),
                dbz_50_area_km2=float(np.count_nonzero(cell_refl >= 50)) * pixel_area,
                dbz_55_area_km2=float(np.count_nonzero(cell_refl >= 55)) * pixel_area,
                dbz_60_area_km2=float(np.count_nonzero(cell_refl >= 60)) * pixel_area,
            ))
        logger.debug(f"Volumetric SCIT: {len(cells)} cells from {len(cells_xy)} 3D candidates")
        return cells

    def _analyze_dual_pol(self, grid, radar, cells: list[_InternalCell]):
        """
        Analyze dual-pol products within each cell.

        Phase 3 upgrade: in addition to whole-cell mean CC/ZDR (used for the
        legacy fallback hail check), performs a strict pixel-level co-location
        test within the Z > 55 dBZ sub-region:
            CC ∈ [HAIL_STRICT_CC_MIN, HAIL_STRICT_CC_MAX]  AND  |ZDR| ≤ HAIL_STRICT_ZDR_MAX
        Pixels passing both gates are counted in `cell.hail_core_pixels`.
        """
        has_cc  = "cross_correlation_ratio" in grid.fields
        has_zdr = "differential_reflectivity" in grid.fields
        has_ref = "reflectivity" in grid.fields

        if not has_cc and not has_zdr:
            return

        cc_data   = None
        zdr_data  = None
        refl_data = None
        if has_cc:
            cc_data   = np.ma.filled(grid.fields["cross_correlation_ratio"]["data"][0], np.nan)
        if has_zdr:
            zdr_data  = np.ma.filled(grid.fields["differential_reflectivity"]["data"][0], np.nan)
        if has_ref:
            refl_data = np.ma.filled(grid.fields["reflectivity"]["data"][0], np.nan)

        for cell in cells:
            mask = cell.pixel_mask

            # ── Whole-cell stats (legacy fallback) ────────────────────────────
            if cc_data is not None:
                cc_vals  = cc_data[mask]
                cc_valid = cc_vals[~np.isnan(cc_vals)]
                if len(cc_valid) > 0:
                    cell.mean_cc = float(np.nanmean(cc_valid))
                    cell.min_cc  = float(np.nanmin(cc_valid))

            if zdr_data is not None:
                zdr_vals  = zdr_data[mask]
                zdr_valid = zdr_vals[~np.isnan(zdr_vals)]
                if len(zdr_valid) > 0:
                    cell.mean_zdr = float(np.nanmean(zdr_valid))

            # ── Phase 3: strict pixel-level co-location inside the Z > 55 sub-region ──
            # Only possible when all three fields are available.
            if refl_data is not None and cc_data is not None and zdr_data is not None:
                high_dbz_mask = mask & (refl_data >= HAIL_REFLECTIVITY_DBZ)
                if np.any(high_dbz_mask):
                    cc_core  = cc_data[high_dbz_mask]
                    zdr_core = zdr_data[high_dbz_mask]
                    strict   = (
                        (cc_core  >= HAIL_STRICT_CC_MIN)  &
                        (cc_core  <= HAIL_STRICT_CC_MAX)  &
                        (np.abs(zdr_core) <= HAIL_STRICT_ZDR_MAX) &
                        ~np.isnan(cc_core) &
                        ~np.isnan(zdr_core)
                    )
                    cell.hail_core_pixels = int(np.count_nonzero(strict))

    def _match_cells(self, new_cells: list[_InternalCell], timestamp: str) -> list[TrackedStormCell]:
        """Match new cells to existing tracked cells using centroid proximity."""
        now = timestamp
        updated_tracked = {}
        matched_new_indices = set()
        matched_old_ids = set()

        # Build list of existing cells with predicted positions
        existing = list(self._tracked_cells.values())

        # Match against each existing cell's PROJECTED position (SCIT first-guess:
        # advance the cell along its own motion to where it *should* be this volume,
        # then match to the nearest detection). This stops a fast cell from being
        # mis-paired to a closer-but-different cell, which inflated motion vectors.
        def _projected(old_cell) -> tuple[float, float]:
            if old_cell.scan_count >= 2 and old_cell.motion_speed_kph > 0:
                try:
                    dt_h = max((datetime.fromisoformat(now) - datetime.fromisoformat(old_cell.last_updated)).total_seconds() / 3600.0, 0.0)
                except (ValueError, TypeError):
                    dt_h = 5.0 / 60.0
                dist = old_cell.motion_speed_kph * dt_h
                br = math.radians(old_cell.motion_direction_deg)
                plat = old_cell.lat + (dist * math.cos(br)) / 111.0
                plon = old_cell.lon + (dist * math.sin(br)) / (111.0 * math.cos(math.radians(old_cell.lat)) or 1.0)
                return plat, plon
            return old_cell.lat, old_cell.lon

        if existing and new_cells:
            # Distance from each existing cell's projected position to each detection.
            distances = np.zeros((len(existing), len(new_cells)))
            for i, old_cell in enumerate(existing):
                plat, plon = _projected(old_cell)
                for j, new_cell in enumerate(new_cells):
                    distances[i, j] = self._haversine_km(plat, plon, new_cell.lat, new_cell.lon)

            # Greedy matching: pick closest pairs under threshold
            while True:
                if distances.size == 0:
                    break
                min_idx = np.unravel_index(np.argmin(distances), distances.shape)
                min_dist = distances[min_idx]
                if min_dist > MAX_MATCH_DISTANCE_KM:
                    break

                old_idx, new_idx = min_idx
                old_cell = existing[old_idx]
                new_cell = new_cells[new_idx]

                matched_old_ids.add(old_cell.cell_id)
                matched_new_indices.add(new_idx)

                # Update tracked cell with new data
                updated = self._update_tracked_cell(old_cell, new_cell, now)
                updated_tracked[updated.cell_id] = updated

                # Remove matched pair from consideration
                distances[old_idx, :] = float("inf")
                distances[:, new_idx] = float("inf")

        # Create new cells for unmatched detections
        for j, new_cell in enumerate(new_cells):
            if j not in matched_new_indices:
                cell_id = f"CELL-{uuid.uuid4().hex[:8].upper()}"
                tracked = TrackedStormCell(
                    cell_id=cell_id,
                    lat=new_cell.lat,
                    lon=new_cell.lon,
                    max_reflectivity_dbz=new_cell.max_dbz,
                    area_km2=new_cell.area_km2,
                    severity_score=0,
                    threat_level="minimal",
                    motion_direction_deg=0.0,
                    motion_speed_kph=0.0,
                    rotation_detected=False,
                    rotation_velocity_ms=None,
                    tvs_detected=False,
                    qlcs_meso_detected=False,
                    qlcs_meso_velocity_ms=None,
                    hail_indicated=False,
                    hail_max_dbz=new_cell.max_dbz if new_cell.max_dbz >= HAIL_REFLECTIVITY_DBZ else None,
                    debris_signature=False,
                    vil_kg_m2=None,
                    cell_top_km=None,
                    track_history=[{"lat": new_cell.lat, "lon": new_cell.lon, "timestamp": now}],
                    forecast_track=[],
                    score_breakdown={},
                    first_detected=now,
                    last_updated=now,
                    trend="steady",
                    scan_count=1,
                )
                updated_tracked[cell_id] = tracked

        # Dead reckoning: unmatched cells live for up to 2 missed scans, with their
        # position advanced along the last known motion vector.  This survives brief
        # beam blockage / tilt gaps without losing the cell_id.
        for old_cell in existing:
            if old_cell.cell_id not in matched_old_ids:
                missed = -old_cell.scan_count + 1 if old_cell.scan_count < 0 else 1
                if missed > 2:
                    continue  # give up after 2 missed scans
                # Extrapolate position along motion vector
                try:
                    old_time = datetime.fromisoformat(old_cell.last_updated)
                    new_time = datetime.fromisoformat(now)
                    dt_hours = max((new_time - old_time).total_seconds() / 3600, 0.0)
                except (ValueError, TypeError):
                    dt_hours = 5.0 / 60.0
                if old_cell.motion_speed_kph > 0 and dt_hours > 0:
                    dist_km = old_cell.motion_speed_kph * dt_hours
                    bearing_rad = math.radians(old_cell.motion_direction_deg)
                    dlat = (dist_km * math.cos(bearing_rad)) / 111.0
                    dlon = (dist_km * math.sin(bearing_rad)) / (
                        111.0 * math.cos(math.radians(old_cell.lat)) or 1.0
                    )
                    old_cell.lat = round(old_cell.lat + dlat, 4)
                    old_cell.lon = round(old_cell.lon + dlon, 4)
                old_cell.scan_count = -missed  # -1 first miss, -2 second miss
                old_cell.trend = "weakening"
                old_cell.last_updated = now
                updated_tracked[old_cell.cell_id] = old_cell

        self._tracked_cells = updated_tracked
        return list(updated_tracked.values())

    def _update_tracked_cell(
        self, old: TrackedStormCell, new: _InternalCell, timestamp: str
    ) -> TrackedStormCell:
        """Update a tracked cell with new scan data."""
        # Motion vector from old to new position
        dist_km = self._haversine_km(old.lat, old.lon, new.lat, new.lon)
        bearing = self._bearing_deg(old.lat, old.lon, new.lat, new.lon)

        # Estimate time between scans
        try:
            old_time = datetime.fromisoformat(old.last_updated)
            new_time = datetime.fromisoformat(timestamp)
            dt_seconds = (new_time - old_time).total_seconds()
            dt_hours = max(dt_seconds / 3600.0, 0.001)
        except (ValueError, TypeError):
            dt_seconds = 300.0
            dt_hours = 5.0 / 60.0

        # Guard: if two active radars produce scans seconds apart, the same
        # storm cell will match across sites with a tiny Δt and a small
        # parallax offset → hundreds of kph.  Below MIN_MOTION_DT_SECONDS,
        # keep the previous motion vector rather than computing a new one.
        if dt_seconds < MIN_MOTION_DT_SECONDS:
            speed_kph = old.motion_speed_kph
            bearing = old.motion_direction_deg
        else:
            speed_kph = min(dist_km / dt_hours, MAX_STORM_SPEED_KPH)

            # Smooth motion with previous motion
            if old.scan_count > 1 and old.motion_speed_kph > 0:
                # Weighted average: 60% new, 40% old
                speed_kph = 0.6 * speed_kph + 0.4 * old.motion_speed_kph
                # Circular mean for direction
                bearing = self._circular_mean(
                    [old.motion_direction_deg, bearing], [0.4, 0.6]
                )

        # Determine trend based on max reflectivity change
        dbz_change = new.max_dbz - old.max_reflectivity_dbz
        if dbz_change > 3:
            trend = "strengthening"
        elif dbz_change < -3:
            trend = "weakening"
        else:
            trend = "steady"

        # Hail indicator from dual-pol (Phase 3 upgrade)
        hail = False
        if new.max_dbz >= HAIL_REFLECTIVITY_DBZ:
            # Primary path (Phase 3): strict pixel-level co-location of
            # Z > 55 dBZ  +  |ZDR| ≤ 0.5 dB  +  CC ∈ [0.85, 0.95].
            # Requires all three dual-pol fields present in the grid.
            if new.hail_core_pixels >= HAIL_STRICT_MIN_PX:
                hail = True
            # Secondary: classic indicators (conjunction, not disjunction, to
            # reduce false positives from pure-rain high-dBZ cells).
            elif (
                new.mean_cc is not None
                and HAIL_STRICT_CC_MIN <= new.mean_cc <= HAIL_STRICT_CC_MAX
                and new.mean_zdr is not None
                and abs(new.mean_zdr) <= HAIL_ZDR_THRESHOLD
            ):
                hail = True
            # Fallback: extreme reflectivity alone is a strong hail proxy
            # when dual-pol data is unavailable or ambiguous.
            if new.max_dbz >= 60:
                hail = True

        # Track history
        history = old.track_history.copy()
        history.append({"lat": new.lat, "lon": new.lon, "timestamp": timestamp})
        if len(history) > self._history_max_scans:
            history = history[-self._history_max_scans:]

        old.lat = new.lat
        old.lon = new.lon
        old.max_reflectivity_dbz = new.max_dbz
        old.area_km2 = new.area_km2
        old.motion_direction_deg = bearing
        old.motion_speed_kph = speed_kph
        old.hail_indicated = hail
        old.hail_max_dbz = new.max_dbz if hail else None
        old.track_history = history
        old.last_updated = timestamp
        old.trend = trend
        old.scan_count = max(old.scan_count, 0) + 1

        return old

    def _detect_rotation(self, grid, radar, cells: list[TrackedStormCell]):
        """Detect rotation signatures (mesocyclones/TVS) in velocity data."""
        from scipy.ndimage import uniform_filter

        # Check for velocity field
        vel_field = None
        for fname in ["velocity_dealiased", "velocity"]:
            if fname in grid.fields:
                vel_field = fname
                break

        if vel_field is None:
            return

        is_dealiased = vel_field == "velocity_dealiased"
        nyq = self._get_nyquist(radar)
        if not is_dealiased:
            logger.debug(
                f"Grid rotation: using non-dealiased velocity (Nyquist {nyq:.1f} m/s); "
                "applying aliasing sanity gate"
            )

        vel_data = np.ma.filled(grid.fields[vel_field]["data"][0], np.nan)
        refl_data = None
        if "reflectivity" in grid.fields:
            refl_data = np.ma.filled(grid.fields["reflectivity"]["data"][0], np.nan)

        cc_data = None
        if "cross_correlation_ratio" in grid.fields:
            cc_data = np.ma.filled(grid.fields["cross_correlation_ratio"]["data"][0], np.nan)

        rad_lat, rad_lon = self._get_radar_latlon(radar)

        # Pre-compute lowest elevation angle for beam-height calculations
        min_elev_deg = 0.5
        try:
            min_elev_deg = float(min(radar.fixed_angle["data"]))
        except Exception:
            pass

        for cell in cells:
            if cell.scan_count < 0:  # Skip dissipating cells
                continue

            # Voronoi: only analyse cells whose primary radar is this one.
            # Cells closer to another active radar are handled when that radar
            # processes — analysing them here would use the wrong grid geometry.
            if rad_lat is not None and not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            # Get cell region from grid coordinates
            try:
                cy, cx = self._latlon_to_grid(cell.lat, cell.lon)
            except (ValueError, IndexError):
                continue

            # Search area: cell extent + 5km buffer
            buffer_px = max(int(5.0 / self._grid_res_km), 3)
            y_min = max(0, cy - buffer_px)
            y_max = min(vel_data.shape[0], cy + buffer_px)
            x_min = max(0, cx - buffer_px)
            x_max = min(vel_data.shape[1], cx + buffer_px)

            region_vel = vel_data[y_min:y_max, x_min:x_max]
            valid_mask = ~np.isnan(region_vel)

            if np.sum(valid_mask) < 10:
                continue

            # NaN-aware 3×3 mean smoothing.  median_filter (the previous
            # approach) does NOT ignore NaNs, so a single missing gate would
            # propagate through the kernel and contaminate argmax/argmin.
            # Here we compute mean = Σ(valid · v) / Σ(valid), restricted to
            # windows where at least 5 of 9 neighbours are valid.
            vel_zeroed = np.where(valid_mask, region_vel, 0.0).astype(np.float32)
            mask_f = valid_mask.astype(np.float32)
            count = uniform_filter(mask_f, size=3, mode="constant", cval=0.0)
            sum_smooth = uniform_filter(vel_zeroed, size=3, mode="constant", cval=0.0)
            smoothed_vel = np.full_like(vel_zeroed, np.nan, dtype=np.float32)
            np.divide(
                sum_smooth, count,
                out=smoothed_vel,
                where=count >= (5.0 / 9.0) - 1e-6,
            )

            if np.all(np.isnan(smoothed_vel)):
                continue

            # Find extrema on the smoothed field
            max_outbound = float(np.nanmax(smoothed_vel))
            max_inbound  = float(np.nanmin(smoothed_vel))

            # Rotational velocity = (outbound - inbound) / 2
            rot_velocity = (max_outbound - max_inbound) / 2

            # Aliasing sanity gate: a single folded gate produces an apparent
            # ΔV of ~2·Nyquist.  When using raw (non-dealiased) velocity,
            # reject Vrot beyond 0.95·Nyquist — physically real rotation that
            # strong should arrive on the dealiased product.
            if not is_dealiased and rot_velocity > nyq * 0.95:
                logger.debug(
                    f"Rotation rejected for {cell.cell_id}: Vrot={rot_velocity:.1f} m/s "
                    f"near Nyquist ({nyq:.1f} m/s) on non-dealiased velocity — likely aliased"
                )
                continue

            # Check couplet diameter (distance between max inbound and outbound)
            if rot_velocity >= MESO_VELOCITY_THRESHOLD_MS:
                outbound_pos = np.unravel_index(
                    np.nanargmax(smoothed_vel), smoothed_vel.shape
                )
                inbound_pos = np.unravel_index(
                    np.nanargmin(smoothed_vel), smoothed_vel.shape
                )
                couplet_dist_px = math.sqrt(
                    (outbound_pos[0] - inbound_pos[0]) ** 2
                    + (outbound_pos[1] - inbound_pos[1]) ** 2
                )
                couplet_dist_km = couplet_dist_px * self._grid_res_km

                # Minimum separation: a 1-pixel pair on adjacent gates is
                # almost always noise, not a real couplet.
                if couplet_dist_px < 2.0:
                    continue

                # Straddling check: the in/out extrema must be on OPPOSITE
                # sides of the cell centroid.  Compute the midpoint of the
                # extrema and require it lies within 40% of the couplet
                # length from the cell centre.  Without this check, a pair
                # of inbound + outbound peaks on the same flank (e.g. a
                # sheared inflow notch) is mis-flagged as a meso.
                center_y = cy - y_min
                center_x = cx - x_min
                mid_y = (outbound_pos[0] + inbound_pos[0]) / 2.0
                mid_x = (outbound_pos[1] + inbound_pos[1]) / 2.0
                mid_offset = math.sqrt(
                    (mid_y - center_y) ** 2 + (mid_x - center_x) ** 2
                )
                if mid_offset > 0.4 * couplet_dist_px:
                    logger.debug(
                        f"Couplet rejected for {cell.cell_id}: extrema on same side of cell "
                        f"(midpoint offset {mid_offset:.1f} px > 0.4 × couplet {couplet_dist_px:.1f} px)"
                    )
                    continue

                if couplet_dist_km <= MESO_MAX_DIAMETER_KM:
                    cell.rotation_detected = True
                    cell.rotation_velocity_ms = round(rot_velocity, 1)

                    # TVS check
                    if rot_velocity >= TVS_VELOCITY_THRESHOLD_MS:
                        cell.tvs_detected = True

                    # Debris signature check — NWS dual-pol TDS criteria:
                    #   1. CC < DEBRIS_CC_THRESHOLD and Z ≥ TDS_MIN_REFL_DBZ, co-located
                    #   2. Rotation ≥ TDS_MIN_ROTATION_MS (strong circulation required)
                    #   3. Beam height ≤ TDS_MAX_BEAM_HEIGHT_KM (near-surface beam)
                    #   4. (Phase 3) TDS visible on ≥ TDS_MIN_TILT_COUNT successive
                    #      low-level tilts — rules out clutter and roost scatter
                    if cc_data is not None and refl_data is not None:
                        rot_cc   = cc_data[y_min:y_max, x_min:x_max]
                        rot_refl = refl_data[y_min:y_max, x_min:x_max]

                        tds_mask = (rot_cc < DEBRIS_CC_THRESHOLD) & (rot_refl >= TDS_MIN_REFL_DBZ)
                        if np.sum(tds_mask) >= 3:
                            rot_vel_for_tds = cell.rotation_velocity_ms or 0.0
                            if rot_vel_for_tds >= TDS_MIN_ROTATION_MS:
                                tds_candidate = True

                                # Beam height guard (unchanged)
                                if rad_lat is not None:
                                    try:
                                        dist_km, _ = self._latlon_to_polar(
                                            rad_lat, rad_lon, cell.lat, cell.lon
                                        )
                                        r_m = dist_km * 1000.0
                                        R_e, k_r = 6_371_000.0, 4.0 / 3.0
                                        h_km = (
                                            r_m * math.sin(math.radians(min_elev_deg))
                                            + r_m ** 2 / (2.0 * k_r * R_e)
                                        ) / 1000.0
                                        if h_km > TDS_MAX_BEAM_HEIGHT_KM:
                                            tds_candidate = False
                                            logger.debug(
                                                f"TDS suppressed for {cell.cell_id}: "
                                                f"lowest beam at {h_km:.2f} km AGL "
                                                f"(>{TDS_MAX_BEAM_HEIGHT_KM} km)"
                                            )
                                    except Exception:
                                        pass

                                if tds_candidate:
                                    # Phase 3: multi-tilt depth / continuity check.
                                    # Requires the low-CC / high-Z signature on ≥ 2
                                    # successive low tilts; single-tilt matches are
                                    # too easily caused by ground clutter or birds.
                                    tilt_count = self._count_tds_tilts(
                                        radar, cell, rad_lat, rad_lon
                                    )
                                    cell.tds_tilt_count = tilt_count
                                    if tilt_count >= TDS_MIN_TILT_COUNT:
                                        cell.debris_signature = True
                                    else:
                                        logger.debug(
                                            f"TDS rejected for {cell.cell_id}: "
                                            f"{tilt_count} confirming tilts "
                                            f"(need ≥ {TDS_MIN_TILT_COUNT})"
                                        )
                else:
                    cell.rotation_detected = False
                    cell.rotation_velocity_ms = None
            else:
                cell.rotation_detected = False
                cell.rotation_velocity_ms = None

    def _detect_qlcs_rotation(self, grid, radar, cells: list[TrackedStormCell]):
        """
        Detect embedded QLCS mesocyclones — smaller, weaker couplets typical of
        quasi-linear convective systems.  Uses a tighter search radius and a lower
        velocity threshold than the supercell meso detector.
        Only applied to cells that don't already have a supercell meso flagged.
        """
        vel_field = None
        for fname in ["velocity_dealiased", "velocity"]:
            if fname in grid.fields:
                vel_field = fname
                break
        if vel_field is None:
            return

        is_dealiased = vel_field == "velocity_dealiased"
        nyq = self._get_nyquist(radar)

        vel_data = np.ma.filled(grid.fields[vel_field]["data"][0], np.nan)

        rad_lat, rad_lon = self._get_radar_latlon(radar)

        for cell in cells:
            # Reset flags only for cells owned by this radar.
            # Cells in another radar's Voronoi region retain their existing
            # QLCS flags from the last time their primary radar processed them.
            if rad_lat is None or self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.qlcs_meso_detected = False
                cell.qlcs_meso_velocity_ms = None

            if cell.scan_count < 0:
                continue
            # Skip if a supercell meso was already found — don't double-count
            if cell.rotation_detected:
                continue
            # Voronoi: only analyse cells owned by this radar
            if rad_lat is not None and not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            try:
                cy, cx = self._latlon_to_grid(cell.lat, cell.lon)
            except (ValueError, IndexError):
                continue

            # Tighter search radius for QLCS (typically 2–3 km couplets)
            buf_px = max(int(QLCS_SEARCH_RADIUS_KM / self._grid_res_km), 2)
            y_min = max(0, cy - buf_px)
            y_max = min(vel_data.shape[0], cy + buf_px)
            x_min = max(0, cx - buf_px)
            x_max = min(vel_data.shape[1], cx + buf_px)

            region = vel_data[y_min:y_max, x_min:x_max]
            valid = region[~np.isnan(region)]
            if len(valid) < 6:
                continue

            max_out = float(np.nanmax(valid))
            max_in  = float(np.nanmin(valid))
            rot_vel = (max_out - max_in) / 2.0

            if rot_vel < QLCS_MESO_VELOCITY_MS:
                continue

            # Aliasing sanity gate (raw velocity only)
            if not is_dealiased and rot_vel > nyq * 0.95:
                logger.debug(
                    f"QLCS rotation rejected for {cell.cell_id}: Vrot={rot_vel:.1f} m/s "
                    f"near Nyquist ({nyq:.1f} m/s) on non-dealiased velocity"
                )
                continue

            # Check couplet diameter
            out_pos = np.unravel_index(np.nanargmax(region), region.shape)
            in_pos  = np.unravel_index(np.nanargmin(region), region.shape)
            diam_km = (
                math.sqrt((out_pos[0] - in_pos[0]) ** 2 + (out_pos[1] - in_pos[1]) ** 2)
                * self._grid_res_km
            )

            if diam_km <= QLCS_MAX_DIAMETER_KM:
                cell.qlcs_meso_detected = True
                cell.qlcs_meso_velocity_ms = round(rot_vel, 1)

    def _detect_llsd_rotation(self, radar, cells: list[TrackedStormCell]):
        """
        Low-Level Shear Detection (LLSD): compute peak azimuthal shear on the
        lowest velocity sweep (typically 0.5° tilt), directly from the polar
        radar data — NOT the gridded product.

        This catches tornado-scale rotation that the gridded 1–2 km AGL layer
        misses because tornadoes live in the lowest few hundred metres.

        For each tracked cell, we:
          1. Convert cell lat/lon to radar-relative azimuth/range.
          2. Slice a window of rays × gates around that point.
          3. Compute ∂V/∂azimuth using a centred stencil: shear =
             (V[ray+k] - V[ray-k]) / arc_length,  arc_length = 2k·Δaz·range.
          4. Record the peak |shear| in the window.
        """
        if radar is None:
            return
        # Reset per-scan
        for cell in cells:
            cell.llsd_rotation_detected = False
            cell.llsd_max_shear = None
            cell.llsd_elevation_deg = None
            cell.llsd_diagnostic = None

        # Find the lowest sweep that has velocity data
        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            for cell in cells:
                cell.llsd_diagnostic = "no velocity field in radar"
            return

        is_dealiased = vel_key == "velocity_dealiased"
        nyq = self._get_nyquist(radar)

        try:
            fixed_angles = radar.fixed_angle["data"]
        except Exception:
            return

        # Pick the lowest sweep that ACTUALLY HAS velocity data.  Modern NEXRAD
        # VCPs interleave surveillance (reflectivity-only) and Doppler
        # (velocity) cuts at the same elevation — sweep 0 is typically the
        # 0.5° surveillance scan with no velocity, and sweep 1 is the matching
        # 0.5° Doppler.  Selecting by elevation alone lands on the surveillance
        # sweep, where the velocity field is fully masked and LLSD silently
        # bails for every cell.  This filter requires meaningful velocity
        # coverage before considering a sweep.
        candidate_sweeps: list[tuple[int, float]] = []
        for i in range(len(fixed_angles)):
            elev_i = float(fixed_angles[i])
            if elev_i > LLSD_MAX_ELEVATION_DEG:
                continue
            try:
                s0 = int(radar.sweep_start_ray_index["data"][i])
                s1 = int(radar.sweep_end_ray_index["data"][i])
            except Exception:
                continue
            sweep_vel = radar.fields[vel_key]["data"][s0:s1 + 1]
            n_valid = int(np.count_nonzero(~np.ma.getmaskarray(sweep_vel)))
            if n_valid >= 500:  # arbitrary minimum; well below 1 full ray
                candidate_sweeps.append((i, elev_i))
        if not candidate_sweeps:
            for cell in cells:
                cell.llsd_diagnostic = "no velocity sweep ≤ 1.2° with valid data"
            return
        sweep_idx, sweep_elev = min(candidate_sweeps, key=lambda x: x[1])

        try:
            s_start = int(radar.sweep_start_ray_index["data"][sweep_idx])
            s_end = int(radar.sweep_end_ray_index["data"][sweep_idx])
        except Exception:
            return

        azimuths = np.asarray(radar.azimuth["data"][s_start:s_end + 1], dtype=float)
        ranges_m = np.asarray(radar.range["data"], dtype=float)
        vel = np.ma.filled(
            radar.fields[vel_key]["data"][s_start:s_end + 1], np.nan
        )
        if vel.shape[0] < (2 * LLSD_KERNEL_RAYS + 3) or vel.shape[1] < 8:
            return

        # Radar origin
        try:
            rad_lat = float(radar.latitude["data"][0])
            rad_lon = float(radar.longitude["data"][0])
        except Exception:
            return

        n_rays = vel.shape[0]
        n_gates = vel.shape[1]
        gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if n_gates > 1 else 250.0

        # Centred azimuthal gradient: shear[r,g] = (V[r+k,g] - V[r-k,g]) / arc
        k = LLSD_KERNEL_RAYS
        # Circular ray indexing (wrap around 360°)
        up = np.roll(vel, -k, axis=0)
        dn = np.roll(vel, k, axis=0)
        dV = up - dn  # m/s

        # Aliasing sanity gate: a single folded gate creates |dV| ≈ 2·Nyquist.
        # When using raw (non-dealiased) velocity, mask gates where |dV| >
        # 1.5·Nyquist — that's beyond any physically plausible azimuthal
        # velocity gradient and indicates one side has wrapped.
        if not is_dealiased:
            dV = np.where(np.abs(dV) > nyq * 1.5, np.nan, dV)

        # Mean Δaz between up/down rays (degrees → radians), per row
        az_up = np.roll(azimuths, -k)
        az_dn = np.roll(azimuths, k)
        daz = (az_up - az_dn + 540.0) % 360.0 - 180.0  # signed, wrapped
        daz_rad = np.deg2rad(np.abs(daz))  # rays×1
        daz_rad = np.where(daz_rad < 1e-4, 1e-4, daz_rad)

        # Arc length per (ray, gate) = |Δaz| * range
        arc = daz_rad[:, None] * ranges_m[None, :]  # metres
        arc = np.where(arc < 1.0, 1.0, arc)  # avoid /0 at range=0
        shear = dV / arc  # /s

        # Invalidate where velocity was missing on either side
        invalid = np.isnan(up) | np.isnan(dn)
        shear[invalid] = np.nan

        # For each cell, convert lat/lon → (az, range), then window in shear grid
        for cell in cells:
            if cell.scan_count < 0:
                continue
            dist_km, bearing_deg = self._latlon_to_polar(
                rad_lat, rad_lon, cell.lat, cell.lon
            )
            dist_m = dist_km * 1000.0
            if dist_m < 5_000:
                cell.llsd_diagnostic = f"too close to radar ({dist_km:.0f} km < 5 km)"
                continue
            if dist_m > float(ranges_m[-1]):
                cell.llsd_diagnostic = f"beyond Doppler range ({dist_km:.0f} km > {ranges_m[-1]/1000:.0f} km)"
                continue
            # Voronoi: only analyse cells owned by this radar.  Don't set a
            # diagnostic here — another radar is responsible for this cell.
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            # Nearest gate
            g_idx = int(np.searchsorted(ranges_m, dist_m))
            g_idx = max(1, min(n_gates - 2, g_idx))

            # Nearest ray (azimuths may not be sorted; pick min circular distance)
            diffs = (azimuths - bearing_deg + 540.0) % 360.0 - 180.0
            r_idx = int(np.argmin(np.abs(diffs)))

            # Window size in gates ≈ LLSD_CELL_SEARCH_KM / gate_spacing
            half_g = max(
                LLSD_KERNEL_GATES,
                int((LLSD_CELL_SEARCH_KM * 1000.0) / gate_spacing_m),
            )
            # Window size in rays: convert LLSD_CELL_SEARCH_KM tangential to
            # azimuth half-angle at this range.
            if dist_m > 0:
                half_angle_rad = (LLSD_CELL_SEARCH_KM * 1000.0) / dist_m
                half_angle_deg = math.degrees(half_angle_rad)
                mean_daz = float(np.nanmean(np.abs(daz))) or 1.0
                half_r = max(LLSD_KERNEL_RAYS + 1, int(half_angle_deg / mean_daz))
            else:
                half_r = LLSD_KERNEL_RAYS + 2

            # Slice with ray wrap-around
            ray_indices = [(r_idx + d) % n_rays for d in range(-half_r, half_r + 1)]
            g_lo = max(0, g_idx - half_g)
            g_hi = min(n_gates, g_idx + half_g + 1)

            window = shear[np.ix_(ray_indices, np.arange(g_lo, g_hi))]
            if window.size == 0 or np.all(np.isnan(window)):
                cell.llsd_diagnostic = "no valid velocity in 4 km window"
                continue

            peak = float(np.nanmax(np.abs(window)))
            cell.llsd_max_shear = round(peak, 5)
            cell.llsd_elevation_deg = round(sweep_elev, 2)
            cell.llsd_diagnostic = None  # successful run
            if peak >= LLSD_MESO_SHEAR:
                cell.llsd_rotation_detected = True

    def _compute_rotation_profile(self, radar, cells: list[TrackedStormCell]):
        """
        Run couplet detection at every tilt ≤ ROTATION_PROFILE_MAX_ELEV_DEG and
        record a rotation-vs-height profile for each cell.  This catches
        mid-level mesos that don't extend to the surface (classic supercells
        often have peak rotation at 3–5 km AGL).

        Populates: max_rot_velocity_ms, max_rot_height_km, rotation_depth_km,
        rotation_profile.
        """
        if radar is None:
            return

        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            return

        is_dealiased = vel_key == "velocity_dealiased"
        nyq = self._get_nyquist(radar)

        try:
            rad_lat = float(radar.latitude["data"][0])
            rad_lon = float(radar.longitude["data"][0])
            fixed_angles = radar.fixed_angle["data"]
            ranges_m = np.asarray(radar.range["data"], dtype=float)
            n_sweeps = len(fixed_angles)
        except Exception:
            return

        R_e = 6_371_000.0
        k_refr = 4.0 / 3.0

        def beam_height_km(r_m: float, elev_deg: float) -> float:
            elev_rad = math.radians(elev_deg)
            h_m = r_m * math.sin(elev_rad) + (r_m * r_m) / (2.0 * k_refr * R_e)
            return h_m / 1000.0

        # Reset per-scan fields
        for cell in cells:
            cell.max_rot_velocity_ms = None
            cell.max_rot_height_km = None
            cell.rotation_depth_km = None
            cell.rotation_base_km = None
            cell.rotation_profile = []
        nyq_cap = nyq * 0.95

        # Candidate sweeps (low → mid level)
        candidate_sweeps = [
            (i, float(fixed_angles[i]))
            for i in range(n_sweeps)
            if float(fixed_angles[i]) <= ROTATION_PROFILE_MAX_ELEV_DEG
        ]
        if not candidate_sweeps:
            return

        for cell in cells:
            if cell.scan_count < 0:
                continue
            dist_km, bearing_deg = self._latlon_to_polar(
                rad_lat, rad_lon, cell.lat, cell.lon
            )
            dist_m = dist_km * 1000.0
            if dist_m < 5_000 or dist_m > float(ranges_m[-1]):
                continue
            # Voronoi: only build rotation profile from the cell's primary radar
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            profile = []
            meso_heights = []
            peak_rot = 0.0
            peak_height = None

            for sw_idx, elev_deg in candidate_sweeps:
                try:
                    s_start = int(radar.sweep_start_ray_index["data"][sw_idx])
                    s_end = int(radar.sweep_end_ray_index["data"][sw_idx])
                except Exception:
                    continue
                az = np.asarray(radar.azimuth["data"][s_start:s_end + 1], dtype=float)
                vel = np.ma.filled(
                    radar.fields[vel_key]["data"][s_start:s_end + 1], np.nan
                )
                if vel.shape[0] < 5:
                    continue

                # Window around cell (± angular half-width for ~4 km radius)
                g_idx = int(np.searchsorted(ranges_m, dist_m))
                g_idx = max(0, min(len(ranges_m) - 1, g_idx))
                gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if len(ranges_m) > 1 else 250.0
                half_g = max(3, int(4000.0 / gate_spacing_m))
                g_lo = max(0, g_idx - half_g)
                g_hi = min(vel.shape[1], g_idx + half_g + 1)

                half_angle_deg = math.degrees(4000.0 / max(dist_m, 1.0))
                diffs = (az - bearing_deg + 540.0) % 360.0 - 180.0
                ray_mask = np.abs(diffs) <= half_angle_deg
                if np.sum(ray_mask) < 3:
                    continue

                region = vel[np.ix_(np.where(ray_mask)[0], np.arange(g_lo, g_hi))]
                valid = region[~np.isnan(region)]
                if valid.size < 6:
                    continue

                v_out = float(np.nanmax(valid))
                v_in = float(np.nanmin(valid))
                rot_vel = (v_out - v_in) / 2.0

                # Aliasing sanity gate per-tilt
                if not is_dealiased and rot_vel > nyq_cap:
                    logger.debug(
                        f"Profile tilt {elev_deg:.1f}° skipped for {cell.cell_id}: "
                        f"Vrot={rot_vel:.1f} m/s near Nyquist ({nyq:.1f} m/s)"
                    )
                    continue

                h_km = beam_height_km(dist_m, elev_deg)
                profile.append({
                    "height_km": round(h_km, 2),
                    "elevation_deg": round(elev_deg, 2),
                    "rot_velocity_ms": round(rot_vel, 1),
                })
                if rot_vel >= MESO_VELOCITY_THRESHOLD_MS:
                    meso_heights.append(h_km)
                if rot_vel > peak_rot:
                    peak_rot = rot_vel
                    peak_height = h_km

            cell.rotation_profile = profile
            if peak_rot > 0:
                cell.max_rot_velocity_ms = round(peak_rot, 1)
                if peak_height is not None:
                    cell.max_rot_height_km = round(peak_height, 1)
            if meso_heights:
                cell.rotation_depth_km = round(max(meso_heights) - min(meso_heights), 1)
                # Lowest tilt with ≥ meso-class rotation — key for low-level
                # vs mid-level classification.
                cell.rotation_base_km = round(min(meso_heights), 2)

    def _compute_cell_structure(self, radar, cells: list[TrackedStormCell]):
        """
        Extract a vertical reflectivity profile for each cell from all radar
        tilts and derive:
          - cell_top_km     — highest elevation where column refl ≥ 18 dBZ
          - cell_base_km    — lowest  elevation where column refl ≥ 18 dBZ
          - max_ref_height_km — height of the peak dBZ in the column
          - centroid_height_km — reflectivity-weighted mean height
          - depth_km        — top − base
          - vil_kg_m2       — cell-based VIL ( ∫ 4·10⁻³ · Z^(4/7) dz, Z capped at 56 dBZ )

        Uses the 4/3-earth beam-height model:  h ≈ r·sin(θ) + r² / (2·k·R_e).
        """
        if radar is None or "reflectivity" not in radar.fields:
            return

        try:
            rad_lat = float(radar.latitude["data"][0])
            rad_lon = float(radar.longitude["data"][0])
            fixed_angles = radar.fixed_angle["data"]
            ranges_m = np.asarray(radar.range["data"], dtype=float)
            n_sweeps = len(fixed_angles)
        except Exception:
            return

        refl_all = np.ma.filled(radar.fields["reflectivity"]["data"], np.nan)
        ECHO_MIN_DBZ = 18.0
        VIL_Z_CAP_DBZ = 56.0   # Standard NSSL VIL cap to avoid hail contamination
        R_e = 6_371_000.0
        k_refr = 4.0 / 3.0

        def beam_height_km(r_m: float, elev_deg: float) -> float:
            elev_rad = math.radians(elev_deg)
            h_m = r_m * math.sin(elev_rad) + (r_m * r_m) / (2.0 * k_refr * R_e)
            return h_m / 1000.0

        # Sort sweeps ascending by elevation so profile is bottom-up
        sweep_order = sorted(range(n_sweeps), key=lambda i: float(fixed_angles[i]))

        # Reset BWER + MESH fields per scan (Voronoi-gated so cells owned by
        # another active radar keep that radar's detection).
        for cell in cells:
            if self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.bwer_detected = False
                cell.bwer_overhang_dbz = None
                cell.mesh_mm = None
                cell.shi_value = None

        freezing_level_km = float(self._freezing_level_km)
        minus_20c_km      = freezing_level_km + MESH_MINUS_20C_DELTA_KM

        for cell in cells:
            if cell.scan_count < 0:
                continue
            dist_km, bearing_deg = self._latlon_to_polar(
                rad_lat, rad_lon, cell.lat, cell.lon
            )
            dist_m = dist_km * 1000.0
            if dist_m < 2_000 or dist_m > float(ranges_m[-1]):
                continue
            # Voronoi: vertical structure should come from the cell's closest radar
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            g_idx = int(np.searchsorted(ranges_m, dist_m))
            g_idx = max(0, min(len(ranges_m) - 1, g_idx))
            g_lo = max(0, g_idx - 2)
            g_hi = min(len(ranges_m), g_idx + 3)

            heights_km: list[float] = []
            max_refl_per_tilt: list[float] = []

            for sw in sweep_order:
                try:
                    s_start = int(radar.sweep_start_ray_index["data"][sw])
                    s_end = int(radar.sweep_end_ray_index["data"][sw])
                except Exception:
                    continue
                az = np.asarray(radar.azimuth["data"][s_start:s_end + 1], dtype=float)
                diffs = (az - bearing_deg + 540.0) % 360.0 - 180.0
                r_idx_rel = int(np.argmin(np.abs(diffs)))
                r_lo = max(0, r_idx_rel - 1)
                r_hi = min(az.size, r_idx_rel + 2)

                sweep_refl = refl_all[s_start:s_end + 1, g_lo:g_hi]
                window = sweep_refl[r_lo:r_hi]
                if window.size == 0 or np.all(np.isnan(window)):
                    continue

                peak = float(np.nanmax(window))
                heights_km.append(beam_height_km(dist_m, float(fixed_angles[sw])))
                max_refl_per_tilt.append(peak)

            if not heights_km:
                continue

            heights_arr = np.asarray(heights_km)
            refls_arr = np.asarray(max_refl_per_tilt)

            # Sort by height (should already be ascending, but guard)
            order = np.argsort(heights_arr)
            heights_arr = heights_arr[order]
            refls_arr = refls_arr[order]

            # --- Top / base / depth ---
            above_min = refls_arr >= ECHO_MIN_DBZ
            if np.any(above_min):
                cell.cell_top_km = round(float(heights_arr[above_min][-1]), 1)
                cell.cell_base_km = round(float(heights_arr[above_min][0]), 1)
                cell.depth_km = round(cell.cell_top_km - cell.cell_base_km, 1)
            else:
                cell.cell_top_km = None
                cell.cell_base_km = None
                cell.depth_km = None

            # --- Max reflectivity height ---
            if refls_arr.size > 0:
                peak_idx = int(np.nanargmax(refls_arr))
                cell.max_ref_height_km = round(float(heights_arr[peak_idx]), 1)

            # --- Reflectivity-weighted centroid height ---
            # Weight by linear Z (not dBZ): Z_lin = 10^(dBZ/10)
            valid = ~np.isnan(refls_arr) & (refls_arr >= 0)
            if np.any(valid):
                z_lin = np.power(10.0, refls_arr[valid] / 10.0)
                w_sum = float(np.sum(z_lin))
                if w_sum > 0:
                    cell.centroid_height_km = round(
                        float(np.sum(heights_arr[valid] * z_lin) / w_sum), 1
                    )

            # --- Cell-based VIL (trapezoidal integration in kg/m²) ---
            # VIL = Σ  3.44e-6 · ((Z_i + Z_{i+1}) / 2)^(4/7) · Δh  (SI constant form)
            # Using the common form with Z in mm⁶/m³:
            #   M = 3.44e-6 · Z^(4/7)  (kg/m³)
            # Cap reflectivity at VIL_Z_CAP_DBZ to suppress hail contamination.
            if valid.sum() >= 2:
                h = heights_arr[valid] * 1000.0  # metres
                dbz = np.minimum(refls_arr[valid], VIL_Z_CAP_DBZ)
                z_lin_capped = np.power(10.0, dbz / 10.0)  # mm^6/m^3
                m_density = 3.44e-6 * np.power(z_lin_capped, 4.0 / 7.0)  # kg/m³
                # Trapezoidal sum
                dh = np.diff(h)
                avg_m = 0.5 * (m_density[:-1] + m_density[1:])
                vil = float(np.sum(avg_m * dh))
                if np.isfinite(vil) and vil >= 0:
                    cell.vil_kg_m2 = round(vil, 1)

            # --- BWER (Bounded Weak Echo Region) ---
            # Search the column for a weak-echo "notch" (Z ≤ BWER_WEAK_DBZ) at
            # mid-levels (3–8 km AGL) bounded above by strong overhang (Z ≥
            # BWER_OVERHANG_DBZ) and below by moderate echo (Z ≥ BWER_BELOW_DBZ).
            # Classic signature of a tilted supercell updraft.
            if valid.sum() >= 3:
                h_v = heights_arr[valid]
                z_v = refls_arr[valid]
                weak_band = (
                    (z_v <= BWER_WEAK_DBZ)
                    & (h_v >= BWER_MIN_WEAK_HEIGHT_KM)
                    & (h_v <= BWER_MAX_WEAK_HEIGHT_KM)
                )
                if np.any(weak_band):
                    weak_top = float(np.nanmax(h_v[weak_band]))
                    weak_bot = float(np.nanmin(h_v[weak_band]))
                    # Strong overhang above (≥ BWER_MIN_OVERHANG_KM separation)
                    above_mask = h_v >= (weak_top + BWER_MIN_OVERHANG_KM)
                    below_mask = h_v <= weak_bot
                    if np.any(above_mask) and np.any(below_mask):
                        z_above = float(np.nanmax(z_v[above_mask]))
                        z_below = float(np.nanmax(z_v[below_mask]))
                        if (z_above >= BWER_OVERHANG_DBZ
                                and z_below >= BWER_BELOW_DBZ):
                            cell.bwer_detected = True
                            cell.bwer_overhang_dbz = round(z_above, 1)

            # --- MESH / SHI (Maximum Estimated Size of Hail) ---
            # Witt et al. 1998 Severe Hail Index.  Integrate hail kinetic
            # energy flux above the freezing level, weighted by temperature
            # band (full weight above −20°C, ramping from 0 at the 0°C level).
            # E(Z) ≈ 5e-6 · 10^(0.084·Z) · W_T(Z),   MESH = 2.54·SHI^0.5  (mm)
            if valid.sum() >= 2:
                h_v = heights_arr[valid]
                z_v = refls_arr[valid]

                # Sort ascending for integration
                order = np.argsort(h_v)
                h_v = h_v[order]
                z_v = z_v[order]

                # Only integrate the portion above the freezing level
                above_fl = h_v >= freezing_level_km
                if np.sum(above_fl) >= 2:
                    h_int = h_v[above_fl] * 1000.0  # metres
                    z_int = np.clip(z_v[above_fl], 0.0, 75.0)  # cap for stability

                    # Temperature weighting: 0 at freezing level, 1 at −20°C
                    span = max(minus_20c_km - freezing_level_km, 0.1)
                    w_t = np.clip(
                        (h_v[above_fl] - freezing_level_km) / span, 0.0, 1.0
                    )

                    # Z weighting: 0 below MESH_Z_LOWER_DBZ, 1 above MESH_Z_UPPER
                    w_z = np.clip(
                        (z_int - MESH_Z_LOWER_DBZ)
                        / max(MESH_Z_UPPER_DBZ - MESH_Z_LOWER_DBZ, 0.1),
                        0.0, 1.0,
                    )

                    # Hail kinetic energy flux (Witt 1998)
                    e_flux = 5.0e-6 * np.power(10.0, 0.084 * z_int) * w_z * w_t
                    # SHI = 0.1 · ∫ e_flux dh (trapezoidal)
                    dh_int = np.diff(h_int)
                    avg_e  = 0.5 * (e_flux[:-1] + e_flux[1:])
                    shi    = 0.1 * float(np.sum(avg_e * dh_int))
                    if np.isfinite(shi) and shi > 0:
                        mesh   = MESH_SHI_TO_MM_COEFF * math.sqrt(shi)
                        cell.shi_value = round(shi, 1)
                        cell.mesh_mm   = round(mesh, 1)

    @staticmethod
    def _latlon_to_polar(
        ref_lat: float, ref_lon: float, lat: float, lon: float
    ) -> tuple[float, float]:
        """Return (distance_km, bearing_deg_from_north) from ref to point."""
        lat1 = math.radians(ref_lat)
        lat2 = math.radians(lat)
        dlat = lat2 - lat1
        dlon = math.radians(lon - ref_lon)
        # Great-circle distance
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        dist_km = 2 * 6371.0 * math.asin(math.sqrt(a))
        # Initial bearing
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        return dist_km, bearing

    def _detect_mcs_systems(
        self, cells: list[TrackedStormCell], timestamp: str
    ) -> list["MCSSystem"]:
        """
        Identify linear Mesoscale Convective Systems (MCS/QLCS/squall lines)
        from the spatial arrangement of active storm cells.

        Algorithm:
        1. PCA on cell centroids — high aspect ratio → linear system
        2. Classify as squall_line, bow_echo, or mcs by length and bow distance
        3. Check book-end vortices at line termini
        4. Count embedded QLCS mesos

        Returns list of MCSSystem objects (typically 0 or 1).
        """
        # Clear previous system membership
        for cell in cells:
            cell.mcs_system_id = None

        active = [
            c for c in cells
            if c.scan_count >= 0 and c.max_reflectivity_dbz >= CELL_DETECT_DBZ
        ]
        if len(active) < MCS_MIN_CELLS:
            return []

        # Convert centroids to local km coordinates
        center_lat = float(np.mean([c.lat for c in active]))
        center_lon = float(np.mean([c.lon for c in active]))
        cos_lat = math.cos(math.radians(center_lat))

        pts = np.array([
            [
                (c.lon - center_lon) * 111.0 * cos_lat,
                (c.lat - center_lat) * 111.0,
            ]
            for c in active
        ])  # shape (N, 2), columns: [dx_km, dy_km]

        # PCA — find primary axis of the point cloud
        cov = np.cov(pts.T)
        eigenvalues, eigenvectors = np.linalg.eigh(cov)
        # eigh returns ascending order; flip to descending
        order = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[order]
        eigenvectors = eigenvectors[:, order]

        if eigenvalues[1] < 1e-4:
            return []  # All cells effectively co-located
        aspect_ratio = math.sqrt(eigenvalues[0] / eigenvalues[1])
        if aspect_ratio < MCS_ASPECT_RATIO_MIN:
            return []  # Not linear enough to be an organised system

        # Project points onto primary axis to get along-line positions
        primary_axis = eigenvectors[:, 0]  # unit vector (dx, dy)
        secondary_axis = eigenvectors[:, 1]
        projections = pts @ primary_axis   # shape (N,)
        perp = pts @ secondary_axis        # perpendicular distance from line
        length_km = float(projections.max() - projections.min())
        width_km = float(perp.max() - perp.min())

        if length_km < MCS_MIN_LENGTH_KM:
            return []
        # Reject wide blobs — real squall lines are narrow.
        if width_km > MCS_MAX_WIDTH_KM:
            return []
        # Reject clumped cells (not evenly distributed along the line).
        sorted_proj = np.sort(projections)
        gaps = np.diff(sorted_proj)
        if gaps.size > 1 and gaps.mean() > 0:
            cv = float(gaps.std() / gaps.mean())
            if cv > MCS_SPACING_CV_MAX:
                return []

        # Line orientation: angle from north (0=N–S, 90=E–W)
        # primary_axis = (dx, dy); angle from north = atan2(dx, dy)
        orientation_deg = float(np.degrees(np.arctan2(primary_axis[0], primary_axis[1])) % 180)

        # Motion: circular-mean of member cell motions
        moving = [c for c in active if c.motion_speed_kph > 0]
        if moving:
            avg_speed = float(np.mean([c.motion_speed_kph for c in moving]))
            avg_dir = float(self._circular_mean(
                [c.motion_direction_deg for c in moving],
                [1.0] * len(moving),
            ))
        else:
            avg_speed = 0.0
            avg_dir = 0.0

        # Check bow echo / rear-inflow notch
        bow_echo, rni = self._check_bow_echo(active, primary_axis, projections, avg_dir)

        # Check book-end vortices at line termini
        book_end = self._check_book_end_vortices(active, projections)

        # Count embedded QLCS mesos
        qlcs_count = sum(1 for c in active if c.qlcs_meso_detected)

        # Classify — bow echo requires enough cells to define an arc
        if bow_echo and len(active) >= MCS_BOW_MIN_CELLS:
            system_type = "bow_echo"
        elif length_km >= 200 or len(active) >= 8:
            system_type = "mcs"
        else:
            system_type = "squall_line"

        max_score = max(c.severity_score for c in active)

        # Stable ID based on member cells so it persists across scans
        sorted_ids = tuple(sorted(c.cell_id for c in active))
        system_id = f"SYS-{hash(sorted_ids) & 0xFFFF:04X}"

        # Tag each member cell
        for cell in active:
            cell.mcs_system_id = system_id

        system = MCSSystem(
            system_id=system_id,
            system_type=system_type,
            cell_ids=[c.cell_id for c in active],
            centroid_lat=round(center_lat, 4),
            centroid_lon=round(center_lon, 4),
            orientation_deg=round(orientation_deg, 1),
            length_km=round(length_km, 1),
            bow_echo_detected=bool(bow_echo),
            rear_inflow_notch=bool(rni),
            book_end_vortices=bool(book_end),
            embedded_qlcs_mesos=qlcs_count,
            max_severity_score=max_score,
            threat_level=self._score_to_threat(max_score),
            motion_direction_deg=round(avg_dir, 1),
            motion_speed_kph=round(avg_speed, 1),
            timestamp=timestamp,
        )

        logger.info(
            f"MCS detected: {system_type}, {len(active)} cells, "
            f"{length_km:.0f} km, bow={bow_echo}, book_end={book_end}, "
            f"qlcs_mesos={qlcs_count}"
        )
        return [system]

    def _check_bow_echo(
        self,
        cells: list[TrackedStormCell],
        primary_axis: np.ndarray,
        projections: np.ndarray,
        motion_dir_deg: float,
    ) -> tuple[bool, bool]:
        """
        Check for bow echo and rear-inflow notch signatures.

        A bow echo occurs when the central cells protrude further in the storm-motion
        direction than the end cells — the leading edge "bows out" at the apex.

        Returns (bow_echo, rear_inflow_notch).
        """
        n = len(cells)
        if n < 3:
            return False, False

        # Storm-motion unit vector in (dx_km, dy_km) space
        motion_rad = math.radians(motion_dir_deg)
        motion_vec = np.array([math.sin(motion_rad), math.cos(motion_rad)])

        # For each cell, compute displacement from centroid along motion direction
        c_lat = float(np.mean([c.lat for c in cells]))
        c_lon = float(np.mean([c.lon for c in cells]))
        cos_lat = math.cos(math.radians(c_lat))

        motion_dists = []
        for cell in cells:
            dx = (cell.lon - c_lon) * 111.0 * cos_lat
            dy = (cell.lat - c_lat) * 111.0
            motion_dists.append(dx * motion_vec[0] + dy * motion_vec[1])

        # Sort cells by along-line position
        order = np.argsort(projections)
        sorted_motion = [motion_dists[i] for i in order]

        # Compare ends vs centre
        end_n = max(1, n // 3)
        end_avg = (sorted_motion[0] + sorted_motion[-1]) / 2.0
        center_slice = sorted_motion[end_n: n - end_n]
        if not center_slice:
            center_slice = [sorted_motion[n // 2]]
        center_avg = float(np.mean(center_slice))

        bulge_km = center_avg - end_avg
        bow_echo = bulge_km >= BOW_ECHO_MIN_BULGE_KM
        rni = bulge_km >= RNI_BULGE_KM
        return bow_echo, rni

    def _check_book_end_vortices(
        self,
        cells: list[TrackedStormCell],
        projections: np.ndarray,
    ) -> bool:
        """
        Check for book-end vortices — rotation at the northern and southern
        termini of a squall line.  Flagged when EITHER end cell shows rotation
        (supercell meso or QLCS meso).
        """
        if len(cells) < 3:
            return False
        order = np.argsort(projections)
        north_cell = cells[order[-1]]
        south_cell = cells[order[0]]
        north_rot = north_cell.rotation_detected or north_cell.qlcs_meso_detected
        south_rot = south_cell.rotation_detected or south_cell.qlcs_meso_detected
        return north_rot or south_rot

    def _compute_trends(self, cells: list[TrackedStormCell], timestamp: str):
        """Append the current scan's key features to each cell's history and
        compute slope (per-scan rate of change) over the last TREND_WINDOW_SCANS.

        Using a linear regression slope (rather than just current − previous)
        gives a more stable estimate when scan timing is slightly irregular.
        Pre-alert detection relies heavily on these trends — a storm going from
        0 to 15 m/s rotation in 3 scans is far more dangerous than one that has
        been steady at 12 m/s for 20 minutes.
        """
        for cell in cells:
            if cell.scan_count < 0:
                continue

            # Append current snapshot
            snap = {
                "ts":         timestamp,
                "llsd_shear": cell.llsd_max_shear,
                "rot_vel":    cell.max_rot_velocity_ms,
                "vil":        cell.vil_kg_m2,
                "echo_top":   cell.cell_top_km,
                "dbz":        cell.max_reflectivity_dbz,
                "severity":   cell.severity_score,
            }
            cell.feature_history.append(snap)
            if len(cell.feature_history) > TREND_HISTORY_MAX:
                cell.feature_history = cell.feature_history[-TREND_HISTORY_MAX:]

            n = min(len(cell.feature_history), TREND_WINDOW_SCANS)
            if n < 2:
                continue
            recent = cell.feature_history[-n:]
            x = list(range(n))  # [0, 1, 2] — scan index

            # Linear least-squares slope for each field
            def _slope(field: str) -> Optional[float]:
                vals = [r[field] for r in recent if r[field] is not None]
                if len(vals) < 2:
                    return None
                # Align x to the available samples (some may be None)
                xi = [i for i, r in enumerate(recent) if r[field] is not None]
                n_ = len(vals)
                x_mean = sum(xi) / n_
                y_mean = sum(vals) / n_
                ss_xx = sum((xi_ - x_mean) ** 2 for xi_ in xi)
                ss_xy = sum((xi_ - x_mean) * (y_ - y_mean)
                            for xi_, y_ in zip(xi, vals))
                if ss_xx == 0:
                    return None
                return round(ss_xy / ss_xx, 5)

            cell.llsd_trend      = _slope("llsd_shear")
            cell.rot_vel_trend   = _slope("rot_vel")
            cell.vil_trend       = _slope("vil")
            cell.echo_top_trend  = _slope("echo_top")
            cell.dbz_trend       = _slope("dbz")

    def _reconcile_rotation_flags(self, cells: list[TrackedStormCell]):
        """Promote the best available rotation signal to the primary flags.

        The analysis pipeline runs three rotation detectors in order:
          1. Grid-based couplet  (_detect_rotation)       — noisy, Barnes2-blurred
          2. LLSD azimuthal shear (_detect_llsd_rotation) — better, polar, lowest tilt
          3. Multi-tilt profile  (_compute_rotation_profile) — best, polar, all tilts

        Grid results are set first, but (3) and (2) are more physically accurate.
        This method runs after all three and uses the strongest reliable signal.

        Rules:
        - If multi-tilt profile shows rotation ≥ MESO threshold → confirm rotation
        - If multi-tilt profile explicitly shows < 80 % of threshold → clear grid flag
        - LLSD ≥ MESO threshold alone is enough to confirm if profile is absent
        - TVS is set only when multi-tilt peak or grid couplet (after outlier rejection)
          meets the TVS threshold
        """
        for cell in cells:
            if cell.scan_count < 0:
                continue

            # ── Multi-tilt profile (most authoritative) ───────────────────
            profile_vel = cell.max_rot_velocity_ms  # peak across all tilts
            if profile_vel is not None and profile_vel > 0:
                if profile_vel >= MESO_VELOCITY_THRESHOLD_MS:
                    peak_h  = cell.max_rot_height_km   # km AGL where peak rot occurs
                    depth   = cell.rotation_depth_km    # km of vertical rotation extent

                    # Height guard: rotation only above 8 km AGL is anvil-level
                    # divergence or outflow, not a mesocyclone.
                    too_high = (peak_h is not None and peak_h > MESO_MAX_CONFIRM_HEIGHT_KM)

                    # Depth guard: 0 km means only a single tilt exceeded the
                    # threshold — not enough vertical extent for a real meso.
                    # LLSD confirmation can substitute (low-level surface shear).
                    single_tilt = (depth is None or depth < MESO_MIN_DEPTH_FOR_CONFIRM_KM)
                    llsd_ok = cell.llsd_rotation_detected

                    if too_high:
                        # Definitively not a mesocyclone — clear without mercy.
                        if not llsd_ok:
                            cell.rotation_detected = False
                            cell.rotation_velocity_ms = None
                            cell.tvs_detected = False
                            logger.debug(
                                f"Rotation cleared for {cell.cell_id}: "
                                f"peak at {peak_h:.1f} km AGL > "
                                f"{MESO_MAX_CONFIRM_HEIGHT_KM} km (anvil/outflow)"
                            )
                    elif single_tilt and not llsd_ok:
                        # Single-tilt, low altitude, no LLSD — uncertain.
                        # Don't promote, but don't clear either; keep grid result.
                        pass
                    else:
                        # Multi-tilt OR LLSD-confirmed AND below height cap → real meso
                        cell.rotation_detected = True
                        cell.rotation_velocity_ms = round(profile_vel, 1)
                        cell.tvs_detected = profile_vel >= TVS_VELOCITY_THRESHOLD_MS

                elif profile_vel < MESO_VELOCITY_THRESHOLD_MS * 0.8:
                    # Profile clearly below threshold — override any grid false positive
                    # unless LLSD independently confirms it
                    if not cell.llsd_rotation_detected:
                        cell.rotation_detected = False
                        cell.rotation_velocity_ms = None
                        cell.tvs_detected = False
                # If profile is in the 80–100 % grey zone, keep whatever the grid said

            # ── LLSD (secondary; better than grid alone) ──────────────────
            # If the profile didn't find rotation but LLSD at the surface confirms
            # meso-class shear, trust that over the grid couplet.
            elif cell.llsd_rotation_detected:
                # LLSD meso-class shear confirmed; approximate Vrot from shear
                # using a nominal couplet half-width of 2 km:  Vrot = shear × 2000 m
                if cell.llsd_max_shear is not None:
                    approx_vel = cell.llsd_max_shear * 2_000.0  # m/s
                    if approx_vel >= MESO_VELOCITY_THRESHOLD_MS:
                        cell.rotation_detected = True
                        # Only override rotation_velocity_ms if the profile gave nothing
                        if cell.rotation_velocity_ms is None:
                            cell.rotation_velocity_ms = round(approx_vel, 1)
                        cell.tvs_detected = (
                            cell.tvs_detected
                            or approx_vel >= TVS_VELOCITY_THRESHOLD_MS
                        )

            # ── Altitude classification ───────────────────────────────────
            # Tornadogenesis correlates with rotation reaching the boundary
            # layer; mid-level rotation alone is a normal supercell trait.
            # A deep meso column can be both low- and mid-level.
            if cell.rotation_detected:
                base_h = cell.rotation_base_km
                peak_h = cell.max_rot_height_km

                cell.low_level_meso_detected = (
                    cell.llsd_rotation_detected
                    or (base_h is not None and base_h <= LOW_LEVEL_MESO_MAX_HEIGHT_KM)
                )

                # Mid-level: any portion of the rotation column sits in 2–6 km.
                # Use both the peak height and the column extent so a meso
                # whose peak is at 4 km AGL with a base at 1 km counts as
                # both low- and mid-level (correctly, for a tornadic supercell).
                mid_top    = cell.rotation_depth_km is not None and base_h is not None and (base_h + cell.rotation_depth_km) > LOW_LEVEL_MESO_MAX_HEIGHT_KM
                peak_in_mid = (
                    peak_h is not None
                    and LOW_LEVEL_MESO_MAX_HEIGHT_KM < peak_h <= MID_LEVEL_MESO_MAX_HEIGHT_KM
                )
                cell.mid_level_meso_detected = bool(peak_in_mid or mid_top)
            else:
                cell.low_level_meso_detected = False
                cell.mid_level_meso_detected = False

    # =========================================================================
    # Phase 3 — Enhanced Dual-Pol Detectors
    # =========================================================================

    def _count_tds_tilts(
        self, radar, cell: "TrackedStormCell", rad_lat: float, rad_lon: float
    ) -> int:
        """
        Count consecutive low-level tilts where the TDS signature is confirmed on
        polar data: CC < DEBRIS_CC_THRESHOLD AND Z ≥ TDS_MIN_REFL_DBZ at the cell
        location, with the beam centre ≤ TDS_MAX_TILT_HEIGHT_KM AGL.

        Returns the count; caller compares against TDS_MIN_TILT_COUNT.
        """
        if "reflectivity" not in radar.fields or "cross_correlation_ratio" not in radar.fields:
            return 0

        try:
            ranges_m     = np.asarray(radar.range["data"], dtype=float)
            fixed_angles = radar.fixed_angle["data"]
            n_sweeps     = len(fixed_angles)
        except Exception:
            return 0

        R_e, k_r = 6_371_000.0, 4.0 / 3.0

        dist_km, bearing_deg = self._latlon_to_polar(rad_lat, rad_lon, cell.lat, cell.lon)
        dist_m = dist_km * 1_000.0
        if dist_m < 2_000 or dist_m > float(ranges_m[-1]):
            return 0

        gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if len(ranges_m) > 1 else 250.0
        g_idx  = int(np.searchsorted(ranges_m, dist_m))
        g_idx  = max(0, min(len(ranges_m) - 1, g_idx))
        half_g = max(2, int(3_000.0 / gate_spacing_m))  # ±3 km search window

        confirm_count = 0

        for sw in range(n_sweeps):
            elev_deg = float(fixed_angles[sw])
            elev_rad = math.radians(elev_deg)
            h_km     = (
                dist_m * math.sin(elev_rad) + dist_m ** 2 / (2.0 * k_r * R_e)
            ) / 1_000.0

            if h_km > TDS_MAX_TILT_HEIGHT_KM:
                continue  # Beam is above the debris column ceiling

            try:
                ss = int(radar.sweep_start_ray_index["data"][sw])
                se = int(radar.sweep_end_ray_index["data"][sw])
            except Exception:
                continue

            az   = np.asarray(radar.azimuth["data"][ss:se + 1], dtype=float)
            refl = np.ma.filled(radar.fields["reflectivity"]["data"][ss:se + 1], np.nan)
            cc   = np.ma.filled(
                radar.fields["cross_correlation_ratio"]["data"][ss:se + 1], np.nan
            )
            if refl.shape[0] < 2:
                continue

            diffs = (az - bearing_deg + 540.0) % 360.0 - 180.0
            r_idx = int(np.argmin(np.abs(diffs)))
            r_lo  = max(0, r_idx - 1)
            r_hi  = min(refl.shape[0], r_idx + 2)
            g_lo  = max(0, g_idx - half_g)
            g_hi  = min(refl.shape[1], g_idx + half_g + 1)

            refl_w = refl[r_lo:r_hi, g_lo:g_hi]
            cc_w   = cc[r_lo:r_hi, g_lo:g_hi]

            if refl_w.size == 0:
                continue

            tds_px = (
                (cc_w < DEBRIS_CC_THRESHOLD)
                & (refl_w >= TDS_MIN_REFL_DBZ)
                & ~np.isnan(cc_w)
                & ~np.isnan(refl_w)
            )
            if int(np.count_nonzero(tds_px)) >= 2:
                confirm_count += 1

        return confirm_count

    def _check_tbss_signatures(self, radar, cells: list["TrackedStormCell"]):
        """
        Detect Three-Body Scatter Spikes (TBSS) behind confirmed hail cores.

        Physics: radar energy → hail → ground → hail → radar.  The extra ground-
        bounce path adds ≈ 2× the hail range of travel time, so the return appears
        at 1.3–2× the hail's true range on the same azimuth, with moderate
        reflectivity (TBSS_SPIKE_DBZ_MIN–MAX) and anomalously low CC.

        Only evaluated for cells where `hail_indicated = True` and range exceeds
        TBSS_MIN_RANGE_KM (< 40 km, the spike would fall within or near the storm
        and be indistinguishable from real echoes).
        """
        if radar is None:
            return
        if "reflectivity" not in radar.fields:
            return

        has_cc = "cross_correlation_ratio" in radar.fields

        try:
            rad_lat      = float(radar.latitude["data"][0])
            rad_lon      = float(radar.longitude["data"][0])
            ranges_m     = np.asarray(radar.range["data"], dtype=float)
            fixed_angles = radar.fixed_angle["data"]
        except Exception:
            return

        # Voronoi-gated reset: only clear flags for cells whose primary radar
        # is THIS one. Cells owned by another active radar keep the flag set
        # by that radar's last analysis.
        for cell in cells:
            if self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.tbss_detected = False

        # Lowest tilt ≤ 1.5° — TBSS is most pronounced at the lowest elevation
        candidates = [
            (i, float(a)) for i, a in enumerate(fixed_angles) if float(a) <= 1.5
        ]
        if not candidates:
            return
        sweep_idx, _ = min(candidates, key=lambda x: x[1])

        try:
            ss = int(radar.sweep_start_ray_index["data"][sweep_idx])
            se = int(radar.sweep_end_ray_index["data"][sweep_idx])
        except Exception:
            return

        azimuths = np.asarray(radar.azimuth["data"][ss:se + 1], dtype=float)
        refl     = np.ma.filled(radar.fields["reflectivity"]["data"][ss:se + 1], np.nan)
        cc       = (
            np.ma.filled(
                radar.fields["cross_correlation_ratio"]["data"][ss:se + 1], np.nan
            ) if has_cc else None
        )
        gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if len(ranges_m) > 1 else 250.0

        for cell in cells:
            if cell.scan_count < 0 or not cell.hail_indicated:
                continue
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            dist_km, bearing_deg = self._latlon_to_polar(rad_lat, rad_lon, cell.lat, cell.lon)
            dist_m = dist_km * 1_000.0

            if dist_km < TBSS_MIN_RANGE_KM:
                continue  # Too close: spike falls within the storm itself

            # Gate window for the TBSS spike region
            spike_min_m = dist_m * TBSS_RANGE_FACTOR_MIN
            spike_max_m = dist_m * TBSS_RANGE_FACTOR_MAX
            if spike_max_m > float(ranges_m[-1]):
                continue  # Spike would be beyond max range

            g_spike_lo = int(np.searchsorted(ranges_m, spike_min_m))
            g_spike_hi = int(np.searchsorted(ranges_m, spike_max_m))
            g_spike_lo = max(0, min(len(ranges_m) - 1, g_spike_lo))
            g_spike_hi = max(g_spike_lo + 1, min(refl.shape[1], g_spike_hi))

            # Nearest ray (TBSS is tightly azimuth-aligned with the hail core)
            diffs = (azimuths - bearing_deg + 540.0) % 360.0 - 180.0
            r_idx = int(np.argmin(np.abs(diffs)))
            r_lo  = max(0, r_idx - 1)
            r_hi  = min(refl.shape[0], r_idx + 2)

            spike_refl = refl[r_lo:r_hi, g_spike_lo:g_spike_hi]
            if spike_refl.size == 0 or np.all(np.isnan(spike_refl)):
                continue

            valid_spike = spike_refl[~np.isnan(spike_refl)]
            if valid_spike.size == 0:
                continue

            # Reflectivity in spike zone must be moderate — significantly lower
            # than the actual hail core but above noise floor
            n_spike_px = int(np.count_nonzero(
                (valid_spike >= TBSS_SPIKE_DBZ_MIN) & (valid_spike <= TBSS_SPIKE_DBZ_MAX)
            ))
            if n_spike_px < TBSS_MIN_SPIKE_PIXELS:
                continue

            # Low CC guard: genuine meteorological echoes have CC > 0.85.
            # A TBSS return is non-meteorological and should be lower.
            if cc is not None:
                spike_cc = cc[r_lo:r_hi, g_spike_lo:g_spike_hi]
                valid_cc = spike_cc[~np.isnan(spike_cc)]
                if valid_cc.size > 0 and float(np.nanmean(valid_cc)) > TBSS_SPIKE_CC_MAX:
                    continue  # CC too high — this is a real echo, not TBSS

            cell.tbss_detected = True
            logger.debug(
                f"TBSS: {cell.cell_id}  hail_range={dist_km:.0f} km  "
                f"spike={spike_min_m/1000:.0f}–{spike_max_m/1000:.0f} km  "
                f"n_px={n_spike_px}"
            )

    # =========================================================================
    # Phase 2 — Kinematic Wind Signature Detectors
    # =========================================================================

    def _detect_downburst_signatures(self, radar, cells: list[TrackedStormCell]):
        """
        Detect microburst / downburst signatures on the lowest radar tilt.

        Physical signature: compact radial divergence where near-radar gates
        show strong inbound velocity (surface outflow rushing toward the radar)
        and far-radar gates show strong outbound (outflow rushing away).  The
        entire pattern must fit within DOWNBURST_MAX_DIAMETER_KM to exclude
        ordinary storm-scale Doppler motion.

        ΔV = V_far_peak − V_near_peak  (always positive for true divergence).
        Flag when ΔV ≥ DOWNBURST_DELTA_V_MS and both components exceed their
        floor thresholds.
        """
        if radar is None:
            return

        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            return

        try:
            rad_lat      = float(radar.latitude["data"][0])
            rad_lon      = float(radar.longitude["data"][0])
            ranges_m     = np.asarray(radar.range["data"], dtype=float)
            fixed_angles = radar.fixed_angle["data"]
        except Exception:
            return

        # Voronoi-gated reset
        for cell in cells:
            if self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.downburst_detected   = False
                cell.downburst_delta_v_ms = None

        # Lowest tilt ≤ 1.5°
        candidates = [
            (i, float(a)) for i, a in enumerate(fixed_angles) if float(a) <= 1.5
        ]
        if not candidates:
            return
        sweep_idx, sweep_elev = min(candidates, key=lambda x: x[1])

        try:
            s_start = int(radar.sweep_start_ray_index["data"][sweep_idx])
            s_end   = int(radar.sweep_end_ray_index["data"][sweep_idx])
        except Exception:
            return

        azimuths = np.asarray(radar.azimuth["data"][s_start:s_end + 1], dtype=float)
        vel      = np.ma.filled(radar.fields[vel_key]["data"][s_start:s_end + 1], np.nan)
        n_rays, n_gates = vel.shape
        gate_spacing_m  = float(ranges_m[1] - ranges_m[0]) if n_gates > 1 else 250.0

        for cell in cells:
            if cell.scan_count < 0:
                continue
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            dist_km, bearing_deg = self._latlon_to_polar(rad_lat, rad_lon, cell.lat, cell.lon)
            dist_m = dist_km * 1_000.0
            if dist_m < 5_000 or dist_m > float(ranges_m[-1]):
                continue

            g_idx = int(np.searchsorted(ranges_m, dist_m))
            g_idx = max(2, min(n_gates - 3, g_idx))

            diffs = (azimuths - bearing_deg + 540.0) % 360.0 - 180.0
            r_idx = int(np.argmin(np.abs(diffs)))

            # Narrow azimuthal window (±2 rays) — stays close to the same radial
            r_lo = max(0, r_idx - 2)
            r_hi = min(n_rays, r_idx + 3)

            # Range window: ±5 km around the cell gate, split at the cell gate
            half_g      = max(4, int(5_000.0 / gate_spacing_m))
            g_near_lo   = max(0, g_idx - half_g)
            g_far_hi    = min(n_gates, g_idx + half_g + 1)

            near_region = vel[r_lo:r_hi, g_near_lo:g_idx]   # radar-side of cell
            far_region  = vel[r_lo:r_hi, g_idx:g_far_hi]    # distal side of cell

            if near_region.size < 3 or far_region.size < 3:
                continue
            if np.all(np.isnan(near_region)) or np.all(np.isnan(far_region)):
                continue

            V_near_min = float(np.nanmin(near_region))  # Most inbound on near side
            V_far_max  = float(np.nanmax(far_region))   # Most outbound on distal side

            # Both divergence components must clear their floor thresholds
            if V_near_min >= DOWNBURST_INBOUND_FLOOR_MS:
                continue
            if V_far_max  <= DOWNBURST_OUTBOUND_FLOOR_MS:
                continue

            delta_v = V_far_max - V_near_min
            if delta_v < DOWNBURST_DELTA_V_MS:
                continue

            # Compact-size check: gate distance between the two extrema
            near_flat      = int(np.nanargmin(near_region))
            far_flat       = int(np.nanargmax(far_region))
            near_gate_col  = near_flat % max(near_region.shape[1], 1)
            far_gate_col   = far_flat  % max(far_region.shape[1], 1)
            g_min_abs      = g_near_lo + near_gate_col
            g_max_abs      = g_idx     + far_gate_col
            gate_sep_km    = abs(g_max_abs - g_min_abs) * gate_spacing_m / 1_000.0

            if gate_sep_km > DOWNBURST_MAX_DIAMETER_KM:
                continue  # Too wide — storm-scale motion, not a localised downburst

            cell.downburst_detected   = True
            cell.downburst_delta_v_ms = round(delta_v, 1)
            logger.debug(
                f"Downburst: {cell.cell_id}  ΔV={delta_v:.1f} m/s  "
                f"diam={gate_sep_km:.1f} km  elev={sweep_elev:.1f}°"
            )

    def _detect_marc_signatures(self, radar, cells: list[TrackedStormCell]):
        """
        Detect Mid-Altitude Radial Convergence (MARC) at 3–9 km AGL.

        MARC is produced by the storm's primary updraft drawing air inward at
        mid-levels.  Strong inbound velocity (< MARC_CONVERGENCE_MS) at 3–9 km
        AGL directly above a high-dBZ low-level core is a recognised precursor
        to large hail and tornado development in supercells.

        Only evaluated for cells whose low-level reflectivity already exceeds
        MARC_REFL_FLOOR_DBZ to suppress false positives in stratiform rain.
        """
        if radar is None:
            return

        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            return

        try:
            rad_lat      = float(radar.latitude["data"][0])
            rad_lon      = float(radar.longitude["data"][0])
            ranges_m     = np.asarray(radar.range["data"], dtype=float)
            fixed_angles = radar.fixed_angle["data"]
            n_sweeps     = len(fixed_angles)
        except Exception:
            return

        # Voronoi-gated reset
        for cell in cells:
            if self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.marc_signature_detected = False
                cell.marc_convergence_ms     = None

        R_e    = 6_371_000.0
        k_refr = 4.0 / 3.0

        def _beam_h_km(r_m: float, elev_deg: float) -> float:
            er = math.radians(elev_deg)
            return (r_m * math.sin(er) + r_m ** 2 / (2.0 * k_refr * R_e)) / 1_000.0

        gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if len(ranges_m) > 1 else 250.0

        for cell in cells:
            if cell.scan_count < 0:
                continue
            if cell.max_reflectivity_dbz < MARC_REFL_FLOOR_DBZ:
                continue  # Only look above high-dBZ cores
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            dist_km, bearing_deg = self._latlon_to_polar(rad_lat, rad_lon, cell.lat, cell.lon)
            dist_m = dist_km * 1_000.0
            if dist_m < 5_000 or dist_m > float(ranges_m[-1]):
                continue

            g_idx          = int(np.searchsorted(ranges_m, dist_m))
            g_idx          = max(0, min(len(ranges_m) - 1, g_idx))
            half_g         = max(2, int(3_000.0 / gate_spacing_m))   # ±3 km in range
            half_angle_deg = math.degrees(3_000.0 / max(dist_m, 1.0))  # ±3 km tangentially

            peak_convergence = 0.0  # track the most negative value found

            for sw in range(n_sweeps):
                elev_deg = float(fixed_angles[sw])
                h_km     = _beam_h_km(dist_m, elev_deg)

                if not (MARC_MIN_HEIGHT_KM <= h_km <= MARC_MAX_HEIGHT_KM):
                    continue

                try:
                    ss = int(radar.sweep_start_ray_index["data"][sw])
                    se = int(radar.sweep_end_ray_index["data"][sw])
                except Exception:
                    continue

                az  = np.asarray(radar.azimuth["data"][ss:se + 1], dtype=float)
                vel = np.ma.filled(radar.fields[vel_key]["data"][ss:se + 1], np.nan)
                if vel.shape[0] < 3:
                    continue

                diffs    = (az - bearing_deg + 540.0) % 360.0 - 180.0
                ray_mask = np.abs(diffs) <= half_angle_deg
                if np.sum(ray_mask) < 2:
                    continue

                g_lo   = max(0, g_idx - half_g)
                g_hi   = min(vel.shape[1], g_idx + half_g + 1)
                region = vel[np.ix_(np.where(ray_mask)[0], np.arange(g_lo, g_hi))]

                if region.size == 0 or np.all(np.isnan(region)):
                    continue

                v_min = float(np.nanmin(region))
                if v_min < peak_convergence:
                    peak_convergence = v_min

            if peak_convergence <= MARC_CONVERGENCE_MS:
                cell.marc_signature_detected = True
                cell.marc_convergence_ms     = round(peak_convergence, 1)
                logger.debug(
                    f"MARC: {cell.cell_id}  convergence={peak_convergence:.1f} m/s  "
                    f"at {dist_km:.0f} km range"
                )

    def _detect_straight_line_winds(self, radar, cells: list[TrackedStormCell]):
        """
        Identify broad areas of damaging straight-line winds (≥ 50 kts / 25.7 m/s)
        at the lowest radar tilt.

        A large spatial footprint of severe-threshold outbound velocity distinguishes
        straight-line wind damage from the compact signatures of rotation couplets and
        microbursts.  The severe-velocity swath must exceed SLW_MIN_SWATH_KM2 to be
        flagged.  Stores the peak outbound velocity in `max_wind_velocity_ms`.
        """
        if radar is None:
            return

        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            return

        try:
            rad_lat      = float(radar.latitude["data"][0])
            rad_lon      = float(radar.longitude["data"][0])
            ranges_m     = np.asarray(radar.range["data"], dtype=float)
            fixed_angles = radar.fixed_angle["data"]
        except Exception:
            return

        # Voronoi-gated reset
        for cell in cells:
            if self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.straight_line_wind_detected = False
                cell.strong_wind_detected         = False
                cell.strong_wind_swath_km2        = None
                cell.max_wind_velocity_ms         = None

        # Lowest tilt ≤ 1.5°
        candidates = [
            (i, float(a)) for i, a in enumerate(fixed_angles) if float(a) <= 1.5
        ]
        if not candidates:
            return
        sweep_idx, _ = min(candidates, key=lambda x: x[1])

        try:
            s_start = int(radar.sweep_start_ray_index["data"][sweep_idx])
            s_end   = int(radar.sweep_end_ray_index["data"][sweep_idx])
        except Exception:
            return

        azimuths = np.asarray(radar.azimuth["data"][s_start:s_end + 1], dtype=float)
        vel      = np.ma.filled(radar.fields[vel_key]["data"][s_start:s_end + 1], np.nan)
        n_rays   = vel.shape[0]
        n_gates  = vel.shape[1]
        gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if n_gates > 1 else 250.0

        # Mean azimuthal spacing (radians) for area estimation
        daz_rad = math.radians(360.0 / max(n_rays, 1))

        for cell in cells:
            if cell.scan_count < 0:
                continue
            if not self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                continue

            dist_km, bearing_deg = self._latlon_to_polar(rad_lat, rad_lon, cell.lat, cell.lon)
            dist_m = dist_km * 1_000.0
            if dist_m < 5_000 or dist_m > float(ranges_m[-1]):
                continue

            g_idx = int(np.searchsorted(ranges_m, dist_m))
            g_idx = max(0, min(n_gates - 1, g_idx))

            # Broad window: SLW_SEARCH_RADIUS_KM in both range and azimuth
            half_g         = int(SLW_SEARCH_RADIUS_KM * 1_000.0 / gate_spacing_m)
            g_lo           = max(0, g_idx - half_g)
            g_hi           = min(n_gates, g_idx + half_g + 1)
            half_angle_deg = math.degrees(SLW_SEARCH_RADIUS_KM * 1_000.0 / max(dist_m, 1.0))

            diffs      = (azimuths - bearing_deg + 540.0) % 360.0 - 180.0
            ray_mask   = np.abs(diffs) <= half_angle_deg
            ray_indices = np.where(ray_mask)[0]

            if len(ray_indices) < 3:
                continue

            region = vel[np.ix_(ray_indices, np.arange(g_lo, g_hi))]
            if region.size == 0 or np.all(np.isnan(region)):
                continue

            v_max = float(np.nanmax(region))
            # Bail only if even the sub-severe threshold isn't met.  Catching
            # the strong tier (35-50 kt) is the whole point of this lower
            # check — developing squall lines often sit in that band before
            # any 50+ kt gusts appear.
            if v_max < SLW_STRONG_MS:
                continue

            cell.max_wind_velocity_ms = round(v_max, 1)

            # Per-cell area on the polar grid (gate × ray) at this range
            mid_range_m       = float(ranges_m[min(g_idx, len(ranges_m) - 1)])
            area_per_cell_km2 = (gate_spacing_m / 1_000.0) * (mid_range_m * daz_rad / 1_000.0)

            # Strong tier: ≥ SLW_STRONG_MS over ≥ SLW_STRONG_MIN_SWATH_KM2
            n_strong = int(np.sum((region >= SLW_STRONG_MS) & ~np.isnan(region)))
            strong_area_km2 = n_strong * area_per_cell_km2

            # Severe tier: ≥ SLW_SEVERE_MS over ≥ SLW_MIN_SWATH_KM2 (existing rule)
            n_severe = int(np.sum((region >= SLW_SEVERE_MS) & ~np.isnan(region)))
            severe_area_km2 = n_severe * area_per_cell_km2

            if strong_area_km2 >= SLW_STRONG_MIN_SWATH_KM2:
                cell.strong_wind_detected = True
                cell.strong_wind_swath_km2 = round(strong_area_km2, 1)

            if severe_area_km2 >= SLW_MIN_SWATH_KM2:
                cell.straight_line_wind_detected = True
                logger.debug(
                    f"SLW SEVERE: {cell.cell_id}  V_max={v_max:.1f} m/s  "
                    f"swath={severe_area_km2:.0f} km²"
                )
            elif cell.strong_wind_detected:
                logger.debug(
                    f"SLW STRONG: {cell.cell_id}  V_max={v_max:.1f} m/s  "
                    f"swath={strong_area_km2:.0f} km²"
                )

    def _detect_rear_inflow_jet(
        self, radar, cells: list[TrackedStormCell], systems: list
    ):
        """
        Detect Rear-Inflow Jets (RIJ) in confirmed bow echo MCS systems.

        A RIJ is a channel of strong mid-level inbound air (1–4 km AGL) that
        descends into the rear flank of a bow echo, dramatically accelerating
        surface wind damage at the bow apex.  Only executes when at least one
        MCS system has `bow_echo_detected = True`.

        The search targets a point displaced AGAINST the storm motion vector by
        25 % of the system length behind the system centroid — the canonical
        rear-inflow entry point.  Flags `rij_detected` on all member cells of
        the bow echo system when a strong inbound channel is found.
        """
        if radar is None or not systems:
            return

        bow_systems = [s for s in systems if s.bow_echo_detected]
        if not bow_systems:
            return

        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            return

        try:
            rad_lat      = float(radar.latitude["data"][0])
            rad_lon      = float(radar.longitude["data"][0])
            ranges_m     = np.asarray(radar.range["data"], dtype=float)
            fixed_angles = radar.fixed_angle["data"]
            n_sweeps     = len(fixed_angles)
        except Exception:
            return

        # Voronoi-gated reset: only clear flags for cells whose primary radar
        # is THIS one. A bow echo straddling two radar Voronoi regions should
        # not lose its RIJ flag just because the non-primary radar processed.
        for cell in cells:
            if self._is_primary_radar_for_cell(rad_lat, rad_lon, cell):
                cell.rij_detected = False

        R_e    = 6_371_000.0
        k_refr = 4.0 / 3.0

        def _beam_h_km(r_m: float, elev_deg: float) -> float:
            er = math.radians(elev_deg)
            return (r_m * math.sin(er) + r_m ** 2 / (2.0 * k_refr * R_e)) / 1_000.0

        gate_spacing_m = float(ranges_m[1] - ranges_m[0]) if len(ranges_m) > 1 else 250.0
        cell_lookup    = {c.cell_id: c for c in cells}

        for system in bow_systems:
            # Rear search point: displace centroid 25 % of system length AGAINST motion
            rear_fraction  = 0.25
            rear_dist_km   = system.length_km * rear_fraction
            motion_rad     = math.radians(system.motion_direction_deg)
            cos_lat        = math.cos(math.radians(system.centroid_lat))
            rear_lat = system.centroid_lat - rear_dist_km * math.cos(motion_rad) / 111.0
            rear_lon = system.centroid_lon - rear_dist_km * math.sin(motion_rad) / (111.0 * max(cos_lat, 0.01))

            rear_dist_km_r, rear_bearing = self._latlon_to_polar(
                rad_lat, rad_lon, rear_lat, rear_lon
            )
            rear_dist_m = rear_dist_km_r * 1_000.0
            if rear_dist_m < 5_000 or rear_dist_m > float(ranges_m[-1]):
                continue

            g_idx = int(np.searchsorted(ranges_m, rear_dist_m))
            g_idx = max(0, min(len(ranges_m) - 1, g_idx))
            half_g         = max(3, int(5_000.0 / gate_spacing_m))     # ±5 km in range
            half_angle_deg = math.degrees(10_000.0 / max(rear_dist_m, 1.0))  # ±10 km tangential

            rij_found = False

            for sw in range(n_sweeps):
                elev_deg = float(fixed_angles[sw])
                h_km     = _beam_h_km(rear_dist_m, elev_deg)

                if not (RIJ_HEIGHT_MIN_KM <= h_km <= RIJ_HEIGHT_MAX_KM):
                    continue

                try:
                    ss = int(radar.sweep_start_ray_index["data"][sw])
                    se = int(radar.sweep_end_ray_index["data"][sw])
                except Exception:
                    continue

                az  = np.asarray(radar.azimuth["data"][ss:se + 1], dtype=float)
                vel = np.ma.filled(radar.fields[vel_key]["data"][ss:se + 1], np.nan)
                if vel.shape[0] < 3:
                    continue

                diffs    = (az - rear_bearing + 540.0) % 360.0 - 180.0
                ray_mask = np.abs(diffs) <= half_angle_deg
                if np.sum(ray_mask) < 2:
                    continue

                g_lo   = max(0, g_idx - half_g)
                g_hi   = min(vel.shape[1], g_idx + half_g + 1)
                region = vel[np.ix_(np.where(ray_mask)[0], np.arange(g_lo, g_hi))]

                if region.size == 0 or np.all(np.isnan(region)):
                    continue

                if float(np.nanmin(region)) <= RIJ_INBOUND_MS:
                    rij_found = True
                    logger.debug(
                        f"RIJ: system {system.system_id}  "
                        f"V_in={float(np.nanmin(region)):.1f} m/s  "
                        f"h={h_km:.1f} km AGL"
                    )
                    break  # One confirming tilt is sufficient

            if rij_found:
                for cid in system.cell_ids:
                    c = cell_lookup.get(cid)
                    if c is not None:
                        c.rij_detected = True

    def _score_cells(self, cells: list[TrackedStormCell], timestamp: str):
        """
        Calculate severity scores for all cells using the Phase 4 11-factor matrix.

        Each factor is scored 0-100 independently, then combined via SCORE_WEIGHTS
        (which must sum to 100) to produce a composite 0-100 severity score.
        LLSD is now a dedicated factor (was folded into "rotation"); three new
        kinematic wind factors cover downburst/MARC, straight-line winds, and (via
        the straight-line factor) rear-inflow jets.
        """
        for cell in cells:
            if cell.scan_count < 0:
                cell.severity_score = max(0, cell.severity_score - 10)
                cell.threat_level = self._score_to_threat(cell.severity_score)
                continue

            breakdown: dict[str, int] = {}

            # ── 1. Reflectivity Core ─────────────────────────────────────────
            # Linear 35→70 dBZ.  The SCIT multi-threshold ensures this is the
            # detection-threshold dBZ of the actual cell core, not a merged blob.
            refl_score = int(np.clip(
                (cell.max_reflectivity_dbz - 35) / (70 - 35) * 100, 0, 100
            ))
            breakdown["reflectivity"] = refl_score

            # ── 2. Intensification (Growth Trend) ───────────────────────────
            # Linear-regression slopes from TREND_WINDOW_SCANS scans (≈ 25 min).
            # Rapid intensification — especially of LLSD and rotation velocity —
            # is the strongest pre-warning signal available per TorNet/WAF 2023.
            # Each slope contributes proportionally to its operational threshold;
            # LLSD trend dominates by design.
            def _t_contrib(val, scale, max_pts):
                if val is None or val <= 0:
                    return 0
                return int(min(max_pts, (val / scale) * max_pts))

            trend_score = 50  # neutral baseline
            if cell.trend == "strengthening":
                trend_score = 60
            elif cell.trend == "weakening":
                trend_score = 20

            # Quantitative slopes — primary contributors
            trend_score += _t_contrib(cell.llsd_trend,     0.002, 25)
            trend_score += _t_contrib(cell.rot_vel_trend,  2.0,   15)
            trend_score += _t_contrib(cell.echo_top_trend, 0.3,   10)
            trend_score += _t_contrib(cell.vil_trend,      1.5,    8)
            trend_score += _t_contrib(cell.dbz_trend,      1.5,    7)

            # Decay term for clearly weakening storms (negative trends)
            if cell.llsd_trend is not None and cell.llsd_trend < -0.001:
                trend_score -= 10
            if cell.rot_vel_trend is not None and cell.rot_vel_trend < -1.0:
                trend_score -= 8

            # Sustained mature severe convection — small recognition bonus
            if cell.scan_count > 3 and cell.max_reflectivity_dbz > 55:
                trend_score += 5

            breakdown["growth_trend"] = max(0, min(100, trend_score))

            # ── 3. Rotation (Mesocyclone / TVS) ─────────────────────────────
            # Velocity couplet from grid-based and multi-tilt polar detectors.
            # LLSD is now its own separate factor (see factor 4); no LLSD
            # contribution here to avoid double-counting.
            #
            # Altitude gating (Phase 5):
            #   - Mid-level rotation with NO low-level component is a normal
            #     supercell trait, NOT a tornado precursor — cap at 50.
            #   - Low-level mesocyclone or TVS is unconstrained.
            #   - Depth bonus only applies when the rotation column extends
            #     into the boundary layer (base ≤ 1.5 km AGL).
            rotation_score = 0
            mid_only = cell.mid_level_meso_detected and not cell.low_level_meso_detected
            if cell.rotation_detected and cell.rotation_velocity_ms:
                rotation_score = int(np.clip(
                    (cell.rotation_velocity_ms - 10) / (25 - 10) * 100, 0, 100
                ))
                if cell.tvs_detected and not mid_only:
                    rotation_score = 100
            elif cell.qlcs_meso_detected and cell.qlcs_meso_velocity_ms:
                # QLCS meso: capped at 60 — shorter-lived than supercell mesos
                rotation_score = int(np.clip(
                    (cell.qlcs_meso_velocity_ms - 8) / (20 - 8) * 60, 0, 60
                ))
            # Multi-tilt profile peak can exceed grid-based couplet estimate
            if cell.max_rot_velocity_ms is not None:
                profile_score = int(np.clip(
                    (cell.max_rot_velocity_ms - 10) / (25 - 10) * 100, 0, 100
                ))
                rotation_score = max(rotation_score, profile_score)
            # Mid-level-only cap: the circulation hasn't reached the boundary
            # layer, so tornado risk is materially lower.  LLSD-confirmed cells
            # bypass this because LLSD measures near-surface shear directly.
            if mid_only:
                rotation_score = min(rotation_score, 50)
            # Depth bonus: requires both deep column AND boundary-layer reach.
            # A 3 km deep rotation centred at 5 km AGL is less dangerous than
            # a 3 km deep rotation centred at 1.5 km AGL.
            if (cell.rotation_depth_km is not None
                    and cell.rotation_depth_km >= 3.0
                    and cell.rotation_base_km is not None
                    and cell.rotation_base_km <= LOW_LEVEL_ROT_DEPTH_BONUS_BASE_KM):
                rotation_score = min(100, rotation_score + 10)
            # BWER bonus: a bounded weak echo region indicates an intense,
            # tilted updraft — strongly correlated with tornadogenesis when
            # combined with rotation.  Add modest boost only when rotation is
            # already detected (BWER alone without rotation is not tornadic).
            if cell.bwer_detected and cell.rotation_detected:
                rotation_score = min(100, rotation_score + 8)
            breakdown["rotation"] = rotation_score

            # ── 4. LLSD (Low-Level Azimuthal Shear) ─────────────────────────
            # Piecewise linear across the four operational shear thresholds.
            # This factor now carries the full weight of near-surface rotation
            # that was previously embedded in the rotation score.  LLSD trend
            # contributes via the growth_trend factor — do not double-count.
            llsd_score = 0
            if cell.llsd_max_shear is not None:
                s = cell.llsd_max_shear
                if s >= LLSD_TORNADIC_SHEAR:
                    llsd_score = 100
                elif s >= LLSD_STRONG_SHEAR:
                    llsd_score = int(80 + (s - LLSD_STRONG_SHEAR)
                                     / (LLSD_TORNADIC_SHEAR - LLSD_STRONG_SHEAR) * 20)
                elif s >= LLSD_MESO_SHEAR:
                    llsd_score = int(50 + (s - LLSD_MESO_SHEAR)
                                     / (LLSD_STRONG_SHEAR - LLSD_MESO_SHEAR) * 30)
                elif s >= LLSD_WEAK_SHEAR:
                    llsd_score = int(20 + (s - LLSD_WEAK_SHEAR)
                                     / (LLSD_MESO_SHEAR - LLSD_WEAK_SHEAR) * 30)
            breakdown["llsd"] = llsd_score

            # ── 5. Hail Core Intensity ───────────────────────────────────────
            # Phase 3 upgrade: strict dual-pol co-location (hail_core_pixels)
            # and TBSS physical confirmation both elevate the score ceiling.
            # Phase 5: MESH (Witt 1998) from vertical Z integration drives the
            # primary score when available — direct size estimate beats the
            # qualitative dBZ-only fallback.
            hail_score = 0
            if cell.hail_indicated:
                hail_score = 60
                if cell.max_reflectivity_dbz >= 60:
                    hail_score = 80
                if cell.max_reflectivity_dbz >= 65:
                    hail_score = 100
            elif cell.max_reflectivity_dbz >= HAIL_REFLECTIVITY_DBZ:
                hail_score = 30  # Possible but dual-pol unavailable or ambiguous
            # MESH-based score (preferred when available)
            if cell.mesh_mm is not None:
                m = cell.mesh_mm
                if m >= MESH_GIANT_HAIL_MM:
                    mesh_score = 100
                elif m >= MESH_LARGE_HAIL_MM:
                    mesh_score = int(80 + (m - MESH_LARGE_HAIL_MM)
                                     / (MESH_GIANT_HAIL_MM - MESH_LARGE_HAIL_MM) * 20)
                elif m >= MESH_SIG_HAIL_MM:
                    mesh_score = int(50 + (m - MESH_SIG_HAIL_MM)
                                     / (MESH_LARGE_HAIL_MM - MESH_SIG_HAIL_MM) * 30)
                else:
                    mesh_score = int((m / MESH_SIG_HAIL_MM) * 40)
                hail_score = max(hail_score, mesh_score)
            # TBSS physically confirms large hail (not just inference from Z/ZDR/CC)
            if cell.tbss_detected:
                hail_score = max(hail_score, 90)
            breakdown["hail"] = hail_score

            # ── 6. Downburst / MARC Potential ───────────────────────────────
            # Downburst ΔV is an active surface wind threat (scales continuously).
            # MARC is a mid-level precursor — important but less immediate (cap 60).
            downburst_marc_score = 0
            if cell.downburst_detected and cell.downburst_delta_v_ms is not None:
                # 20 m/s = 30 (floor), 40+ m/s = 100
                downburst_marc_score = int(np.clip(
                    (cell.downburst_delta_v_ms - 20) / 20 * 70 + 30, 30, 100
                ))
            if cell.marc_signature_detected and cell.marc_convergence_ms is not None:
                # -12 m/s = 20, -25+ m/s = 60
                marc_score = int(np.clip(
                    (abs(cell.marc_convergence_ms) - 12) / 13 * 40 + 20, 20, 60
                ))
                downburst_marc_score = max(downburst_marc_score, marc_score)
            breakdown["downburst_marc"] = downburst_marc_score

            # ── 7. Straight-Line Wind Intensity ─────────────────────────────
            # Two-tier wind signature:
            #   strong (35-50 kt sub-severe): partial credit 10-35
            #   severe (50+ kt, broad swath): main credit 20-100
            # Plus RIJ floor for confirmed bow-echo rear inflow.
            slw_score = 0
            v = cell.max_wind_velocity_ms
            if v is not None:
                if v >= SLW_SEVERE_MS:
                    slw_score = int(np.clip(
                        (v - SLW_SEVERE_MS) / (50 - SLW_SEVERE_MS) * 80 + 20,
                        20, 100,
                    ))
                elif v >= SLW_STRONG_MS:
                    # 18 m/s → 10, 25.7 m/s → 35
                    slw_score = int(np.clip(
                        (v - SLW_STRONG_MS) / (SLW_SEVERE_MS - SLW_STRONG_MS) * 25 + 10,
                        10, 35,
                    ))
            if cell.straight_line_wind_detected:
                slw_score = max(slw_score, 40)
            elif cell.strong_wind_detected:
                slw_score = max(slw_score, 20)
            if cell.rij_detected:
                slw_score = max(slw_score, 70)
            breakdown["straight_line"] = slw_score

            # ── 8. Debris Signature (TDS) ────────────────────────────────────
            # Phase 3: graduated by confirmed tilt depth — deeper column means
            # larger/more intense tornado, not just a brief debris loft.
            debris_score = 0
            if cell.debris_signature:
                debris_score = 90
                if cell.tds_tilt_count >= 3:
                    debris_score = 100  # Deep debris column = highest confidence
            breakdown["debris"] = debris_score

            # ── 9. VIL ───────────────────────────────────────────────────────
            # Cell-based multi-tilt VIL (NSSL formula).  Fallback to reflectivity
            # proxy when the vertical profile failed (e.g. nearby radar / low
            # coverage).
            vil_score = 0
            if cell.vil_kg_m2 is not None:
                vil_score = int(np.clip((cell.vil_kg_m2 - 10.0) / (55.0 - 10.0) * 100, 0, 100))
            else:
                if cell.max_reflectivity_dbz >= 50:
                    vil_score = 30
                if cell.max_reflectivity_dbz >= 55:
                    vil_score = 50
                if cell.max_reflectivity_dbz >= 60 and cell.area_km2 > 20:
                    vil_score = 75
                if cell.max_reflectivity_dbz >= 65 and cell.area_km2 > 30:
                    vil_score = 100
            breakdown["vil"] = vil_score

            # ── 10. Cell Top / Overshooting Top ──────────────────────────────
            # Multi-tilt 18 dBZ echo top (real beam height).  6 km = ordinary
            # convection, 14+ km = severe overshooting top.
            top_score = 0
            if cell.cell_top_km is not None:
                top_score = int(np.clip((cell.cell_top_km - 6.0) / (14.0 - 6.0) * 100, 0, 100))
            else:
                if cell.max_reflectivity_dbz >= 45:
                    top_score = 30
                if cell.max_reflectivity_dbz >= 55:
                    top_score = 60
                if cell.max_reflectivity_dbz >= 60:
                    top_score = 80
                if cell.max_reflectivity_dbz >= 65:
                    top_score = 100
            breakdown["cell_top"] = top_score

            # ── 11. Lightning Flash Rate (GLM) ────────────────────────────────
            # Flashes/min within 25 km over the last 5 minutes.
            lightning_score = 0
            if self._glm_service is not None:
                try:
                    fpm = self._glm_service.flash_rate_near(
                        cell.lat, cell.lon, radius_km=25.0, window_minutes=5
                    )
                    lightning_score = int(min(100, fpm * 10))
                except Exception:
                    pass
            breakdown["lightning"] = lightning_score

            # ── Weighted composite ────────────────────────────────────────────
            total = sum(
                breakdown.get(factor, 0) * weight / 100
                for factor, weight in SCORE_WEIGHTS.items()
            )
            severity = round(min(100, max(0, total)))

            cell.severity_score = severity
            cell.threat_level   = self._score_to_threat(severity)
            cell.score_breakdown = breakdown
            cell.last_updated   = timestamp

    def _generate_forecasts(self, cells: list[TrackedStormCell]):
        """Generate forecast tracks by linear extrapolation."""
        for cell in cells:
            if cell.motion_speed_kph <= 0 or cell.scan_count < 2:
                cell.forecast_track = []
                continue

            forecasts = []
            speed_km_per_min = cell.motion_speed_kph / 60.0
            direction_rad = math.radians(cell.motion_direction_deg)

            for minutes in [15, 30, 45, 60]:
                dist_km = speed_km_per_min * minutes
                # Project position
                dlat = dist_km * math.cos(direction_rad) / 111.0
                dlon = dist_km * math.sin(direction_rad) / (
                    111.0 * math.cos(math.radians(cell.lat))
                )
                forecasts.append({
                    "lat": round(cell.lat + dlat, 4),
                    "lon": round(cell.lon + dlon, 4),
                    "minutes_ahead": minutes,
                })

            cell.forecast_track = forecasts

    def _expire_all_cells(self, timestamp: str):
        """Mark all cells as dissipated when no cells detected."""
        expired = []
        for cell_id, cell in self._tracked_cells.items():
            if cell.scan_count > 0:
                cell.scan_count = -1
                cell.trend = "weakening"
                cell.last_updated = timestamp
            else:
                expired.append(cell_id)

        for cell_id in expired:
            del self._tracked_cells[cell_id]

    def _grid_to_image_latlon(self, cy: int, cx: int) -> tuple[float, float]:
        """Convert a grid (row, col) index to lat/lon in the image's coordinate system.

        The radar PNG is rendered in AEQD space (linear metres from site centre)
        and placed on Leaflet via a simple lat/lon bounding-box.  Leaflet stretches
        the image linearly between the SW and NE corner bounds, which is NOT the
        same as the true geographic position (AEQD ≠ linear lat/lon).

        To make cell markers land *on* their echo in the displayed image, we compute
        the apparent lat/lon that Leaflet would assign to the pixel at (cy, cx):
          x_m = -range_m + cx * res_m   (AEQD metres, east positive)
          y_m = -range_m + cy * res_m   (AEQD metres, north positive)
          col_frac = (x_m + range_m) / (2 * range_m)   # 0 = left, 1 = right
          row_frac = (range_m - y_m) / (2 * range_m)   # 0 = top(N), 1 = bottom(S)
          lon = bounds.west + col_frac * (bounds.east  - bounds.west)
          lat = bounds.north - row_frac * (bounds.north - bounds.south)
        """
        if self._bounds is None or self._grid_lat is None:
            # Fall back to pyproj-derived coordinates
            return float(self._grid_lat[cy, cx]), float(self._grid_lon[cy, cx])

        res_m = self._grid_res_km * 1000.0
        x_m = -self._range_m + cx * res_m
        y_m = -self._range_m + cy * res_m

        col_frac = (x_m + self._range_m) / (2.0 * self._range_m)
        row_frac = (self._range_m - y_m) / (2.0 * self._range_m)

        b = self._bounds
        lat = b["north"] - row_frac * (b["north"] - b["south"])
        lon = b["west"]  + col_frac * (b["east"]  - b["west"])
        return float(lat), float(lon)

    def _latlon_to_grid(self, lat: float, lon: float) -> tuple[int, int]:
        """Convert lat/lon to nearest grid index."""
        if self._grid_lat is None or self._grid_lon is None:
            raise ValueError("Grid coordinates not available")

        # Find nearest grid point
        lat_diff = np.abs(self._grid_lat - lat)
        lon_diff = np.abs(self._grid_lon - lon)
        dist = lat_diff + lon_diff  # Manhattan distance (fast approximation)
        idx = np.unravel_index(np.argmin(dist), dist.shape)
        return int(idx[0]), int(idx[1])

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine distance in km."""
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2
        )
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2 in degrees."""
        dlon = math.radians(lon2 - lon1)
        lat1_r = math.radians(lat1)
        lat2_r = math.radians(lat2)
        x = math.sin(dlon) * math.cos(lat2_r)
        y = (
            math.cos(lat1_r) * math.sin(lat2_r)
            - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
        )
        bearing = math.degrees(math.atan2(x, y))
        return (bearing + 360) % 360

    @staticmethod
    def _circular_mean(angles_deg: list[float], weights: list[float]) -> float:
        """Weighted circular mean of angles in degrees."""
        sin_sum = sum(w * math.sin(math.radians(a)) for a, w in zip(angles_deg, weights))
        cos_sum = sum(w * math.cos(math.radians(a)) for a, w in zip(angles_deg, weights))
        return (math.degrees(math.atan2(sin_sum, cos_sum)) + 360) % 360

    @staticmethod
    def _score_to_threat(score: int) -> str:
        """Convert severity score to threat level string."""
        if score <= 25:
            return "minimal"
        elif score <= 50:
            return "moderate"
        elif score <= 70:
            return "significant"
        elif score <= 85:
            return "severe"
        else:
            return "extreme"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[StormTrackingService] = None


def get_storm_tracking_service() -> Optional[StormTrackingService]:
    return _service


async def start_storm_tracking_service() -> bool:
    """Start the storm tracking service."""
    global _service
    try:
        from scipy import ndimage  # Verify dependency
    except ImportError:
        logger.warning("scipy is required for storm tracking. Install with: pip install scipy")
        return False

    _service = StormTrackingService()
    _service._running = True
    # Best-effort load of trained ML rotation model — inert if not present.
    try:
        _service.load_rotation_model()
    except Exception as e:
        logger.warning(f"Rotation model loader raised: {e}")
    logger.info("Storm tracking service started")
    return True


async def stop_storm_tracking_service():
    """Stop the storm tracking service."""
    global _service
    if _service:
        _service._running = False
        _service = None
        logger.info("Storm tracking service stopped")
