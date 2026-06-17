"""
OSM Impact Service.

Given a warning polygon, queries the OpenStreetMap Overpass API for the
populated places and vulnerable facilities that fall *inside* the polygon, so
the broadcast can call out exactly what's in the path of a warning:

  - Populated places   (cities, towns, villages, hamlets)
  - Mobile home / RV parks   (the most tornado-vulnerable land use)
  - Schools & daycares       (high occupancy, often no shelter)
  - Hospitals & care homes   (hard-to-evacuate populations)

Overpass supports a native polygon filter (`poly:"lat lon lat lon ..."`), so we
pass the alert polygon directly — no bounding-box approximation. Results are
cached per product_id (the polygon never changes for a given warning) so
repeated button clicks don't re-hit the public Overpass servers.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Public Overpass endpoints, tried in order. The service degrades gracefully if
# all are unreachable (returns empty categories rather than raising).
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Be a good citizen — Overpass asks for an identifying User-Agent.
_USER_AGENT = "AlertDashboardV2/1.0 (severe-weather impact scan)"

# Per-warning cache TTL. The polygon is fixed for a warning's life, but facility
# data can shift between OSM edits; an hour is a sane compromise.
_CACHE_TTL_S = 3600

# Safety caps so a county-wide warning can't return a wall of text on stream.
_CATEGORY_CAPS = {
    "places": 18,
    "mobile_home": 12,
    "schools": 15,
    "medical": 12,
}

# Display metadata per category. Order here is the order shown in the widget:
# context (places) first, then the at-risk facilities.
_CATEGORY_META = [
    ("places",      "Towns & Cities",          False),
    ("mobile_home", "Mobile Home / RV Parks",  True),
    ("schools",     "Schools & Daycares",      True),
    ("medical",     "Hospitals & Care",        True),
]


class OSMImpactService:
    """Queries Overpass for at-risk places inside a warning polygon."""

    def __init__(self):
        # product_id -> (fetched_at_monotonic, result_dict)
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    async def scan(
        self,
        product_id: str,
        polygon: list[list[float]],
        event_name: str = "",
        force: bool = False,
    ) -> dict:
        """Return categorized impacted places inside ``polygon``.

        ``polygon`` is a list of [lat, lon] pairs (Alert.polygon format).
        Cached per ``product_id``; pass ``force=True`` to bypass the cache.
        """
        if not polygon or len(polygon) < 3:
            return self._empty_result(product_id, event_name, error="no_polygon")

        # Serialize concurrent scans of the same warning so a double-click does
        # one Overpass call, not two.
        lock = self._locks.setdefault(product_id, asyncio.Lock())
        async with lock:
            if not force:
                cached = self._cache.get(product_id)
                if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
                    return cached[1]

            try:
                elements = await self._query_overpass(polygon)
            except Exception as e:
                logger.warning(f"Overpass scan failed for {product_id}: {e}")
                return self._empty_result(product_id, event_name, error="overpass_failed")

            result = self._build_result(product_id, event_name, elements)
            self._cache[product_id] = (time.monotonic(), result)
            return result

    def get_cached(self, product_id: str) -> Optional[dict]:
        cached = self._cache.get(product_id)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
            return cached[1]
        return None

    def clear_cache(self, product_id: Optional[str] = None) -> None:
        if product_id is None:
            self._cache.clear()
        else:
            self._cache.pop(product_id, None)

    # ── Overpass query ──────────────────────────────────────────────────────

    @staticmethod
    def _poly_string(polygon: list[list[float]]) -> str:
        """Build the Overpass `poly:` argument: "lat lon lat lon ...".

        Alert.polygon is already [[lat, lon], ...], which matches the order
        Overpass expects.
        """
        return " ".join(f"{lat:.5f} {lon:.5f}" for lat, lon in polygon)

    def _build_query(self, polygon: list[list[float]]) -> str:
        p = self._poly_string(polygon)
        # One query, all categories. `out center` yields a center point for
        # ways/relations so unnamed-node-only data still resolves a location.
        return f"""
[out:json][timeout:25];
(
  node["place"~"^(city|town|village|hamlet)$"](poly:"{p}");
  node["amenity"~"^(school|kindergarten|college|university|childcare)$"](poly:"{p}");
  way["amenity"~"^(school|kindergarten|college|university|childcare)$"](poly:"{p}");
  node["amenity"~"^(hospital|clinic)$"](poly:"{p}");
  way["amenity"~"^(hospital|clinic)$"](poly:"{p}");
  node["social_facility"](poly:"{p}");
  way["social_facility"](poly:"{p}");
  way["residential"="mobile_home"](poly:"{p}");
  node["tourism"="camp_site"](poly:"{p}");
  way["tourism"="camp_site"](poly:"{p}");
);
out center 250;
""".strip()

    async def _query_overpass(self, polygon: list[list[float]]) -> list[dict]:
        query = self._build_query(polygon)
        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(
            timeout=35.0, headers={"User-Agent": _USER_AGENT}
        ) as client:
            for endpoint in OVERPASS_ENDPOINTS:
                try:
                    resp = await client.post(endpoint, data={"data": query})
                    resp.raise_for_status()
                    return resp.json().get("elements", [])
                except Exception as e:
                    last_exc = e
                    logger.debug(f"Overpass endpoint {endpoint} failed: {e}")
                    continue
        if last_exc:
            raise last_exc
        return []

    # ── Result shaping ──────────────────────────────────────────────────────

    @staticmethod
    def _categorize(tags: dict) -> Optional[str]:
        """Map an element's tags to one of our display categories."""
        place = tags.get("place")
        if place in ("city", "town", "village", "hamlet"):
            return "places"

        if tags.get("residential") == "mobile_home" or tags.get("tourism") == "camp_site":
            return "mobile_home"

        amenity = tags.get("amenity")
        if amenity in ("school", "kindergarten", "college", "university", "childcare"):
            return "schools"
        if amenity in ("hospital", "clinic") or tags.get("social_facility"):
            return "medical"
        return None

    @staticmethod
    def _sub_label(category: str, tags: dict) -> str:
        """Small secondary line shown under a name (population / type)."""
        if category == "places":
            pop = tags.get("population")
            if pop and str(pop).replace(",", "").isdigit():
                return f"pop. {int(str(pop).replace(',', '')):,}"
            return (tags.get("place") or "").title()
        if category == "medical":
            sf = tags.get("social_facility")
            if sf:
                return sf.replace("_", " ").title()
            return (tags.get("amenity") or "").title()
        if category == "schools":
            return (tags.get("amenity") or "").replace("_", " ").title()
        if category == "mobile_home":
            return "RV / Camp Site" if tags.get("tourism") == "camp_site" else "Mobile Home Park"
        return ""

    def _build_result(self, product_id: str, event_name: str, elements: list[dict]) -> dict:
        buckets: dict[str, list[dict]] = {k: [] for k, _, _ in _CATEGORY_META}
        seen: set[tuple[str, str]] = set()

        for el in elements:
            tags = el.get("tags") or {}
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            category = self._categorize(tags)
            if not category:
                continue
            key = (category, name.lower())
            if key in seen:
                continue
            seen.add(key)

            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            pop_raw = tags.get("population")
            pop = int(str(pop_raw).replace(",", "")) if (pop_raw and str(pop_raw).replace(",", "").isdigit()) else None

            buckets[category].append({
                "name": name,
                "sub": self._sub_label(category, tags),
                "lat": lat,
                "lon": lon,
                "_pop": pop,
            })

        categories = []
        counts = {}
        for key, label, at_risk in _CATEGORY_META:
            items = buckets[key]
            if key == "places":
                # Biggest towns first; named-but-unpopulated fall to the bottom.
                items.sort(key=lambda i: (-(i["_pop"] or -1), i["name"].lower()))
            else:
                items.sort(key=lambda i: i["name"].lower())
            capped = items[: _CATEGORY_CAPS[key]]
            for i in capped:
                i.pop("_pop", None)
            counts[key] = len(items)  # true count (pre-cap) for "+N more"
            categories.append({
                "key": key,
                "label": label,
                "at_risk": at_risk,
                "items": capped,
                "total": len(items),
            })

        return {
            "product_id": product_id,
            "event_name": event_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": counts,
            "total": sum(counts.values()),
            "categories": categories,
        }

    @staticmethod
    def _empty_result(product_id: str, event_name: str, error: str = "") -> dict:
        return {
            "product_id": product_id,
            "event_name": event_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": {},
            "total": 0,
            "categories": [],
            "error": error,
        }


# ── Singleton ───────────────────────────────────────────────────────────────

_service: Optional[OSMImpactService] = None


def get_osm_impact_service() -> OSMImpactService:
    global _service
    if _service is None:
        _service = OSMImpactService()
    return _service
