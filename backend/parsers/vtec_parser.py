"""
VTEC (Valid Time Event Code) parser for Alert Dashboard V2.

This module handles parsing of P-VTEC and H-VTEC strings from NWS alerts.
Based on patterns and edge case handling from pyIEM.

References:
- NWS VTEC: https://www.weather.gov/vtec/
- pyIEM: https://github.com/akrherz/pyIEM
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .patterns import (
    PATTERN_VTEC,
    PATTERN_VTEC_SIMPLE,
    PATTERN_HVTEC,
    VALID_VTEC_ACTIONS,
    VALID_VTEC_SIGNIFICANCE,
)
from ..utils.timezone import TimezoneHelper
from ..models.alert import VTECAction, AlertSignificance, VTECInfo

logger = logging.getLogger(__name__)


@dataclass
class VTECData:
    """
    Parsed VTEC data with validation status.

    This is the result of parsing a VTEC string, including
    any validation errors or warnings encountered.
    """
    vtec_info: Optional[VTECInfo] = None
    raw_string: str = ""
    is_valid: bool = False
    validation_errors: list[str] = None
    validation_warnings: list[str] = None

    def __post_init__(self):
        if self.validation_errors is None:
            self.validation_errors = []
        if self.validation_warnings is None:
            self.validation_warnings = []


class VTECParser:
    """Parser for VTEC (Valid Time Event Code) strings."""

    # Known phenomenon codes (not exhaustive, but covers common ones)
    KNOWN_PHENOMENA = {
        "TO", "SV", "FF", "FA", "FL", "WS", "BZ", "IS", "LE", "WW",
        "WC", "EC", "HT", "EH", "FG", "SM", "HW", "EW", "WI", "DS",
        "FR", "FZ", "HZ", "AS", "CF", "LS", "SU", "RP", "BW", "SC",
        "SW", "RB", "SI", "GL", "SE", "SR", "HF", "TR", "HU", "TY",
        "SS", "TS", "MA", "SQ", "AF", "LO", "ZF", "ZR", "UP", "ZY",
        "FW", "RF", "EQ", "VO", "AV",
    }

    @classmethod
    def parse(cls, text: str) -> VTECData:
        """
        Parse VTEC string(s) from alert text.

        Args:
            text: Raw alert text that may contain VTEC string(s)

        Returns:
            VTECData with parsed information and validation status
        """
        result = VTECData(raw_string=text[:200] if len(text) > 200 else text)

        # Find VTEC string
        simple_match = PATTERN_VTEC_SIMPLE.search(text)
        if not simple_match:
            result.validation_errors.append("No VTEC string found in text")
            return result

        raw_vtec = simple_match.group(1)
        result.raw_string = raw_vtec

        # Parse with detailed pattern
        match = PATTERN_VTEC.search(text)
        if not match:
            result.validation_errors.append(f"VTEC string '{raw_vtec}' does not match expected format")
            return result

        try:
            (
                product_class,
                action,
                office,
                phenomenon,
                significance,
                etn,
                begin_time_str,
                end_time_str
            ) = match.groups()

            # Validate product class
            if product_class not in ("O", "T", "E", "X"):
                result.validation_warnings.append(
                    f"Unusual product class '{product_class}', expected O/T/E/X"
                )

            # Validate action code
            if action not in VALID_VTEC_ACTIONS:
                result.validation_errors.append(
                    f"Invalid action code '{action}', expected one of {VALID_VTEC_ACTIONS}"
                )
                return result

            # Validate office code
            if len(office) != 4:
                result.validation_errors.append(
                    f"Invalid office code '{office}', expected 4 characters"
                )
                return result

            # Validate phenomenon code
            if len(phenomenon) != 2:
                result.validation_errors.append(
                    f"Invalid phenomenon code '{phenomenon}', expected 2 characters"
                )
                return result

            if phenomenon not in cls.KNOWN_PHENOMENA:
                result.validation_warnings.append(
                    f"Unknown phenomenon code '{phenomenon}'"
                )

            # Validate significance
            if significance not in VALID_VTEC_SIGNIFICANCE:
                result.validation_errors.append(
                    f"Invalid significance '{significance}', expected one of {VALID_VTEC_SIGNIFICANCE}"
                )
                return result

            # Parse ETN
            try:
                event_number = int(etn)
                if event_number < 1 or event_number > 9999:
                    result.validation_warnings.append(
                        f"ETN {event_number} outside typical range (1-9999)"
                    )
            except ValueError:
                result.validation_errors.append(f"Invalid ETN '{etn}'")
                return result

            # Parse timestamps
            begin_time = TimezoneHelper.parse_vtec_timestamp(begin_time_str)
            end_time = TimezoneHelper.parse_vtec_timestamp(end_time_str)

            if begin_time is None and not begin_time_str.startswith("0000"):
                result.validation_warnings.append(
                    f"Could not parse begin time '{begin_time_str}'"
                )

            if end_time is None and not end_time_str.startswith("0000"):
                result.validation_warnings.append(
                    f"Could not parse end time '{end_time_str}'"
                )

            # Convert action and significance to enums
            try:
                action_enum = VTECAction(action)
            except ValueError:
                action_enum = VTECAction.NEW
                result.validation_warnings.append(
                    f"Action '{action}' not in VTECAction enum, defaulting to NEW"
                )

            try:
                sig_enum = AlertSignificance(significance)
            except ValueError:
                sig_enum = AlertSignificance.WARNING
                result.validation_warnings.append(
                    f"Significance '{significance}' not in AlertSignificance enum"
                )

            # Create VTECInfo
            result.vtec_info = VTECInfo(
                product_class=product_class,
                action=action_enum,
                office=office,
                phenomenon=phenomenon,
                significance=sig_enum,
                event_tracking_number=event_number,
                begin_time=begin_time,
                end_time=end_time,
                raw_vtec=raw_vtec,
            )
            result.is_valid = True

        except Exception as e:
            logger.exception(f"Error parsing VTEC: {e}")
            result.validation_errors.append(f"Parsing exception: {str(e)}")

        return result

    @classmethod
    def parse_all(cls, text: str) -> list[VTECData]:
        """
        Parse all VTEC strings from text.

        Some products may contain multiple VTEC strings (e.g., upgrades).

        Args:
            text: Raw alert text

        Returns:
            List of VTECData for each VTEC found
        """
        results = []

        # Find all VTEC strings
        matches = PATTERN_VTEC.finditer(text)

        for match in matches:
            # Get the full VTEC string including slashes
            start = match.start()
            end = match.end()

            # Find the leading slash
            while start > 0 and text[start] != '/':
                start -= 1

            vtec_str = text[start:end + 1] if text[end:end+1] == '/' else text[start:end]

            # Parse this VTEC
            result = cls._parse_match(match, vtec_str)
            results.append(result)

        if not results:
            # Return single result indicating no VTEC found
            results.append(VTECData(
                raw_string=text[:100],
                validation_errors=["No VTEC strings found"]
            ))

        return results

    @classmethod
    def _parse_match(cls, match, raw_vtec: str) -> VTECData:
        """Parse a single regex match."""
        result = VTECData(raw_string=raw_vtec)

        try:
            (
                product_class,
                action,
                office,
                phenomenon,
                significance,
                etn,
                begin_time_str,
                end_time_str
            ) = match.groups()

            # Validate and create VTECInfo (similar to parse method)
            if action not in VALID_VTEC_ACTIONS:
                result.validation_errors.append(f"Invalid action '{action}'")
                return result

            if significance not in VALID_VTEC_SIGNIFICANCE:
                result.validation_errors.append(f"Invalid significance '{significance}'")
                return result

            event_number = int(etn)
            begin_time = TimezoneHelper.parse_vtec_timestamp(begin_time_str)
            end_time = TimezoneHelper.parse_vtec_timestamp(end_time_str)

            try:
                action_enum = VTECAction(action)
            except ValueError:
                action_enum = VTECAction.NEW

            try:
                sig_enum = AlertSignificance(significance)
            except ValueError:
                sig_enum = AlertSignificance.WARNING

            result.vtec_info = VTECInfo(
                product_class=product_class,
                action=action_enum,
                office=office,
                phenomenon=phenomenon,
                significance=sig_enum,
                event_tracking_number=event_number,
                begin_time=begin_time,
                end_time=end_time,
                raw_vtec=raw_vtec,
            )
            result.is_valid = True

        except Exception as e:
            result.validation_errors.append(str(e))

        return result

    @classmethod
    def build_product_id(cls, vtec_info: VTECInfo) -> str:
        """
        Build a unique product ID from VTEC information.

        For WARNINGS: {phenomenon}.{office}.{etn}
        For WATCHES:  {phenomenon}A.{etn}  (no office - watches share ETN across offices)

        Watches are issued by SPC with a single ETN that all NWS offices use.
        This allows watch products from different offices to merge automatically.

        Args:
            vtec_info: Parsed VTEC information

        Returns:
            Product ID string
        """
        phenomenon = vtec_info.phenomenon

        # For watches, append 'A' to phenomenon and omit office
        # (ETN is assigned by SPC and shared across all offices)
        if vtec_info.significance == AlertSignificance.WATCH:
            return f"{phenomenon}A.{vtec_info.event_tracking_number:04d}"

        # For warnings/advisories, include office (ETN is office-specific)
        office = vtec_info.office
        if office.startswith("K") and len(office) == 4:
            office = office[1:]

        return f"{phenomenon}.{office}.{vtec_info.event_tracking_number:04d}"

    @classmethod
    def is_cancellation(cls, vtec_info: VTECInfo) -> bool:
        """Check if VTEC represents a cancellation."""
        return vtec_info.action in (VTECAction.CAN, VTECAction.EXP)

    @classmethod
    def is_continuation(cls, vtec_info: VTECInfo) -> bool:
        """Check if VTEC represents a continuation/update."""
        return vtec_info.action in (
            VTECAction.CON, VTECAction.EXT, VTECAction.EXA,
            VTECAction.EXB, VTECAction.UPG, VTECAction.COR
        )

    @classmethod
    def is_new_event(cls, vtec_info: VTECInfo) -> bool:
        """Check if VTEC represents a new event."""
        return vtec_info.action == VTECAction.NEW

    @classmethod
    def get_phenomenon_name(cls, phenomenon: str) -> str:
        """Get human-readable name for phenomenon code."""
        from ..models.alert import PHENOMENON_NAMES
        return PHENOMENON_NAMES.get(phenomenon, f"Unknown ({phenomenon})")


@dataclass
class HVTECData:
    """Parsed H-VTEC (Hydrologic VTEC) data."""
    severity: str = ""           # 0-3, N=None, U=Unknown
    immediate_cause: str = ""    # 2-char cause code
    flood_begin: Optional[datetime] = None
    flood_crest: Optional[datetime] = None
    flood_end: Optional[datetime] = None
    flood_record: str = ""       # 2-char record code
    raw_string: str = ""
    is_valid: bool = False


class HVTECParser:
    """Parser for H-VTEC (Hydrologic VTEC) strings."""

    # Severity codes
    SEVERITY_CODES = {
        "0": "None",
        "1": "Minor",
        "2": "Moderate",
        "3": "Major",
        "N": "None",
        "U": "Unknown",
    }

    # Immediate cause codes
    CAUSE_CODES = {
        "ER": "Excessive Rainfall",
        "SM": "Snowmelt",
        "RS": "Rain and Snowmelt",
        "DM": "Dam or Levee Failure",
        "IJ": "Ice Jam",
        "GO": "Glacier-Dammed Lake Outburst",
        "IC": "Rain and/or Snowmelt and/or Ice Jam",
        "FS": "Upstream Flooding plus Storm Surge",
        "FT": "Upstream Flooding plus Tidal Effects",
        "ET": "Elevated Upstream Flow plus Tidal Effects",
        "WT": "Wind and/or Tidal Effects",
        "DR": "Upstream Dam or Reservoir Release",
        "MC": "Multiple Causes",
        "OT": "Other Effects",
        "UU": "Unknown",
    }

    # Flood record codes
    RECORD_CODES = {
        "NO": "A record flood is not expected",
        "NR": "Near record or record flood expected",
        "UU": "Flood without a period of record to compare",
        "OO": "For areal flood warnings, areal flash flood products, and flood advisories",
    }

    @classmethod
    def parse(cls, text: str) -> Optional[HVTECData]:
        """
        Parse H-VTEC string from text.

        Args:
            text: Raw alert text

        Returns:
            HVTECData if H-VTEC found, None otherwise
        """
        match = PATTERN_HVTEC.search(text)
        if not match:
            return None

        try:
            severity, cause, begin_str, crest_str, end_str, record = match.groups()

            result = HVTECData(
                severity=severity,
                immediate_cause=cause,
                flood_begin=TimezoneHelper.parse_vtec_timestamp(begin_str),
                flood_crest=TimezoneHelper.parse_vtec_timestamp(crest_str),
                flood_end=TimezoneHelper.parse_vtec_timestamp(end_str),
                flood_record=record,
                raw_string=match.group(0),
                is_valid=True,
            )

            return result

        except Exception as e:
            logger.warning(f"Error parsing H-VTEC: {e}")
            return None

    @classmethod
    def get_severity_description(cls, code: str) -> str:
        """Get human-readable severity description."""
        return cls.SEVERITY_CODES.get(code, f"Unknown ({code})")

    @classmethod
    def get_cause_description(cls, code: str) -> str:
        """Get human-readable cause description."""
        return cls.CAUSE_CODES.get(code, f"Unknown ({code})")

    @classmethod
    def get_record_description(cls, code: str) -> str:
        """Get human-readable record description."""
        return cls.RECORD_CODES.get(code, f"Unknown ({code})")
