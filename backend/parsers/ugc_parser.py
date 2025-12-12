"""
UGC (Universal Geographic Code) parser for Alert Dashboard V2.

This module handles parsing of UGC codes from NWS alerts, including:
- County codes (SSC###)
- Zone codes (SSZ###)
- Range expansions (001>005)
- Multi-line continuations
- FIPS code conversion

References:
- NWS UGC Documentation: https://www.weather.gov/emwin/winugc.htm
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from .patterns import (
    PATTERN_UGC_LINE,
    PATTERN_UGC_CODE,
    PATTERN_UGC_RANGE,
    PATTERN_UGC_EXPIRATION,
    PATTERN_XML_FIPS,
)

logger = logging.getLogger(__name__)


@dataclass
class UGCData:
    """
    Parsed UGC data with all extracted codes.
    """
    ugc_codes: list[str] = field(default_factory=list)    # Full codes like "OHC049"
    fips_codes: list[str] = field(default_factory=list)   # 5-digit FIPS codes
    states: set[str] = field(default_factory=set)         # 2-char state codes
    expiration_time: Optional[datetime] = None
    raw_ugc_block: str = ""
    is_valid: bool = False
    validation_warnings: list[str] = field(default_factory=list)


class UGCParser:
    """Parser for UGC (Universal Geographic Code) strings."""

    # State FIPS code prefixes (2-digit state codes)
    STATE_FIPS = {
        "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
        "CO": "08", "CT": "09", "DE": "10", "DC": "11", "FL": "12",
        "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18",
        "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23",
        "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28",
        "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
        "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
        "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
        "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
        "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55",
        "WY": "56", "AS": "60", "GU": "66", "MP": "69", "PR": "72",
        "VI": "78",
    }

    @classmethod
    def parse(cls, text: str) -> UGCData:
        """
        Parse UGC codes from alert text.

        Handles:
        - Single line: OHC049-041-061-201530-
        - Multi-line continuations
        - Range expansions: OHC001>005 = OHC001, OHC002, OHC003, OHC004, OHC005
        - Mixed county and zone codes

        Args:
            text: Raw alert text

        Returns:
            UGCData with extracted codes
        """
        result = UGCData()

        # Find all UGC lines
        ugc_codes = []
        current_prefix = None

        lines = text.split('\n')
        in_ugc_block = False

        for line in lines:
            line = line.strip()

            # Check if this looks like a UGC line
            if cls._is_ugc_line(line):
                in_ugc_block = True
                result.raw_ugc_block += line + "\n"

                # Parse the line
                codes, prefix, expiration = cls._parse_ugc_line(line, current_prefix)
                ugc_codes.extend(codes)

                if prefix:
                    current_prefix = prefix
                if expiration:
                    result.expiration_time = expiration

            elif in_ugc_block and line and not line.startswith('.'):
                # Check for continuation line (just numbers and dashes)
                if re.match(r'^[\d\->]+[-]$', line):
                    result.raw_ugc_block += line + "\n"
                    codes, _, expiration = cls._parse_ugc_line(line, current_prefix)
                    ugc_codes.extend(codes)
                    if expiration:
                        result.expiration_time = expiration
                else:
                    # End of UGC block
                    in_ugc_block = False

        # Deduplicate and sort
        result.ugc_codes = sorted(list(set(ugc_codes)))

        # Extract states
        for code in result.ugc_codes:
            if len(code) >= 2:
                result.states.add(code[:2])

        # Convert to FIPS codes
        result.fips_codes = cls.ugc_to_fips(result.ugc_codes)

        result.is_valid = len(result.ugc_codes) > 0

        return result

    @classmethod
    def _is_ugc_line(cls, line: str) -> bool:
        """Check if a line appears to be a UGC line."""
        # UGC lines start with 2 letters + C or Z + 3 digits
        return bool(re.match(r'^[A-Z]{2}[CZ]\d{3}', line))

    @classmethod
    def _parse_ugc_line(
        cls,
        line: str,
        current_prefix: Optional[str] = None
    ) -> tuple[list[str], Optional[str], Optional[datetime]]:
        """
        Parse a single UGC line.

        Args:
            line: UGC line text
            current_prefix: Prefix from previous line for continuations

        Returns:
            Tuple of (codes, new_prefix, expiration_time)
        """
        codes = []
        new_prefix = None
        expiration = None

        # Use current_prefix as the working prefix, update as we find new ones
        working_prefix = current_prefix

        # Remove trailing dash and whitespace
        line = line.strip().rstrip('-')

        # Check for expiration timestamp at end (DDHHMM)
        exp_match = PATTERN_UGC_EXPIRATION.search(line + '-')
        if exp_match:
            exp_str = exp_match.group(1)
            expiration = cls._parse_ugc_expiration(exp_str)
            # Remove expiration from line for further parsing
            line = line[:exp_match.start()].rstrip('-')

        # Split by dash
        parts = line.split('-')

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Check for state+type prefix (e.g., "OHC" or "OHZ")
            prefix_match = re.match(r'^([A-Z]{2}[CZ])(.*)$', part)
            if prefix_match:
                # Found a new prefix - update working prefix
                working_prefix = prefix_match.group(1)
                new_prefix = working_prefix  # Return this as the line's prefix
                remainder = prefix_match.group(2)

                if remainder:
                    # Parse the remainder (codes after prefix)
                    codes.extend(cls._expand_codes(working_prefix, remainder))
            elif working_prefix:
                # Use working prefix for this part (continuation within same line)
                codes.extend(cls._expand_codes(working_prefix, part))
            else:
                # Standalone number without prefix - skip
                if part.isdigit() and len(part) == 3:
                    logger.warning(f"UGC code '{part}' has no prefix context")

        return codes, new_prefix, expiration

    @classmethod
    def _expand_codes(cls, prefix: str, code_str: str) -> list[str]:
        """
        Expand code string with prefix into full UGC codes.

        Handles:
        - Single: "049" -> ["OHC049"]
        - Multiple: "049041061" -> ["OHC049", "OHC041", "OHC061"]
        - Ranges: "001>005" -> ["OHC001", "OHC002", "OHC003", "OHC004", "OHC005"]

        Args:
            prefix: UGC prefix (e.g., "OHC")
            code_str: Code portion (e.g., "049041>045")

        Returns:
            List of full UGC codes
        """
        codes = []

        # Check for range first
        range_match = PATTERN_UGC_RANGE.search(code_str)
        if range_match:
            start_str, end_str = range_match.groups()
            start = int(start_str)
            end = int(end_str)

            # Validate range (start should be <= end)
            if start > end:
                logger.warning(f"UGC range start ({start}) > end ({end}), swapping")
                start, end = end, start

            # Expand range
            for i in range(start, end + 1):
                codes.append(f"{prefix}{i:03d}")

            # Also parse any parts before/after the range
            before = code_str[:range_match.start()]
            after = code_str[range_match.end():]

            if before:
                codes.extend(cls._expand_codes(prefix, before))
            if after:
                codes.extend(cls._expand_codes(prefix, after))

        else:
            # No range - parse individual 3-digit codes
            # Handle cases like "049041061" (multiple codes concatenated)
            code_matches = re.findall(r'(\d{3})', code_str)
            for code in code_matches:
                codes.append(f"{prefix}{code}")

        return codes

    @classmethod
    def _parse_ugc_expiration(cls, exp_str: str) -> Optional[datetime]:
        """
        Parse UGC expiration timestamp.

        Format: DDHHMM (day, hour, minute in UTC)

        Args:
            exp_str: 6-digit expiration string

        Returns:
            datetime in UTC if valid, None otherwise
        """
        if not exp_str or len(exp_str) != 6:
            return None

        try:
            day = int(exp_str[0:2])
            hour = int(exp_str[2:4])
            minute = int(exp_str[4:6])

            # Validate ranges
            if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
                logger.warning(f"Invalid UGC expiration values: day={day}, hour={hour}, min={minute}")
                return None

            # Build datetime using current month/year
            now = datetime.now(timezone.utc)
            exp_time = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)

            # If expiration is in the past, it might be next month
            if exp_time < now:
                # Add a month
                if exp_time.month == 12:
                    exp_time = exp_time.replace(year=exp_time.year + 1, month=1)
                else:
                    exp_time = exp_time.replace(month=exp_time.month + 1)

            return exp_time

        except ValueError as e:
            logger.warning(f"Failed to parse UGC expiration '{exp_str}': {e}")
            return None

    @classmethod
    def ugc_to_fips(cls, ugc_codes: list[str]) -> list[str]:
        """
        Convert UGC codes to FIPS codes.

        Note: This is a simple conversion for county codes (SSC###).
        Zone codes (SSZ###) require a lookup table for accurate conversion.

        Args:
            ugc_codes: List of UGC codes

        Returns:
            List of 5-digit FIPS codes
        """
        fips_codes = []

        for ugc in ugc_codes:
            if len(ugc) != 6:
                continue

            state = ugc[:2]
            code_type = ugc[2]
            county_num = ugc[3:6]

            if state not in cls.STATE_FIPS:
                logger.warning(f"Unknown state in UGC code: {ugc}")
                continue

            state_fips = cls.STATE_FIPS[state]

            if code_type == 'C':
                # County code - direct conversion
                fips = f"{state_fips}{county_num}"
                fips_codes.append(fips)
            elif code_type == 'Z':
                # Zone code - would need lookup table for accurate conversion
                # For now, skip zones or use a mapping if available
                pass

        return sorted(list(set(fips_codes)))

    @classmethod
    def parse_xml_fips(cls, text: str) -> list[str]:
        """
        Extract FIPS codes from XML/CAP content.

        Args:
            text: XML text containing FIPS codes

        Returns:
            List of normalized 5-digit FIPS codes
        """
        fips_codes = []

        matches = PATTERN_XML_FIPS.findall(text)
        for code in matches:
            # Normalize to 5 digits (SAME codes are 6 digits)
            if len(code) == 6:
                # Take last 5 digits
                normalized = code[-5:]
            elif len(code) == 5:
                normalized = code
            else:
                logger.warning(f"Unexpected FIPS code length: {code}")
                continue

            # Ensure proper zero-padding
            normalized = normalized.zfill(5)
            fips_codes.append(normalized)

        return sorted(list(set(fips_codes)))

    @classmethod
    def get_state_from_ugc(cls, ugc_code: str) -> Optional[str]:
        """Extract state abbreviation from UGC code."""
        if len(ugc_code) >= 2:
            return ugc_code[:2]
        return None

    @classmethod
    def is_county_code(cls, ugc_code: str) -> bool:
        """Check if UGC code is a county code."""
        return len(ugc_code) == 6 and ugc_code[2] == 'C'

    @classmethod
    def is_zone_code(cls, ugc_code: str) -> bool:
        """Check if UGC code is a zone code."""
        return len(ugc_code) == 6 and ugc_code[2] == 'Z'

    @classmethod
    def filter_by_states(cls, ugc_codes: list[str], states: list[str]) -> list[str]:
        """
        Filter UGC codes to only include specified states.

        Args:
            ugc_codes: List of UGC codes
            states: List of state abbreviations to include

        Returns:
            Filtered list of UGC codes
        """
        states_upper = {s.upper() for s in states}
        return [ugc for ugc in ugc_codes if ugc[:2] in states_upper]

    @classmethod
    def format_location_string(cls, ugc_codes: list[str]) -> str:
        """
        Create a human-readable location string from UGC codes.

        Args:
            ugc_codes: List of UGC codes

        Returns:
            String like "OH (3 counties), IN (2 zones)"
        """
        if not ugc_codes:
            return "Unknown"

        # Group by state and type
        state_counts: dict[str, dict[str, int]] = {}

        for ugc in ugc_codes:
            if len(ugc) != 6:
                continue

            state = ugc[:2]
            code_type = "counties" if ugc[2] == 'C' else "zones"

            if state not in state_counts:
                state_counts[state] = {"counties": 0, "zones": 0}
            state_counts[state][code_type] += 1

        # Format string
        parts = []
        for state in sorted(state_counts.keys()):
            counts = state_counts[state]
            type_parts = []
            if counts["counties"] > 0:
                type_parts.append(f"{counts['counties']} {'county' if counts['counties'] == 1 else 'counties'}")
            if counts["zones"] > 0:
                type_parts.append(f"{counts['zones']} {'zone' if counts['zones'] == 1 else 'zones'}")
            parts.append(f"{state} ({', '.join(type_parts)})")

        return ", ".join(parts)
