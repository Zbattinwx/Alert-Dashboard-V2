"""
Spc routes.

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
    RISK_COLORS,
    RISK_NAMES,
    get_spc_service,
)
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from fastapi import Query
import aiohttp

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/spc/outlooks")
async def get_spc_outlooks(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all SPC convective outlooks (Day 1-3).

    Returns categorical outlooks with risk level polygons.
    """
    spc_service = get_spc_service()

    if refresh:
        await spc_service.fetch_all_outlooks(force_refresh=True)
    else:
        # Fetch day1 categorical if not cached
        await spc_service.fetch_outlook("day1_categorical")

    outlooks = {}
    for key in ["day1_categorical", "day2_categorical", "day3_categorical"]:
        outlook = spc_service._outlooks.get(key)
        if outlook:
            outlooks[key] = outlook.to_dict()

    return {
        "outlooks": outlooks,
        "risk_colors": RISK_COLORS,
        "risk_names": RISK_NAMES,
    }
@router.get("/api/spc/outlook/{outlook_key}")
async def get_spc_outlook(
    outlook_key: str,
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get a specific SPC outlook.

    Valid outlook_key values:
    - day1_categorical, day2_categorical, day3_categorical
    - day1_tornado, day1_wind, day1_hail
    """
    spc_service = get_spc_service()
    outlook = await spc_service.fetch_outlook(outlook_key, force_refresh=refresh)

    if not outlook:
        raise HTTPException(status_code=404, detail=f"Outlook '{outlook_key}' not found or unavailable")

    return {
        "outlook": outlook.to_dict(),
        "risk_colors": RISK_COLORS,
        "risk_names": RISK_NAMES,
    }
@router.get("/api/spc/day1")
async def get_spc_day1(
    include_probabilities: bool = Query(False, description="Include probabilistic outlooks"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get Day 1 SPC outlooks.

    Returns categorical outlook and optionally tornado/wind/hail probabilities.
    """
    spc_service = get_spc_service()

    # Always fetch categorical
    categorical = await spc_service.fetch_outlook("day1_categorical", force_refresh=refresh)

    result = {
        "categorical": categorical.to_dict() if categorical else None,
        "risk_colors": RISK_COLORS,
        "risk_names": RISK_NAMES,
    }

    if include_probabilities:
        tornado = await spc_service.fetch_outlook("day1_tornado", force_refresh=refresh)
        wind = await spc_service.fetch_outlook("day1_wind", force_refresh=refresh)
        hail = await spc_service.fetch_outlook("day1_hail", force_refresh=refresh)
        # CIG (Conditional Intensity Group) overlays
        cig_torn = await spc_service.fetch_outlook("day1_cigtorn", force_refresh=refresh)
        cig_wind = await spc_service.fetch_outlook("day1_cigwind", force_refresh=refresh)
        cig_hail = await spc_service.fetch_outlook("day1_cighail", force_refresh=refresh)

        result["tornado"] = tornado.to_dict() if tornado else None
        result["wind"] = wind.to_dict() if wind else None
        result["hail"] = hail.to_dict() if hail else None
        result["cig_tornado"] = cig_torn.to_dict() if cig_torn and cig_torn.polygons else None
        result["cig_wind"] = cig_wind.to_dict() if cig_wind and cig_wind.polygons else None
        result["cig_hail"] = cig_hail.to_dict() if cig_hail and cig_hail.polygons else None

    return result
@router.get("/api/spc/mesoscale-discussions")
async def get_mesoscale_discussions(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get current SPC Mesoscale Discussions.

    Mesoscale discussions are filtered to states matching filter_states setting.
    """
    settings = get_settings()
    spc_service = get_spc_service()

    mds = await spc_service.fetch_mesoscale_discussions(force_refresh=refresh)

    # Filter by configured states
    filtered_mds = spc_service.filter_mds_by_states(mds, settings.filter_states)

    return {
        "count": len(filtered_mds),
        "total_count": len(mds),
        "filter_states": settings.filter_states,
        "discussions": [md.to_dict() for md in filtered_mds],
    }
@router.get("/api/spc/state-images")
async def get_spc_state_images(
    day: int = Query(1, ge=1, le=3, description="Outlook day (1-3)"),
):
    """
    Get state-specific SPC outlook image URLs.

    Returns image URLs for each state in filter_states setting.
    """
    settings = get_settings()
    spc_service = get_spc_service()

    state_images = spc_service.get_state_outlook_urls(settings.filter_states, day=day)

    return {
        "day": day,
        "states": settings.filter_states,
        "images": state_images,
    }
@router.get("/api/spc/risk-at-point")
async def get_risk_at_point(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    outlook_key: str = Query("day1_categorical", description="Which outlook to check"),
):
    """
    Get the highest risk level at a specific point.

    Useful for checking what risk level affects a specific location.
    """
    spc_service = get_spc_service()

    # Ensure we have the outlook data
    await spc_service.fetch_outlook(outlook_key)

    risk = spc_service.get_highest_risk_for_point(lat, lon, outlook_key)

    if not risk:
        return {
            "lat": lat,
            "lon": lon,
            "outlook_key": outlook_key,
            "risk": None,
            "message": "No risk at this location",
        }

    return {
        "lat": lat,
        "lon": lon,
        "outlook_key": outlook_key,
        "risk": risk.to_dict(),
    }
@router.get("/api/spc/discussion")
async def get_spc_discussion(
    day: int = Query(1, ge=1, le=3, description="Outlook day (1-3)"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get SPC outlook discussion text.

    Returns the official SPC discussion text for the specified day.
    """
    spc_service = get_spc_service()
    text = await spc_service.fetch_discussion(day=day, force_refresh=refresh)

    if not text:
        raise HTTPException(status_code=404, detail=f"Day {day} discussion not available")

    return {
        "day": day,
        "text": text,
        "url": f"https://www.spc.noaa.gov/products/outlook/day{day}otlk.html",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
@router.get("/api/spc/stats")
async def get_spc_stats():
    """Get SPC service statistics."""
    spc_service = get_spc_service()
    return spc_service.get_statistics()
@router.get("/api/spc/geojson")
async def get_spc_geojson(url: str = Query(..., description="An spc.noaa.gov outlook GeoJSON URL")):
    """Proxy an SPC outlook GeoJSON for the radar app. The browser is CORS-blocked on
    spc.noaa.gov for these files from the app's origin (the hub's SPC layer rendered
    blank and the recipe engine captured bare maps labelled as 'the outlook'), so the
    app falls back to fetching them through here, exactly as it already does for
    WPC's ERO. Strictly allow-listed: only SPC's outlook trees, only .geojson."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    ok_host = u.scheme == "https" and u.netloc == "www.spc.noaa.gov"
    ok_path = u.path.startswith(("/products/outlook/", "/products/exper/day4-8/",
                                 "/products/fire_wx/")) and u.path.endswith(".geojson")
    if not (ok_host and ok_path) or ".." in u.path:
        raise HTTPException(status_code=400, detail="Not an SPC outlook GeoJSON URL")
    clean = f"https://www.spc.noaa.gov{u.path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                clean,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"User-Agent": "TheBattinFront Radar (dashboard SPC proxy)"},
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail=f"Upstream returned {resp.status}")
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as e:
        logger.error(f"SPC GeoJSON proxy fetch failed for {clean}: {e}")
        raise HTTPException(status_code=502, detail="Fetch failed")
    from fastapi.responses import JSONResponse
    return JSONResponse(content=data, headers={"Cache-Control": "no-store"})
