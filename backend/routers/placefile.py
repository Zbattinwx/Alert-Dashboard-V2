"""
Placefile routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
import aiohttp
import asyncio

logger = logging.getLogger(__name__)

router = APIRouter()


_PLACEFILE_MAX_BYTES = 5 * 1024 * 1024  # placefiles + icon sheets are small

def _placefile_url_ok(url: str) -> bool:
    """SSRF guard for the placefile proxy: http/https only, and the host must
    not resolve to loopback/private/link-local/reserved space (the dashboard is
    reachable over DDNS, so this endpoint is not local-only in practice)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False
    return True


@router.get("/api/placefile")
async def proxy_placefile(url: str = Query(..., description="GR placefile (or icon image) URL")):
    """Proxy a GR placefile or its icon image so the browser can load it despite
    CORS. Presents a GR-compatible User-Agent (placefile hosts gate on it).
    SSRF-guarded: public http/https hosts only, redirects re-validated per hop,
    response size capped."""
    from fastapi.responses import Response

    if not await asyncio.to_thread(_placefile_url_ok, url):  # DNS lookup — off the loop
        raise HTTPException(status_code=400, detail="URL not allowed")
    try:
        async with aiohttp.ClientSession() as session:
            # Follow up to 3 redirects manually so every hop is re-validated —
            # an allowed public host could otherwise redirect us to the LAN.
            target = url
            for _ in range(4):
                async with session.get(
                    target,
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers={"User-Agent": "GRLevel3 2.0 (TheBattinFront Radar placefile client)"},
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            raise HTTPException(status_code=502, detail="Bad redirect from upstream")
                        from urllib.parse import urljoin
                        target = urljoin(target, loc)
                        if not await asyncio.to_thread(_placefile_url_ok, target):
                            raise HTTPException(status_code=400, detail="URL not allowed")
                        continue
                    if resp.status != 200:
                        raise HTTPException(status_code=502, detail=f"Upstream returned {resp.status}")
                    if int(resp.headers.get("Content-Length") or 0) > _PLACEFILE_MAX_BYTES:
                        raise HTTPException(status_code=502, detail="Upstream response too large")
                    data = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > _PLACEFILE_MAX_BYTES:
                            raise HTTPException(status_code=502, detail="Upstream response too large")
                    ctype = resp.headers.get("Content-Type", "text/plain; charset=utf-8")
                    return Response(content=bytes(data), media_type=ctype, headers={"Cache-Control": "no-store"})
            raise HTTPException(status_code=502, detail="Too many redirects")
    except aiohttp.ClientError as e:
        logger.error(f"Placefile proxy fetch failed for {url}: {e}")
        raise HTTPException(status_code=502, detail="Fetch failed")
