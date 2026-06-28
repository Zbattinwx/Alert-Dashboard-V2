"""Surface observations + objective analysis for the radar app's Observations layer.

Fetches a national METAR snapshot (aviationweather.gov), serves the station obs
for plotting, and runs an objective surface analysis on them using standard
methods:
  • High/Low pressure centers — local extrema of the gridded (altimeter) pressure
    field. Reliable.
  • Fronts — thermal-gradient zones (|∇T|) classified warm/cold/stationary by
    temperature advection (−V·∇T). Objective + approximate (automated frontal
    analysis from surface data alone is inherently noisy).

Needs numpy + scipy + contourpy (already bundled for HRRR/MRMS).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

METAR_BASE = "https://aviationweather.gov/api/data/metar?format=json"
# aviationweather.gov caps a METAR query at ~400 stations, so the national bbox
# is sparse (most stations missing). Use it ONLY for the synoptic H/L + fronts
# analysis (gridded/smoothed — sparse is fine); the station PLOTS + card fetch a
# site-centered bbox instead, which returns the full dense local set.
NATIONAL_BBOX = (23.0, -126.0, 50.0, -66.0)  # S, W, N, E
DEFAULT_RADIUS_KM = 400.0
CACHE_TTL = 300  # 5 min


def _bbox_for(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """S, W, N, E box of ~radius_km around (lat, lon)."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)

# Analysis grid (regular lat/lon over CONUS).
G_W, G_E, G_S, G_N = -125.0, -66.5, 23.5, 50.0
G_RES = 0.5
GX = np.arange(G_W, G_E + 1e-6, G_RES)
GY = np.arange(G_S, G_N + 1e-6, G_RES)
MESH_X, MESH_Y = np.meshgrid(GX, GY)


class SurfaceObsService:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[list[dict], float]] = {}  # bbox key → (obs, ts)
        self._analysis: Optional[dict] = None
        self._analysis_ts = 0.0

    # ── Fetch ───────────────────────────────────────────────────────────────
    def _fetch_bbox(self, s: float, w: float, n: float, e: float) -> list[dict]:
        import requests  # bundled (used elsewhere)
        url = f"{METAR_BASE}&bbox={s:.2f},{w:.2f},{n:.2f},{e:.2f}"
        r = requests.get(url, timeout=20, headers={"User-Agent": "TBF-Radar"})
        r.raise_for_status()
        out = []
        for o in r.json():
            lat, lon = o.get("lat"), o.get("lon")
            if lat is None or lon is None:
                continue
            t = o.get("temp")
            out.append({
                "id": o.get("icaoId", ""),
                "name": o.get("name", ""),
                "lat": float(lat),
                "lon": float(lon),
                "tempC": None if t is None else float(t),
                "dewpC": None if o.get("dewp") is None else float(o["dewp"]),
                "wdir": None if not isinstance(o.get("wdir"), (int, float)) else float(o["wdir"]),
                "wspd": None if o.get("wspd") is None else float(o["wspd"]),  # kt
                "gust": None if o.get("wgst") is None else float(o["wgst"]),
                "altim": None if o.get("altim") is None else float(o["altim"]),  # hPa (sea-level)
                "elev": None if o.get("elev") is None else float(o["elev"]),  # m
                "visib": o.get("visib"),          # statute miles (number, or "10+")
                "clouds": o.get("clouds") or [],  # [{cover, base(ft)}] — for ceiling
                "wx": o.get("wxString"),          # present weather (e.g. "-RA BR")
                "fltCat": o.get("fltCat"),        # VFR/MVFR/IFR/LIFR (AWC-computed)
                "raw": o.get("rawOb"),            # raw METAR text
                "obsTime": o.get("obsTime"),      # epoch seconds
            })
        return out

    def _cached(self, key: str, fetch) -> list[dict]:
        now = time.time()
        ent = self._cache.get(key)
        if ent and now - ent[1] < CACHE_TTL:
            return ent[0]
        try:
            obs = fetch()
            self._cache[key] = (obs, now)
            if len(self._cache) > 24:  # bound (a handful of sites + national)
                self._cache.pop(min(self._cache, key=lambda k: self._cache[k][1]), None)
            return obs
        except Exception as e:
            logger.warning("surface obs fetch failed (%s): %s", key, e)
            return ent[0] if ent else []

    def get_obs(self, lat: Optional[float] = None, lon: Optional[float] = None,
                radius_km: float = DEFAULT_RADIUS_KM) -> list[dict]:
        """Station obs. With lat/lon → a dense site-centered box (for plots/cards);
        without → the national set (sparse, for the analysis)."""
        if lat is None or lon is None:
            return self._national()
        s, w, n, e = _bbox_for(float(lat), float(lon), float(radius_km))
        key = f"{round(float(lat), 1)},{round(float(lon), 1)},{int(radius_km)}"
        return self._cached(key, lambda: self._fetch_bbox(s, w, n, e))

    def _national(self) -> list[dict]:
        return self._cached("natl", lambda: self._fetch_bbox(*NATIONAL_BBOX))

    # ── Objective analysis ──────────────────────────────────────────────────
    def get_analysis(self) -> dict:
        now = time.time()
        if self._analysis is not None and now - self._analysis_ts < CACHE_TTL:
            return self._analysis
        obs = self._national()  # synoptic-scale → national set (sparse is fine)
        try:
            self._analysis = self._analyze(obs)
        except Exception as e:
            logger.warning("surface analysis failed: %s", e)
            self._analysis = {"highs": [], "lows": [], "fronts": {"type": "FeatureCollection", "features": []}}
        self._analysis_ts = now
        return self._analysis

    def _analyze(self, obs: list[dict]) -> dict:
        from scipy.interpolate import griddata
        from scipy.ndimage import gaussian_filter, maximum_filter, minimum_filter

        # Pressure field: only low-elevation stations — sea-level reduction over
        # high terrain (>1000 m) is unreliable and creates spurious H/L.
        pres = [(o["lon"], o["lat"], o["altim"]) for o in obs
                if o.get("altim") is not None and (o.get("elev") or 0) < 1000]
        temp = [(o["lon"], o["lat"], o["tempC"]) for o in obs if o.get("tempC") is not None]
        if len(pres) < 30 or len(temp) < 30:
            return {"highs": [], "lows": [], "fronts": {"type": "FeatureCollection", "features": []}}

        pp = np.array(pres)
        tp = np.array(temp)
        # Linear interp (NaN outside the data hull) + nearest fill for smoothing.
        P_lin = griddata(pp[:, :2], pp[:, 2], (MESH_X, MESH_Y), method="linear")
        P_near = griddata(pp[:, :2], pp[:, 2], (MESH_X, MESH_Y), method="nearest")
        inside = np.isfinite(P_lin)
        P = gaussian_filter(np.where(inside, P_lin, P_near), sigma=1.6)

        # Highs are broad/flat (low prominence) — use a gentler gate than lows.
        highs = self._extrema(P, inside, maximum_filter, kind="H", prom=1.6)
        lows = self._extrema(P, inside, minimum_filter, kind="L", prom=2.4)
        fronts = self._fronts(obs, inside, griddata, gaussian_filter)
        return {"highs": highs, "lows": lows, "fronts": fronts}

    def _extrema(self, P, inside, filt, kind: str, size: int = 11, prom: float = 2.5) -> list[dict]:
        # A center must be the extremum over a wide neighborhood AND stand out from
        # the surrounding field by `prom` hPa — so a broad ridge is one H (not many)
        # and shallow wiggles / altimeter-over-terrain noise are rejected.
        ext = filt(P, size=size)
        ismax = kind == "H"
        hits = np.argwhere((P == ext) & inside)
        from scipy.ndimage import uniform_filter
        bg = uniform_filter(P, size=size * 2 + 1)
        ny, nx = P.shape
        margin = 2
        out = []
        for j, i in hits:
            if j < margin or j >= ny - margin or i < margin or i >= nx - margin:
                continue  # drop grid-edge artifacts
            d = float(P[j, i] - bg[j, i])
            if (ismax and d < prom) or (not ismax and d > -prom):
                continue
            out.append({"lat": round(float(MESH_Y[j, i]), 2), "lon": round(float(MESH_X[j, i]), 2),
                        "type": kind, "pressure": int(round(float(P[j, i]))), "_p": abs(d)})
        # de-duplicate: keep the most prominent center within ~6° of others
        out.sort(key=lambda h: h["_p"], reverse=True)
        kept: list[dict] = []
        for h in out:
            if all(abs(h["lat"] - k["lat"]) + abs(h["lon"] - k["lon"]) > 6.0 for k in kept):
                kept.append({k: v for k, v in h.items() if k != "_p"})
        return kept[:5]

    def _fronts(self, obs, inside, griddata, gaussian_filter) -> dict:
        from contourpy import contour_generator, LineType

        tp = np.array([(o["lon"], o["lat"], o["tempC"]) for o in obs if o.get("tempC") is not None])
        # wind components (met FROM dir → u east, v north), kt
        wo = [o for o in obs if o.get("wdir") is not None and o.get("wspd")]
        T_near = griddata(tp[:, :2], tp[:, 2], (MESH_X, MESH_Y), method="nearest")
        T = gaussian_filter(T_near, sigma=1.8)
        # gradient (per grid cell; relative units are fine for threshold + sign)
        dTy, dTx = np.gradient(T)
        gmag = np.hypot(dTx, dTy)

        # wind grid for advection (nearest)
        if wo:
            wp = np.array([[o["lon"], o["lat"]] for o in wo])
            u = np.array([-o["wspd"] * math.sin(math.radians(o["wdir"])) for o in wo])
            v = np.array([-o["wspd"] * math.cos(math.radians(o["wdir"])) for o in wo])
            U = gaussian_filter(griddata(wp, u, (MESH_X, MESH_Y), method="nearest"), sigma=1.8)
            V = gaussian_filter(griddata(wp, v, (MESH_X, MESH_Y), method="nearest"), sigma=1.8)
        else:
            U = V = np.zeros_like(T)
        # temperature advection (-V·∇T): >0 warm advection, <0 cold advection
        adv = -(U * dTx + V * dTy)

        thr = float(np.nanpercentile(gmag[inside], 88))  # strong thermal-gradient zones
        if not math.isfinite(thr) or thr <= 0:
            return {"type": "FeatureCollection", "features": []}
        cg = contour_generator(GX, GY, np.where(inside, gmag, 0.0), line_type=LineType.Separate)
        feats = []
        for arr in cg.lines(thr):
            if len(arr) < 6:
                continue
            # classify by mean advection sampled along the line
            xs = np.clip(((arr[:, 0] - G_W) / G_RES).astype(int), 0, len(GX) - 1)
            ys = np.clip(((arr[:, 1] - G_S) / G_RES).astype(int), 0, len(GY) - 1)
            a = float(np.nanmean(adv[ys, xs]))
            sd = float(np.nanstd(adv[ys, xs]))
            ftype = "stationary"
            if a < -0.6 * (sd + 1e-6):
                ftype = "cold"
            elif a > 0.6 * (sd + 1e-6):
                ftype = "warm"
            coords = [[round(float(x), 2), round(float(y), 2)] for x, y in arr[::2]]
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                          "properties": {"ftype": ftype}})
        return {"type": "FeatureCollection", "features": feats}


_svc: Optional[SurfaceObsService] = None


def get_surface_obs_service() -> SurfaceObsService:
    global _svc
    if _svc is None:
        _svc = SurfaceObsService()
    return _svc
