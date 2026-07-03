"""
511-family traffic camera adapter for Alert Dashboard V2.

Many state 511 systems share one REST contract:

    GET {base}/api/GetCameras?key={key}&format=json

returning camera objects with Latitude/Longitude, Name, RoadwayName, a page
Url, and a VideoUrl (live HLS .m3u8). Those HLS streams are CORS-open, so the
radar app plays them directly with hls.js — no proxy is needed here. This
service only fetches + normalizes the lists server-side (holding the per-state
API keys) and merges with OHGO via the unified /api/cameras endpoint.

Each state needs its own free key (register e.g. at https://511ny.org/developers
or the equivalent 511 developer page). Set them in
settings.cameras_511_keys, e.g. {"NY": "<key>", "WI": "<key>"} — or via the
CAMERAS_511_KEYS env var as JSON.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)

# Known 511 GetCameras-family jurisdictions: state code -> (label, base URL).
# Add more here as you obtain keys; the schema is shared across the family.
KNOWN_511: dict[str, tuple[str, str]] = {
    "NY": ("511NY", "https://511ny.org"),
    "WI": ("511WI", "https://511wi.gov"),
    "GA": ("511GA", "https://511ga.org"),
    "LA": ("511LA", "https://www.511la.org"),
}


@dataclass
class Camera511:
    """A 511 camera normalized to the unified camera shape (live HLS video)."""

    id: str
    source: str
    state: str
    location: str
    latitude: float
    longitude: float
    video_url: str
    image_url: str = ""  # 511 lists give a page Url, not a still — video-only
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Cameras511Service:
    """Fetches + caches camera lists from the configured 511 states."""

    def __init__(self):
        settings = get_settings()
        self._keys: dict[str, str] = dict(getattr(settings, "cameras_511_keys", {}) or {})
        self._ttl = timedelta(seconds=getattr(settings, "cameras_511_cache_ttl_seconds", 300))
        self._cameras: list[Camera511] = []
        self._cache_time: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._last_forced = 0.0  # monotonic ts of the last honored force_refresh

    def _cache_valid(self) -> bool:
        if not self._cache_time:
            return False
        return datetime.now(timezone.utc) - self._cache_time < self._ttl

    async def fetch_all(self, force_refresh: bool = False) -> list[Camera511]:
        # ?refresh=true is client-triggerable and fans out to every configured
        # state API — demote repeated forces to cached reads (≥120 s apart).
        if force_refresh:
            now = time.monotonic()
            if now - self._last_forced < 120.0:
                force_refresh = False
            else:
                self._last_forced = now
        if not force_refresh and self._cache_valid():
            return self._cameras

        async with self._lock:
            if not force_refresh and self._cache_valid():
                return self._cameras

            states = [
                (code, KNOWN_511[code][0], KNOWN_511[code][1], key)
                for code, key in self._keys.items()
                if key and code in KNOWN_511
            ]
            if not states:
                # No keys configured — nothing to fetch (OHGO still flows separately).
                self._cameras = []
                self._cache_time = datetime.now(timezone.utc)
                return self._cameras

            results: list[Camera511] = []
            async with aiohttp.ClientSession() as session:

                async def fetch_one(code: str, label: str, base: str, key: str) -> list[Camera511]:
                    url = f"{base}/api/GetCameras?key={key}&format=json"
                    try:
                        async with session.get(url, timeout=30) as resp:
                            if resp.status != 200:
                                logger.error(f"511 {label} cameras returned status {resp.status}")
                                return []
                            data = await resp.json(content_type=None)
                        return self._parse(code, label, data)
                    except asyncio.TimeoutError:
                        logger.error(f"Timeout fetching 511 {label} cameras")
                        return []
                    except Exception as e:
                        logger.warning(f"Error fetching 511 {label} cameras: {e}")
                        return []

                chunks = await asyncio.gather(*[fetch_one(c, l, b, k) for (c, l, b, k) in states])

            for chunk in chunks:
                results.extend(chunk)

            self._cameras = results
            self._cache_time = datetime.now(timezone.utc)
            logger.info(f"Fetched {len(results)} 511 cameras across {len(states)} state(s)")
            return results

    def _parse(self, code: str, label: str, data: Any) -> list[Camera511]:
        # The family returns a JSON array; tolerate a wrapped shape too.
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("cameras") or data.get("results") or []
        else:
            items = []

        cameras: list[Camera511] = []
        for item in items:
            try:
                if item.get("Disabled") or item.get("Blocked"):
                    continue
                video = (item.get("VideoUrl") or "").strip()
                if not video:
                    continue  # Phase 2 = live-video cameras only
                lat = float(item.get("Latitude"))
                lon = float(item.get("Longitude"))
                name = item.get("Name") or item.get("RoadwayName") or "Traffic Camera"
                cameras.append(
                    Camera511(
                        id=f"{code.lower()}-{item.get('ID', '')}",
                        source=label,
                        state=code,
                        location=str(name),
                        latitude=lat,
                        longitude=lon,
                        video_url=video,
                        description=item.get("RoadwayName") or "",
                    )
                )
            except Exception as e:
                logger.debug(f"511 {label} skip camera: {e}")
                continue

        return cameras


# =============================================================================
# Singleton (lazy — no lifespan wiring needed; self-caches on first request)
# =============================================================================

_service: Optional[Cameras511Service] = None


def get_511_service() -> Cameras511Service:
    global _service
    if _service is None:
        _service = Cameras511Service()
    return _service
