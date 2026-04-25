"""
Full-Screen Headline Graphic Generator.

Produces broadcast-quality 1920x1080 PNG cards for major weather events,
suitable for on-screen display during live streams and social posting.
"""

import io
import logging
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Color palette per phenomenon / significance ───────────────────────────────

PHENOMENON_STYLES: dict[str, dict] = {
    "TO_W": {
        "bg_top":    "#1a0000",
        "bg_bottom": "#550000",
        "accent":    "#ff2222",
        "text":      "#ffffff",
        "label":     "TORNADO WARNING",
    },
    "TO_A": {
        "bg_top":    "#1a1000",
        "bg_bottom": "#554400",
        "accent":    "#ffcc00",
        "text":      "#ffffff",
        "label":     "TORNADO WATCH",
    },
    "SV_W": {
        "bg_top":    "#0d0d00",
        "bg_bottom": "#443300",
        "accent":    "#ffa500",
        "text":      "#ffffff",
        "label":     "SEVERE THUNDERSTORM WARNING",
    },
    "SV_A": {
        "bg_top":    "#0d0d00",
        "bg_bottom": "#3a3000",
        "accent":    "#ddaa00",
        "text":      "#ffffff",
        "label":     "SEVERE THUNDERSTORM WATCH",
    },
    "FF_W": {
        "bg_top":    "#001a00",
        "bg_bottom": "#004400",
        "accent":    "#00cc44",
        "text":      "#ffffff",
        "label":     "FLASH FLOOD WARNING",
    },
    "FF_A": {
        "bg_top":    "#001200",
        "bg_bottom": "#003300",
        "accent":    "#00aa33",
        "text":      "#ffffff",
        "label":     "FLASH FLOOD WATCH",
    },
    "WS_W": {
        "bg_top":    "#000520",
        "bg_bottom": "#001055",
        "accent":    "#6699ff",
        "text":      "#ffffff",
        "label":     "WINTER STORM WARNING",
    },
    "WS_A": {
        "bg_top":    "#000520",
        "bg_bottom": "#001040",
        "accent":    "#4477ee",
        "text":      "#ffffff",
        "label":     "WINTER STORM WATCH",
    },
    "BZ_W": {
        "bg_top":    "#000520",
        "bg_bottom": "#001055",
        "accent":    "#88aaff",
        "text":      "#ffffff",
        "label":     "BLIZZARD WARNING",
    },
    "HW_W": {
        "bg_top":    "#100010",
        "bg_bottom": "#330033",
        "accent":    "#cc88ff",
        "text":      "#ffffff",
        "label":     "HIGH WIND WARNING",
    },
    "EW_W": {
        "bg_top":    "#100010",
        "bg_bottom": "#440044",
        "accent":    "#ff66ff",
        "text":      "#ffffff",
        "label":     "EXTREME WIND WARNING",
    },
    "EMERGENCY": {
        "bg_top":    "#220000",
        "bg_bottom": "#880000",
        "accent":    "#ff0000",
        "text":      "#ffffff",
        "label":     "TORNADO EMERGENCY",
    },
    "END": {
        "bg_top":    "#001100",
        "bg_bottom": "#003300",
        "accent":    "#44dd88",
        "text":      "#ffffff",
        "label":     "ALL CLEAR",
    },
    "DEFAULT": {
        "bg_top":    "#080c1a",
        "bg_bottom": "#101830",
        "accent":    "#3b82f6",
        "text":      "#ffffff",
        "label":     "WEATHER ALERT",
    },
}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))  # type: ignore


def _lerp_color(c1: tuple, c2: tuple, t: float) -> tuple[int, int, int]:
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))  # type: ignore


def generate_headline_graphic(
    event_type: str,           # e.g. "TO_W", "SV_W", "EMERGENCY", "END"
    headline: str,             # Large bold text: "Tornado Warning"
    subtitle: str = "",        # Second line: "Warren, Clinton Counties"
    body: str = "",            # Smaller detail text
    brand_name: str = "",
    brand_tagline: str = "",
    issued_by: str = "",       # "Issued by NWS Indianapolis"
    expires: str = "",         # "Expires 7:45 PM EDT"
    is_emergency: bool = False,
    width_px: int = 1920,
    height_px: int = 1080,
) -> bytes:
    """
    Render a full-screen broadcast-quality headline graphic.

    Returns PNG bytes.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        import numpy as np
    except ImportError as e:
        raise RuntimeError(f"Pillow is required for headline graphics: {e}")

    style = PHENOMENON_STYLES.get(event_type, PHENOMENON_STYLES["DEFAULT"])
    if is_emergency:
        style = PHENOMENON_STYLES["EMERGENCY"]

    bg_top = _hex_to_rgb(style["bg_top"])
    bg_bottom = _hex_to_rgb(style["bg_bottom"])
    accent = _hex_to_rgb(style["accent"])
    text_color = _hex_to_rgb(style["text"])

    img = Image.new("RGB", (width_px, height_px))

    # ── Background gradient ───────────────────────────────────────────────
    import numpy as np
    arr = np.zeros((height_px, width_px, 3), dtype=np.uint8)
    for y in range(height_px):
        t = y / height_px
        color = _lerp_color(bg_top, bg_bottom, t)
        arr[y, :] = color
    img = Image.fromarray(arr, "RGB")
    draw = ImageDraw.Draw(img)

    # ── Accent bar on left edge ───────────────────────────────────────────
    bar_w = 18
    for y in range(height_px):
        t = y / height_px
        # Fade from accent to darker accent
        r = int(accent[0] * (1 - t * 0.4))
        g = int(accent[1] * (1 - t * 0.4))
        b = int(accent[2] * (1 - t * 0.4))
        draw.line([(0, y), (bar_w, y)], fill=(r, g, b))

    # ── Bottom accent stripe ──────────────────────────────────────────────
    stripe_h = 8
    draw.rectangle([(0, height_px - stripe_h), (width_px, height_px)],
                   fill=tuple(accent))

    # ── Helper to find a font ─────────────────────────────────────────────
    def get_font(size: int, bold: bool = False):
        # Try common system fonts; fall back to PIL default
        candidates = []
        if bold:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/calibrib.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
            ]
        else:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/calibri.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
            ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except (IOError, OSError):
                continue
        return ImageFont.load_default()

    # ── Layout constants ──────────────────────────────────────────────────
    margin_left = bar_w + 80
    margin_right = 80
    center_y = height_px // 2

    # ── Event type label (top of card) ────────────────────────────────────
    label_text = style["label"]
    label_font = get_font(36, bold=False)
    label_color = tuple(min(255, int(c * 1.3)) for c in accent)
    draw.text((margin_left, 60), label_text, font=label_font, fill=label_color)

    # ── Emergency flash banner ────────────────────────────────────────────
    if is_emergency or event_type == "EMERGENCY":
        banner_y1, banner_y2 = 110, 160
        draw.rectangle([(0, banner_y1), (width_px, banner_y2)], fill=tuple(accent))
        em_font = get_font(34, bold=True)
        em_text = "⚠  PARTICULARLY DANGEROUS SITUATION  ⚠"
        em_bbox = draw.textbbox((0, 0), em_text, font=em_font)
        em_w = em_bbox[2] - em_bbox[0]
        draw.text(((width_px - em_w) // 2, banner_y1 + 8), em_text,
                  font=em_font, fill=(0, 0, 0))

    # ── Main headline ─────────────────────────────────────────────────────
    headline_font_size = 120
    # Shrink if headline is long
    while headline_font_size > 48:
        hf = get_font(headline_font_size, bold=True)
        hbbox = draw.textbbox((0, 0), headline, font=hf)
        if (hbbox[2] - hbbox[0]) <= width_px - margin_left - margin_right:
            break
        headline_font_size -= 8
    headline_font = get_font(headline_font_size, bold=True)
    headline_y = center_y - 140 if subtitle else center_y - 60
    draw.text((margin_left, headline_y), headline,
              font=headline_font, fill=text_color)

    # ── Divider line under headline ───────────────────────────────────────
    div_y = headline_y + headline_font_size + 10
    draw.rectangle([(margin_left, div_y), (margin_left + 200, div_y + 4)],
                   fill=tuple(accent))

    # ── Subtitle ─────────────────────────────────────────────────────────
    y_cursor = div_y + 24
    if subtitle:
        sub_font_size = 58
        while sub_font_size > 28:
            sf = get_font(sub_font_size, bold=False)
            sbbox = draw.textbbox((0, 0), subtitle, font=sf)
            if (sbbox[2] - sbbox[0]) <= width_px - margin_left - margin_right:
                break
            sub_font_size -= 4
        sub_font = get_font(sub_font_size, bold=False)
        sub_color = tuple(min(255, int(c * 1.5)) for c in accent)
        draw.text((margin_left, y_cursor), subtitle, font=sub_font, fill=sub_color)
        y_cursor += sub_font_size + 16

    # ── Body text ────────────────────────────────────────────────────────
    if body:
        body_font = get_font(32, bold=False)
        body_color = (200, 205, 220)
        # Word-wrap body text
        words = body.split()
        lines = []
        current_line = ""
        for word in words:
            test = (current_line + " " + word).strip()
            bbox = draw.textbbox((0, 0), test, font=body_font)
            if bbox[2] - bbox[0] > width_px - margin_left - margin_right:
                if current_line:
                    lines.append(current_line)
                current_line = word
            else:
                current_line = test
        if current_line:
            lines.append(current_line)
        for line in lines[:4]:  # max 4 lines
            draw.text((margin_left, y_cursor), line, font=body_font, fill=body_color)
            y_cursor += 40

    # ── Bottom info row ───────────────────────────────────────────────────
    bottom_y = height_px - stripe_h - 60
    info_font = get_font(28, bold=False)
    info_color = (160, 168, 190)

    parts = []
    if issued_by:
        parts.append(issued_by)
    if expires:
        parts.append(f"Expires: {expires}")
    if parts:
        draw.text((margin_left, bottom_y), "  ·  ".join(parts),
                  font=info_font, fill=info_color)

    # ── Brand / watermark (right side) ───────────────────────────────────
    if brand_name:
        brand_font = get_font(30, bold=True)
        brand_bbox = draw.textbbox((0, 0), brand_name, font=brand_font)
        brand_w = brand_bbox[2] - brand_bbox[0]
        draw.text((width_px - margin_right - brand_w, bottom_y),
                  brand_name, font=brand_font, fill=tuple(accent))
        if brand_tagline:
            tag_font = get_font(20, bold=False)
            tag_bbox = draw.textbbox((0, 0), brand_tagline, font=tag_font)
            tag_w = tag_bbox[2] - tag_bbox[0]
            draw.text((width_px - margin_right - tag_w, bottom_y + 34),
                      brand_tagline, font=tag_font, fill=info_color)

    # ── Render ────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.read()
