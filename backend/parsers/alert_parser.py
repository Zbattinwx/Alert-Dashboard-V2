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

import hashlib
import logging
import re
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
    def parse_multi(cls, alert_data: Union[dict, str], source: str = "unknown") -> list[Alert]:
        """
        Parse an alert that may contain multiple segments (multi-VTEC products).

        NWS products like "URGENT - WINTER WEATHER MESSAGE" can contain multiple
        segments separated by $$, each with its own UGC block and VTEC line
        (e.g., Winter Storm Warning for one area + Winter Weather Advisory for another).

        Returns a list of parsed alerts (may be 1 for single-segment products).
        """
        if isinstance(alert_data, dict):
            result = cls.parse_api_alert(alert_data, source)
            return [result] if result else []

        if not isinstance(alert_data, str):
            return []

        segments = cls._split_text_segments(alert_data)

        if len(segments) <= 1:
            # Single segment — use normal parser
            result = cls.parse_text_alert(alert_data, source)
            return [result] if result else []

        # Multi-segment product — parse each segment independently
        # Preserve the WMO header (first few lines before the first UGC block)
        # so each segment has context for office/timestamp parsing
        header = cls._extract_product_header(alert_data)
        alerts = []

        for i, segment_text in enumerate(segments):
            try:
                # Prepend the product header so each segment has WMO/office context
                full_segment = header + "\n" + segment_text if header else segment_text
                alert = cls.parse_text_alert(full_segment, source)
                if alert:
                    logger.info(
                        f"Multi-segment product: segment {i+1}/{len(segments)} "
                        f"parsed as {alert.phenomenon}.{alert.significance.value if hasattr(alert.significance, 'value') else alert.significance}"
                        f" ({alert.product_id}), {len(alert.affected_areas)} zones"
                    )
                    alerts.append(alert)
            except Exception as e:
                logger.error(f"Error parsing segment {i+1}/{len(segments)}: {e}")

        return alerts

    @classmethod
    def _split_text_segments(cls, raw_text: str) -> list[str]:
        """
        Split a multi-segment NWS product into individual segments.

        NWS products use $$ as segment separators. Each segment has its own
        UGC block and VTEC line. Only splits if multiple VTEC lines exist.

        Returns list of segment strings. Returns [raw_text] if not multi-segment.
        """
        # Quick check: does this product have multiple VTEC lines?
        all_vtecs = VTECParser.parse_all(raw_text)
        if len(all_vtecs) <= 1:
            return [raw_text]

        # Check if different VTEC lines are actually for different products
        # (different phenomenon or different ETN)
        unique_products = set()
        for v in all_vtecs:
            if v.is_valid:
                key = (v.vtec_info.phenomenon, v.vtec_info.significance.value
                       if hasattr(v.vtec_info.significance, 'value') else v.vtec_info.significance,
                       v.vtec_info.event_tracking_number)
                unique_products.add(key)

        if len(unique_products) <= 1:
            # All VTECs are for the same product (e.g., CAN+CON for same warning)
            return [raw_text]

        # Split on $$ separator
        segments = re.split(r'\n\$\$\s*\n', raw_text)

        # Filter to only segments that contain a VTEC line
        vtec_segments = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            vtec_check = VTECParser.parse(seg)
            if vtec_check.is_valid:
                vtec_segments.append(seg)

        if len(vtec_segments) > 1:
            logger.info(
                f"Split multi-segment product into {len(vtec_segments)} segments "
                f"({len(all_vtecs)} VTEC lines, {len(unique_products)} unique products)"
            )
            return vtec_segments

        # Couldn't split meaningfully — return original
        return [raw_text]

    @classmethod
    def _extract_product_header(cls, raw_text: str) -> str:
        """
        Extract the WMO header lines from a product (before the first UGC block).
        This includes the WMO abbreviated heading, product type, and office line.
        """
        lines = raw_text.split('\n')
        header_lines = []
        for line in lines:
            # Stop when we hit a UGC line (starts with SSC### or SSZ###)
            if re.match(r'^[A-Z]{2}[CZ]\d{3}', line.strip()):
                break
            header_lines.append(line)
        return '\n'.join(header_lines)

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

            # Extract event information
            alert.event_name = properties.get("event", "")
            alert.headline = properties.get("headline", "")
            alert.description = properties.get("description", "")
            alert.instruction = properties.get("instruction", "")

            # Parse timestamps early for SPS ID generation
            sent_str = properties.get("sent")
            if sent_str:
                alert.issued_time = TimezoneHelper.parse_iso_timestamp(sent_str)

            # Extract geographic codes early for SPS ID generation
            geocode = properties.get("geocode", {})
            ugc_list = geocode.get("UGC", [])
            if ugc_list:
                alert.affected_areas = ugc_list if isinstance(ugc_list, list) else [ugc_list]

            if not alert.affected_areas:
                affected_zones = properties.get("affectedZones", [])
                if affected_zones:
                    extracted_ugc = []
                    for zone_url in affected_zones:
                        if isinstance(zone_url, str):
                            zone_id = zone_url.rstrip("/").split("/")[-1]
                            if len(zone_id) == 6 and zone_id[:2].isalpha() and zone_id[2] in "CZ" and zone_id[3:].isdigit():
                                extracted_ugc.append(zone_id.upper())
                    if extracted_ugc:
                        alert.affected_areas = extracted_ugc
                        logger.debug(f"Extracted {len(extracted_ugc)} UGC codes from affectedZones URLs")

            # Build product ID
            if alert.vtec:
                alert.product_id = VTECParser.build_product_id(alert.vtec)
                alert.phenomenon = alert.vtec.phenomenon
                alert.significance = alert.vtec.significance
                alert.sender_office = alert.vtec.office

                if alert.vtec.significance == AlertSignificance.WATCH:
                    logger.info(
                        f"Watch parsed: ETN={alert.vtec.event_tracking_number}, "
                        f"office={alert.vtec.office}, product_id={alert.product_id}"
                    )
                if VTECParser.is_cancellation(alert.vtec):
                    alert.status = AlertStatus.CANCELLED
            else:
                # No VTEC - determine phenomenon and check for SPS
                if not alert.phenomenon and alert.event_name:
                    alert.phenomenon = cls._event_name_to_phenomenon(alert.event_name)
                    if alert.phenomenon == "SPS":
                        alert.significance = AlertSignificance.STATEMENT

                # Special handling for SPS to generate a consistent ID
                if alert.phenomenon == "SPS" and alert.issued_time and alert.affected_areas:
                    sps_id = cls._generate_sps_id(alert.affected_areas, alert.issued_time)
                    if sps_id:
                        alert.product_id = sps_id
                        logger.info(f"Generated consistent SPS ID for API alert: {sps_id}")

                if not alert.product_id:
                    event_name_for_log = properties.get("event", "Unknown")
                    if alert.message_id:
                        alert.product_id = alert.message_id.split("/")[-1]
                        # Non-VTEC products (Air Quality Alert, Hydrologic Outlook, etc.)
                        # legitimately have no VTEC; the message URN is the correct ID
                        # fallback. Most are non-target phenomena filtered out below, so
                        # this is expected — debug, not a warning.
                        logger.debug(f"No VTEC found for '{event_name_for_log}', using fallback ID: {alert.product_id}")
                    else:
                        alert.product_id = f"api_{datetime.now(timezone.utc).timestamp()}"
                        logger.warning(f"No VTEC or message_id for '{event_name_for_log}', using timestamp ID: {alert.product_id}")

            # Sender name
            alert.sender_name = properties.get("senderName", "")
            if not alert.sender_name and alert.sender_office:
                alert.sender_name = get_wfo_name(alert.sender_office)

            # Finish parsing remaining timestamps
            ends_str = properties.get("ends")
            expires_str = properties.get("expires")
            effective_str = properties.get("effective")
            onset_str = properties.get("onset")

            if ends_str:
                alert.expiration_time = TimezoneHelper.parse_iso_timestamp(ends_str)
            elif expires_str:
                alert.expiration_time = TimezoneHelper.parse_iso_timestamp(expires_str)
                alert.message_expires = alert.expiration_time

            if effective_str:
                alert.effective_time = TimezoneHelper.parse_iso_timestamp(effective_str)
            if onset_str:
                alert.onset_time = TimezoneHelper.parse_iso_timestamp(onset_str)

            # FIPS/SAME codes
            same_codes = geocode.get("SAME", [])
            if same_codes:
                alert.fips_codes = [
                    code[-5:].zfill(5)
                    for code in same_codes
                    if code and len(code) >= 5
                ]

            # Area description
            area_desc = properties.get("areaDesc", "")
            if area_desc and not cls._looks_like_ugc_codes(area_desc):
                alert.display_locations = area_desc
            elif alert.affected_areas:
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)
            else:
                alert.display_locations = area_desc

            # Parse polygon from geometry
            if geometry:
                alert.polygon = cls._parse_geojson_geometry(geometry)
                if alert.polygon:
                    alert.centroid = cls._calculate_centroid(alert.polygon)

            if not alert.polygon and alert.description:
                alert.polygon = cls._parse_text_polygon(alert.description, is_xml=False)
                if alert.polygon:
                    alert.centroid = cls._calculate_centroid(alert.polygon)
                    logger.info(f"Parsed polygon from description text for {alert.product_id}")

            # Parse threat data
            alert.threat = ThreatParser.parse(alert.description, is_xml=False)
            cls._parse_api_threat_parameters(parameters, alert)

            # Apply SPS filter
            if alert.phenomenon == "SPS":
                if not cls._is_relevant_sps(alert.description):
                    logger.debug(f"Filtering out non-thunderstorm SPS: {alert.product_id}")
                    return None

            # Assign default expiration
            if not alert.expiration_time and alert.phenomenon in cls.TARGETED_PHENOMENA:
                alert.expiration_time = datetime.now(timezone.utc) + timedelta(minutes=cls.DEFAULT_LIFETIME_MINUTES)
                logger.warning(
                    f"Assigned default {cls.DEFAULT_LIFETIME_MINUTES}-min expiration to "
                    f"{alert.product_id} (no expiration found in API)"
                )

            alert.raw_text = alert.description
            if not cls._is_target_phenomenon(alert.phenomenon):
                logger.debug(f"Filtering out non-target phenomenon: {alert.phenomenon} ({alert.event_name})")
                return None
            if not cls._is_target_state(alert.affected_areas):
                logger.debug(f"Filtering out alert for non-target state: {alert.affected_areas}")
                return None

            original_areas = alert.affected_areas.copy() if alert.affected_areas else []
            alert.affected_areas = cls._filter_to_target_states(alert.affected_areas)
            alert.affected_areas = cls._filter_to_target_counties(alert.affected_areas)
            if alert.affected_areas and len(alert.affected_areas) < len(original_areas):
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)

            if not alert.affected_areas:
                # Expected: alert touches the target state but none of the target
                # counties (county filter doing its job on adjacent-area alerts).
                # Repeats every poll for the same alerts — debug, not a warning.
                logger.debug(
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
        """
        try:
            if cls._is_informational_product(raw_text):
                logger.debug("Filtering out informational product (HWO, etc.)")
                return None

            alert = Alert(source=source, raw_text=raw_text)
            is_xml = is_xml_content(raw_text)

            # Parse VTEC
            vtec_data = VTECParser.parse(raw_text)
            if vtec_data.is_valid:
                # Check for multi-VTEC products (e.g., SVS with CAN + CON for same warning).
                # NWS cancels some counties and continues others in the same product.
                # The first VTEC line may be CAN, but if a CON also exists for the same
                # event, the warning is still active — use the CON instead.
                if VTECParser.is_cancellation(vtec_data.vtec_info):
                    all_vtecs = VTECParser.parse_all(raw_text)
                    for other in all_vtecs:
                        if (other.is_valid and
                            VTECParser.is_continuation(other.vtec_info) and
                            other.vtec_info.phenomenon == vtec_data.vtec_info.phenomenon and
                            other.vtec_info.event_tracking_number == vtec_data.vtec_info.event_tracking_number):
                            logger.info(
                                f"Multi-VTEC product: using {other.vtec_info.action.value} "
                                f"instead of CAN for "
                                f"{vtec_data.vtec_info.phenomenon}.{vtec_data.vtec_info.office}."
                                f"{vtec_data.vtec_info.event_tracking_number:04d}"
                            )
                            vtec_data = other
                            break

                alert.vtec = vtec_data.vtec_info
                alert.product_id = VTECParser.build_product_id(alert.vtec)
                alert.phenomenon = alert.vtec.phenomenon
                alert.significance = alert.vtec.significance
                alert.sender_office = alert.vtec.office
                if alert.vtec.begin_time:
                    alert.effective_time = alert.vtec.begin_time
                    alert.onset_time = alert.vtec.begin_time
                if alert.vtec.end_time:
                    alert.expiration_time = alert.vtec.end_time
                if VTECParser.is_cancellation(alert.vtec):
                    alert.status = AlertStatus.CANCELLED
                for warning in vtec_data.validation_warnings:
                    logger.warning(f"VTEC warning for {alert.product_id}: {warning}")
                # Parse issued time from text body (e.g., "1230 PM CST Sat Feb 14 2026")
                alert.issued_time = TimezoneHelper.parse_nwws_timestamp(raw_text)

            # This block handles non-VTEC alerts (like SPS)
            else:
                alert.issued_time = TimezoneHelper.parse_nwws_timestamp(raw_text)
                header_text = raw_text[:500]
                alert.phenomenon = cls._event_name_to_phenomenon(header_text)
                if alert.phenomenon:
                    alert.significance = AlertSignificance.STATEMENT
                    alert.event_name = cls._build_event_name(alert.phenomenon, alert.significance)

            # Parse UGC codes (needed for SPS ID generation)
            ugc_data = UGCParser.parse(raw_text)
            if ugc_data.is_valid:
                alert.affected_areas = ugc_data.ugc_codes
                alert.fips_codes = ugc_data.fips_codes
                if not alert.expiration_time and ugc_data.expiration_time:
                    alert.expiration_time = ugc_data.expiration_time

            # Multi-segment products (e.g. a WCN that continues some watch
            # counties while clearing others) union every county via the
            # whole-product UGC parse above. Re-classify per $$-segment so the
            # cleared counties are tracked separately and don't get merged in.
            if alert.vtec and ugc_data.is_valid:
                active_ugc, cancelled_ugc = cls._compute_segment_areas(
                    raw_text, alert.vtec.event_tracking_number
                )
                if cancelled_ugc:
                    alert.cancelled_areas = sorted(cancelled_ugc)
                    # When some counties continue, the active set is just those
                    # (drop the cleared ones). For a pure cancellation (no active
                    # segment) leave affected_areas as-is — the alert is CANCELLED
                    # and add_alert subtracts cancelled_areas from the existing one.
                    if active_ugc:
                        alert.affected_areas = sorted(active_ugc)

            # Generate consistent ID for SPS if no VTEC
            if not alert.vtec and alert.phenomenon == "SPS":
                if alert.issued_time and alert.affected_areas:
                    sps_id = cls._generate_sps_id(alert.affected_areas, alert.issued_time)
                    if sps_id:
                        alert.product_id = sps_id
                        logger.info(f"Generated consistent SPS ID for text alert: {sps_id}")

            # Fallback ID generation if still no ID
            if not alert.product_id:
                watch_match = PATTERN_WATCH_TYPE.search(raw_text)
                if watch_match:
                    watch_type = watch_match.group(1).upper()
                    watch_number = watch_match.group(2)
                    alert.phenomenon = "TO" if "TORNADO" in watch_type else "SV"
                    alert.significance = AlertSignificance.WATCH
                    alert.product_id = f"{alert.phenomenon}A.SPC.{watch_number.zfill(4)}"
                else:
                    alert.product_id = f"nwws_{datetime.now(timezone.utc).timestamp()}"
                    for error in vtec_data.validation_errors:
                        logger.debug(f"VTEC parse issue: {error}")

            if is_xml:
                xml_fips = UGCParser.parse_xml_fips(raw_text)
                if xml_fips:
                    alert.fips_codes = list(set(alert.fips_codes + xml_fips))

            if not alert.expiration_time:
                alert.expiration_time = cls._parse_text_expiration(raw_text, is_xml, alert.sender_office)

            location_desc = cls._parse_location_description(raw_text, is_xml)
            if location_desc and not cls._looks_like_ugc_codes(location_desc):
                alert.display_locations = location_desc
            elif alert.affected_areas:
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)
            else:
                alert.display_locations = location_desc

            # Extract description and instruction from body text
            alert.description, alert.instruction = cls._parse_text_body(raw_text)

            alert.polygon = cls._parse_text_polygon(raw_text, is_xml)
            if alert.polygon:
                alert.centroid = cls._calculate_centroid(alert.polygon)

            alert.threat = ThreatParser.parse(raw_text, is_xml)
            if alert.phenomenon:
                alert.event_name = cls._build_event_name(alert.phenomenon, alert.significance)
            if not alert.sender_name and alert.sender_office:
                alert.sender_name = get_wfo_name(alert.sender_office)

            # Generate headline from parsed metadata
            if alert.event_name and not alert.headline:
                parts = [alert.event_name]
                if alert.issued_time:
                    parts.append(f"issued {alert.issued_time.strftime('%B %d at %I:%M%p')}")
                if alert.sender_name:
                    parts.append(f"by {alert.sender_name}")
                alert.headline = " ".join(parts)

            if alert.phenomenon == "SPS":
                if not cls._is_relevant_sps(raw_text):
                    logger.debug(f"Filtering out non-thunderstorm SPS")
                    return None

            if not alert.expiration_time and alert.phenomenon in cls.TARGETED_PHENOMENA:
                alert.expiration_time = datetime.now(timezone.utc) + timedelta(minutes=cls.DEFAULT_LIFETIME_MINUTES)
                logger.warning(
                    f"Assigned default {cls.DEFAULT_LIFETIME_MINUTES}-min expiration to "
                    f"{alert.product_id} (no expiration found in text)"
                )

            if not cls._is_target_phenomenon(alert.phenomenon):
                logger.debug(f"Filtering out non-target phenomenon: {alert.phenomenon}")
                return None
            if not cls._is_target_state(alert.affected_areas):
                logger.debug(f"Filtering out alert for non-target state: {alert.affected_areas}")
                return None

            original_areas = alert.affected_areas.copy() if alert.affected_areas else []
            alert.affected_areas = cls._filter_to_target_states(alert.affected_areas)
            alert.affected_areas = cls._filter_to_target_counties(alert.affected_areas)
            if alert.affected_areas and len(alert.affected_areas) < len(original_areas):
                alert.display_locations = ugc_get_display_locations(alert.affected_areas)

            # Keep cancelled_areas filtered the same way so add_alert only ever
            # subtracts target-area counties from the stored watch.
            if alert.cancelled_areas:
                alert.cancelled_areas = cls._filter_to_target_counties(
                    cls._filter_to_target_states(alert.cancelled_areas)
                )

            if not alert.affected_areas:
                # Expected: alert touches the target state but none of the target
                # counties (county filter doing its job on adjacent-area alerts).
                # Repeats every poll for the same alerts — debug, not a warning.
                logger.debug(
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
    def _generate_sps_id(
        cls,
        ugc_codes: list[str],
        issued_time: datetime
    ) -> Optional[str]:
        """Generate a consistent product ID for non-VTEC Special Weather Statements."""
        if not all([ugc_codes, issued_time]):
            return None

        # Sort UGC codes to ensure consistent order
        sorted_ugc = sorted(ugc_codes)
        ugc_hash = hashlib.sha1("".join(sorted_ugc).encode()).hexdigest()[:8]

        # Format timestamp to nearest minute to handle small discrepancies
        # Use UTC to ensure consistency across timezones
        time_str = issued_time.astimezone(timezone.utc).strftime("%Y%m%d%H%M")

        # Using "adhoc" to indicate a non-VTEC, generated ID
        return f"SPS.adhoc.{time_str}.{ugc_hash}"

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
                logger.info(
                    f"[POLYGON] LAT...LON matched: {len(values)} values "
                    f"({len(values)//2} pairs), raw={repr(coord_text[:200])}"
                )

                # Values come in pairs: lat, lon
                if len(values) >= 2 and len(values) % 2 == 0:
                    for i in range(0, len(values), 2):
                        try:
                            # Values are in format DD.dd without decimal
                            # Need to divide by 100 to get decimal degrees
                            lat = float(values[i]) / 100.0
                            
                            raw_lon = float(values[i + 1]) / 100.0
                            
                            # NWS drops the leading 1 for longitudes >= 100W
                            # E.g., 104.50W is encoded as 0450 (which parses as 4.50)
                            if raw_lon < 40.0:
                                raw_lon += 100.0
                                
                            lon = -raw_lon  # West is negative

                            # Validate ranges
                            if 20 <= lat <= 60 and -130 <= lon <= -60:  # Reasonable US bounds
                                coords.append([lat, lon])
                            else:
                                logger.warning(f"[POLYGON] Coordinate out of range: {lat}, {lon}")
                        except (ValueError, IndexError):
                            continue
                elif len(values) > 0:
                    logger.warning(
                        f"[POLYGON] Odd number of values ({len(values)}), "
                        f"cannot form coordinate pairs"
                    )
            else:
                # Log a snippet of the text around where LAT...LON should be
                lat_pos = text.find("LAT...LON")
                if lat_pos >= 0:
                    snippet = text[lat_pos:lat_pos+200]
                    logger.warning(f"[POLYGON] Found LAT...LON but regex didn't match: {repr(snippet)}")

        # Ensure polygon is closed (first point = last point)
        if coords and len(coords) >= 3:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            logger.info(f"[POLYGON] Parsed polygon with {len(coords)} vertices")
        elif not coords:
            logger.info("[POLYGON] No polygon coordinates found in text")

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
    def _parse_text_body(cls, raw_text: str) -> tuple[str, str]:
        """
        Extract description and instruction from raw NWS product text.

        NWS products follow a standard format:
        - Header (WMO, product ID, UGC, VTEC, office, time)
        - Body description text
        - "PRECAUTIONARY/PREPAREDNESS ACTIONS..."
        - Instruction text
        - "&&"
        - LAT...LON, TIME...MOT, threat tags, $$

        Returns:
            Tuple of (description, instruction)
        """
        description = ""
        instruction = ""

        # Extract instruction from PRECAUTIONARY/PREPAREDNESS ACTIONS section
        precautionary_match = re.search(
            r"PRECAUTIONARY/PREPAREDNESS ACTIONS\.\.\.\s*\n(.*?)(?=\n&&|\n\n&&)",
            raw_text,
            re.DOTALL
        )
        if precautionary_match:
            instruction = precautionary_match.group(1).strip()

        # Extract description: text between the double-newline after the header
        # and the PRECAUTIONARY section (or && if no PRECAUTIONARY)
        # The header ends with the issuance time line, followed by a blank line,
        # then the body text begins
        #
        # Find the body start: look for the pattern after the issuance time line
        # (e.g., "1230 PM CST Sat Feb 14 2026\n\n")
        body_match = re.search(
            r"\d{3,4}\s+(?:AM|PM)\s+[A-Z]{2,4}\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{4}\s*\n\s*\n"
            r"(.*?)(?=PRECAUTIONARY/PREPAREDNESS|&&|\$\$|LAT\.\.\.LON)",
            raw_text,
            re.DOTALL | re.IGNORECASE
        )
        if body_match:
            description = body_match.group(1).strip()

        return description, instruction

    @classmethod
    def _split_product_segments(cls, raw_text: str) -> list[str]:
        """Split a multi-segment NWS product into its $$-delimited segments.

        A product with no $$ marker returns a single-element list (the whole
        text), so single-segment warnings are handled uniformly. Each segment
        carries its own UGC block + VTEC line at the top.
        """
        return re.split(r"\n\s*\$\$", raw_text)

    @classmethod
    def _compute_segment_areas(cls, raw_text: str, etn: Optional[int]) -> tuple[set, set]:
        """Classify each segment's UGC codes as active vs cancelled for one event.

        NWS Watch County Notifications (and some SVS products) continue an event
        for some counties (CON/EXT/NEW) while cancelling it for others
        (CAN/EXP/UPG), each in its own $$-delimited segment. ``UGCParser.parse``
        over the whole product unions *every* county including the cancelled
        ones, so we re-classify per segment here.

        Returns ``(active_ugc, cancelled_ugc)`` where ``cancelled_ugc`` excludes
        any county that is continued in another segment.
        """
        active: set[str] = set()
        cancelled: set[str] = set()
        for seg in cls._split_product_segments(raw_text):
            vtec_data = VTECParser.parse(seg)
            if not vtec_data.is_valid or not vtec_data.vtec_info:
                continue
            vinfo = vtec_data.vtec_info
            # Only this event's segments (a product is normally one event, but
            # guard against an unrelated VTEC line slipping in).
            if etn is not None and vinfo.event_tracking_number != etn:
                continue
            ugc = set(UGCParser.parse(seg).ugc_codes)
            if not ugc:
                continue
            if VTECParser.is_cancellation(vinfo):
                cancelled |= ugc
            else:
                active |= ugc
        return active, (cancelled - active)

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
            # Heat. NWS 2024 hazard simplification renamed EH from "Excessive
            # Heat" to "Extreme Heat" (same VTEC code) — accept both the new and
            # old display names so non-VTEC products still classify correctly.
            "EXTREME HEAT WARNING": "EH",
            "EXCESSIVE HEAT WARNING": "EH",
            "EXTREME HEAT WATCH": "EH",
            "EXCESSIVE HEAT WATCH": "EH",
            "HEAT ADVISORY": "HT",
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
        # Avoid doubling suffix (e.g. "Special Weather Statement Statement")
        if suffix and base_name.endswith(suffix):
            return base_name
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

    @staticmethod
    def _county_basename(name: str) -> str:
        """Normalize a UGC name to a comparable county base name.

        "Clark County, OH" -> "clark"; "Ashtabula Inland, OH" -> "ashtabula inland".
        """
        s = name.split(",")[0].strip()
        if s.lower().endswith(" county"):
            s = s[: -len(" county")].strip()
        return s.lower()

    @classmethod
    def _filter_to_target_counties(cls, affected_areas: list[str]) -> list[str]:
        """
        Narrow affected_areas to the user's selected counties (per-state).

        For each state that has a non-empty county selection, keep only areas
        whose county/zone name matches a selected county.  States without a
        selection are unaffected (all their counties pass).  Matching is by name
        so it works for both county ("C") and forecast-zone ("Z") UGC codes,
        which use different numbering but resolve to county-based names.
        """
        from ..config import get_settings
        from ..services.ugc_service import get_ugc_name

        settings = get_settings()
        filter_counties = getattr(settings, "filter_counties", None) or {}
        if not filter_counties or not affected_areas:
            return affected_areas

        # Per-state set of selected county base names.
        selected: dict[str, set[str]] = {}
        for state, codes in filter_counties.items():
            if not codes:
                continue
            selected[state.upper()] = {
                cls._county_basename(get_ugc_name(c)) for c in codes
            }
        if not selected:
            return affected_areas

        kept: list[str] = []
        for ugc in affected_areas:
            st = ugc[:2].upper() if len(ugc) >= 2 else ""
            sel = selected.get(st)
            if not sel:
                kept.append(ugc)  # no county restriction for this state
                continue
            base_words = set(cls._county_basename(get_ugc_name(ugc)).split())
            if any(set(name.split()).issubset(base_words) for name in sel):
                kept.append(ugc)

        if len(kept) < len(affected_areas):
            logger.debug(
                f"County filter: {len(affected_areas)} -> {len(kept)} areas"
            )
        return kept

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
