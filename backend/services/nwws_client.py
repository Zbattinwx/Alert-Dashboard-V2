"""
NWWS-OI (Weather Wire) XMPP client for Alert Dashboard V2.

This module provides a client for connecting to the NWS Weather Wire Service
via XMPP protocol for real-time alert streaming.

NWWS-OI provides the fastest alert delivery - faster than the NWS API.
"""

import asyncio
import logging
from html import unescape
from datetime import datetime, timezone
from typing import Callable, Optional, Any
from dataclasses import dataclass, field

import slixmpp

from ..config import get_settings
from ..parsers import AlertParser
from ..models.alert import Alert

logger = logging.getLogger(__name__)


@dataclass
class NWWSConfig:
    """Configuration for NWWS-OI connection."""
    username: str
    password: str
    server: str = "nwws-oi.weather.gov"
    resource: str = "nwws"
    room: str = "nwws@conference.nwws-oi.weather.gov"
    nickname: str = "AlertDashboard"
    reconnect_delay: float = 5.0
    max_reconnect_delay: float = 300.0


class NWWSClient(slixmpp.ClientXMPP):
    """
    XMPP client for NWWS-OI Weather Wire.

    Connects to the NWS Weather Wire Service and receives real-time
    weather alerts via XMPP multi-user chat (MUC).

    Based on the working implementation from Alert Dashboard V1.
    """

    def __init__(
        self,
        config: NWWSConfig,
        on_alert: Optional[Callable[[str], None]] = None,
        on_connected: Optional[Callable[[], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize the NWWS client.

        Args:
            config: NWWS connection configuration
            on_alert: Callback for received alerts (raw text)
            on_connected: Callback when connected
            on_disconnected: Callback when disconnected
        """
        jid = f"{config.username}@{config.server}"
        # Use PLAIN SASL mechanism as required by NWWS-OI
        super().__init__(jid, config.password, sasl_mech='PLAIN')

        self.config = config
        self._on_alert = on_alert
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._connected = False
        self._should_reconnect = True
        self._reconnect_task: Optional[asyncio.Task] = None

        # Register plugins before connecting
        self.register_plugin('xep_0045')  # Multi-User Chat

        # Register event handlers
        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("groupchat_message", self._on_muc_message)
        self.add_event_handler("disconnected", self._on_disconnected_event)

    async def _on_session_start(self, event):
        """Handle successful session start."""
        # Cancel any pending reconnection task
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            logger.info("Reconnection successful. Reconnect task cancelled.")
        self._reconnect_task = None

        logger.info("NWWS session started. Joining alert channel...")

        try:
            await self.get_roster()
            self.send_presence()

            # Join the NWWS MUC room
            await self.plugin['xep_0045'].join_muc(
                self.config.room,
                self.config.nickname
            )

            self._connected = True
            logger.info(f"Joined NWWS room: {self.config.room}")

            if self._on_connected:
                self._on_connected()

        except Exception as e:
            logger.error(f"Error during session setup: {e}")

    def _on_muc_message(self, msg):
        """
        Handle incoming MUC messages (alerts).

        NWWS-OI sends alert text in a custom XML namespace {nwws-oi}x,
        not in the standard message body.
        """
        # Ignore messages from ourselves
        if msg['type'] != 'groupchat' or msg['mucnick'] == self.config.nickname:
            return

        # Extract alert text from the NWWS-OI custom namespace
        # This is the key difference from standard XMPP - NWWS uses a custom element
        alert_body = msg.xml.find('{nwws-oi}x')

        if alert_body is not None and alert_body.text:
            # Decode HTML entities in the alert text
            raw_text = unescape(alert_body.text).strip()

            if raw_text:
                logger.debug(f"Received NWWS message ({len(raw_text)} chars)")

                # Call the alert callback
                if self._on_alert:
                    try:
                        self._on_alert(raw_text)
                    except Exception as e:
                        logger.error(f"Error in alert callback: {e}")
        elif msg['body']:
            # Fallback: log if we only got a summary line
            logger.debug(f"Received summary line (no full text): {msg['body'][:100]}")

    def _on_disconnected_event(self, event):
        """Handle disconnection and schedule reconnection."""
        logger.warning("Disconnected from NWWS-OI server.")
        self._connected = False

        if self._on_disconnected:
            self._on_disconnected()

        # Start reconnection task if not already running
        if self._should_reconnect:
            if self._reconnect_task is None or self._reconnect_task.done():
                logger.info("Scheduling persistent reconnection task.")
                self._reconnect_task = asyncio.create_task(self._attempt_reconnect())
            else:
                logger.info("Reconnection task is already active.")

    async def _attempt_reconnect(self):
        """Continuously attempt to reconnect with exponential backoff."""
        wait_time = self.config.reconnect_delay

        while self._should_reconnect:
            try:
                logger.info(f"Attempting to reconnect in {wait_time:.0f} seconds...")
                await asyncio.sleep(wait_time)

                # Attempt to reconnect
                self.connect()

                # If connect() succeeds, session_start will fire and cancel this task
                # Wait a bit to see if connection succeeds
                await asyncio.sleep(10)

                if self._connected:
                    break

            except asyncio.CancelledError:
                logger.info("Reconnect task cancelled (connection succeeded)")
                break
            except Exception as e:
                logger.error(f"Reconnect attempt failed: {e}")

            # Exponential backoff, capped at max delay
            wait_time = min(wait_time * 2, self.config.max_reconnect_delay)

    def start(self):
        """Start the NWWS client (non-blocking)."""
        logger.info(f"Connecting to NWWS: {self.config.server}")
        self._should_reconnect = True

        try:
            # connect() is non-blocking - it starts the connection in the background
            self.connect()
        except Exception as e:
            logger.error(f"Failed to connect to NWWS: {e}")
            raise

    def stop(self):
        """Stop the NWWS client."""
        logger.info("Stopping NWWS client")
        self._should_reconnect = False

        # Cancel reconnection task
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        try:
            # Leave the MUC room
            if self._connected:
                self.plugin['xep_0045'].leave_muc(
                    self.config.room,
                    self.config.nickname
                )

            # Send unavailable presence and disconnect
            self.send_presence(ptype='unavailable')
            self.disconnect()
        except Exception as e:
            logger.warning(f"Error during NWWS disconnect: {e}")

    @property
    def is_connected(self) -> bool:
        """Check if connected to NWWS."""
        return self._connected


class NWWSAlertHandler:
    """
    High-level handler for NWWS alerts.

    Wraps the NWWS client and provides parsed Alert objects
    to registered callbacks.
    """

    def __init__(self):
        """Initialize the alert handler."""
        self._client: Optional[NWWSClient] = None
        self._alert_callbacks: list[Callable[[Alert], None]] = []
        self._raw_callbacks: list[Callable[[str], None]] = []
        self._connected = False

    def add_alert_callback(self, callback: Callable[[Alert], None]):
        """Register a callback for parsed alerts."""
        self._alert_callbacks.append(callback)

    def add_raw_callback(self, callback: Callable[[str], None]):
        """Register a callback for raw alert text."""
        self._raw_callbacks.append(callback)

    def _on_raw_alert(self, raw_text: str):
        """Handle incoming raw alert from NWWS."""
        # Call raw callbacks
        for callback in self._raw_callbacks:
            try:
                callback(raw_text)
            except Exception as e:
                logger.error(f"Error in raw callback: {e}")

        # Parse the alert
        try:
            alert = AlertParser.parse_text_alert(raw_text, source="nwws")
            if alert:
                logger.info(f"Parsed NWWS alert: {alert.product_id} ({alert.event_name})")

                # Call alert callbacks
                for callback in self._alert_callbacks:
                    try:
                        callback(alert)
                    except Exception as e:
                        logger.error(f"Error in alert callback: {e}")
            else:
                logger.debug("NWWS message did not parse to valid alert")
        except Exception as e:
            logger.error(f"Error parsing NWWS alert: {e}")

    def _on_connected(self):
        """Handle connection established."""
        self._connected = True
        logger.info("NWWS handler: Connected")

    def _on_disconnected(self):
        """Handle disconnection."""
        self._connected = False
        logger.warning("NWWS handler: Disconnected")

    async def start(self):
        """Start the NWWS handler."""
        settings = get_settings()

        if not settings.nwws_username or not settings.nwws_password:
            logger.warning("NWWS credentials not configured, skipping NWWS connection")
            return

        config = NWWSConfig(
            username=settings.nwws_username,
            password=settings.nwws_password,
            server=settings.nwws_server,
            resource=settings.nwws_resource,
        )

        self._client = NWWSClient(
            config=config,
            on_alert=self._on_raw_alert,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

        # start() is non-blocking
        self._client.start()

    async def stop(self):
        """Stop the NWWS handler."""
        if self._client:
            self._client.stop()
            self._client = None

    @property
    def is_connected(self) -> bool:
        """Check if connected to NWWS."""
        return self._connected and self._client is not None and self._client.is_connected


# Singleton instance
_handler: Optional[NWWSAlertHandler] = None


def get_nwws_handler() -> NWWSAlertHandler:
    """Get the singleton NWWS handler instance."""
    global _handler
    if _handler is None:
        _handler = NWWSAlertHandler()
    return _handler


async def start_nwws_handler():
    """Start the singleton NWWS handler."""
    handler = get_nwws_handler()
    await handler.start()


async def stop_nwws_handler():
    """Stop the singleton NWWS handler."""
    global _handler
    if _handler:
        await _handler.stop()
        _handler = None
