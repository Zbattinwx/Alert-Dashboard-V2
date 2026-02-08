"""
ODOT (Ohio Department of Transportation) Service for Alert Dashboard V2.

This module fetches and manages ODOT camera and road sensor data from the OHGO API.
It also provides point-in-polygon detection for cameras inside weather alerts.

API Documentation: https://publicapi.ohgo.com/docs/v1/
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

import aiohttp
from shapely.geometry import Point, Polygon

from ..config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ODOTCamera:
    """Represents an ODOT traffic camera."""

    id: str
    location: str
    latitude: float
    longitude: float
    image_url: str
    description: str = ""

    # When camera is inside an alert
    in_alert: bool = False
    alert_type: str = ""
    alert_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class RoadSensor:
    """Represents an ODOT road weather sensor."""

    id: str
    location: str
    latitude: float
    longitude: float
    description: str = ""

    # Atmospheric data
    air_temp: Optional[float] = None  # Fahrenheit
    wind_speed: Optional[float] = None  # MPH
    wind_direction: Optional[str] = None
    precip_rate: Optional[float] = None

    # Surface data
    pavement_temp: Optional[float] = None  # Fahrenheit
    surface_status: Optional[str] = None

    # Derived flags
    is_cold: bool = False  # < 40°F
    is_freezing: bool = False  # <= 32°F

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class ODOTService:
    """
    Service for fetching and managing ODOT camera and sensor data.

    Features:
    - Fetches cameras and sensors from OHGO API
    - In-memory caching with configurable TTL
    - Point-in-polygon detection for cameras inside alerts
    - Cold pavement sensor tracking
    """

    def __init__(self):
        """Initialize the ODOT Service."""
        settings = get_settings()

        self._api_key = settings.odot_api_key
        self._api_base = settings.odot_api_base_url
        self._cache_ttl = timedelta(seconds=settings.odot_cache_ttl_seconds)

        self._cameras: list[ODOTCamera] = []
        self._sensors: list[RoadSensor] = []
        self._cameras_cache_time: Optional[datetime] = None
        self._sensors_cache_time: Optional[datetime] = None

        self._fetch_lock = asyncio.Lock()

    def _get_headers(self) -> dict[str, str]:
        """Get API request headers."""
        return {
            "Authorization": f"APIKEY {self._api_key}",
            "Accept": "application/json",
        }

    def _is_cameras_cache_valid(self) -> bool:
        """Check if camera cache is still valid."""
        if not self._cameras_cache_time:
            return False
        return datetime.now(timezone.utc) - self._cameras_cache_time < self._cache_ttl

    def _is_sensors_cache_valid(self) -> bool:
        """Check if sensor cache is still valid."""
        if not self._sensors_cache_time:
            return False
        return datetime.now(timezone.utc) - self._sensors_cache_time < self._cache_ttl

    async def fetch_cameras(self, force_refresh: bool = False) -> list[ODOTCamera]:
        """
        Fetch cameras from ODOT API.

        Args:
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            List of ODOTCamera objects
        """
        if not force_refresh and self._is_cameras_cache_valid():
            return self._cameras

        async with self._fetch_lock:
            # Double-check after acquiring lock
            if not force_refresh and self._is_cameras_cache_valid():
                return self._cameras

            url = f"{self._api_base}/cameras?page-all=true"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=self._get_headers(),
                        timeout=30
                    ) as response:
                        if response.status != 200:
                            logger.error(f"ODOT cameras API returned status {response.status}")
                            return self._cameras

                        data = await response.json()

                cameras = self._parse_cameras(data)
                self._cameras = cameras
                self._cameras_cache_time = datetime.now(timezone.utc)

                logger.info(f"Fetched {len(cameras)} cameras from ODOT API")
                return cameras

            except asyncio.TimeoutError:
                logger.error("Timeout fetching ODOT cameras")
                return self._cameras
            except Exception as e:
                logger.exception(f"Error fetching ODOT cameras: {e}")
                return self._cameras

    def _parse_cameras(self, data: dict) -> list[ODOTCamera]:
        """Parse camera API response."""
        cameras = []

        items = data.get("results", [])
        for item in items:
            try:
                # Get the first camera view URL
                views = item.get("cameraViews", [])
                image_url = views[0].get("largeUrl", "") if views else ""

                if not image_url:
                    continue  # Skip cameras without images

                camera = ODOTCamera(
                    id=str(item.get("id", "")),
                    location=item.get("location", "Unknown"),
                    latitude=float(item.get("latitude", 0)),
                    longitude=float(item.get("longitude", 0)),
                    image_url=image_url,
                    description=item.get("description", ""),
                )
                cameras.append(camera)

            except Exception as e:
                logger.warning(f"Error parsing camera: {e}")
                continue

        return cameras

    async def fetch_sensors(self, force_refresh: bool = False) -> list[RoadSensor]:
        """
        Fetch road weather sensors from ODOT API.

        Args:
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            List of RoadSensor objects
        """
        if not force_refresh and self._is_sensors_cache_valid():
            return self._sensors

        async with self._fetch_lock:
            if not force_refresh and self._is_sensors_cache_valid():
                return self._sensors

            url = f"{self._api_base}/weather-sensor-sites?page-all=true"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=self._get_headers(),
                        timeout=30
                    ) as response:
                        if response.status != 200:
                            logger.error(f"ODOT sensors API returned status {response.status}")
                            return self._sensors

                        data = await response.json()

                sensors = self._parse_sensors(data)
                self._sensors = sensors
                self._sensors_cache_time = datetime.now(timezone.utc)

                logger.info(f"Fetched {len(sensors)} sensors from ODOT API")
                return sensors

            except asyncio.TimeoutError:
                logger.error("Timeout fetching ODOT sensors")
                return self._sensors
            except Exception as e:
                logger.exception(f"Error fetching ODOT sensors: {e}")
                return self._sensors

    def _parse_sensors(self, data: dict) -> list[RoadSensor]:
        """Parse sensor API response."""
        settings = get_settings()
        cold_threshold = settings.cold_pavement_threshold
        freezing_threshold = settings.freezing_pavement_threshold

        sensors = []

        items = data.get("results", [])
        for item in items:
            try:
                # Get atmospheric data
                atmo = item.get("atmosphericSensors", [])
                atmo_data = atmo[0] if atmo else {}

                air_temp = atmo_data.get("airTemperature")
                if air_temp is not None and air_temp <= -999:
                    air_temp = None

                # Get surface data
                surface = item.get("surfaceSensors", [])
                surface_data = surface[0] if surface else {}

                pavement_temp = surface_data.get("surfaceTemperature")
                if pavement_temp is not None and pavement_temp <= -999:
                    pavement_temp = None

                # Determine cold/freezing flags
                is_cold = pavement_temp is not None and pavement_temp < cold_threshold
                is_freezing = pavement_temp is not None and pavement_temp <= freezing_threshold

                sensor = RoadSensor(
                    id=str(item.get("id", "")),
                    location=item.get("location", "Unknown"),
                    latitude=float(item.get("latitude", 0)),
                    longitude=float(item.get("longitude", 0)),
                    description=item.get("description", ""),
                    air_temp=air_temp,
                    wind_speed=atmo_data.get("averageWindSpeed"),
                    wind_direction=atmo_data.get("windDirection"),
                    precip_rate=atmo_data.get("precipitationRate"),
                    pavement_temp=pavement_temp,
                    surface_status=surface_data.get("surfaceStatus"),
                    is_cold=is_cold,
                    is_freezing=is_freezing,
                )
                sensors.append(sensor)

            except Exception as e:
                logger.warning(f"Error parsing sensor: {e}")
                continue

        return sensors

    def get_cold_sensors(self) -> list[RoadSensor]:
        """Get sensors with cold pavement (< threshold)."""
        return [s for s in self._sensors if s.is_cold]

    def get_freezing_sensors(self) -> list[RoadSensor]:
        """Get sensors with freezing pavement (<= 32°F)."""
        return [s for s in self._sensors if s.is_freezing]

    def find_cameras_in_alerts(
        self,
        alerts: list[dict],
        phenomena_filter: Optional[list[str]] = None
    ) -> list[ODOTCamera]:
        """
        Find cameras that are inside alert polygons.

        Args:
            alerts: List of alert dictionaries with 'polygon' and 'phenomenon' keys
            phenomena_filter: Only check alerts with these phenomena codes

        Returns:
            List of cameras inside matching alerts
        """
        settings = get_settings()
        if phenomena_filter is None:
            phenomena_filter = settings.camera_alert_phenomena

        cameras_in_alerts = []
        processed_camera_ids = set()

        for alert in alerts:
            # Check if alert matches phenomena filter
            phenomenon = alert.get("phenomenon", "")
            if phenomena_filter and phenomenon not in phenomena_filter:
                continue

            # Get polygon
            polygon_coords = alert.get("polygon", [])
            if not polygon_coords or len(polygon_coords) < 4:
                continue

            try:
                # Build Shapely polygon
                # Alert polygons are in [lat, lon] format, convert to (lon, lat) for Shapely
                coords = [(p[1], p[0]) for p in polygon_coords]

                # Ensure polygon is closed
                if coords[0] != coords[-1]:
                    coords.append(coords[0])

                polygon = Polygon(coords)

                if not polygon.is_valid:
                    # Try to fix invalid polygon
                    polygon = polygon.buffer(0)
                    if not polygon.is_valid:
                        continue

                # Check each camera
                for camera in self._cameras:
                    if camera.id in processed_camera_ids:
                        continue

                    point = Point(camera.longitude, camera.latitude)

                    if polygon.contains(point):
                        # Create a copy with alert info
                        camera_copy = ODOTCamera(
                            id=camera.id,
                            location=camera.location,
                            latitude=camera.latitude,
                            longitude=camera.longitude,
                            image_url=camera.image_url,
                            description=camera.description,
                            in_alert=True,
                            alert_type=phenomenon,
                            alert_name=alert.get("event_name", phenomenon),
                        )
                        cameras_in_alerts.append(camera_copy)
                        processed_camera_ids.add(camera.id)

            except Exception as e:
                logger.warning(f"Error processing alert polygon: {e}")
                continue

        return cameras_in_alerts

    def get_statistics(self) -> dict[str, Any]:
        """Get service statistics."""
        cold_sensors = self.get_cold_sensors()
        freezing_sensors = self.get_freezing_sensors()

        return {
            "total_cameras": len(self._cameras),
            "total_sensors": len(self._sensors),
            "cold_sensors": len(cold_sensors),
            "freezing_sensors": len(freezing_sensors),
            "cameras_cache_age_seconds": (
                (datetime.now(timezone.utc) - self._cameras_cache_time).total_seconds()
                if self._cameras_cache_time else None
            ),
            "sensors_cache_age_seconds": (
                (datetime.now(timezone.utc) - self._sensors_cache_time).total_seconds()
                if self._sensors_cache_time else None
            ),
        }


# =============================================================================
# Singleton Instance
# =============================================================================

_service: Optional[ODOTService] = None


def get_odot_service() -> ODOTService:
    """Get the singleton ODOT Service instance."""
    global _service
    if _service is None:
        _service = ODOTService()
    return _service


async def start_odot_service():
    """Start the ODOT service and do initial fetch."""
    service = get_odot_service()
    # Fetch initial data in parallel
    await asyncio.gather(
        service.fetch_cameras(),
        service.fetch_sensors(),
    )
    logger.info("ODOT service started")


async def stop_odot_service():
    """Stop the ODOT service."""
    global _service
    _service = None
    logger.info("ODOT service stopped")
