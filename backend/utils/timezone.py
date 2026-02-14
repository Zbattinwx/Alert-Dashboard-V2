"""
Timezone handling utilities for Alert Dashboard V2.

This module provides robust timezone parsing and conversion, addressing the issues
identified in V1 where unrecognized timezones silently defaulted to UTC.

Key improvements:
- Never silently default to UTC - always log warnings
- Comprehensive timezone abbreviation mapping
- IANA timezone support via zoneinfo
- WFO-to-timezone mapping for office-based alerts
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


# Comprehensive timezone abbreviation mapping
# Note: Some abbreviations are ambiguous (e.g., CST = Central Standard, China Standard)
# This mapping is specific to NWS alerts (US-focused)
TIMEZONE_ABBREVIATIONS: dict[str, timezone | ZoneInfo] = {
    # US Eastern
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "ET": timezone(timedelta(hours=-5)),  # Assume standard time

    # US Central
    "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "CT": timezone(timedelta(hours=-6)),

    # US Mountain
    "MST": timezone(timedelta(hours=-7)),
    "MDT": timezone(timedelta(hours=-6)),
    "MT": timezone(timedelta(hours=-7)),

    # US Pacific
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "PT": timezone(timedelta(hours=-8)),

    # Alaska
    "AKST": timezone(timedelta(hours=-9)),
    "AKDT": timezone(timedelta(hours=-8)),
    "AKT": timezone(timedelta(hours=-9)),

    # Hawaii-Aleutian
    "HST": timezone(timedelta(hours=-10)),
    "HDT": timezone(timedelta(hours=-9)),
    "HAST": timezone(timedelta(hours=-10)),
    "HADT": timezone(timedelta(hours=-9)),

    # Atlantic
    "AST": timezone(timedelta(hours=-4)),
    "ADT": timezone(timedelta(hours=-3)),

    # Chamorro (Guam)
    "ChST": timezone(timedelta(hours=10)),

    # Samoa
    "SST": timezone(timedelta(hours=-11)),

    # UTC variants
    "UTC": timezone.utc,
    "GMT": timezone.utc,
    "Z": timezone.utc,
}


# WFO (Weather Forecast Office) to IANA timezone mapping
# This allows determining timezone from the issuing office
WFO_TIMEZONES: dict[str, str] = {
    # Eastern Time offices
    "CLE": "America/New_York",      # Cleveland, OH
    "ILN": "America/New_York",      # Wilmington, OH
    "PBZ": "America/New_York",      # Pittsburgh, PA
    "RLX": "America/New_York",      # Charleston, WV
    "BUF": "America/New_York",      # Buffalo, NY
    "BGM": "America/New_York",      # Binghamton, NY
    "ALY": "America/New_York",      # Albany, NY
    "OKX": "America/New_York",      # New York, NY
    "PHI": "America/New_York",      # Philadelphia, PA
    "LWX": "America/New_York",      # Baltimore/Washington
    "RNK": "America/New_York",      # Blacksburg, VA
    "AKQ": "America/New_York",      # Wakefield, VA
    "MHX": "America/New_York",      # Newport/Morehead City, NC
    "RAH": "America/New_York",      # Raleigh, NC
    "ILM": "America/New_York",      # Wilmington, NC
    "CAE": "America/New_York",      # Columbia, SC
    "CHS": "America/New_York",      # Charleston, SC
    "GSP": "America/New_York",      # Greenville-Spartanburg, SC
    "FFC": "America/New_York",      # Peachtree City/Atlanta, GA
    "JAX": "America/New_York",      # Jacksonville, FL
    "MLB": "America/New_York",      # Melbourne, FL
    "MFL": "America/New_York",      # Miami, FL
    "TBW": "America/New_York",      # Tampa Bay, FL
    "TAE": "America/New_York",      # Tallahassee, FL
    "CAR": "America/New_York",      # Caribou, ME
    "GYX": "America/New_York",      # Gray/Portland, ME
    "BOX": "America/New_York",      # Boston, MA
    "MRX": "America/New_York",      # Morristown, TN
    "HUN": "America/Chicago",       # Huntsville, AL (Central)
    "BMX": "America/Chicago",       # Birmingham, AL
    "MOB": "America/Chicago",       # Mobile, AL
    "JAN": "America/Chicago",       # Jackson, MS
    "MEG": "America/Chicago",       # Memphis, TN
    "OHX": "America/Chicago",       # Nashville, TN
    "PAH": "America/Chicago",       # Paducah, KY
    "LMK": "America/New_York",      # Louisville, KY
    "JKL": "America/New_York",      # Jackson, KY

    # Central Time offices
    "IWX": "America/Indiana/Indianapolis",  # Northern Indiana
    "IND": "America/Indiana/Indianapolis",  # Indianapolis, IN
    "LOT": "America/Chicago",       # Chicago, IL
    "ILX": "America/Chicago",       # Lincoln, IL
    "DVN": "America/Chicago",       # Quad Cities, IA/IL
    "DMX": "America/Chicago",       # Des Moines, IA
    "ARX": "America/Chicago",       # La Crosse, WI
    "MKX": "America/Chicago",       # Milwaukee, WI
    "GRB": "America/Chicago",       # Green Bay, WI
    "MPX": "America/Chicago",       # Minneapolis, MN
    "DLH": "America/Chicago",       # Duluth, MN
    "FGF": "America/Chicago",       # Grand Forks, ND
    "BIS": "America/Chicago",       # Bismarck, ND
    "ABR": "America/Chicago",       # Aberdeen, SD
    "FSD": "America/Chicago",       # Sioux Falls, SD
    "UNR": "America/Denver",        # Rapid City, SD (Mountain)
    "OAX": "America/Chicago",       # Omaha, NE
    "GID": "America/Chicago",       # Hastings, NE
    "LBF": "America/Chicago",       # North Platte, NE
    "CYS": "America/Denver",        # Cheyenne, WY (Mountain)
    "TOP": "America/Chicago",       # Topeka, KS
    "ICT": "America/Chicago",       # Wichita, KS
    "DDC": "America/Chicago",       # Dodge City, KS
    "GLD": "America/Chicago",       # Goodland, KS
    "OUN": "America/Chicago",       # Norman/Oklahoma City, OK
    "TSA": "America/Chicago",       # Tulsa, OK
    "SHV": "America/Chicago",       # Shreveport, LA
    "LCH": "America/Chicago",       # Lake Charles, LA
    "LIX": "America/Chicago",       # New Orleans, LA
    "FWD": "America/Chicago",       # Dallas/Fort Worth, TX
    "EWX": "America/Chicago",       # Austin/San Antonio, TX
    "HGX": "America/Chicago",       # Houston, TX
    "CRP": "America/Chicago",       # Corpus Christi, TX
    "BRO": "America/Chicago",       # Brownsville, TX
    "SJT": "America/Chicago",       # San Angelo, TX
    "MAF": "America/Chicago",       # Midland/Odessa, TX
    "LUB": "America/Chicago",       # Lubbock, TX
    "AMA": "America/Chicago",       # Amarillo, TX
    "SGF": "America/Chicago",       # Springfield, MO
    "LSX": "America/Chicago",       # St. Louis, MO
    "EAX": "America/Chicago",       # Kansas City, MO
    "LZK": "America/Chicago",       # Little Rock, AR

    # Mountain Time offices
    "BOU": "America/Denver",        # Denver/Boulder, CO
    "GJT": "America/Denver",        # Grand Junction, CO
    "PUB": "America/Denver",        # Pueblo, CO
    "ABQ": "America/Denver",        # Albuquerque, NM
    "EPZ": "America/Denver",        # El Paso, TX / Santa Teresa, NM
    "PHX": "America/Phoenix",       # Phoenix, AZ (no DST)
    "FGZ": "America/Phoenix",       # Flagstaff, AZ
    "TWC": "America/Phoenix",       # Tucson, AZ
    "SLC": "America/Denver",        # Salt Lake City, UT
    "RIW": "America/Denver",        # Riverton, WY
    "BYZ": "America/Denver",        # Billings, MT
    "TFX": "America/Denver",        # Great Falls, MT
    "MSO": "America/Denver",        # Missoula, MT
    "GGW": "America/Denver",        # Glasgow, MT
    "PIH": "America/Boise",         # Pocatello, ID
    "BOI": "America/Boise",         # Boise, ID
    "LKN": "America/Los_Angeles",   # Elko, NV (Pacific)
    "VEF": "America/Los_Angeles",   # Las Vegas, NV
    "REV": "America/Los_Angeles",   # Reno, NV

    # Pacific Time offices
    "SEW": "America/Los_Angeles",   # Seattle, WA
    "OTX": "America/Los_Angeles",   # Spokane, WA
    "PDT": "America/Los_Angeles",   # Pendleton, OR
    "PQR": "America/Los_Angeles",   # Portland, OR
    "MFR": "America/Los_Angeles",   # Medford, OR
    "EKA": "America/Los_Angeles",   # Eureka, CA
    "STO": "America/Los_Angeles",   # Sacramento, CA
    "MTR": "America/Los_Angeles",   # San Francisco Bay, CA
    "HNX": "America/Los_Angeles",   # Hanford/San Joaquin, CA
    "LOX": "America/Los_Angeles",   # Los Angeles, CA
    "SGX": "America/Los_Angeles",   # San Diego, CA

    # Alaska offices
    "AFC": "America/Anchorage",     # Anchorage, AK
    "AFG": "America/Anchorage",     # Fairbanks, AK
    "AJK": "America/Juneau",        # Juneau, AK

    # Pacific territories
    "HFO": "Pacific/Honolulu",      # Honolulu, HI
    "GUM": "Pacific/Guam",          # Guam
    "PPG": "Pacific/Pago_Pago",     # Pago Pago, American Samoa

    # Puerto Rico / Virgin Islands
    "SJU": "America/Puerto_Rico",   # San Juan, PR
}


class TimezoneHelper:
    """Helper class for timezone operations."""

    @staticmethod
    def parse_timezone_abbreviation(abbrev: str) -> Optional[timezone | ZoneInfo]:
        """
        Parse a timezone abbreviation to a timezone object.

        Args:
            abbrev: Timezone abbreviation (e.g., "EST", "CDT", "UTC")

        Returns:
            timezone object if recognized, None otherwise

        Unlike V1, this method does NOT silently default to UTC.
        """
        if not abbrev:
            logger.warning("Empty timezone abbreviation provided")
            return None

        abbrev_upper = abbrev.upper().strip()

        if abbrev_upper in TIMEZONE_ABBREVIATIONS:
            return TIMEZONE_ABBREVIATIONS[abbrev_upper]

        logger.warning(f"Unrecognized timezone abbreviation: '{abbrev}'")
        return None

    @staticmethod
    def get_timezone_for_wfo(wfo_code: str) -> Optional[ZoneInfo]:
        """
        Get IANA timezone for a Weather Forecast Office.

        Args:
            wfo_code: WFO code (e.g., "CLE", "ILN")

        Returns:
            ZoneInfo object if WFO is recognized, None otherwise
        """
        if not wfo_code:
            logger.warning("Empty WFO code provided")
            return None

        # Handle "K" prefix (e.g., "KCLE" -> "CLE")
        clean_code = wfo_code.upper().strip()
        if clean_code.startswith("K") and len(clean_code) == 4:
            clean_code = clean_code[1:]

        iana_tz = WFO_TIMEZONES.get(clean_code)
        if iana_tz:
            try:
                return ZoneInfo(iana_tz)
            except ZoneInfoNotFoundError:
                logger.error(f"IANA timezone not found: {iana_tz}")
                return None

        logger.warning(f"Unrecognized WFO code: '{wfo_code}'")
        return None

    @staticmethod
    def parse_vtec_timestamp(timestamp_str: str) -> Optional[datetime]:
        """
        Parse a VTEC timestamp string to datetime.

        VTEC format: yymmddThhnnZ (e.g., "250120T1530Z")

        The "000000T0000Z" value indicates undefined/indeterminate time.

        Args:
            timestamp_str: VTEC timestamp string

        Returns:
            datetime in UTC if valid, None if undefined or invalid
        """
        if not timestamp_str:
            return None

        # Handle undefined time (pyIEM pattern)
        if timestamp_str.startswith("0000"):
            logger.debug(f"VTEC timestamp is undefined: {timestamp_str}")
            return None

        # Remove the 'Z' suffix if present
        clean_ts = timestamp_str.rstrip("Z").strip()

        # Expected format: yymmddThhmm
        pattern = r"^(\d{2})(\d{2})(\d{2})T(\d{2})(\d{2})$"
        match = re.match(pattern, clean_ts)

        if not match:
            logger.warning(f"Invalid VTEC timestamp format: '{timestamp_str}'")
            return None

        try:
            yy, mm, dd, hh, nn = map(int, match.groups())

            # Handle 2-digit year (assume 2000s for now, handle rollover)
            # If year > 70, assume 1900s (shouldn't happen for weather alerts)
            year = 2000 + yy if yy < 70 else 1900 + yy

            # Validate ranges
            if not (1 <= mm <= 12):
                logger.warning(f"Invalid month in VTEC timestamp: {mm}")
                return None
            if not (1 <= dd <= 31):
                logger.warning(f"Invalid day in VTEC timestamp: {dd}")
                return None
            if not (0 <= hh <= 23):
                logger.warning(f"Invalid hour in VTEC timestamp: {hh}")
                return None
            if not (0 <= nn <= 59):
                logger.warning(f"Invalid minute in VTEC timestamp: {nn}")
                return None

            dt = datetime(year, mm, dd, hh, nn, tzinfo=timezone.utc)

            # Sanity check: reject dates before 1971 (NWS system bug workaround from pyIEM)
            if dt.year < 1971:
                logger.warning(f"VTEC timestamp year too old, likely invalid: {dt}")
                return None

            return dt

        except ValueError as e:
            logger.warning(f"Failed to parse VTEC timestamp '{timestamp_str}': {e}")
            return None

    @staticmethod
    def parse_iso_timestamp(timestamp_str: str) -> Optional[datetime]:
        """
        Parse an ISO 8601 timestamp string to datetime.

        Handles various ISO formats including:
        - 2025-01-20T15:30:00Z
        - 2025-01-20T15:30:00+00:00
        - 2025-01-20T15:30:00-05:00

        Args:
            timestamp_str: ISO timestamp string

        Returns:
            datetime with timezone info if valid, None otherwise
        """
        if not timestamp_str:
            return None

        try:
            # Python 3.11+ handles most ISO formats directly
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            return dt
        except ValueError:
            pass

        # Try alternate formats
        formats = [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(timestamp_str, fmt)
                # If no timezone info, assume UTC
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

        logger.warning(f"Failed to parse ISO timestamp: '{timestamp_str}'")
        return None

    @staticmethod
    def parse_nwws_timestamp(text: str) -> Optional[datetime]:
        """
        Parse a NWWS-style timestamp from raw alert text.
        Example: "339 PM CDT Mon Aug 8 2022"
        """
        # Import here to avoid circular dependency
        from ..parsers.patterns import PATTERN_ISSUED_TIME_LINE

        match = PATTERN_ISSUED_TIME_LINE.search(text)
        if not match:
            return None

        try:
            time_val, am_pm, tz, _, month_name, day_num, year = match.groups()

            # We need to handle time like "339" vs "1039"
            if len(time_val) == 3:
                time_val = f"0{time_val}"

            # Reconstruct for strptime
            ts_string = f"{time_val} {am_pm} {month_name} {day_num} {year}"
            fmt = "%I%M %p %b %d %Y"

            # Parse the date and time part
            dt = datetime.strptime(ts_string, fmt)

            # Get the timezone info
            tz_info = TimezoneHelper.parse_timezone_abbreviation(tz)
            if not tz_info:
                logger.warning(f"Could not parse timezone '{tz}' from NWWS timestamp, using UTC.")
                tz_info = timezone.utc

            return dt.replace(tzinfo=tz_info)

        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse NWWS timestamp '{match.group(0)}': {e}")
            return None

    @staticmethod
    def parse_text_time(
        time_str: str,
        am_pm: Optional[str],
        tz_abbrev: Optional[str],
        reference_date: Optional[datetime] = None
    ) -> Optional[datetime]:
        """
        Parse a human-readable time from alert text.

        Handles formats like:
        - "530 PM EST"
        - "1145 AM CDT"
        - "830 PM"

        Args:
            time_str: Time string (e.g., "530", "1145")
            am_pm: "AM" or "PM" (optional)
            tz_abbrev: Timezone abbreviation (e.g., "EST")
            reference_date: Reference date for the time (defaults to today)

        Returns:
            datetime if parsed successfully, None otherwise
        """
        if not time_str:
            return None

        try:
            # Normalize time string
            time_str = time_str.strip().zfill(4)

            if len(time_str) == 3:
                time_str = "0" + time_str
            elif len(time_str) > 4:
                logger.warning(f"Unusual time string length: '{time_str}'")
                time_str = time_str[:4]

            hour = int(time_str[:2])
            minute = int(time_str[2:4])

            # Validate ranges
            if not (0 <= hour <= 23 if not am_pm else 1 <= hour <= 12):
                logger.warning(f"Invalid hour value: {hour}")
                return None
            if not (0 <= minute <= 59):
                logger.warning(f"Invalid minute value: {minute}")
                return None

            # Handle AM/PM
            if am_pm:
                am_pm_upper = am_pm.upper()
                if am_pm_upper == "PM" and hour != 12:
                    hour += 12
                elif am_pm_upper == "AM" and hour == 12:
                    hour = 0

            # Get timezone
            tz_info: timezone | ZoneInfo | None = timezone.utc
            if tz_abbrev:
                parsed_tz = TimezoneHelper.parse_timezone_abbreviation(tz_abbrev)
                if parsed_tz:
                    tz_info = parsed_tz
                else:
                    logger.warning(
                        f"Could not parse timezone '{tz_abbrev}', "
                        f"using UTC as fallback (this may cause timing errors)"
                    )

            # Use reference date or current date in the target timezone
            if reference_date:
                base_date = reference_date
            else:
                if isinstance(tz_info, ZoneInfo):
                    base_date = datetime.now(tz_info)
                else:
                    base_date = datetime.now(tz_info)

            # Create datetime
            result = base_date.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0
            )

            # If the result is in the past, it might be for tomorrow
            now = datetime.now(tz_info) if isinstance(tz_info, (timezone, ZoneInfo)) else datetime.now(timezone.utc)
            if result < now:
                result += timedelta(days=1)

            return result

        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse text time '{time_str} {am_pm} {tz_abbrev}': {e}")
            return None

    @staticmethod
    def to_utc(dt: datetime) -> datetime:
        """
        Convert a datetime to UTC.

        Args:
            dt: datetime to convert

        Returns:
            datetime in UTC
        """
        if dt.tzinfo is None:
            logger.warning("Converting naive datetime to UTC, assuming it was already UTC")
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def to_local(dt: datetime, tz: ZoneInfo | timezone | str) -> datetime:
        """
        Convert a datetime to a local timezone.

        Args:
            dt: datetime to convert
            tz: Target timezone (ZoneInfo, timezone, or IANA string)

        Returns:
            datetime in the target timezone
        """
        if isinstance(tz, str):
            try:
                tz = ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                logger.error(f"Unknown timezone: {tz}")
                return dt

        if dt.tzinfo is None:
            logger.warning("Converting naive datetime, assuming UTC")
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(tz)

    @staticmethod
    def format_for_display(
        dt: datetime,
        tz: Optional[ZoneInfo | timezone | str] = None,
        include_tz: bool = True
    ) -> str:
        """
        Format a datetime for display.

        Args:
            dt: datetime to format
            tz: Target timezone for display (optional)
            include_tz: Whether to include timezone abbreviation

        Returns:
            Formatted string like "3:30 PM EST"
        """
        if tz:
            dt = TimezoneHelper.to_local(dt, tz)

        time_str = dt.strftime("%-I:%M %p" if hasattr(dt, 'strftime') else "%I:%M %p").lstrip("0")

        if include_tz and dt.tzinfo:
            tz_name = dt.strftime("%Z")
            return f"{time_str} {tz_name}"

        return time_str
