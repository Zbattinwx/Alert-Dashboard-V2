"""
Compiled regex patterns for parsing NWS alerts.

This module contains all regex patterns used for parsing weather alerts.
Patterns are pre-compiled for performance and documented for maintainability.

References:
- pyIEM VTEC patterns: https://github.com/akrherz/pyIEM
- NWS VTEC documentation: https://www.weather.gov/vtec/
- CAP v1.2 specification: https://docs.oasis-open.org/emergency/cap/v1.2/
"""

import re
from typing import Pattern


# =============================================================================
# VTEC PATTERNS
# =============================================================================

# Primary VTEC pattern (P-VTEC)
# Format: /k.aaa.cccc.pp.s.####.yymmddThhnnZB-yymmddThhnnZE/
# Example: /O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/
#
# Components:
#   k    = Product class (O=Operational, T=Test, E=Experimental, X=Experimental in operational)
#   aaa  = Action code (NEW, CON, EXT, EXA, EXB, UPG, CAN, EXP, COR, ROU)
#   cccc = WFO identifier (4 chars, e.g., KCLE)
#   pp   = Phenomenon code (2 chars, e.g., TO, SV, FF)
#   s    = Significance (W=Warning, A=Watch, Y=Advisory, S=Statement, O=Outlook, N=Synopsis, F=Forecast)
#   #### = Event Tracking Number (4 digits)
#   timestamps = Begin and end times in UTC
#
# Pattern based on pyIEM: https://github.com/akrherz/pyIEM/blob/main/src/pyiem/nws/vtec.py
PATTERN_VTEC: Pattern[str] = re.compile(
    r"/([OTEX])\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([WAYSONF])\.(\d{4})\."
    r"(\d{6}T\d{4}Z)-(\d{6}T\d{4}Z)/",
    re.MULTILINE
)

# Simpler VTEC pattern for initial detection (captures entire VTEC string)
PATTERN_VTEC_SIMPLE: Pattern[str] = re.compile(
    r"(/[OTEX]\.[A-Z]{3}\.[A-Z]{4}\.[A-Z]{2}\.[WAYSONF]\.\d{4}\.\d{6}T\d{4}Z-\d{6}T\d{4}Z/)"
)

# Hydrologic VTEC (H-VTEC) for flood products
# Format: /s.ic.yymmddThhnnZB.yymmddThhnnZC.yymmddThhnnZE.fr/
PATTERN_HVTEC: Pattern[str] = re.compile(
    r"/([0-3NUMO])\.([A-Z]{2})\."
    r"(\d{6}T\d{4}Z)\.(\d{6}T\d{4}Z)\.(\d{6}T\d{4}Z)\."
    r"([A-Z]{2})/"
)

# Valid VTEC action codes
VALID_VTEC_ACTIONS = {"NEW", "CON", "EXT", "EXA", "EXB", "UPG", "CAN", "EXP", "COR", "ROU"}

# Valid VTEC significance codes
VALID_VTEC_SIGNIFICANCE = {"W", "A", "Y", "S", "O", "N", "F"}


# =============================================================================
# UGC (Universal Geographic Code) PATTERNS
# =============================================================================

# UGC line pattern
# Format: SSC###-###-###>###-DDHHMM-
# Example: OHC049-041-061>065-201530-
#
# Components:
#   SS  = State abbreviation (2 chars)
#   C/Z = County (C) or Zone (Z)
#   ### = Code number (3 digits)
#   >   = Range indicator (e.g., 061>065 = 061, 062, 063, 064, 065)
#   DDHHMM = Expiration day/hour/minute (UTC)
PATTERN_UGC_LINE: Pattern[str] = re.compile(
    r"^([A-Z]{2}[CZ])(\d{3}(?:[->]\d{3})*)-",
    re.MULTILINE
)

# Full UGC block pattern (captures entire UGC section)
PATTERN_UGC_BLOCK: Pattern[str] = re.compile(
    r"^([A-Z]{2}[CZ][\d\->]+(?:-[A-Z]{2}[CZ][\d\->]+|-\d{3})*-\d{6}-)$",
    re.MULTILINE
)

# UGC expiration timestamp at end of UGC block
# Format: DDHHMM (day, hour, minute in UTC)
PATTERN_UGC_EXPIRATION: Pattern[str] = re.compile(
    r"-(\d{6})-?\s*$"
)

# Individual UGC code extraction
PATTERN_UGC_CODE: Pattern[str] = re.compile(
    r"([A-Z]{2}[CZ])(\d{3})"
)

# UGC range (e.g., "001>005" means 001, 002, 003, 004, 005)
PATTERN_UGC_RANGE: Pattern[str] = re.compile(
    r"(\d{3})>(\d{3})"
)


# =============================================================================
# FIPS/SAME CODE PATTERNS
# =============================================================================

# FIPS code in XML/CAP format
# Format: <valueName>FIPS6</valueName><value>039049</value>
# or: <valueName>SAME</valueName><value>039049</value>
PATTERN_XML_FIPS: Pattern[str] = re.compile(
    r"<valueName>(?:FIPS6|SAME)</valueName>\s*<value>(\d{5,6})</value>",
    re.IGNORECASE
)

# SAME code pattern (6-digit format)
PATTERN_SAME_CODE: Pattern[str] = re.compile(r"\b(\d{6})\b")


# =============================================================================
# POLYGON/COORDINATE PATTERNS
# =============================================================================

# LAT...LON section in text alerts
# Example:
# LAT...LON 4105 8145 4098 8132 4087 8145
#           4093 8167 4105 8167
# Note: \$\$ matches the literal $$ end marker in NWS products
PATTERN_POLYGON_TEXT: Pattern[str] = re.compile(
    r"LAT\.\.\.LON\s+([\d\s]+?)(?=TIME\.\.\.MOT|\n\n|\$\$|&&|$)",
    re.DOTALL
)

# Individual coordinate values (4 or 5 digits)
# 4 digits: DDMM (degrees and minutes)
# 5 digits: DDDMM (for longitudes > 99 degrees)
PATTERN_COORD_VALUE: Pattern[str] = re.compile(r"(\d{4,5})")

# Polygon in XML/CAP format
# Format: <polygon>lat,lon lat,lon lat,lon</polygon>
PATTERN_POLYGON_XML: Pattern[str] = re.compile(
    r"<polygon>([\d\s,.\-]+)</polygon>",
    re.IGNORECASE
)

# GeoJSON polygon coordinates
# Used when parsing NWS API responses
PATTERN_GEOJSON_COORDS: Pattern[str] = re.compile(
    r"\[\s*\[\s*([\d\-.,\s\[\]]+)\s*\]\s*\]"
)


# =============================================================================
# TIME/EXPIRATION PATTERNS
# =============================================================================

# Watch/warning expiration in text
# Examples: "UNTIL 530 PM EST", "THROUGH 1145 PM CDT"
PATTERN_EXPIRATION_TEXT: Pattern[str] = re.compile(
    r"(?:UNTIL|THROUGH|EXPIRES?\s+(?:AT)?)\s+(\d{3,4})\s*(AM|PM)?\s*([A-Z]{2,4})?",
    re.IGNORECASE
)

# SPS-specific expiration pattern
PATTERN_SPS_EXPIRATION: Pattern[str] = re.compile(
    r"(?:UNTIL|EXPIRES?\s+(?:AT)?|THROUGH|AFTER|BY)\s+(\d{3,4})\s*(AM|PM)?\s*([A-Z]{2,4})?",
    re.IGNORECASE
)

# Watch expiration pattern
PATTERN_WATCH_EXPIRATION: Pattern[str] = re.compile(
    r"(?:UNTIL|THROUGH|VALID\s+UNTIL)\s+(\d{3,4})\s+(AM|PM)\s+([A-Z]{2,4})",
    re.IGNORECASE
)

# XML expires timestamp
PATTERN_XML_EXPIRES: Pattern[str] = re.compile(
    r"<expires>([\d\-T:+Z]+)</expires>",
    re.IGNORECASE
)

# XML eventEndingTime (preferred over expires)
PATTERN_XML_EVENT_END: Pattern[str] = re.compile(
    r"<eventEndingTime>([\d\-T:+Z]+)</eventEndingTime>",
    re.IGNORECASE
)

# Pattern for issuance time in raw text alerts (e.g., "339 PM CDT Mon Aug 8 2022")
PATTERN_ISSUED_TIME_LINE: Pattern[str] = re.compile(
    r"(\d{1,4})\s+(AM|PM)\s+([A-Z]{3,4})\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+(\d{4})",
    re.IGNORECASE
)



# =============================================================================
# THREAT/IMPACT PATTERNS
# =============================================================================

# Tornado detection
# Examples: "TORNADO...RADAR INDICATED", "TORNADO...OBSERVED"
PATTERN_TORNADO_DETECTION: Pattern[str] = re.compile(
    r"TORNADO\.{3}(RADAR\s+INDICATED|OBSERVED|POSSIBLE)",
    re.IGNORECASE
)

# Tornado damage threat tag
PATTERN_TORNADO_DAMAGE: Pattern[str] = re.compile(
    r"TORNADO\s+DAMAGE\s+THREAT\.{3}(CONSIDERABLE|CATASTROPHIC)",
    re.IGNORECASE
)

# Wind gust patterns - multiple formats
# Examples: "WIND...60 MPH", "MAX WIND GUST...70 MPH", "WIND GUSTS UP TO 80 MPH", "60 MPH WIND GUSTS"
# Also: "WINDS OF 35 TO 45 MPH", "GUSTS TO 60 MPH", "WIND GUSTS TO 50 MPH"
# Also: "gusts of 45 to 50 mph" - need to capture the higher number (50)
PATTERN_WIND_GUST: Pattern[str] = re.compile(
    r"(?:"
    r"(?:MAX\s+)?(?:WIND|GUST)S?(?:\s+GUST)?S?\.{0,3}\s*(?:UP\s+)?(?:TO\s+)?(\d{2,3})\s*(?:MPH|KT)"
    r"|"
    r"(\d{2,3})\s*(?:MPH|KT)\s+(?:WIND|GUST)S?"
    r"|"
    # "gusts of X to Y mph" or "gusts up to X mph" or "gusts to X mph"
    r"GUSTS?\s+(?:OF\s+)?(?:UP\s+)?(?:TO\s+)?(?:\d+\s+TO\s+)?(\d{2,3})\s*(?:MPH|KT)"
    r"|"
    r"WINDS?\s+(?:OF\s+)?(?:\d+\s+TO\s+)?(\d{2,3})\s*(?:MPH|KT)"
    r")",
    re.IGNORECASE
)

# Wind gust in XML/impact tags
PATTERN_WIND_XML: Pattern[str] = re.compile(
    r"<maxWindGust[^>]*>(\d+)\s*(?:mph|kts?)?</maxWindGust>",
    re.IGNORECASE
)

# Sustained wind pattern - captures "winds X to Y mph" before the "with gusts" part
# Examples: "winds 25 to 35 mph", "west winds 25 to 35 mph", "winds of 25 to 35 mph"
# Group 1 = min wind, Group 2 = max wind (optional, if no range given)
PATTERN_SUSTAINED_WIND: Pattern[str] = re.compile(
    r"(?:WEST|EAST|NORTH|SOUTH|NW|NE|SW|SE|N|S|E|W)?\s*WINDS?\s+(?:OF\s+)?(\d{2,3})\s+TO\s+(\d{2,3})\s*(?:MPH|KT)",
    re.IGNORECASE
)

# Wind damage threat tag
PATTERN_WIND_DAMAGE: Pattern[str] = re.compile(
    r"WIND\s+DAMAGE\s+THREAT\.{3}(CONSIDERABLE|DESTRUCTIVE|CATASTROPHIC)",
    re.IGNORECASE
)

# Hail size patterns - numeric values
# Examples: "HAIL...1.75 INCHES", "HAIL SIZE...1 IN", "HAIL UP TO 2 INCHES"
# Also handles: "1.75 IN HAIL", "2 INCH HAIL"
PATTERN_HAIL_SIZE: Pattern[str] = re.compile(
    r"(?:"
    r"(?:MAX\s+)?HAIL(?:\s+SIZE)?\.{0,3}\s*(?:UP\s+)?(?:TO\s+)?(\d+\.?\d*)\s*(?:INCH(?:ES)?|IN\b)"
    r"|"
    r"(\d+\.?\d*)\s*(?:INCH(?:ES)?|IN\.?)\s*(?:HAIL|SIZE)"
    r")",
    re.IGNORECASE
)

# Hail size in XML
PATTERN_HAIL_XML: Pattern[str] = re.compile(
    r"<maxHailSize[^>]*>(\d+\.?\d*)\s*(?:in)?</maxHailSize>",
    re.IGNORECASE
)

# Hail damage threat tag
PATTERN_HAIL_DAMAGE: Pattern[str] = re.compile(
    r"HAIL\s+DAMAGE\s+THREAT\.{3}(CONSIDERABLE|CATASTROPHIC)",
    re.IGNORECASE
)

# Hail size descriptions (convert to inches)
HAIL_SIZE_DESCRIPTIONS = {
    "PEA": 0.25,
    "MARBLE": 0.5,
    "DIME": 0.5,
    "PENNY": 0.75,
    "NICKEL": 0.88,
    "QUARTER": 1.0,
    "HALF DOLLAR": 1.25,
    "PING PONG": 1.5,
    "GOLF BALL": 1.75,
    "HEN EGG": 2.0,
    "TENNIS BALL": 2.5,
    "BASEBALL": 2.75,
    "APPLE": 3.0,
    "SOFTBALL": 4.0,
    "GRAPEFRUIT": 4.5,
}

# Pattern to match hail descriptions
# Requires "HAIL" or "SIZE" to be present to avoid false matches like "quarter mile"
PATTERN_HAIL_DESC: Pattern[str] = re.compile(
    r"(?:UP\s+TO\s+)?(" + "|".join(HAIL_SIZE_DESCRIPTIONS.keys()) + r")(?:\s+SIZE(?:D)?)?\s+HAIL"
    r"|"
    r"(" + "|".join(HAIL_SIZE_DESCRIPTIONS.keys()) + r")\s+SIZE(?:D)?",
    re.IGNORECASE
)


# =============================================================================
# SNOW/WINTER WEATHER PATTERNS
# =============================================================================

# Snow accumulation patterns - multiple formats
# Examples: "SNOW ACCUMULATION...4 TO 8 INCHES", "UP TO 6 INCHES OF SNOW"
# Also: "TOTAL SNOW ACCUMULATIONS OF 4 TO 8 INCHES", "3 TO 5 INCHES OF SNOW"
# Also: "SNOW EXPECTED...4-6 INCHES", "BETWEEN 3 AND 5 INCHES OF SNOW"
# Also: "UP TO 1 INCH OF QUICK SNOW ACCUMULATION"
PATTERN_SNOW_AMOUNT: Pattern[str] = re.compile(
    r"(?:"
    # Format 1: SNOW...4 TO 8 INCHES or ACCUMULATION...4 TO 8 INCHES
    r"(?:SNOW|ACCUMULATION)S?(?:\s+ACCUMULATION)?S?\.{0,3}\s*(?:OF\s+)?(?:UP\s+TO\s+)?(?:BETWEEN\s+)?(\d+\.?\d*)(?:\s*(?:TO|-|AND)\s*(\d+\.?\d*))?\s*INCH(?:ES)?"
    r"|"
    # Format 2: 4 TO 8 INCHES OF SNOW
    r"(\d+\.?\d*)(?:\s*(?:TO|-|AND)\s*(\d+\.?\d*))?\s*INCH(?:ES)?\s+(?:OF\s+)?(?:NEW\s+)?SNOW"
    r"|"
    # Format 3: UP TO X INCHES OF [adjective] SNOW (allows words like "quick", "new", "additional" between)
    r"UP\s+TO\s+(\d+\.?\d*)\s*INCH(?:ES)?\s+(?:OF\s+)?(?:\w+\s+)*SNOW"
    r")",
    re.IGNORECASE
)

# Ice accumulation
PATTERN_ICE_AMOUNT: Pattern[str] = re.compile(
    r"ICE(?:\s+ACCUMULATION)?\.{0,3}\s*(?:UP\s+TO\s+)?(\d+\.?\d*)\s*(?:TO\s+(\d+\.?\d*)\s*)?INCH(?:ES)?",
    re.IGNORECASE
)

# Snow rate
PATTERN_SNOW_RATE: Pattern[str] = re.compile(
    r"SNOW(?:\s+FALL)?(?:\s+RATE)?S?\.{0,3}\s*(\d+\.?\d*)\s*(?:TO\s+(\d+\.?\d*)\s*)?INCH(?:ES)?\s*PER\s*HOUR",
    re.IGNORECASE
)


# =============================================================================
# STORM MOTION PATTERNS
# =============================================================================

# Storm motion in text alerts
# Example: "TIME...MOT...LOC 1845Z 245DEG 35KT 4105 8132"
PATTERN_MOTION_TEXT: Pattern[str] = re.compile(
    r"TIME\.{3}MOT\.{3}LOC\s+\d{4}Z\s+(\d{3})DEG\s+(\d+)KT",
    re.IGNORECASE
)

# Storm motion in XML
PATTERN_MOTION_XML: Pattern[str] = re.compile(
    r"<eventMotionDescription>.*?(\d{3})\s*(?:DEG|degrees?).*?(\d+)\s*(?:KT|MPH|knots?)",
    re.IGNORECASE | re.DOTALL
)

# Alternative motion pattern
PATTERN_MOTION_ALT: Pattern[str] = re.compile(
    r"MOVING\s+(?:TO\s+THE\s+)?([NSEW]{1,3})\s+AT\s+(\d+)\s*(?:MPH|KT)",
    re.IGNORECASE
)

# Cardinal direction to degrees mapping
CARDINAL_TO_DEGREES = {
    "N": 180,    # Storm moving TO the north, coming FROM the south
    "NNE": 202,
    "NE": 225,
    "ENE": 247,
    "E": 270,
    "ESE": 292,
    "SE": 315,
    "SSE": 337,
    "S": 0,
    "SSW": 22,
    "SW": 45,
    "WSW": 67,
    "W": 90,
    "WNW": 112,
    "NW": 135,
    "NNW": 157,
}


# =============================================================================
# FLASH FLOOD PATTERNS
# =============================================================================

# Flash flood detection
PATTERN_FLOOD_DETECTION: Pattern[str] = re.compile(
    r"FLASH\s+FLOOD(?:ING)?\.{3}(RADAR\s+INDICATED|OBSERVED|POSSIBLE)",
    re.IGNORECASE
)

# Flash flood damage threat
PATTERN_FLOOD_DAMAGE: Pattern[str] = re.compile(
    r"FLASH\s+FLOOD\s+DAMAGE\s+THREAT\.{3}(CONSIDERABLE|CATASTROPHIC)",
    re.IGNORECASE
)


# =============================================================================
# WATCH PRODUCT PATTERNS
# =============================================================================

# Watch type detection
PATTERN_WATCH_TYPE: Pattern[str] = re.compile(
    r"(TORNADO|SEVERE\s+THUNDERSTORM)\s+WATCH\s+(?:NUMBER\s+)?(\d+)",
    re.IGNORECASE
)

# Watch counties block
PATTERN_WATCH_COUNTIES: Pattern[str] = re.compile(
    r"(?:COUNTIES|PARISHES)\s+INCLUDED.*?(?=\n\n|\Z)",
    re.IGNORECASE | re.DOTALL
)

# Watch outline UGC codes
PATTERN_WATCH_UGC: Pattern[str] = re.compile(
    r"^([A-Z]{2}[CZ]\d{3}(?:-\d{3})*-)$",
    re.MULTILINE
)


# =============================================================================
# LOCATION/AREA PATTERNS
# =============================================================================

# Location description line
# Usually appears after the UGC block
PATTERN_LOCATION_DESC: Pattern[str] = re.compile(
    r"^\.{3}(.+?)\.{3}\s*$",
    re.MULTILINE
)

# Area description in CAP/XML
PATTERN_AREA_DESC_XML: Pattern[str] = re.compile(
    r"<areaDesc>([^<]+)</areaDesc>",
    re.IGNORECASE
)

# "Including the cities of" pattern
PATTERN_CITIES: Pattern[str] = re.compile(
    r"INCLUDING\s+(?:THE\s+)?(?:CITIES?\s+OF|TOWNS?\s+OF|COMMUNITIES?\s+OF)\s+(.+?)(?:\.|$)",
    re.IGNORECASE
)


# =============================================================================
# SPS (SPECIAL WEATHER STATEMENT) PATTERNS
# =============================================================================

# SPS thunderstorm keywords (include these)
SPS_THUNDERSTORM_KEYWORDS = [
    "THUNDERSTORM",
    "SEVERE",
    "WIND",
    "HAIL",
    "LIGHTNING",
    "GUSTY",
    "DAMAGING",
    "STRONG STORM",
]

# SPS excluded keywords (exclude these)
SPS_EXCLUDED_KEYWORDS = [
    r"\bFIRE\b",
    r"\bSMOKE\b",
    r"\bFOG\b",
    r"\bHEAT\b",
    r"\bRIP\s*CURRENT",
    r"\bBEACH\s*HAZARD",
    r"\bMARINE\b",
    r"\bAIR\s*QUALITY",
    r"\bDUST\b",
]


# =============================================================================
# XML/CAP DETECTION PATTERNS
# =============================================================================

# Detect if content is XML/CAP wrapped
PATTERN_XML_ALERT: Pattern[str] = re.compile(
    r"<alert\s|<cap:|<info>",
    re.IGNORECASE
)

# CAP message type
PATTERN_CAP_MSG_TYPE: Pattern[str] = re.compile(
    r"<msgType>(\w+)</msgType>",
    re.IGNORECASE
)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def is_xml_content(text: str) -> bool:
    """Check if the text appears to be XML/CAP content."""
    return bool(PATTERN_XML_ALERT.search(text))


def extract_all_vtec(text: str) -> list[tuple]:
    """Extract all VTEC strings from text."""
    return PATTERN_VTEC.findall(text)


def has_vtec(text: str) -> bool:
    """Check if text contains a VTEC string."""
    return bool(PATTERN_VTEC_SIMPLE.search(text))
