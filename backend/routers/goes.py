"""
Goes routes.

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

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/goes/meso")
async def goes_meso(sat: str = "east", sector: str = "1", band: str = "ir"):
    """Meta for the latest GOES mesoscale sector frame: time + lat/lon bbox [w,s,e,n]."""
    from ..services.goes_meso_service import get_goes_meso_service
    ent = await get_goes_meso_service().get(sat, sector, band)
    if not ent:
        raise HTTPException(status_code=503, detail="GOES meso not available")
    return {"time": ent["time"], "bbox": ent["bbox"]}


@router.get("/api/goes/meso/frames")
async def goes_meso_frames(sat: str = "east", sector: str = "1", band: str = "ir",
                           n: int = 8, span: int = 0):
    """`n` GOES mesoscale frames [{time, bbox}], oldest→newest — for looping.
    The sector floats, so each frame carries its own bbox.

    `span` (minutes) spreads those n frames across that window instead of
    returning the newest n consecutive ones. The meso sector is a 1-minute
    product, so without it a 3-hour loop request came back as the newest ~24
    MINUTES. Omitted/0 keeps the original behaviour."""
    from ..services.goes_meso_service import get_goes_meso_service
    frames = await get_goes_meso_service().get_frames(sat, sector, band, n, span)
    if not frames:
        raise HTTPException(status_code=503, detail="GOES meso not available")
    return {"frames": frames}


@router.get("/api/goes/meso/image")
async def goes_meso_image(sat: str = "east", sector: str = "1", band: str = "ir", t: str = ""):
    """Reprojected GOES mesoscale PNG for a MapLibre image source. `t` selects the
    frame time (from /frames); empty serves the latest."""
    from fastapi.responses import Response as FastResponse
    from ..services.goes_meso_service import get_goes_meso_service
    svc = get_goes_meso_service()
    png = await svc.get_image(sat, sector, band, t) if t else None
    if png is None:
        ent = await svc.get(sat, sector, band)
        png = ent["png"] if ent else None
    if png is None:
        raise HTTPException(status_code=503, detail="GOES meso not available")
    return FastResponse(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=120"},
    )


