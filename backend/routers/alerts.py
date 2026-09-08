"""
Alerts routes.

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
    get_alert_manager,
    get_message_broker,
)
from fastapi import HTTPException
from fastapi import Query
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/alerts")
async def get_alerts(
    state: Optional[str] = Query(None, description="Filter by state code (e.g., OH)"),
    phenomenon: Optional[str] = Query(None, description="Filter by phenomenon code (e.g., TO)"),
    priority: bool = Query(True, description="Sort by priority"),
):
    """
    Get all active alerts.

    Returns list of active weather alerts, optionally filtered.
    """
    alert_manager = get_alert_manager()

    if state:
        alerts = alert_manager.get_alerts_by_state(state)
    elif phenomenon:
        alerts = alert_manager.get_alerts_by_phenomenon(phenomenon)
    else:
        alerts = alert_manager.get_alerts_sorted(by_priority=priority)

    return {
        "count": len(alerts),
        "alerts": [alert.to_dict() for alert in alerts],
    }
@router.get("/api/alerts/{product_id}")
async def get_alert(product_id: str):
    """Get a specific alert by product ID."""
    alert_manager = get_alert_manager()
    alert = alert_manager.get_alert(product_id)

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return alert.to_dict()
@router.post("/api/alerts/{product_id}/impact-scan")
async def scan_alert_impact(product_id: str, push: bool = True, force: bool = False):
    """Scan a warning polygon for at-risk places via OpenStreetMap/Overpass.

    Returns the categorized impacted places (towns, mobile home parks, schools,
    hospitals/care). When ``push`` is true and the scan found anything, the
    result is also broadcast to the on-stream impact widget.
    """
    alert_manager = get_alert_manager()
    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    if not alert.polygon or len(alert.polygon) < 3:
        raise HTTPException(status_code=422, detail="Alert has no polygon to scan")

    try:
        from ..services.osm_impact_service import get_osm_impact_service
    except ImportError:
        from backend.services.osm_impact_service import get_osm_impact_service

    service = get_osm_impact_service()
    result = await service.scan(
        product_id, alert.polygon, event_name=alert.event_name, force=force
    )

    if push and result.get("total", 0) > 0:
        broker = get_message_broker()
        await broker.broadcast_impact_places(result)

    return result
@router.post("/api/alerts/{product_id}/focus")
async def focus_alert_on_map(product_id: str):
    """Tell map clients (the radar app) to zoom to and flash this alert.

    Broadcasts the full alert dict so clients have the polygon, centroid, and
    detail fields without another lookup.
    """
    alert_manager = get_alert_manager()
    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    broker = get_message_broker()
    await broker.broadcast_focus_alert(alert.to_dict())
    return {"success": True}
@router.delete("/api/alerts/{product_id}")
async def clear_alert_manual(product_id: str):
    """Manually clear an alert by product ID."""
    alert_manager = get_alert_manager()

    if alert_manager.remove_alert(product_id, reason="MANUAL"):
        return {"success": True, "message": f"Alert {product_id} cleared manually"}
    
    raise HTTPException(status_code=404, detail="Alert not found or already removed")
