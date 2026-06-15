"""HRRR point-sounding service.

Renders a full SHARPpy-style sounding (Skew-T + hodograph + SPC parameter suite)
for a clicked lat/lon using SounderPy's `build_sounding`. Real-time HRRR profiles
come from PSU BUFKIT (fcst hour 0 = the latest run's analysis), which SounderPy
fetches and processes with correct surface levels, wind handling, and derived
indices — far simpler and more correct than decoding raw GRIB ourselves. The
click snaps to the nearest BUFKIT site (master list with coords). The radar app
displays the returned PNG.

(HRRR *field layers* — gridded display — will need the raw run cached; that's a
separate service. This one is point soundings only.)
"""

import matplotlib
matplotlib.use("Agg")  # headless render; must precede pyplot/sounderpy import

import io
import logging
import threading
import time
from typing import Optional

import numpy as np
import requests

logger = logging.getLogger(__name__)

BUFKIT_MASTER_URL = (
    "https://raw.githubusercontent.com/kylejgillett/sounderpy/main/src/BUFKIT-STATIONS-MASTER.txt"
)
# CONUS bounds — restrict candidates to sites PSU runs HRRR BUFKIT for.
CONUS = (24.0, 50.0, -125.0, -66.0)  # south, north, west, east
CACHE_TTL_S = 1800  # reuse a station's PNG within a run window

_patched = False


def _patch_sounderpy_units() -> None:
    """Work around a SounderPy bug: the default parcel-trace path calls
    `density_temperature(...)` with no guard, and `calc_ecape_parcel` returns a
    unit-stripped temperature for some (often stable) profiles → ValueError →
    the whole sounding 503s. Re-attach the expected units so it renders (parcel
    line falls back to NaN/absent for genuinely broken parcels)."""
    global _patched
    if _patched:
        return
    try:
        import sounderpy.plot as spyplot
        from metpy.units import units as mu

        orig = spyplot.density_temperature

        def _fix_temp(x):
            # The bad trace temperature is a Quantity carrying *dimensionless*
            # units (the "none" in the error), or a bare array. The parcel trace
            # temps are Kelvin — strip the bogus unit and reattach kelvin.
            u = getattr(x, "units", None)
            if u is None:
                return x * mu.kelvin
            if str(u) == "dimensionless":
                return x.magnitude * mu.kelvin
            return x

        def safe_density_temperature(pressure, temperature, mixing_ratio):
            return orig(pressure, _fix_temp(temperature), mixing_ratio)

        spyplot.density_temperature = safe_density_temperature
        _patched = True
    except Exception as e:  # non-fatal — soundings just keep their existing behavior
        logger.warning(f"HRRR: could not patch sounderpy units: {e}")


class HRRRSoundingService:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._coords: Optional[np.ndarray] = None  # (N, 2) lat, lon
        self._loaded = False
        self._lock = threading.Lock()  # serialize matplotlib + SounderPy (not thread-safe)
        self._cache: dict[str, tuple[float, bytes, str]] = {}  # station -> (ts, png, run)

    # ── station index ──────────────────────────────────────────────────────
    def _ensure_stations(self) -> None:
        if self._loaded:
            return
        import pandas as pd

        resp = requests.get(BUFKIT_MASTER_URL, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), skiprows=7, skipinitialspace=True)
        df = df[["LAT", "LON", "ID"]].dropna()
        s, n, w, e = CONUS
        df = df[(df.LAT >= s) & (df.LAT <= n) & (df.LON >= w) & (df.LON <= e)]
        self._ids = [str(x).strip() for x in df["ID"].tolist()]
        self._coords = df[["LAT", "LON"]].to_numpy(dtype=float)
        self._loaded = True
        logger.info(f"HRRR sounding: loaded {len(self._ids)} CONUS BUFKIT stations")

    def _nearest(self, lat: float, lon: float, k: int = 8) -> list[str]:
        self._ensure_stations()
        if self._coords is None or len(self._ids) == 0:
            return []
        dlat = self._coords[:, 0] - lat
        dlon = (self._coords[:, 1] - lon) * np.cos(np.radians(lat))
        d2 = dlat * dlat + dlon * dlon
        out: list[str] = []
        seen: set[str] = set()
        for i in np.argsort(d2):  # nearest first; master list has duplicate rows per site
            sid = self._ids[i]
            if sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
            if len(out) >= k:
                break
        return out

    # ── sounding render ────────────────────────────────────────────────────
    def get_sounding_png(self, lat: float, lon: float) -> tuple[bytes, str]:
        """Return (png_bytes, station_id). Raises if no nearby station has data."""
        import os
        import tempfile

        import sounderpy as spy

        _patch_sounderpy_units()
        with self._lock:
            stations = self._nearest(lat, lon, k=8)
            if not stations:
                raise RuntimeError("no BUFKIT stations available")

            last_err: Optional[Exception] = None
            for stn in stations:
                sid = stn.lower()
                # serve a recent cached render for this station/run
                cached = self._cache.get(sid)
                if cached and (time.time() - cached[0]) < CACHE_TTL_S:
                    return cached[1], stn

                try:
                    t0 = time.time()
                    clean = spy.get_bufkit_data("hrrr", sid, 0, hush=True)
                    tmp = tempfile.NamedTemporaryFile(prefix=f"snd_{sid}_", suffix="", delete=False)
                    path = tmp.name
                    tmp.close()
                    # SounderPy's ECAPE parcel trace crashes on many real profiles
                    # (unit-stripped temp / qv, null storm motion). Try the full
                    # render with the parcel; on ANY error retry with
                    # special_parcels='simple', which skips the parcel-trace path
                    # entirely so the sounding still renders (params/hodograph
                    # intact, just no dashed parcel line).
                    import matplotlib.pyplot as plt

                    common = dict(
                        style="full",
                        dark_mode=True,
                        storm_motion="right_moving",
                        radar=None,  # skip the radar-mosaic inset (network + slow)
                        save=True,
                        filename=path,
                    )
                    try:
                        spy.build_sounding(clean, **common)
                    except Exception as parcel_err:
                        logger.debug(f"HRRR {stn}: parcel render failed ({parcel_err}); retry w/o parcel")
                        plt.close("all")
                        spy.build_sounding(clean, special_parcels="simple", **common)
                    png_path = path + ".png"
                    with open(png_path, "rb") as fh:
                        png = fh.read()
                    try:
                        os.unlink(png_path)
                    except OSError:
                        pass
                    run = "-".join(clean.get("site_info", {}).get("run-time", []))
                    self._cache[sid] = (time.time(), png, run)
                    logger.info(
                        f"HRRR sounding: {stn} ({len(png)} bytes) in {time.time() - t0:.0f}s"
                    )
                    return png, stn
                except Exception as e:  # station may not be in the HRRR BUFKIT set
                    last_err = e
                    logger.debug(f"HRRR sounding: {stn} failed: {e}")
                    continue

            raise RuntimeError(f"no HRRR BUFKIT data near {lat:.2f},{lon:.2f}: {last_err}")


_service: Optional[HRRRSoundingService] = None


def get_hrrr_service() -> HRRRSoundingService:
    global _service
    if _service is None:
        _service = HRRRSoundingService()
    return _service
