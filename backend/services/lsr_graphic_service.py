"""
LSR Damage Survey / Summary Graphic Generator.

Produces a server-side rendered PNG showing storm report markers on a
coordinate map, suitable for end-of-event recaps and social media posting.
"""

import io
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Approximate CONUS state center lat/lon for map labels
STATE_CENTERS: dict[str, tuple[float, float]] = {
    "AL": (32.8, -86.8), "AR": (34.8, -92.2), "AZ": (34.3, -111.1),
    "CA": (36.8, -119.4), "CO": (39.0, -105.5), "CT": (41.6, -72.7),
    "DE": (39.0, -75.5), "FL": (27.9, -81.5), "GA": (32.2, -83.4),
    "IA": (42.0, -93.5), "ID": (44.3, -114.5), "IL": (40.3, -89.0),
    "IN": (39.9, -86.3), "KS": (38.5, -98.4), "KY": (37.5, -85.3),
    "LA": (31.1, -91.8), "MA": (42.4, -71.4), "MD": (39.1, -76.8),
    "ME": (45.4, -69.2), "MI": (44.0, -85.5), "MN": (46.4, -93.1),
    "MO": (38.5, -92.5), "MS": (32.7, -89.7), "MT": (47.0, -110.5),
    "NC": (35.6, -79.8), "ND": (47.4, -100.5), "NE": (41.5, -99.9),
    "NH": (43.7, -71.6), "NJ": (40.1, -74.4), "NM": (34.3, -106.0),
    "NV": (39.5, -117.0), "NY": (42.9, -75.5), "OH": (40.4, -82.8),
    "OK": (35.5, -97.5), "OR": (44.0, -120.5), "PA": (41.0, -77.5),
    "RI": (41.7, -71.5), "SC": (33.8, -80.9), "SD": (44.3, -100.3),
    "TN": (35.9, -86.4), "TX": (31.1, -100.0), "UT": (39.5, -111.1),
    "VA": (37.5, -79.5), "VT": (44.1, -72.7), "WA": (47.4, -120.5),
    "WI": (44.5, -89.5), "WV": (38.6, -80.6), "WY": (43.0, -107.5),
}

# Colors for each report type
TYPE_COLORS: dict[str, str] = {
    "TORNADO":       "#ff2222",
    "FUNNEL CLOUD":  "#ff8800",
    "WALL CLOUD":    "#ffaa00",
    "HAIL":          "#22dd44",
    "TSTM WND GST":  "#4488ff",
    "TSTM WND DMG":  "#2266ff",
    "NON-TSTM WND":  "#88aaff",
    "FLASH FLOOD":   "#aa44ff",
    "FLOOD":         "#8833cc",
    "HEAVY RAIN":    "#6655cc",
    "LIGHTNING":     "#ffee00",
    "SNOW":          "#aaccff",
    "HEAVY SNOW":    "#88aaff",
    "BLIZZARD":      "#6688ff",
    "ICE STORM":     "#88ddff",
    "SLEET":         "#aaddee",
    "FREEZING RAIN": "#77ccdd",
}

TYPE_MARKERS: dict[str, str] = {
    "TORNADO": "v",        # triangle down = tornado
    "FUNNEL CLOUD": "^",
    "HAIL": "o",
    "TSTM WND GST": "D",  # diamond
    "TSTM WND DMG": "D",
    "NON-TSTM WND": "D",
    "FLASH FLOOD": "s",   # square
    "FLOOD": "s",
}

DARK_BG = "#0f1117"
CARD_BG = "#1a1d27"
TEXT_PRIMARY = "#e8e8f0"
TEXT_SECONDARY = "#8890aa"
BORDER_COLOR = "#2a2d3d"
ACCENT_BLUE = "#3b82f6"


def _normalize_type(report_type: str) -> str:
    return (report_type or "").upper().strip()


def generate_lsr_summary_graphic(
    reports: list,
    title: str = "Storm Report Summary",
    hours: int = 24,
    brand_name: str = "Alert Dashboard",
    states: Optional[list[str]] = None,
    width_px: int = 1920,
    height_px: int = 1080,
) -> bytes:
    """
    Render a summary graphic of LSR reports on a coordinate map.

    Args:
        reports: List of StormReport objects with lat, lon, report_type, magnitude, city, state
        title: Graphic title text
        hours: Lookback period displayed in subtitle
        brand_name: Watermark / source label
        states: Filter map extent to these states (None = auto-fit to reports)
        width_px / height_px: Output resolution

    Returns:
        PNG bytes
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.gridspec import GridSpec
        import numpy as np
    except ImportError as e:
        raise RuntimeError(f"matplotlib is required for LSR graphics: {e}")

    dpi = 150
    fig_w = width_px / dpi
    fig_h = height_px / dpi

    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=DARK_BG)

    # Layout: wide map on left, stats panel on right
    gs = GridSpec(1, 2, figure=fig, width_ratios=[2.8, 1], wspace=0.02)
    ax_map = fig.add_subplot(gs[0])
    ax_stats = fig.add_subplot(gs[1])

    ax_map.set_facecolor(CARD_BG)
    ax_stats.set_facecolor(DARK_BG)

    # ── Determine map extent ──────────────────────────────────────────────
    valid_reports = [r for r in reports if getattr(r, "lat", None) and getattr(r, "lon", None)]

    if valid_reports:
        lats = [r.lat for r in valid_reports]
        lons = [r.lon for r in valid_reports]
        pad_lat = max(1.5, (max(lats) - min(lats)) * 0.25)
        pad_lon = max(2.5, (max(lons) - min(lons)) * 0.25)
        lat_min, lat_max = min(lats) - pad_lat, max(lats) + pad_lat
        lon_min, lon_max = min(lons) - pad_lon, max(lons) + pad_lon
        # Clamp to CONUS
        lat_min = max(24.0, lat_min)
        lat_max = min(50.0, lat_max)
        lon_min = max(-125.0, lon_min)
        lon_max = min(-65.0, lon_max)
    else:
        lat_min, lat_max = 24.0, 50.0
        lon_min, lon_max = -125.0, -65.0

    ax_map.set_xlim(lon_min, lon_max)
    ax_map.set_ylim(lat_min, lat_max)
    ax_map.set_aspect("equal")

    # ── State boundary lines (approximate from center positions) ─────────
    # Draw subtle state label text
    for state, (slat, slon) in STATE_CENTERS.items():
        if lon_min <= slon <= lon_max and lat_min <= slat <= lat_max:
            ax_map.text(
                slon, slat, state,
                color="#2a3050", fontsize=6, ha="center", va="center",
                fontweight="bold", alpha=0.8, zorder=1,
            )

    # Draw subtle gridlines
    ax_map.grid(True, color="#1e2235", linewidth=0.4, linestyle="--", alpha=0.6, zorder=0)

    # ── Plot report markers ───────────────────────────────────────────────
    plotted_types: set[str] = set()
    # Sort so tornadoes render on top
    def sort_key(r):
        rtype = _normalize_type(getattr(r, "report_type", ""))
        priority = {"TORNADO": 0, "FUNNEL CLOUD": 1, "HAIL": 2}.get(rtype, 3)
        return priority

    sorted_reports = sorted(valid_reports, key=sort_key, reverse=True)

    for r in sorted_reports:
        rtype = _normalize_type(getattr(r, "report_type", ""))
        color = TYPE_COLORS.get(rtype, "#888888")
        marker = TYPE_MARKERS.get(rtype, "o")
        is_tornado = rtype == "TORNADO"
        size = 120 if is_tornado else 50
        edge_w = 1.5 if is_tornado else 0.8

        ax_map.scatter(
            r.lon, r.lat,
            c=color, s=size, marker=marker,
            edgecolors="white" if is_tornado else color,
            linewidths=edge_w,
            zorder=5 if is_tornado else 4,
            alpha=0.92,
        )
        plotted_types.add(rtype)

    # ── Map title / subtitle ──────────────────────────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y %H:%M UTC")
    ax_map.set_title(
        title,
        color=TEXT_PRIMARY, fontsize=14, fontweight="bold", pad=8, loc="left",
    )
    ax_map.set_xlabel(
        f"Last {hours} hours  ·  {now_str}  ·  Source: NWS Local Storm Reports",
        color=TEXT_SECONDARY, fontsize=7, labelpad=4,
    )
    for spine in ax_map.spines.values():
        spine.set_edgecolor(BORDER_COLOR)
    ax_map.tick_params(colors=TEXT_SECONDARY, labelsize=6)

    # ── Stats panel ───────────────────────────────────────────────────────
    ax_stats.set_xlim(0, 1)
    ax_stats.set_ylim(0, 1)
    ax_stats.axis("off")

    # Count by type
    type_counts: dict[str, int] = {}
    max_hail: Optional[float] = None
    max_wind: Optional[float] = None
    tornado_count = 0

    for r in reports:
        rtype = _normalize_type(getattr(r, "report_type", ""))
        type_counts[rtype] = type_counts.get(rtype, 0) + 1
        if rtype == "TORNADO":
            tornado_count += 1
        if "hail" in rtype.lower():
            mag = getattr(r, "magnitude", None)
            if mag:
                try:
                    v = float(str(mag).replace('"', "").replace("IN", "").strip())
                    if max_hail is None or v > max_hail:
                        max_hail = v
                except (ValueError, TypeError):
                    pass
        if "wind" in rtype.lower():
            mag = getattr(r, "magnitude", None)
            if mag:
                try:
                    v = float(str(mag).replace("MPH", "").replace("KTS", "").strip().split()[0])
                    if max_wind is None or v > max_wind:
                        max_wind = v
                except (ValueError, TypeError):
                    pass

    y = 0.94
    ax_stats.text(0.05, y, "REPORT TOTALS", color=TEXT_SECONDARY, fontsize=7,
                  fontweight="bold", transform=ax_stats.transAxes)
    y -= 0.04

    # Total
    ax_stats.text(0.05, y, f"Total Reports", color=TEXT_SECONDARY, fontsize=8,
                  transform=ax_stats.transAxes)
    ax_stats.text(0.95, y, str(len(reports)), color=TEXT_PRIMARY, fontsize=11,
                  fontweight="bold", ha="right", transform=ax_stats.transAxes)
    y -= 0.035

    # Sorted type counts
    sorted_types = sorted(type_counts.items(), key=lambda x: -x[1])
    for rtype, count in sorted_types[:12]:
        color = TYPE_COLORS.get(rtype, "#888888")
        ax_stats.plot([0.04, 0.055], [y, y], color=color, linewidth=4,
                      transform=ax_stats.transAxes, solid_capstyle="round")
        label = rtype.title() if len(rtype) > 12 else rtype
        ax_stats.text(0.07, y, label, color=TEXT_SECONDARY, fontsize=7.5,
                      transform=ax_stats.transAxes, va="center")
        ax_stats.text(0.95, y, str(count), color=color, fontsize=9,
                      fontweight="bold", ha="right", transform=ax_stats.transAxes, va="center")
        y -= 0.05

    y -= 0.02
    # Peak values
    if max_hail is not None:
        ax_stats.text(0.05, y, "Max Hail", color=TEXT_SECONDARY, fontsize=7.5,
                      transform=ax_stats.transAxes)
        ax_stats.text(0.95, y, f'{max_hail:.2f}"', color="#22dd44", fontsize=10,
                      fontweight="bold", ha="right", transform=ax_stats.transAxes)
        y -= 0.05
    if max_wind is not None:
        ax_stats.text(0.05, y, "Max Wind", color=TEXT_SECONDARY, fontsize=7.5,
                      transform=ax_stats.transAxes)
        ax_stats.text(0.95, y, f"{max_wind:.0f} mph", color="#4488ff", fontsize=10,
                      fontweight="bold", ha="right", transform=ax_stats.transAxes)
        y -= 0.05

    # Divider
    ax_stats.axhline(y - 0.01, color=BORDER_COLOR, linewidth=0.8,
                     transform=ax_stats.transAxes, xmin=0.04, xmax=0.96)
    y -= 0.04

    # Brand watermark
    ax_stats.text(0.5, 0.02, brand_name, color=TEXT_SECONDARY, fontsize=7,
                  ha="center", transform=ax_stats.transAxes, alpha=0.5)

    # ── Render to bytes ───────────────────────────────────────────────────
    buf = io.BytesIO()
    plt.tight_layout(pad=0.4)
    fig.savefig(buf, format="png", dpi=dpi, facecolor=DARK_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
