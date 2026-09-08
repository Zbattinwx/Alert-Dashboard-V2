"""
Social routes.

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
    get_social_media_service,
)
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


class SocialPostRequest(BaseModel):
    """Request body for posting to social media."""
    platforms: list[str]
    message: str
    images: list[str] = []  # Base64-encoded images
    alt_text: str = "Weather graphic from The Battin Front"

class GenerateTextRequest(BaseModel):
    """Request body for generating post text from alert/LSR data."""
    source_type: str  # "alert" or "lsr"
    source_id: Optional[str] = None
    source_data: Optional[dict] = None
    template: str = "default"


@router.get("/api/social/status")
async def social_media_status():
    """Check if social media services are configured and available."""
    service = get_social_media_service()
    return service.get_status()
@router.post("/api/social/post")
async def social_media_post(request: SocialPostRequest):
    """Post to one or more social media platforms."""
    service = get_social_media_service()

    # Decode base64 images to bytes
    images = None
    if request.images:
        import base64 as b64
        images = []
        for img_str in request.images:
            # Strip data URI prefix if present
            if ";base64," in img_str:
                img_str = img_str.split(";base64,", 1)[1]
            images.append(b64.b64decode(img_str))

    result = await service.post(
        platforms=request.platforms,
        message=request.message,
        images=images,
        alt_text=request.alt_text,
    )
    return result
@router.post("/api/social/generate-text")
async def social_media_generate_text(request: GenerateTextRequest):
    """Generate post text from alert or LSR data using templates."""
    service = get_social_media_service()

    if request.source_type == "alert":
        if request.source_id:
            alert_manager = get_alert_manager()
            alert = alert_manager.get_alert(request.source_id)
            if not alert:
                raise HTTPException(status_code=404, detail="Alert not found")
            text = service.generate_alert_text(alert.to_dict(), request.template)
        elif request.source_data:
            text = service.generate_alert_text(request.source_data, request.template)
        else:
            raise HTTPException(status_code=400, detail="source_id or source_data required")
    elif request.source_type == "lsr":
        if not request.source_data:
            raise HTTPException(status_code=400, detail="source_data required for LSR")
        reports = request.source_data.get("reports", [request.source_data])
        text = service.generate_lsr_text(reports, request.template)
    else:
        raise HTTPException(status_code=400, detail="source_type must be 'alert' or 'lsr'")

    return {"text": text, "template": request.template}
@router.get("/api/social/history")
async def social_media_history():
    """Get recent post history."""
    service = get_social_media_service()
    return {"posts": service.get_post_history()}
@router.get("/api/social/templates")
async def social_media_templates():
    """Get available post templates."""
    try:
        from ..services.social_media.templates import ALERT_TEMPLATES, LSR_TEMPLATES
    except ImportError:
        from backend.services.social_media.templates import ALERT_TEMPLATES, LSR_TEMPLATES
    return {
        "alert_templates": list(ALERT_TEMPLATES.keys()),
        "lsr_templates": list(LSR_TEMPLATES.keys()),
    }
