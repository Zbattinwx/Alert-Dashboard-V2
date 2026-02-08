"""
SPC (Storm Prediction Center) Service for Alert Dashboard V2.

This module fetches and manages SPC outlook data including:
- Categorical convective outlooks (Day 1-3)
- Probabilistic outlooks (tornado, wind, hail)
- Mesoscale Discussions

Data is fetched from official SPC GeoJSON and RSS feeds.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum

import aiohttp
from shapely.geometry import shape, Point, Polygon, MultiPolygon
from shapely.ops import unary_union

from ..config import get_settings

logger = logging.getLogger(__name__)


def parse_spc_datetime(dt_str: Optional[str]) -> Optional[str]:
    """
    Parse SPC datetime format and convert to ISO format.

    SPC uses formats like:
    - '202412201200' (YYYYMMDDHHMM)
    - '202412201200Z' (with Z suffix)

    Returns ISO 8601 format string or None if parsing fails.
    """
    if not dt_str:
        return None

    # Remove trailing Z if present
    dt_clean = dt_str.rstrip('Z').strip()

    try:
        # Try YYYYMMDDHHMM format (12 digits)
        if len(dt_clean) == 12 and dt_clean.isdigit():
            dt = datetime.strptime(dt_clean, "%Y%m%d%H%M")
            # SPC times are in UTC
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()

        # Try YYYYMMDD format (8 digits)
        if len(dt_clean) == 8 and dt_clean.isdigit():
            dt = datetime.strptime(dt_clean, "%Y%m%d")
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()

        # If it looks like it might already be ISO format, return as-is
        if 'T' in dt_str or '-' in dt_str:
            return dt_str

    except ValueError:
        pass

    # Return original string if we can't parse it
    return dt_str


class RiskLevel(str, Enum):
    """SPC Categorical Risk Levels."""
    TSTM = "TSTM"      # General Thunderstorms
    MRGL = "MRGL"      # Marginal
    SLGT = "SLGT"      # Slight
    ENH = "ENH"        # Enhanced
    MDT = "MDT"        # Moderate
    HIGH = "HIGH"      # High


# Risk level colors matching SPC official colors
RISK_COLORS = {
    "TSTM": "#76C776",   # Light Green
    "MRGL": "#66A366",   # Dark Green
    "SLGT": "#F6F67F",   # Yellow
    "ENH": "#E6C27A",    # Orange/Tan
    "MDT": "#E67F7F",    # Red
    "HIGH": "#FF66FF",   # Magenta
}

# Risk level display names
RISK_NAMES = {
    "TSTM": "General Thunderstorms",
    "MRGL": "Marginal Risk",
    "SLGT": "Slight Risk",
    "ENH": "Enhanced Risk",
    "MDT": "Moderate Risk",
    "HIGH": "High Risk",
}

# Risk level sort order (higher = more severe)
RISK_ORDER = {
    "TSTM": 0,
    "MRGL": 1,
    "SLGT": 2,
    "ENH": 3,
    "MDT": 4,
    "HIGH": 5,
}

# Probabilistic outlook colors (for tornado, wind, hail)
PROB_COLORS = {
    "0.02": "#008B00",   # 2% - Dark Green
    "2": "#008B00",
    "0.05": "#8B4726",   # 5% - Brown
    "5": "#8B4726",
    "0.10": "#FFD700",   # 10% - Gold/Yellow
    "10": "#FFD700",
    "0.15": "#FF0000",   # 15% - Red
    "15": "#FF0000",
    "0.30": "#FF00FF",   # 30% - Magenta
    "30": "#FF00FF",
    "0.45": "#9400D3",   # 45% - Purple
    "45": "#9400D3",
    "0.60": "#882D60",   # 60% - Dark Magenta
    "60": "#882D60",
    "SIGN": "#000000",   # Significant - Black hatching
    "SIGPROB": "#000000",
}

# Probabilistic outlook display names
PROB_NAMES = {
    "0.02": "2% Probability",
    "2": "2% Probability",
    "0.05": "5% Probability",
    "5": "5% Probability",
    "0.10": "10% Probability",
    "10": "10% Probability",
    "0.15": "15% Probability",
    "15": "15% Probability",
    "0.30": "30% Probability",
    "30": "30% Probability",
    "0.45": "45% Probability",
    "45": "45% Probability",
    "0.60": "60% Probability",
    "60": "60% Probability",
    "SIGN": "Significant",
    "SIGPROB": "Significant",
}

# Probabilistic risk order
PROB_ORDER = {
    "0.02": 1, "2": 1,
    "0.05": 2, "5": 2,
    "0.10": 3, "10": 3,
    "0.15": 4, "15": 4,
    "0.30": 5, "30": 5,
    "0.45": 6, "45": 6,
    "0.60": 7, "60": 7,
    "SIGN": 8, "SIGPROB": 8,
}


@dataclass
class OutlookPolygon:
    """Represents a single outlook polygon."""

    risk_level: str
    risk_name: str
    color: str
    valid_time: Optional[str] = None
    expire_time: Optional[str] = None
    issue_time: Optional[str] = None
    geometry: Optional[dict] = None  # GeoJSON geometry

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class OutlookData:
    """Container for a day's outlook data."""

    day: int  # 1, 2, or 3
    outlook_type: str  # "categorical", "tornado", "wind", "hail"
    valid_time: Optional[str] = None
    expire_time: Optional[str] = None
    issue_time: Optional[str] = None
    polygons: list[OutlookPolygon] = field(default_factory=list)
    geojson: Optional[dict] = None  # Full GeoJSON for map rendering

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "day": self.day,
            "outlook_type": self.outlook_type,
            "valid_time": self.valid_time,
            "expire_time": self.expire_time,
            "issue_time": self.issue_time,
            "polygons": [p.to_dict() for p in self.polygons],
        }
        if self.geojson:
            result["geojson"] = self.geojson
        return result


@dataclass
class MesoscaleDiscussion:
    """Represents a Mesoscale Discussion."""

    md_number: str
    title: str
    link: str
    description: str
    image_url: str
    affected_states: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class SPCService:
    """
    Service for fetching and managing SPC outlook data.

    Features:
    - Fetches categorical and probabilistic outlooks
    - Fetches mesoscale discussions
    - In-memory caching with configurable TTL
    - State-based filtering
    """

    # SPC GeoJSON URLs
    OUTLOOK_URLS = {
        # Day 1 Categorical
        "day1_categorical": "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson",
        # Day 2 Categorical
        "day2_categorical": "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.nolyr.geojson",
        # Day 3 Categorical
        "day3_categorical": "https://www.spc.noaa.gov/products/outlook/day3otlk_cat.nolyr.geojson",
        # Day 1 Probabilistic
        "day1_tornado": "https://www.spc.noaa.gov/products/outlook/day1otlk_torn.nolyr.geojson",
        "day1_wind": "https://www.spc.noaa.gov/products/outlook/day1otlk_wind.nolyr.geojson",
        "day1_hail": "https://www.spc.noaa.gov/products/outlook/day1otlk_hail.nolyr.geojson",
        # Day 2 Probabilistic
        "day2_tornado": "https://www.spc.noaa.gov/products/outlook/day2otlk_torn.nolyr.geojson",
        "day2_wind": "https://www.spc.noaa.gov/products/outlook/day2otlk_wind.nolyr.geojson",
        "day2_hail": "https://www.spc.noaa.gov/products/outlook/day2otlk_hail.nolyr.geojson",
    }

    # Mesoscale Discussion RSS feed
    MD_RSS_URL = "https://www.spc.noaa.gov/products/spcmdrss.xml"

    # State outlook image URL template
    STATE_IMAGE_URL = "https://www.spc.noaa.gov/partners/outlooks/state/images/{state}_swody{day}.png"
    STATE_PROB_IMAGE_URL = "https://www.spc.noaa.gov/partners/outlooks/state/images/{state}_swody{day}_{type}.png"

    def __init__(self):
        """Initialize the SPC Service."""
        self._cache_ttl = timedelta(minutes=10)  # 10 minute cache

        # Cached data
        self._outlooks: dict[str, OutlookData] = {}
        self._outlooks_cache_time: Optional[datetime] = None
        self._mds: list[MesoscaleDiscussion] = []
        self._mds_cache_time: Optional[datetime] = None

        self._fetch_lock = asyncio.Lock()

    def _is_outlooks_cache_valid(self) -> bool:
        """Check if outlook cache is still valid."""
        if not self._outlooks_cache_time:
            return False
        return datetime.now(timezone.utc) - self._outlooks_cache_time < self._cache_ttl

    def _is_mds_cache_valid(self) -> bool:
        """Check if MD cache is still valid."""
        if not self._mds_cache_time:
            return False
        return datetime.now(timezone.utc) - self._mds_cache_time < self._cache_ttl

    async def fetch_outlook(
        self,
        outlook_key: str,
        force_refresh: bool = False
    ) -> Optional[OutlookData]:
        """
        Fetch a specific outlook.

        Args:
            outlook_key: Key like "day1_categorical", "day1_tornado", etc.
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            OutlookData or None if fetch failed
        """
        if not force_refresh and self._is_outlooks_cache_valid():
            if outlook_key in self._outlooks:
                return self._outlooks[outlook_key]

        url = self.OUTLOOK_URLS.get(outlook_key)
        if not url:
            logger.warning(f"Unknown outlook key: {outlook_key}")
            return None

        async with self._fetch_lock:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=30) as response:
                        if response.status != 200:
                            logger.error(f"SPC outlook fetch failed: {response.status}")
                            return self._outlooks.get(outlook_key)

                        data = await response.json()

                outlook = self._parse_outlook(outlook_key, data)
                self._outlooks[outlook_key] = outlook
                self._outlooks_cache_time = datetime.now(timezone.utc)

                logger.info(f"Fetched SPC outlook: {outlook_key} with {len(outlook.polygons)} polygons")
                return outlook

            except asyncio.TimeoutError:
                logger.error(f"Timeout fetching SPC outlook: {outlook_key}")
                return self._outlooks.get(outlook_key)
            except Exception as e:
                logger.exception(f"Error fetching SPC outlook {outlook_key}: {e}")
                return self._outlooks.get(outlook_key)

    def _parse_outlook(self, outlook_key: str, data: dict) -> OutlookData:
        """Parse GeoJSON outlook data."""
        parts = outlook_key.split("_")
        day = int(parts[0].replace("day", ""))
        outlook_type = parts[1] if len(parts) > 1 else "categorical"

        polygons = []
        features = data.get("features", [])

        # Extract timing from first feature
        valid_time = None
        expire_time = None
        issue_time = None

        for feature in features:
            props = feature.get("properties", {})

            # Get timing from first feature (parse to ISO format)
            if not valid_time:
                valid_time = parse_spc_datetime(props.get("VALID") or props.get("valid"))
                expire_time = parse_spc_datetime(props.get("EXPIRE") or props.get("expire"))
                issue_time = parse_spc_datetime(props.get("ISSUE") or props.get("issue"))

            # Get risk level
            label = props.get("LABEL") or props.get("label") or ""
            label_upper = label.upper()

            # Determine if this is a probabilistic outlook
            is_prob = outlook_type in ["tornado", "wind", "hail"]

            if is_prob:
                # For probabilistic outlooks, use probability colors/names
                # Labels are typically like "0.02", "0.05", "0.10", etc. or "2", "5", "10"
                # or "SIGN" for significant
                risk_name = PROB_NAMES.get(label, PROB_NAMES.get(label_upper, props.get("LABEL2") or props.get("label2") or f"{label}%"))
                color = props.get("fill") or PROB_COLORS.get(label, PROB_COLORS.get(label_upper, "#888888"))
            else:
                # For categorical outlooks
                if label_upper not in RISK_COLORS:
                    continue
                risk_name = RISK_NAMES.get(label_upper, props.get("LABEL2") or props.get("label2") or label)
                color = props.get("fill") or RISK_COLORS.get(label_upper, "#888888")

            polygon = OutlookPolygon(
                risk_level=label,
                risk_name=risk_name,
                color=color,
                valid_time=parse_spc_datetime(props.get("VALID") or props.get("valid")) or valid_time,
                expire_time=parse_spc_datetime(props.get("EXPIRE") or props.get("expire")) or expire_time,
                issue_time=parse_spc_datetime(props.get("ISSUE") or props.get("issue")) or issue_time,
                geometry=feature.get("geometry"),
            )
            polygons.append(polygon)

        # Sort polygons by risk level (highest risk first)
        is_prob = outlook_type in ["tornado", "wind", "hail"]
        if is_prob:
            polygons.sort(key=lambda p: PROB_ORDER.get(p.risk_level, PROB_ORDER.get(p.risk_level.upper(), -1)), reverse=True)
        else:
            polygons.sort(key=lambda p: RISK_ORDER.get(p.risk_level.upper(), -1), reverse=True)

        return OutlookData(
            day=day,
            outlook_type=outlook_type,
            valid_time=valid_time,
            expire_time=expire_time,
            issue_time=issue_time,
            polygons=polygons,
            geojson=data,
        )

    async def fetch_all_outlooks(self, force_refresh: bool = False) -> dict[str, OutlookData]:
        """
        Fetch all available outlooks.

        Args:
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            Dictionary of outlook_key -> OutlookData
        """
        if not force_refresh and self._is_outlooks_cache_valid():
            return self._outlooks

        # Fetch all outlooks in parallel
        tasks = [
            self.fetch_outlook(key, force_refresh=True)
            for key in self.OUTLOOK_URLS.keys()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for key, result in zip(self.OUTLOOK_URLS.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch {key}: {result}")
            elif result:
                self._outlooks[key] = result

        self._outlooks_cache_time = datetime.now(timezone.utc)
        return self._outlooks

    async def fetch_mesoscale_discussions(
        self,
        force_refresh: bool = False
    ) -> list[MesoscaleDiscussion]:
        """
        Fetch current mesoscale discussions from RSS feed.

        Args:
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            List of MesoscaleDiscussion objects
        """
        if not force_refresh and self._is_mds_cache_valid():
            return self._mds

        async with self._fetch_lock:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(self.MD_RSS_URL, timeout=30) as response:
                        if response.status != 200:
                            logger.error(f"SPC MD RSS fetch failed: {response.status}")
                            return self._mds

                        xml_content = await response.text()

                mds = self._parse_md_rss(xml_content)
                self._mds = mds
                self._mds_cache_time = datetime.now(timezone.utc)

                logger.info(f"Fetched {len(mds)} mesoscale discussions")
                return mds

            except asyncio.TimeoutError:
                logger.error("Timeout fetching SPC MD RSS")
                return self._mds
            except Exception as e:
                logger.exception(f"Error fetching SPC MDs: {e}")
                return self._mds

    def _parse_md_rss(self, xml_content: str) -> list[MesoscaleDiscussion]:
        """Parse mesoscale discussion RSS feed."""
        mds = []

        try:
            root = ET.fromstring(xml_content)

            # Find all items in the RSS feed
            for item in root.findall(".//item"):
                title_elem = item.find("title")
                link_elem = item.find("link")
                desc_elem = item.find("description")

                if not all([title_elem, link_elem]):
                    continue

                title = title_elem.text or ""
                link = link_elem.text or ""
                description = desc_elem.text if desc_elem is not None else ""

                # Extract MD number from link
                md_match = re.search(r"/md(\d+)\.html", link)
                if not md_match:
                    continue

                md_number = md_match.group(1)

                # Build image URL
                image_url = f"https://www.spc.noaa.gov/products/md/mcd{md_number}.png"

                # Extract affected states from title/description
                # MDs often have state abbreviations in title like "...IA KS MO..."
                states = self._extract_states(title + " " + description)

                md = MesoscaleDiscussion(
                    md_number=md_number,
                    title=title,
                    link=link,
                    description=description,
                    image_url=image_url,
                    affected_states=states,
                )
                mds.append(md)

        except ET.ParseError as e:
            logger.error(f"Error parsing MD RSS XML: {e}")

        return mds

    def _extract_states(self, text: str) -> list[str]:
        """Extract state abbreviations from text."""
        # Common US state abbreviations
        state_codes = [
            "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
            "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
            "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
            "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
            "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"
        ]

        found_states = []
        text_upper = text.upper()

        for state in state_codes:
            # Look for state code as standalone word
            if re.search(rf'\b{state}\b', text_upper):
                found_states.append(state)

        return found_states

    def filter_mds_by_states(
        self,
        mds: list[MesoscaleDiscussion],
        states: list[str]
    ) -> list[MesoscaleDiscussion]:
        """
        Filter mesoscale discussions by affected states.

        Args:
            mds: List of MesoscaleDiscussion objects
            states: List of state codes to filter by

        Returns:
            Filtered list of MesoscaleDiscussion objects
        """
        if not states:
            return mds

        states_upper = [s.upper() for s in states]

        return [
            md for md in mds
            if any(state in md.affected_states for state in states_upper)
        ]

    def get_state_outlook_urls(self, states: list[str], day: int = 1) -> dict[str, dict[str, str]]:
        """
        Get state-specific outlook image URLs.

        Args:
            states: List of state codes
            day: Outlook day (1, 2, or 3)

        Returns:
            Dictionary of state -> {outlook_type -> URL}
        """
        result = {}

        for state in states:
            state_upper = state.upper()
            result[state_upper] = {
                "categorical": self.STATE_IMAGE_URL.format(state=state_upper, day=day),
                "tornado": self.STATE_PROB_IMAGE_URL.format(state=state_upper, day=day, type="TORN"),
                "wind": self.STATE_PROB_IMAGE_URL.format(state=state_upper, day=day, type="WIND"),
                "hail": self.STATE_PROB_IMAGE_URL.format(state=state_upper, day=day, type="HAIL"),
            }

        return result

    def get_highest_risk_for_point(
        self,
        lat: float,
        lon: float,
        outlook_key: str = "day1_categorical"
    ) -> Optional[OutlookPolygon]:
        """
        Get the highest risk level at a specific point.

        Args:
            lat: Latitude
            lon: Longitude
            outlook_key: Which outlook to check

        Returns:
            OutlookPolygon with highest risk at point, or None
        """
        outlook = self._outlooks.get(outlook_key)
        if not outlook:
            return None

        point = Point(lon, lat)
        highest_risk = None
        highest_order = -1

        for polygon in outlook.polygons:
            if not polygon.geometry:
                continue

            try:
                geom = shape(polygon.geometry)
                if geom.contains(point):
                    risk_order = RISK_ORDER.get(polygon.risk_level, -1)
                    if risk_order > highest_order:
                        highest_order = risk_order
                        highest_risk = polygon
            except Exception:
                continue

        return highest_risk

    async def fetch_discussion(self, day: int = 1, force_refresh: bool = False) -> Optional[str]:
        """
        Fetch the SPC outlook discussion text.

        Args:
            day: Outlook day (1, 2, or 3)
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            Discussion text string or None if fetch failed
        """
        cache_key = f"discussion_day{day}"

        # Check cache
        if not force_refresh and hasattr(self, '_discussions'):
            cached = self._discussions.get(cache_key)
            if cached:
                cache_time = self._discussion_cache_times.get(cache_key)
                if cache_time and datetime.now(timezone.utc) - cache_time < self._cache_ttl:
                    return cached

        # Initialize cache dicts if not exists
        if not hasattr(self, '_discussions'):
            self._discussions: dict[str, str] = {}
            self._discussion_cache_times: dict[str, datetime] = {}

        # SPC discussion URLs
        discussion_url = f"https://www.spc.noaa.gov/products/outlook/day{day}otlk.txt"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(discussion_url, timeout=30) as response:
                    if response.status != 200:
                        logger.error(f"SPC discussion fetch failed: {response.status}")
                        return self._discussions.get(cache_key)

                    text = await response.text()

                    # Clean up the text - remove excessive whitespace but preserve structure
                    lines = text.split('\n')
                    cleaned_lines = []
                    for line in lines:
                        # Skip empty lines at start
                        if not cleaned_lines and not line.strip():
                            continue
                        cleaned_lines.append(line.rstrip())

                    # Remove trailing empty lines
                    while cleaned_lines and not cleaned_lines[-1].strip():
                        cleaned_lines.pop()

                    cleaned_text = '\n'.join(cleaned_lines)

                    self._discussions[cache_key] = cleaned_text
                    self._discussion_cache_times[cache_key] = datetime.now(timezone.utc)

                    logger.info(f"Fetched SPC Day {day} discussion")
                    return cleaned_text

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching SPC Day {day} discussion")
            return self._discussions.get(cache_key)
        except Exception as e:
            logger.exception(f"Error fetching SPC Day {day} discussion: {e}")
            return self._discussions.get(cache_key)

    def get_statistics(self) -> dict[str, Any]:
        """Get service statistics."""
        return {
            "cached_outlooks": list(self._outlooks.keys()),
            "cached_mds_count": len(self._mds),
            "outlooks_cache_age_seconds": (
                (datetime.now(timezone.utc) - self._outlooks_cache_time).total_seconds()
                if self._outlooks_cache_time else None
            ),
            "mds_cache_age_seconds": (
                (datetime.now(timezone.utc) - self._mds_cache_time).total_seconds()
                if self._mds_cache_time else None
            ),
        }


# =============================================================================
# Singleton Instance
# =============================================================================

_service: Optional[SPCService] = None


def get_spc_service() -> SPCService:
    """Get the singleton SPC Service instance."""
    global _service
    if _service is None:
        _service = SPCService()
    return _service


async def start_spc_service():
    """Start the SPC service and do initial fetch."""
    service = get_spc_service()
    # Fetch initial data in parallel
    await asyncio.gather(
        service.fetch_outlook("day1_categorical"),
        service.fetch_mesoscale_discussions(),
    )
    logger.info("SPC service started")


async def stop_spc_service():
    """Stop the SPC service."""
    global _service
    _service = None
    logger.info("SPC service stopped")
