"""
Bluesky AT Protocol service for Alert Dashboard V2.

Posts images and text to Bluesky via the AT Protocol.
Ported from TheBattinFront/api/bsky-helpers.php.
"""

import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

BSKY_SERVICE = "https://bsky.social"
MAX_IMAGE_SIZE = 1_000_000  # 1MB
MAX_IMAGES = 4


class BlueskyService:
    """Bluesky AT Protocol posting service."""

    def __init__(self, handle: str, app_password: str):
        self._handle = handle
        self._app_password = app_password
        self._session: Optional[dict] = None
        self._stats = {
            "posts_sent": 0,
            "posts_failed": 0,
            "last_post": None,
        }

    def is_configured(self) -> bool:
        return bool(self._handle and self._app_password)

    async def _create_session(self) -> Optional[dict]:
        """Authenticate and get accessJwt + did."""
        url = f"{BSKY_SERVICE}/xrpc/com.atproto.server.createSession"
        payload = {
            "identifier": self._handle,
            "password": self._app_password,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    response = await resp.json()

            if "accessJwt" in response and "did" in response:
                self._session = response
                return response
            else:
                logger.error(f"Bluesky auth failed: {response}")
                return None
        except Exception as e:
            logger.error(f"Bluesky auth error: {e}")
            return None

    async def _ensure_session(self) -> Optional[dict]:
        """Ensure we have a valid session, creating one if needed."""
        if not self._session:
            return await self._create_session()
        return self._session

    def _compress_image(self, image_data: bytes, mime_type: str) -> tuple[bytes, str]:
        """Compress image to fit within Bluesky's 1MB limit.

        Uses Pillow for PNG->JPEG conversion and resizing.
        """
        if len(image_data) <= MAX_IMAGE_SIZE:
            return image_data, mime_type

        try:
            from PIL import Image

            img = Image.open(io.BytesIO(image_data))

            # Convert RGBA to RGB for JPEG
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (0, 0, 0))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background

            # Try JPEG at 85% quality first
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            compressed = buf.getvalue()

            if len(compressed) <= MAX_IMAGE_SIZE:
                return compressed, "image/jpeg"

            # Still too large -- resize
            scale = (MAX_IMAGE_SIZE / len(compressed)) ** 0.5 * 0.9
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue(), "image/jpeg"

        except ImportError:
            logger.warning("Pillow not installed -- cannot compress image")
            return image_data, mime_type
        except Exception as e:
            logger.error(f"Image compression error: {e}")
            return image_data, mime_type

    async def _upload_blob(
        self, image_data: bytes, mime_type: str, access_jwt: str
    ) -> Optional[dict]:
        """Upload an image blob to Bluesky. Auto-compresses if >1MB."""
        image_data, mime_type = self._compress_image(image_data, mime_type)

        url = f"{BSKY_SERVICE}/xrpc/com.atproto.repo.uploadBlob"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=image_data,
                    headers={
                        "Content-Type": mime_type,
                        "Authorization": f"Bearer {access_jwt}",
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    response = await resp.json()

            if "blob" in response:
                return response["blob"]

            logger.error(f"Bluesky blob upload failed: {response}")
            return None

        except Exception as e:
            logger.error(f"Bluesky blob upload error: {e}")
            return None

    @staticmethod
    def _detect_facets(text: str) -> list[dict]:
        """Detect URLs and hashtags with UTF-8 byte offsets for AT Protocol richtext."""
        facets = []
        text_bytes = text.encode("utf-8")

        # Detect URLs
        url_pattern = r'https?://[^\s<>\[\]()"\',]+[^\s<>\[\]()"\',.!?;:]'
        for match in re.finditer(url_pattern, text):
            url = match.group(0)
            # Calculate byte offsets
            byte_start = len(text[: match.start()].encode("utf-8"))
            byte_end = byte_start + len(url.encode("utf-8"))
            facets.append(
                {
                    "index": {"byteStart": byte_start, "byteEnd": byte_end},
                    "features": [
                        {"$type": "app.bsky.richtext.facet#link", "uri": url}
                    ],
                }
            )

        # Detect hashtags
        hash_pattern = r"(?:(?<=\s)|(?<=^))#([a-zA-Z0-9_]+)"
        for match in re.finditer(hash_pattern, text):
            hashtag = match.group(0)
            byte_start = len(text[: match.start()].encode("utf-8"))
            byte_end = byte_start + len(hashtag.encode("utf-8"))
            facets.append(
                {
                    "index": {"byteStart": byte_start, "byteEnd": byte_end},
                    "features": [
                        {
                            "$type": "app.bsky.richtext.facet#tag",
                            "tag": hashtag.lstrip("#"),
                        }
                    ],
                }
            )

        return facets

    async def _create_record(
        self, record: dict, did: str, access_jwt: str
    ) -> dict:
        """Create a post record on Bluesky."""
        url = f"{BSKY_SERVICE}/xrpc/com.atproto.repo.createRecord"
        payload = {
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": record,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {access_jwt}",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    response = await resp.json()

            if "uri" in response:
                return {"success": True, "uri": response["uri"], "error": None}

            error_msg = response.get("message", json.dumps(response))
            return {"success": False, "uri": None, "error": error_msg}

        except Exception as e:
            return {"success": False, "uri": None, "error": str(e)}

    def _build_record(self, message: str) -> dict:
        """Build a base post record with text and facets."""
        record = {
            "$type": "app.bsky.feed.post",
            "text": message,
            "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "langs": ["en"],
        }
        facets = self._detect_facets(message)
        if facets:
            record["facets"] = facets
        return record

    async def post_text(self, message: str) -> dict:
        """Post a text-only message to Bluesky."""
        session = await self._ensure_session()
        if not session:
            self._stats["posts_failed"] += 1
            return {"success": False, "uri": None, "error": "Failed to authenticate with Bluesky"}

        record = self._build_record(message)
        result = await self._create_record(record, session["did"], session["accessJwt"])

        if result["success"]:
            self._stats["posts_sent"] += 1
        else:
            self._stats["posts_failed"] += 1
        return result

    async def post_single_image(
        self,
        image_data: bytes,
        message: str,
        alt_text: str = "Weather graphic from The Battin Front",
    ) -> dict:
        """Post a single image with message to Bluesky."""
        session = await self._ensure_session()
        if not session:
            self._stats["posts_failed"] += 1
            return {"success": False, "uri": None, "error": "Failed to authenticate with Bluesky"}

        # Detect mime type from magic bytes
        mime_type = self._detect_mime_type(image_data)
        blob = await self._upload_blob(image_data, mime_type, session["accessJwt"])
        if not blob:
            self._stats["posts_failed"] += 1
            return {"success": False, "uri": None, "error": "Failed to upload image to Bluesky"}

        record = self._build_record(message)
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": [{"alt": alt_text, "image": blob}],
        }

        result = await self._create_record(record, session["did"], session["accessJwt"])
        if result["success"]:
            self._stats["posts_sent"] += 1
        else:
            self._stats["posts_failed"] += 1
        return result

    async def post_multiple_images(
        self,
        images: list[bytes],
        message: str,
        alt_texts: Optional[list[str]] = None,
    ) -> dict:
        """Post multiple images (max 4) with message to Bluesky."""
        session = await self._ensure_session()
        if not session:
            self._stats["posts_failed"] += 1
            return {"success": False, "uri": None, "error": "Failed to authenticate with Bluesky"}

        images = images[:MAX_IMAGES]
        embed_images = []

        for i, img_data in enumerate(images):
            mime_type = self._detect_mime_type(img_data)
            blob = await self._upload_blob(img_data, mime_type, session["accessJwt"])
            if not blob:
                self._stats["posts_failed"] += 1
                return {
                    "success": False,
                    "uri": None,
                    "error": f"Failed to upload image {i + 1} to Bluesky",
                }

            alt = (
                alt_texts[i]
                if alt_texts and i < len(alt_texts)
                else f"Weather graphic {i + 1} from The Battin Front"
            )
            embed_images.append({"alt": alt, "image": blob})

        if not embed_images:
            return {"success": False, "uri": None, "error": "No images uploaded"}

        record = self._build_record(message)
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": embed_images,
        }

        result = await self._create_record(record, session["did"], session["accessJwt"])
        if result["success"]:
            self._stats["posts_sent"] += 1
        else:
            self._stats["posts_failed"] += 1
        return result

    @staticmethod
    def _detect_mime_type(image_data: bytes) -> str:
        """Detect MIME type from image magic bytes."""
        if image_data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if image_data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if image_data[:4] == b"GIF8":
            return "image/gif"
        if image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
            return "image/webp"
        return "image/png"

    def get_stats(self) -> dict:
        return dict(self._stats)
