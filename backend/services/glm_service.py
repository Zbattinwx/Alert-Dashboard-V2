"""
GOES-East Geostationary Lightning Mapper (GLM) service.

Downloads near-realtime lightning flash data from NOAA's open AWS S3 bucket
(noaa-goes19 — GOES-19, the operational GOES-East at 75.2°W since 2025-04-04,
replacing GOES-16). GLM detects optical lightning (IC + CG) across CONUS with
~20 second latency and ~8 km spatial resolution.

**Satellite choice matters for detection efficiency.** GLM's DE falls off toward
the edge of the satellite's disk. GOES-East puts the eastern/central US (our
Ohio-based coverage) near nadir (~40° earth-central angle → good DE), whereas
GOES-West (noaa-goes18, 137.2°W) sees Ohio ~63° off-nadir near the edge of the
usable field, missing most flashes there — measured live, GOES-West saw 0 flashes
over Dayton in a 2-min window where GOES-East saw 14 (and 4× more across the
eastern US). Use GOES-West only for a Pacific/far-west-focused deployment.

Maintains a rolling 60-minute window of flash positions + energies. Each poll
processes EVERY new granule since the last (GLM emits one file per ~20 s), not
just the newest. Broadcasts new flashes via WebSocket and exposes flash-rate data
for storm cell scoring.
"""

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# AWS S3 bucket for GOES-East GLM data (public, no credentials needed). GOES-19
# is the operational GOES-East (75.2°W); it covers the eastern/central US (our
# audience) near nadir. noaa-goes18 (GOES-West, 137.2°W) badly under-detects
# lightning over the eastern US — see the module docstring.
GLM_BUCKET = "noaa-goes19"
GLM_PREFIX = "GLM-L2-LCFA"   # Lightning Cluster-Filter Algorithm Level 2

# How many minutes of flashes to keep in the rolling window. 60 so the radar
# app's snapshot + "fade over 1 hr" / loop-with-radar options have history.
FLASH_WINDOW_MINUTES = 60
# Hard cap on retained flashes (bounds memory in extreme convection ~24 MB).
FLASH_MAX = 400_000

# Poll interval (GLM files are ~20s cadence, we poll every 30s)
POLL_INTERVAL_SECONDS = 30

# Minimum flash energy to include (filters sensor noise); typical flash ~1e-14 to 1e-11 J
MIN_FLASH_ENERGY = 1e-15


@dataclass
class LightningFlash:
    """A single detected lightning flash from GOES-16 GLM."""
    lat: float
    lon: float
    energy: float        # Optical radiant energy in Joules
    timestamp: str       # ISO-8601 UTC
    epoch: float         # Unix timestamp for fast age checks


class GLMService:
    """
    Polls GOES-16 GLM L2 LCFA files from AWS S3 and maintains a rolling
    15-minute window of lightning flash positions.
    """

    def __init__(self):
        self._flashes: deque[LightningFlash] = deque(maxlen=FLASH_MAX)
        self._last_file_key: Optional[str] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.on_new_flashes: Optional[Callable] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_recent_flashes(self, max_age_minutes: int = FLASH_WINDOW_MINUTES) -> list[LightningFlash]:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_minutes * 60
        return [f for f in self._flashes if f.epoch >= cutoff]

    def flash_rate_near(self, lat: float, lon: float,
                        radius_km: float = 25.0, window_minutes: int = 5) -> float:
        """Flashes per minute within radius_km over the last window_minutes."""
        cutoff = datetime.now(timezone.utc).timestamp() - window_minutes * 60
        count = sum(
            1 for f in self._flashes
            if f.epoch >= cutoff and _haversine_km(lat, lon, f.lat, f.lon) <= radius_km
        )
        return count / window_minutes

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("GLM lightning service started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("GLM lightning service stopped")

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        while self._running:
            try:
                await self._fetch_latest()
            except Exception as e:
                logger.warning(f"GLM poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _fetch_latest(self):
        loop = asyncio.get_event_loop()
        new_flashes = await loop.run_in_executor(None, self._fetch_sync)
        if new_flashes:
            self._add_flashes(new_flashes)
            if self.on_new_flashes:
                await self.on_new_flashes(new_flashes)

    def _fetch_sync(self) -> list[LightningFlash]:
        """Synchronous S3 fetch + netCDF4 parse (runs in executor thread)."""
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config

            s3 = boto3.client(
                "s3",
                config=Config(signature_version=UNSIGNED),
                region_name="us-east-1",
            )

            # GLM files: GLM-L2-LCFA/YYYY/DDD/HH/OR_GLM-L2-LCFA_G16_s...nc
            # Files are produced every ~20s so an hour has ~180 objects.
            # List with a suffix marker so we only fetch the last few objects,
            # avoiding a full bucket listing.
            # Gather granule keys across the current + previous UTC hour (a poll can
            # straddle an hour boundary). Keys embed the scan time, so lexical order
            # is chronological.
            now = datetime.now(timezone.utc)
            all_keys: list[str] = []
            for hour_offset in (-1, 0):
                t = now + timedelta(hours=hour_offset)
                doy = t.timetuple().tm_yday
                prefix = f"{GLM_PREFIX}/{t.year}/{doy:03d}/{t.hour:02d}/"
                try:
                    resp = s3.list_objects_v2(Bucket=GLM_BUCKET, Prefix=prefix, MaxKeys=300)
                    all_keys += [o["Key"] for o in resp.get("Contents", [])]
                except Exception as e:
                    logger.debug(f"GLM list error for hour offset {hour_offset}: {e}")
            all_keys.sort()

            if not all_keys:
                logger.debug("GLM: no files found in the last 2 hours")
                return []

            # Process EVERY new granule since the last poll — GLM emits a file per
            # ~20 s, so grabbing only the newest each 30 s poll dropped ~1/3 of
            # flashes. First run: seed with just the latest few (don't backfill a
            # whole hour on startup).
            if self._last_file_key is None:
                new_keys = all_keys[-3:]
            else:
                new_keys = [k for k in all_keys if k > self._last_file_key]
            if not new_keys:
                return []   # nothing new since last poll
            # Bound the work if we ever fall behind (e.g. after a stall/restart).
            MAX_GRANULES = 12
            if len(new_keys) > MAX_GRANULES:
                new_keys = new_keys[-MAX_GRANULES:]

            flashes: list[LightningFlash] = []
            for key in new_keys:
                try:
                    data = s3.get_object(Bucket=GLM_BUCKET, Key=key)["Body"].read()
                    flashes += self._parse_nc(data, key)
                except Exception as e:
                    logger.debug(f"GLM fetch/parse error {key.split('/')[-1]}: {e}")
            self._last_file_key = new_keys[-1]
            logger.info(f"GLM: {len(new_keys)} granule(s) → {len(flashes)} flashes")
            return flashes

        except ImportError as e:
            logger.warning(f"GLM requires boto3 and netCDF4: {e}")
            return []
        except Exception as e:
            logger.warning(f"GLM fetch error: {e}", exc_info=True)
            return []

    def _parse_nc(self, data: bytes, key: str) -> list[LightningFlash]:
        """Parse a GLM L2 LCFA netCDF4 file from in-memory bytes."""
        try:
            import netCDF4 as nc

            # mode="r" is required when using the memory= parameter
            ds = nc.Dataset("inmemory.nc", mode="r", memory=data)
            try:
                if "flash_lat" not in ds.variables:
                    logger.warning("GLM: flash_lat not in dataset variables")
                    return []

                lats = ds.variables["flash_lat"][:].data.astype(float)
                lons = ds.variables["flash_lon"][:].data.astype(float)
                energies = ds.variables["flash_energy"][:].data.astype(float)

                # Use per-flash time if available, otherwise derive from filename
                if "flash_time_offset_of_first_event" in ds.variables:
                    # Time is seconds offset from product_time (dataset attribute)
                    time_var = ds.variables["flash_time_offset_of_first_event"]
                    offsets = time_var[:].data.astype(float)
                    # product_time is seconds since 2000-01-01 12:00:00 UTC (J2000)
                    j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
                    if hasattr(ds, "time_coverage_start"):
                        try:
                            base_dt = datetime.fromisoformat(
                                ds.time_coverage_start.replace("Z", "+00:00")
                            )
                        except Exception:
                            base_dt = j2000
                    else:
                        base_dt = j2000
                    epochs = [base_dt.timestamp() + float(o) for o in offsets]
                    timestamps = [
                        datetime.fromtimestamp(e, tz=timezone.utc).isoformat()
                        for e in epochs
                    ]
                else:
                    # Fall back to filename timestamp
                    fname = key.split("/")[-1]
                    parts = fname.split("_")
                    start_str = next((p for p in parts if p.startswith("s")), None)
                    if start_str:
                        s = start_str[1:]
                        year = int(s[0:4])
                        doy  = int(s[4:7])
                        hour = int(s[7:9])
                        minute = int(s[9:11])
                        second = int(s[11:13])
                        base_dt = (
                            datetime(year, 1, 1, tzinfo=timezone.utc)
                            + timedelta(days=doy - 1, hours=hour, minutes=minute, seconds=second)
                        )
                    else:
                        base_dt = datetime.now(timezone.utc)
                    ep = base_dt.timestamp()
                    epochs = [ep] * len(lats)
                    ts_iso = base_dt.isoformat()
                    timestamps = [ts_iso] * len(lats)

                flashes = []
                for lat, lon, energy, epoch, ts in zip(lats, lons, energies, epochs, timestamps):
                    if float(energy) < MIN_FLASH_ENERGY:
                        continue
                    if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
                        continue
                    flashes.append(LightningFlash(
                        lat=float(lat),
                        lon=float(lon),
                        energy=float(energy),
                        timestamp=ts,
                        epoch=float(epoch),
                    ))
                return flashes

            finally:
                ds.close()

        except Exception as e:
            logger.warning(f"GLM parse error: {e}", exc_info=True)
            return []

    def _add_flashes(self, new_flashes: list[LightningFlash]):
        cutoff = datetime.now(timezone.utc).timestamp() - FLASH_WINDOW_MINUTES * 60
        self._flashes.extend(new_flashes)
        while self._flashes and self._flashes[0].epoch < cutoff:
            self._flashes.popleft()


# ── Haversine ─────────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Singleton ─────────────────────────────────────────────────────────────────

_service: Optional[GLMService] = None


def get_glm_service() -> Optional[GLMService]:
    return _service


async def start_glm_service() -> bool:
    global _service
    try:
        import boto3      # noqa: F401
        import netCDF4    # noqa: F401
    except ImportError as e:
        logger.warning(
            f"GLM service unavailable — missing dependency: {e}. "
            "Install with: pip install boto3 netCDF4"
        )
        return False

    _service = GLMService()
    await _service.start()
    return True


async def stop_glm_service():
    global _service
    if _service:
        await _service.stop()
        _service = None
