"""
Alert Manager service for Alert Dashboard V2.

This module manages the state of active alerts, including:
- Adding/updating/removing alerts
- Automatic expiration cleanup
- Deduplication between NWWS and API sources
- Persistence to disk
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import get_settings
from ..models.alert import Alert, AlertStatus

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Manages active weather alerts.

    Features:
    - Thread-safe alert storage
    - Automatic expiration cleanup
    - Deduplication by product_id
    - Recent products tracking
    - Persistence to JSON file
    """

    def __init__(
        self,
        cleanup_interval: int = 60,
        max_recent_products: int = 50,
        persistence_path: Optional[Path] = None,
    ):
        """
        Initialize the Alert Manager.

        Args:
            cleanup_interval: Seconds between expiration cleanup runs
            max_recent_products: Maximum recent products to track
            persistence_path: Path to save/load alerts (optional)
        """
        self._alerts: dict[str, Alert] = {}
        self._recent_products: deque[dict] = deque(maxlen=max_recent_products)
        self._cleanup_interval = cleanup_interval
        self._persistence_path = persistence_path
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_alert_added: list[Callable[[Alert], None]] = []
        self._on_alert_updated: list[Callable[[Alert], None]] = []
        self._on_alert_removed: list[Callable[[Alert], None]] = []
        self._on_alerts_changed: list[Callable[[], None]] = []

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def on_alert_added(self, callback: Callable[[Alert], None]):
        """Register callback for when an alert is added."""
        self._on_alert_added.append(callback)

    def on_alert_updated(self, callback: Callable[[Alert], None]):
        """Register callback for when an alert is updated."""
        self._on_alert_updated.append(callback)

    def on_alert_removed(self, callback: Callable[[Alert], None]):
        """Register callback for when an alert is removed."""
        self._on_alert_removed.append(callback)

    def on_alerts_changed(self, callback: Callable[[], None]):
        """Register callback for any change to alerts."""
        self._on_alerts_changed.append(callback)

    def _notify_added(self, alert: Alert):
        """Notify callbacks of added alert."""
        for cb in self._on_alert_added:
            try:
                cb(alert)
            except Exception as e:
                logger.error(f"Error in alert_added callback: {e}")
        self._notify_changed()

    def _notify_updated(self, alert: Alert):
        """Notify callbacks of updated alert."""
        for cb in self._on_alert_updated:
            try:
                cb(alert)
            except Exception as e:
                logger.error(f"Error in alert_updated callback: {e}")
        self._notify_changed()

    def _notify_removed(self, alert: Alert):
        """Notify callbacks of removed alert."""
        for cb in self._on_alert_removed:
            try:
                cb(alert)
            except Exception as e:
                logger.error(f"Error in alert_removed callback: {e}")
        self._notify_changed()

    def _notify_changed(self):
        """Notify callbacks of any change."""
        for cb in self._on_alerts_changed:
            try:
                cb()
            except Exception as e:
                logger.error(f"Error in alerts_changed callback: {e}")

    # =========================================================================
    # Alert Management
    # =========================================================================

    def add_alert(self, alert: Alert) -> bool:
        """
        Add or update an alert.

        Args:
            alert: Alert to add

        Returns:
            True if alert was added/updated, False if ignored
        """
        if not alert.product_id:
            logger.warning("Attempted to add alert without product_id")
            return False

        existing = self._alerts.get(alert.product_id)

        if existing:
            # Check if this is a cancellation
            if alert.status == AlertStatus.CANCELLED:
                self.remove_alert(alert.product_id)
                return True

            # Update existing alert
            existing.headline = alert.headline or existing.headline
            existing.description = alert.description or existing.description
            existing.instruction = alert.instruction or existing.instruction
            existing.expiration_time = alert.expiration_time or existing.expiration_time
            existing.threat = alert.threat if alert.threat.has_tornado or alert.threat.max_wind_gust_mph else existing.threat
            existing.polygon = alert.polygon or existing.polygon
            existing.mark_updated()

            logger.info(f"Updated alert: {alert.product_id}")
            self._notify_updated(existing)
            return True

        else:
            # Add new alert
            if alert.status == AlertStatus.CANCELLED:
                # Don't add cancelled alerts that don't exist
                logger.debug(f"Ignoring cancellation for unknown alert: {alert.product_id}")
                return False

            self._alerts[alert.product_id] = alert
            logger.info(f"Added alert: {alert.product_id} ({alert.event_name})")
            self._notify_added(alert)

            # Track in recent products
            self._add_to_recent(alert)

            return True

    def remove_alert(self, product_id: str) -> bool:
        """
        Remove an alert by product_id.

        Args:
            product_id: Alert product ID

        Returns:
            True if alert was removed, False if not found
        """
        alert = self._alerts.pop(product_id, None)
        if alert:
            logger.info(f"Removed alert: {product_id}")
            self._notify_removed(alert)
            return True
        return False

    def get_alert(self, product_id: str) -> Optional[Alert]:
        """Get an alert by product_id."""
        return self._alerts.get(product_id)

    def get_all_alerts(self) -> list[Alert]:
        """Get all active alerts."""
        return list(self._alerts.values())

    def get_alerts_sorted(self, by_priority: bool = True) -> list[Alert]:
        """
        Get alerts sorted by priority and/or time.

        Args:
            by_priority: Sort by priority first (default True)

        Returns:
            Sorted list of alerts
        """
        alerts = list(self._alerts.values())

        if by_priority:
            # Sort by priority (lower = higher priority), then by issued time (newer first)
            alerts.sort(key=lambda a: (
                a.priority.value,
                -(a.issued_time.timestamp() if a.issued_time else 0)
            ))
        else:
            # Sort by issued time only (newer first)
            alerts.sort(key=lambda a: -(a.issued_time.timestamp() if a.issued_time else 0))

        return alerts

    def get_alerts_by_phenomenon(self, phenomenon: str) -> list[Alert]:
        """Get all alerts for a specific phenomenon code."""
        return [a for a in self._alerts.values() if a.phenomenon == phenomenon]

    def get_alerts_by_state(self, state: str) -> list[Alert]:
        """Get all alerts affecting a specific state."""
        state_upper = state.upper()
        return [
            a for a in self._alerts.values()
            if any(ugc.startswith(state_upper) for ugc in a.affected_areas)
        ]

    @property
    def alert_count(self) -> int:
        """Get total number of active alerts."""
        return len(self._alerts)

    def get_counts_by_type(self) -> dict[str, int]:
        """Get alert counts grouped by phenomenon."""
        counts: dict[str, int] = {}
        for alert in self._alerts.values():
            key = alert.phenomenon or "UNKNOWN"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _add_to_recent(self, alert: Alert):
        """Add alert to recent products list."""
        self._recent_products.appendleft({
            "product_id": alert.product_id,
            "event_name": alert.event_name,
            "headline": alert.headline,
            "issued_time": alert.issued_time.isoformat() if alert.issued_time else None,
            "source": alert.source,
        })

    def get_recent_products(self, limit: int = 20) -> list[dict]:
        """Get recent products list."""
        return list(self._recent_products)[:limit]

    # =========================================================================
    # Expiration Cleanup
    # =========================================================================

    async def start_cleanup_task(self):
        """Start the automatic cleanup task."""
        if self._running:
            return

        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Started alert cleanup task")

    async def stop_cleanup_task(self):
        """Stop the automatic cleanup task."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        logger.info("Stopped alert cleanup task")

    async def _cleanup_loop(self):
        """Background task to clean up expired alerts."""
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                self.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")

    def cleanup_expired(self) -> int:
        """
        Remove expired alerts.

        Returns:
            Number of alerts removed
        """
        now = datetime.now(timezone.utc)
        expired_ids = []

        for product_id, alert in self._alerts.items():
            if alert.expiration_time and alert.expiration_time <= now:
                expired_ids.append(product_id)

        for product_id in expired_ids:
            alert = self._alerts.pop(product_id, None)
            if alert:
                alert.mark_expired()
                logger.info(f"Expired alert: {product_id}")
                self._notify_removed(alert)

        if expired_ids:
            logger.info(f"Cleaned up {len(expired_ids)} expired alerts")

        return len(expired_ids)

    # =========================================================================
    # Persistence
    # =========================================================================

    def save_to_file(self, path: Optional[Path] = None):
        """
        Save alerts to JSON file.

        Args:
            path: File path (default from constructor)
        """
        path = path or self._persistence_path
        if not path:
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "alert_count": len(self._alerts),
                "alerts": [alert.to_dict() for alert in self._alerts.values()],
            }

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            logger.info(f"Saved {len(self._alerts)} alerts to {path}")

        except Exception as e:
            logger.error(f"Failed to save alerts to {path}: {e}")

    def load_from_file(self, path: Optional[Path] = None) -> int:
        """
        Load alerts from JSON file.

        Args:
            path: File path (default from constructor)

        Returns:
            Number of alerts loaded
        """
        path = path or self._persistence_path
        if not path or not path.exists():
            return 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            alerts_data = data.get("alerts", [])
            loaded = 0

            for alert_dict in alerts_data:
                try:
                    alert = Alert.from_dict(alert_dict)
                    # Only load if not expired
                    if not alert.is_expired:
                        self._alerts[alert.product_id] = alert
                        loaded += 1
                except Exception as e:
                    logger.warning(f"Failed to load alert: {e}")

            logger.info(f"Loaded {loaded} alerts from {path}")
            return loaded

        except Exception as e:
            logger.error(f"Failed to load alerts from {path}: {e}")
            return 0

    def clear_all(self):
        """Remove all alerts."""
        count = len(self._alerts)
        self._alerts.clear()
        self._recent_products.clear()
        logger.info(f"Cleared {count} alerts")
        self._notify_changed()

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> dict[str, Any]:
        """Get alert statistics."""
        alerts = list(self._alerts.values())

        warnings = [a for a in alerts if a.is_warning]
        watches = [a for a in alerts if a.is_watch]
        high_priority = [a for a in alerts if a.is_high_priority]

        return {
            "total_alerts": len(alerts),
            "warnings": len(warnings),
            "watches": len(watches),
            "high_priority": len(high_priority),
            "by_phenomenon": self.get_counts_by_type(),
            "by_source": {
                "nwws": len([a for a in alerts if a.source == "nwws"]),
                "api": len([a for a in alerts if a.source == "api"]),
            },
        }


# Singleton instance
_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """Get the singleton Alert Manager instance."""
    global _manager
    if _manager is None:
        settings = get_settings()
        persistence_path = settings.data_dir / "active_alerts.json" if settings.persist_alerts else None
        _manager = AlertManager(
            cleanup_interval=settings.alert_cleanup_interval_seconds,
            persistence_path=persistence_path,
        )
    return _manager


async def start_alert_manager():
    """Start the alert manager background tasks."""
    manager = get_alert_manager()
    manager.load_from_file()
    await manager.start_cleanup_task()


async def stop_alert_manager():
    """Stop the alert manager and save state."""
    global _manager
    if _manager:
        await _manager.stop_cleanup_task()
        _manager.save_to_file()
        _manager = None
