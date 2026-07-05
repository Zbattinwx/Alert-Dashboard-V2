"""
Self-update service for the standalone AlertDashboardV2 Windows deployment.

The remote/team server runs a PyInstaller-frozen bundle deployed as a folder:

    AlertDashboardV2-Server\\
        dashboard-backend\\dashboard-backend.exe   <- sys.executable (frozen)
        start-server.bat  update.bat  apply-update.ps1  Caddyfile
        version.json        <- {"app": "2.0.0", "build": "<UTC yyyyMMddHHmmss>"}

This service backs the in-dashboard "Update available -> Update now" banner so the
remote server updates itself from GitHub Releases instead of someone copying a zip:

    status()  reads the local build id (version.json) and the latest published
              manifest (release latest.json, cached with a TTL) and reports
              whether a newer build exists.
    apply()   spawns the DETACHED updater (apply-update.ps1) which downloads the
              new bundle, verifies its SHA-256, then stops/backs-up/swaps/restarts
              the server. Detached so it survives the backend being killed.

Only meaningful in the frozen Windows deployment. On a source run (no version.json)
it reports no build and apply() is a no-op, so local dev is unaffected.
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

from ..config import get_settings

try:
    from .. import __version__ as APP_VERSION
except Exception:  # pragma: no cover - defensive
    APP_VERSION = "2.0.0"

logger = logging.getLogger(__name__)

# Windows process-creation flags: run the updater detached + windowless so it
# outlives the backend that update.bat/apply-update.ps1 will kill.
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


class UpdateService:
    """Checks for and applies standalone-dashboard updates."""

    def __init__(self) -> None:
        self._manifest: Optional[dict] = None
        self._manifest_at: float = 0.0
        self._applying: bool = False

    # ------------------------------------------------------------------ paths
    @staticmethod
    def is_frozen() -> bool:
        return bool(getattr(sys, "frozen", False))

    def deploy_root(self) -> Path:
        """The folder that holds start-server.bat / update.bat / version.json."""
        if self.is_frozen():
            # onedir layout: <root>/dashboard-backend/dashboard-backend.exe
            return Path(sys.executable).resolve().parent.parent
        # source run: repo root (backend/services/update_service.py -> repo)
        return Path(__file__).resolve().parents[2]

    def _version_file(self) -> Path:
        return self.deploy_root() / "version.json"

    def local_build(self) -> Optional[str]:
        """The deployed build id (a sortable UTC stamp), or None if unknown."""
        vf = self._version_file()
        try:
            if vf.exists():
                # utf-8-sig: build-windows.bat writes version.json via PowerShell
                # Set-Content, which prepends a UTF-8 BOM that plain utf-8 chokes on.
                data = json.loads(vf.read_text(encoding="utf-8-sig"))
                build = str(data.get("build") or "").strip()
                return build or None
        except Exception as e:
            logger.warning(f"update: could not read {vf}: {e}")
        return None

    def local_version_display(self) -> str:
        build = self.local_build()
        return f"{APP_VERSION} (build {build})" if build else f"{APP_VERSION} (dev)"

    # --------------------------------------------------------------- manifest
    async def _fetch_manifest(self, force: bool = False) -> Optional[dict]:
        """Fetch the release manifest, cached for the configured interval."""
        settings = get_settings()
        ttl = max(60, settings.dashboard_update_check_interval_minutes * 60)
        now = time.monotonic()
        if not force and self._manifest is not None and (now - self._manifest_at) < ttl:
            return self._manifest

        url = settings.dashboard_update_manifest_url
        if not url:
            return self._manifest
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.info(f"update: manifest fetch HTTP {resp.status}")
                        return self._manifest
                    # GitHub serves latest.json as application/octet-stream
                    data = await resp.json(content_type=None)
            if isinstance(data, dict):
                self._manifest = data
                self._manifest_at = now
        except Exception as e:
            logger.info(f"update: manifest fetch failed: {e}")
        return self._manifest

    @staticmethod
    def _is_newer(remote_build: Optional[str], local_build: Optional[str]) -> bool:
        """True only when we can confirm remote > local. Unknown either side -> False
        (never offer an update we can't reason about)."""
        if not remote_build or not local_build:
            return False
        try:
            return int(remote_build) > int(local_build)
        except ValueError:
            return str(remote_build) > str(local_build)

    # ----------------------------------------------------------------- public
    async def status(self) -> dict:
        settings = get_settings()
        local = self.local_build()
        manifest = await self._fetch_manifest() if settings.dashboard_update_enabled else None
        remote = None
        if manifest and manifest.get("build"):
            remote = str(manifest.get("build"))
        available = bool(
            settings.dashboard_update_enabled
            and self.is_frozen()
            and self._is_newer(remote, local)
        )
        return {
            "enabled": bool(settings.dashboard_update_enabled),
            "frozen": self.is_frozen(),
            "current": local,
            "current_display": self.local_version_display(),
            "latest": remote,
            "update_available": available,
            "notes": (manifest or {}).get("notes"),
            "pub_date": (manifest or {}).get("pub_date"),
            "applying": self._applying,
        }

    async def apply(self) -> dict:
        """Spawn the detached updater if (and only if) a newer build exists."""
        settings = get_settings()
        if not settings.dashboard_update_enabled:
            return {"started": False, "message": "Updates are disabled on this server."}
        if not self.is_frozen():
            return {"started": False, "message": "Updates only apply to the packaged Windows build."}
        if self._applying:
            return {"started": True, "message": "An update is already in progress."}

        manifest = await self._fetch_manifest(force=True)
        local = self.local_build()
        remote = str(manifest.get("build")) if manifest and manifest.get("build") else None
        if not self._is_newer(remote, local):
            return {"started": False, "message": "Already up to date."}

        url = (manifest or {}).get("url")
        sha = (manifest or {}).get("sha256")
        if not url or not sha:
            return {"started": False, "message": "The update manifest is missing a download URL or checksum."}

        root = self.deploy_root()
        script = root / "apply-update.ps1"
        if not script.exists():
            return {"started": False, "message": "Updater script (apply-update.ps1) not found next to the app."}

        try:
            subprocess.Popen(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                    "-Url", str(url), "-Sha256", str(sha),
                    "-DeployRoot", str(root), "-Build", remote or "",
                ],
                cwd=str(root),
                creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
                close_fds=True,
            )
        except Exception as e:
            logger.error(f"update: failed to spawn updater: {e}")
            return {"started": False, "message": f"Could not start the updater: {e}"}

        self._applying = True
        logger.info(f"update: spawned updater for build {remote}")
        return {
            "started": True,
            "message": f"Updating to build {remote}. The dashboard will restart in a moment.",
        }


_service: Optional[UpdateService] = None


def get_update_service() -> UpdateService:
    global _service
    if _service is None:
        _service = UpdateService()
    return _service
