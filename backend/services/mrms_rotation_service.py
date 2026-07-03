"""
MRMS Rotation Tracks + Azimuthal Shear ingester.

Polls the public AWS S3 bucket `noaa-mrms-pds` for two products and keeps the
latest CONUS grid for each in memory.  The values are sampled at storm cell
lat/lon at training-collection time and at ML inference time, so the rotation
classifier can use multi-radar fused rotation signals on top of its
single-radar features.

Products:
  - **RotationTrack30min** (a.k.a. `RotationTrack0to2kmAGL_30min`).  A 30-minute
    running max of the 0–2 km AGL azimuthal shear.  Captures peak low-level
    rotation in the recent past at each grid cell — strong "this place was
    rotating" signal even when the current scan happens to not show it.
  - **MergedAzShear0to2kmAGL**.  Instantaneous multi-radar 0–2 km azimuthal
    shear.  Same physical quantity as our single-radar LLSD but fused across
    every NEXRAD site that can see the gate, so it's less prone to beam
    blockage / cone-of-silence misses.

Both are GRIB2, gzipped, on the standard MRMS CONUS 0.01° grid
(3500 × 7000 cells covering 20–55°N × -130 to -60°W), republished about every
2 minutes.  Reference: https://www.nssl.noaa.gov/projects/mrms/operational/tables.php

Requires the `eccodes` Python package (same dep as the composite MRMS service).
"""

import asyncio
import gzip
import io
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from .grib_lock import GRIB_DECODE_LOCK

logger = logging.getLogger(__name__)


MRMS_BUCKET = "noaa-mrms-pds"
# Product paths within the bucket.  The folder names are the exact GRIB2
# message short names, and files within are MRMS_<short>_<YYYYMMDD-HHMMSS>.grib2.gz
PRODUCT_ROTATION_TRACK = "CONUS/RotationTrackML1440min_00.50"  # noqa: E501  (fallback if 30 min missing)
PRODUCT_ROTATION_TRACK_PRIMARY = "CONUS/RotationTrack30min_00.50"
PRODUCT_AZSHEAR = "CONUS/MergedAzShear_0-2kmAGL_00.50"

# CONUS MRMS grid bounds (degrees).  Match the existing mrms_service constants
# — these are the same product family.
GRID_NORTH = 55.005
GRID_SOUTH = 20.005
GRID_WEST  = -129.995
GRID_EAST  = -60.005


_eccodes_ok: Optional[bool] = None


def _check_eccodes() -> bool:
    global _eccodes_ok
    if _eccodes_ok is None:
        try:
            import eccodes  # noqa: F401
            _eccodes_ok = True
        except ImportError:
            logger.warning(
                "eccodes not found — MRMS rotation feature is disabled. "
                "To enable: pip install eccodes"
            )
            _eccodes_ok = False
    return _eccodes_ok


class MRMSRotationService:
    """Maintains the latest MRMS rotation track + azimuthal shear grid in memory.

    Public API:
      - `available` — service running and has data
      - `get_rotation_track_at(lat, lon)` — float (per-cell value) or None
      - `get_azshear_at(lat, lon)` — float or None
      - `fetch_grid_at_time(product, target_dt)` — historical fetch for backfill
    """

    def __init__(self):
        self._s3 = None
        # Per-product cache: {product_path: (grid_array, ni, nj, ts_iso)}
        self._cache: dict[str, tuple[np.ndarray, int, int, str]] = {}
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        # Track which rotation product we successfully fetched last — when the
        # 30-min product is unavailable for a date we fall back to ML1440min.
        self._active_rotation_product: Optional[str] = None

    # ─── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not _check_eccodes():
            logger.warning(
                "MRMS rotation service not started (eccodes unavailable)"
            )
            return
        self._running = True
        logger.info("MRMS rotation service starting")
        # Background the initial GRIB fetch so it doesn't block startup; the poll
        # loop keeps the grids fresh and sampling returns None/0.0 until ready.
        async def _initial_fetch():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._poll_all_sync)
        asyncio.create_task(_initial_fetch())
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("MRMS rotation service stopped")

    @property
    def available(self) -> bool:
        return _check_eccodes() and bool(self._cache)

    # ─── public sampling API ──────────────────────────────────────────────
    def get_rotation_track_at(self, lat: float, lon: float) -> Optional[float]:
        """Sample the cached 30-min (or fallback) rotation track at lat/lon.

        Returns the per-cell value in units of /s (peak azimuthal shear over
        the 30-min window), or None if out of grid / no data / not yet loaded.
        """
        product = self._active_rotation_product or PRODUCT_ROTATION_TRACK_PRIMARY
        return self._sample_cached(product, lat, lon)

    def get_azshear_at(self, lat: float, lon: float) -> Optional[float]:
        return self._sample_cached(PRODUCT_AZSHEAR, lat, lon)

    def _sample_cached(
        self, product: str, lat: float, lon: float
    ) -> Optional[float]:
        entry = self._cache.get(product)
        if entry is None:
            return None
        grid, ni, nj, _ = entry
        return self._sample_grid(grid, ni, nj, lat, lon)

    @staticmethod
    def _sample_grid(
        grid: np.ndarray, ni: int, nj: int, lat: float, lon: float
    ) -> Optional[float]:
        """Nearest-neighbour sample on the standard MRMS CONUS grid.

        Grid row 0 is the NORTHERNMOST row (lat = GRID_NORTH); column 0 is
        the westernmost (lon = GRID_WEST).
        """
        if not (GRID_SOUTH <= lat <= GRID_NORTH and GRID_WEST <= lon <= GRID_EAST):
            return None
        lat_span = GRID_NORTH - GRID_SOUTH
        lon_span = GRID_EAST - GRID_WEST
        # 0-indexed cell positions (rounded to nearest)
        row = int(round((GRID_NORTH - lat) / lat_span * (nj - 1)))
        col = int(round((lon - GRID_WEST) / lon_span * (ni - 1)))
        row = max(0, min(nj - 1, row))
        col = max(0, min(ni - 1, col))
        v = float(grid[row, col])
        if not np.isfinite(v):
            return None
        return v

    # ─── polling ──────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(120)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._poll_all_sync)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"MRMS rotation poll error: {e}", exc_info=True,
                )

    def _poll_all_sync(self) -> None:
        """Refresh all tracked products."""
        # Rotation track: prefer the 30-min product, fall back if missing
        if not self._fetch_latest_sync(PRODUCT_ROTATION_TRACK_PRIMARY):
            if self._fetch_latest_sync(PRODUCT_ROTATION_TRACK):
                self._active_rotation_product = PRODUCT_ROTATION_TRACK
        else:
            self._active_rotation_product = PRODUCT_ROTATION_TRACK_PRIMARY

        self._fetch_latest_sync(PRODUCT_AZSHEAR)

    # ─── S3 + GRIB2 ───────────────────────────────────────────────────────
    def _get_s3(self):
        if self._s3 is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
            self._s3 = boto3.client(
                "s3", config=Config(signature_version=UNSIGNED),
            )
        return self._s3

    def _fetch_latest_sync(self, product: str) -> bool:
        """Download the latest file for the product, parse, cache.  True on update."""
        try:
            s3 = self._get_s3()
            now = datetime.now(timezone.utc)
            key = self._find_latest_key(s3, product, now)
            if key is None:
                key = self._find_latest_key(s3, product, now - timedelta(days=1))
            if key is None:
                logger.debug(f"MRMS rotation: no recent files for {product}")
                return False

            # Skip if we already have this exact timestamp
            ts = key.split("/")[-1].rsplit("_", 1)[-1].replace(".grib2.gz", "")
            cached = self._cache.get(product)
            if cached and cached[3] == ts:
                return False

            logger.info(f"MRMS rotation: downloading {key}")
            obj = s3.get_object(Bucket=MRMS_BUCKET, Key=key)
            grib_bytes = gzip.decompress(obj["Body"].read())
            parsed = self._parse_grib2(grib_bytes)
            if parsed is None:
                return False
            grid, ni, nj = parsed
            self._cache[product] = (grid, ni, nj, ts)
            try:
                dt = datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
                age_min = (now - dt).total_seconds() / 60.0
                logger.info(
                    f"MRMS rotation: cached {product} @ {ts} "
                    f"({nj}x{ni}, age {age_min:.1f} min)"
                )
            except ValueError:
                pass
            return True

        except Exception as e:
            logger.error(
                f"MRMS rotation fetch failed for {product}: {e}", exc_info=True,
            )
            return False

    def _find_latest_key(self, s3, product: str, ref_dt: datetime) -> Optional[str]:
        """List the bucket for files in the date partition of `ref_dt`."""
        product_short = product.split("/")[-1]
        date_str = ref_dt.strftime("%Y%m%d")
        prefix = f"{product}/{date_str}/"
        # Look back 30 min from ref_dt for the latest file, with 3 hr fallback
        for lookback in (30, 180):
            cutoff = ref_dt - timedelta(minutes=lookback)
            start_after = f"{prefix}MRMS_{product_short}_{cutoff.strftime('%Y%m%d-%H%M%S')}"
            try:
                resp = s3.list_objects_v2(
                    Bucket=MRMS_BUCKET, Prefix=prefix,
                    StartAfter=start_after, MaxKeys=200,
                )
            except Exception:
                return None
            keys = [
                o["Key"] for o in resp.get("Contents", [])
                if o["Key"].endswith(".grib2.gz")
            ]
            if keys:
                return sorted(keys)[-1]
        return None

    @staticmethod
    def _parse_grib2(grib_bytes: bytes) -> Optional[tuple[np.ndarray, int, int]]:
        try:
            import eccodes
        except ImportError:
            return None
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="mrms_rot_", suffix=".grib2", delete=False,
            ) as f:
                f.write(grib_bytes)
                tmp = f.name
            # eccodes is NOT thread-safe — serialize against the MRMS poller and
            # the HRRR field service (see services/grib_lock.py).
            with GRIB_DECODE_LOCK, open(tmp, "rb") as f:
                msg_id = eccodes.codes_grib_new_from_file(f)
                if msg_id is None:
                    return None
                try:
                    ni = int(eccodes.codes_get(msg_id, "Ni"))
                    nj = int(eccodes.codes_get(msg_id, "Nj"))
                    values = eccodes.codes_get_values(msg_id)
                    data = values.reshape(nj, ni).astype(float)
                    try:
                        mv = eccodes.codes_get(msg_id, "missingValue")
                        data[data >= mv * 0.9] = np.nan
                    except Exception:
                        data[data > 900] = np.nan
                    # MRMS publishes missing/no-data as -999 sometimes
                    data[data <= -900] = np.nan
                finally:
                    eccodes.codes_release(msg_id)
            return data, ni, nj
        except Exception as e:
            logger.error(f"MRMS rotation GRIB2 parse error: {e}", exc_info=True)
            return None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ─── backfill API ─────────────────────────────────────────────────────
    def fetch_grid_at_time(
        self, product: str, target_dt: datetime,
    ) -> Optional[tuple[np.ndarray, int, int, str]]:
        """Find and parse the MRMS file nearest in time to `target_dt`.

        Used by the backfill script to enrich historical training rows.
        Returns (grid, ni, nj, ts_str) or None on miss.
        """
        if not _check_eccodes():
            return None
        try:
            s3 = self._get_s3()
            product_short = product.split("/")[-1]
            date_str = target_dt.strftime("%Y%m%d")
            prefix = f"{product}/{date_str}/"
            # Window: ±30 min from target
            start = target_dt - timedelta(minutes=30)
            end = target_dt + timedelta(minutes=30)
            start_after = f"{prefix}MRMS_{product_short}_{start.strftime('%Y%m%d-%H%M%S')}"
            resp = s3.list_objects_v2(
                Bucket=MRMS_BUCKET, Prefix=prefix,
                StartAfter=start_after, MaxKeys=200,
            )
            keys = [
                o["Key"] for o in resp.get("Contents", [])
                if o["Key"].endswith(".grib2.gz")
            ]
            if not keys:
                return None
            # Pick nearest in time
            def _key_ts(k: str) -> datetime:
                ts = k.split("/")[-1].rsplit("_", 1)[-1].replace(".grib2.gz", "")
                return datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            keys.sort(key=lambda k: abs((_key_ts(k) - target_dt).total_seconds()))
            best = keys[0]
            best_ts = _key_ts(best)
            if abs((best_ts - target_dt).total_seconds()) > 1800:
                return None  # no file within ±30 min
            obj = s3.get_object(Bucket=MRMS_BUCKET, Key=best)
            grib_bytes = gzip.decompress(obj["Body"].read())
            parsed = self._parse_grib2(grib_bytes)
            if parsed is None:
                return None
            grid, ni, nj = parsed
            return grid, ni, nj, best_ts.isoformat()
        except Exception as e:
            logger.warning(
                f"MRMS rotation backfill fetch failed for {product} @ {target_dt}: {e}"
            )
            return None


# ───────────────────────────────────────────────────────────────────────────
# Singleton
# ───────────────────────────────────────────────────────────────────────────

_service: Optional[MRMSRotationService] = None


def get_mrms_rotation_service() -> Optional[MRMSRotationService]:
    return _service


async def start_mrms_rotation_service() -> Optional[MRMSRotationService]:
    global _service
    if _service is None:
        _service = MRMSRotationService()
        await _service.start()
    return _service


async def stop_mrms_rotation_service() -> None:
    global _service
    if _service is not None:
        await _service.stop()
        _service = None
