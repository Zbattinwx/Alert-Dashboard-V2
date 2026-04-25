"""
Facebook Graph API service for Alert Dashboard V2.

Posts images and text to a Facebook Page via the Graph API.
Ported from TheBattinFront/api/fb-helpers.php.
"""

import json
import logging
from io import BytesIO
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v18.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class FacebookService:
    """Facebook Graph API posting service."""

    def __init__(self, page_id: str, access_token: str):
        self._page_id = page_id
        self._access_token = access_token
        self._stats = {
            "posts_sent": 0,
            "posts_failed": 0,
            "last_post": None,
        }

    def is_configured(self) -> bool:
        return bool(self._page_id and self._access_token)

    async def _upload_photo(
        self, image_data: bytes, published: bool = True, message: str = ""
    ) -> dict:
        """Upload a photo to the Facebook Page.

        Args:
            image_data: Raw binary image data.
            published: If True, publish immediately. If False, upload as unpublished.
            message: Caption (only used if published=True).

        Returns:
            Facebook API response dict.
        """
        url = f"{GRAPH_API_BASE}/{self._page_id}/photos"

        data = aiohttp.FormData()
        data.add_field("access_token", self._access_token)
        data.add_field(
            "source",
            BytesIO(image_data),
            filename="weather.png",
            content_type="image/png",
        )
        data.add_field("published", "true" if published else "false")

        if published and message:
            data.add_field("message", message)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=data, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"Facebook photo upload error: {e}")
            return {"error": {"message": str(e)}}

    async def post_single_image(self, image_data: bytes, message: str) -> dict:
        """Post a single image with message.

        Returns:
            {success: bool, post_id: str|None, error: str|None}
        """
        result = await self._upload_photo(image_data, published=True, message=message)

        if "id" in result:
            self._stats["posts_sent"] += 1
            return {"success": True, "post_id": result["id"], "error": None}

        error_msg = result.get("error", {}).get("message", json.dumps(result))
        self._stats["posts_failed"] += 1
        return {"success": False, "post_id": None, "error": error_msg}

    async def post_multiple_images(
        self, images: list[bytes], message: str
    ) -> dict:
        """Post multiple images with message.

        Uploads each image as unpublished, then creates a feed post with attached_media.
        """
        media_ids = []

        for i, image_data in enumerate(images):
            result = await self._upload_photo(image_data, published=False)
            if "id" in result:
                media_ids.append({"media_fbid": result["id"]})
            else:
                error_msg = result.get("error", {}).get("message", "Unknown upload error")
                self._stats["posts_failed"] += 1
                return {
                    "success": False,
                    "post_id": None,
                    "error": f"Failed uploading image {i + 1}: {error_msg}",
                }

        if not media_ids:
            return {"success": False, "post_id": None, "error": "No images uploaded"}

        # Create feed post with attached media
        url = f"{GRAPH_API_BASE}/{self._page_id}/feed"
        post_data = {
            "message": message,
            "access_token": self._access_token,
            "attached_media": json.dumps(media_ids),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=post_data, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    response = await resp.json()

            if "id" in response:
                self._stats["posts_sent"] += 1
                return {"success": True, "post_id": response["id"], "error": None}

            error_msg = response.get("error", {}).get("message", json.dumps(response))
            self._stats["posts_failed"] += 1
            return {"success": False, "post_id": None, "error": error_msg}

        except Exception as e:
            self._stats["posts_failed"] += 1
            return {"success": False, "post_id": None, "error": str(e)}

    async def post_text(self, message: str) -> dict:
        """Post a text-only message."""
        url = f"{GRAPH_API_BASE}/{self._page_id}/feed"
        post_data = {
            "access_token": self._access_token,
            "message": message,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=post_data, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    response = await resp.json()

            if "id" in response:
                self._stats["posts_sent"] += 1
                return {"success": True, "post_id": response["id"], "error": None}

            error_msg = response.get("error", {}).get("message", json.dumps(response))
            self._stats["posts_failed"] += 1
            return {"success": False, "post_id": None, "error": error_msg}

        except Exception as e:
            self._stats["posts_failed"] += 1
            return {"success": False, "post_id": None, "error": str(e)}

    async def post_link(self, message: str, link: str) -> dict:
        """Post a link share."""
        url = f"{GRAPH_API_BASE}/{self._page_id}/feed"
        post_data = {
            "access_token": self._access_token,
            "message": message,
            "link": link,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=post_data, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    response = await resp.json()

            if "id" in response:
                self._stats["posts_sent"] += 1
                return {"success": True, "post_id": response["id"], "error": None}

            error_msg = response.get("error", {}).get("message", json.dumps(response))
            self._stats["posts_failed"] += 1
            return {"success": False, "post_id": None, "error": error_msg}

        except Exception as e:
            self._stats["posts_failed"] += 1
            return {"success": False, "post_id": None, "error": str(e)}

    def get_stats(self) -> dict:
        return dict(self._stats)
