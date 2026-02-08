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
    get_wfo_name,
)
from ..utils.timezone import TimezoneHelper
from ..services.ugc_service import get_display_locations as ugc_get_display_locations

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
            event_name = properties.get("event", "")

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
                elif "Watch" in event_name or "Warning" in event_name:
                    # Debug: log why VTEC wasn't found for watch/warning alerts
                    logger.debug(
                        f"VTEC not found in description for {event_name}. "
                        f"Parameters keys: {list(parameters.keys())}"
                    )
            else:
                vtec_data = VTECParser.parse(vtec_str)
                if vtec_data.is_valid:
                    alert.vtec = vtec_data.vtec_info
                else:
                    logger.warning(f"VTEC string found but invalid: {vtec_str[:100]}")

            # Build product ID
            if alert.vtec:
                alert.product_id = VTECParser.build_product_id(alert.vtec)
                alert.phenomenon = alert.vtec.phenomenon
                alert.significance = alert.vtec.significance
                alert.sender_office = alert.vtec.office

                # Debug logging for watch merging
                if alert.vtec.significance == AlertSignificance.WATCH:
                    logger.info(
                        f"Watch parsed: ETN={alert.vtec.event_tracking_number}, "
                        f"office={alert.vtec.office}, product_id={alert.product_id}"
                    )

                # Handle cancellations
                if VTECParser.is_cancellation(alert.vtec):
                    alert.status = AlertStatus.CANCELLED
            else:
                # Fallback product ID from API ID
                event_name_for_log = properties.get("event", "Unknown")
                if alert.message_id:
                    alert.product_id = alert.message_id.split("/")[-1]
                    logger.warning(f"No VTEC found for '{event_name_for_log}', using fallback ID: {alert.product_id}")
                else:
                    alert.product_id = f"api_{datetime.now(timezone.utc).timestamp()}"
                    logger.warning(f"No VTEC or message_id for '{event_name_for_log}', using timestamp ID: {alert.product_id}")

            # Extract event information
            alert.event_name = properties.get("event", "")
            alert.headline = properties.get("headline", "")
            alert.description = properties.get("description", "")
            alert.instruction = properties.get("instruction", "")

            # Sender name - use API value or lookup from WFO code
            alert.sender_name = properties.get("senderName", "")
            if not alert.sender_name and alert.sender_office:
                alert.sender_name = get_wfo_name(alert.sender_office)

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

            # UGC codes from geocode
            ugc_list = geocode.get("UGC", [])
            if ugc_list:
                alert.affected_areas = ugc_list if isinstance(ugc_list, list) else [ugc_list]

            # Fallback: extract UGC codes from affectedZones URLs if geocode is empty
            # URLs look like: https://api.weather.gov/zones/forecast/OHZ049
            if not alert.affected_areas:
                affected_zones = properties.get("affectedZones", [])
                if affected_zones:
                    extracted_ugc = []
                    for zone_url in affected_zones:
                        if isinstance(zone_url, str):
                            # Extract last path segment (the zone ID)
                            zone_id = zone_url.rstrip("/").split("/")[-1]
                            # Validate it looks like a UGC code (2 letters + C/Z + 3 digits)
                            if len(zone_id) == 6 and zone_id[:2].isalpha() and zone_id[2] in "CZ" and zone_id[3:].isdigit():
                                extracted_ugc.append(zone_id.upper())
                    if extracted_ugc:
                        alert.affected_areas = extracted_ugc
                        logger.debug(f"Extracted {len(extracted_ugc)} UGC codes from affectedZones URLs")

            # FIPS/SAME codes
            same_codes = geocode.get("SAME", [])
            if same_codes:
                # Normalize to 5-digit FIPS
                alert.fips_codes = [
                    code[-5:].zfill(5)
                    for code in same_codes
                    if code and len(code) >= 5
                ]

            # Area description - use UGC service if areaDesc is empty or just UGC codes
            area_desc = properties.get("areaDesc", "")
            if area_desc and not cls._looks_like_ugc_codes(area_desc):
                alert.display_locations = area_desc
            elif alert.affected_areas:
                # Use UGC service to get human-readable names
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)
            else:
                alert.display_locations = area_desc

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

            # Filter by target phenomena from config
            if not cls._is_target_phenomenon(alert.phenomenon):
                logger.debug(f"Filtering out non-target phenomenon: {alert.phenomenon} ({alert.event_name})")
                return None

            # Filter by target states from config
            if not cls._is_target_state(alert.affected_areas):
                logger.debug(f"Filtering out alert for non-target state: {alert.affected_areas}")
                return None

            # Filter affected_areas to only include counties from target states
            # This ensures multi-state alerts only show our target state's counties
            original_areas = alert.affected_areas.copy() if alert.affected_areas else []
            alert.affected_areas = cls._filter_to_target_states(alert.affected_areas)

            # Regenerate display_locations from filtered areas if we filtered any out
            if alert.affected_areas and len(alert.affected_areas) < len(original_areas):
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)

            # Reject alerts with no valid affected_areas (after filtering)
            if not alert.affected_areas:
                logger.warning(
                    f"Rejecting API alert {alert.product_id} - no valid affected_areas after filtering "
                    f"(original: {original_areas})"
                )
                return None

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
            # Filter out informational products that shouldn't create alerts
            if cls._is_informational_product(raw_text):
                logger.debug("Filtering out informational product (HWO, etc.)")
                return None

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

            # Parse location description - prefer UGC service for human-readable names
            location_desc = cls._parse_location_description(raw_text, is_xml)
            if location_desc and not cls._looks_like_ugc_codes(location_desc):
                alert.display_locations = location_desc
            elif alert.affected_areas:
                # Use UGC service to get human-readable county/zone names
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)
            else:
                alert.display_locations = location_desc

            # Parse polygon
            alert.polygon = cls._parse_text_polygon(raw_text, is_xml)
            if alert.polygon:
                alert.centroid = cls._calculate_centroid(alert.polygon)

            # Parse threat data
            alert.threat = ThreatParser.parse(raw_text, is_xml)

            # Set event name
            if alert.phenomenon:
                alert.event_name = cls._build_event_name(alert.phenomenon, alert.significance)

            # Set sender name from WFO code if not already set
            if not alert.sender_name and alert.sender_office:
                alert.sender_name = get_wfo_name(alert.sender_office)

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

            # Filter by target phenomena from config
            if not cls._is_target_phenomenon(alert.phenomenon):
                logger.debug(f"Filtering out non-target phenomenon: {alert.phenomenon}")
                return None

            # Filter by target states from config
            if not cls._is_target_state(alert.affected_areas):
                logger.debug(f"Filtering out alert for non-target state: {alert.affected_areas}")
                return None

            # Filter affected_areas to only include counties from target states
            # This ensures multi-state alerts only show our target state's counties
            original_areas = alert.affected_areas.copy() if alert.affected_areas else []
            alert.affected_areas = cls._filter_to_target_states(alert.affected_areas)

            # Regenerate display_locations from filtered areas if we filtered any out
            if alert.affected_areas and len(alert.affected_areas) < len(original_areas):
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)

            # Reject alerts with no valid affected_areas (after filtering)
            # This catches malformed alerts like SPC watches with invalid UGC codes
            if not alert.affected_areas:
                logger.warning(
                    f"Rejecting alert {alert.product_id} - no valid affected_areas after filtering "
                    f"(original: {original_areas})"
                )
                return None

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
                logger.debug(f"[POLYGON] Found {len(values)} coordinate values in LAT...LON section")

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
            logger.debug(f"[POLYGON] Successfully parsed polygon with {len(coords)} vertices")
        elif not coords:
            logger.debug("[POLYGON] No polygon coordinates found in text")

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
    def _is_informational_product(cls, text: str) -> bool:
        """
        Check if this is an informational product that should NOT create alert cards.

        These products mention watches/warnings for informational purposes but
        are not actual alerts themselves.

        Filtered products:
        - HWO (Hazardous Weather Outlook) - FLUS header or "Hazardous Weather Outlook" title
        - PNS (Public Information Statement)
        - NOW (Short Term Forecast)
        - ZFP (Zone Forecast Product)
        """
        upper_text = text.upper()

        # Check for HWO - Hazardous Weather Outlook
        if "HAZARDOUS WEATHER OUTLOOK" in upper_text:
            return True

        # Check for FLUS header (HWO products)
        if "FLUS" in upper_text[:100]:  # Header is near the start
            return True

        # Check for HWO PIL (product identifier like HWOIWX, HWOCLE, etc.)
        import re
        if re.search(r'\bHWO[A-Z]{2,4}\b', upper_text[:200]):
            return True

        # Check for other informational products by WMO header
        # These appear in first ~50 characters
        header_area = upper_text[:50]
        informational_headers = [
            "NOUS",  # Public Information Statement
            "FPUS",  # Zone Forecast (mentions weather but isn't an alert)
        ]
        for header in informational_headers:
            if header in header_area:
                return True

        return False

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

    @classmethod
    def _is_target_phenomenon(cls, phenomenon: str) -> bool:
        """
        Check if a phenomenon should be processed based on config.

        Returns True if:
        - target_phenomena is empty (show all)
        - phenomenon is in target_phenomena list

        Args:
            phenomenon: Phenomenon code (e.g., "TO", "SV", "FF")

        Returns:
            True if alert should be processed, False to filter out
        """
        if not phenomenon:
            return False

        # Import here to avoid circular import
        from ..config import get_settings
        settings = get_settings()
        target_list = settings.target_phenomena

        # Empty list = accept all phenomena
        if not target_list:
            return True

        # Check if phenomenon is in target list (case-insensitive)
        return phenomenon.upper() in [p.upper() for p in target_list]

    @classmethod
    def _is_target_state(cls, affected_areas: list[str]) -> bool:
        """
        Check if any affected area matches the target states from config.

        UGC codes start with a 2-letter state code (e.g., "OHC049" = Ohio County 049).
        Returns True if:
        - filter_states is empty (accept all)
        - any UGC code's state matches a target state

        Args:
            affected_areas: List of UGC codes (e.g., ["OHC049", "OHC041"])

        Returns:
            True if alert should be processed, False to filter out
        """
        # Import here to avoid circular import
        from ..config import get_settings
        settings = get_settings()
        filter_states = settings.filter_states

        # Empty list = accept all states
        if not filter_states:
            return True

        # No affected areas = can't determine state, reject it
        # This ensures we don't show alerts from unknown states
        if not affected_areas:
            logger.debug("Rejecting alert with empty affected_areas - cannot determine state")
            return False

        # Extract state codes from UGC codes (first 2 characters)
        alert_states = set()
        for ugc in affected_areas:
            if len(ugc) >= 2:
                state_code = ugc[:2].upper()
                alert_states.add(state_code)

        # Check if any alert state matches target states
        target_states_upper = {s.upper() for s in filter_states}
        matching_states = alert_states & target_states_upper

        if matching_states:
            return True

        return False

    @classmethod
    def _filter_to_target_states(cls, affected_areas: list[str]) -> list[str]:
        """
        Filter affected_areas to only include UGC codes from target states.

        This ensures that when an alert spans multiple states (e.g., OH and IN),
        we only show/display the counties from our target states.

        Args:
            affected_areas: List of UGC codes (e.g., ["OHC049", "INC001", "OHC041"])

        Returns:
            Filtered list containing only codes from target states
        """
        # Import here to avoid circular import
        from ..config import get_settings
        settings = get_settings()
        filter_states = settings.filter_states

        # Empty filter list = no filtering, return all
        if not filter_states:
            return affected_areas

        # No areas to filter
        if not affected_areas:
            return affected_areas

        target_states_upper = {s.upper() for s in filter_states}

        # Filter to only include UGC codes from target states
        filtered = [
            ugc for ugc in affected_areas
            if len(ugc) >= 2 and ugc[:2].upper() in target_states_upper
        ]

        if len(filtered) < len(affected_areas):
            logger.debug(
                f"Filtered affected_areas from {len(affected_areas)} to {len(filtered)} "
                f"(keeping only {filter_states})"
            )

        return filtered

    @classmethod
    def _looks_like_ugc_codes(cls, text: str) -> bool:
        """
        Check if text looks like raw UGC codes rather than location names.

        Examples of UGC code patterns:
        - "OHC049" (county)
        - "OHZ049" (zone)
        - "OHC049-OHC041" (multiple codes)
        """
        import re
        # Check if the text is mostly UGC codes (2-letter state + C/Z + 3 digits)
        ugc_pattern = re.compile(r'^[A-Z]{2}[CZ]\d{3}(?:\s*[-;,]\s*[A-Z]{2}[CZ]\d{3})*$')
        # Also check if it starts with UGC-like pattern
        starts_with_ugc = re.compile(r'^[A-Z]{2}[CZ]\d{3}')

        clean_text = text.strip()
        if ugc_pattern.match(clean_text):
            return True
        if starts_with_ugc.match(clean_text) and len(clean_text) < 50:
            return True
        return False


# Convenience function
def parse_alert(alert_data: Union[dict, str], source: str = "unknown") -> Optional[Alert]:
    """Parse an alert from API JSON or raw text."""
    return AlertParser.parse(alert_data, source)
