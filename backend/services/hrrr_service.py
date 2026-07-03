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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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

# Open-Meteo HRRR pressure levels for exact-point soundings (surface→up).
OM_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500,
             450, 400, 350, 300, 250, 200, 150, 100, 70, 50]

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
    # Bounded LRU: entries are several-hundred-KB PNGs and TTL was only checked
    # on READ, so distinct stations / clicked points accumulated forever.
    _CACHE_CAP = 30
    _POINT_CACHE_CAP = 40

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._coords: Optional[np.ndarray] = None  # (N, 2) lat, lon
        self._loaded = False
        self._lock = threading.Lock()  # serialize matplotlib + SounderPy (not thread-safe)
        self._cache: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()  # station -> (ts, png, run)
        self._point_cache: "OrderedDict[str, tuple[float, bytes]]" = OrderedDict()  # latlon/valid -> (ts, png)
        # SounderPy renders take ~30 s; a dedicated small pool keeps them from
        # starving the loop's default executor (which also serves MRMS/obs).
        self.render_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sounding")

    @staticmethod
    def _cache_put(cache: "OrderedDict", key, value, cap: int) -> None:
        now = time.time()
        for k in [k for k, v in cache.items() if now - v[0] >= CACHE_TTL_S]:
            del cache[k]
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > cap:
            cache.popitem(last=False)

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
    def get_sounding_png(self, lat: float, lon: float, fhour: int = 0) -> tuple[bytes, str]:
        """Return (png_bytes, station_id). Raises if no nearby station has data."""
        import os
        import tempfile
        from datetime import datetime, timedelta, timezone

        import sounderpy as spy

        _patch_sounderpy_units()
        # Forecast hours past 18 only exist in the extended 00/06/12/18Z runs, so
        # target the latest extended run (≈2 h production lag) for those.
        run_kwargs: dict = {}
        if fhour > 18:
            t = datetime.now(timezone.utc) - timedelta(hours=2)
            rdt = t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
            run_kwargs = dict(run_year=rdt.strftime("%Y"), run_month=rdt.strftime("%m"),
                              run_day=rdt.strftime("%d"), run_hour=rdt.strftime("%H"))
        with self._lock:
            stations = self._nearest(lat, lon, k=5)
            if not stations:
                raise RuntimeError("no BUFKIT stations available")

            last_err: Optional[Exception] = None
            for stn in stations:
                sid = stn.lower()
                # serve a recent cached render for this station/run
                cached = self._cache.get(f"{sid}:{fhour}")
                if cached and (time.time() - cached[0]) < CACHE_TTL_S:
                    self._cache.move_to_end(f"{sid}:{fhour}")
                    return cached[1], stn

                try:
                    t0 = time.time()
                    clean = spy.get_bufkit_data("hrrr", sid, fhour, hush=True, **run_kwargs)
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
                    self._cache_put(self._cache, f"{sid}:{fhour}", (time.time(), png, run), self._CACHE_CAP)
                    logger.info(
                        f"HRRR sounding: {stn} ({len(png)} bytes) in {time.time() - t0:.0f}s"
                    )
                    return png, stn
                except Exception as e:  # station may not be in the HRRR BUFKIT set
                    last_err = e
                    logger.debug(f"HRRR sounding: {stn} failed: {e}")
                    continue

            raise RuntimeError(f"no HRRR BUFKIT data near {lat:.2f},{lon:.2f}: {last_err}")

    def get_point_sounding_png(self, lat: float, lon: float, fhour: int = 0, run: Optional[str] = None) -> tuple[bytes, str]:
        """Exact-point HRRR sounding: pull the pressure-level profile at the exact
        lat/lon from Open-Meteo's HRRR, assemble a SounderPy clean_data dict, and
        render the full plot. run=YYYYMMDDHH (model-active forecast); None = F00 now.
        Open-Meteo serves the latest run, valid-time aligned."""
        import os
        import tempfile
        from datetime import datetime, timedelta, timezone

        import matplotlib.pyplot as plt
        import sounderpy as spy
        from metpy.calc import dewpoint_from_relative_humidity, wind_components
        from metpy.units import units

        _patch_sounderpy_units()

        if run:
            run_dt = datetime(int(run[:4]), int(run[4:6]), int(run[6:8]), int(run[8:10]), tzinfo=timezone.utc)
        else:
            run_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        valid_dt = run_dt + timedelta(hours=fhour)  # fhour applies with or without an explicit run
        fhr = int(fhour)
        ckey = f"{round(lat, 2)},{round(lon, 2)}@{valid_dt.strftime('%Y%m%d%H')}"

        with self._lock:
            cached = self._point_cache.get(ckey)
            if cached and (time.time() - cached[0]) < CACHE_TTL_S:
                self._point_cache.move_to_end(ckey)
                return cached[1], ckey

        # Fetch the exact-point profile (all levels + surface) for the valid hour.
        pl = []
        for L in OM_LEVELS:
            pl += [f"temperature_{L}hPa", f"relative_humidity_{L}hPa", f"wind_speed_{L}hPa",
                   f"wind_direction_{L}hPa", f"geopotential_height_{L}hPa"]
        sfc = ["surface_pressure", "temperature_2m", "dewpoint_2m", "wind_speed_10m", "wind_direction_10m"]
        vh = valid_dt.strftime("%Y-%m-%dT%H:00")
        url = (f"https://api.open-meteo.com/v1/gfs?latitude={lat}&longitude={lon}"
               f"&models=ncep_hrrr_conus&hourly={','.join(pl + sfc)}"
               f"&temperature_unit=celsius&wind_speed_unit=kn&start_hour={vh}&end_hour={vh}")
        resp = requests.get(url, timeout=25)
        resp.raise_for_status()
        j = resp.json()
        h = j.get("hourly", {})
        elev = float(j.get("elevation", 0) or 0)

        def val(k):
            a = h.get(k)
            return a[0] if a and a[0] is not None else None

        sfc_p = val("surface_pressure")
        if sfc_p is None:
            raise RuntimeError("no HRRR data at this point")

        # Surface first, then pressure levels above ground (p < surface pressure).
        p_a, z_a, T_a, Td_a, ws_a, wd_a = [sfc_p], [elev], [val("temperature_2m")], [val("dewpoint_2m")], [val("wind_speed_10m") or 0.0], [val("wind_direction_10m") or 0.0]
        for L in OM_LEVELS:
            if L >= sfc_p:
                continue
            t, rh, z = val(f"temperature_{L}hPa"), val(f"relative_humidity_{L}hPa"), val(f"geopotential_height_{L}hPa")
            ws, wd = val(f"wind_speed_{L}hPa"), val(f"wind_direction_{L}hPa")
            if None in (t, rh, z, ws, wd):
                continue
            td = float(dewpoint_from_relative_humidity(t * units.degC, max(1.0, rh) * units.percent).m)
            p_a.append(float(L)); z_a.append(z); T_a.append(t); Td_a.append(min(td, t)); ws_a.append(ws); wd_a.append(wd)
        if len(p_a) < 5:
            raise RuntimeError("insufficient HRRR levels at this point")

        u = wind_components(np.array(ws_a) * units.kts, np.array(wd_a) * units.degrees)[0].m
        v = wind_components(np.array(ws_a) * units.kts, np.array(wd_a) * units.degrees)[1].m
        clean = {
            "p": np.array(p_a) * units.hPa,
            "z": np.array(z_a) * units.meter,
            "T": np.array(T_a) * units.degC,
            "Td": np.array(Td_a) * units.degC,
            "u": np.array(u) * units.kts,
            "v": np.array(v) * units.kts,
            "omega": np.zeros(len(p_a)) * units("Pa/s"),
            "site_info": {
                "site-id": "PT", "site-name": "Point", "site-lctn": "HRRR",
                "site-latlon": [round(lat, 2), round(lon, 2)], "site-elv": int(elev),
                "source": "HRRR POINT (Open-Meteo)", "model": "HRRR", "fcst-hour": f"F{fhr:02d}",
                "run-time": [run_dt.strftime("%Y"), run_dt.strftime("%m"), run_dt.strftime("%d"), run_dt.strftime("%H")],
                "valid-time": [valid_dt.strftime("%Y"), valid_dt.strftime("%m"), valid_dt.strftime("%d"), valid_dt.strftime("%H")],
            },
        }
        clean["titles"] = {
            "top_title": f"HRRR POINT FORECAST | {run_dt.strftime('%H')}Z HRRR F{fhr:02d}",
            "left_title": f"VALID: {valid_dt.strftime('%m/%d/%Y %HZ')}  |  RUN: {run_dt.strftime('%m/%d/%Y %HZ')}",
            "right_title": f"{round(lat, 2)}, {round(lon, 2)}    ",
        }

        with self._lock:
            tmp = tempfile.NamedTemporaryFile(prefix="ptsnd_", suffix="", delete=False)
            path = tmp.name
            tmp.close()
            common = dict(style="full", dark_mode=True, storm_motion="right_moving", radar=None, save=True, filename=path)
            try:
                spy.build_sounding(clean, **common)
            except Exception as parcel_err:
                logger.debug(f"point sounding parcel render failed ({parcel_err}); retry simple")
                plt.close("all")
                spy.build_sounding(clean, special_parcels="simple", **common)
            png_path = path + ".png"
            with open(png_path, "rb") as fh:
                png = fh.read()
            try:
                os.unlink(png_path)
            except OSError:
                pass
            self._cache_put(self._point_cache, ckey, (time.time(), png), self._POINT_CACHE_CAP)
        logger.info(f"HRRR point sounding {ckey} ({len(png)} bytes)")
        return png, ckey


_service: Optional[HRRRSoundingService] = None


def get_hrrr_service() -> HRRRSoundingService:
    global _service
    if _service is None:
        _service = HRRRSoundingService()
    return _service
