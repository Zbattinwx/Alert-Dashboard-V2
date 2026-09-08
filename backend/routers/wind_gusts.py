"""
Wind Gusts routes.

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
    DEFAULT_GUST_STATES,
    GUST_THRESHOLD_ADVISORY,
    GUST_THRESHOLD_SEVERE,
    GUST_THRESHOLD_SIGNIFICANT,
    get_wind_gusts_service,
)
from fastapi import Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/wind-gusts")
async def get_wind_gusts(
    hours: int = Query(1, ge=1, le=24, description="Lookback period in hours"),
    limit: int = Query(15, ge=1, le=100, description="Maximum number of results"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get top wind gust observations from ASOS stations.

    Returns wind gusts from Iowa State Mesonet for configured filter_states.
    """
    settings = get_settings()
    wind_service = get_wind_gusts_service()

    # Use filter_states or defaults if empty
    states_to_use = settings.filter_states if settings.filter_states else DEFAULT_GUST_STATES

    gusts = await wind_service.fetch_gusts(
        states=states_to_use,
        hours=hours,
        limit=limit,
        force_refresh=refresh,
    )

    # Group by state for frontend display
    gusts_by_state = wind_service.get_gusts_by_state(gusts)

    return {
        "count": len(gusts),
        "filter_states": states_to_use,
        "thresholds": {
            "significant": GUST_THRESHOLD_SIGNIFICANT,
            "severe": GUST_THRESHOLD_SEVERE,
            "advisory": GUST_THRESHOLD_ADVISORY,
        },
        "gusts": [g.to_dict() for g in gusts],
        "by_state": {
            state: [g.to_dict() for g in state_gusts]
            for state, state_gusts in gusts_by_state.items()
        },
    }
@router.get("/api/wind-gusts/by-state")
async def get_wind_gusts_by_state(
    hours: int = Query(1, ge=1, le=24, description="Lookback period in hours"),
    limit_per_state: int = Query(5, ge=1, le=50, description="Maximum results per state"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get wind gusts organized by state.

    Returns gusts grouped by state with a per-state limit.
    """
    settings = get_settings()
    wind_service = get_wind_gusts_service()

    # Use filter_states or defaults if empty
    states_to_use = settings.filter_states if settings.filter_states else DEFAULT_GUST_STATES

    # Fetch all gusts (higher limit to allow per-state filtering)
    gusts = await wind_service.fetch_gusts(
        states=states_to_use,
        hours=hours,
        limit=100,
        force_refresh=refresh,
    )

    # Group by state and limit each
    gusts_by_state = wind_service.get_gusts_by_state(gusts)
    result = {}
    total = 0

    for state in states_to_use:
        if state in gusts_by_state:
            state_gusts = gusts_by_state[state][:limit_per_state]
            result[state] = [g.to_dict() for g in state_gusts]
            total += len(state_gusts)
        else:
            result[state] = []

    return {
        "count": total,
        "filter_states": states_to_use,
        "thresholds": {
            "significant": GUST_THRESHOLD_SIGNIFICANT,
            "severe": GUST_THRESHOLD_SEVERE,
            "advisory": GUST_THRESHOLD_ADVISORY,
        },
        "by_state": result,
    }
@router.get("/api/wind-gusts/stats")
async def get_wind_gusts_stats():
    """Get wind gusts service statistics."""
    wind_service = get_wind_gusts_service()
    return wind_service.get_statistics()
