"""
WebSocket Message Broker for Alert Dashboard V2.

This module handles broadcasting messages to connected WebSocket clients,
including:
- Alert updates (new, updated, removed)
- System status messages
- Command handling from clients
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from ..models.alert import Alert

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    """Types of WebSocket messages."""
    # Server -> Client
    ALERT_NEW = "alert_new"
    ALERT_UPDATE = "alert_update"
    ALERT_REMOVE = "alert_remove"
    ALERT_BULK = "alert_bulk"
    SYSTEM_STATUS = "system_status"
    CONNECTION_ACK = "connection_ack"
    ERROR = "error"
    PONG = "pong"

    # Client -> Server
    PING = "ping"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    GET_ALERTS = "get_alerts"
    GET_STATUS = "get_status"
    CHASER_POSITION_UPDATE = "chaser_position_update"

    # Chaser tracking (Server -> Client)
    CHASER_POSITION = "chaser_position"
    CHASER_DISCONNECT = "chaser_disconnect"


@dataclass
class ClientConnection:
    """Represents a connected WebSocket client."""
    websocket: WebSocket
    client_id: str
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    subscriptions: Set[str] = field(default_factory=set)
    last_ping: Optional[datetime] = None

    def __hash__(self):
        return hash(self.client_id)

    def __eq__(self, other):
        if isinstance(other, ClientConnection):
            return self.client_id == other.client_id
        return False


class MessageBroker:
    """
    WebSocket message broker for broadcasting alerts and messages.

    Features:
    - Connection management
    - Message broadcasting
    - Client subscriptions (filter by state, alert type)
    - Ping/pong for connection health
    """

    def __init__(self):
        """Initialize the message broker."""
        self._connections: dict[str, ClientConnection] = {}
        self._connection_counter = 0
        self._message_handlers: dict[str, Callable] = {}

        # Register default message handlers
        self._register_default_handlers()

    def _register_default_handlers(self):
        """Register default handlers for client messages."""
        self._message_handlers[MessageType.PING] = self._handle_ping
        self._message_handlers[MessageType.SUBSCRIBE] = self._handle_subscribe
        self._message_handlers[MessageType.UNSUBSCRIBE] = self._handle_unsubscribe
        self._message_handlers[MessageType.GET_ALERTS] = self._handle_get_alerts
        self._message_handlers[MessageType.GET_STATUS] = self._handle_get_status

    def register_handler(self, message_type: str, handler: Callable):
        """Register a custom message handler."""
        self._message_handlers[message_type] = handler

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def connect(self, websocket: WebSocket) -> str:
        """
        Accept a new WebSocket connection.

        Args:
            websocket: FastAPI WebSocket instance

        Returns:
            Client ID for the connection
        """
        await websocket.accept()

        self._connection_counter += 1
        client_id = f"client_{self._connection_counter}_{datetime.now(timezone.utc).strftime('%H%M%S')}"

        connection = ClientConnection(
            websocket=websocket,
            client_id=client_id,
        )
        self._connections[client_id] = connection

        logger.info(f"Client connected: {client_id} (total: {len(self._connections)})")

        # Send connection acknowledgment
        await self._send_to_client(connection, MessageType.CONNECTION_ACK, {
            "client_id": client_id,
            "server_time": datetime.now(timezone.utc).isoformat(),
        })

        return client_id

    async def disconnect(self, client_id: str):
        """
        Handle client disconnection.

        Args:
            client_id: Client ID to disconnect
        """
        connection = self._connections.pop(client_id, None)
        if connection:
            logger.info(f"Client disconnected: {client_id} (total: {len(self._connections)})")
            try:
                await connection.websocket.close()
            except Exception:
                pass

    def get_connection(self, client_id: str) -> Optional[ClientConnection]:
        """Get a connection by client ID."""
        return self._connections.get(client_id)

    @property
    def connection_count(self) -> int:
        """Get number of connected clients."""
        return len(self._connections)

    def get_all_client_ids(self) -> list[str]:
        """Get all connected client IDs."""
        return list(self._connections.keys())

    # =========================================================================
    # Message Handling
    # =========================================================================

    async def handle_message(self, client_id: str, raw_message: str):
        """
        Handle an incoming message from a client.

        Args:
            client_id: Client ID that sent the message
            raw_message: Raw JSON message string
        """
        connection = self._connections.get(client_id)
        if not connection:
            logger.warning(f"Message from unknown client: {client_id}")
            return

        try:
            message = json.loads(raw_message)
            msg_type = message.get("type")
            data = message.get("data", {})

            handler = self._message_handlers.get(msg_type)
            if handler:
                await handler(connection, data)
            else:
                logger.debug(f"Unhandled message type: {msg_type}")
                await self._send_to_client(connection, MessageType.ERROR, {
                    "error": f"Unknown message type: {msg_type}"
                })

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON from {client_id}: {e}")
            await self._send_to_client(connection, MessageType.ERROR, {
                "error": "Invalid JSON format"
            })
        except Exception as e:
            logger.error(f"Error handling message from {client_id}: {e}")

    async def _handle_ping(self, connection: ClientConnection, data: dict):
        """Handle ping message."""
        connection.last_ping = datetime.now(timezone.utc)
        await self._send_to_client(connection, MessageType.PONG, {
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def _handle_subscribe(self, connection: ClientConnection, data: dict):
        """Handle subscription request."""
        topics = data.get("topics", [])
        for topic in topics:
            connection.subscriptions.add(topic)
        logger.debug(f"Client {connection.client_id} subscribed to: {topics}")

    async def _handle_unsubscribe(self, connection: ClientConnection, data: dict):
        """Handle unsubscription request."""
        topics = data.get("topics", [])
        for topic in topics:
            connection.subscriptions.discard(topic)
        logger.debug(f"Client {connection.client_id} unsubscribed from: {topics}")

    async def _handle_get_alerts(self, connection: ClientConnection, data: dict):
        """Handle request for current alerts."""
        # This will be implemented to use AlertManager
        # For now, just acknowledge the request
        logger.debug(f"Client {connection.client_id} requested alerts")

    async def _handle_get_status(self, connection: ClientConnection, data: dict):
        """Handle request for system status."""
        await self._send_to_client(connection, MessageType.SYSTEM_STATUS, {
            "connected_clients": len(self._connections),
            "server_time": datetime.now(timezone.utc).isoformat(),
        })

    # =========================================================================
    # Broadcasting
    # =========================================================================

    async def broadcast_alert_new(self, alert: Alert):
        """
        Broadcast a new alert to all clients.

        Args:
            alert: New alert to broadcast
        """
        await self._broadcast(MessageType.ALERT_NEW, alert.to_dict())

    async def broadcast_alert_update(self, alert: Alert):
        """
        Broadcast an alert update to all clients.

        Args:
            alert: Updated alert to broadcast
        """
        await self._broadcast(MessageType.ALERT_UPDATE, alert.to_dict())

    async def broadcast_alert_remove(self, alert: Alert):
        """
        Broadcast an alert removal to all clients.

        Args:
            alert: Alert that was removed
        """
        await self._broadcast(MessageType.ALERT_REMOVE, {
            "product_id": alert.product_id,
            "event_name": alert.event_name,
            "reason": "expired" if alert.is_expired else "cancelled",
        })

    async def broadcast_alerts_bulk(self, alerts: list[Alert]):
        """
        Broadcast multiple alerts at once (e.g., initial sync).

        Args:
            alerts: List of alerts to broadcast
        """
        await self._broadcast(MessageType.ALERT_BULK, {
            "count": len(alerts),
            "alerts": [alert.to_dict() for alert in alerts],
        })

    async def broadcast_system_status(self, status: dict):
        """
        Broadcast system status to all clients.

        Args:
            status: Status data to broadcast
        """
        await self._broadcast(MessageType.SYSTEM_STATUS, status)

    async def _broadcast(self, msg_type: MessageType, data: Any):
        """
        Broadcast a message to all connected clients.

        Args:
            msg_type: Message type
            data: Message data
        """
        if not self._connections:
            return

        message = self._format_message(msg_type, data)
        disconnected = []

        for client_id, connection in self._connections.items():
            try:
                await connection.websocket.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send to {client_id}: {e}")
                disconnected.append(client_id)

        # Clean up disconnected clients
        for client_id in disconnected:
            await self.disconnect(client_id)

    async def _send_to_client(
        self,
        connection: ClientConnection,
        msg_type: MessageType,
        data: Any
    ):
        """
        Send a message to a specific client.

        Args:
            connection: Client connection
            msg_type: Message type
            data: Message data
        """
        try:
            message = self._format_message(msg_type, data)
            await connection.websocket.send_text(message)
        except Exception as e:
            logger.warning(f"Failed to send to {connection.client_id}: {e}")
            await self.disconnect(connection.client_id)

    def _format_message(self, msg_type: MessageType, data: Any) -> str:
        """Format a message for sending."""
        return json.dumps({
            "type": msg_type.value,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # =========================================================================
    # Filtered Broadcasting
    # =========================================================================

    async def broadcast_to_subscribed(
        self,
        topic: str,
        msg_type: MessageType,
        data: Any
    ):
        """
        Broadcast to clients subscribed to a specific topic.

        Args:
            topic: Subscription topic (e.g., "state:OH", "type:tornado")
            msg_type: Message type
            data: Message data
        """
        message = self._format_message(msg_type, data)
        disconnected = []

        for client_id, connection in self._connections.items():
            if topic in connection.subscriptions or not connection.subscriptions:
                try:
                    await connection.websocket.send_text(message)
                except Exception as e:
                    logger.warning(f"Failed to send to {client_id}: {e}")
                    disconnected.append(client_id)

        for client_id in disconnected:
            await self.disconnect(client_id)

    async def send_to_client_by_id(
        self,
        client_id: str,
        msg_type: MessageType,
        data: Any
    ):
        """
        Send a message to a specific client by ID.

        Args:
            client_id: Target client ID
            msg_type: Message type
            data: Message data
        """
        connection = self._connections.get(client_id)
        if connection:
            await self._send_to_client(connection, msg_type, data)
        else:
            logger.warning(f"Client not found: {client_id}")


# Singleton instance
_broker: Optional[MessageBroker] = None


def get_message_broker() -> MessageBroker:
    """Get the singleton Message Broker instance."""
    global _broker
    if _broker is None:
        _broker = MessageBroker()
    return _broker
