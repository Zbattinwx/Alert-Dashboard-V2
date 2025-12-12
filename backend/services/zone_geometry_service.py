"""
Zone Geometry Service for Alert Dashboard V2.

This module handles fetching and caching zone boundary geometries from the NWS API.
Zone-based alerts (advisories, watches) need precise zone boundaries instead of
entire county outlines for accurate map rendering.

Features:
- In-memory caching with optional disk persistence
- Support for both forecast zones (OHZ049) and counties (OHC049)
- Parallel geometry fetching for multiple zones
- Integration with NWS API client
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from ..config import get_settings
from ..models.alert import Alert
from .nws_api_client import get_nws_client, NWSAPIClient

logger = logging.getLogger(__name__)


# Type alias for polygon coordinates
# Each polygon is a list of [lat, lon] coordinate pairs
PolygonCoords = list[list[float]]


class ZoneGeometryService:
    """
    Service for fetching and caching zone geometry data from NWS API.

    Features:
    - In-memory cache with TTL
    - Disk persistence for faster startup
    - Parallel zone fetching
    - Support for forecast zones and counties
    """

    def __init__(
        self,
        cache_ttl_hours: int = 24,
        persistence_path: Optional[Path] = None,
        nws_client: Optional[NWSAPIClient] = None,
    ):
        """
        Initialize the Zone Geometry Service.

        Args:
            cache_ttl_hours: Hours to keep cached geometries valid
            persistence_path: Path to save/load cache (optional)
            nws_client: NWS API client instance (default: singleton)
        """
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_ttl = timedelta(hours=cache_ttl_hours)
        self._persistence_path = persistence_path
        self._nws_client = nws_client
        self._pending_fetches: dict[str, asyncio.Task] = {}

    def _get_client(self) -> NWSAPIClient:
        """Get the NWS API client."""
        if self._nws_client is None:
            self._nws_client = get_nws_client()
        return self._nws_client

    # =========================================================================
    # Cache Management
    # =========================================================================

    def _is_cache_valid(self, zone_id: str) -> bool:
        """Check if cached entry is still valid."""
        entry = self._cache.get(zone_id)
        if not entry:
            return False

        cached_at = entry.get("cached_at")
        if not cached_at:
            return False

        if isinstance(cached_at, str):
            cached_at = datetime.fromisoformat(cached_at)

        return datetime.now(timezone.utc) - cached_at < self._cache_ttl

    def _get_from_cache(self, zone_id: str) -> Optional[list[PolygonCoords]]:
        """Get geometry from cache if valid."""
        if self._is_cache_valid(zone_id):
            return self._cache[zone_id].get("geometry")
        return None

    def _add_to_cache(self, zone_id: str, geometry: Optional[list[PolygonCoords]]):
        """Add geometry to cache."""
        self._cache[zone_id] = {
            "geometry": geometry,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }

    def clear_cache(self):
        """Clear all cached geometries."""
        self._cache.clear()
        logger.info("Zone geometry cache cleared")

    def get_cache_stats(self) -> dict[str, int]:
        """Get cache statistics."""
        total = len(self._cache)
        valid = sum(1 for z in self._cache if self._is_cache_valid(z))
        with_geometry = sum(
            1 for z, e in self._cache.items()
            if self._is_cache_valid(z) and e.get("geometry")
        )

        return {
            "total_entries": total,
            "valid_entries": valid,
            "with_geometry": with_geometry,
            "without_geometry": valid - with_geometry,
        }

    # =========================================================================
    # Persistence
    # =========================================================================

    def save_to_file(self, path: Optional[Path] = None):
        """
        Save cache to JSON file.

        Args:
            path: File path (default from constructor)
        """
        path = path or self._persistence_path
        if not path:
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "cache_ttl_hours": self._cache_ttl.total_seconds() / 3600,
                "entries": self._cache,
            }

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)

            logger.info(f"Saved {len(self._cache)} zone geometries to {path}")

        except Exception as e:
            logger.error(f"Failed to save zone cache to {path}: {e}")

    def load_from_file(self, path: Optional[Path] = None) -> int:
        """
        Load cache from JSON file.

        Args:
            path: File path (default from constructor)

        Returns:
            Number of entries loaded
        """
        path = path or self._persistence_path
        if not path or not path.exists():
            return 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            entries = data.get("entries", {})
            loaded = 0

            for zone_id, entry in entries.items():
                # Only load if still valid
                cached_at = entry.get("cached_at")
                if cached_at:
                    if isinstance(cached_at, str):
                        cached_at = datetime.fromisoformat(cached_at)
                    if datetime.now(timezone.utc) - cached_at < self._cache_ttl:
                        self._cache[zone_id] = entry
                        loaded += 1

            logger.info(f"Loaded {loaded} zone geometries from {path}")
            return loaded

        except Exception as e:
            logger.error(f"Failed to load zone cache from {path}: {e}")
            return 0

    # =========================================================================
    # Zone Type Detection
    # =========================================================================

    @staticmethod
    def is_forecast_zone(ugc_code: str) -> bool:
        """Check if UGC code is a forecast zone (e.g., OHZ049)."""
        return (
            len(ugc_code) >= 6 and
            ugc_code[2] == 'Z' and
            ugc_code[:2].isalpha() and
            ugc_code[3:].isdigit()
        )

    @staticmethod
    def is_county_code(ugc_code: str) -> bool:
        """Check if UGC code is a county (e.g., OHC049)."""
        return (
            len(ugc_code) >= 6 and
            ugc_code[2] == 'C' and
            ugc_code[:2].isalpha() and
            ugc_code[3:].isdigit()
        )

    @staticmethod
    def get_zone_type(ugc_code: str) -> Optional[str]:
        """Get the type of UGC code."""
        if ZoneGeometryService.is_forecast_zone(ugc_code):
            return "zone"
        elif ZoneGeometryService.is_county_code(ugc_code):
            return "county"
        return None

    # =========================================================================
    # Geometry Fetching
    # =========================================================================

    async def fetch_zone_geometry(self, zone_id: str) -> Optional[list[PolygonCoords]]:
        """
        Fetch geometry for a single zone.

        Args:
            zone_id: UGC zone code (e.g., "OHZ049" or "OHC049")

        Returns:
            List of polygons (each polygon is list of [lat, lon] coords), or None
        """
        # Check cache first
        cached = self._get_from_cache(zone_id)
        if cached is not None:
            return cached

        # Check if already fetching
        if zone_id in self._pending_fetches:
            try:
                return await self._pending_fetches[zone_id]
            except Exception:
                pass

        # Determine zone type
        zone_type = self.get_zone_type(zone_id)
        if not zone_type:
            logger.debug(f"Invalid zone ID format: {zone_id}")
            self._add_to_cache(zone_id, None)
            return None

        # Create fetch task
        task = asyncio.create_task(self._do_fetch(zone_id, zone_type))
        self._pending_fetches[zone_id] = task

        try:
            result = await task
            return result
        finally:
            self._pending_fetches.pop(zone_id, None)

    async def _do_fetch(
        self,
        zone_id: str,
        zone_type: str
    ) -> Optional[list[PolygonCoords]]:
        """Actually fetch the zone geometry from API."""
        client = self._get_client()

        try:
            if zone_type == "zone":
                geometry = await client.get_zone_geometry(zone_id)
            else:
                geometry = await client.get_county_geometry(zone_id)

            if not geometry:
                logger.debug(f"No geometry available for {zone_id}")
                self._add_to_cache(zone_id, None)
                return None

            # Parse geometry
            polygons = self._parse_geometry(geometry)
            self._add_to_cache(zone_id, polygons)

            if polygons:
                logger.debug(f"Fetched geometry for {zone_id}: {len(polygons)} polygon(s)")

            return polygons

        except Exception as e:
            logger.warning(f"Error fetching geometry for {zone_id}: {e}")
            self._add_to_cache(zone_id, None)
            return None

    def _parse_geometry(self, geometry: dict) -> Optional[list[PolygonCoords]]:
        """
        Parse GeoJSON geometry into list of polygons.

        Converts coordinates from [lon, lat] to [lat, lon] for Leaflet compatibility.
        """
        geom_type = geometry.get("type")
        coordinates = geometry.get("coordinates", [])

        if not coordinates:
            return None

        polygons: list[PolygonCoords] = []

        if geom_type == "Polygon":
            # Single polygon - take outer ring (first element)
            outer_ring = coordinates[0]
            # Swap lon,lat to lat,lon for Leaflet
            swapped = [[coord[1], coord[0]] for coord in outer_ring]
            polygons.append(swapped)

        elif geom_type == "MultiPolygon":
            # Multiple polygons
            for polygon_coords in coordinates:
                outer_ring = polygon_coords[0]
                swapped = [[coord[1], coord[0]] for coord in outer_ring]
                polygons.append(swapped)

        return polygons if polygons else None

    # =========================================================================
    # Batch Operations
    # =========================================================================

    async def fetch_multiple_zones(
        self,
        zone_ids: list[str],
        max_concurrent: int = 10,
    ) -> dict[str, Optional[list[PolygonCoords]]]:
        """
        Fetch geometries for multiple zones in parallel.

        Args:
            zone_ids: List of UGC zone codes
            max_concurrent: Maximum concurrent requests

        Returns:
            Dict mapping zone_id to geometry (or None)
        """
        # Use semaphore to limit concurrency
        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_with_limit(zone_id: str):
            async with semaphore:
                return zone_id, await self.fetch_zone_geometry(zone_id)

        tasks = [fetch_with_limit(z) for z in zone_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = {}
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Zone fetch failed: {result}")
            else:
                zone_id, geometry = result
                output[zone_id] = geometry

        return output

    async def populate_alert_geometry(self, alert: Alert) -> bool:
        """
        Populate an alert with zone geometry if it has zone codes but no polygon.

        Args:
            alert: The Alert object to populate

        Returns:
            True if geometry was added, False otherwise
        """
        # Skip if alert already has a polygon
        if alert.polygon:
            return False

        # Skip if no affected areas
        if not alert.affected_areas:
            return False

        # Get zone codes (both forecast zones and counties)
        zone_codes = [
            ugc for ugc in alert.affected_areas
            if self.get_zone_type(ugc) is not None
        ]

        if not zone_codes:
            return False

        logger.debug(
            f"Alert {alert.product_id} has {len(zone_codes)} zone(s) but no polygon. "
            "Fetching zone geometry..."
        )

        # Fetch all zone geometries
        geometries = await self.fetch_multiple_zones(zone_codes)

        # Combine all polygons
        all_polygons: list[PolygonCoords] = []
        for zone_id, geometry in geometries.items():
            if geometry:
                all_polygons.extend(geometry)

        if all_polygons:
            alert.polygon = all_polygons
            logger.debug(
                f"Added {len(all_polygons)} polygon(s) to alert {alert.product_id}"
            )
            return True

        return False

    async def populate_multiple_alerts(
        self,
        alerts: list[Alert],
        max_concurrent: int = 10,
    ) -> int:
        """
        Populate geometry for multiple alerts.

        Args:
            alerts: List of alerts to populate
            max_concurrent: Maximum concurrent API requests

        Returns:
            Number of alerts that received geometry
        """
        # First, collect all unique zone codes needed
        all_zones: set[str] = set()
        alerts_needing_geometry = []

        for alert in alerts:
            if alert.polygon or not alert.affected_areas:
                continue

            zones = [
                ugc for ugc in alert.affected_areas
                if self.get_zone_type(ugc) is not None
            ]
            if zones:
                all_zones.update(zones)
                alerts_needing_geometry.append(alert)

        if not all_zones:
            return 0

        # Fetch all zones at once
        logger.info(f"Fetching geometry for {len(all_zones)} unique zones")
        geometries = await self.fetch_multiple_zones(list(all_zones), max_concurrent)

        # Apply to alerts
        populated = 0
        for alert in alerts_needing_geometry:
            zones = [
                ugc for ugc in alert.affected_areas
                if self.get_zone_type(ugc) is not None
            ]

            all_polygons: list[PolygonCoords] = []
            for zone_id in zones:
                geom = geometries.get(zone_id)
                if geom:
                    all_polygons.extend(geom)

            if all_polygons:
                alert.polygon = all_polygons
                populated += 1

        logger.info(f"Added geometry to {populated}/{len(alerts_needing_geometry)} alerts")
        return populated


# =============================================================================
# Singleton Instance
# =============================================================================

_service: Optional[ZoneGeometryService] = None


def get_zone_geometry_service() -> ZoneGeometryService:
    """Get the singleton Zone Geometry Service instance."""
    global _service
    if _service is None:
        settings = get_settings()
        persistence_path = settings.data_dir / "zone_geometry_cache.json"
        _service = ZoneGeometryService(
            cache_ttl_hours=24,
            persistence_path=persistence_path,
        )
    return _service


async def start_zone_geometry_service():
    """Start the zone geometry service and load cache."""
    service = get_zone_geometry_service()
    service.load_from_file()
    logger.info("Zone geometry service started")


async def stop_zone_geometry_service():
    """Stop the zone geometry service and save cache."""
    global _service
    if _service:
        _service.save_to_file()
        _service = None
    logger.info("Zone geometry service stopped")
