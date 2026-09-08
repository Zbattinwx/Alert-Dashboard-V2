"""
Brand routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..config import brands_dir
from ..config import get_brand_config
from ..config import get_settings
from ..paths import FRONTEND_DIR
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import FileResponse
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_brand_id(brand: Optional[str]) -> str:
    """Resolve a requested white-label brand id, falling back to the active one
    if none/invalid. Lets a stream overlay switch branding via ?brand=<id>."""
    settings = get_settings()
    if brand:
        cand = brand.strip().lower()
        if (brands_dir() / f"{cand}.json").exists():
            return cand
    return settings.brand


@router.get("/api/brand")
async def get_brand(brand: Optional[str] = Query(None, description="White-label brand override; defaults to the active brand")):
    """Get brand configuration for the frontend + radar app (white-label).

    Optional ?brand=<id> serves a specific brand (when a config exists) so a
    stream overlay can switch branding by URL; otherwise the active brand.
    """
    brand_id = _resolve_brand_id(brand)
    bcfg = get_brand_config(brand_id)
    return {
        "brand_id": brand_id,
        "name": bcfg.name,
        "short_name": bcfg.short_name,
        "tagline": bcfg.tagline,
        "logo": bcfg.logo,
        "logo_url": f"/api/brand/logo?brand={brand_id}",  # matches the returned brand
        "logo_is_wordmark": bcfg.logo_is_wordmark,
        "colors": bcfg.colors.model_dump(),  # semantic palette; clients map to their own CSS vars
        "website_url": bcfg.website_url,
        "social_twitter": bcfg.social_twitter,
        "css_overrides": bcfg.css_overrides,
    }
@router.get("/api/brand/logo")
async def get_brand_logo(brand: Optional[str] = Query(None, description="White-label brand override; defaults to the active brand")):
    """Serve a brand's logo (white-label), with default/TBF fallback."""
    bcfg = get_brand_config(_resolve_brand_id(brand))
    path = bcfg.get_asset_path(bcfg.logo, brands_dir())
    if path.exists():
        return FileResponse(path)
    fallback = FRONTEND_DIR / "tbf_logo.png"
    if fallback.exists():
        return FileResponse(fallback, media_type="image/png")
    raise HTTPException(status_code=404, detail="Brand logo not found")
