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

# Scoring weights (must sum to 100)
SCORE_WEIGHTS = {
    "reflectivity": 18,
    "growth_trend": 10,
    "hail": 18,
    "rotation": 24,
    "debris": 15,
    "vil": 5,
    "cell_top": 5,
    "lightning": 5,
}

# Detection thresholds
CELL_DETECT_DBZ = 35  # Minimum reflectivity for cell detection
CELL_MIN_AREA_KM2 = 5  # Minimum cell area to filter noise
MAX_MATCH_DISTANCE_KM = 20  # Max distance for cell matching across scans
# Minimum time between motion vector updates.  When two active sites
# produce scans only seconds apart, cross-site parallax (3–5 km) divided
# by a tiny Δt produces physically impossible speeds (300–600 kph).
# Below this threshold we keep the previous motion vector unchanged.
MIN_MOTION_DT_SECONDS = 120.0   # 2 minutes
MAX_STORM_SPEED_KPH   = 175.0   # Hard physical cap (extreme derecho upper bound)
MESO_VELOCITY_THRESHOLD_MS = 15  # Minimum rotational velocity for mesocyclone
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
    # Multi-tilt rotation profile
    max_rot_velocity_ms: Optional[float] = None  # Peak rotational velocity anywhere in column
    max_rot_height_km: Optional[float] = None    # Height where peak rotation occurs
    rotation_profile: list = field(default_factory=list)  # [{height_km, shear, rot_ms}]
    rotation_depth_km: Optional[float] = None    # Vertical extent of rotation ≥ meso threshold
    cell_base_km: Optional[float] = None       # Lowest 18 dBZ echo height (AGL)
    max_ref_height_km: Optional[float] = None  # Height at which max reflectivity occurs
    centroid_height_km: Optional[float] = None # Reflectivity-weighted mean height
    depth_km: Optional[float] = None           # cell_top - cell_base

    def to_dict(self) -> dict:
        return asdict(self)


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

        # GLM lightning service reference (optional)
        self._glm_service = None

        # Callbacks
        self.on_cells_updated: Optional[Callable] = None   # (cells: list[TrackedStormCell]) -> None
        self.on_systems_updated: Optional[Callable] = None  # (systems: list[MCSSystem]) -> None

        self._running = False

    def set_glm_service(self, glm_svc):
        """Wire in the GLM lightning service for flash-rate scoring."""
        self._glm_service = glm_svc

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

        return cells or []

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

            # Step 1: Identify cells from reflectivity
            raw_cells = self._identify_cells(grid)
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

            # Step 5: Score each cell (uses both rotation + qlcs_meso flags)
            self._score_cells(matched, timestamp)

            # Step 6: Classify linear systems (MCS/QLCS/bow echo)
            systems = self._detect_mcs_systems(matched, timestamp)
            self._tracked_systems = {s.system_id: s for s in systems}

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

    def _identify_cells(self, grid) -> list[_InternalCell]:
        """Identify storm cells using reflectivity thresholding + connected components."""
        from scipy import ndimage

        if "reflectivity" not in grid.fields:
            return []

        refl = grid.fields["reflectivity"]["data"][0]  # First vertical level
        refl_filled = np.ma.filled(refl, -999)

        # Binary threshold
        binary = refl_filled >= CELL_DETECT_DBZ
        if not np.any(binary):
            return []

        # Connected component labeling
        labeled, num_features = ndimage.label(binary)
        if num_features == 0:
            return []

        cells = []
        pixel_area_km2 = self._grid_res_km ** 2

        for label_id in range(1, num_features + 1):
            mask = labeled == label_id
            area_km2 = np.sum(mask) * pixel_area_km2

            # Filter small cells
            if area_km2 < CELL_MIN_AREA_KM2:
                continue

            # Find centroid
            ys, xs = np.where(mask)
            cy = int(np.mean(ys))
            cx = int(np.mean(xs))

            # Max reflectivity in cell
            cell_refl = refl_filled[mask]
            max_dbz = float(np.nanmax(cell_refl))

            # Get lat/lon using image-aligned coordinate system so that markers
            # overlay the correct pixel in the Leaflet ImageOverlay.
            try:
                lat, lon = self._grid_to_image_latlon(cy, cx)
            except (IndexError, TypeError, ValueError):
                continue

            # Bounding box
            bbox = (int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max()))

            # Multi-threshold structure analysis
            dbz_50 = float(np.sum(refl_filled[mask] >= 50) * pixel_area_km2)
            dbz_55 = float(np.sum(refl_filled[mask] >= 55) * pixel_area_km2)
            dbz_60 = float(np.sum(refl_filled[mask] >= 60) * pixel_area_km2)

            cells.append(_InternalCell(
                centroid_y=cy,
                centroid_x=cx,
                lat=lat,
                lon=lon,
                max_dbz=max_dbz,
                area_km2=area_km2,
                pixel_mask=mask,
                bbox=bbox,
                dbz_50_area_km2=dbz_50,
                dbz_55_area_km2=dbz_55,
                dbz_60_area_km2=dbz_60,
            ))

        logger.debug(f"Identified {len(cells)} storm cells")
        return cells

    def _analyze_dual_pol(self, grid, radar, cells: list[_InternalCell]):
        """Analyze dual-pol products within each cell for hail/debris detection."""
        has_cc = "cross_correlation_ratio" in grid.fields
        has_zdr = "differential_reflectivity" in grid.fields

        if not has_cc and not has_zdr:
            return

        cc_data = None
        zdr_data = None
        if has_cc:
            cc_data = np.ma.filled(grid.fields["cross_correlation_ratio"]["data"][0], np.nan)
        if has_zdr:
            zdr_data = np.ma.filled(grid.fields["differential_reflectivity"]["data"][0], np.nan)

        for cell in cells:
            mask = cell.pixel_mask
            if cc_data is not None:
                cc_vals = cc_data[mask]
                cc_valid = cc_vals[~np.isnan(cc_vals)]
                if len(cc_valid) > 0:
                    cell.mean_cc = float(np.nanmean(cc_valid))
                    cell.min_cc = float(np.nanmin(cc_valid))

            if zdr_data is not None:
                zdr_vals = zdr_data[mask]
                zdr_valid = zdr_vals[~np.isnan(zdr_vals)]
                if len(zdr_valid) > 0:
                    cell.mean_zdr = float(np.nanmean(zdr_valid))

    def _match_cells(self, new_cells: list[_InternalCell], timestamp: str) -> list[TrackedStormCell]:
        """Match new cells to existing tracked cells using centroid proximity."""
        now = timestamp
        updated_tracked = {}
        matched_new_indices = set()
        matched_old_ids = set()

        # Build list of existing cells with predicted positions
        existing = list(self._tracked_cells.values())

        # Match by nearest centroid distance
        if existing and new_cells:
            # Compute distance matrix
            distances = np.zeros((len(existing), len(new_cells)))
            for i, old_cell in enumerate(existing):
                for j, new_cell in enumerate(new_cells):
                    distances[i, j] = self._haversine_km(
                        old_cell.lat, old_cell.lon,
                        new_cell.lat, new_cell.lon,
                    )

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

        # Hail indicator from dual-pol
        hail = False
        if new.max_dbz >= HAIL_REFLECTIVITY_DBZ:
            if new.mean_cc is not None and HAIL_CC_RANGE[0] <= new.mean_cc <= HAIL_CC_RANGE[1]:
                hail = True
            if new.mean_zdr is not None and abs(new.mean_zdr) <= HAIL_ZDR_THRESHOLD:
                hail = True
            if new.max_dbz >= 60:
                hail = True  # Very high reflectivity strongly suggests hail

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
        # Check for velocity field
        vel_field = None
        for fname in ["velocity_dealiased", "velocity"]:
            if fname in grid.fields:
                vel_field = fname
                break

        if vel_field is None:
            return

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

            valid_vel = region_vel[valid_mask]

            # Outlier rejection (Tukey IQR method): a single aliased or
            # range-folded gate at ±50 m/s can fake a 33 m/s "meso" by
            # inflating the region max/min.  Remove gates beyond Q1−1.5·IQR
            # and Q3+1.5·IQR before computing the couplet.
            q1, q3 = np.percentile(valid_vel, 25), np.percentile(valid_vel, 75)
            iqr = q3 - q1
            if iqr > 0:
                lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                valid_vel = valid_vel[(valid_vel >= lo) & (valid_vel <= hi)]
            if len(valid_vel) < 5:
                continue

            # Find velocity couplet: max inbound (negative) and outbound (positive)
            max_outbound = float(np.nanmax(valid_vel))
            max_inbound = float(np.nanmin(valid_vel))

            # Rotational velocity = (outbound - inbound) / 2
            rot_velocity = (max_outbound - max_inbound) / 2

            # Check couplet diameter (distance between max inbound and outbound)
            if rot_velocity >= MESO_VELOCITY_THRESHOLD_MS:
                outbound_pos = np.unravel_index(
                    np.nanargmax(region_vel), region_vel.shape
                )
                inbound_pos = np.unravel_index(
                    np.nanargmin(region_vel), region_vel.shape
                )
                couplet_dist_km = (
                    math.sqrt(
                        (outbound_pos[0] - inbound_pos[0]) ** 2
                        + (outbound_pos[1] - inbound_pos[1]) ** 2
                    )
                    * self._grid_res_km
                )

                if couplet_dist_km <= MESO_MAX_DIAMETER_KM:
                    cell.rotation_detected = True
                    cell.rotation_velocity_ms = round(rot_velocity, 1)

                    # TVS check
                    if rot_velocity >= TVS_VELOCITY_THRESHOLD_MS:
                        cell.tvs_detected = True

                    # Debris signature check (NWS dual-pol TDS criteria):
                    #   1. CC < 0.80 in the rotation region
                    #   2. Rotation ≥ TDS_MIN_ROTATION_MS (strong circulation required)
                    #   3. Z ≥ TDS_MIN_REFL_DBZ in the same region (scatterers present)
                    #   4. Beam height ≤ TDS_MAX_BEAM_HEIGHT_KM (near-surface beam)
                    # A plain rain cell can have a weak couplet + slightly reduced CC
                    # from ice aloft; that is NOT a TDS.  A hail core has low CC but
                    # no rotation; that is also NOT a TDS.
                    if cc_data is not None:
                        rot_cc = cc_data[y_min:y_max, x_min:x_max]
                        rot_cc_valid = rot_cc[~np.isnan(rot_cc)]
                        if len(rot_cc_valid) > 0:
                            min_cc = float(np.nanmin(rot_cc_valid))
                            rot_vel_for_tds = cell.rotation_velocity_ms or 0.0
                            tds_candidate = (
                                min_cc < DEBRIS_CC_THRESHOLD
                                and rot_vel_for_tds >= TDS_MIN_ROTATION_MS
                            )
                            if tds_candidate:
                                tds_allowed = True

                                # Check minimum reflectivity in the region
                                if refl_data is not None:
                                    rot_refl = refl_data[y_min:y_max, x_min:x_max]
                                    rot_refl_valid = rot_refl[~np.isnan(rot_refl)]
                                    if len(rot_refl_valid) > 0:
                                        if float(np.nanmax(rot_refl_valid)) < TDS_MIN_REFL_DBZ:
                                            tds_allowed = False

                                # Beam height guard
                                if tds_allowed and rad_lat is not None:
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
                                            tds_allowed = False
                                            logger.debug(
                                                f"TDS suppressed for {cell.cell_id}: "
                                                f"lowest beam at {h_km:.2f} km AGL "
                                                f"(>{TDS_MAX_BEAM_HEIGHT_KM} km) "
                                                f"at {dist_km:.0f} km range"
                                            )
                                    except Exception:
                                        pass

                                if tds_allowed:
                                    cell.debris_signature = True
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

        # Find the lowest sweep that has velocity data
        vel_key = None
        for k in ("velocity_dealiased", "velocity"):
            if k in radar.fields:
                vel_key = k
                break
        if vel_key is None:
            return

        try:
            fixed_angles = radar.fixed_angle["data"]
        except Exception:
            return

        # Pick the sweep with the smallest elevation ≤ LLSD_MAX_ELEVATION_DEG
        candidate_sweeps = [
            (i, float(a)) for i, a in enumerate(fixed_angles)
            if float(a) <= LLSD_MAX_ELEVATION_DEG
        ]
        if not candidate_sweeps:
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
            if dist_m < 5_000 or dist_m > float(ranges_m[-1]):
                continue
            # Voronoi: only analyse cells owned by this radar
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
                continue

            peak = float(np.nanmax(np.abs(window)))
            cell.llsd_max_shear = round(peak, 5)
            cell.llsd_elevation_deg = round(sweep_elev, 2)
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
            cell.rotation_profile = []

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

    def _score_cells(self, cells: list[TrackedStormCell], timestamp: str):
        """Calculate severity scores for all cells."""
        for cell in cells:
            if cell.scan_count < 0:
                cell.severity_score = max(0, cell.severity_score - 10)
                cell.threat_level = self._score_to_threat(cell.severity_score)
                continue

            breakdown = {}

            # 1. Reflectivity score (0-100, linear from 35 to 70 dBZ)
            refl_score = np.clip(
                (cell.max_reflectivity_dbz - 35) / (70 - 35) * 100, 0, 100
            )
            breakdown["reflectivity"] = round(float(refl_score))

            # 2. Growth trend score
            trend_score = 50  # Default: steady
            if cell.trend == "strengthening":
                trend_score = 80
            elif cell.trend == "weakening":
                trend_score = 20
            # Boost if consistently strong
            if cell.scan_count > 3 and cell.max_reflectivity_dbz > 55:
                trend_score = min(100, trend_score + 20)
            breakdown["growth_trend"] = trend_score

            # 3. Hail score
            hail_score = 0
            if cell.hail_indicated:
                hail_score = 60
                if cell.max_reflectivity_dbz >= 60:
                    hail_score = 80
                if cell.max_reflectivity_dbz >= 65:
                    hail_score = 100
            elif cell.max_reflectivity_dbz >= HAIL_REFLECTIVITY_DBZ:
                hail_score = 30  # Possible but not confirmed by dual-pol
            breakdown["hail"] = hail_score

            # 4. Rotation score — supercell meso takes priority; QLCS meso gives partial credit
            rotation_score = 0
            if cell.rotation_detected and cell.rotation_velocity_ms:
                # Supercell mesocyclone: scale 15→25 m/s = 50→100
                rotation_score = np.clip(
                    (cell.rotation_velocity_ms - 10) / (25 - 10) * 100, 0, 100
                )
                if cell.tvs_detected:
                    rotation_score = 100
            elif cell.qlcs_meso_detected and cell.qlcs_meso_velocity_ms:
                # QLCS meso: capped at 60 — dangerous but shorter-lived than supercell mesos
                rotation_score = np.clip(
                    (cell.qlcs_meso_velocity_ms - 8) / (20 - 8) * 60, 0, 60
                )

            # LLSD (low-level azimuthal shear) — can override above if tornadic-class,
            # otherwise augments the rotation score.  Low-level shear is what actually
            # kills people, so we weight it heavily.
            if cell.llsd_max_shear is not None:
                s = cell.llsd_max_shear
                if s >= LLSD_TORNADIC_SHEAR:
                    rotation_score = 100
                elif s >= LLSD_STRONG_SHEAR:
                    # Strong LLSD alone is worth ~85 regardless of mid-level couplet
                    rotation_score = max(rotation_score, 85)
                elif s >= LLSD_MESO_SHEAR:
                    # Weak meso-class low-level shear: floor at 50
                    rotation_score = max(rotation_score, 50)
                elif s >= LLSD_WEAK_SHEAR:
                    rotation_score = max(rotation_score, 25)
            breakdown["rotation"] = round(float(rotation_score))

            # 5. Debris signature score
            debris_score = 0
            if cell.debris_signature:
                debris_score = 100  # Binary: confirmed TDS is always max severity
            breakdown["debris"] = debris_score

            # 6. VIL score — use true multi-tilt integrated VIL when available.
            # NSSL thresholds: <15 = weak, 15-30 = moderate, 30-50 = high, 50+ = severe
            vil_score = 0
            if cell.vil_kg_m2 is not None:
                vil_score = int(np.clip((cell.vil_kg_m2 - 10.0) / (55.0 - 10.0) * 100, 0, 100))
            else:
                # Fallback — reflectivity proxy if vertical profile failed
                if cell.max_reflectivity_dbz >= 50:
                    vil_score = 30
                if cell.max_reflectivity_dbz >= 55:
                    vil_score = 50
                if cell.max_reflectivity_dbz >= 60 and cell.area_km2 > 20:
                    vil_score = 75
                if cell.max_reflectivity_dbz >= 65 and cell.area_km2 > 30:
                    vil_score = 100
            breakdown["vil"] = vil_score

            # 7. Cell top height — from real multi-tilt 18 dBZ echo-top when available,
            # otherwise approximate from max reflectivity intensity.
            top_score = 0
            if cell.cell_top_km is not None:
                # 6 km = pedestrian convection, 12+ km = severe overshooting top
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

            # 8. Lightning flash rate (GLM, flashes/min within 25 km over last 5 min)
            # 0 flashes/min = 0, 1 = 30, 5 = 70, 10+ = 100
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

            # Weighted composite score
            total = sum(
                breakdown.get(factor, 0) * weight / 100
                for factor, weight in SCORE_WEIGHTS.items()
            )
            severity = round(min(100, max(0, total)))

            cell.severity_score = severity
            cell.threat_level = self._score_to_threat(severity)
            cell.score_breakdown = breakdown
            cell.last_updated = timestamp

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
    logger.info("Storm tracking service started")
    return True


async def stop_storm_tracking_service():
    """Stop the storm tracking service."""
    global _service
    if _service:
        _service._running = False
        _service = None
        logger.info("Storm tracking service stopped")
