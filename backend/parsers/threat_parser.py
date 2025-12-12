"""
Threat data parser for Alert Dashboard V2.

This module extracts threat information from NWS alerts including:
- Tornado detection and damage threat
- Wind gust speeds and damage threat
- Hail size and damage threat
- Snow/ice accumulation
- Storm motion
- Flash flood information

Improvements over V1:
- More specific pattern matching order (most specific first)
- Better handling of unit conversions
- Validation of extracted values
- Comprehensive logging
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from .patterns import (
    PATTERN_TORNADO_DETECTION,
    PATTERN_TORNADO_DAMAGE,
    PATTERN_WIND_GUST,
    PATTERN_WIND_XML,
    PATTERN_WIND_DAMAGE,
    PATTERN_HAIL_SIZE,
    PATTERN_HAIL_XML,
    PATTERN_HAIL_DAMAGE,
    PATTERN_HAIL_DESC,
    HAIL_SIZE_DESCRIPTIONS,
    PATTERN_SNOW_AMOUNT,
    PATTERN_ICE_AMOUNT,
    PATTERN_MOTION_TEXT,
    PATTERN_MOTION_XML,
    PATTERN_MOTION_ALT,
    CARDINAL_TO_DEGREES,
    PATTERN_FLOOD_DETECTION,
    PATTERN_FLOOD_DAMAGE,
)
from ..models.alert import ThreatData, StormMotion

logger = logging.getLogger(__name__)


class ThreatParser:
    """Parser for extracting threat data from alert text."""

    @classmethod
    def parse(cls, text: str, is_xml: bool = False) -> ThreatData:
        """
        Parse all threat data from alert text.

        Args:
            text: Alert text (raw NWWS or XML/CAP)
            is_xml: Whether the text is XML format

        Returns:
            ThreatData with extracted values
        """
        threat = ThreatData()

        # Parse each threat type
        threat.tornado_detection = cls.parse_tornado_detection(text)
        threat.tornado_damage_threat = cls.parse_tornado_damage(text)

        wind_mph, wind_kts = cls.parse_wind_gust(text, is_xml)
        threat.max_wind_gust_mph = wind_mph
        threat.max_wind_gust_kts = wind_kts
        threat.wind_damage_threat = cls.parse_wind_damage(text)

        threat.max_hail_size_inches = cls.parse_hail_size(text, is_xml)
        threat.hail_damage_threat = cls.parse_hail_damage(text)

        snow_min, snow_max = cls.parse_snow_amount(text)
        threat.snow_amount_min_inches = snow_min
        threat.snow_amount_max_inches = snow_max
        threat.ice_accumulation_inches = cls.parse_ice_amount(text)

        threat.flash_flood_detection = cls.parse_flood_detection(text)
        threat.flash_flood_damage_threat = cls.parse_flood_damage(text)

        threat.storm_motion = cls.parse_storm_motion(text, is_xml)

        return threat

    @classmethod
    def parse_tornado_detection(cls, text: str) -> Optional[str]:
        """
        Parse tornado detection type.

        Returns:
            "RADAR INDICATED", "OBSERVED", "POSSIBLE", or None
        """
        match = PATTERN_TORNADO_DETECTION.search(text)
        if match:
            detection = match.group(1).upper()
            logger.debug(f"Tornado detection: {detection}")
            return detection
        return None

    @classmethod
    def parse_tornado_damage(cls, text: str) -> Optional[str]:
        """
        Parse tornado damage threat level.

        Returns:
            "CONSIDERABLE", "CATASTROPHIC", or None
        """
        match = PATTERN_TORNADO_DAMAGE.search(text)
        if match:
            threat_level = match.group(1).upper()
            logger.debug(f"Tornado damage threat: {threat_level}")
            return threat_level
        return None

    @classmethod
    def parse_wind_gust(cls, text: str, is_xml: bool = False) -> tuple[Optional[int], Optional[int]]:
        """
        Parse maximum wind gust.

        Args:
            text: Alert text
            is_xml: Whether text is XML format

        Returns:
            Tuple of (mph, knots) - one may be None
        """
        wind_mph = None
        wind_kts = None

        # Try XML format first if applicable
        if is_xml:
            xml_match = PATTERN_WIND_XML.search(text)
            if xml_match:
                try:
                    value = int(xml_match.group(1))
                    # Determine if mph or knots from context
                    if "mph" in xml_match.group(0).lower():
                        wind_mph = value
                        wind_kts = cls._mph_to_kts(value)
                    else:
                        wind_kts = value
                        wind_mph = cls._kts_to_mph(value)
                    logger.debug(f"Wind from XML: {wind_mph} mph / {wind_kts} kts")
                    return wind_mph, wind_kts
                except ValueError:
                    pass

        # Try text patterns
        match = PATTERN_WIND_GUST.search(text)
        if match:
            try:
                # Pattern has two groups - one for each alternate format
                # Group 1: "WIND...60 MPH" format
                # Group 2: "60 MPH WIND" format
                value_str = match.group(1) or match.group(2)
                if value_str:
                    value = int(value_str)
                    match_text = match.group(0).upper()

                    # Validate reasonable range (20-300 mph)
                    if 20 <= value <= 300:
                        if "KT" in match_text:
                            wind_kts = value
                            wind_mph = cls._kts_to_mph(value)
                        else:
                            wind_mph = value
                            wind_kts = cls._mph_to_kts(value)
                        logger.debug(f"Wind gust: {wind_mph} mph / {wind_kts} kts")
                    else:
                        logger.warning(f"Wind gust value {value} outside reasonable range")
            except (ValueError, TypeError):
                pass

        return wind_mph, wind_kts

    @classmethod
    def parse_wind_damage(cls, text: str) -> Optional[str]:
        """
        Parse wind damage threat level.

        Returns:
            "CONSIDERABLE", "DESTRUCTIVE", "CATASTROPHIC", or None
        """
        match = PATTERN_WIND_DAMAGE.search(text)
        if match:
            threat_level = match.group(1).upper()
            logger.debug(f"Wind damage threat: {threat_level}")
            return threat_level
        return None

    @classmethod
    def parse_hail_size(cls, text: str, is_xml: bool = False) -> Optional[float]:
        """
        Parse maximum hail size in inches.

        Handles:
        - Numeric values: "1.75 INCHES"
        - Descriptions: "GOLF BALL SIZE"

        Args:
            text: Alert text
            is_xml: Whether text is XML format

        Returns:
            Hail size in inches, or None
        """
        # Try XML format first if applicable
        if is_xml:
            xml_match = PATTERN_HAIL_XML.search(text)
            if xml_match:
                try:
                    value = float(xml_match.group(1))
                    if 0.25 <= value <= 6.0:  # Reasonable range
                        logger.debug(f"Hail size from XML: {value} in")
                        return value
                except ValueError:
                    pass

        # Try numeric pattern first (more specific)
        numeric_match = PATTERN_HAIL_SIZE.search(text)
        if numeric_match:
            try:
                value = float(numeric_match.group(1))
                if 0.25 <= value <= 6.0:  # Reasonable range
                    logger.debug(f"Hail size (numeric): {value} in")
                    return value
                else:
                    logger.warning(f"Hail size {value} outside reasonable range")
            except ValueError:
                pass

        # Try descriptive pattern
        desc_match = PATTERN_HAIL_DESC.search(text)
        if desc_match:
            description = desc_match.group(1).upper()
            size = HAIL_SIZE_DESCRIPTIONS.get(description)
            if size:
                logger.debug(f"Hail size (description '{description}'): {size} in")
                return size

        return None

    @classmethod
    def parse_hail_damage(cls, text: str) -> Optional[str]:
        """
        Parse hail damage threat level.

        Returns:
            "CONSIDERABLE", "CATASTROPHIC", or None
        """
        match = PATTERN_HAIL_DAMAGE.search(text)
        if match:
            threat_level = match.group(1).upper()
            logger.debug(f"Hail damage threat: {threat_level}")
            return threat_level
        return None

    @classmethod
    def parse_snow_amount(cls, text: str) -> tuple[Optional[float], Optional[float]]:
        """
        Parse snow accumulation amounts.

        Handles:
        - Single value: "UP TO 6 INCHES"
        - Range: "4 TO 8 INCHES"

        Returns:
            Tuple of (min_inches, max_inches) - either may be None
        """
        upper_text = text.upper()

        # Look for snow-related context first
        if "SNOW" not in upper_text and "ACCUMULATION" not in upper_text:
            return None, None

        match = PATTERN_SNOW_AMOUNT.search(text)
        if match:
            try:
                min_val = float(match.group(1))
                max_val = float(match.group(2)) if match.group(2) else min_val

                # Ensure min <= max
                if min_val > max_val:
                    min_val, max_val = max_val, min_val

                # Validate reasonable range (0.1 to 60 inches)
                if 0.1 <= min_val <= 60 and 0.1 <= max_val <= 60:
                    logger.debug(f"Snow amount: {min_val}-{max_val} in")
                    return min_val, max_val
                else:
                    logger.warning(f"Snow amount {min_val}-{max_val} outside reasonable range")
            except ValueError:
                pass

        return None, None

    @classmethod
    def parse_ice_amount(cls, text: str) -> Optional[float]:
        """
        Parse ice accumulation amount.

        Returns:
            Ice accumulation in inches, or None
        """
        match = PATTERN_ICE_AMOUNT.search(text)
        if match:
            try:
                # Use max value if range given
                if match.group(2):
                    value = float(match.group(2))
                else:
                    value = float(match.group(1))

                # Validate reasonable range (0.01 to 3 inches)
                if 0.01 <= value <= 3.0:
                    logger.debug(f"Ice accumulation: {value} in")
                    return value
            except ValueError:
                pass

        return None

    @classmethod
    def parse_flood_detection(cls, text: str) -> Optional[str]:
        """
        Parse flash flood detection type.

        Returns:
            "RADAR INDICATED", "OBSERVED", "POSSIBLE", or None
        """
        match = PATTERN_FLOOD_DETECTION.search(text)
        if match:
            detection = match.group(1).upper()
            logger.debug(f"Flash flood detection: {detection}")
            return detection
        return None

    @classmethod
    def parse_flood_damage(cls, text: str) -> Optional[str]:
        """
        Parse flash flood damage threat level.

        Returns:
            "CONSIDERABLE", "CATASTROPHIC", or None
        """
        match = PATTERN_FLOOD_DAMAGE.search(text)
        if match:
            threat_level = match.group(1).upper()
            logger.debug(f"Flash flood damage threat: {threat_level}")
            return threat_level
        return None

    @classmethod
    def parse_storm_motion(cls, text: str, is_xml: bool = False) -> Optional[StormMotion]:
        """
        Parse storm motion information.

        Extracts:
        - Direction (degrees or cardinal)
        - Speed (mph or knots)

        Args:
            text: Alert text
            is_xml: Whether text is XML format

        Returns:
            StormMotion object or None
        """
        motion = StormMotion()

        # Try standard format: TIME...MOT...LOC 1845Z 245DEG 35KT
        text_match = PATTERN_MOTION_TEXT.search(text)
        if text_match:
            try:
                motion.direction_degrees = int(text_match.group(1))
                motion.speed_kts = int(text_match.group(2))
                motion.speed_mph = cls._kts_to_mph(motion.speed_kts)
                motion.direction_from = cls._degrees_to_cardinal(motion.direction_degrees)
                logger.debug(f"Storm motion: {motion.direction_degrees}° at {motion.speed_mph} mph")
                return motion
            except ValueError:
                pass

        # Try XML format
        if is_xml:
            xml_match = PATTERN_MOTION_XML.search(text)
            if xml_match:
                try:
                    motion.direction_degrees = int(xml_match.group(1))
                    speed_val = int(xml_match.group(2))
                    match_text = xml_match.group(0).upper()

                    if "MPH" in match_text:
                        motion.speed_mph = speed_val
                        motion.speed_kts = cls._mph_to_kts(speed_val)
                    else:
                        motion.speed_kts = speed_val
                        motion.speed_mph = cls._kts_to_mph(speed_val)

                    motion.direction_from = cls._degrees_to_cardinal(motion.direction_degrees)
                    logger.debug(f"Storm motion (XML): {motion.direction_degrees}° at {motion.speed_mph} mph")
                    return motion
                except ValueError:
                    pass

        # Try cardinal direction format: "MOVING SW AT 35 MPH"
        alt_match = PATTERN_MOTION_ALT.search(text)
        if alt_match:
            try:
                cardinal = alt_match.group(1).upper()
                speed_val = int(alt_match.group(2))
                match_text = alt_match.group(0).upper()

                # Convert cardinal to degrees (direction storm is moving FROM)
                # Note: CARDINAL_TO_DEGREES gives direction storm is moving TO
                motion.direction_degrees = CARDINAL_TO_DEGREES.get(cardinal)
                if motion.direction_degrees is not None:
                    motion.direction_from = cls._get_opposite_cardinal(cardinal)

                    if "KT" in match_text:
                        motion.speed_kts = speed_val
                        motion.speed_mph = cls._kts_to_mph(speed_val)
                    else:
                        motion.speed_mph = speed_val
                        motion.speed_kts = cls._mph_to_kts(speed_val)

                    logger.debug(f"Storm motion (cardinal): {cardinal} at {motion.speed_mph} mph")
                    return motion
            except ValueError:
                pass

        return None if not motion.is_valid else motion

    # ==========================================================================
    # Utility methods
    # ==========================================================================

    @staticmethod
    def _mph_to_kts(mph: int) -> int:
        """Convert MPH to knots."""
        return round(mph * 0.868976)

    @staticmethod
    def _kts_to_mph(kts: int) -> int:
        """Convert knots to MPH."""
        return round(kts * 1.15078)

    @staticmethod
    def _degrees_to_cardinal(degrees: int) -> str:
        """
        Convert degrees to cardinal direction.

        Note: This returns the direction the storm is moving FROM.
        """
        # Normalize to 0-360
        degrees = degrees % 360

        directions = [
            "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
        ]

        # Each direction covers 22.5 degrees
        index = round(degrees / 22.5) % 16
        return directions[index]

    @staticmethod
    def _get_opposite_cardinal(cardinal: str) -> str:
        """Get the opposite cardinal direction."""
        opposites = {
            "N": "S", "NNE": "SSW", "NE": "SW", "ENE": "WSW",
            "E": "W", "ESE": "WNW", "SE": "NW", "SSE": "NNW",
            "S": "N", "SSW": "NNE", "SW": "NE", "WSW": "ENE",
            "W": "E", "WNW": "ESE", "NW": "SE", "NNW": "SSE"
        }
        return opposites.get(cardinal.upper(), cardinal)


# Convenience function for direct parsing
def parse_threat_data(text: str, is_xml: bool = False) -> ThreatData:
    """Parse threat data from alert text."""
    return ThreatParser.parse(text, is_xml)
