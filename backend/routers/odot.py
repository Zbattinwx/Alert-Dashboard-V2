"""
Odot routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..config import get_settings
from ..services import (
    get_alert_manager,
    get_odot_service,
)
from fastapi import Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/odot/cameras")
async def get_odot_cameras(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all ODOT traffic cameras.

    Returns camera locations with live image URLs.
    """
    odot_service = get_odot_service()
    cameras = await odot_service.fetch_cameras(force_refresh=refresh)

    return {
        "count": len(cameras),
        "cameras": [c.to_dict() for c in cameras],
    }


@router.get("/api/odot/sensors")
async def get_odot_sensors(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all ODOT road weather sensors.

    Returns sensor data including pavement and air temperatures.
    """
    odot_service = get_odot_service()
    sensors = await odot_service.fetch_sensors(force_refresh=refresh)

    return {
        "count": len(sensors),
        "sensors": [s.to_dict() for s in sensors],
    }


@router.get("/api/odot/cold-sensors")
async def get_cold_sensors(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get sensors with cold pavement (below threshold).

    Returns sensors sorted by temperature (coldest first).
    """
    settings = get_settings()
    odot_service = get_odot_service()

    # Ensure we have fresh data
    await odot_service.fetch_sensors(force_refresh=refresh)

    cold_sensors = odot_service.get_cold_sensors()
    freezing_sensors = odot_service.get_freezing_sensors()

    # Sort by pavement temperature (coldest first)
    cold_sensors.sort(key=lambda s: s.pavement_temp if s.pavement_temp is not None else 100)

    return {
        "count": len(cold_sensors),
        "freezing_count": len(freezing_sensors),
        "cold_threshold": settings.cold_pavement_threshold,
        "freezing_threshold": settings.freezing_pavement_threshold,
        "sensors": [s.to_dict() for s in cold_sensors],
    }


@router.get("/api/odot/cameras-in-alerts")
async def get_cameras_in_alerts(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get cameras that are inside active weather alert polygons.

    Only checks alerts matching the configured camera_alert_phenomena.
    """
    settings = get_settings()
    odot_service = get_odot_service()
    alert_manager = get_alert_manager()

    # Ensure we have fresh camera data
    await odot_service.fetch_cameras(force_refresh=refresh)

    # Get all active alerts with polygons
    alerts = alert_manager.get_alerts_sorted()
    alert_dicts = [a.to_dict() for a in alerts]

    # Find cameras in alerts
    cameras_in_alerts = odot_service.find_cameras_in_alerts(
        alert_dicts,
        phenomena_filter=settings.camera_alert_phenomena
    )

    return {
        "count": len(cameras_in_alerts),
        "phenomena_filter": settings.camera_alert_phenomena,
        "cameras": [c.to_dict() for c in cameras_in_alerts],
    }


@router.get("/api/odot/stats")
async def get_odot_stats():
    """Get ODOT service statistics."""
    odot_service = get_odot_service()
    return odot_service.get_statistics()


# =============================================================================
# SPC (Storm Prediction Center) Endpoints
# =============================================================================

