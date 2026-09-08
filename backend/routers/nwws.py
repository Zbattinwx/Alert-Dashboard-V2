"""
Nwws routes.

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
    get_nwws_handler,
    get_nwws_products_service,
)
from fastapi import Query
from pydantic import BaseModel
from pydantic import Field
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


class NWWSCredentialsUpdate(BaseModel):
    username: str = Field(default="", description="NWWS-OI username (blank to clear)")
    password: str = Field(default="", description="NWWS-OI password (blank to clear)")


@router.get("/api/nwws/status")
async def get_nwws_status():
    """Whether NWWS is configured and connected (for the app's setup prompt)."""
    settings = get_settings()
    handler = get_nwws_handler()
    return {
        "configured": bool(settings.nwws_username and settings.nwws_password),
        "connected": handler.is_connected if handler else False,
        "username": settings.nwws_username or None,
    }
@router.post("/api/nwws/credentials")
async def set_nwws_credentials(update: NWWSCredentialsUpdate):
    """Save (or clear) the user's NWWS-OI credentials and reconnect.

    Blank username/password clears the stored credentials → NWS-API fallback.
    """
    from ..config.settings import save_nwws_credentials, reload_settings
    from ..services import restart_nwws_handler

    username = (update.username or "").strip()
    password = (update.password or "").strip()
    save_nwws_credentials(username, password)
    reload_settings()
    try:
        await restart_nwws_handler()
    except Exception as e:
        logger.error(f"Failed to (re)start NWWS after credential change: {e}")

    settings = get_settings()
    configured = bool(settings.nwws_username and settings.nwws_password)
    logger.info(f"NWWS credentials {'set' if configured else 'cleared'} via API")
    return {
        "success": True,
        "configured": configured,
        "username": settings.nwws_username or None,
    }
@router.get("/api/nwws/products")
async def get_nwws_products_feed(
    limit: int = Query(50, ge=1, le=500, description="Number of products to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    product_type: Optional[str] = Query(None, description="Filter by product type (e.g., SVS, FFW, AFD)"),
    office: Optional[str] = Query(None, description="Filter by office code (e.g., CLE)"),
):
    """Get recent NWWS products for monitoring NWWS connection health."""
    service = get_nwws_products_service()
    nwws_handler = get_nwws_handler()

    products = service.get_products(
        limit=limit,
        offset=offset,
        product_type=product_type,
        office=office,
    )

    return {
        "count": len(products),
        "total_received": service.get_product_count(),
        "nwws_connected": nwws_handler.is_connected if nwws_handler else False,
        "products": products,
    }
@router.get("/api/nwws/stats")
async def get_nwws_products_stats():
    """Get NWWS products service statistics."""
    service = get_nwws_products_service()
    nwws_handler = get_nwws_handler()

    stats = service.get_statistics()
    stats["nwws_connected"] = nwws_handler.is_connected if nwws_handler else False
    return stats
