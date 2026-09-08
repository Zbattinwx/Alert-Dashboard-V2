"""
Meso routes.

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
from fastapi.responses import JSONResponse
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter()


def _meso_service():
    try:
        from ..services.mesoanalysis_service import get_mesoanalysis_service
    except ImportError:
        from backend.services.mesoanalysis_service import get_mesoanalysis_service
    try:
        from ..services.hrrr_field_service import get_hrrr_field_service
    except ImportError:
        from backend.services.hrrr_field_service import get_hrrr_field_service
    if not get_hrrr_field_service().available:
        raise HTTPException(status_code=503, detail="Mesoanalysis unavailable (eccodes/scipy missing)")
    return get_mesoanalysis_service()


@router.get("/api/meso/analysis")
async def get_meso_analysis(run: str | None = None):
    """Threat assessment + cycle-over-cycle trends for a RAP analysis cycle.
    Defaults to the newest cycle that has data."""
    svc = _meso_service()
    data = await asyncio.to_thread(svc.analysis, run)
    if data is None:
        raise HTTPException(status_code=503, detail="No RAP analysis available yet")
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=120"})
@router.get("/api/meso/zones")
async def get_meso_zones(run: str | None = None):
    """Threat + watch areas as GeoJSON polygons (properties: kind, threat, level)."""
    svc = _meso_service()
    data = await asyncio.to_thread(svc.zones, run)
    if data is None:
        raise HTTPException(status_code=503, detail="No RAP analysis available yet")
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=120"})
@router.get("/api/meso/point")
async def get_meso_point(lat: float, lon: float, run: str | None = None):
    """Every mesoanalysis parameter at a point, plus the threats covering it."""
    svc = _meso_service()
    data = await asyncio.to_thread(svc.point, lat, lon, run)
    if data is None:
        raise HTTPException(status_code=404, detail="Point outside the analysis domain")
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=120"})
