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
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np

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
        self._cmap = None           # matplotlib colormap, built lazily
        self._latest_binary: Optional[bytes] = None
        self._latest_png:    Optional[bytes] = None
        self._latest_ts:     Optional[str]   = None  # YYYYMMDD-HHMMSS
        self._latest_iso:    Optional[str]   = None
        # Rolling 60-min history: deque of (ts_str, iso_str, binary_bytes)
        self._history: deque = deque(maxlen=30)
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
        return self._latest_png

    @property
    def latest_timestamp(self) -> Optional[str]:
        return self._latest_iso

    def _get_s3(self):
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

            # Also render PNG (fallback / legacy image source)
            png = self._render_png(data)

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

            with open(tmp, "rb") as f:
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
        import struct

        # 2× subsample; keep row 0 = northernmost (La1=54.995°N)
        sub = data[::2, ::2]
        nj2, ni2 = sub.shape

        # Compute the correct lat/lon bounds AFTER subsampling
        grid_north = 54.995
        grid_south = 20.005
        grid_west  = -129.995
        grid_east  = -60.005

        # Normalise to uint8 (same sentinel-0 convention as NEXRAD)
        vmin, vmax = float(MRMS_VMIN), float(MRMS_VMAX)
        span = vmax - vmin
        norm = np.clip((sub - vmin) / span, 0.0, 1.0)
        gate = (norm * 255.0).astype(np.uint8)
        # Sentinel: missing values (-999 or NaN) → 0
        gate[~np.isfinite(sub) | (sub < -15.0)] = 0
        # Bump valid values that round to 0 up to 1
        valid = np.isfinite(sub) & (sub >= -15.0)
        gate[valid & (gate == 0)] = 1

        header = (
            self.BINARY_MAGIC
            + struct.pack("<II", ni2, nj2)
            + struct.pack("<dddd", grid_north, grid_south, grid_west, grid_east)
            + struct.pack("<ff", vmin, vmax)
        )
        return header + gate.tobytes()

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
