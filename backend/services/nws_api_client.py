"""
NWS API client for Alert Dashboard V2.

This module provides a client for the National Weather Service API
with retry logic, rate limiting, and proper error handling.

API Documentation: https://www.weather.gov/documentation/services-web-api
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from ..config import get_settings
from ..parsers import AlertParser
from ..models.alert import Alert

logger = logging.getLogger(__name__)


class NWSAPIError(Exception):
    """Base exception for NWS API errors."""
    pass


class NWSAPIRateLimitError(NWSAPIError):
    """Raised when rate limited by NWS API."""
    pass


class NWSAPIClient:
    """
    Async client for the NWS API.

    Features:
    - Automatic retry with exponential backoff
    - Rate limiting (respects 30-second minimum between requests)
    - Proper User-Agent header
    - Connection pooling
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        """
        Initialize the NWS API client.

        Args:
            base_url: API base URL (default from settings)
            user_agent: User agent string (default from settings)
            timeout: Request timeout in seconds (default from settings)
        """
        settings = get_settings()

        self.base_url = base_url or settings.nws_api_base_url
        self.user_agent = user_agent or settings.nws_api_user_agent
        self.timeout = timeout or settings.nws_api_timeout

        self._client: Optional[httpx.AsyncClient] = None
        self._last_request_time: Optional[datetime] = None
        self._min_request_interval = 1.0  # Minimum seconds between requests

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/geo+json",
                },
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _rate_limit(self):
        """Enforce rate limiting between requests."""
        if self._last_request_time:
            elapsed = (datetime.now(timezone.utc) - self._last_request_time).total_seconds()
            if elapsed < self._min_request_interval:
                await asyncio.sleep(self._min_request_interval - elapsed)
        self._last_request_time = datetime.now(timezone.utc)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((httpx.HTTPError, NWSAPIError)),
        reraise=True,
    )
    async def _request(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        Make a request to the NWS API with retry logic.

        Args:
            endpoint: API endpoint (e.g., "/alerts/active")
            params: Query parameters

        Returns:
            JSON response as dict

        Raises:
            NWSAPIError: On API errors
            NWSAPIRateLimitError: When rate limited
        """
        await self._rate_limit()

        client = await self._get_client()

        try:
            response = await client.get(endpoint, params=params)

            if response.status_code == 429:
                logger.warning("NWS API rate limited")
                raise NWSAPIRateLimitError("Rate limited by NWS API")

            if response.status_code >= 500:
                logger.warning(f"NWS API server error: {response.status_code}")
                raise NWSAPIError(f"Server error: {response.status_code}")

            response.raise_for_status()

            return response.json()

        except httpx.HTTPStatusError as e:
            logger.error(f"NWS API HTTP error: {e}")
            raise NWSAPIError(f"HTTP error: {e}") from e
        except httpx.RequestError as e:
            logger.error(f"NWS API request error: {e}")
            raise NWSAPIError(f"Request error: {e}") from e

    async def get_active_alerts(
        self,
        area: Optional[str] = None,
        event: Optional[str] = None,
    ) -> list[dict]:
        """
        Get active alerts from NWS API.

        Args:
            area: State code (e.g., "OH") - single state only
            event: Event type filter (e.g., "Tornado Warning")

        Returns:
            List of alert feature dictionaries
        """
        # NWS API /alerts/active endpoint doesn't accept limit/status params anymore
        params = {}

        if area:
            params["area"] = area
        if event:
            params["event"] = event

        try:
            data = await self._request("/alerts/active", params)
            features = data.get("features", [])
            logger.info(f"Retrieved {len(features)} active alerts from NWS API")
            return features
        except Exception as e:
            logger.error(f"Failed to get active alerts: {e}")
            return []

    async def get_alert_by_id(self, alert_id: str) -> Optional[dict]:
        """
        Get a specific alert by ID.

        Args:
            alert_id: Alert ID or URN

        Returns:
            Alert feature dictionary or None
        """
        # Extract ID from URN if needed
        if alert_id.startswith("urn:"):
            alert_id = alert_id.split(":")[-1]

        try:
            data = await self._request(f"/alerts/{alert_id}")
            return data
        except NWSAPIError as e:
            logger.warning(f"Failed to get alert {alert_id}: {e}")
            return None

    async def get_zone_geometry(self, zone_id: str) -> Optional[dict]:
        """
        Get geometry for a forecast zone.

        Args:
            zone_id: Zone ID (e.g., "OHZ049")

        Returns:
            GeoJSON geometry dictionary or None
        """
        try:
            data = await self._request(f"/zones/forecast/{zone_id}")
            return data.get("geometry")
        except NWSAPIError as e:
            logger.warning(f"Failed to get zone geometry for {zone_id}: {e}")
            return None

    async def get_county_geometry(self, county_id: str) -> Optional[dict]:
        """
        Get geometry for a county.

        Args:
            county_id: County ID (e.g., "OHC049")

        Returns:
            GeoJSON geometry dictionary or None
        """
        try:
            data = await self._request(f"/zones/county/{county_id}")
            return data.get("geometry")
        except NWSAPIError as e:
            logger.warning(f"Failed to get county geometry for {county_id}: {e}")
            return None

    async def fetch_and_parse_alerts(
        self,
        states: Optional[list[str]] = None,
    ) -> list[Alert]:
        """
        Fetch alerts from API and parse them into Alert objects.

        Args:
            states: List of state codes to filter (default from settings)

        Returns:
            List of parsed Alert objects
        """
        settings = get_settings()
        states = states or settings.filter_states

        # Fetch all active alerts (NWS API doesn't support multi-state area param well)
        # We'll filter by state locally after parsing
        features = await self.get_active_alerts()

        # Parse each alert
        alerts = []
        for feature in features:
            try:
                alert = AlertParser.parse_api_alert(feature, source="api")
                if alert:
                    alerts.append(alert)
            except Exception as e:
                logger.error(f"Failed to parse API alert: {e}")

        # Filter by state if specified
        if states:
            states_upper = {s.upper() for s in states}
            filtered_alerts = []
            for alert in alerts:
                # Check if any affected area matches our target states
                for ugc in alert.affected_areas:
                    if len(ugc) >= 2 and ugc[:2].upper() in states_upper:
                        filtered_alerts.append(alert)
                        break
            logger.info(f"Filtered to {len(filtered_alerts)}/{len(alerts)} alerts for states {states}")
            return filtered_alerts

        logger.info(f"Parsed {len(alerts)} alerts from NWS API")
        return alerts


# Singleton instance
_client: Optional[NWSAPIClient] = None


def get_nws_client() -> NWSAPIClient:
    """Get the singleton NWS API client instance."""
    global _client
    if _client is None:
        _client = NWSAPIClient()
    return _client


async def close_nws_client():
    """Close the singleton NWS API client."""
    global _client
    if _client:
        await _client.close()
        _client = None
