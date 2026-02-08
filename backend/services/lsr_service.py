"""
Local Storm Reports (LSR) Service for Alert Dashboard V2.

This module fetches and manages Local Storm Reports from the Iowa State Mesonet API.
LSRs include tornado, hail, wind, flood, and other severe weather reports.

Data source: https://mesonet.agron.iastate.edu/geojson/lsr.geojson
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from dataclasses import dataclass, field, asdict
import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)


# LSR type colors matching NWS conventions
LSR_TYPE_COLORS = {
    "TORNADO": "#FF0000",
    "FUNNEL CLOUD": "#FF6600",
    "WALL CLOUD": "#FF9900",
    "HAIL": "#00FF00",
    "TSTM WND GST": "#FFD700",
    "TSTM WND DMG": "#FFA500",
    "NON-TSTM WND GST": "#CCCC00",
    "NON-TSTM WND DMG": "#CC9900",
    "FLOOD": "#00FF7F",
    "FLASH FLOOD": "#8B0000",
    "HEAVY RAIN": "#0066FF",
    "LIGHTNING": "#FFFF00",
    "SNOW": "#00BFFF",
    "HEAVY SNOW": "#1E90FF",
    "BLIZZARD": "#FF69B4",
    "ICE STORM": "#8B008B",
    "SLEET": "#9370DB",
    "FREEZING RAIN": "#DA70D6",
}

# LSR type icons (Font Awesome classes)
LSR_TYPE_ICONS = {
    "TORNADO": "fa-tornado",
    "FUNNEL CLOUD": "fa-tornado",
    "WALL CLOUD": "fa-cloud",
    "HAIL": "fa-cloud-meatball",
    "TSTM WND GST": "fa-wind",
    "TSTM WND DMG": "fa-house-damage",
    "NON-TSTM WND GST": "fa-wind",
    "NON-TSTM WND DMG": "fa-house-damage",
    "FLOOD": "fa-water",
    "FLASH FLOOD": "fa-house-flood-water",
    "HEAVY RAIN": "fa-cloud-showers-heavy",
    "LIGHTNING": "fa-bolt",
    "SNOW": "fa-snowflake",
    "HEAVY SNOW": "fa-snowflake",
    "BLIZZARD": "fa-snowflake",
    "ICE STORM": "fa-icicles",
    "SLEET": "fa-cloud-sleet",
    "FREEZING RAIN": "fa-icicles",
}


@dataclass
class StormReport:
    """Represents a Local Storm Report."""

    id: str
    report_type: str  # e.g., "TORNADO", "HAIL", "TSTM WND GST"
    magnitude: Optional[str] = None  # e.g., "1.00 INCH", "65 MPH"
    city: str = ""
    county: str = ""
    state: str = ""
    lat: float = 0.0
    lon: float = 0.0
    valid_time: Optional[str] = None  # ISO timestamp
    remark: str = ""
    source: str = ""  # e.g., "TRAINED SPOTTER", "PUBLIC", "VIEWER"
    wfo: str = ""  # Weather Forecast Office

    # Viewer report specific fields
    is_viewer: bool = False  # True if submitted by viewer
    submitter: str = ""  # Name of submitter (for viewer reports)
    location_text: str = ""  # Human-readable location from viewer

    # Derived fields
    color: str = field(default="")
    icon: str = field(default="")

    def __post_init__(self):
        """Set derived fields based on report type."""
        self.color = LSR_TYPE_COLORS.get(self.report_type, "#FFFFFF")
        self.icon = LSR_TYPE_ICONS.get(self.report_type, "fa-exclamation-circle")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class LSRService:
    """
    Service for fetching and managing Local Storm Reports.

    Features:
    - Fetches LSRs from Iowa State Mesonet API
    - In-memory caching with configurable TTL
    - Support for multiple states
    - Manual report submission
    """

    # Iowa State Mesonet API endpoint
    API_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"

    def __init__(
        self,
        cache_ttl_seconds: int = 300,  # 5 minutes
        default_hours: int = 24,
    ):
        """
        Initialize the LSR Service.

        Args:
            cache_ttl_seconds: How long to cache LSR data
            default_hours: Default lookback period for LSRs
        """
        self._cache: list[StormReport] = []
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._default_hours = default_hours
        self._manual_reports: list[StormReport] = []
        self._fetch_lock = asyncio.Lock()

    def _is_cache_valid(self) -> bool:
        """Check if cache is still valid."""
        if not self._cache_time:
            return False
        return datetime.now(timezone.utc) - self._cache_time < self._cache_ttl

    async def fetch_reports(
        self,
        states: Optional[list[str]] = None,
        hours: Optional[int] = None,
        force_refresh: bool = False,
    ) -> list[StormReport]:
        """
        Fetch storm reports from the API.

        Args:
            states: List of state codes (e.g., ["OH", "IN"])
            hours: Lookback period in hours
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            List of StormReport objects
        """
        # Use settings if not provided
        settings = get_settings()
        if states is None:
            states = settings.filter_states or ["OH"]
        if hours is None:
            hours = self._default_hours

        # Return cached data if valid and not forcing refresh
        if not force_refresh and self._is_cache_valid():
            return self._get_filtered_reports(states)

        # Use lock to prevent concurrent fetches
        async with self._fetch_lock:
            # Double-check cache after acquiring lock
            if not force_refresh and self._is_cache_valid():
                return self._get_filtered_reports(states)

            # Build API URL
            states_param = ",".join(states) if states else "OH"
            url = f"{self.API_URL}?states={states_param}&hours={hours}"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=30) as response:
                        if response.status != 200:
                            logger.error(f"LSR API returned status {response.status}")
                            return self._cache  # Return stale cache on error

                        data = await response.json()

                reports = self._parse_geojson(data)

                # Update cache
                self._cache = reports
                self._cache_time = datetime.now(timezone.utc)

                logger.info(f"Fetched {len(reports)} storm reports from Mesonet API")
                return reports

            except asyncio.TimeoutError:
                logger.error("Timeout fetching LSR data")
                return self._cache
            except Exception as e:
                logger.exception(f"Error fetching LSR data: {e}")
                return self._cache

    def _parse_geojson(self, data: dict) -> list[StormReport]:
        """Parse GeoJSON response into StormReport objects."""
        reports = []

        features = data.get("features", [])
        for feature in features:
            try:
                props = feature.get("properties", {})
                geom = feature.get("geometry", {})
                coords = geom.get("coordinates", [0, 0])

                # Generate unique ID
                report_id = f"lsr_{props.get('valid', '')}_{props.get('type', '')}_{coords[1]}_{coords[0]}"

                report = StormReport(
                    id=report_id,
                    report_type=props.get("typetext", "UNKNOWN"),
                    magnitude=props.get("magnitude") or props.get("mag"),
                    city=props.get("city", ""),
                    county=props.get("county", ""),
                    state=props.get("state", ""),
                    lat=coords[1] if len(coords) >= 2 else 0.0,
                    lon=coords[0] if len(coords) >= 2 else 0.0,
                    valid_time=props.get("valid"),
                    remark=props.get("remark", ""),
                    source=props.get("source", ""),
                    wfo=props.get("wfo", ""),
                )
                reports.append(report)

            except Exception as e:
                logger.warning(f"Error parsing LSR feature: {e}")
                continue

        # Sort by time (newest first)
        reports.sort(
            key=lambda r: r.valid_time or "",
            reverse=True
        )

        return reports

    def _get_filtered_reports(self, states: list[str]) -> list[StormReport]:
        """Filter cached reports by state."""
        if not states:
            return self._cache

        states_upper = {s.upper() for s in states}
        return [r for r in self._cache if r.state.upper() in states_upper]

    def add_manual_report(self, report: StormReport) -> None:
        """Add a manual/viewer-submitted report."""
        report.is_viewer = True
        report.source = "VIEWER"
        self._manual_reports.append(report)
        logger.info(f"Added viewer storm report: {report.report_type} at {report.city or report.location_text}")

    def remove_manual_report(self, report_id: str) -> bool:
        """Remove a viewer report by ID."""
        for i, report in enumerate(self._manual_reports):
            if report.id == report_id:
                self._manual_reports.pop(i)
                logger.info(f"Removed viewer storm report: {report_id}")
                return True
        return False

    def get_manual_reports(self) -> list[StormReport]:
        """Get all manual/viewer-submitted reports."""
        return self._manual_reports.copy()

    def clear_manual_reports(self) -> None:
        """Clear all manual reports."""
        self._manual_reports.clear()
        logger.info("Cleared all viewer storm reports")

    def get_all_reports(self, states: Optional[list[str]] = None) -> list[StormReport]:
        """
        Get all reports (official + manual).

        Official reports are filtered by state, but viewer reports are always included.

        Args:
            states: Filter official reports by states (viewer reports always included)

        Returns:
            Combined list of official and manual reports
        """
        # Filter official reports by state
        if states:
            official = self._get_filtered_reports(states)
        else:
            official = self._cache

        # Always include all viewer reports (they can be from anywhere)
        manual = self._manual_reports

        # Combine and sort by time
        all_reports = official + manual
        all_reports.sort(
            key=lambda r: r.valid_time or "",
            reverse=True
        )

        return all_reports

    def get_reports_by_type(self, report_type: str) -> list[StormReport]:
        """Get reports filtered by type."""
        return [r for r in self._cache if r.report_type.upper() == report_type.upper()]

    def get_statistics(self) -> dict[str, Any]:
        """Get report statistics."""
        type_counts: dict[str, int] = {}
        for report in self._cache:
            type_counts[report.report_type] = type_counts.get(report.report_type, 0) + 1

        return {
            "total_reports": len(self._cache),
            "manual_reports": len(self._manual_reports),
            "by_type": type_counts,
            "cache_age_seconds": (
                (datetime.now(timezone.utc) - self._cache_time).total_seconds()
                if self._cache_time else None
            ),
        }


# =============================================================================
# Singleton Instance
# =============================================================================

_service: Optional[LSRService] = None


def get_lsr_service() -> LSRService:
    """Get the singleton LSR Service instance."""
    global _service
    if _service is None:
        _service = LSRService()
    return _service


async def start_lsr_service():
    """Start the LSR service and do initial fetch."""
    service = get_lsr_service()
    await service.fetch_reports()
    logger.info("LSR service started")


async def stop_lsr_service():
    """Stop the LSR service."""
    global _service
    _service = None
    logger.info("LSR service stopped")
