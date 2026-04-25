"""
Post templates for social media sharing.

Variable substitution templates for generating post text from alert and LSR data.
"""

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


# Emoji mapping by phenomenon code
PHENOMENON_EMOJIS = {
    "TO": "\u26a0\ufe0f",      # Warning sign - tornado
    "SV": "\u26a1",             # Lightning - severe thunderstorm
    "FF": "\U0001f30a",         # Water wave - flash flood
    "FL": "\U0001f30a",         # Water wave - flood
    "WS": "\u2744\ufe0f",      # Snowflake - winter storm
    "BZ": "\U0001f328\ufe0f",  # Cloud with snow - blizzard
    "IS": "\U0001f9ca",         # Ice - ice storm
    "LE": "\u2744\ufe0f",      # Snowflake - lake effect snow
    "WW": "\u2744\ufe0f",      # Snowflake - winter weather
    "WC": "\U0001f321\ufe0f",  # Thermometer - wind chill
    "CW": "\U0001f321\ufe0f",  # Thermometer - cold weather
    "HW": "\U0001f4a8",        # Dash/wind
    "SQ": "\u26a1",             # Lightning - squall
}

# LSR report type emojis
LSR_EMOJIS = {
    "TORNADO": "\U0001f32a\ufe0f",
    "HAIL": "\u26c8\ufe0f",
    "TSTM WND GST": "\U0001f4a8",
    "TSTM WND DMG": "\U0001f4a8",
    "FLASH FLOOD": "\U0001f30a",
    "FLOOD": "\U0001f30a",
    "SNOW": "\u2744\ufe0f",
    "ICE": "\U0001f9ca",
    "HEAVY RAIN": "\U0001f327\ufe0f",
    "FUNNEL CLOUD": "\U0001f32a\ufe0f",
}

# Alert templates
ALERT_TEMPLATES = {
    "default": (
        "{emoji} {event_name}\n\n"
        "{locations}\n\n"
        "{threats}\n\n"
        "Issued by {sender_name}\n"
        "Until {expiration}\n\n"
        "#OHwx #weather #TheBattinFront"
    ),
    "breaking": (
        "{emoji} BREAKING: {event_name}\n\n"
        "{headline}\n\n"
        "Locations: {locations}\n"
        "Threats: {threats}\n"
        "Expires: {expiration}\n\n"
        "#OHwx #SevereWeather #TheBattinFront"
    ),
    "tornado_emergency": (
        "\U0001f6a8 TORNADO EMERGENCY \U0001f6a8\n\n"
        "{locations}\n\n"
        "{threats}\n\n"
        "TAKE COVER NOW!\n\n"
        "Issued by {sender_name}\n"
        "#TornadoEmergency #OHwx #TheBattinFront"
    ),
    "minimal": (
        "{emoji} {event_name} - {locations}\n"
        "{threats}\n"
        "Until {expiration}\n\n"
        "#OHwx #TheBattinFront"
    ),
}

# LSR templates
LSR_TEMPLATES = {
    "single": (
        "{emoji} Storm Report: {report_type}\n"
        "{magnitude}\n"
        "Near {location}\n"
        "{time}\n\n"
        "#StormReport #OHwx #TheBattinFront"
    ),
    "summary": (
        "\U0001f4cb Storm Reports Summary\n"
        "Out of {count} reports, here are the most significant:\n\n"
        "{report_list}\n\n"
        "#StormReports #OHwx #TheBattinFront"
    ),
}


def _build_threat_summary(alert_data: dict) -> str:
    """Build a human-readable threat summary from alert data.

    Works with alert dicts (from Alert.to_dict() or API response).
    """
    threats = []
    threat = alert_data.get("threat", {})
    if isinstance(threat, str):
        return threat

    # Tornado
    tornado_detection = threat.get("tornado_detection")
    if tornado_detection:
        tornado_str = f"Tornado: {tornado_detection}"
        damage = threat.get("tornado_damage_threat")
        if damage:
            tornado_str += f" ({damage})"
        threats.append(tornado_str)

    # Wind
    wind_gust = threat.get("max_wind_gust_mph")
    if wind_gust:
        wind_str = f"Wind: {wind_gust} MPH"
        damage = threat.get("wind_damage_threat")
        if damage:
            wind_str += f" ({damage})"
        threats.append(wind_str)

    # Hail
    hail = threat.get("max_hail_size_inches")
    if hail:
        hail_str = f'Hail: {hail}" diameter'
        damage = threat.get("hail_damage_threat")
        if damage:
            hail_str += f" ({damage})"
        threats.append(hail_str)

    # Snow
    snow_max = threat.get("snow_amount_max_inches")
    if snow_max:
        snow_min = threat.get("snow_amount_min_inches")
        if snow_min:
            threats.append(f'Snow: {snow_min}-{snow_max}"')
        else:
            threats.append(f'Snow: Up to {snow_max}"')

    # Ice
    ice = threat.get("ice_accumulation_inches")
    if ice:
        threats.append(f'Ice: {ice}"')

    # Flash Flood
    flood = threat.get("flash_flood_detection")
    if flood:
        flood_str = f"Flooding: {flood}"
        damage = threat.get("flash_flood_damage_threat")
        if damage:
            flood_str += f" ({damage})"
        threats.append(flood_str)

    return ", ".join(threats) if threats else ""


def _format_time(iso_string: Optional[str]) -> str:
    """Format an ISO time string for display."""
    if not iso_string:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        eastern = ZoneInfo("America/New_York")
        local_dt = dt.astimezone(eastern)
        return local_dt.strftime("%-I:%M %p %Z")
    except Exception:
        return iso_string


def render_alert_template(alert_data: dict, template_name: str = "default") -> str:
    """Render a post template with alert data substitution.

    Args:
        alert_data: Alert dict (from Alert.to_dict() or API response).
        template_name: Template key from ALERT_TEMPLATES.

    Returns:
        Rendered post text.
    """
    template = ALERT_TEMPLATES.get(template_name, ALERT_TEMPLATES["default"])

    phenomenon = alert_data.get("phenomenon", "")
    emoji = PHENOMENON_EMOJIS.get(phenomenon, "\u26a0\ufe0f")
    threats = _build_threat_summary(alert_data)

    values = {
        "emoji": emoji,
        "event_name": alert_data.get("event_name", "Weather Alert"),
        "headline": alert_data.get("headline", "") or "",
        "locations": alert_data.get("display_locations", "Multiple counties"),
        "threats": threats or "See alert for details",
        "sender_name": alert_data.get("sender_name", "") or alert_data.get("sender_office", "NWS"),
        "expiration": _format_time(alert_data.get("expiration_time")),
    }

    return template.format(**values)


def _parse_magnitude(mag_str: str) -> float:
    """Parse a magnitude string into a numeric value for sorting.

    Handles formats like:
    - "5.2" (snow inches, rain inches)
    - "70 MPH" or "70 mph" (wind gusts)
    - "1.75 INCH" (hail diameter)
    - "E1.75 INCH" (estimated hail)
    - "M70 MPH" (measured wind)
    - "" or None (no magnitude)
    """
    if not mag_str:
        return 0.0
    mag_str = str(mag_str).strip().upper()
    # Strip leading E (estimated) or M (measured) prefix
    if mag_str and mag_str[0] in ('E', 'M'):
        mag_str = mag_str[1:].strip()
    # Extract the first number
    import re
    match = re.search(r'(\d+\.?\d*)', mag_str)
    if match:
        return float(match.group(1))
    return 0.0


# Report type significance tiers - used as tiebreaker when magnitudes are equal
_REPORT_TYPE_PRIORITY = {
    "TORNADO": 100,
    "FUNNEL CLOUD": 90,
    "TSTM WND DMG": 80,
    "TSTM WND GST": 70,
    "HAIL": 65,
    "FLASH FLOOD": 60,
    "FLOOD": 55,
    "HEAVY RAIN": 40,
    "SNOW": 35,
    "ICE": 30,
}


def _sort_reports_by_significance(reports: list[dict]) -> list[dict]:
    """Sort storm reports by significance (highest magnitude first).

    Tornado reports always come first regardless of magnitude.
    Then sorts by parsed numeric magnitude descending.
    Uses report type priority as tiebreaker.
    """
    def sort_key(r: dict) -> tuple:
        rtype = r.get("report_type", "")
        type_priority = _REPORT_TYPE_PRIORITY.get(rtype, 10)
        mag = _parse_magnitude(r.get("magnitude") or "")

        # Tornadoes always on top
        is_tornado = 1 if rtype == "TORNADO" else 0

        # For wind: magnitude is in MPH (60-100+), for snow: inches (1-30),
        # for hail: inches (0.5-5). Normalize so they're comparable.
        # Wind: divide by 10 to bring into similar range as snow/hail
        if "WND" in rtype:
            normalized_mag = mag / 10.0
        else:
            normalized_mag = mag

        return (-is_tornado, -normalized_mag, -type_priority)

    return sorted(reports, key=sort_key)


def render_lsr_template(
    reports: list[dict], template_name: str = "summary"
) -> str:
    """Render a post template with storm report data.

    Args:
        reports: List of storm report dicts.
        template_name: Template key from LSR_TEMPLATES.

    Returns:
        Rendered post text.
    """
    if template_name == "single" and reports:
        report = reports[0]
        rtype = report.get("report_type", "UNKNOWN")
        emoji = LSR_EMOJIS.get(rtype, "\u26a0\ufe0f")

        city = report.get("city", "Unknown")
        state = report.get("state", "")
        location = f"{city}, {state}" if state else city

        return LSR_TEMPLATES["single"].format(
            emoji=emoji,
            report_type=rtype,
            magnitude=report.get("magnitude", ""),
            location=location,
            time=_format_time(report.get("valid_time")),
        )

    # Summary template - sort by magnitude to get top 10 most significant
    sorted_reports = _sort_reports_by_significance(reports)
    top_reports = sorted_reports[:10]

    lines = []
    for r in top_reports:
        rtype = r.get("report_type", "?")
        emoji = LSR_EMOJIS.get(rtype, "\u2022")
        city = r.get("city", "Unknown")
        state = r.get("state", "")
        mag = r.get("magnitude", "")
        location = f"{city}, {state}" if state else city
        line = f"{emoji} {rtype}: {mag}" if mag else f"{emoji} {rtype}"
        line += f" near {location}"
        lines.append(line)

    return LSR_TEMPLATES["summary"].format(
        count=len(reports),
        report_list="\n".join(lines),
    )
