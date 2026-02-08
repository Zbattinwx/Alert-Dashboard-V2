"""
Wind Gusts Service for Alert Dashboard V2.

Fetches wind gust data from Iowa State Mesonet ASOS stations for configured states.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)


# Station name mappings by state
STATION_NAMES: dict[str, dict[str, str]] = {
    "OH": {
        "DAY": "Dayton",
        "BJJ": "Wooster",
        "SGH": "Springfield",
        "I69": "Batavia",
        "S24": "Fremont",
        "I67": "Harrison",
        "GDK": "Dayton",
        "10G": "Millersburg",
        "CDI": "Cambridge",
        "OWX": "Ottawa",
        "I23": "Washington Court House",
        "FFO": "Wright Patterson",
        "I68": "Lebanon",
        "OXD": "Oxford",
        "I74": "Urbana",
        "POV": "Ravenna",
        "LNN": "Willoughby",
        "RZT": "Chillicothe",
        "I95": "Kenton",
        "JRO": "Jackson",
        "UYF": "London",
        "LCK": "Rickenbacker ANG",
        "VNW": "Van Wert County",
        "AXV": "Wapakoneta",
        "MWO": "Middletown",
        "UNI": "Albany",
        "ILN": "Wilmington",
        "AKR": "Akron",
        "VTA": "Newark",
        "FDY": "Findlay",
        "BKL": "Cleveland Burke",
        "OSU": "Columbus OSU",
        "MNN": "Marion",
        "MGY": "Dayton",
        "ZZV": "Zanesville",
        "TDZ": "Toledo",
        "AOH": "Lima",
        "PHD": "New Philadelphia",
        "HZY": "Ashtabula",
        "DFI": "Defiance",
        "LPR": "Lorain/Elyria",
        "HAO": "Hamilton",
        "LUK": "Cincinnati Lunken",
        "LHQ": "Lancaster",
        "MFD": "Mansfield",
        "TOL": "Toledo",
        "CAK": "Akron/Canton",
        "CMH": "Columbus",
        "YNG": "Youngstown",
        "CLE": "Cleveland",
    },
    "WA": {
        "SEA": "Seattle-Tacoma",
        "GEG": "Spokane",
        "PDT": "Pendleton",
        "YKM": "Yakima",
        "BLI": "Bellingham",
        "ALW": "Walla Walla",
        "RNT": "Renton",
        "BFI": "Boeing Field",
        "PAE": "Paine Field",
        "OLM": "Olympia",
        "PUW": "Pullman",
        "PSC": "Tri-Cities",
        "EAT": "Wenatchee",
        "MWH": "Moses Lake",
        "SFF": "Spokane Felts Field",
        "RLD": "Richland",
        "EPH": "Ephrata",
        "LWS": "Lewiston",
        "SMP": "Stampede Pass",
        "UIL": "Quillayute",
        "CLM": "Port Angeles",
        "HQM": "Hoquiam",
        "AST": "Astoria",
        "KLS": "Kelso",
        "VUO": "Vancouver",
        "TIW": "Tacoma Narrows",
        "AWO": "Arlington",
        "NUW": "Whidbey Island",
        "SKA": "Fairchild AFB",
        "TCM": "McChord AFB",
        "GRF": "Gray Army Airfield",
    },
    "OR": {
        "PDX": "Portland",
        "EUG": "Eugene",
        "MFR": "Medford",
        "RDM": "Redmond",
        "SLE": "Salem",
        "OTH": "North Bend",
        "LMT": "Klamath Falls",
        "ONO": "Ontario",
        "DLS": "The Dalles",
        "HIO": "Hillsboro",
        "TTD": "Portland Troutdale",
        "CVO": "Corvallis",
        "AST": "Astoria",
        "SXT": "Sexton Summit",
        "BNO": "Burns",
        "RBG": "Roseburg",
        "LGD": "La Grande",
        "PFC": "Pacific City",
        "UKI": "Ukiah",
        "S21": "Sunriver",
        "S33": "Scapoose",
        "77S": "Creswell",
        "MMV": "McMinnville",
        "UAO": "Aurora",
        "4S2": "Hood River",
        "1S5": "Sumpter",
        "OAS": "John Day",
        "BKE": "Baker City",
        "P91": "Prineville",
        "20S": "Albany",
    },
}

# Gust severity thresholds (mph)
GUST_THRESHOLD_SIGNIFICANT = 70  # Significant damage likely
GUST_THRESHOLD_SEVERE = 58       # Severe thunderstorm criteria
GUST_THRESHOLD_ADVISORY = 46     # Wind advisory level

# Default states to query if filter_states is empty
# These are the states we have station name mappings for
DEFAULT_GUST_STATES = ["OH", "WA", "OR"]


@dataclass
class WindGustReport:
    """Single wind gust observation."""
    station: str
    city: str
    state: str
    gust_mph: float
    valid_time: datetime
    lat: Optional[float] = None
    lon: Optional[float] = None

    @property
    def severity(self) -> str:
        """Get severity level based on gust speed."""
        if self.gust_mph >= GUST_THRESHOLD_SIGNIFICANT:
            return "significant"
        elif self.gust_mph >= GUST_THRESHOLD_SEVERE:
            return "severe"
        elif self.gust_mph >= GUST_THRESHOLD_ADVISORY:
            return "advisory"
        return "normal"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "station": self.station,
            "city": self.city,
            "state": self.state,
            "gust_mph": self.gust_mph,
            "valid_time": self.valid_time.isoformat(),
            "severity": self.severity,
            "lat": self.lat,
            "lon": self.lon,
        }


@dataclass
class WindGustsService:
    """Service for fetching wind gust data from Iowa State Mesonet."""

    _cache: dict[str, list[WindGustReport]] = field(default_factory=dict)
    _cache_time: Optional[datetime] = None
    _cache_ttl: timedelta = field(default_factory=lambda: timedelta(minutes=5))

    async def fetch_gusts(
        self,
        states: Optional[list[str]] = None,
        hours: int = 1,
        limit: int = 15,
        force_refresh: bool = False,
    ) -> list[WindGustReport]:
        """
        Fetch wind gust data from Iowa State Mesonet ASOS stations.

        Args:
            states: List of state codes to fetch (e.g., ["OH", "WA", "OR"])
                   If None or empty, uses filter_states from settings or defaults
            hours: Number of hours to look back (default: 1)
            limit: Maximum number of results to return (default: 15)
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            List of WindGustReport objects sorted by gust speed (highest first)
        """
        if states is None:
            settings = get_settings()
            states = settings.filter_states

        # If states is empty, use defaults
        if not states:
            states = DEFAULT_GUST_STATES
            logger.info(f"No filter_states configured, using defaults: {states}")

        cache_key = f"{','.join(sorted(states))}_{hours}"

        # Check cache
        if not force_refresh and cache_key in self._cache:
            if self._cache_time and datetime.now(timezone.utc) - self._cache_time < self._cache_ttl:
                logger.debug(f"Returning cached wind gusts for {states}")
                return self._cache[cache_key][:limit]

        all_gusts: list[WindGustReport] = []

        # Fetch from each state's ASOS network
        for state in states:
            try:
                state_gusts = await self._fetch_state_gusts(state, hours)
                all_gusts.extend(state_gusts)
            except Exception as e:
                logger.error(f"Error fetching gusts for {state}: {e}")
                continue

        # Sort by gust speed (highest first)
        all_gusts.sort(key=lambda g: g.gust_mph, reverse=True)

        # Cache results
        self._cache[cache_key] = all_gusts
        self._cache_time = datetime.now(timezone.utc)

        logger.info(f"Fetched {len(all_gusts)} wind gusts from {len(states)} states")
        return all_gusts[:limit]

    async def _fetch_state_gusts(self, state: str, hours: int) -> list[WindGustReport]:
        """Fetch wind gusts for a single state."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)

        # Build URL for Iowa State Mesonet ASOS API
        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"network={state}_ASOS"
            f"&data=gust&data=lat&data=lon"
            f"&format=comma"
            f"&tz=Etc/UTC"
            f"&year1={start.year}&month1={start.month}&day1={start.day}"
            f"&hour1={start.hour}&minute1={start.minute}"
            f"&year2={now.year}&month2={now.month}&day2={now.day}"
            f"&hour2={now.hour}&minute2={now.minute}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as response:
                    if response.status != 200:
                        logger.error(f"Mesonet API returned {response.status} for {state}")
                        return []

                    text = await response.text()
                    return self._parse_csv(text, state)

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching gusts for {state}")
            return []
        except Exception as e:
            logger.exception(f"Error fetching gusts for {state}: {e}")
            return []

    def _parse_csv(self, csv_text: str, state: str) -> list[WindGustReport]:
        """Parse CSV response from Mesonet API."""
        gusts: list[WindGustReport] = []
        lines = csv_text.strip().split("\n")

        # Filter out comment lines and empty lines
        lines = [l for l in lines if l and not l.startswith("#")]

        if len(lines) < 2:
            return gusts

        # Parse header
        headers = lines[0].split(",")

        # Get column indices
        try:
            station_idx = headers.index("station")
            gust_idx = headers.index("gust")
            valid_idx = headers.index("valid")
        except ValueError as e:
            logger.error(f"Missing required column in CSV: {e}")
            return gusts

        # Optional columns
        lat_idx = headers.index("lat") if "lat" in headers else None
        lon_idx = headers.index("lon") if "lon" in headers else None

        # Get station names for this state
        state_stations = STATION_NAMES.get(state, {})

        # Track highest gust per station (to avoid duplicates)
        station_max: dict[str, WindGustReport] = {}

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(station_idx, gust_idx, valid_idx):
                continue

            station = parts[station_idx]
            gust_str = parts[gust_idx]
            valid_str = parts[valid_idx]

            # Skip if no valid gust
            if not gust_str or gust_str == "M" or gust_str == "":
                continue

            try:
                gust_mph = float(gust_str)
                if gust_mph <= 0:
                    continue

                # Parse timestamp
                valid_time = datetime.fromisoformat(valid_str.replace("Z", "+00:00"))

                # Get lat/lon if available
                lat = float(parts[lat_idx]) if lat_idx and lat_idx < len(parts) and parts[lat_idx] else None
                lon = float(parts[lon_idx]) if lon_idx and lon_idx < len(parts) and parts[lon_idx] else None

                # Get city name
                city = state_stations.get(station, station)

                report = WindGustReport(
                    station=station,
                    city=city,
                    state=state,
                    gust_mph=gust_mph,
                    valid_time=valid_time,
                    lat=lat,
                    lon=lon,
                )

                # Keep only the highest gust per station
                if station not in station_max or gust_mph > station_max[station].gust_mph:
                    station_max[station] = report

            except (ValueError, IndexError) as e:
                logger.debug(f"Error parsing gust line: {e}")
                continue

        return list(station_max.values())

    def get_gusts_by_state(self, gusts: list[WindGustReport]) -> dict[str, list[WindGustReport]]:
        """Group gusts by state."""
        by_state: dict[str, list[WindGustReport]] = {}
        for gust in gusts:
            if gust.state not in by_state:
                by_state[gust.state] = []
            by_state[gust.state].append(gust)

        # Sort each state's gusts by gust speed
        for state in by_state:
            by_state[state].sort(key=lambda g: g.gust_mph, reverse=True)

        return by_state

    def get_statistics(self) -> dict[str, Any]:
        """Get service statistics."""
        return {
            "cached_states": list(self._cache.keys()),
            "cache_time": self._cache_time.isoformat() if self._cache_time else None,
            "total_cached_gusts": sum(len(g) for g in self._cache.values()),
        }


# Global service instance
_service: Optional[WindGustsService] = None


def get_wind_gusts_service() -> WindGustsService:
    """Get the global WindGustsService instance."""
    global _service
    if _service is None:
        _service = WindGustsService()
    return _service


async def start_wind_gusts_service():
    """Start the wind gusts service."""
    global _service
    _service = WindGustsService()
    logger.info("Wind gusts service started")


async def stop_wind_gusts_service():
    """Stop the wind gusts service."""
    global _service
    _service = None
    logger.info("Wind gusts service stopped")
