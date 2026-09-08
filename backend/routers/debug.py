"""
Debug routes.

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
    get_message_broker,
    get_zone_geometry_service,
)
from fastapi import HTTPException
from fastapi import Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/debug/alerts-summary")
async def debug_alerts_summary():
    """
    Debug endpoint to see all alerts with their polygon counts.
    """
    alert_manager = get_alert_manager()
    alerts = alert_manager.get_alerts_sorted()

    summary = []
    for alert in alerts:
        polygon_count = 0
        if alert.polygon:
            # Check if multi-polygon
            if alert.polygon and len(alert.polygon) > 0:
                if isinstance(alert.polygon[0], list) and len(alert.polygon[0]) > 0:
                    if isinstance(alert.polygon[0][0], list):
                        # Multi-polygon format
                        polygon_count = len(alert.polygon)
                    else:
                        # Single polygon format (list of [lat, lon])
                        polygon_count = 1

        summary.append({
            "product_id": alert.product_id,
            "event_name": alert.event_name,
            "significance": alert.significance.value if alert.significance else None,
            "affected_areas_count": len(alert.affected_areas or []),
            "polygon_count": polygon_count,
            "has_polygon": polygon_count > 0,
        })

    return {
        "alert_count": len(summary),
        "alerts": summary,
    }
@router.get("/api/debug/alert/{product_id}/geometry")
async def debug_alert_geometry(product_id: str):
    """
    Debug endpoint to inspect an alert's zone geometry.

    Returns detailed info about the alert's polygon, affected_areas,
    and what zones are in the cache.
    """
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()

    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Check each zone in affected_areas
    zone_details = []
    for ugc in (alert.affected_areas or []):
        zone_type = zone_service.get_zone_type(ugc)
        cached = zone_service._get_from_cache(ugc)
        zone_details.append({
            "ugc": ugc,
            "zone_type": zone_type,
            "in_cache": cached is not None,
            "cached_polygon_count": len(cached) if cached else 0,
        })

    # Count total polygons in current alert
    polygon_info = None
    if alert.polygon:
        if isinstance(alert.polygon[0][0], list):
            # Multi-polygon: [[[[lat, lon], ...]], [[[lat, lon], ...]]]
            polygon_info = {
                "format": "multi-polygon",
                "polygon_count": len(alert.polygon),
                "first_polygon_points": len(alert.polygon[0]) if alert.polygon else 0,
            }
        else:
            # Single polygon or flat list: [[lat, lon], ...]
            polygon_info = {
                "format": "single-polygon or flat",
                "point_count": len(alert.polygon),
            }

    return {
        "product_id": product_id,
        "event_name": alert.event_name,
        "significance": alert.significance.value if alert.significance else None,
        "affected_areas_count": len(alert.affected_areas or []),
        "affected_areas": alert.affected_areas,
        "zone_details": zone_details,
        "polygon_info": polygon_info,
        "cache_stats": zone_service.get_cache_stats(),
    }
@router.delete("/api/debug/zone-cache")
async def debug_clear_zone_cache(
    delete_file: bool = Query(False, description="Also delete the cache file on disk"),
):
    """
    Debug endpoint to clear the zone geometry cache.

    This forces a fresh fetch from the NWS API on next request.
    """
    zone_service = get_zone_geometry_service()
    settings = get_settings()
    stats_before = zone_service.get_cache_stats()

    zone_service.clear_cache()

    file_deleted = False
    cache_file = settings.data_dir / "zone_geometry_cache.json"
    if delete_file and cache_file.exists():
        try:
            cache_file.unlink()
            file_deleted = True
            logger.info(f"Deleted zone geometry cache file: {cache_file}")
        except Exception as e:
            logger.error(f"Failed to delete cache file: {e}")

    stats_after = zone_service.get_cache_stats()

    return {
        "success": True,
        "message": "Zone geometry cache cleared",
        "file_deleted": file_deleted,
        "cache_file": str(cache_file),
        "before": stats_before,
        "after": stats_after,
    }
@router.post("/api/debug/alert/{product_id}/add-zones")
async def debug_add_zones(product_id: str, zones: str = Query(..., description="Comma-separated zone codes")):
    """
    Debug endpoint to manually add zones to an alert and repopulate geometry.

    Use this when NWS issues multiple products for the same event covering different areas.
    """
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()

    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Parse zone list
    new_zones = [z.strip().upper() for z in zones.split(",") if z.strip()]

    # Merge with existing
    existing_zones = set(alert.affected_areas or [])
    added_zones = [z for z in new_zones if z not in existing_zones]
    existing_zones.update(new_zones)
    alert.affected_areas = sorted(list(existing_zones))

    # Fetch geometry for all zones (including new ones)
    all_polygons = []
    fetch_results = []

    for ugc in alert.affected_areas:
        zone_type = zone_service.get_zone_type(ugc)
        if zone_type:
            geometry = await zone_service.fetch_zone_geometry(ugc)
            fetch_results.append({
                "ugc": ugc,
                "zone_type": zone_type,
                "polygon_count": len(geometry) if geometry else 0,
                "is_new": ugc in added_zones,
            })
            if geometry:
                all_polygons.extend(geometry)

    # Update alert
    alert.polygon = all_polygons
    alert_manager.save_to_file()

    # Broadcast update
    broker = get_message_broker()
    await broker.broadcast_alert_update(alert)

    return {
        "product_id": product_id,
        "zones_added": added_zones,
        "total_zones": len(alert.affected_areas),
        "total_polygons": len(all_polygons),
        "fetch_results": fetch_results,
    }
@router.post("/api/debug/alert/{product_id}/repopulate")
async def debug_repopulate_geometry(product_id: str, force: bool = Query(True)):
    """
    Debug endpoint to manually repopulate zone geometry for an alert.

    Returns detailed debug info about what was fetched.
    """
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()

    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Store original polygon info
    original_polygon_count = len(alert.polygon) if alert.polygon else 0

    # Clear existing polygon if forcing
    if force:
        alert.polygon = []

    # Manually fetch each zone and track results
    fetch_results = []
    all_polygons = []

    for ugc in (alert.affected_areas or []):
        zone_type = zone_service.get_zone_type(ugc)
        if zone_type:
            geometry = await zone_service.fetch_zone_geometry(ugc)
            fetch_results.append({
                "ugc": ugc,
                "zone_type": zone_type,
                "polygon_count": len(geometry) if geometry else 0,
                "success": geometry is not None,
            })
            if geometry:
                all_polygons.extend(geometry)

    # Update alert
    alert.polygon = all_polygons

    # Save the alert
    alert_manager.save_to_file()

    # Broadcast update
    broker = get_message_broker()
    await broker.broadcast_alert_update(alert)

    return {
        "product_id": product_id,
        "event_name": alert.event_name,
        "original_polygon_count": original_polygon_count,
        "new_polygon_count": len(all_polygons),
        "zones_processed": len(fetch_results),
        "zones_with_geometry": sum(1 for r in fetch_results if r["success"]),
        "fetch_results": fetch_results,
    }
