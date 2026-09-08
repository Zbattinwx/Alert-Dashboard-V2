"""
Afd routes.

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
    get_nwws_products_service,
)
from fastapi import HTTPException
from fastapi import Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/afd")
async def get_afd_offices():
    """Get list of offices with available AFDs."""
    service = get_nwws_products_service()
    offices = service.get_afd_offices()

    return {
        "count": len(offices),
        "offices": offices,
    }
@router.get("/api/afd/{office}")
async def get_afd(
    office: str,
    index: int = Query(0, ge=0, le=4, description="AFD index (0=latest, up to 4)"),
    fallback: bool = Query(True, description="Fetch from NWS API if not cached"),
):
    """Get AFD for a specific office. Checks NWWS cache first, then NWS API."""
    service = get_nwws_products_service()

    # Try NWWS cache first
    afd = service.get_afd(office, index=index)

    if afd:
        return {
            "source": "nwws",
            "afd": afd,
        }

    # Fallback to NWS API
    if fallback and index == 0:
        afd = await service.fetch_afd_from_api(office)
        if afd:
            return {
                "source": "api",
                "afd": afd,
            }

    raise HTTPException(
        status_code=404,
        detail=f"No AFD available for office '{office.upper()}'"
    )
@router.get("/api/afd/{office}/headlines")
async def get_afd_headlines(
    office: str,
    count: int = Query(4, ge=1, le=6, description="Number of headlines to extract"),
):
    """Extract weather headlines from an AFD for social media graphics."""
    service = get_nwws_products_service()

    # Try NWWS cache first
    afd = service.get_afd(office)

    # Fallback to API
    if not afd:
        afd = await service.fetch_afd_from_api(office)

    if not afd:
        raise HTTPException(
            status_code=404,
            detail=f"No AFD available for office '{office.upper()}'"
        )

    headlines = await service.extract_headlines_llm(afd, max_headlines=count)

    return {
        "office": afd.get("office", office.upper()),
        "wfo_name": afd.get("wfo_name", ""),
        "received_at": afd.get("received_at", ""),
        "headlines": headlines,
    }
