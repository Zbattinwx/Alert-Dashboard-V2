"""
Main alert parser for Alert Dashboard V2.

This module provides the primary interface for parsing NWS alerts from both:
- NWS API (JSON/GeoJSON format)
- NWWS-OI Weather Wire (raw text format)

Key improvements over V1:
- Modular parsing with dedicated sub-parsers
- Comprehensive error handling and logging
- Never silent failures - all issues are logged
- Validation at each step
- Clear separation of API vs text parsing
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Union

from .vtec_parser import VTECParser, VTECData
from .ugc_parser import UGCParser, UGCData
from .threat_parser import ThreatParser
from .patterns import (
    is_xml_content,
    PATTERN_XML_EXPIRES,
    PATTERN_XML_EVENT_END,
    PATTERN_EXPIRATION_TEXT,
    PATTERN_LOCATION_DESC,
    PATTERN_AREA_DESC_XML,
    PATTERN_POLYGON_TEXT,
    PATTERN_POLYGON_XML,
    PATTERN_COORD_VALUE,
    PATTERN_WATCH_TYPE,
    SPS_THUNDERSTORM_KEYWORDS,
    SPS_EXCLUDED_KEYWORDS,
)
from ..models.alert import (
    Alert,
    AlertStatus,
    AlertSignificance,
    VTECInfo,
    PHENOMENON_NAMES,
)
from ..utils.timezone import TimezoneHelper

logger = logging.getLogger(__name__)


class AlertParser:
    """
    Main parser for NWS weather alerts.

    Supports parsing from:
    - NWS API JSON responses (dict)
    - NWWS-OI raw text alerts (str)
    - XML/CAP wrapped alerts (str)
    """

    # Default alert lifetime when expiration can't be determined
    DEFAULT_LIFETIME_MINUTES = 60

    # Phenomena that should get default lifetime if expiration not found
    TARGETED_PHENOMENA = {
        "TO", "SV", "FF", "SS", "SPS", "SVR", "FFW", "TOR",
        "SVS", "FFS", "TOA", "SVA", "FFA"
    }

    @classmethod
    def parse(cls, alert_data: Union[dict, str], source: str = "unknown") -> Optional[Alert]:
        """
        Parse an alert from either API JSON or raw text.

        Args:
            alert_data: Either a dict (NWS API) or str (NWWS text)
            source: Source identifier ("api" or "nwws")

        Returns:
            Parsed Alert object, or None if parsing fails
        """
        try:
            if isinstance(alert_data, dict):
                return cls.parse_api_alert(alert_data, source)
            elif isinstance(alert_data, str):
                return cls.parse_text_alert(alert_data, source)
            else:
                logger.error(f"Unexpected alert data type: {type(alert_data)}")
                return None
        except Exception as e:
            logger.exception(f"Error parsing alert: {e}")
            return None

    @classmethod
    def parse_api_alert(cls, feature: dict, source: str = "api") -> Optional[Alert]:
        """
        Parse an alert from NWS API GeoJSON format.

        Args:
            feature: GeoJSON feature dict from NWS API
            source: Source identifier

        Returns:
            Parsed Alert object, or None if parsing fails
        """
        try:
            properties = feature.get("properties", {})
            geometry = feature.get("geometry")

            alert = Alert(source=source)

            # Extract basic identification
            alert.message_id = properties.get("id") or properties.get("@id")

            # Parse VTEC from parameters or description
            vtec_str = None
            parameters = properties.get("parameters", {})
            if "VTEC" in parameters:
                vtec_list = parameters["VTEC"]
                if vtec_list:
                    vtec_str = vtec_list[0] if isinstance(vtec_list, list) else vtec_list

            # If no VTEC in parameters, try description
            if not vtec_str:
                description = properties.get("description", "")
                vtec_data = VTECParser.parse(description)
                if vtec_data.is_valid:
                    alert.vtec = vtec_data.vtec_info
            else:
                vtec_data = VTECParser.parse(vtec_str)
                if vtec_data.is_valid:
                    alert.vtec = vtec_data.vtec_info

            # Build product ID
            if alert.vtec:
                alert.product_id = VTECParser.build_product_id(alert.vtec)
                alert.phenomenon = alert.vtec.phenomenon
                alert.significance = alert.vtec.significance
                alert.sender_office = alert.vtec.office

                # Handle cancellations
                if VTECParser.is_cancellation(alert.vtec):
                    alert.status = AlertStatus.CANCELLED
            else:
                # Fallback product ID from API ID
                if alert.message_id:
                    alert.product_id = alert.message_id.split("/")[-1]
                else:
                    alert.product_id = f"api_{datetime.now(timezone.utc).timestamp()}"

            # Extract event information
            alert.event_name = properties.get("event", "")
            alert.headline = properties.get("headline", "")
            alert.description = properties.get("description", "")
            alert.instruction = properties.get("instruction", "")
            alert.sender_name = properties.get("senderName", "")

            # Determine phenomenon from event name if not from VTEC
            if not alert.phenomenon and alert.event_name:
                alert.phenomenon = cls._event_name_to_phenomenon(alert.event_name)

            # Parse timestamps
            # Priority: ends > expires (ends is actual event end, expires is message expiration)
            ends_str = properties.get("ends")
            expires_str = properties.get("expires")
            effective_str = properties.get("effective")
            onset_str = properties.get("onset")
            sent_str = properties.get("sent")

            if ends_str:
                alert.expiration_time = TimezoneHelper.parse_iso_timestamp(ends_str)
            elif expires_str:
                alert.expiration_time = TimezoneHelper.parse_iso_timestamp(expires_str)
                alert.message_expires = alert.expiration_time

            if effective_str:
                alert.effective_time = TimezoneHelper.parse_iso_timestamp(effective_str)
            if onset_str:
                alert.onset_time = TimezoneHelper.parse_iso_timestamp(onset_str)
            if sent_str:
                alert.issued_time = TimezoneHelper.parse_iso_timestamp(sent_str)

            # Extract geographic codes
            geocode = properties.get("geocode", {})

            # UGC codes
            ugc_list = geocode.get("UGC", [])
            if ugc_list:
                alert.affected_areas = ugc_list if isinstance(ugc_list, list) else [ugc_list]

            # FIPS/SAME codes
            same_codes = geocode.get("SAME", [])
            if same_codes:
                # Normalize to 5-digit FIPS
                alert.fips_codes = [
                    code[-5:].zfill(5)
                    for code in same_codes
                    if code and len(code) >= 5
                ]

            # Area description
            alert.display_locations = properties.get("areaDesc", "")

            # Parse polygon from geometry
            if geometry:
                alert.polygon = cls._parse_geojson_geometry(geometry)
                if alert.polygon:
                    alert.centroid = cls._calculate_centroid(alert.polygon)

            # Parse threat data from description
            alert.threat = ThreatParser.parse(alert.description, is_xml=False)

            # Also check parameters for threat tags
            cls._parse_api_threat_parameters(parameters, alert)

            # Apply SPS filter if applicable
            if alert.phenomenon == "SPS":
                if not cls._is_relevant_sps(alert.description):
                    logger.debug(f"Filtering out non-thunderstorm SPS: {alert.product_id}")
                    return None

            # Assign default expiration if needed
            if not alert.expiration_time and alert.phenomenon in cls.TARGETED_PHENOMENA:
                alert.expiration_time = datetime.now(timezone.utc) + timedelta(
                    minutes=cls.DEFAULT_LIFETIME_MINUTES
                )
                logger.warning(
                    f"Assigned default {cls.DEFAULT_LIFETIME_MINUTES}-min expiration to "
                    f"{alert.product_id} (no expiration found in API response)"
                )

            # Store raw text for reference
            alert.raw_text = alert.description

            return alert

        except Exception as e:
            logger.exception(f"Error parsing API alert: {e}")
            return None

    @classmethod
    def parse_text_alert(cls, raw_text: str, source: str = "nwws") -> Optional[Alert]:
        """
        Parse an alert from raw NWWS text or XML/CAP format.

        Args:
            raw_text: Raw alert text
            source: Source identifier

        Returns:
            Parsed Alert object, or None if parsing fails
        """
        try:
            alert = Alert(source=source)
            alert.raw_text = raw_text

            # Detect if XML content
            is_xml = is_xml_content(raw_text)

            # Parse VTEC
            vtec_data = VTECParser.parse(raw_text)
            if vtec_data.is_valid:
                alert.vtec = vtec_data.vtec_info
                alert.product_id = VTECParser.build_product_id(alert.vtec)
                alert.phenomenon = alert.vtec.phenomenon
                alert.significance = alert.vtec.significance
                alert.sender_office = alert.vtec.office

                # Use VTEC end time as expiration
                if alert.vtec.end_time:
                    alert.expiration_time = alert.vtec.end_time

                # Handle cancellations
                if VTECParser.is_cancellation(alert.vtec):
                    alert.status = AlertStatus.CANCELLED

                # Log validation warnings
                for warning in vtec_data.validation_warnings:
                    logger.warning(f"VTEC warning for {alert.product_id}: {warning}")
            else:
                # Check for watch product without standard VTEC
                watch_match = PATTERN_WATCH_TYPE.search(raw_text)
                if watch_match:
                    watch_type = watch_match.group(1).upper()
                    watch_number = watch_match.group(2)

                    if "TORNADO" in watch_type:
                        alert.phenomenon = "TO"
                    else:
                        alert.phenomenon = "SV"

                    alert.significance = AlertSignificance.WATCH
                    alert.product_id = f"{alert.phenomenon}A.SPC.{watch_number.zfill(4)}"
                else:
                    # No VTEC or watch - generate fallback ID
                    alert.product_id = f"nwws_{datetime.now(timezone.utc).timestamp()}"
                    for error in vtec_data.validation_errors:
                        logger.debug(f"VTEC parse issue: {error}")

            # Parse UGC codes
            ugc_data = UGCParser.parse(raw_text)
            if ugc_data.is_valid:
                alert.affected_areas = ugc_data.ugc_codes
                alert.fips_codes = ugc_data.fips_codes

                # Use UGC expiration if no VTEC expiration
                if not alert.expiration_time and ugc_data.expiration_time:
                    alert.expiration_time = ugc_data.expiration_time

            # Also try XML FIPS if XML content
            if is_xml:
                xml_fips = UGCParser.parse_xml_fips(raw_text)
                if xml_fips:
                    alert.fips_codes = list(set(alert.fips_codes + xml_fips))

            # Parse expiration from text if still not found
            if not alert.expiration_time:
                alert.expiration_time = cls._parse_text_expiration(raw_text, is_xml, alert.sender_office)

            # Parse location description
            alert.display_locations = cls._parse_location_description(raw_text, is_xml)
            if not alert.display_locations and alert.affected_areas:
                alert.display_locations = UGCParser.format_location_string(alert.affected_areas)

            # Parse polygon
            alert.polygon = cls._parse_text_polygon(raw_text, is_xml)
            if alert.polygon:
                alert.centroid = cls._calculate_centroid(alert.polygon)

            # Parse threat data
            alert.threat = ThreatParser.parse(raw_text, is_xml)

            # Set event name
            if alert.phenomenon:
                alert.event_name = cls._build_event_name(alert.phenomenon, alert.significance)

            # Apply SPS filter
            if alert.phenomenon == "SPS":
                if not cls._is_relevant_sps(raw_text):
                    logger.debug(f"Filtering out non-thunderstorm SPS")
                    return None

            # Assign default expiration if needed
            if not alert.expiration_time and alert.phenomenon in cls.TARGETED_PHENOMENA:
                alert.expiration_time = datetime.now(timezone.utc) + timedelta(
                    minutes=cls.DEFAULT_LIFETIME_MINUTES
                )
                logger.warning(
                    f"Assigned default {cls.DEFAULT_LIFETIME_MINUTES}-min expiration to "
                    f"{alert.product_id} (no expiration found in text)"
                )

            return alert

        except Exception as e:
            logger.exception(f"Error parsing text alert: {e}")
            return None

    # ==========================================================================
    # Helper methods
    # ==========================================================================

    @classmethod
    def _parse_geojson_geometry(cls, geometry: dict) -> list[list[float]]:
        """Parse polygon coordinates from GeoJSON geometry."""
        coords = []

        geom_type = geometry.get("type", "")
        geom_coords = geometry.get("coordinates", [])

        if geom_type == "Polygon" and geom_coords:
            # Polygon: coordinates is array of rings, first ring is outer boundary
            outer_ring = geom_coords[0] if geom_coords else []
            # GeoJSON is [lon, lat], we want [lat, lon]
            coords = [[coord[1], coord[0]] for coord in outer_ring]

        elif geom_type == "MultiPolygon" and geom_coords:
            # MultiPolygon: use first polygon's outer ring
            if geom_coords and geom_coords[0]:
                outer_ring = geom_coords[0][0]
                coords = [[coord[1], coord[0]] for coord in outer_ring]

        return coords

    @classmethod
    def _parse_text_polygon(cls, text: str, is_xml: bool) -> list[list[float]]:
        """Parse polygon coordinates from text alert."""
        coords = []

        if is_xml:
            # XML format: <polygon>lat,lon lat,lon ...</polygon>
            match = PATTERN_POLYGON_XML.search(text)
            if match:
                poly_str = match.group(1).strip()
                pairs = poly_str.split()
                for pair in pairs:
                    try:
                        lat_str, lon_str = pair.split(',')
                        lat = float(lat_str)
                        lon = float(lon_str)
                        # Validate ranges
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            coords.append([lat, lon])
                    except (ValueError, IndexError):
                        continue
        else:
            # Text format: LAT...LON 4105 8145 4098 8132 ...
            match = PATTERN_POLYGON_TEXT.search(text)
            if match:
                coord_text = match.group(1)
                values = PATTERN_COORD_VALUE.findall(coord_text)

                # Values come in pairs: lat, lon
                if len(values) >= 2 and len(values) % 2 == 0:
                    for i in range(0, len(values), 2):
                        try:
                            # Values are in format: DDMM or DDDMM
                            # Need to divide by 100 to get decimal degrees
                            lat = float(values[i]) / 100.0
                            lon = -float(values[i + 1]) / 100.0  # West is negative

                            # Validate ranges
                            if 20 <= lat <= 60 and -130 <= lon <= -60:  # Reasonable US bounds
                                coords.append([lat, lon])
                            else:
                                logger.warning(f"Coordinate out of range: {lat}, {lon}")
                        except (ValueError, IndexError):
                            continue

        # Ensure polygon is closed (first point = last point)
        if coords and len(coords) >= 3:
            if coords[0] != coords[-1]:
                coords.append(coords[0])

        return coords

    @classmethod
    def _calculate_centroid(cls, polygon: list[list[float]]) -> Optional[tuple[float, float]]:
        """Calculate centroid of a polygon."""
        if not polygon:
            return None

        lat_sum = sum(p[0] for p in polygon)
        lon_sum = sum(p[1] for p in polygon)
        n = len(polygon)

        return (lat_sum / n, lon_sum / n)

    @classmethod
    def _parse_text_expiration(
        cls,
        text: str,
        is_xml: bool,
        office: Optional[str] = None
    ) -> Optional[datetime]:
        """Parse expiration time from text alert."""

        # Try XML eventEndingTime first (preferred)
        if is_xml:
            end_match = PATTERN_XML_EVENT_END.search(text)
            if end_match:
                return TimezoneHelper.parse_iso_timestamp(end_match.group(1))

            # Then XML expires
            exp_match = PATTERN_XML_EXPIRES.search(text)
            if exp_match:
                return TimezoneHelper.parse_iso_timestamp(exp_match.group(1))

        # Try text patterns
        match = PATTERN_EXPIRATION_TEXT.search(text)
        if match:
            time_str = match.group(1)
            am_pm = match.group(2)
            tz_str = match.group(3)

            # If no timezone, try to infer from office
            if not tz_str and office:
                office_tz = TimezoneHelper.get_timezone_for_wfo(office)
                if office_tz:
                    # Use office timezone name
                    tz_str = None  # Will use office_tz in parse_text_time

            return TimezoneHelper.parse_text_time(time_str, am_pm, tz_str)

        return None

    @classmethod
    def _parse_location_description(cls, text: str, is_xml: bool) -> str:
        """Parse location description from text."""
        if is_xml:
            match = PATTERN_AREA_DESC_XML.search(text)
            if match:
                return match.group(1).strip()

        # Text format: look for "...LOCATION..." line
        match = PATTERN_LOCATION_DESC.search(text)
        if match:
            desc = match.group(1).strip()
            # Clean up and truncate if needed
            desc = desc.split('\n')[0]  # First line only
            if not desc.startswith('/O.'):  # Not a VTEC line
                return desc.rstrip('-').strip()

        return ""

    @classmethod
    def _parse_api_threat_parameters(cls, parameters: dict, alert: Alert) -> None:
        """Extract threat data from API parameters."""
        # Max wind gust
        if "maxWindGust" in parameters:
            try:
                gust_list = parameters["maxWindGust"]
                if gust_list:
                    gust_str = gust_list[0] if isinstance(gust_list, list) else gust_list
                    # Format might be "70 mph" or just "70"
                    gust_val = int(''.join(filter(str.isdigit, str(gust_str))))
                    if gust_val > (alert.threat.max_wind_gust_mph or 0):
                        alert.threat.max_wind_gust_mph = gust_val
            except (ValueError, TypeError):
                pass

        # Max hail size
        if "maxHailSize" in parameters:
            try:
                hail_list = parameters["maxHailSize"]
                if hail_list:
                    hail_str = hail_list[0] if isinstance(hail_list, list) else hail_list
                    hail_val = float(''.join(c for c in str(hail_str) if c.isdigit() or c == '.'))
                    if hail_val > (alert.threat.max_hail_size_inches or 0):
                        alert.threat.max_hail_size_inches = hail_val
            except (ValueError, TypeError):
                pass

        # Tornado detection
        if "tornadoDetection" in parameters:
            detection_list = parameters["tornadoDetection"]
            if detection_list:
                detection = detection_list[0] if isinstance(detection_list, list) else detection_list
                alert.threat.tornado_detection = str(detection).upper()

    @classmethod
    def _is_relevant_sps(cls, text: str) -> bool:
        """
        Check if an SPS (Special Weather Statement) is thunderstorm-related.

        Filters out SPS for fire weather, fog, heat, marine, etc.
        """
        upper_text = text.upper()

        # Check exclusions first (using regex for word boundaries)
        import re
        for pattern in SPS_EXCLUDED_KEYWORDS:
            if re.search(pattern, upper_text):
                logger.debug(f"SPS excluded by keyword pattern: {pattern}")
                return False

        # Check for thunderstorm keywords
        for keyword in SPS_THUNDERSTORM_KEYWORDS:
            if keyword in upper_text:
                return True

        # If no thunderstorm keywords found, exclude
        logger.debug("SPS excluded: no thunderstorm keywords found")
        return False

    @classmethod
    def _event_name_to_phenomenon(cls, event_name: str) -> str:
        """Convert event name to phenomenon code."""
        event_upper = event_name.upper()

        # Direct mappings
        mappings = {
            "TORNADO WARNING": "TO",
            "TORNADO WATCH": "TO",
            "SEVERE THUNDERSTORM WARNING": "SV",
            "SEVERE THUNDERSTORM WATCH": "SV",
            "FLASH FLOOD WARNING": "FF",
            "FLASH FLOOD WATCH": "FF",
            "FLOOD WARNING": "FL",
            "FLOOD WATCH": "FL",
            "WINTER STORM WARNING": "WS",
            "WINTER STORM WATCH": "WS",
            "BLIZZARD WARNING": "BZ",
            "ICE STORM WARNING": "IS",
            "WIND CHILL WARNING": "WC",
            "WIND CHILL ADVISORY": "WC",
            "WINTER WEATHER ADVISORY": "WW",
            "SPECIAL WEATHER STATEMENT": "SPS",
            "HIGH WIND WARNING": "HW",
            "LAKE EFFECT SNOW WARNING": "LE",
            "SNOW SQUALL WARNING": "SQ",
        }

        for name, code in mappings.items():
            if name in event_upper:
                return code

        return ""

    @classmethod
    def _build_event_name(cls, phenomenon: str, significance: AlertSignificance) -> str:
        """Build event name from phenomenon and significance."""
        base_name = PHENOMENON_NAMES.get(phenomenon, f"Unknown ({phenomenon})")

        suffix_map = {
            AlertSignificance.WARNING: "Warning",
            AlertSignificance.WATCH: "Watch",
            AlertSignificance.ADVISORY: "Advisory",
            AlertSignificance.STATEMENT: "Statement",
            AlertSignificance.OUTLOOK: "Outlook",
        }

        suffix = suffix_map.get(significance, "")
        return f"{base_name} {suffix}".strip()


# Convenience function
def parse_alert(alert_data: Union[dict, str], source: str = "unknown") -> Optional[Alert]:
    """Parse an alert from API JSON or raw text."""
    return AlertParser.parse(alert_data, source)
