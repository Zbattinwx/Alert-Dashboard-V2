"""HRRR gridded field layers for the radar app's "Model" overlays.

Mirrors the MRMS pattern: produce a compact uint8 lat/lon grid (same binary wire
format, magic 'HRRR') that the app's WebGL custom layer renders directly.

Storage-light by design — nothing is pre-downloaded. A (run, field, forecast
hour) is fetched ON DEMAND:
  1. read the GRIB2 `.idx` for the file, find the field's byte range,
  2. byte-range GET just that message (~0.5–1 MB) from AWS `noaa-hrrr-bdp-pds`,
  3. decode with eccodes,
  4. regrid HRRR's native Lambert-Conformal grid to a regular lat/lon raster
     using a STATIC mapping computed once (the HRRR grid never changes), so each
     fetch is a fast array gather — no reinterpolation,
  5. pack the uint8 binary, cache it (in-memory LRU).

Requires `eccodes` + `scipy` (both already bundled for MRMS/soundings).
"""

from __future__ import annotations

import logging
import os
import struct
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from .grib_lock import GRIB_DECODE_LOCK as _DECODE_LOCK

logger = logging.getLogger(__name__)

HRRR_BUCKET = "noaa-hrrr-bdp-pds"
BINARY_MAGIC = b"HRRR"
CACHE_MAX = 130  # LRU entries (~2 MB each → ~260 MB cap); fits a full GFS F0–120 loop
RUNS_CACHE_TTL_S = 60   # list_runs makes a burst of HEADs — reuse briefly
IDX_CACHE_MAX = 200     # .idx files are tiny (~KB); bounded LRU
IDX_CACHE_TTL_S = 300   # the newest hour's .idx can grow while NOMADS uploads —
                        # a short uniform TTL is the simple robust choice (one
                        # tiny re-read per 5 min per file is negligible)

# ── Field registry (extend by adding entries) ───────────────────────────────
# Each entry needs either an `idx` (one GRIB record, matched as a substring of
# the .idx line) or a `derive` tuple (multiple records combined):
#   ("mag",  u_idx, v_idx)                  → vector magnitude √(u²+v²)
#   ("ptype", rain, snow, icep, frzr)       → categorical precip type 1/2/3/4
# file: "sfc" (wrfsfc, default) | "prs" (wrfprs, pressure levels).
# conv: None | k2f | k2c | ms2kt | m2in | mm2in | x1e5  (see _conv).
# nodata_below: pack 0 (transparent) for values under this threshold.
# group: sidebar grouping + ordering.
HRRR_FIELDS: dict[str, dict] = {
    # ── Surface ──
    "t2m":  {"idx": ":TMP:2 m above ground:", "label": "2 m Temperature",        "conv": "k2f", "vmin": -30.0, "vmax": 120.0, "units": "°F",  "lut": "hrrr_temp",     "group": "Surface"},
    "td2m": {"idx": ":DPT:2 m above ground:", "label": "2 m Dew Point",          "conv": "k2f", "vmin": -30.0, "vmax": 90.0,  "units": "°F",  "lut": "hrrr_dewpoint", "group": "Surface"},
    "refc": {"idx": ":REFC:entire atmosphere", "label": "Composite Reflectivity","conv": None, "vmin": -20.0, "vmax": 80.0,  "units": "dBZ", "lut": "reflectivity",  "group": "Surface", "nodata_below": 5.0},

    # ── Severe ──
    "sbcape":  {"idx": ":CAPE:surface:",                 "label": "Surface CAPE",   "conv": None, "vmin": 0.0, "vmax": 8000.0, "units": "J/kg", "lut": "cape",  "group": "Severe", "nodata_below": 100.0},
    "mlcape":  {"idx": ":CAPE:90-0 mb above ground:",    "label": "ML CAPE",        "conv": None, "vmin": 0.0, "vmax": 8000.0, "units": "J/kg", "lut": "cape",  "group": "Severe", "nodata_below": 100.0},
    "mucape":  {"idx": ":CAPE:255-0 mb above ground:",   "label": "MU CAPE",        "conv": None, "vmin": 0.0, "vmax": 8000.0, "units": "J/kg", "lut": "cape",  "group": "Severe", "nodata_below": 100.0},
    "sbcin":   {"idx": ":CIN:surface:",                  "label": "Surface CIN",    "conv": None, "vmin": -400.0, "vmax": 0.0, "units": "J/kg", "lut": "cin",   "group": "Severe"},
    "srh01":   {"idx": ":HLCY:1000-0 m above ground:",   "label": "0–1 km SRH",     "conv": None, "vmin": 0.0, "vmax": 800.0,  "units": "m²/s²", "lut": "srh",  "group": "Severe", "nodata_below": 50.0},
    "srh03":   {"idx": ":HLCY:3000-0 m above ground:",   "label": "0–3 km SRH",     "conv": None, "vmin": 0.0, "vmax": 900.0,  "units": "m²/s²", "lut": "srh",  "group": "Severe", "nodata_below": 50.0},
    "shear06": {"derive": ("mag", ":VUCSH:0-6000 m above ground:", ":VVCSH:0-6000 m above ground:"), "label": "0–6 km Bulk Shear", "conv": "ms2kt", "vmin": 0.0, "vmax": 100.0, "units": "kt", "lut": "shear", "group": "Severe"},
    "pwat":    {"idx": ":PWAT:entire atmosphere",        "label": "Precipitable Water", "conv": "mm2in", "vmin": 0.0, "vmax": 2.5, "units": "in", "lut": "pwat", "group": "Severe"},
    "lftx4":   {"idx": ":4LFTX:180-0 mb above ground:",  "label": "Best Lifted Index", "conv": None, "vmin": -12.0, "vmax": 12.0, "units": "°C", "lut": "lftx", "group": "Severe"},

    # ── Composite parameters (derived from CAPE / SRH / shear / LCL) ──
    "ehi01": {"derive": ("calc", "ehi", [(":CAPE:90-0 mb above ground:", None), (":HLCY:1000-0 m above ground:", None)]), "label": "Energy Helicity Index 0–1 km", "conv": None, "vmin": 0.0, "vmax": 8.0, "units": "", "lut": "composite", "group": "Composite", "nodata_below": 0.5},
    "ehi03": {"derive": ("calc", "ehi", [(":CAPE:90-0 mb above ground:", None), (":HLCY:3000-0 m above ground:", None)]), "label": "Energy Helicity Index 0–3 km", "conv": None, "vmin": 0.0, "vmax": 8.0, "units": "", "lut": "composite", "group": "Composite", "nodata_below": 0.5},
    "scp":   {"derive": ("calc", "scp", [(":CAPE:255-0 mb above ground:", None), (":HLCY:3000-0 m above ground:", None), (":VUCSH:0-6000 m above ground:", None), (":VVCSH:0-6000 m above ground:", None)]), "label": "Supercell Composite", "conv": None, "vmin": 0.0, "vmax": 50.0, "units": "", "lut": "composite", "group": "Composite", "nodata_below": 0.5},
    "stp":   {"derive": ("calc", "stp", [(":CAPE:surface:", None), (":CIN:surface:", None), (":TMP:2 m above ground:", None), (":DPT:2 m above ground:", None), (":HLCY:1000-0 m above ground:", None), (":VUCSH:0-6000 m above ground:", None), (":VVCSH:0-6000 m above ground:", None)]), "label": "Sig Tornado (STP)", "conv": None, "vmin": 0.0, "vmax": 10.0, "units": "", "lut": "composite", "group": "Composite", "nodata_below": 0.25},

    # ── Explicit convective (updraft helicity; max-in-window → no F00) ──
    "uh25":     {"idx": ":MXUPHL:5000-2000 m above ground:", "label": "2–5 km UH (1 h max)", "conv": None, "vmin": 0.0, "vmax": 400.0, "units": "m²/s²", "lut": "uphl", "group": "Convective", "nodata_below": 25.0, "zero_at_f0": True},
    "uh03":     {"idx": ":MXUPHL:3000-0 m above ground:",    "label": "0–3 km UH (1 h max)", "conv": None, "vmin": 0.0, "vmax": 250.0, "units": "m²/s²", "lut": "uphl", "group": "Convective", "nodata_below": 25.0, "zero_at_f0": True},
    "uh25_3h":  {"timeagg": ("max", 3),     "base": "uh25", "label": "2–5 km UH (3 h max)", "vmin": 0.0, "vmax": 600.0, "units": "m²/s²", "lut": "uphl", "group": "Convective", "nodata_below": 25.0, "zero_at_f0": True},
    "uh25_run": {"timeagg": ("max", "run"), "base": "uh25", "label": "2–5 km UH (run max)", "vmin": 0.0, "vmax": 800.0, "units": "m²/s²", "lut": "uphl", "group": "Convective", "nodata_below": 25.0, "zero_at_f0": True},
    "uh03_run": {"timeagg": ("max", "run"), "base": "uh03", "label": "0–3 km UH (run max)", "vmin": 0.0, "vmax": 400.0, "units": "m²/s²", "lut": "uphl", "group": "Convective", "nodata_below": 25.0, "zero_at_f0": True},
    "refc_uh":  {"idx": ":REFC:entire atmosphere", "label": "Reflectivity + UH>75", "conv": None, "vmin": -20.0, "vmax": 80.0, "units": "dBZ", "lut": "reflectivity", "group": "Convective", "nodata_below": 5.0, "uh_contour": "uh25"},

    # ── Upper Air (height / temp / wind / moisture) ──
    "t850":    {"idx": ":TMP:850 mb:",  "label": "850 mb Temp",  "conv": "k2c", "vmin": -30.0, "vmax": 30.0,  "units": "°C", "lut": "temp_upper", "group": "Upper Air"},
    "rh850":   {"idx": ":RH:850 mb:",   "label": "850 mb RH",    "conv": None,  "vmin": 0.0,   "vmax": 100.0, "units": "%",  "lut": "rh",         "group": "Upper Air"},
    "wspd850": {"derive": ("mag", ":UGRD:850 mb:", ":VGRD:850 mb:"), "label": "850 mb Wind", "conv": "ms2kt", "vmin": 0.0, "vmax": 80.0, "units": "kt", "lut": "wind_upper", "group": "Upper Air"},
    "t700":    {"idx": ":TMP:700 mb:",  "label": "700 mb Temp",  "conv": "k2c", "vmin": -40.0, "vmax": 20.0,  "units": "°C", "lut": "temp_upper", "group": "Upper Air"},
    "rh700":   {"idx": ":RH:700 mb:",   "label": "700 mb RH",    "conv": None,  "vmin": 0.0,   "vmax": 100.0, "units": "%",  "lut": "rh",         "group": "Upper Air"},
    "z500":    {"idx": ":HGT:500 mb:",  "label": "500 mb Height","conv": None,  "vmin": 5160.0, "vmax": 6000.0, "units": "m", "lut": "height",    "group": "Upper Air"},
    "t500":    {"idx": ":TMP:500 mb:",  "label": "500 mb Temp",  "conv": "k2c", "vmin": -45.0, "vmax": 0.0,   "units": "°C", "lut": "temp_upper", "group": "Upper Air"},
    "wspd500": {"derive": ("mag", ":UGRD:500 mb:", ":VGRD:500 mb:"), "label": "500 mb Wind", "conv": "ms2kt", "vmin": 0.0, "vmax": 120.0, "units": "kt", "lut": "wind_upper", "group": "Upper Air"},
    "z300":    {"idx": ":HGT:300 mb:",  "label": "300 mb Height","conv": None,  "vmin": 8640.0, "vmax": 9960.0, "units": "m", "lut": "height",    "group": "Upper Air"},
    "wspd250": {"derive": ("mag", ":UGRD:250 mb:", ":VGRD:250 mb:"), "label": "250 mb Wind", "conv": "ms2kt", "vmin": 0.0, "vmax": 160.0, "units": "kt", "lut": "wind_upper", "group": "Upper Air"},

    # ── Dynamics ──
    "vort500":  {"idx": ":ABSV:500 mb:", "label": "500 mb Abs Vorticity", "conv": "x1e5", "vmin": 0.0,  "vmax": 50.0, "units": "×10⁻⁵ s⁻¹", "lut": "vort",  "group": "Dynamics"},
    "omega700": {"idx": ":VVEL:700 mb:", "label": "700 mb Vertical Velocity", "conv": None, "vmin": -5.0, "vmax": 5.0, "units": "Pa/s", "lut": "omega", "group": "Dynamics"},
    "wspd300":  {"derive": ("mag", ":UGRD:300 mb:", ":VGRD:300 mb:"), "label": "300 mb Jet", "conv": "ms2kt", "vmin": 0.0, "vmax": 160.0, "units": "kt", "lut": "wind_upper", "group": "Dynamics"},

    # ── Winter ──
    "snod":  {"idx": ":SNOD:surface:",  "label": "Snow Depth",        "conv": "m2in", "vmin": 0.0, "vmax": 24.0, "units": "in", "lut": "snow", "group": "Winter", "nodata_below": 0.1},
    "asnow": {"idx": ":ASNOW:surface:", "label": "Accumulated Snow",  "conv": "m2in", "vmin": 0.0, "vmax": 18.0, "units": "in", "lut": "snow", "group": "Winter", "nodata_below": 0.1},
    "ptype": {"derive": ("ptype", ":CRAIN:surface:", ":CSNOW:surface:", ":CICEP:surface:", ":CFRZR:surface:"), "label": "Precip Type", "conv": None, "vmin": 0.0, "vmax": 4.0, "units": "", "lut": "ptype", "group": "Winter", "nodata_below": 0.5},

    # ── Smoke (HRRR-Smoke, operational since HRRRv4) ──
    # MASSDEN = lowest-model-level (~8 m AGL) smoke mass density, GRIB stores
    # kg/m³ → ×1e9 = µg/m³. COLMD = vertically integrated smoke, kg/m² → ×1e6 =
    # mg/m². Both live in the wrfsfc file (verified in the live .idx).
    # vmax 400: Canadian-wildfire events push near-surface smoke past 200 µg/m³.
    "smoke_sfc": {"idx": ":MASSDEN:8 m above ground:", "label": "Near-Surface Smoke", "conv": "x1e9", "vmin": 0.0, "vmax": 400.0, "units": "µg/m³", "lut": "smoke", "group": "Smoke", "nodata_below": 1.0},
    "smoke_col": {"idx": ":COLMD:entire atmosphere",   "label": "Vertically Integrated Smoke", "conv": "x1e6", "vmin": 0.0, "vmax": 500.0, "units": "mg/m²", "lut": "smoke", "group": "Smoke", "nodata_below": 5.0},
}

# Mark pressure-level fields (wrfprs file) — everything at an "mb" level.
for _id, _spec in HRRR_FIELDS.items():
    _m = _spec.get("idx") or (_spec.get("derive") or (None, ""))[1]
    if isinstance(_m, tuple):
        _m = _m[0]  # multi-part matcher → the var:level part carries the level
    if isinstance(_m, str) and " mb:" in _m:
        _spec["file"] = "prs"

# ── RRFS-A deterministic registry ───────────────────────────────────────────
# The deterministic RRFS (rrfs_a/rrfs.YYYYMMDD) is a full 3 km CONUS model with
# the same GRIB field names as HRRR — 2dfld (surface, like wrfsfc) + prslev
# (pressure, like wrfprs). So we derive the RRFS registry from HRRR_FIELDS,
# remapping the file token (sfc→2dfld, prs→prslev). Differences: vertical
# velocity is DZDT (m/s, up-positive) not VVEL; MSLP is MSLET (handled per-model).
def _rrfs_fields() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for k, v in HRRR_FIELDS.items():
        s = dict(v)
        s["file"] = "prslev" if s.get("file") == "prs" else "2dfld"
        out[k] = s
    out["omega700"] = {"idx": ":DZDT:700 mb:", "label": "700 mb Vertical Velocity",
                       "conv": None, "vmin": -3.0, "vmax": 3.0, "units": "m/s",
                       "lut": "omega_up", "group": "Dynamics", "file": "prslev"}
    # RRFS disambiguates aerosol species with GRIB2 attributes appended to the
    # idx line (smoke vs dust vs total share :MASSDEN:8 m above ground:), so the
    # smoke fields need multi-part matchers — every part must be in the line.
    # Verified live in rrfs.tXXz.2dfld f000/f006 idx (prslev carries NO smoke).
    out["smoke_sfc"] = {**out["smoke_sfc"], "idx": (":MASSDEN:8 m above ground:", "aerosol=Particulate organic matter dry")}
    out["smoke_col"] = {**out["smoke_col"], "idx": (":COLMD:entire atmosphere", "aerosol=Particulate organic matter dry")}
    # RRFS bonus: total-aerosol 1-h averages = PM2.5 / PM10 (µg/m³). No record at
    # f000 (an average needs an hour) → zero_at_f0 keeps loop prefetch unstalled.
    out["pm25"] = {"idx": (":MASSDEN:8 m above ground:", "aerosol=Total aerosol:aerosol_size <2.5e-06"), "label": "PM2.5 (1-h avg)", "conv": "x1e9", "vmin": 0.0, "vmax": 250.0, "units": "µg/m³", "lut": "smoke", "group": "Smoke", "nodata_below": 2.0, "file": "2dfld", "zero_at_f0": True}
    out["pm10"] = {"idx": (":MASSDEN:8 m above ground:", "aerosol=Total aerosol:aerosol_size <1e-05"), "label": "PM10 (1-h avg)", "conv": "x1e9", "vmin": 0.0, "vmax": 425.0, "units": "µg/m³", "lut": "smoke", "group": "Smoke", "nodata_below": 2.0, "file": "2dfld", "zero_at_f0": True}
    return out

RRFS_FIELDS: dict[str, dict] = _rrfs_fields()

# ── GFS registry (0.25° global; already a regular lat/lon grid) ──────────────
# GFS is a single pgrb2.0p25 file (all surface + pressure levels in one object),
# so there's no sfc/prs split — its `key` ignores the file token. It carries a
# SUBSET of the HRRR fields: no 0–6 km shear *components* (→ no shear06/SCP/STP),
# no 0–1 km SRH (→ no ehi01), no 4LFTX, no updraft helicity, no ASNOW. Verified
# against the live .idx. The generic KDTree regrid handles its lat/lon grid.
_GFS_KEEP = (
    "t2m", "td2m", "refc",
    "sbcape", "mlcape", "mucape", "sbcin", "srh03", "pwat", "ehi03",
    "t850", "rh850", "wspd850", "t700", "rh700",
    "z500", "t500", "wspd500", "z300", "wspd250",
    "vort500", "omega700", "wspd300",
    "snod", "ptype",
)
GFS_FIELDS: dict[str, dict] = {k: dict(HRRR_FIELDS[k]) for k in _GFS_KEEP}

# ── NAM-NEST registry (3 km CONUS nest; Lambert-Conformal) ──────────────────
# The 3 km CONUS nest carries nearly the full HRRR field set in one file
# (surface + pressure levels together, incl. updraft helicity) — only ASNOW and
# the smoke fields are absent. Single file → `key` ignores the token.
NAM_FIELDS: dict[str, dict] = {
    k: dict(v) for k, v in HRRR_FIELDS.items() if k not in ("asnow", "smoke_sfc", "smoke_col")
}

# ── RAP registry (13 km CONUS grid 130; RAP runs until RRFSv2, not RRFSv1) ──
# awp130pgrb carries surface + pressure levels together (verified live idx), so
# every field comes from that one ~18 MB file. u/v pairs are NAM-style combined
# submessages (shared idx offset) — _get_uv handles that. Dropped fields:
#   - the UH family + refc_uh (no MXUPHL at 13 km);
#   - smoke_col — RAP's COLMD exists only in wrfprs/wrfnat, whose rotated
#     Arakawa E-grid (gridType ncep_32769) eccodes can't geolocate (latitudes
#     lookup fails). RAP keeps Near-Surface Smoke; column smoke = HRRR/RRFS.
_RAP_DROP = ("uh25", "uh03", "uh25_3h", "uh25_run", "uh03_run", "refc_uh", "smoke_col")

def _rap_fields() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for k, v in HRRR_FIELDS.items():
        if k in _RAP_DROP:
            continue
        s = dict(v)
        s.pop("file", None)  # single-file model: awp130pgrb has the mb levels too
        out[k] = s
    return out

RAP_FIELDS: dict[str, dict] = _rap_fields()

# ── NBM registry (National Blend of Models, 2.5 km CONUS "core" file) ───────
# Consensus/calibrated guidance — the "most likely" forecast, great on-air for
# temps/gusts/thunder chances. Hourly runs; core is hourly through F36 (3-/6-
# hourly beyond — not exposed since the app steps hour-by-hour). No MSLP in the
# core file → no isobar overlay (get_isobars guards on it).
NBM_FIELDS: dict[str, dict] = {
    "t2m":   {"idx": ":TMP:2 m above ground:",   "label": "2 m Temperature",  "conv": "k2f",   "vmin": -30.0, "vmax": 120.0, "units": "°F", "lut": "hrrr_temp",     "group": "Surface"},
    "td2m":  {"idx": ":DPT:2 m above ground:",   "label": "2 m Dew Point",    "conv": "k2f",   "vmin": -30.0, "vmax": 90.0,  "units": "°F", "lut": "hrrr_dewpoint", "group": "Surface"},
    "appt":  {"idx": ":APTMP:2 m above ground:", "label": "Apparent Temp",    "conv": "k2f",   "vmin": -40.0, "vmax": 120.0, "units": "°F", "lut": "hrrr_temp",     "group": "Surface"},
    "rh2m":  {"idx": ":RH:2 m above ground:",    "label": "2 m Humidity",     "conv": None,    "vmin": 0.0,   "vmax": 100.0, "units": "%",  "lut": "rh",            "group": "Surface"},
    "gust":  {"idx": ":GUST:10 m above ground:", "label": "Wind Gust",        "conv": "ms2kt", "vmin": 0.0,   "vmax": 80.0,  "units": "kt", "lut": "wind_upper",    "group": "Surface"},
    "sky":   {"idx": ":TCDC:surface:",           "label": "Sky Cover",        "conv": None,    "vmin": 0.0,   "vmax": 100.0, "units": "%",  "lut": "rh",            "group": "Surface"},
    "tstm":  {"idx": ":TSTM:surface:",           "label": "Thunder Chance",   "conv": None,    "vmin": 0.0,   "vmax": 100.0, "units": "%",  "lut": "composite",     "group": "Convective", "nodata_below": 10.0},
    "maxref":{"idx": ":MAXREF:1000 m above ground:", "label": "Simulated Reflectivity", "conv": None, "vmin": -20.0, "vmax": 80.0, "units": "dBZ", "lut": "reflectivity", "group": "Convective", "nodata_below": 5.0, "zero_at_f0": True},
    "cape":  {"idx": ":CAPE:surface:",           "label": "Surface CAPE",     "conv": None,    "vmin": 0.0,   "vmax": 8000.0, "units": "J/kg", "lut": "cape",        "group": "Severe", "nodata_below": 100.0},
    "pwat":  {"idx": ":PWAT:entire atmosphere",  "label": "Precipitable Water","conv": "mm2in","vmin": 0.0,   "vmax": 2.5,   "units": "in", "lut": "pwat",          "group": "Severe"},
}

# ── GEFS registry (ensemble MEAN, 0.5° pgrb2a; 3-hourly files → fhour_step 3) ─
GEFS_FIELDS: dict[str, dict] = {
    "prmsl":   {"idx": ":PRMSL:mean sea level:", "label": "MSLP (ens mean)",  "conv": "pa2mb", "vmin": 960.0, "vmax": 1050.0, "units": "hPa", "lut": "height",     "group": "Surface"},
    "t2m":     {"idx": ":TMP:2 m above ground:", "label": "2 m Temp (mean)",  "conv": "k2f",   "vmin": -30.0, "vmax": 120.0,  "units": "°F", "lut": "hrrr_temp",   "group": "Surface"},
    "cape":    {"idx": ":CAPE:180-0 mb above ground:", "label": "MU CAPE (mean)", "conv": None, "vmin": 0.0, "vmax": 6000.0, "units": "J/kg", "lut": "cape",      "group": "Severe", "nodata_below": 100.0},
    "pwat":    {"idx": ":PWAT:entire atmosphere","label": "Precipitable Water","conv": "mm2in","vmin": 0.0,   "vmax": 2.5,    "units": "in", "lut": "pwat",        "group": "Severe"},
    "z500":    {"idx": ":HGT:500 mb:",           "label": "500 mb Height",    "conv": None,    "vmin": 5160.0,"vmax": 6000.0, "units": "m",  "lut": "height",      "group": "Upper Air"},
    "t850":    {"idx": ":TMP:850 mb:",           "label": "850 mb Temp",      "conv": "k2c",   "vmin": -30.0, "vmax": 30.0,   "units": "°C", "lut": "temp_upper",  "group": "Upper Air"},
    "rh700":   {"idx": ":RH:700 mb:",            "label": "700 mb RH",        "conv": None,    "vmin": 0.0,   "vmax": 100.0,  "units": "%",  "lut": "rh",          "group": "Upper Air"},
    "wspd850": {"derive": ("mag", ":UGRD:850 mb:", ":VGRD:850 mb:"), "label": "850 mb Wind", "conv": "ms2kt", "vmin": 0.0, "vmax": 80.0,  "units": "kt", "lut": "wind_upper", "group": "Upper Air"},
    "wspd500": {"derive": ("mag", ":UGRD:500 mb:", ":VGRD:500 mb:"), "label": "500 mb Wind", "conv": "ms2kt", "vmin": 0.0, "vmax": 120.0, "units": "kt", "lut": "wind_upper", "group": "Upper Air"},
    "wspd250": {"derive": ("mag", ":UGRD:250 mb:", ":VGRD:250 mb:"), "label": "250 mb Wind", "conv": "ms2kt", "vmin": 0.0, "vmax": 160.0, "units": "kt", "lut": "wind_upper", "group": "Upper Air"},
}

# ── Model registry (HRRR + RRFS-A + GFS + NAM-NEST + RAP + NBM + GEFS) ───────
MODELS: dict[str, dict] = {
    "hrrr": {
        "label": "HRRR", "bucket": "noaa-hrrr-bdp-pds", "fields": HRRR_FIELDS,
        "run_hours": tuple(range(24)), "fhour_offset": 0,
        "max_fhour": (lambda hh: 48 if hh % 6 == 0 else 18),
        "default_file": "sfc", "mslp": (":MSLMA:mean sea level:", "sfc"),
        "key": (lambda date, hh, f, tok: f"hrrr.{date}/conus/hrrr.t{hh:02d}z.wrf{tok}f{f:02d}.grib2"),
    },
    "rrfs": {
        "label": "RRFS-A", "bucket": "noaa-rrfs-pds", "fields": RRFS_FIELDS,
        "run_hours": tuple(range(24)), "fhour_offset": 0,  # deterministic has f000
        "max_fhour": (lambda hh: 84 if hh % 6 == 0 else 18),  # synoptic runs to F84
        "default_file": "2dfld", "mslp": (":MSLET:mean sea level:", "2dfld"),
        "key": (lambda date, hh, f, tok: f"rrfs_a/rrfs.{date}/{hh:02d}/rrfs.t{hh:02d}z.{tok}.3km.f{f:03d}.conus.grib2"),
    },
    "gfs": {
        "label": "GFS", "bucket": "noaa-gfs-bdp-pds", "fields": GFS_FIELDS,
        "run_hours": (0, 6, 12, 18), "fhour_offset": 0,
        "max_fhour": (lambda hh: 120),  # 0.25° is hourly to F120 (3-hourly after — not exposed)
        "default_file": "", "mslp": (":PRMSL:mean sea level:", ""),  # single file → token unused
        "key": (lambda date, hh, f, tok: f"gfs.{date}/{hh:02d}/atmos/gfs.t{hh:02d}z.pgrb2.0p25.f{f:03d}"),
    },
    "nam": {
        "label": "NAM-NEST", "bucket": "noaa-nam-pds", "fields": NAM_FIELDS,
        "run_hours": (0, 6, 12, 18), "fhour_offset": 0,
        "max_fhour": (lambda hh: 60),  # 3 km CONUS nest, hourly to F60
        "default_file": "", "mslp": (":MSLET:mean sea level:", ""),  # single file → token unused
        "key": (lambda date, hh, f, tok: f"nam.{date}/nam.t{hh:02d}z.conusnest.hiresf{f:02d}.tm00.grib2"),
    },
    "nbm": {
        "label": "NBM", "bucket": "noaa-nbm-grib2-pds", "fields": NBM_FIELDS,
        "run_hours": tuple(range(24)), "fhour_offset": 1,  # no f000 published — slots start at f001
        "max_fhour": (lambda hh: 35),  # app index 0..35 → files f001..f036 (3-/6-hourly beyond, not exposed)
        "default_file": "core", "mslp": None,  # no PRMSL in the core file
        "key": (lambda date, hh, f, tok: f"blend.{date}/{hh:02d}/core/blend.t{hh:02d}z.core.f{f:03d}.co.grib2"),
    },
    "gefs": {
        "label": "GEFS Mean", "bucket": "noaa-gefs-pds", "fields": GEFS_FIELDS,
        "run_hours": (0, 6, 12, 18), "fhour_offset": 0, "fhour_step": 3,
        "max_fhour": (lambda hh: 240),  # 0.5° mean, 3-hourly to F240
        "default_file": "", "mslp": (":PRMSL:mean sea level:", ""),
        "key": (lambda date, hh, f, tok: f"gefs.{date}/{hh:02d}/atmos/pgrb2ap5/geavg.t{hh:02d}z.pgrb2a.0p50.f{f:03d}"),
    },
    "rap": {
        "label": "RAP", "bucket": "noaa-rap-pds", "fields": RAP_FIELDS,
        "run_hours": tuple(range(24)), "fhour_offset": 0,
        "max_fhour": (lambda hh: 51 if hh % 6 == 3 else 21),  # extended at 03/09/15/21Z
        "default_file": "awp130pgrb", "mslp": (":MSLMA:mean sea level:", "awp130pgrb"),
        # Token is the literal file stem: awp130pgrbf06 / wrfprsf06 (COLMD only).
        "key": (lambda date, hh, f, tok: f"rap.{date}/rap.t{hh:02d}z.{tok}f{f:02d}.grib2"),
    },
}

# Target display grid (regular lat/lon, covers the HRRR CONUS domain).
T_W, T_E, T_S, T_N = -134.0, -60.5, 20.5, 53.0
T_RES = 0.035
T_NI = int(round((T_E - T_W) / T_RES))  # columns, W→E
T_NJ = int(round((T_N - T_S) / T_RES))  # rows, N→S

_eccodes_ok: Optional[bool] = None


def _check_deps() -> bool:
    global _eccodes_ok
    if _eccodes_ok is None:
        try:
            import eccodes  # noqa: F401
            import scipy.spatial  # noqa: F401
            _eccodes_ok = True
        except Exception:
            logger.warning("HRRR fields disabled — need `pip install eccodes scipy`.")
            _eccodes_ok = False
    return _eccodes_ok


def _conv(name: Optional[str], v: np.ndarray) -> np.ndarray:
    if name == "k2f":   return (v - 273.15) * 9.0 / 5.0 + 32.0   # Kelvin → °F
    if name == "k2c":   return v - 273.15                        # Kelvin → °C
    if name == "ms2kt": return v * 1.94384                       # m/s → knots
    if name == "m2in":  return v * 39.3701                       # meters → inches
    if name == "mm2in": return v * 0.0393701                     # kg/m² (mm) → inches
    if name == "x1e5":  return v * 1e5                           # s⁻¹ → ×10⁻⁵ s⁻¹
    if name == "x1e9":  return v * 1e9                           # kg/m³ → µg/m³ (smoke)
    if name == "x1e6":  return v * 1e6                           # kg/m² → mg/m² (column smoke)
    if name == "pa2mb": return v / 100.0                     # Pa -> hPa (MSLP)
    return v


# ── Composite-parameter calculators (operate on native-grid arrays) ─────────
# Inputs are decoded in registry order; units are SI as stored in GRIB
# (CAPE/CIN J/kg, SRH m²/s², shear components m/s, temps K).
def _calc_ehi(a):  # [CAPE, SRH] → Energy Helicity Index
    return a[0] * a[1] / 160000.0

def _calc_scp(a):  # [MUCAPE, SRH 0-3km, u-shear 0-6km, v-shear 0-6km] → SCP (fixed-layer)
    mucape, srh3, u, v = a
    bwd = np.sqrt(u * u + v * v)                       # m/s
    shr = np.clip(bwd / 20.0, 0.0, 1.5)
    shr = np.where(bwd < 10.0, 0.0, shr)              # SPC: <10 m/s → 0
    return (mucape / 1000.0) * (srh3 / 50.0) * shr

def _calc_stp(a):  # [SBCAPE, SBCIN, T2m(K), Td2m(K), SRH 0-1km, u-shear, v-shear] → STP (fixed-layer)
    sbcape, sbcin, tK, tdK, srh1, u, v = a
    lcl = 125.0 * ((tK - 273.15) - (tdK - 273.15))   # ≈ LCL height (m AGL)
    lcl_term = np.clip((2000.0 - lcl) / 1000.0, 0.0, 1.0)  # 1 if <1000 m, 0 if >2000 m
    bwd = np.sqrt(u * u + v * v)
    shr = np.clip(bwd / 20.0, 0.0, 1.5)
    shr = np.where(bwd < 12.5, 0.0, shr)
    shr = np.where(bwd > 30.0, 1.5, shr)
    cin_term = np.clip((200.0 + sbcin) / 150.0, 0.0, 1.0)  # sbcin ≤ 0; 1 if >-50, 0 if <-200
    return (sbcape / 1500.0) * lcl_term * (srh1 / 150.0) * shr * cin_term

_CALCS = {"ehi": _calc_ehi, "scp": _calc_scp, "stp": _calc_stp}


class HRRRFieldService:
    def __init__(self) -> None:
        self._s3 = None
        # sig → [T_NJ*T_NI] native flat indices. Cached per model/grid-size so
        # concurrent requests for different models can't swap the mapping out
        # from under each other (IndexError / wrong grids); bounded (~15 MB ea).
        self._mappings: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._map_lock = threading.Lock()
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._runs_cache: dict[str, tuple[float, list[dict]]] = {}  # model → (ts, runs)
        self._idx_cache: "OrderedDict[str, tuple[float, list[str]]]" = OrderedDict()
        self._lock = threading.Lock()
        self._map_dir = os.path.join(tempfile.gettempdir(), "tbf_hrrr")
        os.makedirs(self._map_dir, exist_ok=True)

    @property
    def available(self) -> bool:
        return _check_deps()

    def _get_s3(self):
        if self._s3 is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
            self._s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
        return self._s3

    # ── Run discovery ──────────────────────────────────────────────────────
    @staticmethod
    def _key(model: str, date: str, hh: int, fhour: int, token: Optional[str] = None) -> str:
        m = MODELS[model]
        tok = token or m["default_file"]
        return m["key"](date, hh, fhour + m["fhour_offset"], tok)  # offset → real file hour

    def _exists(self, model: str, key: str) -> bool:
        try:
            self._get_s3().head_object(Bucket=MODELS[model]["bucket"], Key=key + ".idx")
            return True
        except Exception:
            return False

    def list_runs(self, model: str = "hrrr", limit: int = 10) -> list[dict]:
        """The most recent `limit` runs for the model (newest first). Walks back
        from now to the latest available run hour, then enumerates prior runs.
        Cached briefly — each call probes S3 with a burst of HEADs."""
        if model not in MODELS:
            return []
        with self._lock:
            cached = self._runs_cache.get(model)
            if cached and time.time() - cached[0] < RUNS_CACHE_TTL_S:
                return cached[1]
        m = MODELS[model]
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        latest: Optional[datetime] = None
        for back in range(0, 24):  # 6-hourly models may be several h old
            t = now - timedelta(hours=back)
            if t.hour not in m["run_hours"]:
                continue
            if self._exists(model, self._key(model, t.strftime("%Y%m%d"), t.hour, 0)):
                latest = t
                break
        if latest is None:
            return []
        runs, t = [], latest
        while len(runs) < limit and t > latest - timedelta(days=10):
            if t.hour in m["run_hours"]:
                runs.append({"run": t.strftime("%Y%m%d%H"), "iso": t.isoformat(),
                             "max_fhour": m["max_fhour"](t.hour)})
            t -= timedelta(hours=1)
        with self._lock:
            self._runs_cache[model] = (time.time(), runs)
        return runs

    def fields(self, model: str = "hrrr") -> list[dict]:
        reg = MODELS.get(model, MODELS["hrrr"])["fields"]
        return [
            {"id": k, "label": v["label"], "units": v["units"], "lut": v["lut"],
             "vmin": v["vmin"], "vmax": v["vmax"], "group": v.get("group", "Other"),
             "barbs": (v.get("derive") or (None,))[0] == "mag",
             "uh_contour": v.get("uh_contour")}
            for k, v in reg.items()
        ]

    # ── Field fetch (on demand, cached) ────────────────────────────────────
    def get_field(self, model: str, run: str, param: str, fhour: int) -> Optional[bytes]:
        if not _check_deps() or model not in MODELS or param not in MODELS[model]["fields"]:
            return None
        ck = f"{model}:{run}:{param}:{fhour}"
        with self._lock:
            if ck in self._cache:
                self._cache.move_to_end(ck)
                return self._cache[ck]
        try:
            data = self._build_field(model, run, param, fhour)
        except Exception as e:
            # A not-yet-produced forecast hour (fresh run still uploading) is
            # expected — log it quietly, not as a warning.
            if "NoSuchKey" in str(e) or "Not Found" in str(e):
                logger.debug("field %s not available yet", ck)
            else:
                logger.warning("field %s failed: %s", ck, e)
            return None
        if data is None:
            return None
        with self._lock:
            self._cache[ck] = data
            self._cache.move_to_end(ck)
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)
        return data

    def _build_field(self, model: str, run: str, param: str, fhour: int) -> Optional[bytes]:
        spec = MODELS[model]["fields"][param]
        grid = self._field_grid(model, run, param, fhour)
        if grid is None:
            return None
        return self._encode(grid, float(spec["vmin"]), float(spec["vmax"]), spec.get("nodata_below"))

    def _zero_or_none(self, spec: dict, fhour: int):
        """F00 of a max-in-window field (e.g. updraft helicity) has no record —
        return a blank grid so the loop has a valid first frame, not a hole."""
        if fhour == 0 and spec.get("zero_at_f0"):
            return np.zeros((T_NJ, T_NI), dtype=np.float32)
        return None

    def _field_grid(self, model: str, run: str, param: str, fhour: int):
        """Produce the regridded float grid (T_NJ×T_NI) for a field, or None."""
        spec = MODELS[model]["fields"][param]
        if "timeagg" in spec:
            return self._timeagg_grid(model, run, param, fhour, spec)

        date, hh = run[:8], int(run[8:10])
        key = self._key(model, date, hh, fhour, spec.get("file"))
        lines = self._read_idx(model, key)  # raises NoSuchKey if the hour isn't produced yet

        derive = spec.get("derive")
        if derive:
            kind = derive[0]
            if kind == "mag":
                uv = self._get_uv(model, key, lines, derive[1], derive[2])
                if uv is None:
                    return self._zero_or_none(spec, fhour)
                u, v, lats, lons = uv
                values = np.sqrt(u ** 2 + v ** 2)
            elif kind == "ptype":
                msgs = [self._range_get(model, key, lines, m) for m in derive[1:]]
                if any(g is None for g in msgs):
                    return self._zero_or_none(spec, fhour)
                decoded = [self._decode(g) for g in msgs]
                lats, lons = decoded[0][1], decoded[0][2]
                arrs = [d[0] for d in decoded]
                values = np.zeros_like(arrs[0])  # rain, snow, icep, frzr → 1/2/3/4 (later wins)
                for code, a in enumerate(arrs, start=1):
                    values = np.where(a > 0.5, float(code), values)
            elif kind == "calc":
                name, inputs = derive[1], derive[2]
                arrs, lats, lons = [], None, None
                for idx_match, cv in inputs:
                    g = self._range_get(model, key, lines, idx_match)
                    if g is None:
                        return self._zero_or_none(spec, fhour)
                    vv, lats, lons, _, _ = self._decode(g)
                    arrs.append(_conv(cv, vv))
                values = _CALCS[name](arrs)
            else:
                raise ValueError(f"unknown derive kind {kind}")
        else:
            grib = self._range_get(model, key, lines, spec["idx"])
            if grib is None:
                return self._zero_or_none(spec, fhour)
            values, lats, lons, _, _ = self._decode(grib)

        values = _conv(spec.get("conv"), values)
        mapping = self._get_mapping(lats, lons, model)
        return values[mapping].reshape(T_NJ, T_NI).astype(np.float32)

    def _timeagg_grid(self, model: str, run: str, param: str, fhour: int, spec: dict):
        """Aggregate a base field over a forecast-hour window (e.g. UH run/3-h max)."""
        op, window = spec["timeagg"]
        base = spec["base"]
        cur = self._field_grid(model, run, base, fhour)
        if cur is None:
            return self._zero_or_none(spec, fhour)
        if window == "run":  # cumulative — reuse the cached previous run-max (O(1)/frame)
            if fhour <= 0:
                return cur
            prev = self.get_field(model, run, param, fhour - 1)
            if prev is None:
                return cur
            return np.maximum(cur, self._dequantize(prev))
        out = cur  # last N hours
        for h in range(max(0, fhour - int(window) + 1), fhour):
            g = self._field_grid(model, run, base, h)
            if g is not None:
                out = np.maximum(out, g)
        return out

    def _dequantize(self, data: bytes) -> np.ndarray:
        """Reconstruct a float grid from a packed HRRR binary (for timeagg max)."""
        vmin, vmax = struct.unpack("<ff", data[44:52])
        b = np.frombuffer(data[52:], dtype=np.uint8).astype(np.float32)
        out = vmin + (b - 1.0) / 254.0 * (vmax - vmin)
        out[b == 0] = 0.0
        return out.reshape(T_NJ, T_NI)

    def get_barbs(self, model: str, run: str, param: str, fhour: int, stride: int = 14) -> Optional[dict]:
        """Downsampled wind vectors for a `mag` wind field → barb plotting data.
        Returns compact [lon, lat, speed_kt, from_dir_deg] rows."""
        spec = MODELS.get(model, {}).get("fields", {}).get(param)
        if not spec or spec.get("derive", (None,))[0] != "mag":
            return None
        _, u_idx, v_idx = spec["derive"]
        date, hh = run[:8], int(run[8:10])
        key = self._key(model, date, hh, fhour, spec.get("file"))
        lines = self._read_idx(model, key)
        uv = self._get_uv(model, key, lines, u_idx, v_idx)
        if uv is None:
            return None
        u, v, lats, lons = uv
        mapping = self._get_mapping(lats, lons, model)
        ug = u[mapping].reshape(T_NJ, T_NI)
        vg = v[mapping].reshape(T_NJ, T_NI)
        rs = np.arange(stride // 2, T_NJ, stride)
        cs = np.arange(stride // 2, T_NI, stride)
        U = ug[np.ix_(rs, cs)]
        V = vg[np.ix_(rs, cs)]
        spd = np.sqrt(U ** 2 + V ** 2) * 1.94384  # kt
        drc = (270.0 - np.degrees(np.arctan2(V, U))) % 360.0  # meteorological FROM dir
        latg = (T_N - (rs + 0.5) * T_RES)[:, None] * np.ones((1, cs.size))
        long = (T_W + (cs + 0.5) * T_RES)[None, :] * np.ones((rs.size, 1))
        mask = np.isfinite(spd) & (spd >= 2.0)
        pts = np.stack([long[mask].round(3), latg[mask].round(3), spd[mask].round(), drc[mask].round()], axis=1)
        return {"param": param, "fhour": fhour, "points": pts.tolist()}

    def get_isobars(self, model: str, run: str, fhour: int, interval: int = 4) -> Optional[dict]:
        """MSLP isobars as GeoJSON LineStrings (hPa in `p`, every-20 hPa flagged
        `bold`). Smoothed + downsampled before contouring to keep lines clean."""
        if not MODELS[model].get("mslp"):
            return None  # e.g. NBM core carries no MSLP
        mslp_idx, mslp_tok = MODELS[model]["mslp"]
        date, hh = run[:8], int(run[8:10])
        key = self._key(model, date, hh, fhour, mslp_tok)
        lines = self._read_idx(model, key)
        grib = self._range_get(model, key, lines, mslp_idx)
        if grib is None:
            return None
        vals, lats, lons, _, _ = self._decode(grib)
        mapping = self._get_mapping(lats, lons, model)
        grid = vals[mapping].reshape(T_NJ, T_NI) / 100.0  # Pa → hPa

        from scipy.ndimage import gaussian_filter
        from contourpy import contour_generator, LineType
        step = 4
        g = gaussian_filter(grid[::step, ::step], sigma=1.2)
        xs = T_W + (np.arange(0, T_NI, step) + 0.5) * T_RES               # W→E (increasing)
        ys = (T_N - (np.arange(0, T_NJ, step) + 0.5) * T_RES)[::-1]       # flip to S→N (increasing)
        g = g[::-1, :]
        cg = contour_generator(xs, ys, g, line_type=LineType.Separate)
        lo = int(np.floor(float(np.nanmin(g)) / interval) * interval)
        hi = int(np.ceil(float(np.nanmax(g)) / interval) * interval)
        feats = []
        for level in range(lo, hi + 1, interval):
            for arr in cg.lines(level):
                if len(arr) < 3:
                    continue
                coords = [[round(float(x), 3), round(float(y), 3)] for x, y in arr]
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {"p": int(level), "bold": 1 if level % 20 == 0 else 0},
                })
        return {"type": "FeatureCollection", "features": feats, "fhour": fhour}

    def get_contours(self, model: str, run: str, param: str, fhour: int, levels: list[float]) -> Optional[dict]:
        """Contour a registered field at the given levels → GeoJSON LineStrings
        (level in `v`). Used for the updraft-helicity overlay on reflectivity."""
        if model not in MODELS or param not in MODELS[model]["fields"]:
            return None
        grid = self._field_grid(model, run, param, fhour)
        if grid is None:
            return {"type": "FeatureCollection", "features": [], "fhour": fhour}
        from scipy.ndimage import gaussian_filter
        from contourpy import contour_generator, LineType
        step = 2
        g = gaussian_filter(np.nan_to_num(grid[::step, ::step], nan=0.0), sigma=0.8)
        xs = T_W + (np.arange(0, T_NI, step) + 0.5) * T_RES
        ys = (T_N - (np.arange(0, T_NJ, step) + 0.5) * T_RES)[::-1]
        g = g[::-1, :]
        cg = contour_generator(xs, ys, g, line_type=LineType.Separate)
        feats = []
        for lv in levels:
            for arr in cg.lines(float(lv)):
                if len(arr) < 3:
                    continue
                coords = [[round(float(x), 3), round(float(y), 3)] for x, y in arr]
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {"v": float(lv)},
                })
        return {"type": "FeatureCollection", "features": feats, "fhour": fhour}

    def _read_idx(self, model: str, key: str) -> list[str]:
        # Cached with a short uniform TTL: .idx files are immutable once the
        # hour is fully uploaded, but the newest hour's can still be growing
        # (see IDX_CACHE_TTL_S) — TTL-ing everything is the simple robust option.
        ck = f"{model}:{key}"
        with self._lock:
            ent = self._idx_cache.get(ck)
            if ent and time.time() - ent[0] < IDX_CACHE_TTL_S:
                self._idx_cache.move_to_end(ck)
                return ent[1]
        s3 = self._get_s3()
        idx = s3.get_object(Bucket=MODELS[model]["bucket"], Key=key + ".idx")["Body"].read().decode("utf-8", "replace")
        lines = idx.splitlines()
        with self._lock:
            self._idx_cache[ck] = (time.time(), lines)
            self._idx_cache.move_to_end(ck)
            while len(self._idx_cache) > IDX_CACHE_MAX:
                self._idx_cache.popitem(last=False)
        return lines

    @staticmethod
    def _idx_range(lines: list[str], idx_match) -> tuple[Optional[int], Optional[int]]:
        """(start, end) byte range of the record matching idx_match — a substring,
        or a tuple of substrings that must ALL be in the line (RRFS appends
        aerosol qualifiers to distinguish smoke/dust/total at one var:level).
        `end` is the next record's offset, SKIPPING idx sub-entries that share
        this offset — some models (NAM) pack u & v as `n.1`/`n.2` at one offset,
        so a naive next-line `end` would be empty/backwards and S3 would serve
        the whole file. None end → open-ended (last record)."""
        parts = idx_match if isinstance(idx_match, tuple) else (idx_match,)
        start = None
        idx = -1
        for i, line in enumerate(lines):
            if all(p in line for p in parts):
                start = int(line.split(":")[1])
                idx = i
                break
        if start is None:
            return None, None
        end = None
        for line in lines[idx + 1:]:
            o = int(line.split(":")[1])
            if o > start:
                end = o
                break
        return start, end

    def _range_get(self, model: str, key: str, lines: list[str], idx_match) -> Optional[bytes]:
        """Byte-range GET the single GRIB record whose .idx line matches idx_match
        (str substring, or tuple of substrings that must all match)."""
        start, end = self._idx_range(lines, idx_match)
        if start is None:
            logger.warning("field %s not in idx for %s", idx_match, key)
            return None
        rng = f"bytes={start}-" + (str(end - 1) if end else "")
        return self._get_s3().get_object(Bucket=MODELS[model]["bucket"], Key=key, Range=rng)["Body"].read()

    def _get_uv(self, model: str, key: str, lines: list[str], u_idx: str, v_idx: str):
        """Decode a u/v wind pair → (u, v, lats, lons). Handles models (NAM nest)
        that pack u & v in ONE combined GRIB message (same .idx byte offset) by
        multi-decoding both submessages; otherwise fetches the two records."""
        us, _ = self._idx_range(lines, u_idx)
        vs, _ = self._idx_range(lines, v_idx)
        if us is None or vs is None:
            return None
        if us == vs:  # combined message → both components live in one record
            grib = self._range_get(model, key, lines, u_idx)
            if grib is None:
                return None
            comps = self._decode_multi(grib)
            if len(comps) < 2:
                return None
            return comps[0][0], comps[1][0], comps[0][1], comps[0][2]
        gu = self._range_get(model, key, lines, u_idx)
        gv = self._range_get(model, key, lines, v_idx)
        if gu is None or gv is None:
            return None
        u, lats, lons, _, _ = self._decode(gu)
        v, _, _, _, _ = self._decode(gv)
        return u, v, lats, lons

    @staticmethod
    def _fix_alt_scan(gid, values: np.ndarray, ni: int, nj: int) -> np.ndarray:
        # NBM core files use scanning mode 80 (alternativeRowScanning: odd rows
        # written right-to-left). eccodes' lat/lon geoiterator ignores the flag
        # for Lambert grids, so the values must be un-boustrophedoned to match
        # the uniform left-to-right lat/lon arrays — without this the regrid
        # mirror-smears every other row (symmetric "butterfly" CONUS).
        import eccodes
        try:
            if int(eccodes.codes_get(gid, "alternativeRowScanning")) != 1:
                return values
        except Exception:
            return values
        if values.size != ni * nj:
            return values
        v = values.reshape(nj, ni).copy()
        v[1::2, :] = v[1::2, ::-1]
        return v.ravel()

    def _decode(self, grib: bytes):
        # Decode straight from the in-memory message (no temp file → avoids
        # Windows file locking and is faster). Serialized: eccodes isn't
        # thread-safe (see _DECODE_LOCK).
        import eccodes
        with _DECODE_LOCK:
            gid = eccodes.codes_new_from_message(grib)
            if gid is None:
                raise ValueError("no GRIB message")
            try:
                ni = int(eccodes.codes_get(gid, "Ni"))
                nj = int(eccodes.codes_get(gid, "Nj"))
                values = np.asarray(eccodes.codes_get_values(gid), dtype=np.float64)
                values = self._fix_alt_scan(gid, values, ni, nj)
                lats = np.asarray(eccodes.codes_get_array(gid, "latitudes"), dtype=np.float64)
                lons = np.asarray(eccodes.codes_get_array(gid, "longitudes"), dtype=np.float64)
                try:
                    mv = eccodes.codes_get(gid, "missingValue")
                    values = np.where(values == mv, np.nan, values)
                except Exception:
                    pass
            finally:
                eccodes.codes_release(gid)
        lons = np.where(lons > 180.0, lons - 360.0, lons)  # → [-180,180]
        return values, lats, lons, ni, nj

    def _decode_multi(self, grib: bytes):
        """Decode EVERY submessage in a combined GRIB record (e.g. NAM's u/v pair)
        → [(values, lats, lons, ni, nj), ...] in order. eccodes can't iterate
        submessages from memory, so this round-trips through a temp file (only
        used for the rare combined-message case, not the hot scalar path)."""
        import eccodes
        out = []
        with _DECODE_LOCK:
            eccodes.codes_grib_multi_support_on()
            fd, path = tempfile.mkstemp(suffix=".grib2")
            try:
                os.write(fd, grib)
                os.close(fd)
                with open(path, "rb") as f:
                    while True:
                        gid = eccodes.codes_grib_new_from_file(f)
                        if gid is None:
                            break
                        try:
                            values = np.asarray(eccodes.codes_get_values(gid), dtype=np.float64)
                            lats = np.asarray(eccodes.codes_get_array(gid, "latitudes"), dtype=np.float64)
                            lons = np.asarray(eccodes.codes_get_array(gid, "longitudes"), dtype=np.float64)
                            ni = int(eccodes.codes_get(gid, "Ni"))
                            nj = int(eccodes.codes_get(gid, "Nj"))
                            values = self._fix_alt_scan(gid, values, ni, nj)
                            try:
                                mv = eccodes.codes_get(gid, "missingValue")
                                values = np.where(values == mv, np.nan, values)
                            except Exception:
                                pass
                            lons = np.where(lons > 180.0, lons - 360.0, lons)
                            out.append((values, lats, lons, ni, nj))
                        finally:
                            eccodes.codes_release(gid)
            finally:
                eccodes.codes_grib_multi_support_off()
                try:
                    os.remove(path)
                except Exception:
                    pass
        return out

    def _get_mapping(self, lats: np.ndarray, lons: np.ndarray, model: str) -> np.ndarray:
        # Per-model (HRRR & RRFS-A share the 1799×1059 size but are distinct grids).
        # Returns the mapping array (never stored in a single mutable slot) so a
        # concurrent request for another model can't swap it mid-regrid; the lock
        # only guards cache access — a rare duplicate KDTree build is acceptable.
        sig = f"{model}:{lats.size}"
        with self._map_lock:
            cached = self._mappings.get(sig)
            if cached is not None:
                self._mappings.move_to_end(sig)
                return cached
        cache_path = os.path.join(self._map_dir, f"map_{model}_{lats.size}_{T_NI}x{T_NJ}.npy")
        if os.path.exists(cache_path):
            mapping = np.load(cache_path)
        else:
            from scipy.spatial import cKDTree
            tree = cKDTree(np.column_stack([lons, lats]))
            tx = T_W + (np.arange(T_NI) + 0.5) * T_RES
            ty = T_N - (np.arange(T_NJ) + 0.5) * T_RES  # N→S rows
            gx, gy = np.meshgrid(tx, ty)
            _, idx = tree.query(np.column_stack([gx.ravel(), gy.ravel()]))
            mapping = idx.astype(np.int64)
            try:
                np.save(cache_path, mapping)
            except Exception:
                pass
        with self._map_lock:
            self._mappings[sig] = mapping
            self._mappings.move_to_end(sig)
            while len(self._mappings) > 8:  # 4 models × occasional grid change
                self._mappings.popitem(last=False)
        return mapping

    def _encode(self, grid: np.ndarray, vmin: float, vmax: float, nodata_below: Optional[float]) -> bytes:
        span = (vmax - vmin) or 1.0
        norm = (grid - vmin) / span
        byte = np.clip(np.round(norm * 254.0) + 1.0, 1, 255).astype(np.uint8)
        byte[~np.isfinite(grid)] = 0
        if nodata_below is not None:
            byte[grid < nodata_below] = 0
        header = bytearray()
        header += BINARY_MAGIC
        header += int(T_NI).to_bytes(4, "little")
        header += int(T_NJ).to_bytes(4, "little")
        import struct
        header += struct.pack("<dddd", T_N, T_S, T_W, T_E)  # north, south, west, east
        header += struct.pack("<ff", vmin, vmax)
        return bytes(header) + byte.tobytes()


_service: Optional[HRRRFieldService] = None


def get_hrrr_field_service() -> HRRRFieldService:
    global _service
    if _service is None:
        _service = HRRRFieldService()
    return _service
