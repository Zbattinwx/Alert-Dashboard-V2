"""
ASOS/METAR Surface Observations Service for Alert Dashboard V2.

Fetches current surface observations (temp, dewpoint, wind, visibility, sky)
from Iowa State Mesonet ASOS stations for configured states.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)

# Fields to request from the Mesonet API
_ASOS_FIELDS = [
    "tmpf",    # Temperature (°F)
    "dwpf",    # Dewpoint (°F)
    "sknt",    # Wind speed (knots)
    "drct",    # Wind direction (degrees)
    "gust",    # Wind gust (knots)
    "vsby",    # Visibility (miles)
    "alti",    # Altimeter (inHg)
    "skyc1",   # Sky condition 1 (CLR/FEW/SCT/BKN/OVC/VV)
    "skyl1",   # Sky level 1 (hundreds of feet)
    "skyc2",   # Sky condition 2
    "wxcodes", # Present weather codes
    "lat",
    "lon",
]

# Sky condition sort order for display
_SKY_ORDER = {"CLR": 0, "SKC": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 5}

# Cardinal direction from degrees
def _deg_to_cardinal(deg: Optional[float]) -> str:
    if deg is None:
        return "—"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


def _knots_to_mph(knots: Optional[float]) -> Optional[float]:
    if knots is None:
        return None
    return round(knots * 1.15078, 1)


@dataclass
class AsosObservation:
    """Single ASOS/METAR surface observation."""
    station: str
    state: str
    lat: Optional[float]
    lon: Optional[float]
    valid_time: datetime
    temp_f: Optional[float]
    dewpoint_f: Optional[float]
    wind_dir_deg: Optional[float]
    wind_speed_mph: Optional[float]
    wind_gust_mph: Optional[float]
    visibility_mi: Optional[float]
    altimeter_inhg: Optional[float]
    sky_condition: Optional[str]   # e.g. "BKN040"
    wx_codes: Optional[str]        # e.g. "-RA BR"

    @property
    def wind_dir_cardinal(self) -> str:
        return _deg_to_cardinal(self.wind_dir_deg)

    @property
    def sky_cover(self) -> str:
        """Highest sky cover layer abbreviation."""
        if not self.sky_condition:
            return "CLR"
        return self.sky_condition[:3].upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "state": self.state,
            "lat": self.lat,
            "lon": self.lon,
            "valid_time": self.valid_time.isoformat(),
            "temp_f": self.temp_f,
            "dewpoint_f": self.dewpoint_f,
            "wind_dir_deg": self.wind_dir_deg,
            "wind_dir_cardinal": self.wind_dir_cardinal,
            "wind_speed_mph": self.wind_speed_mph,
            "wind_gust_mph": self.wind_gust_mph,
            "visibility_mi": self.visibility_mi,
            "altimeter_inhg": self.altimeter_inhg,
            "sky_condition": self.sky_condition,
            "sky_cover": self.sky_cover,
            "wx_codes": self.wx_codes,
        }


@dataclass
class AsosService:
    """Service for fetching ASOS surface observations from Iowa State Mesonet."""

    _cache: dict[str, list[AsosObservation]] = field(default_factory=dict)
    _cache_time: Optional[datetime] = None
    _cache_ttl: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    # Station metadata cache: {station_id: (lat, lon)}
    _station_coords: dict[str, tuple[float, float]] = field(default_factory=dict)
    _stations_fetched: set[str] = field(default_factory=set)

    async def _fetch_network_stations(self, state: str) -> None:
        """Fetch station lat/lon from Iowa State network GeoJSON (authoritative coords)."""
        if state in self._stations_fetched:
            return
        url = f"https://mesonet.agron.iastate.edu/geojson/network/{state}_ASOS.geojson"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json(content_type=None)
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                sid = props.get("sid") or props.get("id")
                geom = feature.get("geometry", {})
                coords = geom.get("coordinates")
                if sid and coords and len(coords) >= 2:
                    # GeoJSON coordinates are [lon, lat]
                    self._station_coords[sid] = (float(coords[1]), float(coords[0]))
            self._stations_fetched.add(state)
            logger.debug(f"Loaded {len(self._station_coords)} station coords for {state}")
        except Exception as e:
            logger.warning(f"Could not fetch network stations for {state}: {e}")

    async def fetch_observations(
        self,
        states: Optional[list[str]] = None,
        hours: int = 1,
        force_refresh: bool = False,
    ) -> list[AsosObservation]:
        """
        Fetch latest ASOS observations for the given states.

        Returns the most recent observation per station, sorted by state then station.
        """
        if not states:
            settings = get_settings()
            states = settings.filter_states or []

        if not states:
            return []

        cache_key = ",".join(sorted(states))
        if not force_refresh and cache_key in self._cache:
            if self._cache_time and datetime.now(timezone.utc) - self._cache_time < self._cache_ttl:
                return self._cache[cache_key]

        # Pre-fetch station coordinates in parallel
        await asyncio.gather(*[self._fetch_network_stations(s) for s in states])

        all_obs: list[AsosObservation] = []
        for state in states:
            try:
                obs = await self._fetch_state(state, hours)
                all_obs.extend(obs)
            except Exception as e:
                logger.error(f"ASOS fetch error for {state}: {e}")

        # Fill missing lat/lon from station metadata
        for obs in all_obs:
            if obs.lat is None or obs.lon is None:
                coords = self._station_coords.get(obs.station)
                if coords:
                    obs.lat, obs.lon = coords

        # Sort by state, then station
        all_obs.sort(key=lambda o: (o.state, o.station))

        self._cache[cache_key] = all_obs
        self._cache_time = datetime.now(timezone.utc)
        logger.info(f"Fetched {len(all_obs)} ASOS observations for {states}")
        return all_obs

    async def _fetch_state(self, state: str, hours: int) -> list[AsosObservation]:
        """Fetch observations for one state."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)

        fields_param = "".join(f"&data={f}" for f in _ASOS_FIELDS)
        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"network={state}_ASOS"
            f"{fields_param}"
            f"&format=comma&tz=Etc/UTC"
            f"&year1={start.year}&month1={start.month}&day1={start.day}"
            f"&hour1={start.hour}&minute1={start.minute}"
            f"&year2={now.year}&month2={now.month}&day2={now.day}"
            f"&hour2={now.hour}&minute2={now.minute}"
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning(f"ASOS API returned {resp.status} for {state}")
                    return []
                text = await resp.text()

        return self._parse_csv(text, state)

    def _parse_csv(self, csv_text: str, state: str) -> list[AsosObservation]:
        """Parse CSV response; keep only the most recent observation per station."""
        lines = [l for l in csv_text.strip().split("\n") if l and not l.startswith("#")]
        if len(lines) < 2:
            return []

        headers = [h.strip() for h in lines[0].split(",")]

        def idx(name: str) -> Optional[int]:
            try:
                return headers.index(name)
            except ValueError:
                return None

        i_station = idx("station")
        i_valid = idx("valid")
        i_tmpf = idx("tmpf")
        i_dwpf = idx("dwpf")
        i_sknt = idx("sknt")
        i_drct = idx("drct")
        i_gust = idx("gust")
        i_vsby = idx("vsby")
        i_alti = idx("alti")
        i_skyc1 = idx("skyc1")
        i_skyl1 = idx("skyl1")
        i_skyc2 = idx("skyc2")
        i_wxcodes = idx("wxcodes")
        i_lat = idx("lat")
        i_lon = idx("lon")

        if i_station is None or i_valid is None:
            return []

        def _float(parts: list[str], i: Optional[int]) -> Optional[float]:
            if i is None or i >= len(parts):
                return None
            v = parts[i].strip()
            if not v or v in ("M", "T", ""):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        def _str(parts: list[str], i: Optional[int]) -> Optional[str]:
            if i is None or i >= len(parts):
                return None
            v = parts[i].strip()
            return v if v and v != "M" else None

        # Latest observation per station
        latest: dict[str, AsosObservation] = {}

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(filter(lambda x: x is not None, [i_station, i_valid])):
                continue

            station = parts[i_station].strip() if i_station is not None else ""
            valid_str = parts[i_valid].strip() if i_valid is not None else ""
            if not station or not valid_str:
                continue

            try:
                valid_time = datetime.fromisoformat(valid_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            # Build sky condition string (e.g. "BKN040")
            sky = None
            skyc1 = _str(parts, i_skyc1)
            skyl1 = _float(parts, i_skyl1)
            skyc2 = _str(parts, i_skyc2)
            if skyc1 and skyc1 not in ("CLR", "SKC"):
                level = f"{int(skyl1):03d}" if skyl1 is not None else "???"
                sky = f"{skyc1}{level}"
                # Also check layer 2
                if skyc2 and skyc2 not in ("CLR", "SKC"):
                    sky = f"{skyc1}{level}/{skyc2}"
            elif skyc1:
                sky = skyc1

            obs = AsosObservation(
                station=station,
                state=state,
                lat=_float(parts, i_lat),
                lon=_float(parts, i_lon),
                valid_time=valid_time,
                temp_f=_float(parts, i_tmpf),
                dewpoint_f=_float(parts, i_dwpf),
                wind_dir_deg=_float(parts, i_drct),
                wind_speed_mph=_knots_to_mph(_float(parts, i_sknt)),
                wind_gust_mph=_knots_to_mph(_float(parts, i_gust)),
                visibility_mi=_float(parts, i_vsby),
                altimeter_inhg=_float(parts, i_alti),
                sky_condition=sky,
                wx_codes=_str(parts, i_wxcodes),
            )

            # Keep most recent per station
            existing = latest.get(station)
            if existing is None or valid_time > existing.valid_time:
                latest[station] = obs

        return list(latest.values())

    def get_by_state(self, obs: list[AsosObservation]) -> dict[str, list[AsosObservation]]:
        result: dict[str, list[AsosObservation]] = {}
        for o in obs:
            result.setdefault(o.state, []).append(o)
        for state_obs in result.values():
            state_obs.sort(key=lambda o: o.station)
        return result


# Global singleton
_service: Optional[AsosService] = None


def get_asos_service() -> AsosService:
    global _service
    if _service is None:
        _service = AsosService()
    return _service


async def start_asos_service():
    global _service
    _service = AsosService()
    logger.info("ASOS service started")


async def stop_asos_service():
    global _service
    _service = None
    logger.info("ASOS service stopped")
