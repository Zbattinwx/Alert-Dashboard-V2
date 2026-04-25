#!/usr/bin/env python3
"""
NEXRAD Diagnostic Tool
======================
Downloads the latest volume scans for one or more sites, runs them through
the storm-tracking pipeline, and prints a detailed QC report so you can
verify that every detection, motion vector, and dual-pol signature is
grounded in what the raw radar actually shows.

Usage:
    python scripts/radar_diagnostic.py                    # default: KILN + KIND
    python scripts/radar_diagnostic.py KILN               # single site
    python scripts/radar_diagnostic.py KILN KIND KIWX     # custom list

Output is written to stdout and also saved to data/diagnostic_<timestamp>.txt
"""

import asyncio
import math
import sys
import tempfile
import os
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pyart
import nexradaws

from backend.services.nexrad_sites import NEXRAD_SITES

# ─── Config ───────────────────────────────────────────────────────────────────
DEFAULT_SITES = ["KILN", "KIND"]
GATE_DBZ = 10.0          # same as service default
CELL_DETECT_DBZ = 35.0
MESO_VELOCITY_THRESHOLD_MS = 15.0
MESO_MAX_DIAMETER_KM = 10.0
DEBRIS_CC_THRESHOLD = 0.80
TDS_MIN_ROTATION_MS = 20.0    # Requires strong rotation for TDS (not just weak meso)
TDS_MIN_REFL_DBZ = 20.0       # NWS: Z >= 20 dBZ required in low-CC region
TDS_MAX_BEAM_HEIGHT_KM = 1.5
R_E = 6_371_000.0
K_REFR = 4.0 / 3.0

# ─── Helpers ──────────────────────────────────────────────────────────────────

def beam_height_km(range_m: float, elev_deg: float) -> float:
    er = math.radians(elev_deg)
    return (range_m * math.sin(er) + range_m ** 2 / (2 * K_REFR * R_E)) / 1000.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def latlon_to_polar(ref_lat, ref_lon, lat, lon):
    lat1 = math.radians(ref_lat); lat2 = math.radians(lat)
    dlat = lat2 - lat1; dlon = math.radians(lon - ref_lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    dist_km = 2 * 6371.0 * math.asin(math.sqrt(a))
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return dist_km, bearing


def sep(char="-", width=80):
    return char * width


# ─── Download one site ────────────────────────────────────────────────────────

def download_latest(site: str) -> tuple[str, object] | tuple[None, None]:
    """Download and parse the latest standard volume scan for *site*.
    Returns (local_filepath, pyart_radar) or (None, None) on failure."""
    conn = nexradaws.NexradAwsInterface()
    now = datetime.now(timezone.utc)
    try:
        scans = conn.get_avail_scans(now.year, now.month, now.day, site)
    except Exception as e:
        print(f"  [ERROR] Could not list scans for {site}: {e}")
        return None, None

    def scan_key(s):
        return getattr(s, "filename", None) or getattr(s, "key", None) or str(s)

    volume_scans = [
        s for s in scans
        if "_MDM" not in scan_key(s) and "_THR" not in scan_key(s) and "_NFL" not in scan_key(s)
    ]
    if not volume_scans:
        print(f"  [WARN] No standard volume scans found for {site}")
        return None, None

    latest = volume_scans[-1]
    print(f"  Downloading {scan_key(latest)} ...")
    try:
        results = conn.download(latest, tempfile.gettempdir())
    except Exception as e:
        print(f"  [ERROR] Download failed: {e}")
        return None, None

    if not results.success:
        print(f"  [ERROR] Download incomplete: {results.failed}")
        return None, None

    local_file = results.success[0].filepath
    try:
        radar = pyart.io.read_nexrad_archive(str(local_file), linear_interp=False)
    except Exception as e:
        print(f"  [ERROR] Py-ART parse failed: {e}")
        try:
            os.remove(local_file)
        except OSError:
            pass
        return None, None

    return local_file, radar


# ─── Grid helper ──────────────────────────────────────────────────────────────

def make_grid(radar, site_lat, site_lon, max_range_km=230, res_km=1.0):
    range_m = max_range_km * 1000
    res_m = res_km * 1000
    grid_n = int(2 * range_m / res_m)
    try:
        grid = pyart.map.grid_from_radars(
            (radar,),
            grid_shape=(1, grid_n, grid_n),
            grid_limits=((1000, 2000), (-range_m, range_m), (-range_m, range_m)),
            fields=list(radar.fields.keys()),
            weighting_function="Barnes2",
            grid_origin=(site_lat, site_lon),
        )
        return grid
    except Exception as e:
        print(f"  [ERROR] Gridding failed: {e}")
        return None


# ─── Cell identification (mirrors storm_tracking_service logic) ───────────────

def identify_cells(grid, res_km=1.0):
    from scipy import ndimage
    if "reflectivity" not in grid.fields:
        return []
    refl = np.ma.filled(grid.fields["reflectivity"]["data"][0], -999)
    binary = refl >= CELL_DETECT_DBZ
    if not np.any(binary):
        return []
    labeled, n = ndimage.label(binary)
    cells = []
    px_area = res_km ** 2
    for lid in range(1, n + 1):
        mask = labeled == lid
        area = float(np.sum(mask) * px_area)
        if area < 5.0:
            continue
        ys, xs = np.where(mask)
        cy, cx = int(np.mean(ys)), int(np.mean(xs))
        cells.append({
            "cy": cy, "cx": cx,
            "max_dbz": float(np.nanmax(refl[mask])),
            "area_km2": area,
            "mask": mask,
            "bbox": (int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())),
        })
    return cells


# ─── Per-cell diagnostic ──────────────────────────────────────────────────────

def diagnose_cell(cell, grid, radar, site_lat, site_lon, site_id, res_km=1.0, max_range_km=230):
    range_m = max_range_km * 1000
    grid_n = grid.fields["reflectivity"]["data"][0].shape[0]

    # ── Position ──────────────────────────────────────────────────────────────
    # Convert grid index to AEQD metres, then to lat/lon using the image-overlay
    # linear mapping (same as storm_tracking_service._grid_to_image_latlon)
    res_m = res_km * 1000
    x_m = -range_m + cell["cx"] * res_m
    y_m = -range_m + cell["cy"] * res_m

    try:
        from pyproj import Proj
        p = Proj(proj="aeqd", lat_0=site_lat, lon_0=site_lon, datum="WGS84", units="m")
        cell_lon, cell_lat = p(x_m, y_m, inverse=True)
    except Exception:
        cell_lat = site_lat + y_m / (111_000)
        cell_lon = site_lon + x_m / (111_000 * math.cos(math.radians(site_lat)))

    dist_km, bearing = latlon_to_polar(site_lat, site_lon, cell_lat, cell_lon)

    # ── Beam height at cell range ─────────────────────────────────────────────
    fixed_angles = [float(a) for a in radar.fixed_angle["data"]]
    min_elev = min(fixed_angles)
    bh_km = beam_height_km(dist_km * 1000, min_elev)

    # ── Velocity analysis ─────────────────────────────────────────────────────
    vel_field = "velocity_dealiased" if "velocity_dealiased" in grid.fields else (
        "velocity" if "velocity" in grid.fields else None
    )
    vel_info = {}
    rotation_verdict = "NO_VELOCITY_DATA"
    rot_vel_for_tds = 0.0
    if vel_field:
        vel_data = np.ma.filled(grid.fields[vel_field]["data"][0], np.nan)
        buf = max(int(5.0 / res_km), 3)
        y0 = max(0, cell["cy"] - buf); y1 = min(vel_data.shape[0], cell["cy"] + buf)
        x0 = max(0, cell["cx"] - buf); x1 = min(vel_data.shape[1], cell["cx"] + buf)
        region = vel_data[y0:y1, x0:x1]
        valid_raw = region[~np.isnan(region)]
        if len(valid_raw) >= 10:
            # Raw couplet (before outlier rejection)
            vmax_raw = float(np.nanmax(valid_raw))
            vmin_raw = float(np.nanmin(valid_raw))
            rot_raw = (vmax_raw - vmin_raw) / 2.0

            # Outlier-rejected couplet (Tukey IQR)
            q1, q3 = np.percentile(valid_raw, 25), np.percentile(valid_raw, 75)
            iqr = q3 - q1
            if iqr > 0:
                lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                valid = valid_raw[(valid_raw >= lo) & (valid_raw <= hi)]
            else:
                valid = valid_raw
            if len(valid) < 5:
                rotation_verdict = "TOO_FEW_GATES_AFTER_OUTLIER_REJECTION"
            else:
                vmax = float(np.nanmax(valid))
                vmin = float(np.nanmin(valid))
                rot_vel = (vmax - vmin) / 2.0
                rot_vel_for_tds = rot_vel

                # Couplet diameter (using outlier-cleaned region)
                clean_region = region.copy()
                if iqr > 0:
                    clean_region[(clean_region < lo) | (clean_region > hi)] = np.nan
                if np.any(~np.isnan(clean_region)):
                    outbound_pos = np.unravel_index(np.nanargmax(clean_region), clean_region.shape)
                    inbound_pos = np.unravel_index(np.nanargmin(clean_region), clean_region.shape)
                    couplet_km = math.sqrt(
                        (outbound_pos[0] - inbound_pos[0]) ** 2 +
                        (outbound_pos[1] - inbound_pos[1]) ** 2
                    ) * res_km
                else:
                    couplet_km = 0.0

                outlier_note = (
                    f" [RAW: {rot_raw:.1f} m/s, {len(valid_raw)-len(valid)} outlier gates removed]"
                    if abs(rot_vel - rot_raw) > 1.0 else ""
                )
                vel_info = {
                    "field": vel_field,
                    "region_min_ms": round(vmin_raw, 1),
                    "region_max_ms": round(vmax_raw, 1),
                    "cleaned_min_ms": round(vmin, 1),
                    "cleaned_max_ms": round(vmax, 1),
                    "rotational_velocity_ms": round(rot_vel, 1),
                    "rotational_velocity_raw_ms": round(rot_raw, 1),
                    "couplet_diameter_km": round(couplet_km, 1),
                    "n_valid_gates": int(len(valid)),
                    "n_outliers_removed": int(len(valid_raw) - len(valid)),
                }
                if rot_vel >= MESO_VELOCITY_THRESHOLD_MS and couplet_km <= MESO_MAX_DIAMETER_KM:
                    rotation_verdict = f"ROTATION_DETECTED ({rot_vel:.1f} m/s, diam {couplet_km:.1f} km){outlier_note}"
                elif rot_vel >= MESO_VELOCITY_THRESHOLD_MS:
                    rotation_verdict = f"COUPLET_TOO_WIDE ({rot_vel:.1f} m/s, diam={couplet_km:.1f} km){outlier_note}"
                else:
                    rotation_verdict = f"NO_ROTATION ({rot_vel:.1f} m/s < {MESO_VELOCITY_THRESHOLD_MS} m/s){outlier_note}"
        else:
            rotation_verdict = f"TOO_FEW_VEL_GATES ({len(valid_raw)})"

    # CC / ZDR / TDS analysis (NWS criteria: CC<0.80 + strong rotation + Z>=20 dBZ)
    cc_info = {}
    zdr_info = {}
    tds_verdict = "NO_CC_DATA"
    tds_buf = max(int(5.0 / res_km), 3)
    if "cross_correlation_ratio" in grid.fields:
        cc_data = np.ma.filled(grid.fields["cross_correlation_ratio"]["data"][0], np.nan)
        ty0 = max(0, cell["cy"] - tds_buf); ty1 = min(cc_data.shape[0], cell["cy"] + tds_buf)
        tx0 = max(0, cell["cx"] - tds_buf); tx1 = min(cc_data.shape[1], cell["cx"] + tds_buf)
        rot_cc_valid = cc_data[ty0:ty1, tx0:tx1]
        rot_cc_valid = rot_cc_valid[~np.isnan(rot_cc_valid)]
        if "differential_reflectivity" in grid.fields:
            zdr_data = np.ma.filled(grid.fields["differential_reflectivity"]["data"][0], np.nan)
            rot_zdr = zdr_data[ty0:ty1, tx0:tx1][~np.isnan(zdr_data[ty0:ty1, tx0:tx1])]
            if len(rot_zdr) > 0:
                zdr_info = {"mean_zdr": round(float(np.nanmean(rot_zdr)), 2),
                            "min_zdr": round(float(np.nanmin(rot_zdr)), 2)}
        refl_g = np.ma.filled(grid.fields["reflectivity"]["data"][0], np.nan)
        rot_refl = refl_g[ty0:ty1, tx0:tx1][~np.isnan(refl_g[ty0:ty1, tx0:tx1])]
        max_refl_region = float(np.nanmax(rot_refl)) if len(rot_refl) > 0 else 0.0
        if len(rot_cc_valid) > 0:
            min_cc = float(np.nanmin(rot_cc_valid))
            mean_cc = float(np.nanmean(rot_cc_valid))
            cc_info = {"min_cc": round(min_cc, 3), "mean_cc": round(mean_cc, 3),
                       "n_cc_gates": int(len(rot_cc_valid))}
            if min_cc >= DEBRIS_CC_THRESHOLD:
                tds_verdict = f"NO_TDS (min_cc={min_cc:.3f} >= {DEBRIS_CC_THRESHOLD})"
            elif rot_vel_for_tds < TDS_MIN_ROTATION_MS:
                if rot_vel_for_tds < MESO_VELOCITY_THRESHOLD_MS:
                    tds_verdict = (f"NO_TDS: LOW_CC_NO_ROTATION (min_cc={min_cc:.3f}, "
                                   f"rot={rot_vel_for_tds:.1f} m/s) -> likely HAIL not debris")
                else:
                    tds_verdict = (f"NO_TDS: ROTATION_TOO_WEAK (min_cc={min_cc:.3f}, "
                                   f"rot={rot_vel_for_tds:.1f} m/s < {TDS_MIN_ROTATION_MS} m/s needed)")
            elif max_refl_region < TDS_MIN_REFL_DBZ:
                tds_verdict = f"NO_TDS: LOW_REFLECTIVITY ({max_refl_region:.0f} dBZ < {TDS_MIN_REFL_DBZ})"
            elif bh_km > TDS_MAX_BEAM_HEIGHT_KM:
                tds_verdict = (f"NO_TDS: BEAM_TOO_HIGH (min_cc={min_cc:.3f}, "
                               f"rot={rot_vel_for_tds:.1f} m/s, beam={bh_km:.2f} km AGL)")
            else:
                zdr_note = f", ZDR={zdr_info.get('mean_zdr','?')} dB" if zdr_info else ""
                tds_verdict = (f"CONFIRMED_TDS (min_cc={min_cc:.3f}, "
                               f"rot={rot_vel_for_tds:.1f} m/s, beam={bh_km:.2f} km AGL{zdr_note})")

    # ── Raw reflectivity stats ────────────────────────────────────────────────
    refl_data = np.ma.filled(grid.fields["reflectivity"]["data"][0], np.nan)
    cell_refl = refl_data[cell["mask"]]
    cell_refl_valid = cell_refl[~np.isnan(cell_refl)]

    return {
        "cell_lat": round(cell_lat, 4),
        "cell_lon": round(cell_lon, 4),
        "dist_from_site_km": round(dist_km, 1),
        "bearing_deg": round(bearing, 1),
        "beam_height_km_agl": round(bh_km, 2),
        "min_elev_deg": round(min_elev, 2),
        "max_dbz": round(cell["max_dbz"], 1),
        "mean_dbz": round(float(np.nanmean(cell_refl_valid)), 1) if len(cell_refl_valid) > 0 else None,
        "area_km2": round(cell["area_km2"], 1),
        "velocity": vel_info,
        "rotation_verdict": rotation_verdict,
        "cc": cc_info,
        "zdr": zdr_info,
        "tds_verdict": tds_verdict,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(sites: list[str]):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path("data") / f"diagnostic_{timestamp}.txt"
    out_path.parent.mkdir(exist_ok=True)

    lines = []

    def p(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        print(text, **kwargs)
        lines.append(text)

    p(sep("═"))
    p(f"NEXRAD STORM TRACKER DIAGNOSTIC REPORT")
    p(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    p(f"Sites:     {', '.join(sites)}")
    p(sep("═"))

    site_data = {}

    # ── Download & parse ──────────────────────────────────────────────────────
    for site in sites:
        p(f"\n{'─'*40}")
        p(f"SITE: {site}")
        p(f"{'─'*40}")
        site_info = NEXRAD_SITES.get(site, {})
        site_lat = site_info.get("lat")
        site_lon = site_info.get("lon")
        if not site_lat:
            p(f"  [ERROR] Unknown site {site}")
            continue
        p(f"  Location: {site_lat:.3f}°N, {site_lon:.3f}°E")

        local_file, radar = download_latest(site)
        if radar is None:
            continue

        try:
            # Scan metadata
            scan_time_str = radar.time["units"].split("since ")[-1].replace("Z", "+00:00")
            scan_time = datetime.fromisoformat(scan_time_str)
        except Exception:
            scan_time = datetime.now(timezone.utc)

        fixed = sorted([float(a) for a in radar.fixed_angle["data"]])
        p(f"  Scan time:      {scan_time.isoformat()}")
        p(f"  Tilts:          {len(fixed)} sweeps, lowest={fixed[0]:.1f}°, highest={fixed[-1]:.1f}°")
        p(f"  Fields:         {list(radar.fields.keys())}")
        p(f"  Max range:      {radar.range['data'][-1]/1000:.0f} km")

        # Dealias velocity
        if "velocity" in radar.fields:
            try:
                gf = pyart.filters.GateFilter(radar)
                gf.exclude_below("reflectivity", -20)
                corrected = pyart.correct.dealias_region_based(radar, gatefilter=gf,
                    skip_between_rays=True, skip_along_ray=True)
                radar.add_field("velocity_dealiased", corrected, replace_existing=True)
                p(f"  Velocity:       dealiased OK")
            except Exception as e:
                p(f"  Velocity:       dealiasing failed ({e})")

        p(f"\n  Gridding (Barnes2, 1 km, 230 km range)...")
        grid = make_grid(radar, site_lat, site_lon)
        if grid is None:
            continue

        cells = identify_cells(grid)
        p(f"  Cells ≥35 dBZ:  {len(cells)}")

        site_data[site] = {
            "radar": radar,
            "grid": grid,
            "cells": cells,
            "site_lat": site_lat,
            "site_lon": site_lon,
            "scan_time": scan_time,
            "fixed_angles": fixed,
            "local_file": local_file,
        }

    # ── Per-cell diagnostic ───────────────────────────────────────────────────
    for site, sd in site_data.items():
        p(f"\n{sep('═')}")
        p(f"CELL ANALYSIS — {site}  ({sd['scan_time'].isoformat()})")
        p(sep("═"))

        radar = sd["radar"]
        grid = sd["grid"]
        cells = sd["cells"]
        site_lat = sd["site_lat"]
        site_lon = sd["site_lon"]

        if not cells:
            p("  No cells ≥35 dBZ detected.")
            continue

        # Sort by max reflectivity
        cells_sorted = sorted(cells, key=lambda c: c["max_dbz"], reverse=True)
        p(f"  {len(cells_sorted)} cells found (showing top 15 by reflectivity)\n")

        for i, cell in enumerate(cells_sorted[:15]):
            d = diagnose_cell(cell, grid, radar, site_lat, site_lon, site)
            p(f"  {'─'*70}")
            p(f"  Cell #{i+1}   {d['max_dbz']} dBZ peak,  {d['area_km2']} km²")
            p(f"  Position:     {d['cell_lat']}°N, {d['cell_lon']}°E")
            p(f"  From {site}: {d['dist_from_site_km']} km at {d['bearing_deg']}°")
            p(f"  Beam height:  {d['beam_height_km_agl']} km AGL  (lowest tilt={d['min_elev_deg']}°)")

            # Is this cell in this site's Voronoi territory?
            voronoi_notes = []
            for other_site, osd in site_data.items():
                if other_site == site:
                    continue
                dist_other = haversine_km(osd["site_lat"], osd["site_lon"], d["cell_lat"], d["cell_lon"])
                dist_self = d["dist_from_site_km"]
                closer = "CLOSER" if dist_other < dist_self else "farther"
                voronoi_notes.append(f"{other_site} is {closer} ({dist_other:.0f} km)")
            if voronoi_notes:
                p(f"  Voronoi:      " + "; ".join(voronoi_notes))
                if any("CLOSER" in n for n in voronoi_notes):
                    p(f"             ⚠ Another radar is closer — analysis should come from that site")

            p(f"  Reflectivity: peak={d['max_dbz']} dBZ, mean={d['mean_dbz']} dBZ")

            # Velocity / rotation
            v = d["velocity"]
            if v:
                outlier_note = (f"  [{v.get('n_outliers_removed',0)} outlier gates removed, "
                                f"cleaned: {v.get('cleaned_min_ms','?')} to {v.get('cleaned_max_ms','?')} m/s]"
                                if v.get("n_outliers_removed", 0) > 0 else "")
                p(f"  Velocity:     raw min={v['region_min_ms']} m/s, raw max={v['region_max_ms']} m/s  "
                  f"({v['n_valid_gates']} gates after outlier rejection){outlier_note}")
                p(f"  Rotation:     rot_vel={v['rotational_velocity_ms']} m/s "
                  f"(raw={v.get('rotational_velocity_raw_ms','?')} m/s), "
                  f"couplet_diam={v['couplet_diameter_km']} km")
            p(f"  -> ROTATION:  {d['rotation_verdict']}")

            # CC / ZDR / TDS
            cc = d["cc"]
            zdr = d.get("zdr", {})
            if cc:
                p(f"  CC:           min={cc['min_cc']}, mean={cc['mean_cc']}  ({cc['n_cc_gates']} gates)")
            if zdr:
                p(f"  ZDR:          mean={zdr.get('mean_zdr','?')} dB, min={zdr.get('min_zdr','?')} dB"
                  f"  (near 0 dB = tumbling debris; high = rain/hail)")
            p(f"  -> TDS:       {d['tds_verdict']}")

        p("")

    # ── Cross-site summary ────────────────────────────────────────────────────
    if len(site_data) > 1:
        p(f"\n{sep('═')}")
        p("CROSS-SITE OVERLAP ANALYSIS")
        p(sep("═"))
        site_list = list(site_data.items())
        for i, (sa, sda) in enumerate(site_list):
            for sb, sdb in site_list[i+1:]:
                dist = haversine_km(sda["site_lat"], sda["site_lon"],
                                    sdb["site_lat"], sdb["site_lon"])
                voronoi_km = dist / 2.0
                p(f"\n  {sa} ↔ {sb}: {dist:.0f} km apart, Voronoi boundary at ~{voronoi_km:.0f} km from each")

                # Which cells are in the overlap zone (within 20 km of boundary = within 20 km of dist/2 from each site)?
                for site_id, sd in [(sa, sda), (sb, sdb)]:
                    other_id = sb if site_id == sa else sa
                    other_sd = sdb if site_id == sa else sda
                    overlap_cells = []
                    for cell in sd["cells"]:
                        # Get cell lat/lon
                        res_m = 1000.0
                        range_m = 230_000.0
                        x_m = -range_m + cell["cx"] * res_m
                        y_m = -range_m + cell["cy"] * res_m
                        try:
                            from pyproj import Proj
                            pp = Proj(proj="aeqd", lat_0=sd["site_lat"], lon_0=sd["site_lon"], datum="WGS84", units="m")
                            clon, clat = pp(x_m, y_m, inverse=True)
                        except Exception:
                            clat = sd["site_lat"] + y_m / 111_000
                            clon = sd["site_lon"] + x_m / (111_000 * math.cos(math.radians(sd["site_lat"])))
                        d_self = haversine_km(sd["site_lat"], sd["site_lon"], clat, clon)
                        d_other = haversine_km(other_sd["site_lat"], other_sd["site_lon"], clat, clon)
                        # "Overlap zone" = within 30 km of the Voronoi boundary
                        if abs(d_self - d_other) < 30:
                            overlap_cells.append((clat, clon, cell["max_dbz"], d_self, d_other))
                    if overlap_cells:
                        p(f"  {site_id} cells near Voronoi boundary ({len(overlap_cells)}):")
                        for (clat, clon, mdbz, ds, do) in overlap_cells[:5]:
                            closer = site_id if ds < do else other_id
                            p(f"    ({clat:.3f}, {clon:.3f}) {mdbz:.0f} dBZ — "
                              f"{ds:.0f} km from {site_id}, {do:.0f} km from {other_id} → {closer} owns it")

    # ── Motion vector sanity check ────────────────────────────────────────────
    p(f"\n{sep('═')}")
    p("MOTION VECTOR SANITY CHECK")
    p(sep("═"))
    p("  (This section shows what speeds would be computed if a cell matched")
    p("   between scans at various time intervals and position offsets)")
    p("")
    for dt_s in [30, 60, 180, 300]:
        for dist_km in [1.0, 3.0, 5.0, 10.0, 20.0]:
            speed = dist_km / (max(dt_s, 36) / 3600)
            flag = " ← PHYSICALLY IMPOSSIBLE" if speed > 175 else (" ← SUSPECT" if speed > 100 else "")
            if speed > 100:
                p(f"  dt={dt_s:3d}s, δ={dist_km:4.1f}km → {speed:6.0f} kph{flag}")
    p("")
    p("  Root cause: with 2 active sites processing seconds apart, the same storm")
    p("  can match at dt~30-60s with parallax offset of 3-5 km → 180-600 kph.")
    p("  Fix required: minimum inter-scan interval for motion updates + hard speed cap.")

    p(f"\n{sep('═')}")
    p(f"Report saved to: {out_path}")
    p(sep("═"))

    # Save to file
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # Cleanup temp files
    for sd in site_data.values():
        try:
            os.remove(sd["local_file"])
        except OSError:
            pass


if __name__ == "__main__":
    sites = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SITES
    sites = [s.upper() for s in sites]
    main(sites)
