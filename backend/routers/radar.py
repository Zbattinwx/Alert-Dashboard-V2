"""
Radar routes.

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
    NEXRAD_SITES,
    get_nearest_sites,
    get_nexrad_service,
    get_storm_tracking_service,
)
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel
from pydantic import Field
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


class RadarSiteRequest(BaseModel):
    """Request body for setting active radar site."""
    site_id: str

class RadarGateRequest(BaseModel):
    gate_dbz: float = Field(..., ge=-20, le=40, description="Reflectivity gate threshold in dBZ")


@router.get("/api/radar/status")
async def get_radar_status():
    """Get current NEXRAD radar service status."""
    settings = get_settings()
    if not settings.nexrad_enabled:
        return {"enabled": False, "active_site": None, "active_sites": [], "last_update": None, "processing": False}
    svc = get_nexrad_service()
    if not svc:
        return {"enabled": False, "active_site": None, "active_sites": [], "last_update": None, "processing": False}
    return svc.status.to_dict()
@router.get("/api/radar/chunks/diagnostic")
async def get_radar_chunks_diagnostic():
    """Per-site chunks-bucket pipeline diagnostics.

    Empty diagnostics block if the chunks path is disabled — no error.
    """
    settings = get_settings()
    if not settings.nexrad_chunks_enabled:
        return {
            "enabled": False,
            "reason": "nexrad_chunks_enabled = false",
            "diagnostics": {},
        }
    try:
        from ..services.nexrad_chunks_service import get_nexrad_chunks_service
        svc = get_nexrad_chunks_service()
    except Exception as e:
        return {"enabled": False, "reason": f"load error: {e}", "diagnostics": {}}
    if svc is None:
        return {"enabled": False, "reason": "service not started", "diagnostics": {}}
    return {
        "enabled":        True,
        "poll_interval_s": svc._poll_interval,
        "min_partial":    svc._min_partial,
        "render_on_complete": svc._render_on_complete,
        "now_utc":        datetime.now(timezone.utc).isoformat(),
        "diagnostics":    svc.diagnostics,
    }
@router.get("/api/radar/diagnostic")
async def get_radar_diagnostic():
    """Per-site pipeline diagnostics for latency debugging.

    Returns, per active site, the latest scan we're showing, the latest scan
    available on S3, and a breakdown of stage timings (list/download/parse/
    dealias/render/grid).  When complaints come in like "the scan is 12 min
    old" this endpoint tells you whether the upstream is slow, the network
    is slow, or our pipeline is slow.
    """
    settings = get_settings()
    if not settings.nexrad_enabled:
        return {"enabled": False, "active_sites": [], "diagnostics": {}}
    svc = get_nexrad_service()
    if not svc:
        return {"enabled": False, "active_sites": [], "diagnostics": {}}

    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)

    # VCP-duration estimates from NEXRAD VCP catalog.  Used to compute the
    # archive-bucket "irreducible floor": you cannot show a scan before the
    # last tilt has been collected.  Tilt count is a coarse proxy because
    # SAILS/MRLE inject extra low-level passes, but it's good enough to tell
    # the user whether their lag is pipeline-induced or VCP-induced.
    def _vcp_duration_seconds(tilt_count):
        if not tilt_count or tilt_count <= 0:
            return None
        if tilt_count <= 5:   return 600   # VCP 31/32 clear-air (10 min)
        if tilt_count <= 7:   return 420   # VCP 35 clear-air (7 min)
        if tilt_count <= 9:   return 360   # VCP 21/121 (6 min)
        if tilt_count <= 14:  return 270   # VCP 12/211/212 base (4.5 min)
        if tilt_count <= 15:  return 360   # VCP 215 (6 min)
        if tilt_count <= 17:  return 300   # 212/215 + SAILSx3 (~5 min)
        return 360                          # severe + SAILS + MRLE, ~6 min

    diag_out = {}
    for site, d in svc.diagnostics.items():
        entry = dict(d)
        # Recompute "live" ages so they reflect now, not when diag was written
        showing = entry.get("showing_scan_ts")
        if showing:
            try:
                entry["showing_age_s_live"] = round(
                    (now - _dt.fromisoformat(showing)).total_seconds(), 1
                )
            except (ValueError, TypeError):
                pass
        s3_ts = entry.get("latest_available_ts")
        if s3_ts:
            try:
                entry["latest_available_age_s_live"] = round(
                    (now - _dt.fromisoformat(s3_ts)).total_seconds(), 1
                )
            except (ValueError, TypeError):
                pass

        # Latency budget breakdown — surfaces whether the user's pipeline
        # overhead is meaningful relative to the unavoidable VCP duration.
        tilts = entry.get("last_tilt_count")
        vcp_s = _vcp_duration_seconds(tilts)
        if vcp_s is not None:
            entry["vcp_estimated_duration_s"] = vcp_s
            # Archive floor ≈ VCP duration + S3 propagation (30s) + poll discovery
            poll_int = getattr(svc, "_poll_interval", 10) or 10
            entry["archive_bucket_floor_s"] = vcp_s + 30 + poll_int
            # How much we add on top of the floor
            stage_sum = sum(
                float(entry.get(k, 0) or 0)
                for k in (
                    "last_list_duration_s",
                    "last_download_duration_s",
                    "last_parse_duration_s",
                    "last_dealias_duration_s",
                    "last_render_duration_s",
                )
            )
            entry["pipeline_overhead_s"] = round(stage_sum, 2)
            # Headline number: if showing_age_s_live ≈ archive_bucket_floor_s,
            # the lag is the radar, not us.  If it's much higher, we've got
            # work to do.
            if "showing_age_s_live" in entry:
                entry["excess_over_archive_floor_s"] = round(
                    entry["showing_age_s_live"] - entry["archive_bucket_floor_s"], 1
                )

        diag_out[site] = entry
    return {
        "enabled":        True,
        "active_sites":   svc.active_sites,
        "poll_interval_s": getattr(svc, "_poll_interval", None),
        "now_utc":        now.isoformat(),
        "diagnostics":    diag_out,
    }
@router.get("/api/radar/sites")
async def get_radar_sites(lat: Optional[float] = None, lon: Optional[float] = None):
    """Get list of NEXRAD radar sites, optionally sorted by distance from a point."""
    if lat is not None and lon is not None:
        return get_nearest_sites(lat, lon, count=20)
    # Return all sites
    sites = []
    for site_id, info in NEXRAD_SITES.items():
        sites.append({
            "id": site_id,
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "state": info["state"],
        })
    sites.sort(key=lambda s: (s["state"], s["name"]))
    return sites
@router.get("/api/radar/frame/{product}")
async def get_radar_frame(product: str):
    """Get the latest radar frame(s) for a product — one per active site."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    from ..services.nexrad_service import RADAR_PRODUCTS as _RP
    if product not in _RP:
        raise HTTPException(status_code=400, detail=f"Unknown product: {product}")

    if not settings.nexrad_serve_frames:
        raise HTTPException(
            status_code=409,
            detail=("Radar display frames are disabled on this server "
                    "(nexrad_serve_frames=false). Ingestion and storm-cell "
                    "tracking are unaffected; the radar app decodes Level 2 "
                    "client-side and does not use this endpoint."))

    frames = svc.get_latest_frames_for_product(product)
    return [f.to_dict() for f in frames]
@router.get("/api/radar/frames/{product}")
async def get_radar_frame_history(product: str, count: int = 10, site: str | None = None):
    """Get frame history for animation. Optional ?site= for a specific active site."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    from ..services.nexrad_service import RADAR_PRODUCTS as _RP
    if product not in _RP:
        raise HTTPException(status_code=400, detail=f"Unknown product: {product}")
    if not settings.nexrad_serve_frames:
        raise HTTPException(
            status_code=409,
            detail=("Radar display frames are disabled on this server "
                    "(nexrad_serve_frames=false). Ingestion and storm-cell "
                    "tracking are unaffected; the radar app decodes Level 2 "
                    "client-side and does not use this endpoint."))


    frames = svc.get_frame_history(product, count, site=site)
    return [f.to_dict() for f in frames]
@router.post("/api/radar/site")
async def set_radar_site(request: RadarSiteRequest):
    """Replace all active sites with one site (backward compat)."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    try:
        await svc.set_active_site(request.site_id)
        return {"status": "ok", "active_sites": svc.active_sites}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
@router.post("/api/radar/sites/add")
async def add_radar_site(request: RadarSiteRequest):
    """Add a site to the active radar set (max 3)."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    try:
        await svc.add_site(request.site_id)
        return {"status": "ok", "active_sites": svc.active_sites}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
@router.post("/api/radar/sites/remove")
async def remove_radar_site(request: RadarSiteRequest):
    """Remove a site from the active radar set."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    try:
        await svc.remove_site(request.site_id)
        return {"status": "ok", "active_sites": svc.active_sites}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
@router.get("/api/radar/systems")
async def get_mcs_systems():
    """Get all currently detected MCS/QLCS systems."""
    svc = get_storm_tracking_service()
    if not svc:
        return []
    return [s.to_dict() for s in svc.tracked_systems]
@router.get("/api/radar/cells")
async def get_storm_cells():
    """Get all currently tracked storm cells."""
    svc = get_storm_tracking_service()
    if not svc:
        return []
    return [c.to_dict() for c in svc.tracked_cells]
@router.get("/api/radar/cell/{cell_id}")
async def get_storm_cell(cell_id: str):
    """Get detailed info for a specific storm cell."""
    svc = get_storm_tracking_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Storm tracking service not enabled")

    cell = svc.get_cell(cell_id)
    if not cell:
        raise HTTPException(status_code=404, detail=f"Cell not found: {cell_id}")
    return cell.to_dict()
@router.get("/api/radar/images/{site}/{filename}")
async def serve_radar_image(site: str, filename: str):
    """Serve rendered radar images (WebP or PNG) — used by social graphic pipeline."""
    settings = get_settings()
    image_path = Path(settings.data_dir) / "radar" / site / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    media_type = "image/webp" if filename.endswith(".webp") else "image/png"
    return FileResponse(
        str(image_path),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
@router.get("/api/radar/cached/{site}")
async def get_cached_radar_frame(site: str):
    """Return the latest cached reflectivity binary for any NEXRAD site.

    Returns 404 if the site has never been loaded this session.  The frontend
    uses this to show a stale-but-instant frame when switching sites while
    the fresh download runs in the background.
    """
    from fastapi.responses import Response as FastResponse
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not running")
    binary = svc.get_cached_frame(site)
    if not binary:
        raise HTTPException(status_code=404, detail="No cached frame for this site")
    return FastResponse(
        content=binary,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )
@router.get("/api/radar/binary/{site}/{product}/{frame_id}")
async def get_radar_binary_frame(site: str, product: str, frame_id: str):
    """Return a cached binary radar frame by ID (RDRF wire format) for history scrubber."""
    from fastapi.responses import Response as FastResponse
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not running")
    frame = svc.get_frame_by_id(site, product, frame_id)
    if not frame or not frame.binary_data:
        raise HTTPException(status_code=404, detail="Frame not found")
    return FastResponse(
        content=frame.binary_data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )
@router.get("/api/radar/gate")
async def get_radar_gate():
    """Get current reflectivity gate threshold (dBZ)."""
    service = get_nexrad_service()
    # service can be None before NEXRAD finishes starting (or if disabled);
    # fall back to the default threshold instead of raising a 500.
    return {"gate_dbz": getattr(service, "_gate_dbz", 10.0)}
@router.post("/api/radar/gate")
async def set_radar_gate(request: RadarGateRequest):
    """Set reflectivity gate threshold and re-render current scan."""
    service = get_nexrad_service()
    await service.set_gate_dbz(request.gate_dbz)
    return {"gate_dbz": service._gate_dbz}
