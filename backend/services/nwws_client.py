"""
NWWS-OI (Weather Wire) XMPP client for Alert Dashboard V2.

This module provides a client for connecting to the NWS Weather Wire Service
via XMPP protocol for real-time alert streaming.

NWWS-OI provides the fastest alert delivery - faster than the NWS API.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Any
from dataclasses import dataclass, field

import slixmpp
from slixmpp.xmlstream import ET

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
        jid = f"{config.username}@{config.server}/{config.resource}"
        super().__init__(jid, config.password)

        self.config = config
        self._on_alert = on_alert
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._connected = False
        self._reconnect_delay = config.reconnect_delay
        self._should_reconnect = True

        # Register plugins
        self.register_plugin('xep_0030')  # Service Discovery
        self.register_plugin('xep_0045')  # Multi-User Chat
        self.register_plugin('xep_0199')  # XMPP Ping

        # Register event handlers
        self.add_event_handler("session_start", self._handle_session_start)
        self.add_event_handler("disconnected", self._handle_disconnected)
        self.add_event_handler("connection_failed", self._handle_connection_failed)
        self.add_event_handler("groupchat_message", self._handle_groupchat_message)

    async def _handle_session_start(self, event):
        """Handle successful session start."""
        logger.info("NWWS session started")
        self._connected = True
        self._reconnect_delay = self.config.reconnect_delay  # Reset delay

        try:
            # Send presence
            self.send_presence()

            # Get roster (required by some servers)
            await self.get_roster()

            # Join the NWWS room
            muc = self.plugin['xep_0045']
            await muc.join_muc(
                self.config.room,
                self.config.nickname,
                wait=True,
            )
            logger.info(f"Joined NWWS room: {self.config.room}")

            if self._on_connected:
                self._on_connected()

        except Exception as e:
            logger.error(f"Error during session setup: {e}")

    async def _handle_disconnected(self, event):
        """Handle disconnection."""
        logger.warning("NWWS disconnected")
        self._connected = False

        if self._on_disconnected:
            self._on_disconnected()

        # Attempt reconnection
        if self._should_reconnect:
            await self._reconnect()

    async def _handle_connection_failed(self, event):
        """Handle connection failure."""
        logger.error("NWWS connection failed")
        self._connected = False

        if self._should_reconnect:
            await self._reconnect()

    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        logger.info(f"Attempting reconnect in {self._reconnect_delay:.1f} seconds")
        await asyncio.sleep(self._reconnect_delay)

        # Increase delay for next attempt (exponential backoff)
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self.config.max_reconnect_delay
        )

        try:
            self.connect()
        except Exception as e:
            logger.error(f"Reconnection attempt failed: {e}")

    def _handle_groupchat_message(self, msg):
        """Handle incoming groupchat messages (alerts)."""
        # Ignore messages from ourselves or system messages
        if msg['mucnick'] == self.config.nickname:
            return

        # Get the message body
        body = msg['body']
        if not body:
            return

        # Log receipt
        logger.debug(f"Received NWWS message ({len(body)} chars)")

        # Call the alert callback
        if self._on_alert:
            try:
                self._on_alert(body)
            except Exception as e:
                logger.error(f"Error in alert callback: {e}")

    async def start(self):
        """Start the NWWS client."""
        logger.info(f"Connecting to NWWS: {self.config.server}")
        self._should_reconnect = True

        try:
            self.connect()
            # The connection runs in the background via slixmpp's event loop
        except Exception as e:
            logger.error(f"Failed to connect to NWWS: {e}")
            raise

    async def stop(self):
        """Stop the NWWS client."""
        logger.info("Stopping NWWS client")
        self._should_reconnect = False

        try:
            # Leave the MUC room
            if self._connected:
                muc = self.plugin['xep_0045']
                muc.leave_muc(self.config.room, self.config.nickname)

            # Disconnect
            self.disconnect(wait=True)
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

        await self._client.start()

    async def stop(self):
        """Stop the NWWS handler."""
        if self._client:
            await self._client.stop()
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
