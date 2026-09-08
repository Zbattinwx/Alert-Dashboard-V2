"""
Obs routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..paths import FRONTEND_DIR
from fastapi import HTTPException
from fastapi.responses import FileResponse
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/obs/surface")
async def get_surface_obs(lat: float | None = None, lon: float | None = None, radius_km: float = 400.0):
    """Surface station obs (METAR) for the radar app's Observations layer. With
    lat/lon → a dense box around the active radar site (aviationweather.gov caps a
    query at ~400 stations, so a site-centered box returns far more local stations
    than the national snapshot); without → the national set. Each ob includes
    decoded extras (visibility, clouds/ceiling, flight category, raw METAR)."""
    try:
        from ..services.surface_obs_service import get_surface_obs_service
    except ImportError:
        from backend.services.surface_obs_service import get_surface_obs_service
    svc = get_surface_obs_service()
    obs = await asyncio.to_thread(svc.get_obs, lat, lon, radius_km)
    return {"obs": obs}
@router.get("/api/obs/analysis")
async def get_surface_analysis():
    """Objective surface analysis from the obs: High/Low pressure centers + fronts
    (thermal-gradient zones classified warm/cold/stationary by advection)."""
    try:
        from ..services.surface_obs_service import get_surface_obs_service
    except ImportError:
        from backend.services.surface_obs_service import get_surface_obs_service
    svc = get_surface_obs_service()
    return await asyncio.to_thread(svc.get_analysis)
@router.get("/api/obs/wpc")
async def get_wpc_surface():
    """Official WPC surface analysis — hand-analyzed fronts + High/Low centers
    (parsed from the Coded Surface Bulletin)."""
    try:
        from ..services.wpc_fronts_service import get_wpc_fronts_service
    except ImportError:
        from backend.services.wpc_fronts_service import get_wpc_fronts_service
    svc = get_wpc_fronts_service()
    return await asyncio.to_thread(svc.get)
@router.get("/obs")
@router.get("/obs/{path:path}")
async def serve_obs_overlay(path: str = ""):
    """Serve the frontend for OBS overlay route."""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Frontend not built")
