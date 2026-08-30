"""
Login gate for the remote Hub deployment.

WHY THIS EXISTS
---------------
This dashboard was built as a single-operator LAN tool and has no authentication
anywhere. That is fine on loopback; it is not fine on the open internet. Its
WebSocket accepts `radar_set_site`, so anyone who finds the host can change the
radar site out from under a live broadcast. Before the DDNS host is reachable
from outside, something has to stand in front of it.

WHERE THE GATE ACTUALLY IS
--------------------------
**Caddy**, not here. Caddy runs `forward_auth` against `/api/auth/verify` for
every request except the login page and these endpoints, so ONE gate covers the
static Hub build, the whole dashboard API, the WebSocket upgrade, and the
thebattinfront.com proxy. This module only issues and checks the session.

That split is deliberate: the backend binds loopback, so nothing can reach it
except through Caddy, and no route can be accidentally left unprotected by
forgetting a decorator.

OFF BY DEFAULT, ON PURPOSE
--------------------------
With `hub_auth_password` unset the gate is disabled and `/verify` always passes.
The desktop app bundles this same backend on 127.0.0.1 where a login prompt would
be pure friction, and it must keep working untouched. The remote deployment sets
the password in its own `.env`; `deploy/install-services.ps1` refuses to install
if Caddy is configured to gate a backend that reports auth disabled, so the
convenient default can't silently become an exposed one.

SESSIONS
--------
Stateless signed cookie: `<expiry>.<hmac-sha256>`, keyed by a secret that is
generated once and persisted. The password's own digest is mixed into the
signature, so **changing the password invalidates every existing session** — the
only revocation a single-operator deployment actually needs.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

try:
    from ..config import get_settings
except ImportError:  # direct execution
    from config import get_settings  # type: ignore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tbf_hub"
SESSION_TTL = 30 * 24 * 3600  # 30 days — the tablet should not re-login weekly
LOGIN_PATH = "/login.html"

# Brute-force throttle. In-memory and per-process, which is all a single-operator
# box needs; the point is to make an online password guess hopeless, not to build
# a distributed rate limiter.
_MAX_FAILS = 8
_FAIL_WINDOW = 15 * 60
_fails: dict[str, list[float]] = {}


# --------------------------------------------------------------------------- #
# secret
# --------------------------------------------------------------------------- #
def _secret_file() -> Path:
    # Beside the running server (the scheduled task sets its working directory),
    # matching how the rest of the app treats data/.
    return Path("data") / "hub_auth_secret"


def _secret() -> bytes:
    """Signing key: explicit setting if given, else generated once and persisted.

    Persisting matters — a fresh key per restart would silently log the tablet out
    every time the media server reboots for Windows Update.
    """
    configured = getattr(get_settings(), "hub_auth_secret", None)
    if configured:
        return str(configured).encode()

    f = _secret_file()
    try:
        if f.exists():
            data = f.read_text(encoding="utf-8").strip()
            if data:
                return data.encode()
    except OSError as e:
        logger.warning(f"Could not read hub auth secret: {e}")

    generated = secrets.token_urlsafe(48)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(generated, encoding="utf-8")
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass  # best effort; Windows ACLs are not POSIX modes
    except OSError as e:
        # Not fatal, but say so loudly: sessions won't survive a restart.
        logger.warning(f"Could not persist hub auth secret ({e}); sessions reset on restart")
    return generated.encode()


def _password() -> Optional[str]:
    pw = getattr(get_settings(), "hub_auth_password", None)
    pw = (pw or "").strip()
    return pw or None


def auth_enabled() -> bool:
    return _password() is not None


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
def _pw_digest() -> str:
    """Short digest of the current password, mixed into the signature so that
    changing the password invalidates outstanding sessions."""
    return hashlib.sha256((_password() or "").encode()).hexdigest()[:16]


def _sign(expiry: int) -> str:
    msg = f"{expiry}.{_pw_digest()}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def _issue() -> str:
    expiry = int(time.time()) + SESSION_TTL
    return f"{expiry}.{_sign(expiry)}"


def _valid(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    raw_exp, _, sig = token.partition(".")
    try:
        expiry = int(raw_exp)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    return hmac.compare_digest(sig, _sign(expiry))


def request_is_authed(request: Request) -> bool:
    if not auth_enabled():
        return True
    return _valid(request.cookies.get(COOKIE_NAME))


# --------------------------------------------------------------------------- #
# throttle
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttled(ip: str) -> int:
    """Seconds the caller must wait, or 0."""
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
    _fails[ip] = hits
    if len(hits) < _MAX_FAILS:
        return 0
    return int(_FAIL_WINDOW - (now - hits[0])) + 1


def _record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    password: str


@router.get("/status")
async def auth_status():
    """Whether the gate is armed. Public on purpose — the login page uses it to
    tell 'wrong password' from 'auth isn't configured', and install-services.ps1
    uses it to refuse a deployment that would be exposed."""
    return {"enabled": auth_enabled()}


@router.get("/verify")
async def auth_verify(request: Request):
    """Caddy's forward_auth target. 204 = let it through.

    A failed check answers differently depending on who asked: a browser opening
    a page gets redirected to the login screen, while anything else (fetch, XHR,
    the WebSocket handshake) gets a plain 401 it can handle. Redirecting an API
    call would hand it an HTML login page as if it were data.
    """
    if request_is_authed(request):
        return Response(status_code=204)

    original = request.headers.get("x-forwarded-uri", "/")
    accept = request.headers.get("accept", "")
    mode = request.headers.get("sec-fetch-mode", "")
    is_navigation = mode == "navigate" or "text/html" in accept

    if is_navigation:
        nxt = original if original.startswith("/") else "/"
        # PERCENT-ENCODE. The original URI carries its own query string, and the
        # app's deep links use several parameters (?site=...&dashboard=...) --
        # dropped in raw, everything after the first & would be parsed as a
        # parameter of the LOGIN url instead, and the round-trip would silently
        # lose it. Caught by a two-hop browser test, 2026-08-20.
        return RedirectResponse(url=f"{LOGIN_PATH}?next={quote(nxt, safe='')}", status_code=302)
    return JSONResponse({"error": "authentication required"}, status_code=401)


@router.post("/login")
async def auth_login(body: LoginBody, request: Request):
    if not auth_enabled():
        # Nothing to log into. Say so rather than pretending to succeed.
        return JSONResponse({"error": "authentication is not configured"}, status_code=400)

    ip = _client_ip(request)
    wait = _throttled(ip)
    if wait:
        return JSONResponse(
            {"error": f"Too many attempts. Try again in {wait // 60 + 1} minute(s)."},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )

    if not hmac.compare_digest(body.password, _password() or ""):
        _record_fail(ip)
        logger.warning(f"Hub login failed from {ip}")
        return JSONResponse({"error": "Incorrect password."}, status_code=401)

    _fails.pop(ip, None)
    logger.info(f"Hub login succeeded from {ip}")

    resp = Response(status_code=204)
    resp.set_cookie(
        COOKIE_NAME,
        _issue(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        # Only mark Secure when the edge actually served HTTPS — otherwise the
        # cookie would be dropped on a plain-HTTP LAN deployment and login would
        # appear to succeed while nothing ever stayed logged in.
        secure=request.headers.get("x-forwarded-proto", "").lower() == "https",
        path="/",
    )
    return resp


@router.post("/logout")
async def auth_logout():
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
