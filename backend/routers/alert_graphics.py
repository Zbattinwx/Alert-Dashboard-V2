"""
Alert Graphics routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..paths import _GRAPHICS_DIR
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/alert-graphics/save")
async def save_alert_graphic(request: Request):
    """Save a generated alert graphic PNG (base64) to disk."""
    import base64 as _base64
    data = await request.json()
    product_id = data.get("product_id", "").strip()
    event_name = data.get("event_name", "")
    image_data: str = data.get("image_data", "")

    if not product_id or not image_data:
        raise HTTPException(status_code=400, detail="Missing product_id or image_data")

    # Strip data URL prefix if present
    if "," in image_data:
        image_data = image_data.split(",", 1)[1]

    try:
        img_bytes = _base64.b64decode(image_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)

    # Safe filename: replace anything that isn't alphanumeric, dash, or dot
    import re as _re
    safe_id = _re.sub(r"[^\w\-.]", "_", product_id)
    img_path = _GRAPHICS_DIR / f"{safe_id}.png"
    img_path.write_bytes(img_bytes)

    # Save metadata sidecar so we can show event_name in the gallery
    meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
    import json as _json
    meta_path.write_text(_json.dumps({"product_id": product_id, "event_name": event_name}))

    return {"status": "saved", "product_id": product_id}
@router.get("/api/alert-graphics")
async def list_alert_graphics():
    """List all saved alert graphics, newest first."""
    import json as _json
    if not _GRAPHICS_DIR.exists():
        return {"graphics": []}

    graphics = []
    for img_path in sorted(
        _GRAPHICS_DIR.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        product_id = img_path.stem
        event_name = product_id
        meta_path = img_path.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = _json.loads(meta_path.read_text())
                product_id = meta.get("product_id", product_id)
                event_name = meta.get("event_name", event_name)
            except Exception:
                pass
        graphics.append({
            "product_id": product_id,
            "event_name": event_name,
            "url": f"/api/alert-graphics/image/{img_path.name}",
            "created_at": datetime.fromtimestamp(img_path.stat().st_mtime, timezone.utc).isoformat(),
        })
    return {"graphics": graphics}
@router.get("/api/alert-graphics/image/{filename}")
async def get_alert_graphic_image(filename: str):
    """Serve a saved alert graphic PNG."""
    import re as _re
    if not _re.match(r"^[\w\-.]+\.png$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    img_path = _GRAPHICS_DIR / filename
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Graphic not found")
    return FileResponse(str(img_path), media_type="image/png")
@router.delete("/api/alert-graphics/{product_id}")
async def delete_alert_graphic(product_id: str):
    """Delete a saved alert graphic."""
    import re as _re
    safe_id = _re.sub(r"[^\w\-.]", "_", product_id)
    img_path = _GRAPHICS_DIR / f"{safe_id}.png"
    meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
    if img_path.exists():
        img_path.unlink()
    if meta_path.exists():
        meta_path.unlink()
    return {"status": "deleted"}
