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
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

HRRR_BUCKET = "noaa-hrrr-bdp-pds"
BINARY_MAGIC = b"HRRR"
CACHE_MAX = 80  # LRU entries (~2 MB each → ~160 MB cap)

# eccodes is NOT thread-safe: its .def parser (flex) keeps global buffer state,
# so concurrent codes_new_from_message() calls corrupt it ("end of buffer
# missed" / template syntax errors). The app prefetches with several workers and
# derived fields decode 2+ messages each, so we serialize the eccodes decode
# (the slow S3 byte-range fetches stay parallel — they run outside this lock).
_DECODE_LOCK = threading.Lock()

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
    "refc": {"idx": ":REFC:entire atmosphere:", "label": "Composite Reflectivity","conv": None, "vmin": -20.0, "vmax": 80.0,  "units": "dBZ", "lut": "reflectivity",  "group": "Surface", "nodata_below": 5.0},

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
}

# Mark pressure-level fields (wrfprs file) — everything at an "mb" level.
for _id, _spec in HRRR_FIELDS.items():
    _m = _spec.get("idx") or (_spec.get("derive") or (None, ""))[1]
    if isinstance(_m, str) and " mb:" in _m:
        _spec["file"] = "prs"

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
        self._mapping: Optional[np.ndarray] = None  # [T_NJ*T_NI] native flat indices
        self._mapping_sig: Optional[str] = None
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
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
    def _key(date: str, hh: int, fhour: int, kind: str = "sfc") -> str:
        return f"hrrr.{date}/conus/hrrr.t{hh:02d}z.wrf{kind}f{fhour:02d}.grib2"

    @staticmethod
    def _max_fhour(hh: int) -> int:
        return 48 if hh % 6 == 0 else 18  # 00/06/12/18Z run to F48, others F18

    def _exists(self, key: str) -> bool:
        try:
            self._get_s3().head_object(Bucket=HRRR_BUCKET, Key=key + ".idx")
            return True
        except Exception:
            return False

    def list_runs(self, limit: int = 10) -> list[dict]:
        """The most recent `limit` HRRR runs (newest first). Walks back from the
        current hour to find the latest available run, then enumerates back."""
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        latest: Optional[datetime] = None
        for back in range(0, 6):  # latest run is usually 1–2 h old
            t = now - timedelta(hours=back)
            if self._exists(self._key(t.strftime("%Y%m%d"), t.hour, 0)):
                latest = t
                break
        if latest is None:
            return []
        runs = []
        for i in range(limit):
            t = latest - timedelta(hours=i)
            hh = t.hour
            runs.append({
                "run": t.strftime("%Y%m%d%H"),
                "iso": t.isoformat(),
                "max_fhour": self._max_fhour(hh),
            })
        return runs

    def fields(self) -> list[dict]:
        return [
            {"id": k, "label": v["label"], "units": v["units"], "lut": v["lut"],
             "vmin": v["vmin"], "vmax": v["vmax"], "group": v.get("group", "Other")}
            for k, v in HRRR_FIELDS.items()
        ]

    # ── Field fetch (on demand, cached) ────────────────────────────────────
    def get_field(self, run: str, param: str, fhour: int) -> Optional[bytes]:
        if not _check_deps() or param not in HRRR_FIELDS:
            return None
        ck = f"{run}:{param}:{fhour}"
        with self._lock:
            if ck in self._cache:
                self._cache.move_to_end(ck)
                return self._cache[ck]
        try:
            data = self._build_field(run, param, fhour)
        except Exception as e:
            # A not-yet-produced forecast hour (fresh run still uploading) is
            # expected — log it quietly, not as a warning.
            if "NoSuchKey" in str(e) or "Not Found" in str(e):
                logger.debug("HRRR field %s not available yet", ck)
            else:
                logger.warning("HRRR field %s failed: %s", ck, e)
            return None
        if data is None:
            return None
        with self._lock:
            self._cache[ck] = data
            self._cache.move_to_end(ck)
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)
        return data

    def _build_field(self, run: str, param: str, fhour: int) -> Optional[bytes]:
        spec = HRRR_FIELDS[param]
        grid = self._field_grid(run, param, fhour)
        if grid is None:
            return None
        return self._encode(grid, float(spec["vmin"]), float(spec["vmax"]), spec.get("nodata_below"))

    def _zero_or_none(self, spec: dict, fhour: int):
        """F00 of a max-in-window field (e.g. updraft helicity) has no record —
        return a blank grid so the loop has a valid first frame, not a hole."""
        if fhour == 0 and spec.get("zero_at_f0"):
            return np.zeros((T_NJ, T_NI), dtype=np.float32)
        return None

    def _field_grid(self, run: str, param: str, fhour: int):
        """Produce the regridded float grid (T_NJ×T_NI) for a field, or None."""
        spec = HRRR_FIELDS[param]
        if "timeagg" in spec:
            return self._timeagg_grid(run, param, fhour, spec)

        date, hh = run[:8], int(run[8:10])
        key = self._key(date, hh, fhour, spec.get("file", "sfc"))
        lines = self._read_idx(key)  # raises NoSuchKey if the hour isn't produced yet

        derive = spec.get("derive")
        if derive:
            kind = derive[0]
            if kind in ("mag", "ptype"):
                msgs = [self._range_get(key, lines, m) for m in derive[1:]]
                if any(g is None for g in msgs):
                    return self._zero_or_none(spec, fhour)
                decoded = [self._decode(g) for g in msgs]
                lats, lons = decoded[0][1], decoded[0][2]
                arrs = [d[0] for d in decoded]
                if kind == "mag":
                    values = np.sqrt(arrs[0] ** 2 + arrs[1] ** 2)
                else:  # ptype: rain, snow, icep, frzr → 1/2/3/4 (later wins)
                    values = np.zeros_like(arrs[0])
                    for code, a in enumerate(arrs, start=1):
                        values = np.where(a > 0.5, float(code), values)
            elif kind == "calc":
                name, inputs = derive[1], derive[2]
                arrs, lats, lons = [], None, None
                for idx_match, cv in inputs:
                    g = self._range_get(key, lines, idx_match)
                    if g is None:
                        return self._zero_or_none(spec, fhour)
                    vv, lats, lons, _, _ = self._decode(g)
                    arrs.append(_conv(cv, vv))
                values = _CALCS[name](arrs)
            else:
                raise ValueError(f"unknown derive kind {kind}")
        else:
            grib = self._range_get(key, lines, spec["idx"])
            if grib is None:
                return self._zero_or_none(spec, fhour)
            values, lats, lons, _, _ = self._decode(grib)

        values = _conv(spec.get("conv"), values)
        self._ensure_mapping(lats, lons)
        return values[self._mapping].reshape(T_NJ, T_NI).astype(np.float32)

    def _timeagg_grid(self, run: str, param: str, fhour: int, spec: dict):
        """Aggregate a base field over a forecast-hour window (e.g. UH run/3-h max)."""
        op, window = spec["timeagg"]
        base = spec["base"]
        cur = self._field_grid(run, base, fhour)
        if cur is None:
            return self._zero_or_none(spec, fhour)
        if window == "run":  # cumulative — reuse the cached previous run-max (O(1)/frame)
            if fhour <= 0:
                return cur
            prev = self.get_field(run, param, fhour - 1)
            if prev is None:
                return cur
            return np.maximum(cur, self._dequantize(prev))
        out = cur  # last N hours
        for h in range(max(0, fhour - int(window) + 1), fhour):
            g = self._field_grid(run, base, h)
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

    def _read_idx(self, key: str) -> list[str]:
        s3 = self._get_s3()
        idx = s3.get_object(Bucket=HRRR_BUCKET, Key=key + ".idx")["Body"].read().decode("utf-8", "replace")
        return idx.splitlines()

    def _range_get(self, key: str, lines: list[str], idx_match: str) -> Optional[bytes]:
        """Byte-range GET the single GRIB record whose .idx line contains idx_match."""
        start = end = None
        for i, line in enumerate(lines):
            if idx_match in line:
                start = int(line.split(":")[1])
                if i + 1 < len(lines):
                    end = int(lines[i + 1].split(":")[1])
                break
        if start is None:
            logger.warning("HRRR field %s not in idx for %s", idx_match, key)
            return None
        rng = f"bytes={start}-" + (str(end - 1) if end else "")
        return self._get_s3().get_object(Bucket=HRRR_BUCKET, Key=key, Range=rng)["Body"].read()

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

    def _ensure_mapping(self, lats: np.ndarray, lons: np.ndarray) -> None:
        sig = str(lats.size)  # HRRR native grid is fixed (1799×1059)
        if self._mapping is not None and self._mapping_sig == sig:
            return
        cache_path = os.path.join(self._map_dir, f"map_{sig}_{T_NI}x{T_NJ}.npy")
        if os.path.exists(cache_path):
            self._mapping = np.load(cache_path)
            self._mapping_sig = sig
            return
        from scipy.spatial import cKDTree
        tree = cKDTree(np.column_stack([lons, lats]))
        tx = T_W + (np.arange(T_NI) + 0.5) * T_RES
        ty = T_N - (np.arange(T_NJ) + 0.5) * T_RES  # N→S rows
        gx, gy = np.meshgrid(tx, ty)
        _, idx = tree.query(np.column_stack([gx.ravel(), gy.ravel()]))
        self._mapping = idx.astype(np.int64)
        self._mapping_sig = sig
        try:
            np.save(cache_path, self._mapping)
        except Exception:
            pass

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
