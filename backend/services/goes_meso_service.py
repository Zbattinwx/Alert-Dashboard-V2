"""
GOES mesoscale sector imagery, reprojected to web-mercator for the radar app.

Reads the latest 1-minute ABI L2 CMIP mesoscale file from the public NOAA GOES
S3 buckets (noaa-goes19 = GOES-East, noaa-goes18 = GOES-West) for a sector
(M1/M2) and band (C13 Clean IR or C02 Visible), reprojects the geostationary
fixed grid to EPSG:3857, colorizes, and caches a PNG + lat/lon bbox per combo.

Lazy: a combo is (re)fetched on request when its cache is older than the TTL.
Heavy deps (boto3/xarray/pyproj/PIL) are imported inside the worker so the
module loads even where they're absent, and startup stays fast.
"""

import asyncio
import datetime
import io
import logging
import re
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_BUCKETS = {"east": "noaa-goes19", "west": "noaa-goes18"}
_BANDS = {"ir": "13", "visible": "02"}
_TTL_SEC = 55  # meso refreshes ~1 min — refetch when the cache is older than this
_OUT_SIZE = 1000  # reprojected PNG edge length (px)


def _colorize_ir(bt: np.ndarray) -> np.ndarray:
    """Clean IR → RGBA: grayscale for warm clouds/ground, a color enhancement for
    cold tops (< -30 C: green → yellow → red → magenta → white), transparent where
    there's no data."""
    finite = np.isfinite(bt)
    btc = np.where(finite, bt - 273.15, 0.0)  # Celsius
    gray = np.clip((30.0 - btc) / 60.0, 0, 1) * 255.0  # +30C→black, -30C→white
    R = gray.copy()
    G = gray.copy()
    B = gray.copy()
    cold = finite & (btc < -30.0)
    # stops (°C, RGB), warm→cold; np.interp needs ascending x so we reverse.
    cs = np.array([-80, -70, -60, -50, -40, -30], float)
    rs = np.array([255, 255, 255, 255, 0, 255], float)
    gs = np.array([255, 0, 0, 255, 200, 255], float)
    bs = np.array([255, 255, 0, 0, 0, 255], float)
    bc = btc[cold]
    R[cold] = np.interp(bc, cs, rs)
    G[cold] = np.interp(bc, cs, gs)
    B[cold] = np.interp(bc, cs, bs)
    a = np.where(finite, 255, 0).astype("uint8")
    return np.dstack([R.astype("uint8"), G.astype("uint8"), B.astype("uint8"), a])


def _colorize_visible(refl: np.ndarray) -> np.ndarray:
    """Visible reflectance → grayscale RGBA (mild gamma); transparent where no data
    (dark at night is expected — there is no visible signal)."""
    finite = np.isfinite(refl)
    v = np.clip(np.where(finite, refl, 0.0), 0, 1) ** 0.7 * 255.0
    g = v.astype("uint8")
    a = np.where(finite, 255, 0).astype("uint8")
    return np.dstack([g, g, g, a])


def _parse_time(key: str) -> str:
    """ABI filename scan-start (…_sYYYYDDDHHMMSS…) → ISO Z."""
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    if not m:
        return ""
    yr, doy, hh, mm, ss = (int(x) for x in m.groups())
    dt = datetime.datetime(yr, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
        days=doy - 1, hours=hh, minutes=mm, seconds=ss
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class GoesMesoService:
    def __init__(self) -> None:
        self._cache: dict = {}  # (sat,sector,band) -> {png,bbox,time,fetched}
        self._locks: dict = {}
        self._s3 = None

    def _client(self):
        if self._s3 is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config

            self._s3 = boto3.client(
                "s3", config=Config(signature_version=UNSIGNED, max_pool_connections=8)
            )
        return self._s3

    async def get(self, sat: str, sector: str, band: str) -> Optional[dict]:
        """Return {png, bbox:[w,s,e,n], time} for a combo, refetching past the TTL.
        Serves the stale entry on a transient fetch failure."""
        if sat not in _BUCKETS or sector not in ("1", "2") or band not in _BANDS:
            return None
        key = (sat, sector, band)
        ent = self._cache.get(key)
        if ent and time.time() - ent["fetched"] < _TTL_SEC:
            return ent
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            ent = self._cache.get(key)
            if ent and time.time() - ent["fetched"] < _TTL_SEC:
                return ent
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(None, self._fetch_reproject, sat, sector, band)
            except Exception as e:  # noqa: BLE001
                logger.warning("GOES meso fetch failed %s: %s", key, e)
                return self._cache.get(key)
            if result:
                result["fetched"] = time.time()
                self._cache[key] = result
            return self._cache.get(key)

    def _latest_key(self, sat: str, sector: str, band: str):
        s3 = self._client()
        bucket = _BUCKETS[sat]
        bn = _BANDS[band]
        now = datetime.datetime.now(datetime.timezone.utc)
        for back in range(0, 3):
            t = now - datetime.timedelta(hours=back)
            prefix = f"ABI-L2-CMIPM/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
            keys: list = []
            token = None
            while True:
                kw = dict(Bucket=bucket, Prefix=prefix, MaxKeys=1000)
                if token:
                    kw["ContinuationToken"] = token
                r = s3.list_objects_v2(**kw)
                keys += [
                    o["Key"] for o in r.get("Contents", []) if f"CMIPM{sector}-M6C{bn}_" in o["Key"]
                ]
                if not r.get("IsTruncated"):
                    break
                token = r.get("NextContinuationToken")
            if keys:
                return bucket, sorted(keys)[-1]
        return bucket, None

    def _fetch_reproject(self, sat: str, sector: str, band: str) -> Optional[dict]:
        import xarray as xr
        from pyproj import CRS, Transformer
        from PIL import Image

        bucket, key = self._latest_key(sat, sector, band)
        if not key:
            return None
        raw = self._client().get_object(Bucket=bucket, Key=key)["Body"].read()
        ds = xr.open_dataset(io.BytesIO(raw), engine="h5netcdf")
        try:
            cmi = ds["CMI"].values.astype("float32")
            P = ds["goes_imager_projection"]
            H = float(P.perspective_point_height)
            lon0 = float(P.longitude_of_projection_origin)
            a = float(P.semi_major_axis)
            b = float(P.semi_minor_axis)
            sweep = str(P.sweep_angle_axis)
            xm = ds["x"].values * H  # geostationary projection meters
            ym = ds["y"].values * H
        finally:
            ds.close()

        geos = CRS.from_proj4(
            f"+proj=geos +h={H} +lon_0={lon0} +a={a} +b={b} +sweep={sweep} +units=m +no_defs"
        )
        # sector lat/lon bbox from the data edges
        t_ll = Transformer.from_crs(geos, 4326, always_xy=True)
        ex = np.concatenate([xm, xm, np.full_like(ym, xm[0]), np.full_like(ym, xm[-1])])
        ey = np.concatenate([np.full_like(xm, ym[0]), np.full_like(xm, ym[-1]), ym, ym])
        lon, lat = t_ll.transform(ex, ey)
        g = np.isfinite(lon) & np.isfinite(lat)
        west, east = float(np.nanmin(lon[g])), float(np.nanmax(lon[g]))
        south, north = float(np.nanmin(lat[g])), float(np.nanmax(lat[g]))

        # resample to a web-mercator grid over the bbox
        t_ll2m = Transformer.from_crs(4326, 3857, always_xy=True)
        wx, sy = t_ll2m.transform(west, south)
        ex2, ny = t_ll2m.transform(east, north)
        mx = np.linspace(wx, ex2, _OUT_SIZE)
        my = np.linspace(ny, sy, _OUT_SIZE)  # top → bottom
        MX, MY = np.meshgrid(mx, my)
        t_m2g = Transformer.from_crs(3857, geos, always_xy=True)
        GX, GY = t_m2g.transform(MX.ravel(), MY.ravel())
        GX = GX.reshape(_OUT_SIZE, _OUT_SIZE)
        GY = GY.reshape(_OUT_SIZE, _OUT_SIZE)
        ci = np.round((GX - xm[0]) / (xm[1] - xm[0])).astype(int)
        ri = np.round((GY - ym[0]) / (ym[1] - ym[0])).astype(int)
        valid = (
            np.isfinite(GX)
            & np.isfinite(GY)
            & (ci >= 0)
            & (ci < len(xm))
            & (ri >= 0)
            & (ri < len(ym))
        )
        out = np.full((_OUT_SIZE, _OUT_SIZE), np.nan, "float32")
        out[valid] = cmi[ri[valid], ci[valid]]

        rgba = _colorize_ir(out) if band == "ir" else _colorize_visible(out)
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG")
        return {"png": buf.getvalue(), "bbox": [west, south, east, north], "time": _parse_time(key)}


_service: Optional[GoesMesoService] = None


def get_goes_meso_service() -> GoesMesoService:
    global _service
    if _service is None:
        _service = GoesMesoService()
    return _service
