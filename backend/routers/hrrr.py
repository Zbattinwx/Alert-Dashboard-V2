"""
Hrrr routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from fastapi import HTTPException
from typing import Optional
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/hrrr/sounding.png")
async def get_hrrr_sounding(lat: float, lon: float, fhour: int = 0, run: Optional[str] = None):
    """Full SounderPy HRRR point sounding PNG at the EXACT lat/lon (Open-Meteo
    pressure-level profile). fhour + run (YYYYMMDDHH) give a forecast-hour
    sounding when the model is active; default is F00 (now). Falls back to the
    nearest-BUFKIT-site render if the exact-point path fails. The radar app shows
    an instant quick-look while this builds."""
    from fastapi.responses import Response as FastResponse
    from ..services.hrrr_service import get_hrrr_service

    svc = get_hrrr_service()
    loop = asyncio.get_event_loop()
    # Dedicated pool: a ~30 s SounderPy render must not occupy the default
    # executor that also serves MRMS/HRRR-field/obs offloads.
    try:
        png, key = await loop.run_in_executor(svc.render_executor, lambda: svc.get_point_sounding_png(lat, lon, fhour, run))
    except Exception as e:
        try:  # exact-point failed (beyond Open-Meteo's ~45 h horizon?) → nearest
            # BUFKIT site at the SAME forecast hour (BUFKIT reaches F48).
            png, key = await loop.run_in_executor(svc.render_executor, lambda: svc.get_sounding_png(lat, lon, fhour))
        except Exception:
            raise HTTPException(status_code=503, detail="HRRR sounding unavailable")
    return FastResponse(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Sounding-Source": key},
    )
@router.get("/api/hrrr/runs")
async def get_hrrr_runs(model: str = "hrrr", before: str | None = None):
    """Manifest for a model's field overlays (model=hrrr|rrfs): the last ~10 runs
    (with each run's max forecast hour), the available fields, and the model's
    forecast-hour offset. Lazy/cached — see hrrr_field_service.

    `before` (YYYYMMDDHH or an ISO instant) anchors the run list at a past time so
    an event review can find the runs that were current during the event instead
    of today's."""
    try:
        from ..services.hrrr_field_service import get_hrrr_field_service, MODELS
    except ImportError:
        from backend.services.hrrr_field_service import get_hrrr_field_service, MODELS
    svc = get_hrrr_field_service()
    if not svc.available:
        raise HTTPException(status_code=503, detail="HRRR fields unavailable (eccodes/scipy missing)")
    runs = await asyncio.to_thread(svc.list_runs, model, 10, before)
    mcfg = MODELS.get(model, MODELS["hrrr"])
    return {
        "runs": runs,
        "fields": svc.fields(model),
        "fhour_offset": mcfg.get("fhour_offset", 0),
        "fhour_step": mcfg.get("fhour_step", 1),
    }
@router.get("/api/hrrr/field")
async def get_hrrr_field(run: str, param: str, fhour: int = 0, model: str = "hrrr"):
    """One model field as a compact lat/lon binary grid (magic 'HRRR') for the
    app's WebGL layer. run=YYYYMMDDHH, param in the field registry, fhour int."""
    try:
        from ..services.hrrr_field_service import get_hrrr_field_service
    except ImportError:
        from backend.services.hrrr_field_service import get_hrrr_field_service
    from fastapi.responses import Response as FastResponse
    svc = get_hrrr_field_service()
    if not svc.available:
        raise HTTPException(status_code=503, detail="HRRR fields unavailable")
    data = await asyncio.to_thread(svc.get_field, model, run, param, fhour)
    if data is None:
        raise HTTPException(status_code=404, detail="HRRR field not available")
    return FastResponse(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )
@router.get("/api/hrrr/barbs")
async def get_hrrr_barbs(run: str, param: str, fhour: int = 0, model: str = "hrrr"):
    """Downsampled wind vectors for a wind field → barb plotting ([lon,lat,kt,dir])."""
    try:
        from ..services.hrrr_field_service import get_hrrr_field_service
    except ImportError:
        from backend.services.hrrr_field_service import get_hrrr_field_service
    svc = get_hrrr_field_service()
    if not svc.available:
        raise HTTPException(status_code=503, detail="HRRR fields unavailable")
    data = await asyncio.to_thread(svc.get_barbs, model, run, param, fhour)
    if data is None:
        raise HTTPException(status_code=404, detail="HRRR barbs not available")
    return data
@router.get("/api/hrrr/isobars")
async def get_hrrr_isobars(run: str, fhour: int = 0, model: str = "hrrr"):
    """MSLP isobars (GeoJSON LineStrings) for the run/forecast hour."""
    try:
        from ..services.hrrr_field_service import get_hrrr_field_service
    except ImportError:
        from backend.services.hrrr_field_service import get_hrrr_field_service
    svc = get_hrrr_field_service()
    if not svc.available:
        raise HTTPException(status_code=503, detail="HRRR fields unavailable")
    data = await asyncio.to_thread(svc.get_isobars, model, run, fhour)
    if data is None:
        raise HTTPException(status_code=404, detail="HRRR isobars not available")
    return data
@router.get("/api/hrrr/contours")
async def get_hrrr_contours(run: str, param: str, fhour: int = 0, levels: str = "75,150", model: str = "hrrr"):
    """Contour a registered model field at `levels` (GeoJSON; for the UH overlay)."""
    try:
        from ..services.hrrr_field_service import get_hrrr_field_service
    except ImportError:
        from backend.services.hrrr_field_service import get_hrrr_field_service
    svc = get_hrrr_field_service()
    if not svc.available:
        raise HTTPException(status_code=503, detail="HRRR fields unavailable")
    lv = [float(x) for x in levels.split(",") if x.strip()]
    data = await asyncio.to_thread(svc.get_contours, model, run, param, fhour, lv)
    if data is None:
        raise HTTPException(status_code=404, detail="HRRR contours not available")
    return data
