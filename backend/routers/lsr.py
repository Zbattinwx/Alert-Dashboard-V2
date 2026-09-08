"""
Lsr routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..config import get_brand_config
from ..config import get_settings
from ..paths import _GRAPHICS_DIR
from ..services import (
    LSR_TYPE_COLORS,
    MessageType,
    StormReport,
    get_lsr_service,
    get_message_broker,
)
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from pydantic import Field
from typing import Optional
import uuid

logger = logging.getLogger(__name__)

router = APIRouter()


class ViewerReportSubmission(BaseModel):
    """Model for viewer report submission from dashboard."""
    report_type: str = Field(..., description="Report type (TORNADO, HAIL, etc.)")
    lat: float = Field(..., description="Latitude")
    lon: float = Field(..., description="Longitude")
    magnitude: Optional[str] = Field(None, description="Magnitude (e.g., '1.00 INCH', '65 MPH')")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    location: Optional[str] = Field(None, description="Human-readable location")
    submitter: Optional[str] = Field("Anonymous", description="Submitter name")


@router.get("/api/lsr")
async def get_storm_reports(
    hours: int = Query(24, ge=1, le=168, description="Lookback period in hours"),
    report_type: Optional[str] = Query(None, description="Filter by report type"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get Local Storm Reports from Iowa State Mesonet.

    Returns tornado, hail, wind, flood, and other severe weather reports.
    """
    lsr_service = get_lsr_service()
    settings = get_settings()

    # Fetch reports
    reports = await lsr_service.fetch_reports(
        states=settings.filter_states,
        hours=hours,
        force_refresh=refresh,
    )

    # Filter by type if specified
    if report_type:
        reports = [r for r in reports if r.report_type.upper() == report_type.upper()]

    return {
        "count": len(reports),
        "reports": [r.to_dict() for r in reports],
        "type_colors": LSR_TYPE_COLORS,
    }
@router.get("/api/lsr/stats")
async def get_lsr_stats():
    """Get LSR statistics."""
    lsr_service = get_lsr_service()
    return lsr_service.get_statistics()
@router.get("/api/lsr/types")
async def get_lsr_types():
    """Get available LSR types and their colors."""
    return {
        "types": list(LSR_TYPE_COLORS.keys()),
        "colors": LSR_TYPE_COLORS,
    }
@router.get("/api/lsr/summary-graphic")
async def get_lsr_summary_graphic(
    hours: int = Query(24, ge=1, le=168, description="Lookback hours"),
    title: Optional[str] = Query(None, description="Graphic title override"),
    save: bool = Query(False, description="Also save to alert graphics gallery"),
):
    """
    Generate a server-side rendered LSR damage survey summary graphic (PNG).

    Returns a PNG image showing storm report markers on a coordinate map
    with a stats panel. Suitable for recap posts and on-stream display.
    """
    from fastapi.responses import StreamingResponse
    import io

    try:
        from ..services.lsr_graphic_service import generate_lsr_summary_graphic
    except ImportError:
        from backend.services.lsr_graphic_service import generate_lsr_summary_graphic

    lsr_service = get_lsr_service()
    settings = get_settings()
    brand = get_brand_config(settings.brand)

    reports = await lsr_service.fetch_reports(states=settings.filter_states, hours=hours)

    if title is None:
        title = f"Storm Report Summary — {', '.join(settings.filter_states) if settings.filter_states else 'All States'}"

    try:
        png_bytes = generate_lsr_summary_graphic(
            reports=reports,
            title=title,
            hours=hours,
            brand_name=brand.name,
            states=settings.filter_states or None,
        )
    except Exception as e:
        logger.exception(f"LSR summary graphic generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Graphic generation failed: {e}")

    if save:
        import re as _re
        import json as _json
        from datetime import datetime, timezone
        _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_id = f"lsr_summary_{ts}"
        img_path = _GRAPHICS_DIR / f"{safe_id}.png"
        img_path.write_bytes(png_bytes)
        meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
        meta_path.write_text(_json.dumps({"product_id": safe_id, "event_name": title}))
        logger.info(f"Saved LSR summary graphic: {safe_id}.png")

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="lsr_summary.png"'},
    )
@router.get("/api/lsr/all")
async def get_all_storm_reports(
    hours: int = Query(24, ge=1, le=168, description="Lookback period in hours"),
    report_type: Optional[str] = Query(None, description="Filter by report type"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all storm reports (official + viewer).

    Official reports are filtered by state settings, viewer reports are always included.
    """
    lsr_service = get_lsr_service()
    settings = get_settings()

    # Fetch official reports (filtered by state)
    await lsr_service.fetch_reports(
        states=settings.filter_states,
        hours=hours,
        force_refresh=refresh,
    )

    # Get all reports (official filtered by state + all viewer reports)
    reports = lsr_service.get_all_reports(states=settings.filter_states)

    # Filter by type if specified
    if report_type:
        reports = [r for r in reports if r.report_type.upper() == report_type.upper()]

    return {
        "count": len(reports),
        "reports": [r.to_dict() for r in reports],
        "viewer_count": sum(1 for r in reports if r.is_viewer),
        "type_colors": LSR_TYPE_COLORS,
    }
@router.get("/api/lsr/viewer")
async def get_viewer_reports():
    """Get all viewer-submitted reports."""
    lsr_service = get_lsr_service()
    reports = lsr_service.get_manual_reports()

    return {
        "count": len(reports),
        "reports": [r.to_dict() for r in reports],
    }
@router.post("/api/lsr/viewer")
async def submit_viewer_report(report: ViewerReportSubmission):
    """
    Submit a storm report from the dashboard.

    Used for manual report entry by dashboard users.
    """
    lsr_service = get_lsr_service()

    # Normalize report type
    report_type = report.report_type.upper()

    # Create StormReport
    storm_report = StormReport(
        id=f"viewer_{uuid.uuid4().hex[:12]}",
        report_type=report_type,
        magnitude=report.magnitude,
        lat=report.lat,
        lon=report.lon,
        valid_time=datetime.now(timezone.utc).isoformat(),
        remark=report.remarks or "",
        location_text=report.location or "",
        submitter=report.submitter or "Anonymous",
        is_viewer=True,
        source="VIEWER",
    )

    lsr_service.add_manual_report(storm_report)

    # Broadcast to WebSocket clients
    broker = get_message_broker()
    await broker.broadcast(
        MessageType.SYSTEM_STATUS,
        {
            "event": "viewer_report_added",
            "report": storm_report.to_dict(),
        }
    )

    return {
        "success": True,
        "report": storm_report.to_dict(),
    }
@router.delete("/api/lsr/viewer/{report_id}")
async def remove_viewer_report(report_id: str):
    """Remove a viewer-submitted report by ID."""
    lsr_service = get_lsr_service()

    if lsr_service.remove_manual_report(report_id):
        # Broadcast removal to WebSocket clients
        broker = get_message_broker()
        await broker.broadcast(
            MessageType.SYSTEM_STATUS,
            {
                "event": "viewer_report_removed",
                "report_id": report_id,
            }
        )
        return {"success": True, "message": f"Report {report_id} removed"}

    raise HTTPException(status_code=404, detail="Viewer report not found")
@router.delete("/api/lsr/viewer")
async def clear_viewer_reports():
    """Clear all viewer-submitted reports."""
    lsr_service = get_lsr_service()
    lsr_service.clear_manual_reports()

    # Broadcast to WebSocket clients
    broker = get_message_broker()
    await broker.broadcast(
        MessageType.SYSTEM_STATUS,
        {
            "event": "viewer_reports_cleared",
        }
    )

    return {"success": True, "message": "All viewer reports cleared"}
