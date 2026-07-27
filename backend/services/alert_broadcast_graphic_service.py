"""
Alert Broadcast Graphic Service.

Generates a 1920x1080 broadcast-quality PNG for severe weather alerts:
- Left panel (380px): event info, threat cards, counties, branding
- Right panel (1540px): dark base map + radar overlay + alert polygon + city labels
"""

import io
import math
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Canvas ────────────────────────────────────────────────────────────────────
TOTAL_W = 1920
TOTAL_H = 1080
PANEL_W = 380
MAP_W   = TOTAL_W - PANEL_W   # 1540
MAP_H   = TOTAL_H

# ── Palette ───────────────────────────────────────────────────────────────────
PANEL_BG    = (12,  19,  35)
CARD_BG     = (20,  32,  56)
CARD_BORDER = (35,  52,  88)
TORNADO_HL  = (90,  40,   0)   # warm brown highlight for tornado row
TEXT_WHITE  = (241, 245, 249)
TEXT_MUTED  = (148, 163, 184)
TEXT_DIM    = ( 71,  85, 105)
MAP_DARK    = ( 12,  14,  24)
DIVIDER_CLR = ( 30,  46,  78)

PHENOMENON_COLOR: dict[str, tuple[int,int,int]] = {
    "TO": (220,  30,  30),
    "SV": (255, 160,   0),
    "FF": (  0, 180,  60),
    "FA": (  0, 155,  50),
    "FL": (  0, 140,  45),
    "WS": (100, 149, 237),
    "BZ": (140, 175, 255),
    "IS": (150, 190, 255),
    "LE": (160, 195, 255),
    "WW": (155, 185, 255),
    "WC": (165, 205, 255),
    "CW": (180, 210, 255),
    "HW": (200, 100, 255),
    "EW": (220,  80, 255),
    "SQ": (210,  80,  80),
    "HT": (255,  90,  30),
    "EH": (255,  50,   0),
}
DEFAULT_COLOR = (59, 130, 246)

# Flip a cardinal direction to its opposite (FROM → TO, for motion display)
_OPPOSITE_CARDINAL: dict[str, str] = {
    "N": "S",   "NNE": "SSW", "NE": "SW",  "ENE": "WSW",
    "E": "W",   "ESE": "WNW", "SE": "NW",  "SSE": "NNW",
    "S": "N",   "SSW": "NNE", "SW": "NE",  "WSW": "ENE",
    "W": "E",   "WNW": "ESE", "NW": "SE",  "NNW": "SSE",
}

def _motion_toward(direction_from: str) -> str:
    """Convert a 'coming from' cardinal to the 'moving toward' cardinal for display."""
    return _OPPOSITE_CARDINAL.get(direction_from.upper(), direction_from)


# ── US Cities database ─────────────────────────────────────────────────────
# Loaded lazily from cities_db (backend/data/us_cities.csv if present,
# otherwise the bundled ~1600-city fallback in cities_db.py).
def _get_cities():
    from .cities_db import get_cities as _gc
    return _gc()

# Legacy alias used throughout this module
_CITIES = None  # resolved on first use via _get_cities()



# ── Impacted locations parser ─────────────────────────────────────────────────

import re as _re

def _parse_impacted_locations(alert) -> list[str]:
    """
    Extract city/town names from the NWS 'Locations impacted include...' section
    of the alert description or raw text.
    Handles both warning format (comma-separated) and watch format (cities of...).
    """
    text = (getattr(alert, "description", "") or "") + "\n" + (getattr(alert, "raw_text", "") or "")
    if not text.strip():
        return []

    names: list[str] = []

    # Pattern 1: "* Locations impacted include...\n  City1, City2, and City3."
    # Pattern 2: "Some locations that will experience X include...\n  ..."
    m = _re.search(
        r"(?:some\s+)?locations?\s+(?:impacted\s+|that\s+will\s+\S+\s+\S+\s+)?include[s]?\.\.\.\s*\n?(.*?)(?:\n\s*\n|\n\s*This|\Z)",
        text, _re.IGNORECASE | _re.DOTALL,
    )
    if m:
        raw = m.group(1)
        raw = _re.sub(r"\band\b\s*", "", raw, flags=_re.IGNORECASE)
        for part in _re.split(r"[,\n]+", raw):
            p = part.strip().strip(".")
            if p and not _re.match(r"(This|Interstate|Highway|I-\d|US-\d|SR-\d|State\s+Route)", p, _re.IGNORECASE):
                names.append(p)

    # Pattern 2: "THIS INCLUDES THE CITIES OF City1, City2, AND City3."
    if not names:
        m2 = _re.search(r"this\s+includes?\s+the\s+cities?\s+of\s+(.*?)\.?\s*\n", text, _re.IGNORECASE | _re.DOTALL)
        if m2:
            raw = m2.group(1)
            raw = _re.sub(r"\band\b\s*", "", raw, flags=_re.IGNORECASE)
            for part in _re.split(r"[,\n]+", raw):
                p = part.strip().strip(".")
                if p:
                    names.append(p)

    return [n for n in names if n]


def _match_locations_to_db(names: list[str], state_code: str = "") -> list[tuple]:
    """
    Match parsed location names to _CITIES entries.
    State-first: only match cities in the alert's state.
    Falls back to any-state only if no in-state match is found for a name.
    Sorted by population descending.
    """
    results: list[tuple] = []
    used: set[str] = set()

    def _score(name_up: str, loc_up: str) -> float:
        if name_up == loc_up:
            return 1.0
        if loc_up in name_up or name_up in loc_up:
            return 0.75
        return 0.0

    for loc in names:
        loc_up = loc.strip().upper()
        if not loc_up:
            continue

        # Pass 1: in-state exact/partial match
        best: tuple | None = None
        best_score = 0.0
        for entry in _get_cities():
            name, state, *_ = entry
            if state_code and state != state_code:
                continue
            s = _score(name.upper(), loc_up)
            if s > best_score:
                best_score = s
                best = entry

        # No cross-state fallback — a wrong big city is worse than no city.
        # The geographic fallback in _render_map_panel fills the gap.

        if best and best_score >= 0.75 and best[0].upper() not in used:
            results.append(best)
            used.add(best[0].upper())

    return sorted(results, key=lambda c: -c[4])


# ── State → timezone mapping (primary timezone for each state) ───────────────

_STATE_TZ: dict[str, str] = {
    # Eastern (UTC-5 / EDT UTC-4)
    "CT": "America/New_York", "DE": "America/New_York", "GA": "America/New_York",
    "MA": "America/New_York", "MD": "America/New_York", "ME": "America/New_York",
    "MI": "America/Detroit",  "NC": "America/New_York", "NH": "America/New_York",
    "NJ": "America/New_York", "NY": "America/New_York", "OH": "America/New_York",
    "PA": "America/New_York", "RI": "America/New_York", "SC": "America/New_York",
    "VA": "America/New_York", "VT": "America/New_York", "WV": "America/New_York",
    "FL": "America/New_York", "IN": "America/New_York", "KY": "America/New_York",
    # Central (UTC-6 / CDT UTC-5)
    "AL": "America/Chicago",  "AR": "America/Chicago",  "IA": "America/Chicago",
    "IL": "America/Chicago",  "KS": "America/Chicago",  "LA": "America/Chicago",
    "MN": "America/Chicago",  "MO": "America/Chicago",  "MS": "America/Chicago",
    "ND": "America/Chicago",  "NE": "America/Chicago",  "OK": "America/Chicago",
    "SD": "America/Chicago",  "TX": "America/Chicago",  "WI": "America/Chicago",
    "TN": "America/Chicago",
    # Mountain (UTC-7 / MDT UTC-6)
    "AZ": "America/Phoenix",  "CO": "America/Denver",   "ID": "America/Boise",
    "MT": "America/Denver",   "NM": "America/Denver",   "UT": "America/Denver",
    "WY": "America/Denver",
    # Pacific (UTC-8 / PDT UTC-7)
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    # Other
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}


def _alert_local_time(dt, alert) -> "datetime":
    """Convert a UTC datetime to the alert's local timezone, falling back to UTC."""
    try:
        import pytz
        # Derive state from the first UGC area code (e.g. "TXC123" → "TX")
        areas = getattr(alert, "affected_areas", []) or []
        tz_name = None
        for area in areas:
            state = area[:2].upper() if len(area) >= 2 else ""
            tz_name = _STATE_TZ.get(state)
            if tz_name:
                break
        if not tz_name:
            # Fall back to WFO office code heuristic
            sender = (getattr(alert, "sender_office", "") or "").upper().lstrip("K")
            tz_name = _STATE_TZ.get(sender[:2]) or "UTC"
        tz = pytz.timezone(tz_name)
        return dt.astimezone(tz)
    except Exception:
        return dt   # return UTC if anything fails


# ── Coordinate math ───────────────────────────────────────────────────────────

def _merc_y(lat: float) -> float:
    """Latitude → Web Mercator y (normalized 0–1, 0=north)."""
    r = math.radians(lat)
    return (1 - math.log(math.tan(r / 2 + math.pi / 4) / math.pi)) / 2


def _merc_x(lon: float) -> float:
    """Longitude → Web Mercator x (normalized 0–1)."""
    return (lon + 180) / 360


def _to_px(lat: float, lon: float,
           lat_min: float, lat_max: float,
           lon_min: float, lon_max: float,
           w: int, h: int) -> tuple[int, int]:
    mx = _merc_x(lon)
    my = _merc_y(lat)
    x0 = _merc_x(lon_min); x1 = _merc_x(lon_max)
    y0 = _merc_y(lat_max); y1 = _merc_y(lat_min)   # y0 < y1 in Mercator
    px = int((mx - x0) / (x1 - x0) * w)
    py = int((my - y0) / (y1 - y0) * h)
    return (px, py)


def _select_zoom(extent_deg: float) -> int:
    if extent_deg > 6:  return 7
    if extent_deg > 3:  return 8
    if extent_deg > 1.5: return 9
    return 10


def _tile_bounds(tx: int, ty: int, z: int) -> tuple[float, float, float, float]:
    """Return (lat_min, lat_max, lon_min, lon_max) for a tile."""
    n = 2 ** z
    lon_min = tx / n * 360 - 180
    lon_max = (tx + 1) / n * 360 - 180
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty+1) / n))))
    return lat_min, lat_max, lon_min, lon_max


def _latlon_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    tx = int((lon + 180) / 360 * n)
    lat_r = math.radians(lat)
    ty = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return tx, ty


# ── Font helper ───────────────────────────────────────────────────────────────

def _get_font(size: int, bold: bool = False):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


# ── Tile fetching ─────────────────────────────────────────────────────────────

_TILE_CACHE: dict[str, bytes] = {}

def _basemap_tile_url() -> tuple[str, str]:
    """Pick the basemap tile source. Returns (url_template, cache_style_tag).

    When a Stadia Maps API key is configured, use the same
    "alidade_smooth_dark" basemap the radar uses (server-side raster tiles need
    a key). Otherwise fall back to the keyless CARTO dark basemap.
    """
    key = ""
    try:
        from backend.config.settings import get_settings
        key = (getattr(get_settings(), "stadia_api_key", "") or "").strip()
    except Exception:
        key = ""
    if not key:
        key = os.environ.get("STADIA_API_KEY", "").strip()
    if key:
        return (
            "https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/"
            "{z}/{x}/{y}.png?api_key=" + key,
            "stadia",
        )
    return ("https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", "carto")


def _fetch_tiles(lat_min: float, lat_max: float,
                 lon_min: float, lon_max: float) -> "PIL.Image.Image | None":
    """Fetch dark basemap tiles (Stadia alidade_smooth_dark or CARTO) and stitch."""
    try:
        from PIL import Image
        import httpx
    except ImportError:
        return None

    extent_deg = max(lat_max - lat_min, lon_max - lon_min)
    z = _select_zoom(extent_deg)

    tx0, ty0 = _latlon_to_tile(lat_max, lon_min, z)
    tx1, ty1 = _latlon_to_tile(lat_min, lon_max, z)
    tx0, tx1 = min(tx0, tx1), max(tx0, tx1)
    ty0, ty1 = min(ty0, ty1), max(ty0, ty1)

    n_x = tx1 - tx0 + 1
    n_y = ty1 - ty0 + 1
    TILE_PX = 256
    stitched = Image.new("RGB", (n_x * TILE_PX, n_y * TILE_PX), MAP_DARK)

    TILE_URL, style_tag = _basemap_tile_url()
    headers = {"User-Agent": "AlertDashboardV2/1.0 (+https://thebattinfront.com)"}

    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            for ty in range(ty0, ty1 + 1):
                for tx in range(tx0, tx1 + 1):
                    key = f"{style_tag}/{z}/{tx}/{ty}"
                    if key in _TILE_CACHE:
                        tile_bytes = _TILE_CACHE[key]
                    else:
                        url = TILE_URL.format(z=z, x=tx, y=ty)
                        resp = client.get(url, headers=headers)
                        if resp.status_code != 200:
                            continue
                        tile_bytes = resp.content
                        _TILE_CACHE[key] = tile_bytes
                        if len(_TILE_CACHE) > 500:
                            oldest = next(iter(_TILE_CACHE))
                            del _TILE_CACHE[oldest]
                    tile_img = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
                    px_off = (tx - tx0) * TILE_PX
                    py_off = (ty - ty0) * TILE_PX
                    stitched.paste(tile_img, (px_off, py_off))
    except Exception as e:
        logger.warning(f"Tile fetch failed: {e}")
        return None

    # Crop to exact lat/lon extent.
    # tile_lat_min = southern edge of last tile row (ty1), NOT ty1+1 which is one
    # tile further south and causes all overlay elements to render shifted north.
    tile_lat_min, _, tile_lon_min, _ = _tile_bounds(tx0, ty1, z)
    _, tile_lat_max, tile_lon_max, _ = _tile_bounds(tx1 + 1, ty0, z)
    full_w, full_h = stitched.size

    def tile_lon_to_px(lon):
        return int((lon - tile_lon_min) / (tile_lon_max - tile_lon_min) * full_w)

    def tile_lat_to_py(lat):
        my = _merc_y(lat)
        my_top = _merc_y(tile_lat_max)
        my_bot = _merc_y(tile_lat_min)
        return int((my - my_top) / (my_bot - my_top) * full_h)

    cx0 = max(0, tile_lon_to_px(lon_min))
    cx1 = min(full_w, tile_lon_to_px(lon_max))
    cy0 = max(0, tile_lat_to_py(lat_max))
    cy1 = min(full_h, tile_lat_to_py(lat_min))

    if cx1 <= cx0 or cy1 <= cy0:
        return stitched

    return stitched.crop((cx0, cy0, cx1, cy1))


# ── Drawing helpers ───────────────────────────────────────────────────────────

def _rounded_rect(draw, x0, y0, x1, y1, r, fill, outline=None, outline_w=1):
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=outline, width=outline_w)


def _wrap_text(text: str, font, draw, max_w: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        bb = draw.textbbox((0, 0), test, font=font)
        if bb[2] - bb[0] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ── Left panel ────────────────────────────────────────────────────────────────

def _render_left_panel(alert, radar_frame, brand_name: str,
                       meteorologist_name: str) -> "PIL.Image.Image":
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (PANEL_W, TOTAL_H), PANEL_BG)
    draw = ImageDraw.Draw(img)

    phenomenon = getattr(alert, "phenomenon", "") or "DEFAULT"
    event_color = PHENOMENON_COLOR.get(phenomenon, DEFAULT_COLOR)
    event_name = getattr(alert, "event_name", "Weather Alert").upper()
    _sig_raw = getattr(alert, "significance", "W")
    significance = getattr(_sig_raw, "value", str(_sig_raw))

    # Tornado Emergency outranks a PDS — both come from the canonical Alert
    # properties (single source of truth).
    is_emergency = alert.is_tornado_emergency
    is_pds = alert.is_pds

    # Top accent stripe — wider for PDS
    stripe_h = 8 if not is_pds else 0
    draw.rectangle([0, 0, PANEL_W, stripe_h], fill=event_color)

    # PDS / Tornado Emergency banner (full-width header strip). The emergency
    # variant uses a crimson-magenta fill to match the rest of the stack.
    if is_pds:
        pds_h = 36
        banner_fill = (150, 0, 70) if is_emergency else (160, 10, 10)
        draw.rectangle([0, 0, PANEL_W, pds_h], fill=banner_fill)
        pds_f = _get_font(14, bold=True)
        pds_txt = "TORNADO EMERGENCY" if is_emergency else "PARTICULARLY DANGEROUS SITUATION"
        bb = draw.textbbox((0, 0), pds_txt, font=pds_f)
        tx = (PANEL_W - (bb[2] - bb[0])) // 2
        draw.text((tx, (pds_h - (bb[3] - bb[1])) // 2), pds_txt, font=pds_f, fill=(255, 220, 245) if is_emergency else (255, 210, 210))
        stripe_h = pds_h

    # ── Event title (one word per line, max possible font size) ────────────
    mx = 24
    y  = stripe_h + 14

    words = event_name.split()
    # Each word on its own line — allows the largest possible font
    title_lines = words if words else [event_name]

    # Find the largest font where the widest word still fits
    title_font_size = 80
    avail_w = PANEL_W - mx * 2
    while title_font_size > 28:
        tf = _get_font(title_font_size, bold=True)
        widest = max(
            draw.textbbox((0, 0), ln, font=tf)[2] - draw.textbbox((0, 0), ln, font=tf)[0]
            for ln in title_lines
        )
        if widest <= avail_w:
            break
        title_font_size -= 2

    title_font = _get_font(title_font_size, bold=True)
    line_h = title_font_size + 6
    for ln in title_lines:
        draw.text((mx, y), ln, font=title_font, fill=TEXT_WHITE)
        y += line_h
    y += 8

    # Expiration — converted to the alert's local timezone
    exp_str = ""
    exp_time = getattr(alert, "expiration_time", None)
    if exp_time:
        local_exp = _alert_local_time(exp_time, alert)
        exp_str = "Until " + local_exp.strftime("%I:%M %p %m/%d/%Y").lstrip("0")
    elif hasattr(alert, "expiration_str"):
        exp_str = alert.expiration_str or ""

    if exp_str:
        ef = _get_font(22, bold=False)
        draw.text((mx, y), exp_str, font=ef, fill=TEXT_MUTED)
        bb = draw.textbbox((0, 0), exp_str, font=ef)
        y += (bb[3] - bb[1]) + 18

    # Divider
    draw.rectangle([mx, y, PANEL_W - mx, y + 2], fill=DIVIDER_CLR)
    y += 14

    # ── Threat cards ───────────────────────────────────────────────────────
    threat = getattr(alert, "threat", None)
    storm_motion = getattr(threat, "storm_motion", None) if threat else None
    cards = []

    if threat:
        # WIND
        wind_mph = getattr(threat, "max_wind_gust_mph", None)
        if wind_mph:
            det = getattr(threat, "wind_damage_threat", "") or ""
            label_parts = [f"{wind_mph} MPH"]
            wd = getattr(threat, "wind_damage_threat", "") or ""
            # Try to get wind detection string
            wind_label = f"{wind_mph} MPH"
            # check description for RADAR INDICATED etc.
            desc = getattr(alert, "description", "") or ""
            for kw in ("RADAR INDICATED", "MEASURED", "ESTIMATED"):
                if kw in desc.upper():
                    wind_label += f" {kw}"
                    break
            cards.append(("WIND", wind_label, False))

        # HAIL
        hail = getattr(threat, "max_hail_size_inches", None)
        if hail:
            hail_label = f"{hail:.2f} inches".rstrip("0").rstrip(".")
            desc = getattr(alert, "description", "") or ""
            for kw in ("RADAR INDICATED", "MEASURED", "ESTIMATED"):
                if kw in desc.upper():
                    hail_label += f" {kw}"
                    break
            cards.append(("HAIL", hail_label, False))

        # TORNADO
        tornado_det = getattr(threat, "tornado_detection", None)
        if tornado_det and significance != "A":
            tor_label = tornado_det.title() if tornado_det else "Possible"
        elif phenomenon == "TO" and significance != "A":
            tor_label = "Radar Indicated"
        else:
            tor_label = None

        if (tor_label or phenomenon == "TO") and significance != "A":
            cards.append(("TORNADO", tor_label or "Possible", True))

    # MOTION
    if storm_motion and getattr(storm_motion, "is_valid", False):
        dir_str = getattr(storm_motion, "direction_from", "") or ""
        spd = getattr(storm_motion, "speed_mph", 0) or 0
        mot_label = f"{_motion_toward(dir_str)} {spd} MPH".strip()
        cards.append(("MOTION", mot_label, False))

    CARD_H = 90
    CARD_GAP = 8
    CARD_MX = mx
    CARD_W = PANEL_W - CARD_MX * 2

    for (card_label, card_value, highlighted) in cards:
        bg   = TORNADO_HL if highlighted else CARD_BG
        bord = event_color if highlighted else CARD_BORDER

        _rounded_rect(draw, CARD_MX, y, CARD_MX + CARD_W, y + CARD_H,
                      r=6, fill=bg, outline=bord, outline_w=2)

        # Colored left-edge accent bar on the label area
        accent_color = event_color if highlighted else (80, 120, 180)
        draw.rectangle([CARD_MX, y, CARD_MX + 4, y + CARD_H], fill=accent_color)

        # Label — bold, larger, clearly visible
        lf = _get_font(15, bold=True)
        lbl_color = (
            (min(255, event_color[0] + 100), min(255, event_color[1] + 100), min(255, event_color[2] + 100))
            if highlighted else (160, 185, 225)
        )
        draw.text((CARD_MX + 14, y + 11), card_label, font=lf, fill=lbl_color)

        # Value (large, bold, auto-shrink)
        val_size = 32
        while val_size > 16:
            vf = _get_font(val_size, bold=True)
            bb = draw.textbbox((0, 0), card_value, font=vf)
            if bb[2] - bb[0] <= CARD_W - 28:
                break
            val_size -= 2
        vf = _get_font(val_size, bold=True)
        draw.text((CARD_MX + 14, y + CARD_H - val_size - 14), card_value,
                  font=vf, fill=TEXT_WHITE)

        y += CARD_H + CARD_GAP

    y += 8

    # ── Watch probability section (TOA / SVA watches only) ────────────────
    if significance in ("A", "Y"):
        import re as _re2
        # Read the product text here rather than relying on a local from an
        # earlier branch: the only other reads live inside `if wind:` / `if hail:`,
        # which a WATCH never enters — so this line raised NameError on every
        # watch, which is the one case the block exists for.
        raw = getattr(alert, "description", "") or ""

        # NWS SPC format: "PROBABILITY OF TORNADOES...30 PERCENT"
        # Level qualifier (HIGH/MODERATE/LOW) is optional — derive from percentage.
        prob_matches = _re2.findall(
            r'PROBABILITY\s+OF\s+(.+?)\s*\.{2,3}\s*'
            r'(?:(?:HIGH|MODERATE|MOD|LOW)\s*\.{0,3}\s*)?(\d{1,3})\s*PERCENT',
            raw, _re2.IGNORECASE
        )

        # Also check for max hail / max wind lines common in SPC watches
        hail_max_m = _re2.search(r'MAXIMUM\s+HAIL\s+SIZE\s*\.{2,3}\s*([\d.]+)\s*IN', raw, _re2.IGNORECASE)
        wind_max_m = _re2.search(r'MAXIMUM\s+WIND\s+GUSTS\s*\.{2,3}\s*(\d+)\s*MPH', raw, _re2.IGNORECASE)

        def _pct_level(pct: int) -> tuple[str, tuple]:
            """Derive HIGH/MOD/LOW label and color from a percentage value."""
            if pct >= 60: return "HIGH",     (255,  60,  60)
            if pct >= 30: return "MOD",      (255, 165,   0)
            return              "LOW",       (100, 185, 255)

        def _prob_category(desc: str) -> str:
            d = desc.upper()
            if "TORNADO" in d: return "TORNADOES"
            if "COMBINED" in d: return "COMBINED HAIL/WIND"
            if "HAIL" in d:    return "HAIL"
            if "WIND" in d:    return "WIND"
            return d.strip()

        if prob_matches or hail_max_m or wind_max_m:
            draw.rectangle([mx, y, PANEL_W - mx, y + 2], fill=DIVIDER_CLR)
            y += 10
            hdr_f = _get_font(13, bold=True)
            draw.text((mx, y), "WATCH PROBABILITIES", font=hdr_f, fill=TEXT_DIM)
            y += draw.textbbox((0, 0), "WATCH PROBABILITIES", font=hdr_f)[3] + 10

            seen_cats: set[str] = set()
            for raw_desc, pct_str in prob_matches:
                cat = _prob_category(raw_desc)
                if cat in seen_cats:
                    continue
                seen_cats.add(cat)
                pct_val = int(pct_str)
                lvl_lbl, lv_color = _pct_level(pct_val)

                # Row: category label left, "MOD (30%)" right, progress bar below
                lbl_f  = _get_font(13, bold=False)
                val_f  = _get_font(14, bold=True)
                val_str = f"{lvl_lbl}  {pct_val}%"
                bb_v = draw.textbbox((0, 0), val_str, font=val_f)
                val_x = PANEL_W - mx - (bb_v[2] - bb_v[0])
                draw.text((mx, y), cat, font=lbl_f, fill=TEXT_MUTED)
                draw.text((val_x, y - 1), val_str, font=val_f, fill=lv_color)
                y += 17

                # Progress bar
                bar_w = PANEL_W - mx * 2
                bar_h = 5
                _rounded_rect(draw, mx, y, mx + bar_w, y + bar_h, r=2, fill=(30, 40, 60))
                filled_w = max(4, int(bar_w * pct_val / 100))
                _rounded_rect(draw, mx, y, mx + filled_w, y + bar_h, r=2, fill=lv_color)
                y += bar_h + 10

            # Max hail / max wind from SPC watch text
            if hail_max_m or wind_max_m:
                y += 2
                extra_f = _get_font(13, bold=False)
                bold_f  = _get_font(13, bold=True)
                if hail_max_m:
                    h_str = f"{hail_max_m.group(1)}\""
                    draw.text((mx, y), "Max hail size", font=extra_f, fill=TEXT_MUTED)
                    bb = draw.textbbox((0, 0), h_str, font=bold_f)
                    draw.text((PANEL_W - mx - (bb[2]-bb[0]), y), h_str, font=bold_f, fill=TEXT_WHITE)
                    y += 18
                if wind_max_m:
                    w_str = f"{wind_max_m.group(1)} MPH"
                    draw.text((mx, y), "Max wind gusts", font=extra_f, fill=TEXT_MUTED)
                    bb = draw.textbbox((0, 0), w_str, font=bold_f)
                    draw.text((PANEL_W - mx - (bb[2]-bb[0]), y), w_str, font=bold_f, fill=TEXT_WHITE)
                    y += 18

    # ── Affected counties ──────────────────────────────────────────────────
    # The full county list (all states) now renders as a horizontal "Affected
    # Areas" bar across the top of the map panel (see _render_map_panel), which
    # has the room to show every county including out-of-home-state ones. The
    # cramped, 12-county-capped sidebar pills were removed.

    # ── Meteorologist ──────────────────────────────────────────────────────
    # Push to bottom area
    BOTTOM_ZONE = TOTAL_H - 90
    if y < BOTTOM_ZONE:
        y = BOTTOM_ZONE

    if meteorologist_name:
        draw.rectangle([mx, y, PANEL_W - mx, y + 2], fill=DIVIDER_CLR)
        y += 14
        mf = _get_font(24, bold=True)
        draw.text((mx, y), "METEOROLOGIST", font=_get_font(13, bold=False), fill=TEXT_DIM)
        bb = draw.textbbox((0, 0), "METEOROLOGIST", font=_get_font(13, bold=False))
        y += (bb[3] - bb[1]) + 4
        draw.text((mx, y), meteorologist_name.upper(), font=mf, fill=TEXT_WHITE)
        bb = draw.textbbox((0, 0), meteorologist_name.upper(), font=mf)
        y += (bb[3] - bb[1]) + 10

    # Footer
    footer_parts = []
    if radar_frame:
        rf_site = getattr(radar_frame, "site", "")
        rf_ts = getattr(radar_frame, "timestamp", "")
        if rf_site and rf_ts:
            try:
                dt = datetime.fromisoformat(rf_ts.replace("Z", "+00:00"))
                ztime = dt.strftime("%H:%MZ")
            except Exception:
                ztime = ""
            if ztime:
                footer_parts.append(f"Radar: {rf_site} ({ztime})")
    if brand_name:
        footer_parts.append(f"Powered by {brand_name}")
    if footer_parts:
        ff = _get_font(13, bold=False)
        footer_text = "  ·  ".join(footer_parts)
        draw.text((mx, TOTAL_H - 24), footer_text, font=ff, fill=TEXT_DIM)

    return img


# ── Composite radar tile sources (IEM → RainViewer cascade) ──────────────────

def _radar_zoom(extent_deg: float) -> int:
    """Radar tiles at one zoom level higher than base gives better storm detail."""
    if extent_deg > 5:  return 7
    if extent_deg > 2:  return 8
    return 9  # county-level warnings get zoom 9 — fine enough to show storm cores


def _fetch_iem_wms_radar(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    width: int = MAP_W,
    height: int = MAP_H,
) -> "PIL.Image.Image | None":
    """
    Fetch IEM NEXRAD N0Q composite via WMS in EPSG:3857 (Web Mercator).
    Returns an RGBA image that exactly covers the bbox in Mercator — no tile
    stitching or crop math needed, so alignment with the CartoDB base map is perfect.
    """
    try:
        from PIL import Image
        import httpx
    except ImportError:
        return None

    # Convert bbox to EPSG:3857 meters
    def _lon_m(lon: float) -> float:
        return lon * 20037508.34 / 180.0

    def _lat_m(lat: float) -> float:
        lat_r = math.radians(lat)
        return math.log(math.tan(math.pi / 4 + lat_r / 2)) * 6378137.0

    west_m  = _lon_m(lon_min)
    east_m  = _lon_m(lon_max)
    south_m = _lat_m(lat_min)
    north_m = _lat_m(lat_max)

    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi"
        "?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap"
        "&FORMAT=image/png&TRANSPARENT=true"
        "&LAYERS=nexrad-n0q-900913"
        f"&WIDTH={width}&HEIGHT={height}"
        "&SRS=EPSG:3857"
        f"&BBOX={west_m},{south_m},{east_m},{north_m}"
    )
    try:
        headers = {"User-Agent": "AlertDashboardV2/1.0 (severe weather dashboard)"}
        with httpx.Client(timeout=12.0, follow_redirects=True) as c:
            resp = c.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"IEM WMS HTTP {resp.status_code}")
                return None
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            # IEM WMS returns the image sized to (width, height) already
            if img.size != (width, height):
                img = img.resize((width, height), Image.LANCZOS)
            return img
    except Exception as e:
        logger.warning(f"IEM WMS fetch failed: {e}")
        return None


def _fetch_radar_tile_overlay(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
) -> "PIL.Image.Image | None":
    """
    Fetch composite radar as a transparent RGBA image sized to MAP_W × MAP_H.
    Priority: IEM WMS (exact Mercator bbox, perfect alignment) → RainViewer tiles → IEM tile fallback.
    """
    result = _fetch_iem_wms_radar(lat_min, lat_max, lon_min, lon_max)
    if result is not None:
        logger.info("Radar overlay source: IEM WMS N0Q")
        return result

    logger.info("IEM WMS failed, trying RainViewer tiles...")
    result = _fetch_rainviewer_radar_tiles(lat_min, lat_max, lon_min, lon_max)
    if result is not None:
        logger.info("Radar overlay source: RainViewer")
        return result

    logger.info("RainViewer failed, trying IEM N0Q tiles...")
    result = _fetch_iem_radar_tiles(lat_min, lat_max, lon_min, lon_max, product="nexrad-n0q-900913")
    if result is not None:
        logger.info("Radar overlay source: IEM N0Q tiles")
        return result

    logger.info("IEM N0Q tiles failed, trying IEM N0R composite tiles...")
    result = _fetch_iem_radar_tiles(lat_min, lat_max, lon_min, lon_max, product="composite-n0r-900913")
    if result is not None:
        logger.info("Radar overlay source: IEM N0R tiles")
    return result


def _stitch_radar_tiles(
    urls: list[str],
    tx0: int, ty0: int, tx1: int, ty1: int,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    z: int,
) -> "PIL.Image.Image | None":
    """Fetch tiles concurrently, stitch, and crop. Logs every failure for diagnosis."""
    try:
        from PIL import Image
        import httpx
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except ImportError:
        return None

    TILE_PX = 256
    n_x = tx1 - tx0 + 1
    n_y = ty1 - ty0 + 1
    canvas = Image.new("RGBA", (n_x * TILE_PX, n_y * TILE_PX), (0, 0, 0, 0))

    headers = {"User-Agent": "AlertDashboardV2/1.0 (severe weather dashboard)"}
    results: dict[int, bytes] = {}

    def _fetch_one(idx: int, url: str) -> tuple[int, bytes | None, int]:
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as c:
                resp = c.get(url, headers=headers)
                if resp.status_code == 200:
                    return idx, resp.content, 200
                logger.warning(f"Radar tile HTTP {resp.status_code}: {url}")
                return idx, None, resp.status_code
        except Exception as e:
            logger.warning(f"Radar tile fetch exception: {e} — {url}")
            return idx, None, 0

    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as pool:
        futures = {pool.submit(_fetch_one, i, u): i for i, u in enumerate(urls)}
        for fut in as_completed(futures):
            idx, content, status = fut.result()
            if content:
                results[idx] = content

    if not results:
        logger.warning(f"All {len(urls)} radar tiles failed")
        return None

    fetched = 0
    for idx, content in results.items():
        tx = tx0 + (idx % n_x)
        ty = ty0 + (idx // n_x)
        try:
            tile = Image.open(io.BytesIO(content)).convert("RGBA")
            canvas.paste(tile, ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX))
            fetched += 1
        except Exception as e:
            logger.warning(f"Radar tile decode failed: {e}")

    if fetched == 0:
        return None

    logger.info(f"Radar tiles: {fetched}/{len(urls)} fetched (z={z})")

    # Crop to exact lat/lon extent (same math as _fetch_tiles)
    tile_lat_min, _, tile_lon_min, _ = _tile_bounds(tx0, ty1, z)
    _, tile_lat_max, tile_lon_max, _ = _tile_bounds(tx1 + 1, ty0, z)
    full_w, full_h = canvas.size

    def _lon_px(lon):
        return int((lon - tile_lon_min) / (tile_lon_max - tile_lon_min) * full_w)

    def _lat_py(lat):
        my = _merc_y(lat); my_t = _merc_y(tile_lat_max); my_b = _merc_y(tile_lat_min)
        return int((my - my_t) / (my_b - my_t) * full_h)

    cx0 = max(0, _lon_px(lon_min)); cx1 = min(full_w, _lon_px(lon_max))
    cy0 = max(0, _lat_py(lat_max)); cy1 = min(full_h, _lat_py(lat_min))
    if cx1 <= cx0 or cy1 <= cy0:
        return canvas
    return canvas.crop((cx0, cy0, cx1, cy1))


def _fetch_iem_radar_tiles(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    product: str = "nexrad-n0q-900913",
) -> "PIL.Image.Image | None":
    """IEM NEXRAD composite reflectivity tiles (free, no key, transparent PNG)."""
    extent_deg = max(lat_max - lat_min, lon_max - lon_min)
    z = _radar_zoom(extent_deg)

    tx0, ty0 = _latlon_to_tile(lat_max, lon_min, z)
    tx1, ty1 = _latlon_to_tile(lat_min, lon_max, z)
    tx0, tx1 = min(tx0, tx1), max(tx0, tx1)
    ty0, ty1 = min(ty0, ty1), max(ty0, ty1)

    BASE = f"https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/{product}/{{z}}/{{x}}/{{y}}.png"
    urls = [BASE.format(z=z, x=tx, y=ty)
            for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]

    logger.info(f"Fetching {len(urls)} IEM {product} tiles at zoom {z}")
    return _stitch_radar_tiles(urls, tx0, ty0, tx1, ty1,
                               lat_min, lat_max, lon_min, lon_max, z)


def _fetch_rainviewer_radar_tiles(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
) -> "PIL.Image.Image | None":
    """RainViewer composite radar tiles (free, no key, transparent PNG)."""
    try:
        import httpx
    except ImportError:
        return None

    # Step 1: get latest timestamp path
    try:
        with httpx.Client(timeout=5.0) as client:
            meta = client.get("https://api.rainviewer.com/public/weather-maps.json")
            meta.raise_for_status()
            data = meta.json()
            past = data.get("radar", {}).get("past", [])
            if not past:
                return None
            path = past[-1].get("path", "")
            if not path:
                return None
    except Exception as e:
        logger.warning(f"RainViewer metadata fetch failed: {e}")
        return None

    extent_deg = max(lat_max - lat_min, lon_max - lon_min)
    z = _radar_zoom(extent_deg)

    tx0, ty0 = _latlon_to_tile(lat_max, lon_min, z)
    tx1, ty1 = _latlon_to_tile(lat_min, lon_max, z)
    tx0, tx1 = min(tx0, tx1), max(tx0, tx1)
    ty0, ty1 = min(ty0, ty1), max(ty0, ty1)

    # color=6 (NWS-like), smooth=1, snow=1
    BASE = f"https://tilecache.rainviewer.com{path}/256/{{z}}/{{x}}/{{y}}/6/1_1.png"
    urls = []
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            urls.append(BASE.format(z=z, x=tx, y=ty))

    result = _stitch_radar_tiles(urls, tx0, ty0, tx1, ty1,
                                  lat_min, lat_max, lon_min, lon_max, z)
    if result is not None:
        logger.debug(f"RainViewer radar tiles fetched ({z}/{tx0}-{tx1}/{ty0}-{ty1})")
    return result


# ── Map panel ─────────────────────────────────────────────────────────────────

def _render_map_panel(alert, radar_frame,
                      zone_polygons: "list[list[list[float]]] | None" = None) -> "PIL.Image.Image":
    """
    Render the map panel.

    zone_polygons: flat list of county/zone polygon rings [[lat,lon],...] for
    zone-based alerts (watches, advisories) that have no precise warning polygon.
    """
    from PIL import Image, ImageDraw, ImageFont

    # Determine map extent: precise polygon > zone polygons > centroid > default
    polygon = getattr(alert, "polygon", []) or []
    centroid = getattr(alert, "centroid", None)

    def _extract_pts(seq):
        """
        Flatten a sequence of coordinate data into a list of (lat_float, lon_float) tuples.
        Handles nesting levels: [lat,lon], [[lat,lon],...], or [[[lat,lon],...],...]
        """
        out = []
        for item in seq:
            if not item:
                continue
            # Item is already a [lat, lon] pair
            if isinstance(item[0], (int, float)):
                out.append((float(item[0]), float(item[1])))
            else:
                # Item is a ring or list of rings — recurse one level
                for sub in item:
                    if sub and isinstance(sub[0], (int, float)):
                        out.append((float(sub[0]), float(sub[1])))
                    else:
                        for subsub in sub:
                            if subsub and isinstance(subsub[0], (int, float)):
                                out.append((float(subsub[0]), float(subsub[1])))
        return out

    # Flatten all zone polygon coords for extent calculation
    all_zone_pts = _extract_pts(zone_polygons) if zone_polygons else []

    # When zone_polygons are supplied, alert.polygon may contain the nested county-ring
    # structure that zone_geometry_service stores there.  Calling _extract_pts() on that
    # structure would flatten all ring vertices into a single list, which the warning-polygon
    # branch (below) would then draw as one giant mis-connected polygon — producing the
    # diagonal spiderweb artefacts.  If zone_polygons is present, skip alert.polygon so the
    # county-fill branch runs instead.
    polygon_pts  = [] if zone_polygons else _extract_pts(polygon)

    if len(polygon_pts) >= 3:
        lats = [p[0] for p in polygon_pts]
        lons = [p[1] for p in polygon_pts]
        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)
    elif all_zone_pts:
        lats = [p[0] for p in all_zone_pts]
        lons = [p[1] for p in all_zone_pts]
        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)
    elif centroid:
        lat_c, lon_c = float(centroid[0]), float(centroid[1])
        lat_min, lat_max = lat_c - 1.5, lat_c + 1.5
        lon_min, lon_max = lon_c - 2.5, lon_c + 2.5
    else:
        lat_min, lat_max, lon_min, lon_max = 35.0, 45.0, -100.0, -80.0

    # Padding: tighter around the warning polygon
    lat_rng = lat_max - lat_min
    lon_rng = lon_max - lon_min
    pad_lat = max(0.20, lat_rng * 0.28)
    pad_lon = max(0.30, lon_rng * 0.32)
    lat_min -= pad_lat;  lat_max += pad_lat
    lon_min -= pad_lon;  lon_max += pad_lon

    # Keep aspect ratio close to the map panel (1540:1080 ≈ 1.43:1)
    target_ratio = MAP_W / MAP_H
    lat_span = lat_max - lat_min
    cos_factor = math.cos(math.radians((lat_min + lat_max) / 2))
    lon_span_needed = lat_span / cos_factor * target_ratio
    cur_lon_span = lon_max - lon_min
    if lon_span_needed > cur_lon_span:
        extra = (lon_span_needed - cur_lon_span) / 2
        lon_min -= extra; lon_max += extra

    # Base map
    base = _fetch_tiles(lat_min, lat_max, lon_min, lon_max)
    if base is None:
        base = Image.new("RGB", (MAP_W, MAP_H), MAP_DARK)
    base = base.resize((MAP_W, MAP_H), Image.LANCZOS)

    # ── Radar overlay ─────────────────────────────────────────────────────────
    # Tier 1: local NEXRAD frame (highest resolution, Level 2)
    # Only use it if its geographic bounds actually overlap the map extent.
    radar_applied = False
    if radar_frame:
        try:
            rf_path   = getattr(radar_frame, "image_path", "")
            rf_bounds = getattr(radar_frame, "bounds", {})
            r_south = rf_bounds.get("south", 0)
            r_north = rf_bounds.get("north", 0)
            r_west  = rf_bounds.get("west",  0)
            r_east  = rf_bounds.get("east",  0)
            frame_covers = (
                r_south < lat_max and r_north > lat_min and
                r_west  < lon_max and r_east  > lon_min
            )
            if rf_path and rf_bounds and frame_covers:
                radar_img = Image.open(rf_path).convert("RGBA")
                rx0, ry1 = _to_px(r_south, r_west, lat_min, lat_max, lon_min, lon_max, MAP_W, MAP_H)
                rx1, ry0 = _to_px(r_north, r_east, lat_min, lat_max, lon_min, lon_max, MAP_W, MAP_H)
                rw = max(1, rx1 - rx0); rh = max(1, ry1 - ry0)
                radar_resized = radar_img.resize((rw, rh), Image.LANCZOS)
                base_rgba = base.convert("RGBA")
                base_rgba.paste(radar_resized, (rx0, ry0), radar_resized)
                base = base_rgba.convert("RGB")
                radar_applied = True
        except Exception as e:
            logger.warning(f"Local radar overlay failed: {e}")

    # Tier 2 & 3: IEM WMS → RainViewer → IEM tiles (always covers the alert area)
    # _fetch_radar_tile_overlay returns an image already at MAP_W × MAP_H (from WMS)
    # or tile-stitched and cropped. Either way we can paste directly at (0,0).
    if not radar_applied:
        try:
            radar_tiles = _fetch_radar_tile_overlay(lat_min, lat_max, lon_min, lon_max)
            if radar_tiles is not None:
                if radar_tiles.size != (MAP_W, MAP_H):
                    radar_tiles = radar_tiles.resize((MAP_W, MAP_H), Image.LANCZOS)
                # Apply at 78% opacity so the base map shows through for context
                r_ch, g_ch, b_ch, a_ch = radar_tiles.split()
                a_ch = a_ch.point(lambda v: int(v * 0.78))
                radar_tiles = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
                base_rgba = base.convert("RGBA")
                base_rgba.paste(radar_tiles, (0, 0), radar_tiles)
                base = base_rgba.convert("RGB")
                radar_applied = True
        except Exception as e:
            logger.warning(f"Composite radar tile overlay failed: {e}")

    map_draw = ImageDraw.Draw(base, "RGBA")

    phenomenon = getattr(alert, "phenomenon", "")
    _sig_raw2 = getattr(alert, "significance", "W")
    significance = getattr(_sig_raw2, "value", str(_sig_raw2))
    evt_color = PHENOMENON_COLOR.get(phenomenon, DEFAULT_COLOR)

    # ── Warning polygon (precise, glowing outline) ─────────────────────────
    # Use pre-normalized polygon_pts (guaranteed float pairs from _extract_pts)
    if len(polygon_pts) >= 3:
        pts = [_to_px(p[0], p[1], lat_min, lat_max, lon_min, lon_max, MAP_W, MAP_H)
               for p in polygon_pts]
        fill_rgba = (*evt_color, 40)
        map_draw.polygon(pts, fill=fill_rgba)
        edges = [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
        for p1, p2 in edges:
            map_draw.line([p1, p2], fill=(*evt_color, 90), width=14)
        for p1, p2 in edges:
            map_draw.line([p1, p2], fill=(*evt_color, 180), width=7)
        bright = tuple(min(255, c + 60) for c in evt_color)
        for p1, p2 in edges:
            map_draw.line([p1, p2], fill=(*bright, 255), width=3)

    # ── Zone/county fills (watches, advisories, zone-based warnings) ───────
    elif zone_polygons:
        norm_rings = [
            _extract_pts(ring) if ring and not isinstance(ring[0][0], float)
            else [(float(p[0]), float(p[1])) for p in ring]
            for ring in zone_polygons
        ]

        # Log first ring so we can see the coordinate structure in server output
        if norm_rings and norm_rings[0]:
            r0 = norm_rings[0]
            logger.info(
                f"Zone ring[0]: len={len(r0)}, "
                f"first3={[(round(p[0],3), round(p[1],3)) for p in r0[:3]]}"
            )

        fill_rgba = (*evt_color, 48)
        bright    = tuple(min(255, c + 60) for c in evt_color)
        outline_c = (*bright, 235)

        def _safe_ring_pts(ring_pts):
            """
            Convert a ring to pixel coords, fixing coordinate order if swapped.

            Keeps EVERY vertex (PIL clips off-canvas points to the raster edge
            correctly). The previous version dropped vertices that mapped beyond
            the image margin, which made the ring reconnect across the gap with a
            straight diagonal — the spiderweb/jagged cuts. Only genuinely corrupt
            (non-US) coordinates are skipped.
            """
            result = []
            for p in ring_pts:
                lat, lon = float(p[0]), float(p[1])
                # Detect swapped (lon, lat) — US longitudes are -60 to -170
                if lon > 20 and -180 < lat < -50:
                    lat, lon = lon, lat
                # Skip only truly corrupt vertices (outside the US envelope).
                if not (15 <= lat <= 75 and -180 <= lon <= -50):
                    continue
                result.append(
                    _to_px(lat, lon, lat_min, lat_max, lon_min, lon_max, MAP_W, MAP_H)
                )
            return result

        all_county_pts = []
        for ring_pts in norm_rings:
            if len(ring_pts) >= 3:
                pts = _safe_ring_pts(ring_pts)
                if len(pts) >= 3:
                    all_county_pts.append(pts)
                    map_draw.polygon(pts, fill=fill_rgba)

        for pts in all_county_pts:
            # Draw outline as individual edge segments — same technique used
            # for warning polygons, which renders correctly.
            n = len(pts)
            for i in range(n):
                p1 = pts[i]
                p2 = pts[(i + 1) % n]
                # Skip zero-length closing edges (GeoJSON rings repeat first vertex)
                if p1 != p2:
                    map_draw.line([p1, p2], fill=outline_c, width=2)

    # ── Build shapely geometry for city queries ────────────────────────────
    _shapely_geom = None
    try:
        from shapely.geometry import Polygon as _SPoly, MultiPolygon as _SMPoly
        from shapely.ops import unary_union as _sunion
        shapes = []
        if len(polygon_pts) >= 3:
            shapes.append(_SPoly([(p[1], p[0]) for p in polygon_pts]))
        elif zone_polygons:
            for ring in zone_polygons:
                rpts = [(float(p[0]), float(p[1])) for p in _extract_pts([ring])] if ring else []
                if len(rpts) >= 3:
                    shapes.append(_SPoly([(p[1], p[0]) for p in rpts]))
        if shapes:
            _shapely_geom = _sunion(shapes)
    except Exception:
        pass

    def _in_or_near_polygon(lat, lon):
        if _shapely_geom is not None:
            try:
                from shapely.geometry import Point as _SPoint
                tol = 0.15 if zone_polygons else 0.5
                return _shapely_geom.distance(_SPoint(lon, lat)) < tol
            except Exception:
                pass
        return True

    city_font_cache: dict[int, object] = {}
    visible_cities: list[tuple[str, str, float, float, int]] = []

    lat_span = lat_max - lat_min
    lon_span = lon_max - lon_min

    for name, state, lat, lon, pop in _get_cities():
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            visible_cities.append((name, state, lat, lon, pop))

    # Sort: cities inside/near polygon first by population, then rest by population
    def _city_sort_key(c):
        name, state, lat, lon, pop = c
        inside = _in_or_near_polygon(lat, lon)
        return (0 if inside else 1, -pop)

    visible_cities.sort(key=_city_sort_key)

    # Grid-based density filter: divide the viewport into cells and allow at most
    # max_per_cell cities per cell (by population rank). This prevents dense metro
    # areas from crowding out entire regions while still ensuring geographic spread.
    _GRID_COLS, _GRID_ROWS = 6, 4
    _MAX_PER_CELL = 2
    _cell_counts: dict[tuple[int,int], int] = {}
    _density_filtered: list[tuple] = []
    for city in visible_cities:
        _n, _s, _lat, _lon, _pop = city
        _col = min(int((_lon - lon_min) / lon_span * _GRID_COLS), _GRID_COLS - 1)
        _row = min(int((lat_max - _lat) / lat_span * _GRID_ROWS), _GRID_ROWS - 1)
        _cell = (_row, _col)
        if _cell_counts.get(_cell, 0) < _MAX_PER_CELL:
            _density_filtered.append(city)
            _cell_counts[_cell] = _cell_counts.get(_cell, 0) + 1

    # County seats for each affected county (drawn prominently after the generic
    # city pass). Collect their names so the generic pass doesn't double-label
    # the same town.
    county_seat_pts: list[tuple[str, float, float]] = []
    _seat_names: set[str] = set()
    try:
        from backend.services.county_seats import get_county_seat as _get_seat
    except Exception:
        _get_seat = None
    if _get_seat is not None:
        for _ugc in (getattr(alert, "affected_areas", []) or []):
            _s = _get_seat(_ugc)
            if not _s:
                continue
            _sn, _sla, _slo = _s
            if _sn.upper() in _seat_names:
                continue
            if not (lat_min <= _sla <= lat_max and lon_min <= _slo <= lon_max):
                continue
            _seat_names.add(_sn.upper())
            county_seat_pts.append((_sn, _sla, _slo))

    # Draw city dots and labels (avoid clutter)
    label_rects: list[tuple[int,int,int,int]] = []
    for name, state, lat, lon, pop in _density_filtered[:40]:
        if name.upper() in _seat_names:
            continue  # shown as a prominent county-seat marker below
        px, py = _to_px(lat, lon, lat_min, lat_max, lon_min, lon_max, MAP_W, MAP_H)

        # Dot size by population
        if pop > 200000:
            dot_r = 5; fsize = 18; bold = True
        elif pop > 80000:
            dot_r = 4; fsize = 15; bold = True
        elif pop > 30000:
            dot_r = 3; fsize = 13; bold = False
        else:
            dot_r = 2; fsize = 11; bold = False

        dot_color = (200, 210, 230)
        map_draw.ellipse([px-dot_r, py-dot_r, px+dot_r, py+dot_r], fill=dot_color)

        if fsize not in city_font_cache:
            city_font_cache[fsize] = _get_font(fsize, bold=bold)
        cf = city_font_cache[fsize]
        lbl = name.upper()
        bb = map_draw.textbbox((0, 0), lbl, font=cf)
        lw, lh = bb[2]-bb[0], bb[3]-bb[1]
        lx = px + dot_r + 4
        ly = py - lh // 2

        # Simple overlap check
        lr = (lx - 2, ly - 2, lx + lw + 2, ly + lh + 2)
        overlap = any(
            lr[0] < r[2] and lr[2] > r[0] and lr[1] < r[3] and lr[3] > r[1]
            for r in label_rects
        )
        if overlap:
            continue
        label_rects.append(lr)

        # Shadow
        map_draw.text((lx+1, ly+1), lbl, font=cf, fill=(0, 0, 0, 180))
        map_draw.text((lx, ly), lbl, font=cf, fill=(230, 235, 245, 255))

    # ── County-seat markers — one per affected county, drawn on top ────────
    # A ringed dot + bold white label so each affected county reads as
    # "this county = this town" at a glance.
    seat_font = _get_font(16, bold=True)
    seat_rects: list[tuple[int, int, int, int]] = []
    for sname, slat, slon in county_seat_pts:
        spx, spy = _to_px(slat, slon, lat_min, lat_max, lon_min, lon_max, MAP_W, MAP_H)
        map_draw.ellipse([spx-5, spy-5, spx+5, spy+5], outline=(255, 255, 255, 235), width=2)
        map_draw.ellipse([spx-3, spy-3, spx+3, spy+3], fill=(*evt_color, 255))
        lbl = sname.upper()
        bb = map_draw.textbbox((0, 0), lbl, font=seat_font)
        lw, lh = bb[2]-bb[0], bb[3]-bb[1]
        lx, ly = spx + 9, spy - lh // 2
        if lx + lw > MAP_W - 6:          # flip to the left near the right edge
            lx = spx - 9 - lw
        srect = (lx-2, ly-2, lx+lw+2, ly+lh+2)
        # Skip only a colliding LABEL (the marker is always drawn).
        if any(srect[0] < r[2] and srect[2] > r[0] and srect[1] < r[3] and srect[3] > r[1]
               for r in seat_rects):
            continue
        seat_rects.append(srect)
        map_draw.text((lx+1, ly+1), lbl, font=seat_font, fill=(0, 0, 0, 200))
        map_draw.text((lx, ly), lbl, font=seat_font, fill=(255, 255, 255, 255))

    # TOP CITIES card
    # Priority 1: "Locations impacted include..." from alert text (NWS-authoritative)
    areas = getattr(alert, "affected_areas", []) or []
    # Collect ALL states represented in the affected areas (watches can span multiple states)
    alert_states: set[str] = {a[:2].upper() for a in areas if len(a) >= 2}
    state_hint = next(iter(alert_states), "")
    # Fallback: extract state from "NWS <Office> <State>" pattern in sender_name
    if not alert_states:
        sender_name = (getattr(alert, "sender_name", "") or "").upper()
        m_state = _re.search(r'\b([A-Z]{2})\s*$', sender_name)
        if m_state:
            alert_states = {m_state.group(1)}
            state_hint = m_state.group(1)

    _sig_raw3 = getattr(alert, "significance", "W")
    significance = getattr(_sig_raw3, "value", str(_sig_raw3))
    is_watch = significance in ("A", "Y")  # Watch or Advisory covers multiple states

    impacted_names = _parse_impacted_locations(alert)
    top_cities: list[tuple] = []
    top_cities_from_nws = False

    if impacted_names:
        # For watches spanning multiple states, match against all alert states.
        # For single-state warnings, restrict to the one state to avoid cross-state errors.
        if is_watch and len(alert_states) > 1:
            # Balanced per state: round-robin by population so every affected
            # state is represented before the largest metro fills all 5 slots.
            per_state: dict[str, list[tuple]] = {}
            seen_names: set[str] = set()
            for st in alert_states:
                lst: list[tuple] = []
                for entry in _match_locations_to_db(impacted_names, st):
                    if entry[0].upper() not in seen_names:
                        lst.append(entry)
                        seen_names.add(entry[0].upper())
                lst.sort(key=lambda c: -c[4])
                if lst:
                    per_state[st] = lst
            # Visit states in order of their biggest city, taking the Nth-largest
            # from each per pass — guarantees ≥1 (then ≥2) from every state.
            state_order = sorted(per_state, key=lambda s: -per_state[s][0][4])
            matched = []
            rank = 0
            while len(matched) < 5 and any(rank < len(per_state[s]) for s in state_order):
                for s in state_order:
                    if rank < len(per_state[s]):
                        matched.append(per_state[s][rank])
                        if len(matched) >= 5:
                            break
                rank += 1
        else:
            matched = _match_locations_to_db(impacted_names, state_hint)
        top_cities = matched[:5]

        # For unmatched NWS-listed names: try any state before creating a synthetic entry
        if len(top_cities) < len(impacted_names):
            matched_up = {c[0].upper() for c in top_cities}
            for raw_name in impacted_names:
                if len(top_cities) >= 5:
                    break
                norm = " ".join(w.capitalize() for w in raw_name.strip().split())
                if norm.upper() in matched_up:
                    continue
                # Try matching this name in any state (last resort before synthetic)
                any_state_match = _match_locations_to_db([raw_name], "")
                if any_state_match:
                    top_cities.append(any_state_match[0])
                    matched_up.add(any_state_match[0][0].upper())
                else:
                    # Truly not in our DB — synthetic entry with no population
                    top_cities.append((norm, state_hint, 0.0, 0.0, 0))
                    matched_up.add(norm.upper())

        if top_cities:
            top_cities_from_nws = True

    # Priority 2: geographic — cities strictly inside the polygon/county geometry
    # Only used when NWS text gave us nothing at all.
    if not top_cities_from_nws and _shapely_geom is not None:
        try:
            from shapely.geometry import Point as _SPoint2
            strictly_inside = [
                c for c in visible_cities
                if _shapely_geom.distance(_SPoint2(c[3], c[2])) < 1e-6
            ]
            strictly_inside.sort(key=lambda c: -c[4])
            if len(strictly_inside) >= 2:
                top_cities = strictly_inside[:5]
            else:
                near = [c for c in visible_cities
                        if _shapely_geom.distance(_SPoint2(c[3], c[2])) < 0.15]
                near.sort(key=lambda c: -c[4])
                top_cities = near[:5] if near else strictly_inside[:5] or visible_cities[:3]
        except Exception:
            top_cities = visible_cities[:5]

    if not top_cities:
        top_cities = visible_cities[:5]

    # ── Affected Areas bar (ONW-style horizontal county list across the top) ─
    # Shows EVERY affected county across ALL states, bare names — so out-of-
    # home-state counties (e.g. the Ohio ones) are no longer dropped the way the
    # old 12-county sidebar cap dropped them.
    display_loc = getattr(alert, "display_locations", "") or ""
    area_counties: list[str] = []
    raw_parts = display_loc.split(";") if ";" in display_loc else display_loc.split(",")
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        # Drop a bare trailing state token ("OH") from comma-split fallbacks.
        if len(part) == 2 and part.isalpha() and part.isupper():
            continue
        name = part.split(",")[0].strip()            # "Bartholomew County"
        if name.lower().endswith(" county"):
            name = name[:-len(" county")].strip()     # "Bartholomew"
        if name and name not in area_counties:
            area_counties.append(name)
    if not area_counties:
        for ugc in areas:
            if len(ugc) >= 5:
                nm = ugc[2:].strip()
                if nm and nm not in area_counties:
                    area_counties.append(nm)

    cities_card_top = 16
    if area_counties:
        def _tw(s, f):
            bb = map_draw.textbbox((0, 0), s, font=f)
            return bb[2] - bb[0]
        bar_pad_x, bar_pad_y, line_h = 22, 11, 30
        lbl_font  = _get_font(20, bold=True)
        name_font = _get_font(20, bold=True)
        label = "Affected Areas:"
        sep = "   •   "
        max_w = MAP_W - bar_pad_x * 2
        lbl_w = _tw(label + "  ", lbl_font)
        rows: list[list[str]] = [[]]
        row_w: list[float] = [lbl_w]
        for nm in area_counties:
            tok_w = _tw(nm + sep, name_font)
            if rows[-1] and row_w[-1] + tok_w > max_w:
                rows.append([]); row_w.append(0.0)
            rows[-1].append(nm); row_w[-1] += tok_w
        bar_h = bar_pad_y * 2 + line_h * len(rows)
        # Translucent header strip + brand-color bottom accent.
        map_draw.rectangle([0, 0, MAP_W, bar_h], fill=(8, 12, 22, 210))
        map_draw.rectangle([0, bar_h, MAP_W, bar_h + 3], fill=(*evt_color, 235))
        ty = bar_pad_y
        for ri, row in enumerate(rows):
            tx = bar_pad_x
            if ri == 0:
                map_draw.text((tx, ty), label, font=lbl_font, fill=(150, 170, 210, 255))
                tx += lbl_w
            map_draw.text((tx, ty), sep.join(row), font=name_font, fill=(236, 240, 250, 255))
            ty += line_h
        cities_card_top = bar_h + 14

    if top_cities:
        card_x, card_y = 16, cities_card_top
        card_w = 220
        row_h  = 34
        hdr_h  = 30
        card_h = hdr_h + len(top_cities) * row_h + 12
        card_fill = (10, 16, 30, 210)
        card_border = (50, 70, 110, 200)

        map_draw.rounded_rectangle(
            [card_x, card_y, card_x+card_w, card_y+card_h],
            radius=8, fill=card_fill, outline=card_border, width=1
        )
        hf = _get_font(13, bold=True)
        map_draw.text((card_x+12, card_y+9), "LOCATIONS IMPACTED" if top_cities_from_nws else "TOP CITIES",
                      font=hf, fill=(150, 170, 210, 255))

        cf2 = _get_font(13, bold=False)
        cf2b = _get_font(13, bold=True)
        for i, (cname, cstate, clat, clon, cpop) in enumerate(top_cities):
            ry = card_y + hdr_h + i * row_h + 4
            # Pin dot
            map_draw.ellipse([card_x+10, ry+6, card_x+18, ry+14],
                             fill=(255, 100, 100, 220))
            # City name
            city_label = f"{cname}, {cstate}" if cstate else cname
            map_draw.text((card_x+24, ry+2), city_label,
                          font=cf2b, fill=(220, 225, 240, 255))
            # Population — omit for synthetic NWS-text entries (pop == 0)
            if cpop > 0:
                pop_str = f"Pop. {cpop:,}"
                map_draw.text((card_x+24, ry+16), pop_str,
                              font=cf2, fill=(130, 145, 175, 220))

    return base.convert("RGB")


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_alert_broadcast_graphic(
    alert,
    radar_frame=None,
    zone_polygons: "list[list[list[float]]] | None" = None,
    brand_name: str = "",
    meteorologist_name: str = "",
    width_px: int = TOTAL_W,
    height_px: int = TOTAL_H,
) -> bytes:
    """
    Generate a 1920x1080 broadcast-quality alert graphic.

    zone_polygons: flat list of county/zone polygon rings for zone-based alerts
    (watches, advisories). Each ring is [[lat, lon], ...].

    Returns PNG bytes.
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow is required for alert broadcast graphics")

    left  = _render_left_panel(alert, radar_frame, brand_name, meteorologist_name)
    right = _render_map_panel(alert, radar_frame, zone_polygons=zone_polygons)

    canvas = Image.new("RGB", (TOTAL_W, TOTAL_H), PANEL_BG)
    canvas.paste(left, (0, 0))
    canvas.paste(right, (PANEL_W, 0))

    # Vertical separator line between panels
    from PIL import ImageDraw
    sep_draw = ImageDraw.Draw(canvas)
    sep_color = tuple(min(255, c + 15) for c in DIVIDER_CLR)
    sep_draw.rectangle([PANEL_W - 2, 0, PANEL_W, TOTAL_H], fill=sep_color)

    if width_px != TOTAL_W or height_px != TOTAL_H:
        canvas = canvas.resize((width_px, height_px), Image.LANCZOS)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()


# ── State name lookup ─────────────────────────────────────────────────────────

_STATE_NAMES: dict[str, str] = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS",
    "CA": "CALIFORNIA", "CO": "COLORADO", "CT": "CONNECTICUT", "DE": "DELAWARE",
    "FL": "FLORIDA", "GA": "GEORGIA", "HI": "HAWAII", "ID": "IDAHO",
    "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS",
    "KY": "KENTUCKY", "LA": "LOUISIANA", "ME": "MAINE", "MD": "MARYLAND",
    "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA", "MS": "MISSISSIPPI",
    "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA",
    "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY", "NM": "NEW MEXICO", "NY": "NEW YORK",
    "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA", "OH": "OHIO", "OK": "OKLAHOMA",
    "OR": "OREGON", "PA": "PENNSYLVANIA", "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA",
    "SD": "SOUTH DAKOTA", "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH",
    "VT": "VERMONT", "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WEST VIRGINIA",
    "WI": "WISCONSIN", "WY": "WYOMING", "DC": "WASHINGTON D.C.",
}


# ── PDS / Confirmed Tornado Warning graphic ───────────────────────────────────

# Canvas constants specific to this layout
_PDS_W      = 1920
_PDS_H      = 1080
_PDS_HDR_H  = 90       # header bar height
_PDS_FTR_H  = 42       # footer bar height
_PDS_LEFT_W = 400      # left info panel width
_PDS_MAP_W  = _PDS_W - _PDS_LEFT_W   # 1520
_PDS_MAP_H  = _PDS_H                  # full 1080 — map drawn behind header/footer


def _draw_pds_map(
    alert,
    radar_frame,
    zone_polygons,
    width: int = _PDS_MAP_W,
    height: int = _PDS_MAP_H,
) -> "PIL.Image.Image":
    """
    Render the map panel for the PDS graphic at an arbitrary size.
    Wraps _render_map_panel and resizes to (width, height).
    """
    try:
        from PIL import Image
    except ImportError:
        try:
            from PIL import Image
        except Exception:
            raise

    full = _render_map_panel(alert, radar_frame, zone_polygons=zone_polygons)
    if full.size != (width, height):
        full = full.resize((width, height), Image.LANCZOS)
    return full


def _draw_mini_us_map(
    centroid: "tuple[float, float] | None",
    width: int = 200,
    height: int = 126,
    state_label: str = "",
) -> "PIL.Image.Image | None":
    """
    Tiny inset map of the continental US with a glowing red dot + state label at centroid.
    Fetches CartoDB tiles at zoom 4 for bbox (24–50°N, 125–65°W).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    US_LAT_MIN, US_LAT_MAX = 24.0, 50.0
    US_LON_MIN, US_LON_MAX = -125.0, -65.0

    base = _fetch_tiles(US_LAT_MIN, US_LAT_MAX, US_LON_MIN, US_LON_MAX)
    if base is None:
        base = Image.new("RGB", (width, height), (10, 14, 24))
    base = base.resize((width, height), Image.LANCZOS)

    draw = ImageDraw.Draw(base)

    if centroid:
        clat, clon = centroid
        clat = max(US_LAT_MIN, min(US_LAT_MAX, clat))
        clon = max(US_LON_MIN, min(US_LON_MAX, clon))
        cx, cy = _to_px(clat, clon, US_LAT_MIN, US_LAT_MAX, US_LON_MIN, US_LON_MAX, width, height)

        # Glow rings: simulate alpha with progressively brighter reds on dark bg
        for gr, gc in [(14, (90, 10, 10)), (11, (150, 15, 15)), (8, (200, 20, 20))]:
            draw.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=gc)

        # Crosshair lines through the dot
        ch_len = 10
        draw.line([cx - ch_len, cy, cx - 8, cy], fill=(255, 60, 60), width=1)
        draw.line([cx + 8, cy, cx + ch_len, cy], fill=(255, 60, 60), width=1)
        draw.line([cx, cy - ch_len, cx, cy - 8], fill=(255, 60, 60), width=1)
        draw.line([cx, cy + 8, cx, cy + ch_len], fill=(255, 60, 60), width=1)

        # White halo + bright red center dot
        draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=(255, 255, 255))
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(230, 30, 30))

        # State label — small text, positioned so it doesn't clip the edge
        if state_label:
            lf = _get_font(11, bold=True)
            bb = draw.textbbox((0, 0), state_label, font=lf)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
            lx = min(cx + 9, width - lw - 3)
            ly = max(cy - lh - 2, 2)
            # Dark backing rect for legibility
            draw.rectangle([lx - 2, ly - 1, lx + lw + 2, ly + lh + 1], fill=(10, 10, 18))
            draw.text((lx, ly), state_label, font=lf, fill=(255, 200, 200))

    # Thin border
    draw.rectangle([0, 0, width - 1, height - 1], outline=(80, 90, 120), width=1)
    return base


def generate_tornado_confirmed_graphic(
    alert,
    radar_frame=None,
    zone_polygons: "list[list[list[float]]] | None" = None,
    brand_name: str = "",
    meteorologist_name: str = "",
) -> bytes:
    """
    Generate a 1920×1080 high-impact graphic for tornado warnings with a
    confirmed/observed tornado or PDS designation.

    Layout:
    - Full-width red header bar (brand + warning title)
    - Left panel (400px): damage header, threat tags, expiry, impacts, safety tips
    - Right panel (1520px): full-size map with radar, polygon, cities
    - Mini US inset map (bottom-left of right panel)
    - Full-width dark footer bar
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RuntimeError("Pillow is required for alert broadcast graphics")

    # ── Determine metadata ─────────────────────────────────────────────────────
    threat = getattr(alert, "threat", None)
    tor_det   = (getattr(threat, "tornado_detection", None) or "").upper()
    dmg_threat = (getattr(threat, "tornado_damage_threat", None) or "").upper()
    description = (getattr(alert, "description", "") or "")

    # Tornado Emergency is the single most severe wording the NWS issues and
    # must always read louder than a PDS. Both come from the canonical Alert
    # properties (single source of truth).
    is_emergency = alert.is_tornado_emergency
    is_pds = alert.is_pds
    is_considerable = dmg_threat == "CONSIDERABLE"

    state_code = ""
    areas = getattr(alert, "affected_areas", []) or []
    if areas:
        state_code = areas[0][:2].upper()
    state_name = _STATE_NAMES.get(state_code, state_code or "YOUR AREA")

    hail = getattr(threat, "max_hail_size_inches", None)
    wind = getattr(threat, "max_wind_gust_mph", None)
    storm_motion = getattr(threat, "storm_motion", None) if threat else None

    exp_local = _alert_local_time(
        getattr(alert, "expiration_time", None), alert
    ) if getattr(alert, "expiration_time", None) else None

    centroid = getattr(alert, "centroid", None)

    product_id = getattr(alert, "product_id", "")
    brand_url = getattr(alert, "_brand_url", "")

    # ── Color scheme ───────────────────────────────────────────────────────────
    BG          = (8,   8,  12)         # near-black canvas
    # Emergencies get a hotter crimson-magenta header so they're unmistakable
    # next to a (red) PDS / confirmed tornado graphic.
    HDR_RED     = (150,  0,  60) if is_emergency else (155, 10, 10)  # header bar
    HDR_TEXT    = (255, 255, 255)
    LEFT_BG     = (12,  12,  18)
    DMG_BOX_BG  = (25,   6,   6)       # dark red tint for damage header box
    DMG_BOX_BRD = (200, 25,  25)       # bright red border
    DMG_TEXT    = (255, 255, 255)
    BADGE_RED   = (120,  15,  15)
    BADGE_TEXT  = (255, 220, 220)
    EXP_LABEL   = (140, 155, 175)
    EXP_TIME    = (255, 255, 255)
    CARD_DARK   = (20,  20,  28)
    CARD_BRD    = (50,  55,  75)
    CARD_LBL    = (140, 165, 210)
    CARD_VAL    = (255, 255, 255)
    NWS_BOX_BG  = (16,  16,  24)
    WHAT_HDR_BG = (90,  55,   0)       # dark amber for "WHAT TO DO"
    WHAT_HDR_TX = (255, 225, 130)
    WHAT_TEXT   = (210, 215, 225)
    FTR_BG      = (15,  15,  22)
    FTR_TEXT    = (100, 115, 140)
    SEP_CLR     = (35,  40,  60)

    # ── Canvas ─────────────────────────────────────────────────────────────────
    canvas = Image.new("RGB", (_PDS_W, _PDS_H), BG)
    draw   = ImageDraw.Draw(canvas)

    # ── Map panel (right side, full height; header/footer drawn over it) ───────
    map_img = _draw_pds_map(alert, radar_frame, zone_polygons, _PDS_MAP_W, _PDS_MAP_H)
    canvas.paste(map_img, (_PDS_LEFT_W, 0))

    # Mini US inset — bottom-left corner of the map area
    mini_w, mini_h = 200, 126
    mini = _draw_mini_us_map(centroid, mini_w, mini_h, state_label=state_code)
    if mini:
        mini_x = _PDS_LEFT_W + 14
        mini_y = _PDS_H - _PDS_FTR_H - mini_h - 12
        canvas.paste(mini, (mini_x, mini_y))

    # ── Left panel ─────────────────────────────────────────────────────────────
    draw.rectangle([0, 0, _PDS_LEFT_W, _PDS_H], fill=LEFT_BG)
    # Right-edge separator
    draw.rectangle([_PDS_LEFT_W - 2, 0, _PDS_LEFT_W, _PDS_H], fill=SEP_CLR)

    mx = 20   # left margin inside panel
    pw = _PDS_LEFT_W - mx * 2   # usable panel width
    y  = _PDS_HDR_H + 18         # start below header

    # ── SIGNIFICANT DAMAGE LIKELY box ─────────────────────────────────────────
    box_h = 88
    _rounded_rect(draw, mx, y, mx + pw, y + box_h, r=6,
                  fill=DMG_BOX_BG, outline=DMG_BOX_BRD, outline_w=3)
    dmg_label = (
        "TORNADO\nEMERGENCY" if is_emergency else
        "SIGNIFICANT DAMAGE\nLIKELY" if is_pds else
        "CONSIDERABLE DAMAGE\nPOSSIBLE" if is_considerable else
        "TAKE SHELTER\nIMMEDIATELY"
    )
    dlf = _get_font(22, bold=True)
    dy = y + 10
    for line in dmg_label.split("\n"):
        bb = draw.textbbox((0, 0), line, font=dlf)
        # shadow
        draw.text((mx + 12 + 1, dy + 1), line, font=dlf, fill=(0, 0, 0, 200))
        draw.text((mx + 12, dy), line, font=dlf, fill=DMG_TEXT)
        dy += (bb[3] - bb[1]) + 4
    y += box_h + 14

    # ── Threat badges ──────────────────────────────────────────────────────────
    badge_h   = 28
    badge_gap = 8
    badges = []
    if is_emergency:
        badges.append("TORNADO EMERGENCY")
    elif is_pds:
        badges.append("PARTICULARLY DANGEROUS")
    if tor_det == "OBSERVED":
        badges.append("TORNADO CONFIRMED")
    elif tor_det == "RADAR INDICATED":
        badges.append("TORNADO RADAR INDICATED")
    else:
        badges.append("TORNADO WARNING")

    for badge_text in badges:
        bf = _get_font(13, bold=True)
        bb = draw.textbbox((0, 0), badge_text, font=bf)
        bw = bb[2] - bb[0] + 24
        _rounded_rect(draw, mx, y, mx + bw, y + badge_h, r=4,
                      fill=BADGE_RED, outline=(180, 20, 20), outline_w=1)
        draw.text((mx + 12, y + 7), badge_text, font=bf, fill=BADGE_TEXT)
        y += badge_h + badge_gap
    y += 6

    # Thin divider
    draw.rectangle([mx, y, mx + pw, y + 1], fill=SEP_CLR)
    y += 14

    # ── Expiration time ────────────────────────────────────────────────────────
    ef_lbl = _get_font(12, bold=True)
    draw.text((mx, y), "EXPIRES", font=ef_lbl, fill=EXP_LABEL)
    if exp_local:
        try:
            date_str = exp_local.strftime("%a, %b %d, %Y").replace(" 0", " ")
        except Exception:
            date_str = str(exp_local)
        bb = draw.textbbox((0, 0), "EXPIRES", font=ef_lbl)
        draw.text((mx + bb[2] - bb[0] + 14, y + 1), date_str, font=ef_lbl, fill=EXP_LABEL)
    y += 18

    if exp_local:
        time_str = exp_local.strftime("%I:%M %p").lstrip("0") + " " + _tz_abbr(exp_local)
        tf = _get_font(52, bold=True)
        bb = draw.textbbox((0, 0), time_str, font=tf)
        draw.text((mx, y), time_str, font=tf, fill=EXP_TIME)
        y += (bb[3] - bb[1]) + 10
    else:
        y += 10

    # Divider
    draw.rectangle([mx, y, mx + pw, y + 1], fill=SEP_CLR)
    y += 14

    # ── Threat cards (HAIL, WIND) ──────────────────────────────────────────────
    _card_h = 76
    _card_gap = 8

    def _draw_threat_card(label: str, value: str, sub: str = ""):
        nonlocal y
        _rounded_rect(draw, mx, y, mx + pw, y + _card_h, r=5,
                      fill=CARD_DARK, outline=CARD_BRD, outline_w=1)
        # left accent bar
        draw.rectangle([mx, y, mx + 4, y + _card_h], fill=(80, 100, 160))
        lf2 = _get_font(13, bold=True)
        draw.text((mx + 12, y + 10), label, font=lf2, fill=CARD_LBL)
        vf2 = _get_font(30, bold=True)
        draw.text((mx + 12, y + 26), value, font=vf2, fill=CARD_VAL)
        if sub:
            sf2 = _get_font(11, bold=False)
            draw.text((mx + 12, y + 58), sub, font=sf2, fill=EXP_LABEL)
        y += _card_h + _card_gap

    if hail:
        hail_val = f"{hail:.2f}".rstrip("0").rstrip(".") + "in"
        hail_sub = _hail_descriptor(hail)
        _draw_threat_card("HAIL", hail_val, hail_sub)

    if wind:
        _draw_threat_card("WIND GUSTS", f"{wind} MPH")

    if storm_motion and getattr(storm_motion, "is_valid", False):
        dir_s = getattr(storm_motion, "direction_from", "") or ""
        spd_s = getattr(storm_motion, "speed_mph", 0) or 0
        _draw_threat_card("MOTION", f"{_motion_toward(dir_s)} {spd_s} MPH".strip())

    # ── NWS notable text box ───────────────────────────────────────────────────
    # Pull meaningful content from description — skip NWS header boilerplate,
    # prefer the storm position statement, then impact, then any clean line.
    import re as _re_notable
    notable = ""
    if description:
        lines = [l.strip() for l in description.split("\n")]
        # Pass 1: storm position line — "AT H:MM PM CDT, A TORNADO WAS LOCATED..."
        for line in lines:
            clean = line.lstrip("* ").strip()
            if (_re_notable.match(r'AT \d+:\d+', clean, _re_notable.IGNORECASE)
                    and len(clean) > 30 and "LAT" not in clean):
                notable = clean[:160]
                break
        # Pass 2: IMPACT bullet
        if not notable:
            for line in lines:
                clean = line.lstrip("* ").strip()
                if clean.upper().startswith("IMPACT") and "..." in clean and len(clean) > 30:
                    notable = clean.split("...", 1)[-1].strip()[:160]
                    break
        # Pass 3: first non-boilerplate, non-structural line
        _SKIP = ("THE NATIONAL WEATHER SERVICE", "A TORNADO", "A SEVERE THUNDERSTORM",
                 "TORNADO WARNING", "SEVERE THUNDERSTORM WARNING", "UNTIL ", "* ")
        if not notable:
            for line in lines:
                if (len(line) > 30 and "LAT" not in line
                        and not any(line.upper().startswith(s) for s in _SKIP)):
                    notable = line[:160]
                    break
    if notable:
        draw.rectangle([mx, y, mx + pw, y + 1], fill=SEP_CLR)
        y += 10
        nb_f = _get_font(11, bold=False)
        _rounded_rect(draw, mx, y, mx + pw, y + 2, r=3, fill=NWS_BOX_BG)
        wrapped = _wrap_text(notable.upper(), nb_f, draw, pw - 16)
        nb_h = len(wrapped) * 14 + 14
        _rounded_rect(draw, mx, y, mx + pw, y + nb_h, r=4,
                      fill=NWS_BOX_BG, outline=SEP_CLR, outline_w=1)
        ty2 = y + 8
        for wline in wrapped:
            draw.text((mx + 8, ty2), wline, font=nb_f, fill=(180, 190, 210))
            ty2 += 14
        y += nb_h + 10

    # ── WHAT TO DO ─────────────────────────────────────────────────────────────
    safety_tips = (
        "Move to the lowest floor of a sturdy building. "
        "Go to an interior room away from windows — a bathroom, closet, "
        "or beneath stairs. If you cannot find shelter, "
        "lie flat in a low-lying area and cover your head."
    )

    remaining = _PDS_H - _PDS_FTR_H - y - 8
    if remaining > 60:
        wt_hdr_h = 28
        _rounded_rect(draw, mx, y, mx + pw, y + wt_hdr_h, r=4,
                      fill=WHAT_HDR_BG, outline=(130, 85, 0), outline_w=1)
        whf = _get_font(13, bold=True)
        draw.text((mx + 10, y + 8), "WHAT TO DO", font=whf, fill=WHAT_HDR_TX)
        y += wt_hdr_h + 8

        stf = _get_font(12, bold=False)
        wrapped_tips = _wrap_text(safety_tips, stf, draw, pw - 4)
        for tip_line in wrapped_tips:
            if y + 14 > _PDS_H - _PDS_FTR_H - 4:
                break
            draw.text((mx, y), tip_line, font=stf, fill=WHAT_TEXT)
            y += 14

    # ── Header bar (drawn OVER map and left panel) ─────────────────────────────
    draw.rectangle([0, 0, _PDS_W, _PDS_HDR_H], fill=HDR_RED)
    # Bottom edge accent
    draw.rectangle([0, _PDS_HDR_H - 3, _PDS_W, _PDS_HDR_H], fill=(200, 20, 20))

    # Brand logo / name (left)
    logo_x = 18
    logo_w  = 0
    brand_display = brand_name or "Alert Dashboard"
    logo_font = _get_font(16, bold=True)
    bb = draw.textbbox((0, 0), brand_display, font=logo_font)
    lh = bb[3] - bb[1]
    draw.text((logo_x, (_PDS_HDR_H - lh) // 2), brand_display,
              font=logo_font, fill=(255, 200, 200))
    logo_w = bb[2] - bb[0] + 28
    # Vertical separator after brand
    draw.rectangle([logo_x + logo_w, 12, logo_x + logo_w + 2, _PDS_HDR_H - 12],
                   fill=(200, 60, 60))

    # Warning title
    title = (
        f"TORNADO EMERGENCY IN {state_name}" if is_emergency else
        f"PDS TORNADO WARNING IN {state_name}" if is_pds else
        f"CONFIRMED TORNADO WARNING IN {state_name}"
    )
    hdr_font_size = 42
    while hdr_font_size > 24:
        hf2 = _get_font(hdr_font_size, bold=True)
        bb = draw.textbbox((0, 0), title, font=hf2)
        if bb[2] - bb[0] <= _PDS_W - logo_x - logo_w - 30:
            break
        hdr_font_size -= 2
    hf2 = _get_font(hdr_font_size, bold=True)
    bb = draw.textbbox((0, 0), title, font=hf2)
    th = bb[3] - bb[1]
    tx = logo_x + logo_w + 12
    ty = (_PDS_HDR_H - th) // 2
    draw.text((tx + 1, ty + 1), title, font=hf2, fill=(80, 0, 0))  # shadow
    draw.text((tx, ty), title, font=hf2, fill=HDR_TEXT)

    # ── Footer bar ─────────────────────────────────────────────────────────────
    fy = _PDS_H - _PDS_FTR_H
    draw.rectangle([0, fy, _PDS_W, _PDS_H], fill=FTR_BG)
    draw.rectangle([0, fy, _PDS_W, fy + 1], fill=SEP_CLR)

    ff = _get_font(13, bold=False)
    # Left: brand website or meteorologist credit
    credit = f"Powered by {meteorologist_name}" if meteorologist_name else (brand_name or "The Battin Front")
    draw.text((18, fy + 14), credit, font=ff, fill=FTR_TEXT)

    # Center: product ID
    if product_id:
        bb = draw.textbbox((0, 0), product_id, font=ff)
        draw.text((_PDS_W // 2 - (bb[2] - bb[0]) // 2, fy + 14),
                  product_id, font=ff, fill=FTR_TEXT)

    # Right: timestamp
    now_str = datetime.now(timezone.utc).strftime("Generated %I:%M %p UTC").lstrip("0")
    bb = draw.textbbox((0, 0), now_str, font=ff)
    draw.text((_PDS_W - 18 - (bb[2] - bb[0]), fy + 14), now_str, font=ff, fill=FTR_TEXT)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()


def _hail_descriptor(inches: float) -> str:
    """Return a human-readable hail size descriptor."""
    if inches >= 4.0:  return "Softball size"
    if inches >= 2.75: return "Baseball size"
    if inches >= 2.0:  return "Tennis ball size"
    if inches >= 1.75: return "Golf ball size"
    if inches >= 1.5:  return "Ping pong ball size"
    if inches >= 1.0:  return "Quarter size"
    if inches >= 0.75: return "Penny size"
    return "Marble size"


def _tz_abbr(dt) -> str:
    """Return timezone abbreviation from a timezone-aware datetime."""
    try:
        return dt.strftime("%Z")
    except Exception:
        return ""
