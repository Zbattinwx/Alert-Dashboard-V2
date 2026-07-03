"""
CARS-program GraphQL traffic-camera adapter for Alert Dashboard V2.

Several state 511 systems ("TrafficWise"/CARS map app) share one GraphQL API:

    POST https://{base}/api/graphql   query MapFeatures(layerSlugs:["normalCameras"])

returning camera map-features with a bbox (point), a title, and CameraViews that
carry a poster image (`url`) and live HLS `sources` ({type, src}). No API key is
required, and the HLS streams are CORS-open (reflected origin), so the radar app
plays them directly with hls.js — no proxy needed. This service fetches +
normalizes the lists server-side and merges into the unified /api/cameras.

Confirmed members (probed live): CO, IN, IA, KS, MA, MN, NE. The map API
clusters cameras at low zoom, so we recursively expand each Cluster (it carries a
bbox + maxZoom) down to individual cameras and dedupe by uri.
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

# state -> (label, graphql base URL, state bbox as (south, west, north, east))
KNOWN_CARS: dict[str, tuple[str, str, tuple[float, float, float, float]]] = {
    "CO": ("COtrip", "https://maps.cotrip.org", (36.99, -109.06, 41.00, -102.04)),
    "IN": ("INDOT TrafficWise", "https://511in.org", (37.77, -88.10, 41.77, -84.78)),
    "IA": ("511IA", "https://511ia.org", (40.36, -96.64, 43.51, -90.14)),
    "KS": ("KanDrive", "https://kandrive.gov", (36.99, -102.06, 40.01, -94.58)),
    "MA": ("Mass511", "https://mass511.com", (41.23, -73.51, 42.89, -69.86)),
    "MN": ("511MN", "https://511mn.org", (43.49, -97.24, 49.39, -89.48)),
    "NE": ("511NE", "https://511.nebraska.gov", (39.99, -104.06, 43.01, -95.30)),
}

_QUERY = (
    "query MapFeatures($input: MapFeaturesArgs!) { mapFeaturesQuery(input: $input) "
    "{ mapFeatures { uri title bbox __typename "
    "... on Cluster { maxZoom } "
    "... on Camera { views(limit: 1) { category ... on CameraView { url sources { type src } } } } } "
    "error { message type } } }"
)

_START_ZOOM = 8
_MAX_ZOOM = 15
_CONCURRENCY = 8
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class CameraCARS:
    id: str
    source: str
    state: str
    location: str
    latitude: float
    longitude: float
    video_url: str = ""  # live HLS .m3u8
    image_url: str = ""  # poster snapshot
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CamerasCARSService:
    """Fetches + caches cameras from the configured CARS GraphQL states."""

    def __init__(self):
        settings = get_settings()
        cfg = getattr(settings, "cameras_cars_states", None)
        self._states: list[str] = [s for s in (cfg or list(KNOWN_CARS)) if s in KNOWN_CARS]
        self._ttl = timedelta(seconds=getattr(settings, "cameras_cars_cache_ttl_seconds", 900))
        self._cameras: list[CameraCARS] = []
        self._cache_time: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._last_forced = 0.0  # monotonic ts of the last honored force_refresh

    def _cache_valid(self) -> bool:
        if not self._cache_time:
            return False
        return datetime.now(timezone.utc) - self._cache_time < self._ttl

    async def fetch_all(self, force_refresh: bool = False) -> list[CameraCARS]:
        # ?refresh=true is client-triggerable and re-runs the recursive cluster
        # expansion (~4400 cams, 7 states) — demote repeated forces (≥120 s apart).
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
            if not self._states:
                self._cameras = []
                self._cache_time = datetime.now(timezone.utc)
                return self._cameras

            results: list[CameraCARS] = []
            timeout = aiohttp.ClientTimeout(total=25)
            headers = {"Content-Type": "application/json", "User-Agent": _UA}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                await asyncio.gather(
                    *[self._fetch_state(session, code, results) for code in self._states],
                    return_exceptions=True,
                )

            self._cameras = results
            self._cache_time = datetime.now(timezone.utc)
            logger.info(f"Fetched {len(results)} CARS cameras across {len(self._states)} state(s)")
            return results

    async def _fetch_state(self, session: aiohttp.ClientSession, code: str, out: list[CameraCARS]):
        label, base, (s, w, n, e) = KNOWN_CARS[code]
        sem = asyncio.Semaphore(_CONCURRENCY)
        seen: set[str] = set()
        try:
            await self._walk(session, sem, label, code, base, n, s, e, w, _START_ZOOM, out, seen)
            logger.info(f"CARS {label}: {len(seen)} cameras")
        except Exception as ex:  # one state failing must not sink the rest
            logger.warning(f"CARS {label} fetch error: {ex}")

    async def _walk(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        label: str,
        code: str,
        base: str,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom: int,
        out: list[CameraCARS],
        seen: set[str],
    ):
        features = await self._query(session, sem, base, north, south, east, west, zoom)
        clusters: list[tuple[float, float, float, float, int]] = []
        for f in features:
            tn = f.get("__typename")
            if tn == "Camera":
                cam = self._to_camera(label, code, f)
                if cam and cam.id not in seen:
                    seen.add(cam.id)
                    out.append(cam)
            elif tn == "Cluster" and zoom < _MAX_ZOOM:
                bb = f.get("bbox")  # [west, south, east, north]
                if bb and len(bb) >= 4:
                    nz = min((f.get("maxZoom") or zoom) + 1, _MAX_ZOOM)
                    if nz <= zoom:
                        nz = zoom + 1
                    clusters.append((bb[3], bb[1], bb[2], bb[0], nz))
        if clusters:
            await asyncio.gather(
                *[
                    self._walk(session, sem, label, code, base, cn, cs, ce, cw, cz, out, seen)
                    for (cn, cs, ce, cw, cz) in clusters
                ],
                return_exceptions=True,
            )

    async def _query(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        base: str,
        north: float,
        south: float,
        east: float,
        west: float,
        zoom: int,
    ) -> list[dict]:
        body = {
            "query": _QUERY,
            "variables": {
                "input": {
                    "north": north,
                    "south": south,
                    "east": east,
                    "west": west,
                    "zoom": zoom,
                    "layerSlugs": ["normalCameras"],
                    "nonClusterableUris": ["dashboard"],
                }
            },
        }
        url = f"{base}/api/graphql"
        async with sem:
            try:
                async with session.post(url, json=body, headers={"Origin": base}) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
            except Exception:
                return []
        mf = (((data.get("data") or {}).get("mapFeaturesQuery") or {}).get("mapFeatures"))
        return mf or []

    def _to_camera(self, label: str, code: str, f: dict) -> Optional[CameraCARS]:
        bb = f.get("bbox")
        if not bb or len(bb) < 4:
            return None
        lon = (float(bb[0]) + float(bb[2])) / 2.0
        lat = (float(bb[1]) + float(bb[3])) / 2.0

        views = f.get("views") or []
        view = views[0] if views else {}
        video = ""
        for src in view.get("sources") or []:
            s = (src.get("src") or "").strip()
            if ".m3u8" in s or "mpegurl" in (src.get("type") or "").lower():
                video = s
                break
        poster = (view.get("url") or "").strip()
        if not poster.startswith("http"):  # skip relative icon/placeholder posters
            poster = ""
        if not (video or poster):
            return None

        uri = f.get("uri") or ""
        return CameraCARS(
            id=f"{code.lower()}-{uri.replace('/', '-') or lat}",
            source=label,
            state=code,
            location=f.get("title") or "Traffic Camera",
            latitude=lat,
            longitude=lon,
            video_url=video,
            image_url=poster,
        )


# =============================================================================
# Singleton (lazy — self-caches on first request, no lifespan wiring needed)
# =============================================================================

_service: Optional[CamerasCARSService] = None


def get_cars_service() -> CamerasCARSService:
    global _service
    if _service is None:
        _service = CamerasCARSService()
    return _service
