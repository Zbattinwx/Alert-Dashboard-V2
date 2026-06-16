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

# ── Field registry (MVP; extend by adding entries) ──────────────────────────
# conv: None | "k2f"  (Kelvin → °F for surface temps).  nodata_below: pack 0
# (transparent) for values under this threshold (e.g. clear-air reflectivity).
HRRR_FIELDS: dict[str, dict] = {
    "t2m":  {"idx": ":TMP:2 m above ground:",    "label": "2 m Temperature",       "conv": "k2f", "vmin": -30.0, "vmax": 120.0, "units": "°F",  "lut": "hrrr_temp"},
    "td2m": {"idx": ":DPT:2 m above ground:",    "label": "2 m Dew Point",         "conv": "k2f", "vmin": -30.0, "vmax": 90.0,  "units": "°F",  "lut": "hrrr_dewpoint"},
    "refc": {"idx": ":REFC:entire atmosphere:",  "label": "Composite Reflectivity","conv": None,  "vmin": -20.0, "vmax": 80.0,  "units": "dBZ", "lut": "reflectivity", "nodata_below": 5.0},
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


def _k2f(k: np.ndarray) -> np.ndarray:
    return (k - 273.15) * 9.0 / 5.0 + 32.0


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
    def _key(date: str, hh: int, fhour: int) -> str:
        return f"hrrr.{date}/conus/hrrr.t{hh:02d}z.wrfsfcf{fhour:02d}.grib2"

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
             "vmin": v["vmin"], "vmax": v["vmax"]}
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
        date, hh = run[:8], int(run[8:10])
        key = self._key(date, hh, fhour)
        grib = self._fetch_message(key, spec["idx"])
        if grib is None:
            return None
        values, lats, lons, ni, nj = self._decode(grib)
        self._ensure_mapping(lats, lons, ni, nj)
        if spec["conv"] == "k2f":
            values = _k2f(values)
        grid = values[self._mapping].reshape(T_NJ, T_NI).astype(np.float32)
        return self._encode(grid, float(spec["vmin"]), float(spec["vmax"]), spec.get("nodata_below"))

    def _fetch_message(self, key: str, idx_match: str) -> Optional[bytes]:
        s3 = self._get_s3()
        idx = s3.get_object(Bucket=HRRR_BUCKET, Key=key + ".idx")["Body"].read().decode("utf-8", "replace")
        lines = idx.splitlines()
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
        return s3.get_object(Bucket=HRRR_BUCKET, Key=key, Range=rng)["Body"].read()

    def _decode(self, grib: bytes):
        # Decode straight from the in-memory message (no temp file → avoids
        # Windows file locking and is faster).
        import eccodes
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

    def _ensure_mapping(self, lats: np.ndarray, lons: np.ndarray, ni: int, nj: int) -> None:
        sig = f"{ni}x{nj}"
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
