"""
NEXRAD Level 2 radar data service.
Downloads volume scans from AWS S3, processes with Py-ART, and renders
georeferenced PNG images for frontend display.
"""

import asyncio
import io
import logging
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
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RadarFrame:
    """A single rendered radar frame for one product."""
    product: str
    image_path: str
    image_url: str
    bounds: dict  # {south, north, west, east}
    timestamp: str  # ISO format
    site: str
    elevation: float

    def to_dict(self) -> dict:
        return {
            "product": self.product,
            "image_url": self.image_url,
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

        # nexradaws connection (lazy init)
        self._conn = None

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

        # Clean up cached frames from disk
        for frames_list in self._frames.pop(site_id, {}).values():
            for frame in frames_list:
                try:
                    if os.path.exists(frame.image_path):
                        os.remove(frame.image_path)
                except OSError:
                    pass
        self._last_scan_key.pop(site_id, None)
        self._last_volume.pop(site_id, None)

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
                for frames_list in self._frames.pop(old, {}).values():
                    for frame in frames_list:
                        try:
                            if os.path.exists(frame.image_path):
                                os.remove(frame.image_path)
                        except OSError:
                            pass
                self._last_scan_key.pop(old, None)
                self._last_volume.pop(old, None)

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

        # Initial fetch
        await self._fetch_and_process()

        # Start polling loop
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        """Stop the radar service."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._status.enabled = False
        logger.info("NEXRAD service stopped")

    async def _poll_loop(self):
        """Periodically check for new volume scans.
        NEXRAD VCPs complete every 4–6 minutes. We poll every 30 s to catch new
        scans within ~30 s of them landing on S3 rather than waiting up to 60 s.
        """
        while self._running:
            try:
                await asyncio.sleep(30)
                await self._fetch_and_process()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Radar poll error: {e}", exc_info=True)
                self._status.error = str(e)
                await asyncio.sleep(30)  # Back off on error

    async def _fetch_and_process(self):
        """Fetch the latest volume scan for all active sites (sequentially)."""
        if self._status.processing:
            logger.debug("Radar already processing, skipping")
            return

        self._status.processing = True
        self._status.error = None
        if self.on_status_change:
            await self.on_status_change(self._status)

        try:
            for site in list(self._active_sites):
                await self._fetch_and_process_site(site)
        except Exception as e:
            logger.error(f"Radar fetch/process error: {e}", exc_info=True)
            self._status.error = str(e)
        finally:
            self._status.processing = False
            if self.on_status_change:
                await self.on_status_change(self._status)

    async def _fetch_and_process_site(self, site: str):
        """Fetch and process the latest scan for one site."""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._download_and_process_sync, site
            )
        except Exception as e:
            logger.error(f"Radar fetch/process error for {site}: {e}", exc_info=True)
            return

        if result:
            frames, volume = result
            site_frames = self._frames.setdefault(site, {p: [] for p in RADAR_PRODUCTS})
            for frame in frames:
                product_frames = site_frames[frame.product]
                product_frames.append(frame)
                if len(product_frames) > self._history_count:
                    old = product_frames.pop(0)
                    try:
                        if os.path.exists(old.image_path):
                            os.remove(old.image_path)
                    except OSError:
                        pass

            self._last_volume[site] = volume
            self._status.last_update = volume.timestamp

            # Broadcast frames immediately — don't wait for storm tracking
            if self.on_frame_ready:
                await self.on_frame_ready(frames)

            # Storm tracking runs after frame broadcast so UI updates first
            if self.on_volume_ready:
                await self.on_volume_ready(volume)

            logger.info(
                f"Radar update: {site} @ {volume.timestamp} "
                f"({len(frames)} products rendered)"
            )

    def _download_and_process_sync(self, site: str) -> Optional[tuple[list[RadarFrame], VolumeScanData]]:
        """
        Synchronous download + processing for one site (runs in executor thread).
        Returns (frames, volume_data) or None if no new data.
        """
        _ensure_imports()
        now = datetime.now(timezone.utc)

        # Find available scans from the last 30 minutes
        conn = self._get_conn()
        try:
            scans = conn.get_avail_scans(
                now.year, now.month, now.day, site
            )
        except Exception as e:
            logger.error(f"Failed to list scans for {site}: {e}")
            return None

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
        if volume_scans:
            logger.debug(f"Latest scan key: {_scan_key(volume_scans[-1])}")

        if not volume_scans:
            logger.warning(f"No standard volume scans for {site}. Raw list: {[_scan_key(s) for s in scans[-5:]]}")
            return None

        # Get the most recent scan
        latest_scan = volume_scans[-1]
        scan_key = _scan_key(latest_scan)

        # Skip if we already processed this scan
        if scan_key == self._last_scan_key.get(site):
            logger.debug(f"No new scan for {site} (already processed {scan_key})")
            return None

        # Download the scan
        logger.info(f"Downloading scan: {scan_key}")
        try:
            results = conn.download(latest_scan, tempfile.gettempdir())
        except Exception as e:
            logger.error(f"Failed to download scan {scan_key}: {e}")
            return None

        if not results.success:
            logger.error(f"Download failed for {scan_key}: {results.failed}")
            return None

        result = results.success[0]
        local_file = result.filepath

        try:
            # Parse with Py-ART
            radar = pyart.io.read_nexrad_archive(
                str(local_file),
                linear_interp=False
            )

            # Extract timestamp from radar metadata
            try:
                time_start = radar.time["units"]
                # Format: "seconds since YYYY-MM-DDTHH:MM:SSZ"
                time_str = time_start.split("since ")[-1].replace("Z", "+00:00")
                scan_time = datetime.fromisoformat(time_str)
            except Exception:
                scan_time = now

            scan_ts = scan_time.strftime("%Y%m%d_%H%M%S")
            scan_iso = scan_time.isoformat()

            # Get site location for bounds
            from backend.services.nexrad_sites import NEXRAD_SITES
            site_info = NEXRAD_SITES.get(site, {})
            site_lat = site_info.get("lat", radar.latitude["data"][0])
            site_lon = site_info.get("lon", radar.longitude["data"][0])

            # Compute image bounds for Leaflet ImageOverlay.
            # Leaflet stretches the image linearly between [south,west] and [north,east].
            # Our image grid is a square in AEQD metres: x runs west→east, y runs south→north.
            # The cardinal extremes (due N/S/E/W from the site) give the correct edges:
            #   north edge = point due north at range_m
            #   south edge = point due south at range_m
            #   east  edge = point due east  at range_m  (widest longitude extent)
            #   west  edge = point due west  at range_m
            # This avoids the AEQD corner distortion that makes the image appear shifted.
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
            # We project each site's lat/lon into the current site's AEQD frame
            # so the renderer can compare pixel distances in metres.
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
                        # Flat-earth fallback
                        _dlat_km = (_o_lat - site_lat) * 111.0
                        _dlon_km = (
                            (_o_lon - site_lon) * 111.0
                            * np.cos(np.radians(site_lat))
                        )
                        other_sites_aeqd.append(
                            (_dlon_km * 1000.0, _dlat_km * 1000.0)
                        )

            # Dealias velocity if available
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

            # Render each product directly from polar data
            frames = []
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

            # Return frames immediately so the caller can broadcast before gridding
            # (gridding for storm tracking is slow and shouldn't delay the UI update)
            return_frames = frames

            # Grid the data for storm tracking (Barnes2 — slow, do after frames ready)
            grid = self._create_grid(radar, site_lat, site_lon)

            # Build volume scan data for storm tracking
            elevation_angles = []
            try:
                sweep_start = radar.sweep_start_ray_index["data"]
                for i in range(radar.nsweeps):
                    el = radar.elevation["data"][sweep_start[i]]
                    elevation_angles.append(float(el))
            except Exception:
                pass

            volume = VolumeScanData(
                site=site,
                timestamp=scan_iso,
                radar_object=radar,
                grid=grid,
                bounds=bounds,
                elevation_angles=elevation_angles,
            )

            self._last_scan_key[site] = scan_key
            return frames, volume

        finally:
            # Clean up downloaded file
            try:
                if os.path.exists(local_file):
                    os.remove(local_file)
            except OSError:
                pass

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
        High-resolution render directly from polar (azimuth × range) radar data.
        Produces a 2048×2048 RGBA WebP at native ~225 m gate spacing — no gridding,
        no interpolation smoothing.  Pixels beyond max range are transparent.
        """
        from PIL import Image

        field_name = product_config["field"]
        if field_name == "velocity" and "velocity_dealiased" in radar.fields:
            field_name = "velocity_dealiased"
        if field_name not in radar.fields:
            if product_config["field"] not in radar.fields:
                logger.debug(f"Product {product_name} not available in this scan")
                return None
            field_name = product_config["field"]

        try:
            # ── Find lowest sweep that actually contains this field ─────────────
            # NEXRAD split-cut VCPs put reflectivity and velocity on different sweeps.
            # Sweep 0 often has reflectivity only; velocity may start on sweep 1.
            sweep_idx = 0
            for i in range(radar.nsweeps):
                sl = radar.get_slice(i)
                field_data = radar.fields[field_name]["data"][sl]
                valid_count = np.sum(~np.ma.getmaskarray(field_data))
                if valid_count > 100:
                    sweep_idx = i
                    break
            else:
                logger.debug(f"No valid data for {product_name} in any sweep")
                return None

            sweep_slice = radar.get_slice(sweep_idx)
            sweep_data = np.ma.filled(
                radar.fields[field_name]["data"][sweep_slice].astype(float), np.nan
            )
            azimuths = radar.azimuth["data"][sweep_slice]   # shape (n_rays,)
            # Use actual gate count from this sweep's data (may differ from global range array)
            n_gates = sweep_data.shape[1]
            ranges_m = radar.range["data"][:n_gates]        # shape (n_gates,)

            logger.debug(f"{product_name}: using sweep {sweep_idx} "
                         f"(el={radar.elevation['data'][sweep_slice][0]:.1f}°, "
                         f"gates={len(ranges_m)}, rays={len(azimuths)})")

            sweep_max_range_m = float(ranges_m[-1]) if len(ranges_m) else float(self._max_range_km * 1000)
            max_range_m = min(float(self._max_range_km * 1000), sweep_max_range_m)
            img_size = 2048  # 2048×2048 — native gate resolution (~225m/px at 230km range)

            n_rays, n_gates = sweep_data.shape

            # ── Build Cartesian output grid ────────────────────────────────────
            coords = np.linspace(-max_range_m, max_range_m, img_size)
            xx, yy = np.meshgrid(coords, coords[::-1])

            r_map = np.sqrt(xx ** 2 + yy ** 2)
            az_map = np.degrees(np.arctan2(xx, yy)) % 360

            # ── Bilinear interpolation in polar coordinates ────────────────────
            # Convert continuous azimuth → fractional ray index
            # Sort rays by azimuth for interpolation
            sort_idx = np.argsort(azimuths)
            sorted_az = azimuths[sort_idx]
            sorted_data = sweep_data[sort_idx, :]  # (n_rays, n_gates) sorted

            az_flat = az_map.ravel()

            # Find bracketing ray indices (with wrap-around at 0/360)
            pos = np.searchsorted(sorted_az, az_flat, side="right")
            ray_hi = pos % n_rays
            ray_lo = (pos - 1) % n_rays

            az_lo = sorted_az[ray_lo]
            az_hi = sorted_az[ray_hi]

            # Angular difference, accounting for 0/360 wrap
            daz = (az_hi - az_lo) % 360.0
            daz = np.where(daz == 0, 1e-6, daz)  # avoid divide-by-zero
            t_az = ((az_flat - az_lo) % 360.0) / daz  # 0→1 between lo and hi ray
            t_az = np.clip(t_az, 0.0, 1.0)

            # Convert range → fractional gate index
            gate_step = ranges_m[1] - ranges_m[0] if n_gates > 1 else 1.0
            gate_frac = (r_map.ravel() - ranges_m[0]) / gate_step
            gate_lo = np.clip(gate_frac.astype(int), 0, n_gates - 2)
            gate_hi = gate_lo + 1
            t_gate = np.clip(gate_frac - gate_lo, 0.0, 1.0)

            # Bilinear sample: (1-t_az)*(1-t_gate)*v00 + t_az*(1-t_gate)*v10 + ...
            v00 = sorted_data[ray_lo, gate_lo]
            v10 = sorted_data[ray_hi, gate_lo]
            v01 = sorted_data[ray_lo, gate_hi]
            v11 = sorted_data[ray_hi, gate_hi]

            # If any corner is NaN, fall back to nearest available (keeps edges crisp)
            nan_any = np.isnan(v00) | np.isnan(v10) | np.isnan(v01) | np.isnan(v11)
            v_bilinear = (
                (1 - t_az) * (1 - t_gate) * np.nan_to_num(v00) +
                t_az       * (1 - t_gate) * np.nan_to_num(v10) +
                (1 - t_az) * t_gate       * np.nan_to_num(v01) +
                t_az       * t_gate       * np.nan_to_num(v11)
            )
            # Where all four corners are NaN, the pixel is truly missing
            all_nan = np.isnan(v00) & np.isnan(v10) & np.isnan(v01) & np.isnan(v11)
            v_bilinear[all_nan] = np.nan

            values = v_bilinear.reshape(img_size, img_size)

            # Mask pixels outside radar range
            values[r_map > max_range_m] = np.nan

            # Voronoi compositing: suppress pixels that are closer to another
            # active site than to this one.  Each pixel is "owned" by its
            # nearest radar, which gives the lowest beam height and the most
            # accurate dual-pol data.  This eliminates the visual artefacts
            # (doubled echoes, mis-coloured CC) in overlap zones.
            if other_sites_aeqd:
                dist_self = r_map  # distance from this site (already computed)
                for ox, oy in other_sites_aeqd:
                    dist_other = np.sqrt((xx - ox) ** 2 + (yy - oy) ** 2)
                    values[dist_other < dist_self] = np.nan

            # Reflectivity gate filter
            if product_name == "reflectivity":
                values[values < self._gate_dbz] = np.nan

            # ── Subtle smooth for reflectivity only ───────────────────────────
            # Sigma 0.5 just softens the azimuthal ray-stepping without blurring
            # echo boundaries.  Velocity and CC are left unsmoothed so couplets
            # and correlation structure stay sharp.
            if product_name == "reflectivity":
                try:
                    from scipy.ndimage import gaussian_filter
                    valid_mask = ~np.isnan(values)
                    smoothed = gaussian_filter(np.nan_to_num(values, nan=0.0), sigma=0.5)
                    weights  = gaussian_filter(valid_mask.astype(float), sigma=0.5)
                    # Avoid dividing by near-zero weights at echo edges
                    with np.errstate(invalid="ignore", divide="ignore"):
                        values = np.where(weights > 0.3, smoothed / weights, values)
                    # Re-apply masks that smoothing may have partially relaxed
                    values[r_map > max_range_m] = np.nan
                    values[values < self._gate_dbz] = np.nan
                except ImportError:
                    pass

            # ── Colormap ───────────────────────────────────────────────────────
            vmin, vmax = product_config["vmin"], product_config["vmax"]
            norm = np.clip((values - vmin) / (vmax - vmin), 0.0, 1.0)
            cmap = self._get_cmap(product_name)
            rgba = cmap(norm)

            nan_mask = np.isnan(values)
            rgba[nan_mask] = [0, 0, 0, 0]

            # ── Save WebP ──────────────────────────────────────────────────────
            # WebP RGBA is ~60% smaller than PNG at equivalent visual quality,
            # decodes faster in the browser, and fully supports transparency.
            rgba_uint8 = (rgba * 255).astype(np.uint8)
            filename = f"{scan_ts}_{product_name}.webp"
            filepath = site_dir / filename
            Image.fromarray(rgba_uint8, "RGBA").save(
                str(filepath), "WEBP", quality=90, method=4, lossless=False
            )
            logger.debug(f"Saved {filename} ({filepath.stat().st_size // 1024} KB)")

            elevation = float(radar.elevation["data"][radar.get_slice(0)][0])
            return RadarFrame(
                product=product_name,
                image_path=str(filepath),
                image_url=f"/api/radar/images/{site}/{filename}",
                bounds=bounds,
                timestamp=scan_iso,
                site=site,
                elevation=elevation,
            )

        except Exception as e:
            logger.error(f"Failed polar render for {product_name}: {e}", exc_info=True)
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

    def purge_old_data(self):
        """Remove radar images older than 2 hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        for site_dir in self._data_dir.iterdir():
            if not site_dir.is_dir():
                continue
            for img_file in [*site_dir.glob("*.webp"), *site_dir.glob("*.png")]:
                try:
                    # Parse timestamp from filename: YYYYMMDD_HHMMSS_product.png
                    parts = img_file.stem.split("_")
                    if len(parts) >= 2:
                        ts = datetime.strptime(
                            f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S"
                        ).replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            img_file.unlink()
                except (ValueError, OSError):
                    pass


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

    # Verify PIL is available
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow is required for NEXRAD rendering. Install with: pip install Pillow")
        return False

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
