"""
Unified Social Media Service for Alert Dashboard V2.

Orchestrates posting to Facebook and Bluesky, manages post history,
and generates text from alert/LSR data via templates.
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from ...config import get_settings
from .facebook_service import FacebookService
from .bluesky_service import BlueskyService
from .templates import render_alert_template, render_lsr_template

logger = logging.getLogger(__name__)

MAX_HISTORY = 50


class SocialMediaService:
    """Unified service orchestrating Facebook + Bluesky posting."""

    def __init__(self):
        self._facebook: Optional[FacebookService] = None
        self._bluesky: Optional[BlueskyService] = None
        self._post_history: list[dict] = []

    def initialize(self):
        """Create sub-services from current settings."""
        settings = get_settings()

        if settings.fb_enabled and settings.fb_access_token:
            self._facebook = FacebookService(
                page_id=settings.fb_page_id,
                access_token=settings.fb_access_token,
            )
            logger.info(f"Facebook service initialized (page: {settings.fb_page_id})")
        else:
            logger.info("Facebook posting disabled or not configured")

        if settings.bsky_enabled and settings.bsky_app_password:
            self._bluesky = BlueskyService(
                handle=settings.bsky_handle,
                app_password=settings.bsky_app_password,
            )
            logger.info(f"Bluesky service initialized (handle: {settings.bsky_handle})")
        else:
            logger.info("Bluesky posting disabled or not configured")

    def get_status(self) -> dict:
        """Return configuration status for both platforms. Never exposes tokens."""
        settings = get_settings()
        return {
            "facebook": {
                "enabled": settings.fb_enabled,
                "configured": bool(self._facebook and self._facebook.is_configured()),
                "page_id": settings.fb_page_id,
            },
            "bluesky": {
                "enabled": settings.bsky_enabled,
                "configured": bool(self._bluesky and self._bluesky.is_configured()),
                "handle": settings.bsky_handle,
            },
        }

    async def post(
        self,
        platforms: list[str],
        message: str,
        images: Optional[list[bytes]] = None,
        alt_text: str = "Weather graphic from The Battin Front",
    ) -> dict:
        """Post to one or more platforms.

        Args:
            platforms: ["facebook"], ["bluesky"], or ["both"] / ["facebook", "bluesky"]
            message: Post text.
            images: Optional list of raw image bytes.
            alt_text: Alt text for images (Bluesky).

        Returns:
            {facebook: {success, post_id, error}, bluesky: {success, uri, error}}
        """
        # Normalize platforms
        target_platforms = set()
        for p in platforms:
            if p == "both":
                target_platforms.update(["facebook", "bluesky"])
            else:
                target_platforms.add(p)

        results = {}

        # Post to Facebook
        if "facebook" in target_platforms:
            if self._facebook and self._facebook.is_configured():
                if images and len(images) == 1:
                    results["facebook"] = await self._facebook.post_single_image(
                        images[0], message
                    )
                elif images and len(images) > 1:
                    results["facebook"] = await self._facebook.post_multiple_images(
                        images, message
                    )
                else:
                    results["facebook"] = await self._facebook.post_text(message)
            else:
                results["facebook"] = {
                    "success": False,
                    "post_id": None,
                    "error": "Facebook not configured",
                }

        # Post to Bluesky
        if "bluesky" in target_platforms:
            if self._bluesky and self._bluesky.is_configured():
                if images and len(images) == 1:
                    results["bluesky"] = await self._bluesky.post_single_image(
                        images[0], message, alt_text=alt_text
                    )
                elif images and len(images) > 1:
                    results["bluesky"] = await self._bluesky.post_multiple_images(
                        images, message
                    )
                else:
                    results["bluesky"] = await self._bluesky.post_text(message)
            else:
                results["bluesky"] = {
                    "success": False,
                    "uri": None,
                    "error": "Bluesky not configured",
                }

        # Record in history
        any_success = any(
            r.get("success", False) for r in results.values()
        )
        history_entry = {
            "id": str(uuid4()),
            "platforms": list(target_platforms),
            "message": message[:200],
            "has_image": bool(images),
            "image_count": len(images) if images else 0,
            "results": results,
            "success": any_success,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post_history.insert(0, history_entry)
        if len(self._post_history) > MAX_HISTORY:
            self._post_history = self._post_history[:MAX_HISTORY]

        log_level = logging.INFO if any_success else logging.ERROR
        logger.log(
            log_level,
            f"Social media post {'succeeded' if any_success else 'failed'}: "
            f"platforms={list(target_platforms)}, results={results}",
        )

        return results

    def generate_alert_text(
        self, alert_data: dict, template_name: str = "default"
    ) -> str:
        """Generate post text from alert data using templates."""
        return render_alert_template(alert_data, template_name)

    def generate_lsr_text(
        self, reports: list[dict], template_name: str = "summary"
    ) -> str:
        """Generate post text from storm report data."""
        return render_lsr_template(reports, template_name)

    def get_post_history(self) -> list[dict]:
        """Return recent post history."""
        return list(self._post_history)

    def get_statistics(self) -> dict:
        """Get combined statistics."""
        return {
            "status": self.get_status(),
            "history_count": len(self._post_history),
            "facebook_stats": self._facebook.get_stats() if self._facebook else None,
            "bluesky_stats": self._bluesky.get_stats() if self._bluesky else None,
        }


# Global service instance
_service: Optional[SocialMediaService] = None


def get_social_media_service() -> SocialMediaService:
    """Get the global SocialMediaService instance."""
    global _service
    if _service is None:
        _service = SocialMediaService()
    return _service


async def start_social_media_service() -> bool:
    """Start the social media service."""
    global _service
    _service = SocialMediaService()
    _service.initialize()

    status = _service.get_status()
    fb_ok = status["facebook"]["configured"]
    bsky_ok = status["bluesky"]["configured"]

    if fb_ok or bsky_ok:
        platforms = []
        if fb_ok:
            platforms.append("Facebook")
        if bsky_ok:
            platforms.append("Bluesky")
        logger.info(f"Social media service started: {', '.join(platforms)}")
        return True
    else:
        logger.info("Social media service started (no platforms configured)")
        return False


async def stop_social_media_service():
    """Stop the social media service."""
    global _service
    if _service:
        logger.info(f"Social media service stopped. Stats: {_service.get_statistics()}")
        _service = None
