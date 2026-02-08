"""
Radar Snapshot Service for Alert Dashboard V2.

Captures radar reflectivity images from Iowa State Mesonet RadMap API
and saves them with a chaser location overlay for chase log records.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class RadarService:
    """Captures radar images for chase log."""

    # Iowa State Mesonet RadMap API
    RADMAP_URL = "https://mesonet.agron.iastate.edu/GIS/radmap.php"

    def __init__(self, data_dir: Path):
        self._radar_dir = data_dir / "chase_logs" / "radar"
        self._radar_dir.mkdir(parents=True, exist_ok=True)
        self._capture_interval = 120  # seconds between captures
        self._last_capture_time: float = 0
        self._bbox_padding = 1.5  # degrees around chaser position

    async def capture_radar_snapshot(
        self,
        lat: float,
        lon: float,
        label: str = "",
    ) -> Optional[str]:
        """
        Capture a radar image centered on the given position.

        Returns the saved filename (relative to chase_logs/) or None on failure.
        Throttled to every self._capture_interval seconds.
        """
        now = time.time()
        if now - self._last_capture_time < self._capture_interval:
            return None

        self._last_capture_time = now

        try:
            # Build RadMap URL
            bbox = (
                f"{lon - self._bbox_padding},{lat - 1},"
                f"{lon + self._bbox_padding},{lat + 1}"
            )
            params = {
                "layers[]": ["nexrad", "uscounties", "sbw"],
                "bbox": bbox,
                "width": "800",
                "height": "600",
            }

            # Fetch radar image
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.RADMAP_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"RadMap API returned HTTP {resp.status}")
                        return None

                    image_bytes = await resp.read()

            if not image_bytes or len(image_bytes) < 1000:
                logger.warning("RadMap returned empty or too-small image")
                return None

            # Overlay chaser position marker using Pillow
            image_bytes = self._overlay_chaser_marker(
                image_bytes, lat, lon, bbox, label
            )

            # Save to disk
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            filename = f"{timestamp}_reflectivity.png"
            filepath = self._radar_dir / filename

            with open(filepath, "wb") as f:
                f.write(image_bytes)

            logger.info(f"Radar snapshot saved: {filename}")
            return f"radar/{filename}"

        except asyncio.TimeoutError:
            logger.warning("RadMap API request timed out")
            return None
        except Exception as e:
            logger.error(f"Radar snapshot error: {e}")
            return None

    def _overlay_chaser_marker(
        self,
        image_bytes: bytes,
        lat: float,
        lon: float,
        bbox_str: str,
        label: str,
    ) -> bytes:
        """Overlay a red crosshair marker at the chaser's position on the image."""
        try:
            from PIL import Image, ImageDraw, ImageFont

            # Parse bbox
            parts = [float(x) for x in bbox_str.split(",")]
            xmin, ymin, xmax, ymax = parts

            img = Image.open(BytesIO(image_bytes))
            w, h = img.size

            # Convert lat/lon to pixel coordinates
            px = int((lon - xmin) / (xmax - xmin) * w)
            py = int((ymax - lat) / (ymax - ymin) * h)

            draw = ImageDraw.Draw(img)

            # Draw crosshair
            size = 12
            color = (255, 0, 0)
            draw.line([(px - size, py), (px + size, py)], fill=color, width=2)
            draw.line([(px, py - size), (px, py + size)], fill=color, width=2)
            # Circle around crosshair
            draw.ellipse(
                [(px - size, py - size), (px + size, py + size)],
                outline=color,
                width=2,
            )

            # Label
            if label:
                try:
                    font = ImageFont.truetype("arial.ttf", 14)
                except (IOError, OSError):
                    font = ImageFont.load_default()
                draw.text(
                    (px + size + 4, py - 8),
                    label,
                    fill=(255, 255, 255),
                    font=font,
                )

            # Save back to bytes
            output = BytesIO()
            img.save(output, format="PNG")
            return output.getvalue()

        except ImportError:
            logger.warning("Pillow not installed - saving radar image without marker overlay")
            return image_bytes
        except Exception as e:
            logger.warning(f"Failed to overlay marker: {e}")
            return image_bytes

    @property
    def capture_interval(self) -> int:
        return self._capture_interval

    @capture_interval.setter
    def capture_interval(self, seconds: int):
        self._capture_interval = max(30, seconds)


# Singleton
_service: Optional[RadarService] = None


def get_radar_service() -> RadarService:
    """Get the singleton Radar service instance."""
    global _service
    if _service is None:
        from ..config import get_settings
        settings = get_settings()
        _service = RadarService(settings.data_dir)
    return _service
