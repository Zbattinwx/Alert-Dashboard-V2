"""Official WPC surface analysis — fronts + High/Low centers.

Fetches the WPC Coded Surface Frontal Positions bulletin (CODSUS / ASUS01 KWBC)
from IEM's AFOS archive and parses it. Coordinates are encoded as a single token
per point: latitude = first two digits, longitude = the remaining digits (°W,
negated). Front lines carry an optional intensity token (WK/MDT/STG).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil=CODSUS&limit=1&fmt=text"
CACHE_TTL = 1800  # 30 min (WPC issues ~4×/day + intermediates)

_KEYWORDS = {"HIGHS", "LOWS", "COLD", "WARM", "STNRY", "OCFNT", "TROF"}
_INTENS = {"WK", "MDT", "STG"}
_FTYPE = {"COLD": "cold", "WARM": "warm", "STNRY": "stationary", "OCFNT": "occluded", "TROF": "trough"}


def _coord(tok: str):
    # lat = first 2 digits, lon = remaining digits (°W → negative)
    lat = int(tok[:2])
    lon = -int(tok[2:])
    if not (10 <= lat <= 85) or not (-180 <= lon <= -40):
        raise ValueError("out of range")
    return lat, lon


def _parse(text: str) -> dict:
    toks = text.split()
    i = 0
    while i < len(toks) and toks[i] not in _KEYWORDS:
        i += 1
    highs: list[dict] = []
    lows: list[dict] = []
    fronts: list[dict] = []
    while i < len(toks):
        kw = toks[i]
        i += 1
        seg: list[str] = []
        while i < len(toks) and toks[i] not in _KEYWORDS:
            seg.append(toks[i])
            i += 1
        if kw in ("HIGHS", "LOWS"):
            for k in range(0, len(seg) - 1, 2):
                try:
                    p = int(seg[k])
                    la, lo = _coord(seg[k + 1])
                    (highs if kw == "HIGHS" else lows).append(
                        {"type": "H" if kw == "HIGHS" else "L", "pressure": p, "lat": la, "lon": lo})
                except Exception:
                    continue
        else:
            pts = seg[1:] if seg and seg[0] in _INTENS else seg
            coords = []
            for tok in pts:
                try:
                    la, lo = _coord(tok)
                    coords.append([lo, la])
                except Exception:
                    continue
            if len(coords) >= 2:
                fronts.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                               "properties": {"ftype": _FTYPE.get(kw, "front")}})
    return {"highs": highs, "lows": lows, "fronts": {"type": "FeatureCollection", "features": fronts}}


class WPCFrontsService:
    def __init__(self) -> None:
        self._cache: Optional[dict] = None
        self._ts = 0.0

    def get(self) -> dict:
        now = time.time()
        if self._cache is not None and now - self._ts < CACHE_TTL:
            return self._cache
        try:
            import requests
            r = requests.get(URL, timeout=20, headers={"User-Agent": "TBF-Radar"})
            r.raise_for_status()
            self._cache = _parse(r.text)
        except Exception as e:
            logger.warning("WPC fronts fetch/parse failed: %s", e)
            if self._cache is None:
                self._cache = {"highs": [], "lows": [], "fronts": {"type": "FeatureCollection", "features": []}}
        self._ts = now
        return self._cache


_svc: Optional[WPCFrontsService] = None


def get_wpc_fronts_service() -> WPCFrontsService:
    global _svc
    if _svc is None:
        _svc = WPCFrontsService()
    return _svc
