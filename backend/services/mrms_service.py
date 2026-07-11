"""
MRMS Composite Reflectivity Service.

Downloads the latest CONUS MergedReflectivityQCComposite_00.50 product from the
public AWS S3 bucket (noaa-mrms-pds), parses the GRIB2 file using pygrib, applies
the standard NWS reflectivity colormap, and caches a RGBA PNG that the REST endpoint
serves to the frontend as a MapLibre image source.

Requires the `eccodes` Python package (ships bundled Windows/Linux DLLs — no conda needed):
    pip install eccodes

If eccodes is not available the service starts but immediately disables itself — the
frontend COMP button will be hidden and no MRMS data will load.

Update cadence: every 2 minutes (MRMS re-publishes ~every 2 min on average).
"""

import asyncio
import gzip
import io
import logging
import os
import tempfile
import threading
import time
from collections import deque, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np

from .grib_lock import GRIB_DECODE_LOCK

logger = logging.getLogger(__name__)

# ── MRMS CONUS geographic extent (MergedReflectivityQCComposite 0.01° grid) ──
MRMS_WEST   = -129.995
MRMS_EAST   =  -60.005
MRMS_NORTH  =   55.005
MRMS_SOUTH  =   20.005

MRMS_BUCKET  = "noaa-mrms-pds"
MRMS_PRODUCT = "CONUS/MergedReflectivityQCComposite_00.50"
MRMS_VMIN    = -20.0
MRMS_VMAX    =  80.0

# ── Product registry (id → S3 folder + display range/transform) ───────────────
# All MRMS CONUS products share the same 0.01° grid + S3 layout, so adding a
# product is just an entry here. `scale` maps the raw GRIB value to the display
# unit (mm→in 0.0393701, km→kft 3.28084, else 1.0); `vmin/vmax` are in DISPLAY
# units (they drive the app legend); `nodata_below` packs display values under it
# as transparent — which also drops the −999/−3 MRMS sentinels (and, for the
# rotation products, anticyclonic/weak values so only real rotation shows).
# Reflectivity stays on the dedicated continuously-polled path below; everything
# else is fetched on demand. Verified ranges/units against live S3 (2026-06-28).
MRMS_PRODUCTS: dict[str, dict] = {
    "reflectivity": {"s3": "CONUS/MergedReflectivityQCComposite_00.50", "vmin": -20.0, "vmax": 80.0, "scale": 1.0, "nodata_below": -15.0, "units": "dBZ"},
    # QPE: use the RADAR-ONLY accumulations, not MultiSensor *Pass2*. Pass2 waits
    # ~1 h to fold in hourly gauge data (and the MultiSensor accumulations publish
    # only hourly), so the app showed precip "an hour+ behind." RadarOnly_QPE_01H
    # updates every ~2 min (near-real-time); the multi-hour ones publish right on
    # the hour. Trade-off: radar-derived (no gauge bias correction), which is the
    # right call for a live radar view — freshness over gauge totals that land an
    # hour late. Same 0.01° grid + GRIB2 format, so only the S3 path changes.
    # vmax raised to represent real flooding rainfall (the old 4-15" ceilings
    # clipped events like 5"/hr and 17"/24h). Keep in lockstep with the radar app's
    # config/mrmsProducts.ts vmax — the byte packing normalizes over [vmin,vmax], so
    # a mismatch clips data or misaligns the legend/colors.
    "qpe_1h":   {"s3": "CONUS/RadarOnly_QPE_01H_00.00", "vmin": 0.0, "vmax": 8.0,  "scale": 0.0393701, "nodata_below": 0.01, "units": "in"},
    "qpe_3h":   {"s3": "CONUS/RadarOnly_QPE_03H_00.00", "vmin": 0.0, "vmax": 12.0, "scale": 0.0393701, "nodata_below": 0.01, "units": "in"},
    "qpe_6h":   {"s3": "CONUS/RadarOnly_QPE_06H_00.00", "vmin": 0.0, "vmax": 16.0, "scale": 0.0393701, "nodata_below": 0.01, "units": "in"},
    "qpe_12h":  {"s3": "CONUS/RadarOnly_QPE_12H_00.00", "vmin": 0.0, "vmax": 20.0, "scale": 0.0393701, "nodata_below": 0.01, "units": "in"},
    "qpe_24h":  {"s3": "CONUS/RadarOnly_QPE_24H_00.00", "vmin": 0.0, "vmax": 24.0, "scale": 0.0393701, "nodata_below": 0.01, "units": "in"},
    "precip_rate": {"s3": "CONUS/PrecipRate_00.00", "vmin": 0.0, "vmax": 10.0, "scale": 0.0393701, "nodata_below": 0.01, "units": "in/hr"},
    "mesh":     {"s3": "CONUS/MESH_00.50",            "vmin": 0.0, "vmax": 4.0, "scale": 0.0393701, "nodata_below": 0.05, "units": "in"},
    "mesh_30":  {"s3": "CONUS/MESH_Max_30min_00.50",  "vmin": 0.0, "vmax": 4.0, "scale": 0.0393701, "nodata_below": 0.05, "units": "in"},
    "mesh_60":  {"s3": "CONUS/MESH_Max_60min_00.50",  "vmin": 0.0, "vmax": 4.0, "scale": 0.0393701, "nodata_below": 0.05, "units": "in"},
    "mesh_120": {"s3": "CONUS/MESH_Max_120min_00.50", "vmin": 0.0, "vmax": 4.0, "scale": 0.0393701, "nodata_below": 0.05, "units": "in"},
    "mesh_240": {"s3": "CONUS/MESH_Max_240min_00.50", "vmin": 0.0, "vmax": 4.0, "scale": 0.0393701, "nodata_below": 0.05, "units": "in"},
    "posh":     {"s3": "CONUS/POSH_00.50", "vmin": 0.0, "vmax": 100.0, "scale": 1.0, "nodata_below": 1.0, "units": "%"},
    "azshear_low": {"s3": "CONUS/MergedAzShear_0-2kmAGL_00.50", "vmin": 0.0, "vmax": 20.0, "scale": 1.0, "nodata_below": 1.0, "units": "×10⁻³ s⁻¹"},
    "azshear_mid": {"s3": "CONUS/MergedAzShear_3-6kmAGL_00.50", "vmin": 0.0, "vmax": 15.0, "scale": 1.0, "nodata_below": 1.0, "units": "×10⁻³ s⁻¹"},
    "rot_30":   {"s3": "CONUS/RotationTrack30min_00.50",  "vmin": 0.0, "vmax": 30.0, "scale": 1.0, "nodata_below": 1.0, "units": "×10⁻³ s⁻¹"},
    "rot_60":   {"s3": "CONUS/RotationTrack60min_00.50",  "vmin": 0.0, "vmax": 30.0, "scale": 1.0, "nodata_below": 1.0, "units": "×10⁻³ s⁻¹"},
    "rot_120":  {"s3": "CONUS/RotationTrack120min_00.50", "vmin": 0.0, "vmax": 30.0, "scale": 1.0, "nodata_below": 1.0, "units": "×10⁻³ s⁻¹"},
    "rot_240":  {"s3": "CONUS/RotationTrack240min_00.50", "vmin": 0.0, "vmax": 30.0, "scale": 1.0, "nodata_below": 1.0, "units": "×10⁻³ s⁻¹"},
    "vil":      {"s3": "CONUS/VIL_00.50",      "vmin": 0.0, "vmax": 70.0, "scale": 1.0,     "nodata_below": 0.1, "units": "kg/m²"},
    "echotop_18": {"s3": "CONUS/EchoTop_18_00.50", "vmin": 0.0, "vmax": 60.0, "scale": 3.28084, "nodata_below": 0.5, "units": "kft"},
}

# ── Dependency check ──────────────────────────────────────────────────────────

_eccodes_ok: Optional[bool] = None

def _check_eccodes() -> bool:
    global _eccodes_ok
    if _eccodes_ok is None:
        try:
            import eccodes  # noqa: F401
            _eccodes_ok = True
        except ImportError:
            logger.warning(
                "eccodes not found — MRMS composite reflectivity is disabled. "
                "To enable: pip install eccodes"
            )
            _eccodes_ok = False
    return _eccodes_ok


# ── Service ───────────────────────────────────────────────────────────────────

class MRMSService:
    """Downloads, processes, and caches MRMS composite reflectivity.

    Caches two representations of the latest data:
      • binary: compact uint8 grid for the WebGL custom layer (primary)
      • PNG: fallback Mercator-reprojected image (kept for /api/mrms/latest.png)
    """

    # Binary wire format header (little-endian):
    #   [0:4]   magic 'MRMS'
    #   [4:8]   uint32 ni (columns, W→E)
    #   [8:12]  uint32 nj (rows, N→S)
    #   [12:20] float64 north   (La1 — first row latitude)
    #   [20:28] float64 south   (La2 — last row latitude)
    #   [28:36] float64 west    (Lo1 normalised to [-180,180])
    #   [36:44] float64 east    (Lo2 normalised to [-180,180])
    #   [44:48] float32 vmin    (dBZ at gate_value 1/255)
    #   [48:52] float32 vmax    (dBZ at gate_value 255/255)
    #   [52:]   uint8[] gate values, row-major, 0=no-data, 1-255=normalised

    BINARY_MAGIC = b"MRMS"

    def __init__(self):
        self._s3 = None
        self._s3_lock = threading.Lock()  # lazy boto3 init races across to_thread workers
        self._cmap = None           # matplotlib colormap, built lazily
        self._latest_binary: Optional[bytes] = None
        self._latest_png:    Optional[bytes] = None
        self._latest_grid   = None  # raw grid held for the lazy legacy-PNG render
        self._png_wanted_until = 0.0  # last PNG request + 10 min
        self._latest_ts:     Optional[str]   = None  # YYYYMMDD-HHMMSS
        self._latest_iso:    Optional[str]   = None
        # Rolling 60-min history: deque of (ts_str, iso_str, binary_bytes)
        self._history: deque = deque(maxlen=30)
        # On-demand per-product cache (everything except the polled reflectivity):
        #   pid → {ts, binary, fetched_at, last_access, frames: OrderedDict[ts → binary]}
        # Guarded by _pcache_lock (read-modify-write from concurrent to_thread
        # workers) and bounded GLOBALLY — each packed frame is ~6 MB, so a
        # per-product cap alone allows multi-GB growth across 20 products.
        self._pcache: dict[str, dict] = {}
        self._pcache_lock = threading.Lock()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

        # Fired when new data arrives: (timestamp_iso: str) -> Awaitable
        self.on_update: Optional[Callable] = None

    @property
    def available(self) -> bool:
        return _check_eccodes()

    @property
    def latest_binary(self) -> Optional[bytes]:
        return self._latest_binary

    def get_frame_list(self) -> list[dict]:
        """Return metadata for all cached frames, oldest first."""
        return [{"ts": ts, "iso": iso} for ts, iso, _ in self._history]

    def get_frame_binary(self, ts: str) -> Optional[bytes]:
        """Return packed binary for a specific frame timestamp."""
        for frame_ts, _, binary in self._history:
            if frame_ts == ts:
                return binary
        return None

    @property
    def latest_png(self) -> Optional[bytes]:
        # Marks the PNG "wanted" so subsequent polls keep it fresh; renders on
        # demand for the first request after a quiet spell (legacy endpoint).
        self._png_wanted_until = time.time() + 600
        if self._latest_png is None and self._latest_grid is not None:
            self._latest_png = self._render_png(self._latest_grid)
        return self._latest_png

    @property
    def latest_timestamp(self) -> Optional[str]:
        return self._latest_iso

    def _get_s3(self):
        with self._s3_lock:
            if self._s3 is None:
                import boto3
                from botocore import UNSIGNED
                from botocore.config import Config
                self._s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
            return self._s3

    def _get_cmap(self):
        """Build (once) the same NWS reflectivity colormap used by nexrad_service."""
        if self._cmap is not None:
            return self._cmap
        try:
            from matplotlib.colors import LinearSegmentedColormap

            def p(dbz: float) -> float:
                return (dbz + 20) / 100.0

            def c(r, g, b, a=0.9):
                return (r / 255.0, g / 255.0, b / 255.0, a)

            eps = 1e-4
            data = [
                (p(-20),        c(0, 0, 0, 0.0)),
                (p(-15),        c(0, 0, 0, 0.0)),
                (p(5) - eps,    c(0, 0, 0, 0.0)),
                (p(5),          c(29, 37, 60)),
                (p(17.5) - eps, c(29, 37, 60)),
                (p(17.5),       c(89, 155, 171)),
                (p(22.5) - eps, c(89, 155, 171)),
                (p(22.5),       c(33, 186, 72)),
                (p(32.5) - eps, c(33, 186, 72)),
                (p(32.5),       c(5, 101, 1)),
                (p(37.5) - eps, c(5, 101, 1)),
                (p(37.5),       c(251, 252, 0)),
                (p(42.5) - eps, c(199, 176, 0)),
                (p(42.5),       c(253, 149, 2)),
                (p(50) - eps,   c(172, 92, 2)),
                (p(50),         c(253, 38, 0)),
                (p(60) - eps,   c(135, 43, 22)),
                (p(60),         c(193, 148, 179, 0.95)),
                (p(70) - eps,   c(200, 23, 119, 0.95)),
                (p(70),         c(165, 2, 215, 0.95)),
                (p(75) - eps,   c(64, 0, 146, 0.95)),
                (p(75),         c(135, 255, 253, 1.0)),
                (p(80) - eps,   c(54, 120, 142, 1.0)),
                (p(80),         c(173, 99, 64, 1.0)),
            ]
            self._cmap = LinearSegmentedColormap.from_list("mrms_ref", data, N=512)
        except Exception as e:
            logger.warning(f"Could not build MRMS colormap: {e}")
        return self._cmap

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @staticmethod
    def _sweep_orphan_temp_files(max_age_hours: float = 2.0) -> int:
        """Delete orphaned mrms_*.grib2 temp files left by an unclean shutdown.

        Each fetch parses GRIB2 via a NamedTemporaryFile(delete=False) and
        removes it in a finally block — but a hard kill (e.g. ending a stream
        with Ctrl-C / process termination) skips the finally and orphans the
        file.  These accumulated directly in %TEMP%.  A download+parse takes
        seconds, so anything older than ``max_age_hours`` is dead and safe to
        remove regardless of which process owns it.  Returns files removed.
        """
        import glob
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).timestamp()
        removed = 0
        pattern = os.path.join(tempfile.gettempdir(), "mrms_*.grib2")
        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        if removed:
            logger.info(f"MRMS cleanup: removed {removed} orphaned temp grib2 file(s)")
        return removed

    async def start(self):
        if not _check_eccodes():
            logger.warning("MRMS service not started (eccodes unavailable — pip install eccodes)")
            return
        self._running = True
        logger.info("MRMS service starting")
        self._sweep_orphan_temp_files()
        # Background the initial GRIB fetch/decode so it doesn't block startup;
        # the poll loop refreshes every 120 s and the cache serves the first
        # composite as soon as this completes.
        async def _initial_fetch():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._fetch_sync)
        asyncio.create_task(_initial_fetch())
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self):
        while self._running:
            try:
                await asyncio.sleep(120)
                loop = asyncio.get_event_loop()
                updated = await loop.run_in_executor(None, self._fetch_sync)
                if updated and self.on_update and self._latest_iso:
                    await self.on_update(self._latest_iso)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"MRMS poll error: {e}", exc_info=True)

    # ── Core fetch + render ───────────────────────────────────────────────────

    def _fetch_sync(self) -> bool:
        """Download and render the latest MRMS file. Returns True if updated."""
        try:
            s3  = self._get_s3()
            now = datetime.now(timezone.utc)

            # S3 path format: CONUS/{Product}/YYYYMMDD/  (flat 8-digit date, no year subfolder)
            # Files are named MRMS_..._YYYYMMDD-HHMMSS.grib2.gz so alphabetical = chronological.
            # We use StartAfter to skip to ~30 min ago so we never load hundreds of old files.
            product_short = MRMS_PRODUCT.split("/")[-1]

            def _list_recent(dt: datetime) -> list:
                date_str  = dt.strftime("%Y%m%d")
                prefix    = f"{MRMS_PRODUCT}/{date_str}/"
                # Build a start-after key based on ~30 min ago so we only scan recent files
                cutoff    = dt - timedelta(minutes=30)
                cutoff_str = cutoff.strftime("%Y%m%d-%H%M%S")
                start_after = f"{prefix}MRMS_{product_short}_{cutoff_str}"
                resp = s3.list_objects_v2(
                    Bucket=MRMS_BUCKET, Prefix=prefix,
                    StartAfter=start_after, MaxKeys=50,
                )
                keys = [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".grib2.gz")]
                if not keys:
                    # Nothing in last 30 min — fall back to last 3 hours (handles gaps/outages)
                    cutoff2    = dt - timedelta(hours=3)
                    cutoff2_str = cutoff2.strftime("%Y%m%d-%H%M%S")
                    start_after2 = f"{prefix}MRMS_{product_short}_{cutoff2_str}"
                    resp2 = s3.list_objects_v2(
                        Bucket=MRMS_BUCKET, Prefix=prefix,
                        StartAfter=start_after2, MaxKeys=100,
                    )
                    keys = [o["Key"] for o in resp2.get("Contents", []) if o["Key"].endswith(".grib2.gz")]
                return keys

            keys = _list_recent(now)
            if not keys:
                keys = _list_recent(now - timedelta(days=1))
            if not keys:
                logger.warning("MRMS: no files found in bucket for today or yesterday")
                return False

            latest_key = sorted(keys)[-1]
            # Filename: MRMS_MergedReflectivityQCComposite_00.50_YYYYMMDD-HHMMSS.grib2.gz
            fname = latest_key.split("/")[-1]
            ts = fname.rsplit("_", 1)[-1].replace(".grib2.gz", "")  # YYYYMMDD-HHMMSS

            if ts == self._latest_ts:
                logger.debug("MRMS: no new data")
                return False

            logger.info(f"MRMS: downloading {latest_key}")
            obj = s3.get_object(Bucket=MRMS_BUCKET, Key=latest_key)
            grib_bytes = gzip.decompress(obj["Body"].read())

            data = self._parse_grib2(grib_bytes)
            if data is None:
                return False

            # Pack binary (primary — used by WebGL layer for native-resolution rendering)
            binary = self._pack_binary(data)

            # Legacy PNG (/api/mrms/latest.png only): the scipy reproject + PIL
            # encode is the most expensive step of this poll — render it only
            # while something has actually requested the PNG recently.
            self._latest_grid = data
            png = self._render_png(data) if time.time() < self._png_wanted_until else None

            # Parse a human-readable ISO timestamp
            try:
                dt = datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
                self._latest_iso = dt.isoformat()
            except ValueError:
                self._latest_iso = ts

            self._latest_binary = binary
            self._latest_png    = png
            self._latest_ts     = ts
            self._history.append((ts, self._latest_iso or ts, binary))
            logger.info(
                f"MRMS: updated @ {ts} "
                f"(binary {len(binary) // 1024} KB, PNG {len(png) // 1024 if png else 0} KB)"
            )
            return True

        except Exception as e:
            logger.error(f"MRMS fetch failed: {e}", exc_info=True)
            return False

    def _parse_grib2(self, grib_bytes: bytes) -> Optional[np.ndarray]:
        """Write bytes to a temp file, parse with eccodes, return data array."""
        try:
            import eccodes
        except ImportError:
            return None

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="mrms_", suffix=".grib2", delete=False
            ) as f:
                f.write(grib_bytes)
                tmp = f.name

            # eccodes is NOT thread-safe — serialize against the HRRR field
            # service and the rotation poller (see services/grib_lock.py).
            with GRIB_DECODE_LOCK, open(tmp, "rb") as f:
                msg_id = eccodes.codes_grib_new_from_file(f)
                if msg_id is None:
                    logger.warning("MRMS GRIB2: no messages found")
                    return None
                try:
                    ni = eccodes.codes_get(msg_id, "Ni")   # points along longitude
                    nj = eccodes.codes_get(msg_id, "Nj")   # points along latitude
                    values = eccodes.codes_get_values(msg_id)
                    data = values.reshape(nj, ni).astype(float)

                    # Mask missing values (typically 9999.0 in MRMS)
                    try:
                        mv = eccodes.codes_get(msg_id, "missingValue")
                        data[data >= mv * 0.9] = np.nan
                    except Exception:
                        data[data > 900] = np.nan
                finally:
                    eccodes.codes_release(msg_id)

            logger.debug(
                f"MRMS grid: {nj}×{ni}, "
                f"range {np.nanmin(data):.1f}–{np.nanmax(data):.1f} dBZ"
            )
            return data

        except Exception as e:
            logger.error(f"MRMS GRIB2 parse error: {e}", exc_info=True)
            return None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _pack_binary(self, data: np.ndarray) -> bytes:
        """Pack MRMS grid as a compact binary for the WebGL layer.

        Uses 2× spatial subsample (1750×3500) to keep the payload ~6 MB while
        still providing enough resolution for crisp rendering at any zoom level.
        The WebGL layer uses GL_LINEAR texture filtering so the GPU interpolates
        between grid points smoothly — no additional shader math needed.

        Wire format (all LE):
          [0:4]   'MRMS' magic
          [4:8]   uint32 ni  (columns, W→E)
          [8:12]  uint32 nj  (rows, N→S)
          [12:20] float64 north
          [20:28] float64 south
          [28:36] float64 west
          [36:44] float64 east
          [44:48] float32 vmin
          [48:52] float32 vmax
          [52:]   uint8[] gate values row-major; 0=no-data, 1-255=normalised
        """
        return self._pack_grid(data, MRMS_VMIN, MRMS_VMAX, 1.0, -15.0)

    def _pack_grid(self, data: np.ndarray, vmin: float, vmax: float,
                   scale: float = 1.0, nodata_below: float = -15.0) -> bytes:
        """Generalized packer (any product): 2× subsample, transform to display
        units via `scale`, normalize to uint8 over [vmin, vmax], 0 = no-data."""
        import struct

        # Subsample to ~3500 columns regardless of native resolution — most
        # products are 0.01° (7000 wide → step 2), but az-shear / rotation are
        # 0.005° (14000 wide → step 4); normalizing keeps every payload ~6 MB.
        step = max(1, round(data.shape[1] / 3500))
        sub = data[::step, ::step] * float(scale)  # subsample + raw → display units
        nj2, ni2 = sub.shape
        grid_north, grid_south, grid_west, grid_east = 54.995, 20.005, -129.995, -60.005

        span = (float(vmax) - float(vmin)) or 1.0
        norm = np.clip((sub - float(vmin)) / span, 0.0, 1.0)
        gate = (norm * 255.0).astype(np.uint8)
        invalid = ~np.isfinite(sub) | (sub < float(nodata_below))  # NaN + −999/−3 sentinels
        gate[invalid] = 0
        gate[~invalid & (gate == 0)] = 1  # bump valid-but-rounds-to-0 up to 1

        header = (
            self.BINARY_MAGIC
            + struct.pack("<II", ni2, nj2)
            + struct.pack("<dddd", grid_north, grid_south, grid_west, grid_east)
            + struct.pack("<ff", float(vmin), float(vmax))
        )
        return header + gate.tobytes()

    # ── On-demand multi-product access (everything except polled reflectivity) ──
    @staticmethod
    def _ts_of(key: str) -> str:
        return key.split("/")[-1].rsplit("_", 1)[-1].replace(".grib2.gz", "")

    @staticmethod
    def _iso_of(ts: str) -> str:
        try:
            return datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return ts

    def _list_keys(self, s3_prefix: str, lookback_min: int = 180, max_keys: int = 300) -> list[str]:
        """Recent .grib2.gz keys for a product (today, falling back to yesterday)."""
        s3 = self._get_s3()
        short = s3_prefix.split("/")[-1]
        now = datetime.now(timezone.utc)
        for day_back in (0, 1):
            d = now - timedelta(days=day_back)
            prefix = f"{s3_prefix}/{d.strftime('%Y%m%d')}/"
            kwargs = {"Bucket": MRMS_BUCKET, "Prefix": prefix, "MaxKeys": max_keys}
            if day_back == 0:
                cutoff = (now - timedelta(minutes=lookback_min)).strftime("%Y%m%d-%H%M%S")
                kwargs["StartAfter"] = f"{prefix}MRMS_{short}_{cutoff}"
            resp = s3.list_objects_v2(**kwargs)
            keys = sorted(o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".grib2.gz"))
            if keys:
                return keys
        return []

    def _fetch_pack(self, spec: dict, key: str) -> Optional[bytes]:
        raw = gzip.decompress(self._get_s3().get_object(Bucket=MRMS_BUCKET, Key=key)["Body"].read())
        data = self._parse_grib2(raw)
        if data is None:
            return None
        return self._pack_grid(data, spec["vmin"], spec["vmax"], spec.get("scale", 1.0), spec.get("nodata_below", -15.0))

    # Global frame budget: each packed frame is ~6 MB, so bounding per product
    # alone (40 frames × 20 products) allows multi-GB growth over a session.
    _PCACHE_MAX_FRAMES = 48        # ≈290 MB worst case across all products
    _PCACHE_IDLE_SEC = 30 * 60     # drop products untouched this long

    def _pc(self, pid: str) -> dict:
        """Get/create a product's cache slot. Caller must hold _pcache_lock."""
        pc = self._pcache.setdefault(
            pid, {"ts": None, "binary": None, "fetched_at": 0.0, "last_access": 0.0, "frames": OrderedDict()}
        )
        pc["last_access"] = time.time()
        return pc

    def _prune_pcache_locked(self) -> None:
        """Enforce the global budget. Caller must hold _pcache_lock."""
        now = time.time()
        for pid in [p for p, pc in self._pcache.items() if now - pc.get("last_access", 0.0) > self._PCACHE_IDLE_SEC]:
            del self._pcache[pid]
        total = sum(len(pc["frames"]) for pc in self._pcache.values())
        while total > self._PCACHE_MAX_FRAMES:
            victim = min(
                (pc for pc in self._pcache.values() if pc["frames"]),
                key=lambda pc: pc.get("last_access", 0.0),
                default=None,
            )
            if victim is None:
                break
            victim["frames"].popitem(last=False)
            total -= 1

    def get_product_binary(self, pid: str) -> Optional[bytes]:
        """Latest packed binary for a product (cached ~60 s, fetched on demand)."""
        spec = MRMS_PRODUCTS.get(pid)
        if not spec or not _check_eccodes():
            return None
        # Lock only around cache access — S3 list/fetch run unlocked.
        with self._pcache_lock:
            pc = self._pc(pid)
            cached_ts, cached_binary = pc["ts"], pc["binary"]
            if cached_binary is not None and time.time() - pc["fetched_at"] < 60:
                return cached_binary
        try:
            keys = self._list_keys(spec["s3"], lookback_min=180, max_keys=50)
            if not keys:
                return cached_binary
            key = keys[-1]
            ts = self._ts_of(key)
            if ts == cached_ts:
                with self._pcache_lock:
                    self._pc(pid)["fetched_at"] = time.time()
                return cached_binary
            binary = self._fetch_pack(spec, key)
            if binary is None:
                return cached_binary
            with self._pcache_lock:
                pc = self._pc(pid)
                pc.update(ts=ts, binary=binary, fetched_at=time.time())
                pc["frames"][ts] = binary
                self._prune_pcache_locked()
            return binary
        except Exception as e:
            logger.warning("MRMS %s latest failed: %s", pid, e)
            return cached_binary

    def get_product_frames(self, pid: str, n: int = 30) -> list[dict]:
        """Recent frame timestamps for a product (for looping), oldest→newest."""
        spec = MRMS_PRODUCTS.get(pid)
        if not spec:
            return []
        try:
            keys = self._list_keys(spec["s3"], lookback_min=240, max_keys=300)[-max(1, n):]
            return [{"ts": self._ts_of(k), "iso": self._iso_of(self._ts_of(k))} for k in keys]
        except Exception as e:
            logger.warning("MRMS %s frames failed: %s", pid, e)
            return []

    def get_product_frame(self, pid: str, ts: str) -> Optional[bytes]:
        """Packed binary for one product frame timestamp (cached on demand)."""
        spec = MRMS_PRODUCTS.get(pid)
        if not spec or not _check_eccodes():
            return None
        with self._pcache_lock:
            pc = self._pc(pid)
            if ts in pc["frames"]:
                pc["frames"].move_to_end(ts)
                return pc["frames"][ts]
        try:
            short = spec["s3"].split("/")[-1]
            key = f"{spec['s3']}/{ts.split('-')[0]}/MRMS_{short}_{ts}.grib2.gz"
            binary = self._fetch_pack(spec, key)
            if binary is not None:
                with self._pcache_lock:
                    pc = self._pc(pid)
                    pc["frames"][ts] = binary
                    self._prune_pcache_locked()
            return binary
        except Exception as e:
            logger.warning("MRMS %s frame %s failed: %s", pid, ts, e)
            return None

    def _render_png(self, data: np.ndarray) -> Optional[bytes]:
        """
        Reproject MRMS data into Web Mercator and encode as RGBA PNG.

        The MRMS CONUS grid is north→south, uniform in degrees of lat/lon.
        MapLibre's image source stretches pixels uniformly in Mercator space.
        A naive per-degree render therefore appears ~2° too far north at 45°N.

        Fix: build an output grid that is uniformly spaced in Mercator Y and use
        scipy bilinear resampling to pull values from the input.  This ensures
        every pixel in the PNG maps to the correct geographic location.
        """
        try:
            from PIL import Image
            from scipy.ndimage import map_coordinates
        except ImportError:
            logger.warning("Pillow or scipy not available — MRMS render skipped")
            return None

        try:
            cmap = self._get_cmap()
            if cmap is None:
                return None

            # Exact grid parameters verified from GRIB2 metadata (row 0 = north)
            grid_north = 54.995    # La1 — northernmost row
            grid_south = 20.005    # La2 — southernmost row
            dj         = 0.01      # degrees per row

            nj, ni = data.shape    # 3500 × 7000

            # Output dimensions — larger = sharper, but bigger PNG
            out_h, out_w = 1024, 2048

            # Mercator Y for a latitude (radians)
            def _merc(lat_deg: np.ndarray) -> np.ndarray:
                return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))

            def _lat(merc: np.ndarray) -> np.ndarray:
                return np.degrees(2 * np.arctan(np.exp(merc)) - np.pi / 2)

            # Build output rows uniformly spaced in Mercator Y (north→south)
            merc_n = _merc(grid_north)
            merc_s = _merc(grid_south)
            merc_rows = np.linspace(merc_n, merc_s, out_h)
            out_lats  = _lat(merc_rows)

            # Convert output latitudes to input row indices (0 = 54.995°N)
            in_rows = np.clip((grid_north - out_lats) / dj, 0, nj - 1)

            # Columns: longitude is linear in Mercator, so simple uniform spacing
            in_cols = np.linspace(0, ni - 1, out_w)

            # 2-D index grids
            row_grid = in_rows[:, np.newaxis] * np.ones((1, out_w))
            col_grid = np.ones((out_h, 1))    * in_cols[np.newaxis, :]

            # Bilinear resample — much smoother than nearest-neighbour subsampling
            resampled = map_coordinates(
                data, [row_grid, col_grid], order=1, mode="nearest"
            )

            # Gate threshold and normalization
            valid = np.isfinite(resampled) & (resampled >= -15.0) & (resampled <= MRMS_VMAX)
            norm  = np.clip((resampled - MRMS_VMIN) / (MRMS_VMAX - MRMS_VMIN), 0.0, 1.0)

            rgba = (cmap(norm) * 255).astype(np.uint8)
            rgba[~valid] = 0    # transparent for missing / sub-threshold

            buf = io.BytesIO()
            Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=False)
            return buf.getvalue()

        except Exception as e:
            logger.error(f"MRMS render failed: {e}", exc_info=True)
            return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_service: Optional[MRMSService] = None


def get_mrms_service() -> Optional[MRMSService]:
    return _service


async def start_mrms_service() -> bool:
    global _service
    _check_eccodes()          # trigger early import check so warning appears at startup
    _service = MRMSService()
    await _service.start()
    return _service.available


async def stop_mrms_service():
    global _service
    if _service:
        await _service.stop()
        _service = None
