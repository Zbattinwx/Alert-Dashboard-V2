"""
Spotter Network Integration Service for Alert Dashboard V2.

Polls the Spotter Network API for chaser GPS position and broadcasts
it to all connected dashboard clients via the message broker.
This allows the dashboard to track the chaser even when they don't
have the /chase page open on their phone.

API endpoints (from Spotter Network):
  POST /login        - Authenticate, returns application ID
  POST /positions    - Fetch positions for specific marker IDs
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)


class SpotterNetworkService:
    """Polls Spotter Network API for chaser position."""

    LOGIN_URL = "https://www.spotternetwork.org/login"
    POSITIONS_URL = "https://www.spotternetwork.org/positions"

    def __init__(
        self,
        username: str,
        password: str,
        marker_id: int,
        poll_interval: int = 30,
    ):
        self._username = username
        self._password = password
        self._marker_id = marker_id
        self._poll_interval = poll_interval
        self._application_id: Optional[str] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_position: Optional[dict] = None
        self._consecutive_errors = 0

    async def start(self):
        """Login and start the background polling loop."""
        self._session = aiohttp.ClientSession()
        logged_in = await self._login()
        if not logged_in:
            logger.error("Spotter Network login failed - service will not start")
            await self._session.close()
            return False

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"Spotter Network service started (marker {self._marker_id}, "
            f"polling every {self._poll_interval}s)"
        )
        return True

    async def stop(self):
        """Stop polling and clean up."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Spotter Network service stopped")

    async def _login(self) -> bool:
        """Authenticate with Spotter Network API."""
        try:
            async with self._session.post(
                self.LOGIN_URL,
                json={"username": self._username, "password": self._password},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Spotter Network login HTTP {resp.status}: {text}")
                    return False

                data = await resp.json()
                if not data.get("success"):
                    errors = data.get("errors", ["Unknown error"])
                    logger.error(f"Spotter Network login failed: {errors}")
                    return False

                self._application_id = data["id"]
                logger.info(f"Spotter Network login successful (app ID: {self._application_id})")
                return True

        except Exception as e:
            logger.error(f"Spotter Network login error: {e}")
            return False

    async def _poll_loop(self):
        """Background loop that polls for position updates."""
        while self._running:
            try:
                position = await self._fetch_position()
                if position:
                    self._last_position = position
                    self._consecutive_errors = 0
                    await self._broadcast_position(position)
                else:
                    self._consecutive_errors += 1
                    # Re-login after 5 consecutive failures
                    if self._consecutive_errors >= 5:
                        logger.warning("Too many failures, re-authenticating...")
                        await self._login()
                        self._consecutive_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Spotter Network poll error: {e}")
                self._consecutive_errors += 1

            await asyncio.sleep(self._poll_interval)

    async def _fetch_position(self) -> Optional[dict]:
        """Fetch position for configured marker from SN API."""
        if not self._application_id:
            return None

        try:
            async with self._session.post(
                self.POSITIONS_URL,
                json={"id": self._application_id, "markers": [self._marker_id]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Spotter Network positions HTTP {resp.status}")
                    return None

                data = await resp.json()
                positions = data.get("positions", [])
                if not positions:
                    return None

                spotter = positions[0]
                return {
                    "lat": float(spotter.get("lat", 0)),
                    "lon": float(spotter.get("lon", 0)),
                    "heading": float(spotter.get("dir", 0)) if spotter.get("dir") is not None else None,
                    "speed": float(spotter.get("spd", 0)) if spotter.get("spd") is not None else None,
                }

        except asyncio.TimeoutError:
            logger.warning("Spotter Network position fetch timed out")
            return None
        except Exception as e:
            logger.error(f"Spotter Network position fetch error: {e}")
            return None

    async def _broadcast_position(self, position: dict):
        """Broadcast position to all dashboard clients via message broker."""
        # Import here to avoid circular imports
        from .message_broker import get_message_broker, MessageType

        broker = get_message_broker()
        settings = get_settings()

        # Build the chaser position payload (same format as WebSocket chase mode)
        chaser_id = f"spotter_network_{self._marker_id}"
        chaser_data = {
            "client_id": chaser_id,
            "name": self._username,
            "lat": position["lat"],
            "lon": position["lon"],
            "heading": position["heading"],
            "speed": position["speed"],
            "last_update": datetime.now(timezone.utc).isoformat(),
            "source": "spotter_network",
        }

        # Store in the global chaser positions dict (shared with WebSocket handler)
        from ..main import _chaser_positions
        _chaser_positions[chaser_id] = chaser_data

        # Broadcast to all connected clients
        await broker._broadcast(MessageType.CHASER_POSITION, chaser_data)

    @property
    def last_position(self) -> Optional[dict]:
        """Get the last known position."""
        return self._last_position

    @property
    def is_running(self) -> bool:
        return self._running


# Singleton
_service: Optional[SpotterNetworkService] = None


def get_spotter_network_service() -> Optional[SpotterNetworkService]:
    """Get the singleton Spotter Network service instance."""
    return _service


async def start_spotter_network_service() -> bool:
    """Initialize and start the Spotter Network service."""
    global _service
    settings = get_settings()

    if not settings.spotter_network_enabled:
        return False

    if not settings.spotter_network_username or not settings.spotter_network_password:
        logger.warning("Spotter Network enabled but credentials not configured")
        return False

    if not settings.spotter_network_marker_id:
        logger.warning("Spotter Network enabled but marker_id not configured")
        return False

    _service = SpotterNetworkService(
        username=settings.spotter_network_username,
        password=settings.spotter_network_password,
        marker_id=settings.spotter_network_marker_id,
        poll_interval=settings.spotter_network_poll_interval,
    )

    return await _service.start()


async def stop_spotter_network_service():
    """Stop the Spotter Network service."""
    global _service
    if _service:
        await _service.stop()
        _service = None
