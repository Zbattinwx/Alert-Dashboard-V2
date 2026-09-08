"""
Stream routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..runtime_state import get_radar_product, set_radar_product
from ..services import (
    get_message_broker,
)
from fastapi import Query
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/stream/radar-product")
async def stream_radar_product(value: Optional[str] = Query(None, alias="set")):
    """
    Get or set the on-air radar product label for the stream overlay.

    - `GET /api/stream/radar-product`            → current product
    - `GET /api/stream/radar-product?set=velocity` → set it + broadcast to overlays

    GET-to-set keeps it trivial to call from AutoHotkey / curl on localhost.
    """
    # Accessors, not `global`: the name lives in backend.runtime_state and a
    # `global` here would bind in THIS module, where it does not exist.
    if value is not None:
        product = set_radar_product(value)
        await get_message_broker().broadcast_radar_product(product)
    return {"product": get_radar_product()}
