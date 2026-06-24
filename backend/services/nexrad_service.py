"""
NEXRAD Level 2 radar data service.
Downloads volume scans from AWS S3, processes with Py-ART, and renders
georeferenced PNG images for frontend display.
"""

import asyncio
import io
import logging
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Lazy imports for optional heavy dependencies
pyart = None
nexradaws = None


def _ensure_imports():
    """Lazily import heavy radar processing libraries."""
    global pyart, nexradaws
    if pyart is None:
        try:
            import pyart as _pyart
            pyart = _pyart
            logger.info("Py-ART loaded successfully")
        except ImportError:
            raise ImportError(
                "arm-pyart is required for NEXRAD radar. "
                "Install with: pip install arm-pyart"
            )
    if nexradaws is None:
        try:
            import nexradaws as _nexradaws
            nexradaws = _nexradaws
            logger.info("nexradaws loaded successfully")
        except ImportError:
            raise ImportError(
                "nexradaws is required for NEXRAD radar. "
                "Install with: pip install nexradaws"
            )


# ---------------------------------------------------------------------------
# Color tables matching professional radar applications
# ---------------------------------------------------------------------------

def _build_reflectivity_cmap():
    """RadarScope-style reflectivity colormap (BR product color table).
    vmin=-20, vmax=80 dBZ  →  normalized position = (dBZ + 20) / 100
    Solid bands use step-function; gradient bands interpolate within the band.
    Discontinuities at band edges handled via epsilon-offset duplicate positions.
    """
    from matplotlib.colors import LinearSegmentedColormap

    def p(dbz):
        return (dbz + 20) / 100.0

    def c(r, g, b, a=0.9):
        return (r / 255.0, g / 255.0, b / 255.0, a)

    eps = 1e-4

    # List of (normalized_position, rgba) with duplicates for step/discontinuity edges
    data = [
        # Below -15: transparent (sub-noise floor)
        (p(-20),        c(0, 0, 0, 0.0)),
        (p(-15),        c(0, 0, 0, 0.0)),
        # 5 dBZ: dark navy – solid band to 17.5
        (p(5) - eps,    c(0, 0, 0, 0.0)),
        (p(5),          c(29, 37, 60)),
        (p(17.5) - eps, c(29, 37, 60)),
        # 17.5: steel blue – solid band to 22.5
        (p(17.5),       c(89, 155, 171)),
        (p(22.5) - eps, c(89, 155, 171)),
        # 22.5: green – solid band to 32.5
        (p(22.5),       c(33, 186, 72)),
        (p(32.5) - eps, c(33, 186, 72)),
        # 32.5: dark green – solid band to 37.5
        (p(32.5),       c(5, 101, 1)),
        (p(37.5) - eps, c(5, 101, 1)),
        # 37.5–42.5: yellow gradient
        (p(37.5),       c(251, 252, 0)),
        (p(42.5) - eps, c(199, 176, 0)),
        # 42.5–50: orange gradient (jump at 42.5)
        (p(42.5),       c(253, 149, 2)),
        (p(50) - eps,   c(172, 92, 2)),
        # 50–60: red gradient (jump at 50)
        (p(50),         c(253, 38, 0)),
        (p(60) - eps,   c(135, 43, 22)),
        # 60–70: magenta gradient (jump at 60)
        (p(60),         c(193, 148, 179, 0.95)),
        (p(70) - eps,   c(200, 23, 119, 0.95)),
        # 70–75: purple gradient (jump at 70)
        (p(70),         c(165, 2, 215, 0.95)),
        (p(75) - eps,   c(64, 0, 146, 0.95)),
        # 75–80: cyan gradient (jump at 75)
        (p(75),         c(135, 255, 253, 1.0)),
        (p(80) - eps,   c(54, 120, 142, 1.0)),
        # 80+: brownish → dark red (clip at vmax)
        (p(80),         c(173, 99, 64, 1.0)),
    ]

    return LinearSegmentedColormap.from_list("radarscope_ref", data, N=512)


def _build_velocity_cmap():
    """RadarScope BV velocity colormap.
    Source palette in knots; radar data is in m/s.  vmin=-35, vmax=35 m/s.
    KTS→m/s: divide by 1.9426.  Normalized pos = (m/s + 35) / 70.
    Stops outside ±35 m/s are clamped to 0.0/1.0 with an interpolated colour.
    """
    from matplotlib.colors import LinearSegmentedColormap

    VMIN, VMAX = -35.0, 35.0
    KTS = 1.9426

    def ms(kts):
        return kts / KTS

    def pos(kts):
        return max(0.0, min(1.0, (ms(kts) - VMIN) / (VMAX - VMIN)))

    def c(r, g, b, a=0.9):
        return (r / 255.0, g / 255.0, b / 255.0, a)

    eps = 1e-4

    # All stops strictly in increasing position order.
    # -120 kts = -61.8 m/s → clamped to pos 0.0  (colour: dark blue)
    # -50 kts  = -25.7 m/s → pos 0.132
    # -10 kts  =  -5.1 m/s → pos 0.426
    #   0 kts  =   0.0 m/s → pos 0.500
    #  10 kts  =   5.1 m/s → pos 0.574
    #  30 kts  =  15.4 m/s → pos 0.721
    #  60 kts  =  30.9 m/s → pos 0.941
    # 120 kts  =  61.8 m/s → clamped to pos 1.0  (colour: dark orange)
    data = [
        (0.0,               c(0, 0, 155)),          # ≤ -120 kts: dark blue
        (pos(-50),          c(0, 255, 255)),         # -50 kts: cyan
        (pos(-10),          c(0, 102, 0)),           # -10 kts: dark green
        (pos(0) - eps,      c(128, 128, 128, 0.6)), # just below 0: gray
        (pos(0),            c(128, 128, 128, 0.6)), # 0 kts: gray
        (pos(0) + eps,      c(96, 13, 23)),          # just above 0: dark red
        (pos(10),           c(96, 13, 23)),          # 10 kts: dark red
        (pos(30),           c(200, 0, 0)),           # 30 kts: red
        (pos(60),           c(255, 255, 0)),         # 60 kts: yellow
        (1.0,               c(120, 60, 0)),          # ≥ 120 kts: dark orange
    ]

    return LinearSegmentedColormap.from_list("radarscope_vel", data, N=512)


def _build_cc_cmap():
    """Correlation coefficient colormap: purple → blue → green → yellow."""
    from matplotlib.colors import LinearSegmentedColormap
    colors = [
        (0.0, (0.5, 0.0, 0.5, 0.9)),        # 0.20 purple
        (0.18, (0.6, 0.2, 0.8, 0.9)),        # 0.35 violet
        (0.35, (0.0, 0.0, 1.0, 0.9)),        # 0.50 blue
        (0.53, (0.0, 0.8, 0.8, 0.9)),        # 0.65 cyan
        (0.71, (0.0, 0.8, 0.0, 0.9)),        # 0.80 green
        (0.82, (0.5, 1.0, 0.0, 0.9)),        # 0.90 yellow-green
        (0.88, (1.0, 1.0, 0.0, 0.9)),        # 0.95 yellow
        (0.94, (1.0, 0.65, 0.0, 0.9)),       # 1.00 orange
        (1.0, (1.0, 1.0, 1.0, 0.9)),         # 1.05 white
    ]
    positions = [c[0] for c in colors]
    rgba = [c[1] for c in colors]
    r = [(p, c[0], c[0]) for p, c in zip(positions, rgba)]
    g = [(p, c[1], c[1]) for p, c in zip(positions, rgba)]
    b = [(p, c[2], c[2]) for p, c in zip(positions, rgba)]
    a = [(p, c[3], c[3]) for p, c in zip(positions, rgba)]
    cdict = {"red": r, "green": g, "blue": b, "alpha": a}
    return LinearSegmentedColormap("correlation_coefficient", cdict, N=256)


# Product configuration
RADAR_PRODUCTS = {
    "reflectivity": {
        "field": "reflectivity",
        "vmin": -20,
        "vmax": 80,
        "label": "Reflectivity (dBZ)",
        "build_cmap": _build_reflectivity_cmap,
    },
    "velocity": {
        "field": "velocity",
        "vmin": -35,
        "vmax": 35,
        "label": "Velocity (m/s)",
        "build_cmap": _build_velocity_cmap,
    },
    "cross_correlation_ratio": {
        "field": "cross_correlation_ratio",
        "vmin": 0.2,
        "vmax": 1.05,
        "label": "Correlation Coefficient",
        "build_cmap": _build_cc_cmap,
    },
    # Storm-Relative Velocity: rendered from the velocity field with the
    # mean storm motion radial component subtracted at each ray.  Lets the
    # operator see rotation signatures that are masked by ground-relative
    # storm motion (e.g., a fast-moving supercell where half the meso is
    # cancelled out by translation in ground-relative view).
    "storm_relative_velocity": {
        "field": "velocity",
        "vmin": -35,
        "vmax": 35,
        "label": "Storm-Relative Velocity (m/s)",
        "build_cmap": _build_velocity_cmap,
    },
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RadarFrame:
    """A single radar frame. Live frames carry raw binary polar data; oneshot
    frames (social graphics) carry an image_path written by Pillow."""
    product: str
    bounds: dict  # {south, north, west, east}
    timestamp: str  # ISO format
    site: str
    elevation: float
    frame_id: str = ""
    # Binary polar data path (live WebSocket frames)
    binary_data: Optional[bytes] = None
    # File-backed path (oneshot_frame / social graphics only)
    image_path: Optional[str] = None
    image_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "product": self.product,
            "frame_id": self.frame_id,
            "has_binary": self.binary_data is not None,
            "bounds": self.bounds,
            "timestamp": self.timestamp,
            "site": self.site,
            "elevation": self.elevation,
        }


@dataclass
class VolumeScanData:
    """Processed volume scan data passed to storm tracking."""
    site: str
    timestamp: str
    radar_object: object  # pyart Radar object
    grid: object  # pyart Grid object (cartesian)
    bounds: dict
    elevation_angles: list[float] = field(default_factory=list)


MAX_ACTIVE_SITES = 3


@dataclass
class RadarStatus:
    """Current state of the radar service."""
    enabled: bool = False
    active_sites: list[str] = field(default_factory=list)
    last_update: Optional[str] = None
    processing: bool = False
    available_products: list[str] = field(default_factory=lambda: list(RADAR_PRODUCTS.keys()))
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "active_sites": self.active_sites,
            "active_site": self.active_sites[0] if self.active_sites else "",  # backward compat
            "last_update": self.last_update,
            "processing": self.processing,
            "available_products": self.available_products,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class NexradService:
    """Downloads, processes, and renders NEXRAD Level 2 radar data."""

    def __init__(self):
        from backend.config.settings import get_settings
        settings = get_settings()

        default_site = settings.nexrad_default_site.upper()
        self._active_sites: list[str] = [default_site]
        self._poll_interval: int = settings.nexrad_poll_interval
        self._history_count: int = settings.nexrad_history_count
        self._grid_resolution_km: float = settings.nexrad_grid_resolution_km
        self._max_range_km: int = settings.nexrad_max_range_km

        # Data directory
        self._data_dir = Path(settings.data_dir) / "radar"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Frame cache: {site: {product: [RadarFrame, ...]}} most recent last
        self._frames: dict[str, dict[str, list[RadarFrame]]] = {
            default_site: {p: [] for p in RADAR_PRODUCTS}
        }
        # Last processed volume scan data per site (for storm tracking)
        self._last_volume: dict[str, VolumeScanData] = {}
        self._last_scan_key: dict[str, Optional[str]] = {default_site: None}

        # Per-site processing flags — replaces the old global processing lock.
        # Multiple sites can be in Phase 1 concurrently; the global `processing`
        # flag exposed on status is derived as `any(...)`.  This eliminates the
        # cascading lock that caused new polls to be skipped while a slow site
        # finished its Phase 1.
        self._site_processing: dict[str, bool] = {}

        # Per-site diagnostic state — populated throughout the pipeline so the
        # /api/radar/diagnostic endpoint and the watchdog log line can pinpoint
        # exactly where latency is being introduced (S3 lag vs download vs
        # parse vs render vs grid).  Each entry is a flat dict of stage
        # timings + the latest "what's on S3" vs "what we're showing" pair.
        self._diagnostics: dict[str, dict] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

        # Anti-stale guards.  Track the most recent scan timestamp that has
        # been broadcast and the most recent scan that's been fed to storm
        # tracking.  Used by `finalize_phase1_async` to drop a late-arriving
        # older scan (e.g., archive bucket finishing a scan that chunks
        # already broadcast a newer version of) and to prevent the same
        # volume being gridded twice (chunks-complete + archive races).
        self._last_broadcast_scan_dt: dict[str, datetime] = {}
        self._last_grid_scan_dt: dict[str, datetime] = {}

        # Mean storm motion (degrees, kph) — pushed by the storm tracking
        # service after each grid completes.  Used by the storm-relative
        # velocity product to subtract the storm motion radial component
        # from each ray.  None until the first storm tracking grid runs.
        self._storm_motion: Optional[tuple[float, float]] = None

        # Status
        self._status = RadarStatus(enabled=True, active_sites=[default_site])
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

        # Gate filter threshold (dBZ) – values below this are rendered transparent
        self._gate_dbz: float = 10.0

        # Colormaps (lazy init)
        self._cmaps: dict = {}

        # Callbacks
        self.on_frame_ready: Optional[Callable] = None  # (frames: list[RadarFrame]) -> None
        self.on_volume_ready: Optional[Callable] = None  # (volume: VolumeScanData) -> None
        self.on_status_change: Optional[Callable] = None  # (status: RadarStatus) -> None

        # Latest-frame cache keyed by site — populated whenever any site is
        # processed so switching sites can show a cached frame immediately.
        # Only reflectivity is cached (most useful product for quick preview).
        self._site_cache: dict[str, bytes] = {}

        # nexradaws connection (lazy init)
        self._conn = None

        # Disk-cleanup bookkeeping.  The render paths drop image files into
        # data/radar/<site>/ and into %TEMP%/nexrad_{oneshot,graphic}_<site>/;
        # none of those were ever pruned, so they filled the C: drive during
        # long streaming sessions.  purge_old_data() is now called on startup,
        # on shutdown, and on this throttle inside the poll loop.
        self._data_retention_hours: float = float(
            getattr(settings, "nexrad_data_retention_hours", 2.0)
        )
        self._purge_interval_s: float = 600.0  # sweep at most every 10 min
        self._last_purge_monotonic: float = 0.0

    def _get_conn(self):
        """Get or create nexradaws connection."""
        _ensure_imports()
        if self._conn is None:
            self._conn = nexradaws.NexradAwsInterface()
        return self._conn

    def _get_cmap(self, product: str):
        """Get (or lazily build) the colormap for a product."""
        if product not in self._cmaps:
            self._cmaps[product] = RADAR_PRODUCTS[product]["build_cmap"]()
        return self._cmaps[product]

    @property
    def status(self) -> RadarStatus:
        return self._status

    @property
    def active_site(self) -> str:
        """Primary (first) active site — backward compat."""
        return self._active_sites[0] if self._active_sites else ""

    @property
    def active_sites(self) -> list[str]:
        return list(self._active_sites)

    @property
    def diagnostics(self) -> dict:
        """Per-site pipeline diagnostics for /api/radar/diagnostic."""
        return {s: dict(d) for s, d in self._diagnostics.items()}

    def _diag(self, site: str) -> dict:
        return self._diagnostics.setdefault(site, {})

    def set_storm_motion(self, direction_deg: float, speed_kph: float) -> None:
        """Update the cached mean storm motion used by the SRV product.

        Called by the storm tracking service after each volume's storm cells
        have been computed.  None argument or zero speed clears the cache
        (SRV falls back to ground-relative velocity).
        """
        if direction_deg is None or speed_kph is None or speed_kph <= 0:
            self._storm_motion = None
            return
        self._storm_motion = (float(direction_deg) % 360.0, float(speed_kph))

    def get_storm_motion(self) -> Optional[tuple[float, float]]:
        return self._storm_motion

    def mark_chunks_processed(self, site: str, scan_dt: datetime) -> None:
        """Tell the archive-bucket poller it should skip this scan timestamp.

        Called by the chunks-bucket service after it broadcasts a complete
        volume.  We synthesize the archive filename pattern from the volume
        start time so the archive poller's existing `_last_scan_key` check
        catches it on the next poll.
        """
        if scan_dt is None:
            return
        # Archive key pattern: SSSSYYYYMMDD_HHMMSS_V06
        scan_key = f"{site.upper()}{scan_dt.strftime('%Y%m%d_%H%M%S')}_V06"
        self._last_scan_key[site] = scan_key
        diag = self._diag(site)
        diag["last_chunks_completion_ts"] = scan_dt.isoformat()

    @staticmethod
    def _parse_scan_ts_from_key(scan_key: str) -> Optional[datetime]:
        """Parse the volume-start timestamp encoded in a NEXRAD archive key.

        Standard archive filename:  <SITE><YYYYMMDD>_<HHMMSS>_V0X
        Returns UTC datetime or None if the key doesn't match.
        """
        try:
            base = scan_key.rsplit("/", 1)[-1]
            parts = base.split("_")
            if len(parts) < 2 or len(parts[0]) < 12:
                return None
            ymd = parts[0][-8:]
            hms = parts[1][:6]
            return datetime.strptime(ymd + hms, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            return None

    def get_latest_frames_for_product(self, product: str) -> list[RadarFrame]:
        """Latest frame for a product from every active site."""
        result = []
        for site in self._active_sites:
            frames = self._frames.get(site, {}).get(product, [])
            if frames:
                result.append(frames[-1])
        return result

    def get_latest_frames(self) -> dict[str, Optional[RadarFrame]]:
        """Backward-compat: latest frame per product for the primary site."""
        primary = self.active_site
        result: dict[str, Optional[RadarFrame]] = {}
        for product in RADAR_PRODUCTS:
            frames = self._frames.get(primary, {}).get(product, [])
            result[product] = frames[-1] if frames else None
        return result

    def get_frame_history(
        self, product: str, count: int = 10, site: Optional[str] = None
    ) -> list[RadarFrame]:
        """Last N frames for a product, for the given site (defaults to primary)."""
        target = site or self.active_site
        frames = self._frames.get(target, {}).get(product, [])
        return frames[-count:]

    def get_cached_frame(self, site: str) -> Optional[bytes]:
        """Return the latest cached reflectivity binary for any NEXRAD site.

        Populated automatically whenever a site is processed.  Used by the
        frontend to show an immediate (possibly stale) frame when switching
        sites while the fresh download runs in the background.
        """
        return self._site_cache.get(site.upper())

    def get_frame_by_id(self, site: str, product: str, frame_id: str) -> Optional[RadarFrame]:
        """Look up a cached binary frame by its frame_id (for REST history endpoint)."""
        for frame in self._frames.get(site, {}).get(product, []):
            if frame.frame_id == frame_id:
                return frame
        return None

    @staticmethod
    def _ensure_uniform_azimuth_grid(
        azimuths: np.ndarray,
        sweep_data: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Resample a polar sweep onto a uniform azimuth grid spanning 360°.

        The WebGL fragment shader at frontend/src/components/RadarGLLayer.ts
        computes `ray_spacing = 360 / n_rays` and uses that to index rays from
        screen-space azimuth.  That works only if rays are uniformly spaced
        around the full 360° disk — which is true for a complete super-res
        NEXRAD sweep (720 rays at 0.5°), but FALSE for partial sweeps coming
        out of the chunks pipeline (e.g., a velocity sweep that's only 1 of 4
        chunks complete has ~180 rays covering ~90° of azimuth).  Without
        this regrid, those partial sweeps appear azimuthally stretched —
        visually the data shows up in the wrong geographic locations.

        After regridding:
          - Each input ray is snapped to its nearest target bin.
          - Bins with no input ray are NaN, which the shader treats as no-data.
          - The shader's uniform-spacing assumption is satisfied.

        The target resolution is inferred from the input's median ray spacing:
          - ≤ 0.7° → 720-bin grid (super-res NEXRAD)
          - > 0.7° → 360-bin grid (legacy)
        """
        if sweep_data.size == 0 or azimuths.size == 0:
            return azimuths, sweep_data

        # Infer target resolution from existing ray spacing
        sorted_az = np.sort(azimuths)
        spacings = np.diff(sorted_az)
        spacings = spacings[spacings > 1e-6]
        if spacings.size > 0:
            median_spacing = float(np.median(spacings))
        else:
            median_spacing = 0.5
        target_res = 0.5 if median_spacing < 0.7 else 1.0
        n_target = int(round(360.0 / target_res))

        # If the input already has the right uniform structure (full coverage
        # at the inferred resolution), short-circuit to avoid needless work.
        if (
            azimuths.size == n_target
            and abs(median_spacing - target_res) < 0.05
        ):
            # Check for full 360° span — if any bin missing the data is partial
            full_span = (
                float(np.max(spacings) if spacings.size else 0.0) < 1.5 * target_res
            )
            if full_span:
                return azimuths, sweep_data

        # Build the regular grid and snap each input ray into its bin.
        # We deliberately AVOID np.round here — it uses banker's rounding
        # (round-half-to-even), which on rays offset by exactly half a bin
        # (NEXRAD often transmits at e.g. 0.25, 0.75, 1.25, ... rather than
        # 0, 0.5, 1.0, ...) drops them into [0, 2, 2, 4, 4, ...].  Every
        # other bin stays empty, producing visible radial stripes on the
        # rendered output.  floor(x + 0.5) is round-half-up for positives
        # and gives every ray a unique bin in those offset cases.
        n_gates = sweep_data.shape[1]
        regular = np.full((n_target, n_gates), np.nan, dtype=np.float32)
        bin_idx = np.floor(azimuths / target_res + 0.5).astype(int) % n_target

        # If multiple input rays fall into the same bin (very rare under
        # super-res), the later assignment wins — close enough for display.
        for i, b in enumerate(bin_idx):
            regular[b] = np.asarray(sweep_data[i], dtype=np.float32)

        # Fill any NaN bins from their nearest non-NaN neighbour in azimuth.
        # NEXRAD sweeps regularly contain dropped rays or rays that collide
        # into the same target bin (which leaves the OTHER bin empty).
        # Without this fill, those gaps render as visible radial stripes —
        # the WebGL shader uses uniform-spacing nearest-neighbour ray
        # lookup, so any NaN bin becomes a transparent radial slice.
        # Legitimate wedge gaps (chunks-partial 90° sweeps) never reach
        # this point — the caller's source-coverage guard already returned
        # None for those, so anything we see here is a high-coverage sweep
        # where filling is the right call.
        nan_rows = np.all(np.isnan(regular), axis=1)
        n_nan = int(nan_rows.sum())
        if 0 < n_nan < n_target:
            valid_idx = np.where(~nan_rows)[0]
            nan_idx   = np.where(nan_rows)[0]
            # Circular nearest-neighbour lookup per NaN bin.  Vectorised
            # against a small (≤720) array — cheap.
            d  = (nan_idx[:, None] - valid_idx[None, :]) % n_target
            d  = np.minimum(d, n_target - d)
            nearest = valid_idx[np.argmin(d, axis=1)]
            regular[nan_idx] = regular[nearest]

        regular_az = (np.arange(n_target, dtype=np.float32) * target_res)
        return regular_az, regular

    def _pack_binary_frame(
        self,
        azimuths: np.ndarray,
        ranges_m: np.ndarray,
        sweep_data: np.ndarray,
        product: str,
        site: str,
        timestamp_iso: str,
        elevation: float,
        bounds: dict,
        vmin: float,
        vmax: float,
        max_range_m: Optional[float] = None,
    ) -> bytes:
        """Pack raw polar data into the RDRF binary wire format.

        Format (all multi-byte integers little-endian):
          [0:4]   magic 'RDRF'
          [4:8]   uint32 metadata_len
          [8:12]  uint32 n_rays
          [12:16] uint32 n_gates
          [16:16+metadata_len]  UTF-8 JSON metadata
          [...]   float32[] azimuths (degrees)
          [...]   float32[] ranges_m (metres)
          [...]   uint8[]   gate_values, row-major; 0x00 = no-data sentinel
        """
        import struct
        import json as _json

        MAGIC = b"\x52\x44\x52\x46"  # 'RDRF'

        # Sort rays by azimuth so the shader's uniform-spacing index estimate is
        # accurate regardless of where the NEXRAD sweep started (~350° is common).
        sort_idx = np.argsort(azimuths)
        azimuths_s  = azimuths[sort_idx].astype(float)
        sweep_data_s = sweep_data[sort_idx, :]

        # Normalize float data to uint8; 0x00 is the no-data sentinel
        span = vmax - vmin if vmax != vmin else 1.0
        norm = np.clip((sweep_data_s - vmin) / span, 0.0, 1.0)
        gate_u8 = (norm * 255.0).astype(np.uint8)
        gate_u8[np.isnan(sweep_data_s)] = 0
        # Bump valid values that round to 0 up to 1 so they're not mistaken for no-data
        nonzero = ~np.isnan(sweep_data_s) & (sweep_data_s >= vmin)
        gate_u8[nonzero & (gate_u8 == 0)] = 1

        n_rays, n_gates = gate_u8.shape
        # Use the caller-supplied max_range_m (which matches the geographic bounds)
        # rather than ranges_m[-1], which can be larger and would cause a scale mismatch.
        if max_range_m is None:
            max_range_m = float(self._max_range_km * 1000)

        meta = {
            "product": product,
            "site": site,
            "timestamp": timestamp_iso,
            "elevation": elevation,
            "vmin": float(vmin),
            "vmax": float(vmax),
            "max_range_m": max_range_m,
            "bounds": bounds,
        }
        meta_bytes = _json.dumps(meta, separators=(",", ":")).encode("utf-8")
        # Pad metadata to 4-byte boundary so float32 arrays start at an aligned offset.
        # Float32Array requires byte offset % 4 == 0; header is 16 bytes so we pad
        # meta_bytes until (16 + len(meta_bytes)) % 4 == 0.
        pad = (4 - len(meta_bytes) % 4) % 4
        if pad:
            meta_bytes = meta_bytes + b" " * pad  # trailing spaces are valid JSON

        header = MAGIC + struct.pack("<III", len(meta_bytes), n_rays, n_gates)
        az_bytes   = azimuths_s.astype(np.float32).tobytes()   # sorted azimuths
        rng_bytes  = ranges_m.astype(np.float32).tobytes()
        gate_bytes = gate_u8.tobytes()  # C row-major, rows sorted by azimuth

        return header + meta_bytes + az_bytes + rng_bytes + gate_bytes

    def oneshot_frame(self, site_id: str) -> "Optional[RadarFrame]":
        """
        Download and render the latest reflectivity frame for any NEXRAD site
        without modifying service state.  Safe to call from a thread executor.

        Returns a RadarFrame, or None if the site is unavailable or download fails.
        """
        site_id = site_id.upper()
        try:
            _ensure_imports()
            from backend.services.nexrad_sites import NEXRAD_SITES
            if site_id not in NEXRAD_SITES:
                logger.warning(f"oneshot_frame: {site_id} not in NEXRAD_SITES")
                return None

            now = datetime.now(timezone.utc)
            conn = self._get_conn()

            scans = conn.get_avail_scans(now.year, now.month, now.day, site_id)
            if not scans:
                logger.warning(f"oneshot_frame: no scans for {site_id}")
                return None

            # Filter out non-archive products
            def _key(s):
                return getattr(s, "filename", None) or getattr(s, "key", None) or str(s)

            volume_scans = [
                s for s in scans
                if not any(x in _key(s) for x in ("_MDM", "_THR", "_NFL"))
            ]
            if not volume_scans:
                return None

            latest = volume_scans[-1]
            logger.info(f"oneshot_frame: downloading {site_id} {_key(latest)}")

            results = conn.download(latest, tempfile.gettempdir())
            if not results.success:
                logger.warning(f"oneshot_frame: download failed for {site_id}")
                return None

            local_file = results.success[0].filepath

            radar = pyart.io.read_nexrad_archive(str(local_file), linear_interp=False)

            # Timestamp
            try:
                scan_iso = datetime.fromisoformat(
                    radar.time["units"].split("since ")[-1].replace("Z", "+00:00")
                ).isoformat()
            except Exception:
                scan_iso = now.isoformat()

            scan_ts = now.strftime("%Y%m%d_%H%M%S")

            # Bounds (same cardinal-direction approach as main service)
            site_info = NEXRAD_SITES.get(site_id, {})
            site_lat = site_info.get("lat", float(radar.latitude["data"][0]))
            site_lon = site_info.get("lon", float(radar.longitude["data"][0]))
            range_m = self._max_range_km * 1000

            try:
                import pyproj
                geod = pyproj.Geod(ellps="WGS84")
                _, _, n_dist = geod.inv(site_lon, site_lat, site_lon, site_lat + 1)
                lat_deg_per_m = 1.0 / n_dist
                lon_deg_per_m = 1.0 / (n_dist * math.cos(math.radians(site_lat)))
                bounds = {
                    "north": site_lat + range_m * lat_deg_per_m,
                    "south": site_lat - range_m * lat_deg_per_m,
                    "east":  site_lon + range_m * lon_deg_per_m,
                    "west":  site_lon - range_m * lon_deg_per_m,
                }
            except Exception:
                deg = range_m / 111_000
                bounds = {
                    "north": site_lat + deg, "south": site_lat - deg,
                    "east":  site_lon + deg, "west":  site_lon - deg,
                }

            # Render reflectivity using the service's existing pipeline
            site_dir = Path(tempfile.gettempdir()) / f"nexrad_oneshot_{site_id}"
            site_dir.mkdir(exist_ok=True)

            ref_config = RADAR_PRODUCTS.get("reflectivity", {})
            if not ref_config:
                return None

            frame = self._render_polar_to_file(
                radar, "reflectivity", ref_config,
                site_id, scan_ts, scan_iso, bounds, site_dir,
            )

            if frame is not None:
                logger.info(f"oneshot_frame: rendered {site_id} OK")

            # Cleanup
            try:
                os.remove(str(local_file))
            except Exception:
                pass

            return frame

        except Exception as e:
            logger.warning(f"oneshot_frame failed for {site_id}: {e}")
            return None

    async def set_gate_dbz(self, gate: float):
        """Update the reflectivity gate threshold and re-render the current scan."""
        self._gate_dbz = float(gate)
        # Invalidate reflectivity colormap cache so it rebuilds cleanly
        self._cmaps.pop("reflectivity", None)
        await self._rerender_current()

    async def _rerender_current(self):
        """Re-render cached volume scans for all active sites (e.g. after gate change)."""
        loop = asyncio.get_event_loop()
        for site, volume in list(self._last_volume.items()):
            if volume is None:
                continue
            result = await loop.run_in_executor(None, self._rerender_sync, volume)
            if result and self.on_frame_ready:
                frames, _ = result
                await self.on_frame_ready(frames)

    def _rerender_sync(self, volume):
        """Synchronous re-render from a cached VolumeScanData."""
        _ensure_imports()
        scan_ts = volume.timestamp.replace(":", "").replace("-", "").replace("T", "_").split("+")[0].split(".")[0]
        site = volume.site
        site_dir = self._data_dir / site
        site_dir.mkdir(parents=True, exist_ok=True)

        # Recompute Voronoi other-site positions for this re-render
        other_sites_aeqd: list[tuple[float, float]] = []
        if len(self._active_sites) > 1:
            try:
                from backend.services.nexrad_sites import NEXRAD_SITES as _NS
                from pyproj import Proj as _Proj
                _si = _NS.get(site, {})
                _s_lat = _si.get("lat")
                _s_lon = _si.get("lon")
                if _s_lat and _s_lon:
                    _p = _Proj(
                        proj="aeqd", lat_0=_s_lat, lon_0=_s_lon,
                        datum="WGS84", units="m",
                    )
                    for _other in self._active_sites:
                        if _other == site:
                            continue
                        _osi = _NS.get(_other, {})
                        _o_lat = _osi.get("lat")
                        _o_lon = _osi.get("lon")
                        if _o_lat and _o_lon:
                            _ox, _oy = _p(_o_lon, _o_lat)
                            other_sites_aeqd.append((float(_ox), float(_oy)))
            except Exception:
                pass

        frames = []
        for product_name, product_config in RADAR_PRODUCTS.items():
            frame = self._render_from_polar(
                volume.radar_object, product_name, product_config,
                site, scan_ts, volume.timestamp, volume.bounds, site_dir,
                other_sites_aeqd=other_sites_aeqd,
            )
            if frame:
                frames.append(frame)

        site_frames = self._frames.setdefault(site, {p: [] for p in RADAR_PRODUCTS})
        for frame in frames:
            site_frames[frame.product].append(frame)
            if len(site_frames[frame.product]) > self._history_count:
                site_frames[frame.product].pop(0)

        return frames, volume

    async def add_site(self, site_id: str):
        """Add a radar site to the active set (max 3)."""
        site_id = site_id.upper()
        from backend.services.nexrad_sites import NEXRAD_SITES
        if site_id not in NEXRAD_SITES:
            raise ValueError(f"Unknown NEXRAD site: {site_id}")
        if site_id in self._active_sites:
            return
        if len(self._active_sites) >= MAX_ACTIVE_SITES:
            raise ValueError(f"Maximum {MAX_ACTIVE_SITES} radar sites can be active at once")

        logger.info(f"Adding radar site: {site_id}")
        self._active_sites.append(site_id)
        self._frames[site_id] = {p: [] for p in RADAR_PRODUCTS}
        self._last_scan_key[site_id] = None

        self._status.active_sites = list(self._active_sites)
        if self.on_status_change:
            await self.on_status_change(self._status)

        # Fetch this site immediately (don't wait for next poll cycle)
        asyncio.create_task(self._fetch_and_process_site(site_id))

    async def remove_site(self, site_id: str):
        """Remove a radar site from the active set."""
        site_id = site_id.upper()
        if site_id not in self._active_sites:
            return
        if len(self._active_sites) <= 1:
            raise ValueError("Cannot remove the only active radar site")

        logger.info(f"Removing radar site: {site_id}")
        self._active_sites.remove(site_id)

        self._frames.pop(site_id, None)
        self._last_scan_key.pop(site_id, None)
        self._last_volume.pop(site_id, None)
        self._site_processing.pop(site_id, None)
        self._diagnostics.pop(site_id, None)
        self._last_broadcast_scan_dt.pop(site_id, None)
        self._last_grid_scan_dt.pop(site_id, None)

        self._status.active_sites = list(self._active_sites)
        if self.on_status_change:
            await self.on_status_change(self._status)

    async def set_active_site(self, site_id: str):
        """Replace all active sites with just this one (backward compat / WS handler)."""
        site_id = site_id.upper()
        from backend.services.nexrad_sites import NEXRAD_SITES
        if site_id not in NEXRAD_SITES:
            raise ValueError(f"Unknown NEXRAD site: {site_id}")

        if self._active_sites == [site_id]:
            return

        logger.info(f"Setting radar site to {site_id} (replacing {self._active_sites})")

        # Remove all sites except the new one
        for old in list(self._active_sites):
            if old != site_id:
                self._frames.pop(old, None)
                self._last_scan_key.pop(old, None)
                self._last_volume.pop(old, None)
                self._site_processing.pop(old, None)
                self._diagnostics.pop(old, None)
                self._last_broadcast_scan_dt.pop(old, None)
                self._last_grid_scan_dt.pop(old, None)

        self._active_sites = [site_id]
        if site_id not in self._frames:
            self._frames[site_id] = {p: [] for p in RADAR_PRODUCTS}
        self._last_scan_key[site_id] = None

        self._status.active_sites = [site_id]
        self._status.last_update = None
        if self.on_status_change:
            await self.on_status_change(self._status)

        asyncio.create_task(self._fetch_and_process())

    async def start(self):
        """Start the radar polling loop."""
        _ensure_imports()
        self._running = True
        self._status.enabled = True
        logger.info(f"NEXRAD service starting for sites {self._active_sites}")

        # Sweep any stale image files left over from a previous run before we
        # start generating new ones (mirrors the broadcast-graphic prune in
        # main.py's startup path).
        try:
            self.purge_old_data()
        except Exception as e:
            logger.warning(f"Startup radar cleanup failed: {e}")
        self._last_purge_monotonic = time.monotonic()

        # Initial fetch — backgrounded so it doesn't block FastAPI startup
        # (a full volume download+decode is ~15-25 s). The poll loop sleeps one
        # interval before its first fetch and a per-site flag prevents overlap,
        # so the first frame still arrives shortly after the server is ready.
        asyncio.create_task(self._fetch_and_process())

        # Start polling loop and watchdog
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self):
        """Stop the radar service."""
        self._running = False
        for task in (self._poll_task, self._watchdog_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Final sweep so a clean shutdown doesn't leave the last session's
        # graphics behind on disk.
        try:
            self.purge_old_data()
        except Exception as e:
            logger.warning(f"Shutdown radar cleanup failed: {e}")
        self._status.enabled = False
        logger.info("NEXRAD service stopped")

    async def _watchdog_loop(self):
        """Emit a one-line latency summary per active site every 60 s.

        Helps diagnose "scan is N minutes behind" complaints by exposing
        where time is going: S3 archive lag vs. each pipeline stage.
        Grep your logs for `[radar-watchdog]` to inspect.
        """
        while self._running:
            try:
                await asyncio.sleep(60)
                now = datetime.now(timezone.utc)
                for site in list(self._active_sites):
                    diag = self._diagnostics.get(site)
                    if not diag:
                        logger.info(f"[radar-watchdog] {site}: no activity yet")
                        continue

                    # "what we're showing" age
                    showing_ts = diag.get("showing_scan_ts")
                    showing_age = None
                    if showing_ts:
                        try:
                            showing_age = (
                                now - datetime.fromisoformat(showing_ts)
                            ).total_seconds()
                        except (ValueError, TypeError):
                            pass

                    # "what S3 has" gap
                    s3_ts  = diag.get("latest_available_ts")
                    s3_age = diag.get("latest_available_age_s")

                    # Stage timings (last successful processing)
                    parts = []
                    parts.append(
                        f"showing={showing_ts or 'never'}"
                        + (f" (age {showing_age:.0f}s)" if showing_age is not None else "")
                    )
                    if s3_ts and showing_ts and s3_ts != showing_ts:
                        parts.append(f"s3_latest={s3_ts} (age {s3_age}s) — NEWER ON S3")
                    parts.append(
                        f"phase1={diag.get('last_phase1_duration_s', '?')}s "
                        f"[list={diag.get('last_list_duration_s', '?')}s "
                        f"dl={diag.get('last_download_duration_s', '?')}s "
                        f"parse={diag.get('last_parse_duration_s', '?')}s "
                        f"dealias={diag.get('last_dealias_duration_s', '?')}s "
                        f"render={diag.get('last_render_duration_s', '?')}s]"
                    )
                    if diag.get("last_grid_duration_s") is not None:
                        parts.append(f"grid={diag['last_grid_duration_s']}s")
                    if diag.get("polls_since_new_scan", 0) > 0:
                        parts.append(
                            f"empty_polls={diag['polls_since_new_scan']}"
                        )
                    if diag.get("last_error"):
                        parts.append(f"last_error={diag['last_error']}")

                    logger.info(f"[radar-watchdog] {site}: " + " | ".join(parts))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Radar watchdog error: {e}", exc_info=True)

    async def _poll_loop(self):
        """Periodically check for new volume scans.

        NEXRAD VCPs complete every 4–6 minutes, and the new archive file lands
        on S3 ~30 s after VCP completion.  We poll on `nexrad_poll_interval`
        (default 10 s) — most polls are cheap (one S3 LIST + key compare) and
        return quickly when there's no new scan.  Per-site processing flags
        prevent any site from getting re-fetched while still in Phase 1, so a
        tight poll interval doesn't pile up work.
        """
        interval = max(5, int(self._poll_interval))
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._fetch_and_process()
                # Throttled disk cleanup — runs at most every _purge_interval_s
                # regardless of how tight the poll interval is.
                now_mono = time.monotonic()
                if now_mono - self._last_purge_monotonic >= self._purge_interval_s:
                    self._last_purge_monotonic = now_mono
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.purge_old_data
                        )
                    except Exception as e:
                        logger.warning(f"Periodic radar cleanup failed: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Radar poll error: {e}", exc_info=True)
                self._status.error = str(e)
                await asyncio.sleep(interval)  # Back off on error

    async def _fetch_and_process(self):
        """Fetch the latest volume scan for every active site, in parallel.

        Per-site flags prevent one site from re-entering its own Phase 1
        while a previous Phase 1 is still running; sites that aren't busy
        proceed immediately even if another site is mid-download.
        """
        self._status.error = None

        # Filter out sites that are already mid-Phase 1.  Anything else gets
        # dispatched concurrently.
        sites_to_fetch = [
            s for s in list(self._active_sites)
            if not self._site_processing.get(s, False)
        ]
        if not sites_to_fetch:
            logger.debug("All active sites are processing; nothing to dispatch")
            return

        # Mark sites busy and broadcast status BEFORE awaiting any I/O so
        # rapid back-to-back polls correctly skip these sites.
        for s in sites_to_fetch:
            self._site_processing[s] = True
        await self._broadcast_processing_status()

        async def _wrap(site: str):
            try:
                await self._fetch_and_process_site(site)
            except Exception as e:
                logger.error(f"Radar fetch/process error for {site}: {e}", exc_info=True)
                self._status.error = str(e)
            finally:
                self._site_processing[site] = False

        try:
            await asyncio.gather(*(_wrap(s) for s in sites_to_fetch))
        finally:
            await self._broadcast_processing_status()

    async def _broadcast_processing_status(self):
        """Recompute the global processing flag and broadcast on change."""
        any_busy = any(self._site_processing.values())
        if self._status.processing != any_busy:
            self._status.processing = any_busy
            if self.on_status_change:
                await self.on_status_change(self._status)

    async def _fetch_and_process_site(self, site: str):
        """Fetch and process the latest scan for one site.

        Splits into two phases so the UI update is never blocked by Barnes2 gridding:
          Phase 1 (executor): download + parse + pack binary  →  ~15-25 s
          Phase 2 (background task): Barnes2 grid for storm tracking  →  ~30-60 s

        The processing flag is cleared after Phase 1 so poll cycles can continue
        while Phase 2 is still running in the background.
        """
        diag = self._diag(site)
        diag["last_poll_started"] = datetime.now(timezone.utc).isoformat()
        poll_start = time.perf_counter()

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._download_and_process_sync, site
            )
        except Exception as e:
            diag["last_error"] = f"phase1: {e}"
            logger.error(f"Radar fetch/process error for {site}: {e}", exc_info=True)
            return

        elapsed = round(time.perf_counter() - poll_start, 2)

        if not result:
            # No new scan available — track empty polls and record this
            # cheap-path duration separately so it can't be mistaken for
            # actual Phase 1 cost on the diagnostic.
            diag["last_empty_poll_duration_s"] = elapsed
            diag["polls_since_new_scan"] = diag.get("polls_since_new_scan", 0) + 1
            return

        # Only record full Phase 1 duration when an actual scan was processed
        diag["last_phase1_duration_s"] = elapsed
        diag["polls_since_new_scan"] = 0

        await self.finalize_phase1_async(site, result, source="archive")

    async def finalize_phase1_async(
        self,
        site: str,
        result_tuple,
        source: str = "archive",
        skip_grid: bool = False,
    ) -> None:
        """Cache frames, broadcast to clients, optionally spawn Phase 2 gridding.

        Shared between archive-bucket and chunks-bucket ingestion paths.
        `source` is logged for diagnosability ("archive" | "chunks-partial"
        | "chunks-complete").

        When `skip_grid` is True, the storm-tracking grid is NOT spawned —
        used for chunks-partial which only carries the lowest few tilts.
        Storm tracking is delegated to the chunks-complete (or archive)
        path for the same volume, where the full vertical column is present.

        Anti-stale guards:
          - Broadcast is skipped if `scan_dt` is strictly older than the
            last broadcast for this site (handles a late archive download
            arriving after chunks already broadcast a newer volume).
          - Grid is skipped if `scan_dt` has already been gridded for this
            site (prevents chunks-complete + archive double-gridding).
        """
        diag = self._diag(site)
        loop = asyncio.get_event_loop()

        frames, radar_obj, scan_iso, bounds, elevation_angles, site_lat, site_lon = result_tuple

        # Parse scan timestamp once for the guards
        try:
            scan_dt = datetime.fromisoformat(scan_iso)
        except (ValueError, TypeError):
            scan_dt = None

        # ── Anti-stale guard ─────────────────────────────────────────────────
        # A scan older than what we've already shown must not overwrite the
        # display.  BUT a "stale" timestamp must NOT suppress storm-tracking
        # gridding: the chunks-complete *full volume* is timestamped at
        # volume-start, which is EARLIER than the SAILS revisit sub-scans that
        # were broadcast moments before it — yet that full volume is the
        # authoritative vertical column the tracker needs.  So when this call
        # owns gridding (skip_grid=False) we suppress only the re-broadcast and
        # still fall through to Phase 2.  Partial/skip_grid calls are dropped
        # outright when stale, as before.
        last_broadcast_dt = self._last_broadcast_scan_dt.get(site)
        stale = (
            scan_dt is not None
            and last_broadcast_dt is not None
            and scan_dt < last_broadcast_dt
        )
        if stale and skip_grid:
            logger.info(
                f"Stale scan dropped [{source}]: {site} @ {scan_iso} is older "
                f"than already-broadcast {last_broadcast_dt.isoformat()}"
            )
            return

        if not stale:
            # ── Phase 1 complete: update caches and broadcast immediately ─────
            site_frames = self._frames.setdefault(site, {p: [] for p in RADAR_PRODUCTS})
            for frame in frames:
                product_frames = site_frames[frame.product]
                product_frames.append(frame)
                if len(product_frames) > self._history_count:
                    product_frames.pop(0)
                if frame.product == 'reflectivity' and frame.binary_data:
                    self._site_cache[site] = frame.binary_data

            self._status.last_update = scan_iso

            if self.on_frame_ready:
                await self.on_frame_ready(frames)

            if scan_dt is not None:
                self._last_broadcast_scan_dt[site] = scan_dt

            # Diagnostic snapshot at broadcast time
            try:
                broadcast_dt = datetime.now(timezone.utc)
                age_at_broadcast_s = (
                    (broadcast_dt - scan_dt).total_seconds() if scan_dt else None
                )
                diag["showing_scan_ts"]         = scan_iso
                diag["showing_source"]          = source
                diag["last_broadcast_at"]       = broadcast_dt.isoformat()
                diag["last_age_at_broadcast_s"] = (
                    round(age_at_broadcast_s, 1) if age_at_broadcast_s is not None else None
                )
                diag["last_tilt_count"]         = len(elevation_angles)
            except (ValueError, TypeError):
                age_at_broadcast_s = None

            if age_at_broadcast_s is not None:
                logger.info(
                    f"Radar broadcast [{source}]: {site} @ {scan_iso} "
                    f"({len(frames)} products, "
                    f"age@broadcast={age_at_broadcast_s:.0f}s, "
                    f"tilts={len(elevation_angles)})"
                )
            else:
                logger.info(
                    f"Radar broadcast [{source}]: {site} @ {scan_iso} ({len(frames)} products)"
                )
        else:
            logger.info(
                f"Stale broadcast suppressed but still gridding [{source}]: "
                f"{site} @ {scan_iso} (older than shown "
                f"{last_broadcast_dt.isoformat()}) — full volume for storm tracking"
            )

        # ── Phase 2: grid for storm tracking ─────────────────────────────────
        # Skipped explicitly for chunks-partial (lowest tilts only — not enough
        # vertical structure for VIL, MESH, BWER, mid-level rotation).  Also
        # guarded against same-scan double-gridding via _last_grid_scan_dt.
        if skip_grid:
            logger.debug(
                f"Skipping grid for {site} @ {scan_iso} [{source}] — "
                "partial volume, storm tracking deferred to chunks-complete"
            )
            return

        if scan_dt is not None:
            last_grid_dt = self._last_grid_scan_dt.get(site)
            if last_grid_dt is not None and scan_dt <= last_grid_dt:
                logger.debug(
                    f"Grid skipped for {site} @ {scan_iso} [{source}] — "
                    f"already gridded at {last_grid_dt.isoformat()}"
                )
                diag["grid_skipped_reason"] = (
                    f"already gridded at {last_grid_dt.isoformat()}"
                )
                return
            # Reserve immediately so a parallel call can't both pass the guard
            self._last_grid_scan_dt[site] = scan_dt

        if not self.on_volume_ready:
            diag["grid_skipped_reason"] = "on_volume_ready not wired"
        if self.on_volume_ready:
            on_vol = self.on_volume_ready  # capture ref before async gap

            async def _background_grid():
                t_grid = time.perf_counter()
                diag["grid_started_at"] = datetime.now(timezone.utc).isoformat()
                diag["grid_skipped_reason"] = None
                try:
                    grid = await loop.run_in_executor(
                        None, self._create_grid, radar_obj, site_lat, site_lon
                    )
                    elev = []
                    try:
                        sweep_start = radar_obj.sweep_start_ray_index["data"]
                        for i in range(radar_obj.nsweeps):
                            elev.append(float(radar_obj.elevation["data"][sweep_start[i]]))
                    except Exception:
                        elev = elevation_angles

                    volume = VolumeScanData(
                        site=site, timestamp=scan_iso,
                        radar_object=radar_obj, grid=grid,
                        bounds=bounds, elevation_angles=elev,
                    )
                    self._last_volume[site] = volume
                    await on_vol(volume)

                    grid_duration = time.perf_counter() - t_grid
                    diag["last_grid_duration_s"] = round(grid_duration, 2)
                    try:
                        age_at_grid = (
                            datetime.now(timezone.utc) - datetime.fromisoformat(scan_iso)
                        ).total_seconds()
                        diag["last_age_at_grid_s"] = round(age_at_grid, 1)
                    except Exception:
                        age_at_grid = None
                    logger.info(
                        f"Storm tracking grid ready [{source}]: {site} "
                        f"(grid={grid_duration:.1f}s, "
                        f"age@grid={age_at_grid:.0f}s)"
                        if age_at_grid is not None
                        else f"Storm tracking grid ready [{source}]: {site} "
                             f"(grid={grid_duration:.1f}s)"
                    )
                except Exception as e:
                    diag["grid_error"] = f"{type(e).__name__}: {e}"
                    logger.error(f"Background grid failed for {site}: {e}", exc_info=True)

            asyncio.create_task(_background_grid())

    def _download_and_process_sync(
        self, site: str
    ) -> Optional[tuple[list[RadarFrame], object, str, dict, list, float, float]]:
        """
        Synchronous download + processing for one site (runs in executor thread).
        Returns (frames, volume_data) or None if no new data.
        """
        _ensure_imports()
        now = datetime.now(timezone.utc)
        diag = self._diag(site)

        # ── Stage 1: LIST objects on S3 ────────────────────────────────────
        t_list = time.perf_counter()
        conn = self._get_conn()
        try:
            scans = conn.get_avail_scans(
                now.year, now.month, now.day, site
            )
        except Exception as e:
            diag["last_error"] = f"list: {e}"
            logger.error(f"Failed to list scans for {site}: {e}")
            return None
        diag["last_list_duration_s"] = round(time.perf_counter() - t_list, 2)
        diag["last_list_count"]      = len(scans) if scans else 0

        if not scans:
            logger.debug(f"No scans found for {site}")
            return None

        # Filter to standard volume scans only — exclude MDM, THR, and other
        # non-archive product files that Py-ART can't parse.
        # nexradaws scan objects have a .filename attribute; fall back to str().
        def _scan_key(s):
            return getattr(s, "filename", None) or getattr(s, "key", None) or str(s)

        volume_scans = [
            s for s in scans
            if "_MDM" not in _scan_key(s)
            and "_THR" not in _scan_key(s)
            and "_NFL" not in _scan_key(s)
        ]
        logger.debug(f"Scans available: {len(scans)}, after filter: {len(volume_scans)}")

        if not volume_scans:
            logger.warning(f"No standard volume scans for {site}. Raw list: {[_scan_key(s) for s in scans[-5:]]}")
            return None

        # Get the most recent scan
        latest_scan = volume_scans[-1]
        scan_key = _scan_key(latest_scan)
        diag["latest_available_key"] = scan_key

        # Parse the volume-start timestamp from the key so we can show the
        # gap between "what's on S3" and "what we've already processed."
        avail_dt = self._parse_scan_ts_from_key(scan_key)
        if avail_dt is not None:
            diag["latest_available_ts"]    = avail_dt.isoformat()
            diag["latest_available_age_s"] = round((now - avail_dt).total_seconds(), 1)

        # Skip if we already processed this scan
        if scan_key == self._last_scan_key.get(site):
            logger.debug(
                f"No new scan for {site}: still on {scan_key} "
                f"(S3 age {diag.get('latest_available_age_s', '?')}s, "
                f"list took {diag['last_list_duration_s']:.1f}s)"
            )
            return None

        diag["downloading_scan_key"] = scan_key
        logger.info(
            f"Downloading scan: {scan_key} for {site} "
            f"(S3 age {diag.get('latest_available_age_s', '?')}s; "
            f"list took {diag['last_list_duration_s']:.1f}s)"
        )

        # ── Stage 2: DOWNLOAD ──────────────────────────────────────────────
        t_dl = time.perf_counter()
        try:
            results = conn.download(latest_scan, tempfile.gettempdir())
        except Exception as e:
            diag["last_error"] = f"download: {e}"
            logger.error(f"Failed to download scan {scan_key}: {e}")
            return None
        diag["last_download_duration_s"] = round(time.perf_counter() - t_dl, 2)

        if not results.success:
            logger.error(f"Download failed for {scan_key}: {results.failed}")
            return None

        result = results.success[0]
        local_file = result.filepath

        try:
            result_tuple = self._process_local_file_sync(
                local_file, site, scan_key=scan_key, cleanup=False,
            )
            if result_tuple is not None:
                # Mark as processed only when the parse+render succeeded.
                self._last_scan_key[site] = scan_key
            return result_tuple
        finally:
            try:
                if os.path.exists(local_file):
                    os.remove(local_file)
            except OSError:
                pass

    def _process_local_file_sync(
        self,
        local_file: str,
        site: str,
        scan_key: Optional[str] = None,
        cleanup: bool = False,
    ) -> Optional[tuple]:
        """Parse + render a Level II file on disk.

        Shared between the archive-bucket pipeline and the new chunks-bucket
        pipeline.  Returns the same tuple shape as `_download_and_process_sync`:
          (frames, radar, scan_iso, bounds, elevation_angles, site_lat, site_lon)
        or None on parse failure.
        """
        radar = self._parse_local_file_sync(local_file, site)
        try:
            if radar is None:
                return None
            return self._process_radar_object(radar, site)
        finally:
            if cleanup:
                try:
                    if os.path.exists(local_file):
                        os.remove(local_file)
                except OSError:
                    pass

    def _parse_local_file_sync(
        self, local_file: str, site: str
    ) -> Optional[object]:
        """Parse a Level II file and return the Py-ART radar object.

        Separate from the render pipeline so the chunks-bucket service can
        parse once and then render multiple sub-scans (e.g. SAILS revisits)
        without re-doing the parse.
        """
        _ensure_imports()
        diag = self._diag(site)
        t_parse = time.perf_counter()
        try:
            radar = pyart.io.read_nexrad_archive(
                str(local_file),
                linear_interp=False,
            )
        except Exception as e:
            diag["last_error"] = f"parse: {e}"
            logger.warning(f"Py-ART parse failed for {site} ({local_file}): {e}")
            return None
        diag["last_parse_duration_s"] = round(time.perf_counter() - t_parse, 2)
        return radar

    def _process_radar_object(
        self,
        radar: object,
        site: str,
        scan_time_override: Optional[datetime] = None,
        skip_dealias: bool = False,
    ) -> Optional[tuple]:
        """Render a parsed Py-ART radar into broadcast-ready frames.

        - `scan_time_override`: when provided (e.g. for a SAILS sub-scan), this
          datetime is used as the scan timestamp instead of the radar's volume
          start time.  Lets us emit each SAILS revisit as its own timeline event.
        - `skip_dealias`: when True, assume `velocity_dealiased` is already
          present in `radar.fields` (caller did the dealiasing on the parent
          radar before extracting this sub-radar).  Saves a redundant ~3-5s
          per sub-scan.
        """
        _ensure_imports()
        diag = self._diag(site)

        # Extract timestamp from radar metadata, unless overridden
        if scan_time_override is not None:
            scan_time = scan_time_override
        else:
            try:
                time_start = radar.time["units"]
                time_str = time_start.split("since ")[-1].replace("Z", "+00:00")
                scan_time = datetime.fromisoformat(time_str)
            except Exception:
                scan_time = datetime.now(timezone.utc)

        scan_ts  = scan_time.strftime("%Y%m%d_%H%M%S")
        scan_iso = scan_time.isoformat()

        # Get site location for bounds
        from backend.services.nexrad_sites import NEXRAD_SITES
        site_info = NEXRAD_SITES.get(site, {})
        site_lat = site_info.get("lat", radar.latitude["data"][0])
        site_lon = site_info.get("lon", radar.longitude["data"][0])

        # Compute image bounds for Leaflet ImageOverlay (AEQD cardinal extremes).
        range_m = self._max_range_km * 1000
        try:
            from pyproj import Proj
            p = Proj(proj="aeqd", lat_0=site_lat, lon_0=site_lon, datum="WGS84", units="m")
            _,  lat_n = p(0,        range_m,  inverse=True)
            _,  lat_s = p(0,       -range_m,  inverse=True)
            lon_e, _  = p(range_m,  0,        inverse=True)
            lon_w, _  = p(-range_m, 0,        inverse=True)
            bounds = {
                "south": float(lat_s),
                "north": float(lat_n),
                "west":  float(lon_w),
                "east":  float(lon_e),
            }
        except Exception:
            lat_per_km = 1.0 / 111.0
            lon_per_km = 1.0 / (111.0 * np.cos(np.radians(site_lat)))
            rk = self._max_range_km
            bounds = {
                "south": float(site_lat - rk * lat_per_km),
                "north": float(site_lat + rk * lat_per_km),
                "west": float(site_lon - rk * lon_per_km),
                "east": float(site_lon + rk * lon_per_km),
            }

        # Compute AEQD positions of other active sites for Voronoi masking.
        other_sites_aeqd: list[tuple[float, float]] = []
        if len(self._active_sites) > 1:
            from backend.services.nexrad_sites import NEXRAD_SITES as _NS
            for _other in self._active_sites:
                if _other == site:
                    continue
                _osi = _NS.get(_other, {})
                _o_lat = _osi.get("lat")
                _o_lon = _osi.get("lon")
                if _o_lat is None or _o_lon is None:
                    continue
                try:
                    from pyproj import Proj as _Proj
                    _p = _Proj(
                        proj="aeqd", lat_0=site_lat, lon_0=site_lon,
                        datum="WGS84", units="m",
                    )
                    _ox, _oy = _p(_o_lon, _o_lat)
                    other_sites_aeqd.append((float(_ox), float(_oy)))
                except Exception:
                    _dlat_km = (_o_lat - site_lat) * 111.0
                    _dlon_km = (
                        (_o_lon - site_lon) * 111.0
                        * np.cos(np.radians(site_lat))
                    )
                    other_sites_aeqd.append(
                        (_dlon_km * 1000.0, _dlat_km * 1000.0)
                    )

        # ── DEALIAS velocity ──────────────────────────────────────────────
        if not skip_dealias:
            t_dealias = time.perf_counter()
            if "velocity" in radar.fields:
                try:
                    gatefilter = pyart.filters.GateFilter(radar)
                    gatefilter.exclude_below("reflectivity", -20)
                    corrected_vel = pyart.correct.dealias_region_based(
                        radar,
                        gatefilter=gatefilter,
                        skip_between_rays=True,
                        skip_along_ray=True,
                    )
                    radar.add_field("velocity_dealiased", corrected_vel, replace_existing=True)
                except Exception as e:
                    logger.warning(f"Velocity dealiasing failed: {e}")
            diag["last_dealias_duration_s"] = round(time.perf_counter() - t_dealias, 2)

        # ── RENDER each product to binary ─────────────────────────────────
        t_render = time.perf_counter()
        frames: list[RadarFrame] = []
        site_dir = self._data_dir / site
        site_dir.mkdir(parents=True, exist_ok=True)

        for product_name, product_config in RADAR_PRODUCTS.items():
            frame = self._render_from_polar(
                radar, product_name, product_config,
                site, scan_ts, scan_iso, bounds, site_dir,
                other_sites_aeqd=other_sites_aeqd,
            )
            if frame:
                frames.append(frame)
        diag["last_render_duration_s"] = round(time.perf_counter() - t_render, 2)
        diag["last_product_count"]     = len(frames)

        # Collect elevation angles
        elevation_angles = []
        try:
            sweep_start = radar.sweep_start_ray_index["data"]
            for i in range(radar.nsweeps):
                elevation_angles.append(float(radar.elevation["data"][sweep_start[i]]))
        except Exception:
            pass

        return frames, radar, scan_iso, bounds, elevation_angles, site_lat, site_lon

    @staticmethod
    def _identify_low_tilt_scans(radar) -> list[tuple[int, Optional[int], datetime, str]]:
        """Find all 0.5° (or whatever the lowest tilt is) scan groups in a VCP.

        Modern NEXRAD severe-weather VCPs use **SAILS** (Supplemental Adaptive
        Intra-Volume Low-Level Scan) — the radar revisits the lowest tilt 1–3
        extra times mid-VCP.  Each revisit is a fresh low-level reflectivity
        and velocity pair tagged at the same elevation as the original sweep 0
        but at later sweep indices.  Extracting these as separate scans gives
        the operator sub-VCP timeline updates during severe weather (75–100 s
        cadence vs 4–5 min for the full volume).

        Returns a list of (refl_sweep_idx, vel_sweep_idx_or_None, scan_time,
        label) tuples, one per low-tilt scan group.  `label` is "original" for
        the first occurrence, "sails-1", "sails-2", ... for subsequent ones.
        scan_time uses per-ray timestamps in radar.time["data"], so it
        reflects when the sweep was actually performed, not the VCP start.
        """
        try:
            n_sweeps = int(radar.nsweeps)
            fixed_angles = np.asarray(radar.fixed_angle["data"], dtype=float)
        except Exception:
            return []
        if n_sweeps == 0:
            return []

        target_elev = float(np.min(fixed_angles))
        tol = 0.3  # any sweep within ±0.3° of the lowest is considered the same tilt

        # Volume start time (datetime) for resolving per-ray time offsets
        try:
            units = radar.time["units"]
            base_str = units.split("since ")[-1].replace("Z", "+00:00")
            base_dt = datetime.fromisoformat(base_str)
            time_data = np.asarray(radar.time["data"], dtype=float)
            sweep_start = np.asarray(radar.sweep_start_ray_index["data"], dtype=int)
            sweep_end = np.asarray(radar.sweep_end_ray_index["data"], dtype=int)
        except Exception:
            return []

        scans: list[tuple[int, Optional[int], datetime, str]] = []
        i = 0
        while i < n_sweeps:
            if abs(fixed_angles[i] - target_elev) <= tol:
                refl_idx = i
                vel_idx: Optional[int] = None
                # Look for an immediately-following sweep at the same low tilt
                # (the paired Doppler scan); NEXRAD pairs them adjacent.
                if i + 1 < n_sweeps and abs(fixed_angles[i + 1] - target_elev) <= tol:
                    vel_idx = i + 1
                    next_i = i + 2
                else:
                    next_i = i + 1

                # Median ray time for the refl sweep — robust against edge rays
                try:
                    s0 = int(sweep_start[refl_idx])
                    s1 = int(sweep_end[refl_idx])
                    offset_s = float(np.median(time_data[s0:s1 + 1]))
                    scan_time = base_dt + timedelta(seconds=offset_s)
                except Exception:
                    scan_time = base_dt

                label = "original" if not scans else f"sails-{len(scans)}"
                scans.append((refl_idx, vel_idx, scan_time, label))
                i = next_i
            else:
                i += 1
        return scans

    def dealias_radar_in_place(self, radar) -> None:
        """Run velocity dealiasing on the radar object once; later sub-scans
        extracted from it will inherit `velocity_dealiased` and pass
        `skip_dealias=True` to `_process_radar_object`.
        """
        _ensure_imports()
        if "velocity" not in radar.fields:
            return
        try:
            gatefilter = pyart.filters.GateFilter(radar)
            gatefilter.exclude_below("reflectivity", -20)
            corrected_vel = pyart.correct.dealias_region_based(
                radar,
                gatefilter=gatefilter,
                skip_between_rays=True,
                skip_along_ray=True,
            )
            radar.add_field("velocity_dealiased", corrected_vel, replace_existing=True)
        except Exception as e:
            logger.warning(f"Velocity dealiasing failed: {e}")

    def _create_grid(self, radar, site_lat: float, site_lon: float):
        """Create a cartesian grid from polar radar data."""
        range_m = self._max_range_km * 1000
        res_m = self._grid_resolution_km * 1000
        grid_shape = int(2 * range_m / res_m)

        grid = pyart.map.grid_from_radars(
            (radar,),
            grid_shape=(1, grid_shape, grid_shape),
            grid_limits=(
                (1000, 2000),  # Single level near surface
                (-range_m, range_m),
                (-range_m, range_m),
            ),
            fields=list(radar.fields.keys()),
            weighting_function="Barnes2",
            grid_origin=(site_lat, site_lon),
        )
        return grid

    def _render_from_polar(
        self,
        radar,
        product_name: str,
        product_config: dict,
        site: str,
        scan_ts: str,
        scan_iso: str,
        bounds: dict,
        site_dir: Path,
        other_sites_aeqd: list[tuple[float, float]] = (),
    ) -> Optional[RadarFrame]:
        """
        Extract raw polar (azimuth × range) arrays and pack into the RDRF binary
        wire format for WebGL rendering on the client.  No server-side rasterization.
        """
        field_name = product_config["field"]
        # SRV is derived from velocity — use dealiased if available for both.
        if field_name == "velocity" and "velocity_dealiased" in radar.fields:
            field_name = "velocity_dealiased"
        if field_name not in radar.fields:
            if product_config["field"] not in radar.fields:
                logger.debug(f"Product {product_name} not available in this scan")
                return None
            field_name = product_config["field"]

        # SRV requires a storm motion estimate.  If we don't have one yet (no
        # storm tracking has run), skip producing the product so the frontend
        # doesn't show a stale or misleading SRV identical to base velocity.
        if product_name == "storm_relative_velocity":
            if self._storm_motion is None:
                logger.debug("SRV skipped: no storm motion available")
                return None

        try:
            # ── Find lowest sweep with valid data ──────────────────────────────
            sweep_idx = 0
            for i in range(radar.nsweeps):
                sl = radar.get_slice(i)
                field_data = radar.fields[field_name]["data"][sl]
                if np.sum(~np.ma.getmaskarray(field_data)) > 100:
                    sweep_idx = i
                    break
            else:
                logger.debug(f"No valid data for {product_name} in any sweep")
                return None

            sweep_slice = radar.get_slice(sweep_idx)
            sweep_data = np.ma.filled(
                radar.fields[field_name]["data"][sweep_slice].astype(float), np.nan
            )
            azimuths = radar.azimuth["data"][sweep_slice].astype(float)  # (n_rays,)
            n_gates = sweep_data.shape[1]
            ranges_m = radar.range["data"][:n_gates].astype(float)       # (n_gates,)
            elevation = float(radar.elevation["data"][sweep_slice][0])

            # Coverage check on the INPUT sweep — done BEFORE the regrid so
            # we measure what the radar actually delivered, not what got
            # filled in afterward.  Chunks-partial 90° wedges have ~25%
            # coverage; we skip those.  Full sweeps with some
            # dropped/colliding rays (the common stripe-causing case) have
            # ~95-100% coverage; we proceed and let the regrid's always-on
            # NaN fill handle the holes regardless of how many bins they
            # span post-regrid.
            n_rays_in = len(azimuths)
            if n_rays_in > 1:
                spacings_chk = np.diff(np.sort(azimuths))
                spacings_chk = spacings_chk[spacings_chk > 1e-6]
                median_spacing = float(np.median(spacings_chk)) if spacings_chk.size > 0 else 0.5
                expected_full = int(round(360.0 / max(median_spacing, 0.1)))
            else:
                expected_full = 720
            source_coverage_pct = (n_rays_in / max(expected_full, 1)) * 100.0
            if source_coverage_pct < 50.0:
                logger.info(
                    f"{product_name}: skipping — source coverage "
                    f"{source_coverage_pct:.0f}% ({n_rays_in}/{expected_full} rays); "
                    "deferring to next broadcast"
                )
                return None

            # Regrid to uniform azimuth bins.  The helper always fills any
            # post-snap NaN bins from their nearest valid neighbour — the
            # source-coverage guard above ensures this only runs on sweeps
            # that actually cover the disk, so the fill can't paper over a
            # legitimate wedge.
            azimuths, sweep_data = self._ensure_uniform_azimuth_grid(
                azimuths, sweep_data,
            )

            # Storm-Relative Velocity: subtract the radial component of the
            # cached mean storm motion from each ray.  The radial unit vector
            # at azimuth θ (measured from north) is (sin θ, cos θ) in
            # (east, north) space, so the storm motion's radial component is
            # v_x·sin θ + v_y·cos θ where v_x, v_y are the storm's east/north
            # velocity components.  Subtracting this yields the velocity each
            # parcel has *relative to the storm* — revealing rotation that's
            # masked by ground-relative translation.
            if product_name == "storm_relative_velocity":
                dir_deg, speed_kph = self._storm_motion  # checked non-None above
                speed_ms = speed_kph / 3.6
                rad = math.radians(dir_deg)
                v_x = speed_ms * math.sin(rad)
                v_y = speed_ms * math.cos(rad)
                az_rad = np.radians(azimuths)
                radial_storm = v_x * np.sin(az_rad) + v_y * np.cos(az_rad)
                sweep_data = sweep_data - radial_storm[:, np.newaxis]

            logger.debug(
                f"{product_name}: sweep {sweep_idx} el={elevation:.1f}° "
                f"gates={n_gates} rays_in={n_rays_in} rays_out={len(azimuths)}"
            )

            max_range_m = min(
                float(self._max_range_km * 1000),
                float(ranges_m[-1]) if len(ranges_m) else float(self._max_range_km * 1000),
            )

            # ── Polar Voronoi masking ──────────────────────────────────────────
            # Each gate at (azimuth θ, range r) has AEQD position (r·sinθ, r·cosθ).
            # Mask it if any other active site is closer than this site (range r).
            if other_sites_aeqd:
                theta_rad = np.radians(azimuths)[:, np.newaxis]  # (n_rays, 1)
                r_grid    = ranges_m[np.newaxis, :]              # (1, n_gates)
                x_gate    = r_grid * np.sin(theta_rad)           # (n_rays, n_gates)
                y_gate    = r_grid * np.cos(theta_rad)
                for ox, oy in other_sites_aeqd:
                    dist_sq_other = (x_gate - ox) ** 2 + (y_gate - oy) ** 2
                    sweep_data[dist_sq_other < r_grid ** 2] = np.nan

            # Mask gates beyond max range
            gate_mask = ranges_m > max_range_m
            if gate_mask.any():
                sweep_data[:, gate_mask] = np.nan

            # Reflectivity gate filter
            if product_name == "reflectivity":
                sweep_data[sweep_data < self._gate_dbz] = np.nan

            # ── Pack and return ────────────────────────────────────────────────
            vmin = float(product_config["vmin"])
            vmax_val = float(product_config["vmax"])
            frame_id = f"{site}_{scan_ts}_{product_name}"
            binary = self._pack_binary_frame(
                azimuths, ranges_m, sweep_data,
                product=product_name, site=site,
                timestamp_iso=scan_iso, elevation=elevation,
                bounds=bounds, vmin=vmin, vmax=vmax_val,
                max_range_m=max_range_m,  # must match bounds range
            )
            logger.debug(f"Packed binary frame {frame_id} ({len(binary) // 1024} KB)")
            return RadarFrame(
                product=product_name,
                bounds=bounds,
                timestamp=scan_iso,
                site=site,
                elevation=elevation,
                frame_id=frame_id,
                binary_data=binary,
            )

        except Exception as e:
            logger.error(f"Failed polar pack for {product_name}: {e}", exc_info=True)
            return None

    def _render_polar_to_file(
        self,
        radar,
        product_name: str,
        product_config: dict,
        site: str,
        scan_ts: str,
        scan_iso: str,
        bounds: dict,
        site_dir: Path,
    ) -> Optional[RadarFrame]:
        """
        Server-side Pillow rasterization path — used only by oneshot_frame for
        social media graphics.  Produces a 2048×2048 RGBA WebP file on disk.
        """
        from PIL import Image

        field_name = product_config["field"]
        if field_name == "velocity" and "velocity_dealiased" in radar.fields:
            field_name = "velocity_dealiased"
        if field_name not in radar.fields:
            if product_config["field"] not in radar.fields:
                return None
            field_name = product_config["field"]

        try:
            sweep_idx = 0
            for i in range(radar.nsweeps):
                sl = radar.get_slice(i)
                field_data = radar.fields[field_name]["data"][sl]
                if np.sum(~np.ma.getmaskarray(field_data)) > 100:
                    sweep_idx = i
                    break
            else:
                return None

            sweep_slice = radar.get_slice(sweep_idx)
            sweep_data = np.ma.filled(
                radar.fields[field_name]["data"][sweep_slice].astype(float), np.nan
            )
            azimuths = radar.azimuth["data"][sweep_slice]
            n_gates = sweep_data.shape[1]
            ranges_m = radar.range["data"][:n_gates]

            sweep_max_range_m = float(ranges_m[-1]) if len(ranges_m) else float(self._max_range_km * 1000)
            max_range_m = min(float(self._max_range_km * 1000), sweep_max_range_m)

            rgba_uint8 = self._rasterize_polar_to_rgba(
                np.asarray(azimuths, dtype=float),
                np.asarray(ranges_m, dtype=float),
                sweep_data, product_name, max_range_m,
            )
            if rgba_uint8 is None:
                return None

            filename = f"{scan_ts}_{product_name}.webp"
            filepath = site_dir / filename
            Image.fromarray(rgba_uint8, "RGBA").save(
                str(filepath), "WEBP", quality=90, method=4, lossless=False
            )
            logger.debug(f"oneshot: saved {filename} ({filepath.stat().st_size // 1024} KB)")

            elevation = float(radar.elevation["data"][radar.get_slice(0)][0])
            return RadarFrame(
                product=product_name,
                bounds=bounds,
                timestamp=scan_iso,
                site=site,
                elevation=elevation,
                frame_id=f"{site}_{scan_ts}_{product_name}",
                image_path=str(filepath),
                image_url=f"/api/radar/images/{site}/{filename}",
            )

        except Exception as e:
            logger.error(f"Failed oneshot polar render for {product_name}: {e}", exc_info=True)
            return None

    def _rasterize_polar_to_rgba(
        self,
        azimuths: np.ndarray,
        ranges_m: np.ndarray,
        sweep_data: np.ndarray,
        product_name: str,
        max_range_m: float,
        img_size: int = 2048,
    ) -> "Optional[np.ndarray]":
        """Rasterize a polar sweep (rays × gates) into an RGBA uint8 image via
        nearest-ray / bilinear-gate sampling and the product colormap.

        Shared by the oneshot Pillow path (`_render_polar_to_file`) and the
        cached-binary graphic path (`render_binary_frame_to_image`).
        """
        try:
            n_rays, n_gates = sweep_data.shape
            if n_rays < 2 or n_gates < 2:
                return None

            coords = np.linspace(-max_range_m, max_range_m, img_size)
            xx, yy = np.meshgrid(coords, coords[::-1])
            r_map = np.sqrt(xx ** 2 + yy ** 2)
            az_map = np.degrees(np.arctan2(xx, yy)) % 360

            sort_idx = np.argsort(azimuths)
            sorted_az = azimuths[sort_idx]
            sorted_data = sweep_data[sort_idx, :]
            az_flat = az_map.ravel()

            pos = np.searchsorted(sorted_az, az_flat, side="right")
            ray_hi = pos % n_rays
            ray_lo = (pos - 1) % n_rays
            az_lo_v = sorted_az[ray_lo]
            az_hi_v = sorted_az[ray_hi]
            daz = (az_hi_v - az_lo_v) % 360.0
            daz = np.where(daz == 0, 1e-6, daz)
            t_az = np.clip(((az_flat - az_lo_v) % 360.0) / daz, 0.0, 1.0)

            gate_step = ranges_m[1] - ranges_m[0] if n_gates > 1 else 1.0
            gate_frac = (r_map.ravel() - ranges_m[0]) / gate_step
            gate_lo = np.clip(gate_frac.astype(int), 0, n_gates - 2)
            gate_hi = gate_lo + 1
            t_gate = np.clip(gate_frac - gate_lo, 0.0, 1.0)

            v00 = sorted_data[ray_lo, gate_lo]
            v10 = sorted_data[ray_hi, gate_lo]
            v01 = sorted_data[ray_lo, gate_hi]
            v11 = sorted_data[ray_hi, gate_hi]
            v_bilinear = (
                (1 - t_az) * (1 - t_gate) * np.nan_to_num(v00)
                + t_az * (1 - t_gate) * np.nan_to_num(v10)
                + (1 - t_az) * t_gate * np.nan_to_num(v01)
                + t_az * t_gate * np.nan_to_num(v11)
            )
            all_nan = np.isnan(v00) & np.isnan(v10) & np.isnan(v01) & np.isnan(v11)
            v_bilinear[all_nan] = np.nan
            values = v_bilinear.reshape(img_size, img_size)
            values[r_map > max_range_m] = np.nan
            if product_name == "reflectivity":
                values[values < self._gate_dbz] = np.nan
                try:
                    from scipy.ndimage import gaussian_filter
                    valid_mask = ~np.isnan(values)
                    smoothed = gaussian_filter(np.nan_to_num(values, nan=0.0), sigma=0.5)
                    weights = gaussian_filter(valid_mask.astype(float), sigma=0.5)
                    with np.errstate(invalid="ignore", divide="ignore"):
                        values = np.where(weights > 0.3, smoothed / weights, values)
                    values[r_map > max_range_m] = np.nan
                    values[values < self._gate_dbz] = np.nan
                except ImportError:
                    pass

            product_config = RADAR_PRODUCTS.get(product_name, {})
            vmin = float(product_config.get("vmin", 0.0))
            vmax = float(product_config.get("vmax", 1.0))
            norm = np.clip((values - vmin) / (vmax - vmin), 0.0, 1.0)
            cmap = self._get_cmap(product_name)
            rgba = cmap(norm)
            rgba[np.isnan(values)] = [0, 0, 0, 0]
            return (rgba * 255).astype(np.uint8)

        except Exception as e:
            logger.error(f"_rasterize_polar_to_rgba failed for {product_name}: {e}", exc_info=True)
            return None

    def render_binary_frame_to_image(
        self, frame: "RadarFrame", out_dir: "Optional[Path]" = None
    ) -> "Optional[RadarFrame]":
        """Rasterize an in-memory binary (RDRF) frame to a PNG file so the
        Pillow-based broadcast graphic can overlay OUR Level-2 radar instead of
        falling back to public composite tiles.

        Returns a new RadarFrame carrying ``image_path`` (the source frame is left
        unchanged), or None if the frame has no binary payload or rendering fails.
        """
        if frame is None or not getattr(frame, "binary_data", None):
            return None
        try:
            _ensure_imports()
            import struct
            import re as _re
            import json as _json
            from PIL import Image

            buf = frame.binary_data
            if len(buf) < 16 or buf[:4] != b"\x52\x44\x52\x46":
                logger.warning("render_binary_frame_to_image: bad RDRF magic")
                return None

            meta_len, n_rays, n_gates = struct.unpack_from("<III", buf, 4)
            off = 16
            meta = _json.loads(buf[off:off + meta_len].decode("utf-8"))
            off += meta_len
            azimuths = np.frombuffer(buf, dtype="<f4", count=n_rays, offset=off).astype(float)
            off += n_rays * 4
            ranges_m = np.frombuffer(buf, dtype="<f4", count=n_gates, offset=off).astype(float)
            off += n_gates * 4
            gate_u8 = np.frombuffer(
                buf, dtype=np.uint8, count=n_rays * n_gates, offset=off
            ).reshape(n_rays, n_gates)

            product_name = meta.get("product", frame.product)
            vmin = float(meta.get("vmin", 0.0))
            vmax = float(meta.get("vmax", 1.0))
            span = (vmax - vmin) if vmax != vmin else 1.0
            # Reconstruct float values; 0x00 was the no-data sentinel at pack time.
            sweep_data = vmin + (gate_u8.astype(float) / 255.0) * span
            sweep_data[gate_u8 == 0] = np.nan

            max_range_m = float(
                meta.get("max_range_m")
                or (ranges_m[-1] if len(ranges_m) else self._max_range_km * 1000)
            )

            rgba_uint8 = self._rasterize_polar_to_rgba(
                azimuths, ranges_m, sweep_data, product_name, max_range_m
            )
            if rgba_uint8 is None:
                return None

            out_dir = out_dir or (Path(tempfile.gettempdir()) / f"nexrad_graphic_{frame.site}")
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_id = _re.sub(r"[^\w\-.]", "_", frame.frame_id or f"{frame.site}_{product_name}")
            filepath = out_dir / f"{safe_id}.png"
            Image.fromarray(rgba_uint8, "RGBA").save(str(filepath), "PNG")
            logger.info(f"render_binary_frame_to_image: wrote {filepath.name} for graphic overlay")

            return RadarFrame(
                product=product_name,
                bounds=frame.bounds,
                timestamp=frame.timestamp,
                site=frame.site,
                elevation=frame.elevation,
                frame_id=frame.frame_id,
                image_path=str(filepath),
            )

        except Exception as e:
            logger.warning(f"render_binary_frame_to_image failed: {e}")
            return None

    def _render_product(
        self,
        grid,
        radar,
        product_name: str,
        product_config: dict,
        site: str,
        scan_ts: str,
        scan_iso: str,
        bounds: dict,
        site_dir: Path,
    ) -> Optional[RadarFrame]:
        """Render a single radar product to PNG."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from PIL import Image

        field_name = product_config["field"]

        # Use dealiased velocity if available
        if field_name == "velocity" and "velocity_dealiased" in radar.fields:
            field_name = "velocity_dealiased"

        # Check if field exists in grid
        if field_name not in grid.fields:
            # Fall back to original field name
            if product_config["field"] not in grid.fields:
                logger.debug(f"Product {product_name} not available in this scan")
                return None
            field_name = product_config["field"]

        try:
            data = grid.fields[field_name]["data"][0]  # First (only) vertical level
            data = np.ma.filled(data, np.nan)

            # Gate filter: mask sub-threshold reflectivity so it renders transparent
            if product_name == "reflectivity":
                data[data < self._gate_dbz] = np.nan

            # Normalize to 0-1 range
            vmin = product_config["vmin"]
            vmax = product_config["vmax"]
            normalized = (data - vmin) / (vmax - vmin)
            normalized = np.clip(normalized, 0, 1)

            # Apply colormap
            cmap = self._get_cmap(product_name)
            rgba = cmap(normalized)

            # Set NaN pixels to transparent
            nan_mask = np.isnan(data)
            rgba[nan_mask] = [0, 0, 0, 0]

            # Convert to 8-bit RGBA
            rgba_uint8 = (rgba * 255).astype(np.uint8)

            # Flip vertically (image convention vs array convention)
            rgba_uint8 = np.flipud(rgba_uint8)

            # Save as WebP
            filename = f"{scan_ts}_{product_name}.webp"
            filepath = site_dir / filename
            img = Image.fromarray(rgba_uint8, "RGBA")
            img.save(str(filepath), "WEBP", quality=90, method=4, lossless=False)

            image_url = f"/api/radar/images/{site}/{filename}"

            return RadarFrame(
                product=product_name,
                image_path=str(filepath),
                image_url=image_url,
                bounds=bounds,
                timestamp=scan_iso,
                site=site,
                elevation=0.5,
            )

        except Exception as e:
            logger.error(f"Failed to render {product_name}: {e}", exc_info=True)
            return None

    # Temp-dir name patterns the render paths create under the system temp
    # directory.  oneshot_frame writes social-graphic WebPs to
    # nexrad_oneshot_<SITE>/; render_binary_frame_to_image writes broadcast
    # PNGs to nexrad_graphic_<SITE>/.  Neither cleaned up after itself.
    _TEMP_DIR_GLOBS = ("nexrad_oneshot_*", "nexrad_graphic_*")

    def purge_old_data(self, max_age_hours: Optional[float] = None) -> int:
        """Delete stale radar image files from every location that accumulates them.

        Covers two unbounded sinks that filled the C: drive during long
        streaming sessions:
          1. ``data/radar/<site>/*.{webp,png}`` — legacy Pillow live-path output
             and social-graphic re-renders.  Pruned by the scan timestamp
             encoded in the filename (mtime fallback).
          2. ``%TEMP%/nexrad_oneshot_<site>/`` and ``nexrad_graphic_<site>/`` —
             social/broadcast graphic rasterizations.  Pruned by file mtime
             (their filenames don't carry a reliably parseable scan time), and
             the directory itself is removed once empty.

        The live WebSocket path keeps frames in memory (RadarFrame.binary_data),
        so it is unaffected.  Returns the number of files removed.
        """
        if max_age_hours is None:
            max_age_hours = self._data_retention_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        removed = 0

        # 1) data/radar/<site>
        if self._data_dir.exists():
            for site_dir in self._data_dir.iterdir():
                if not site_dir.is_dir():
                    continue
                for img_file in (*site_dir.glob("*.webp"), *site_dir.glob("*.png")):
                    if self._image_is_stale(img_file, cutoff):
                        try:
                            img_file.unlink()
                            removed += 1
                        except OSError:
                            pass

        # 2) %TEMP%/nexrad_{oneshot,graphic}_*
        tmp_root = Path(tempfile.gettempdir())
        cutoff_ts = cutoff.timestamp()
        for pattern in self._TEMP_DIR_GLOBS:
            for tmp_dir in tmp_root.glob(pattern):
                if not tmp_dir.is_dir():
                    continue
                for f in tmp_dir.glob("*"):
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff_ts:
                            f.unlink()
                            removed += 1
                    except OSError:
                        pass
                # Drop the directory if it's now empty.
                try:
                    next(tmp_dir.iterdir())
                except StopIteration:
                    try:
                        tmp_dir.rmdir()
                    except OSError:
                        pass
                except OSError:
                    pass

        if removed:
            logger.info(
                f"Radar cleanup: removed {removed} stale image file(s) "
                f"(older than {max_age_hours:g}h)"
            )
        return removed

    @staticmethod
    def _image_is_stale(path: Path, cutoff: datetime) -> bool:
        """True if a data/radar image is older than ``cutoff``.

        Prefers the ``YYYYMMDD_HHMMSS`` scan timestamp encoded in the filename;
        falls back to file mtime when the name doesn't parse.
        """
        parts = path.stem.split("_")
        if len(parts) >= 2:
            try:
                ts = datetime.strptime(
                    f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S"
                ).replace(tzinfo=timezone.utc)
                return ts < cutoff
            except ValueError:
                pass
        try:
            return path.stat().st_mtime < cutoff.timestamp()
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[NexradService] = None


def get_nexrad_service() -> Optional[NexradService]:
    return _service


async def start_nexrad_service() -> bool:
    """Start the NEXRAD service. Returns True if started successfully."""
    global _service
    try:
        _ensure_imports()
    except ImportError as e:
        logger.warning(f"Cannot start NEXRAD service: {e}")
        return False

    # PIL is only needed for oneshot_frame (social graphics), not the live path
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        logger.warning("Pillow not found — oneshot_frame (social graphics radar) will be unavailable")

    _service = NexradService()
    try:
        await _service.start()
        return True
    except Exception as e:
        logger.error(f"Failed to start NEXRAD service: {e}", exc_info=True)
        _service = None
        return False


async def stop_nexrad_service():
    """Stop the NEXRAD service."""
    global _service
    if _service:
        await _service.stop()
        _service = None
