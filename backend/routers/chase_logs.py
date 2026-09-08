"""
Chase Logs routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..services import (
    get_chase_log_service,
)
from fastapi import HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/chase-logs")
async def list_chase_logs():
    """List all chase log sessions."""
    service = get_chase_log_service()
    return {"sessions": service.list_sessions()}
@router.get("/api/chase-logs/{date}")
async def get_chase_log(date: str):
    """Get a specific chase log by date (YYYY-MM-DD)."""
    service = get_chase_log_service()
    session = service.get_session(date)
    if not session:
        raise HTTPException(status_code=404, detail=f"No chase log for {date}")
    return session
@router.get("/api/chase-logs/{date}/geojson")
async def get_chase_log_geojson(date: str):
    """Export a chase log as GeoJSON LineString."""
    service = get_chase_log_service()
    geojson = service.get_session_geojson(date)
    if not geojson:
        raise HTTPException(status_code=404, detail=f"No chase log for {date}")
    return geojson
