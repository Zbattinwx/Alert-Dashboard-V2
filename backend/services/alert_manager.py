"""
Alert Manager service for Alert Dashboard V2.

This module manages the state of active alerts, including:
- Adding/updating/removing alerts
- Automatic expiration cleanup
- Deduplication between NWWS and API sources
- Persistence to disk
- Audit logging for debugging merge operations
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import get_settings
from ..models.alert import Alert, AlertStatus, AlertSignificance, get_wfo_name
from ..services.ugc_service import get_display_locations

logger = logging.getLogger(__name__)

# Separate audit logger for detailed alert operations
# This captures before/after state for debugging merge issues
audit_logger = logging.getLogger("alert_audit")

# Alerts with no parsed expiration_time are purged once older than this, as a
# backstop against a missed cancellation leaving one active forever. Generous on
# purpose — longer than any real watch/warning duration.
NO_EXPIRY_MAX_AGE_HOURS = 24


def setup_audit_logger():
    """Setup the audit logger with its own rotating file handler."""
    if audit_logger.handlers:
        return  # Already configured

    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    audit_file = log_dir / "alert_audit.log"
    handler = RotatingFileHandler(
        filename=str(audit_file),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=10,  # Keep more history for audits
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False  # Don't also log to main log


def _change_signature(a: Alert) -> tuple:
    """A comparable snapshot of the fields a client renders, used to detect
    whether a merge actually changed anything. The API poll re-adds every active
    alert each interval; without this we re-broadcast and regenerate the
    broadcast graphic (which can trigger a radar download) on every identical
    re-poll. Err toward including fields — a missed field would suppress a real
    update, an extra field only costs an occasional redundant broadcast."""
    t = a.threat
    return (
        tuple(a.affected_areas or []),
        a.expiration_time.isoformat() if a.expiration_time else None,
        a.status.value if a.status else None,
        a.vtec.action.value if a.vtec and a.vtec.action else None,
        tuple(tuple(p) for p in (a.polygon or [])),
        a.headline, a.description, a.instruction, a.display_locations,
        t.tornado_detection, t.tornado_damage_threat,
        t.max_wind_gust_mph, t.max_hail_size_inches,
        t.snow_amount_min_inches, t.snow_amount_max_inches,
        t.flash_flood_detection, t.flash_flood_damage_threat,
        str(t.storm_motion),
    )


def _alert_summary(alert: Alert) -> dict:
    """Create a summary dict of an alert for audit logging."""
    return {
        "product_id": alert.product_id,
        "event_name": alert.event_name,
        "vtec_action": alert.vtec.action.value if alert.vtec else None,
        "significance": alert.significance.value if alert.significance else None,
        "sender_office": alert.sender_office,
        "affected_areas": alert.affected_areas[:5] if alert.affected_areas else [],  # First 5
        "area_count": len(alert.affected_areas) if alert.affected_areas else 0,
        "polygon_vertices": len(alert.polygon) if alert.polygon else 0,
        "issuing_offices": alert.issuing_offices,
        "expiration": alert.expiration_time.isoformat() if alert.expiration_time else None,
        "threat": {
            "tornado": alert.threat.tornado_detection,
            "wind_mph": alert.threat.max_wind_gust_mph,
            "hail_in": alert.threat.max_hail_size_inches,
        },
    }


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
        # Per-product count of consecutive API polls an alert has been absent
        # from the active feed (drives reconcile_api_alerts).
        self._api_missing_counts: dict[str, int] = {}
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
                # Protection against API/NWWS race conditions:
                # If the existing alert was recently updated from NWWS (real-time) and
                # this cancellation is from the API, skip it - NWWS is more authoritative
                now = datetime.now(timezone.utc)
                seconds_since_update = (now - existing.last_updated).total_seconds() if existing.last_updated else 999

                if (alert.source == "api" and
                    existing.source == "nwws" and
                    seconds_since_update < 60):
                    audit_logger.info(
                        f"SKIP_CANCEL | {alert.product_id} | "
                        f"Reason: NWWS updated {seconds_since_update:.1f}s ago, ignoring API cancellation | "
                        f"Incoming: {json.dumps(_alert_summary(alert))}"
                    )
                    logger.info(
                        f"Skipping API cancellation for {alert.product_id} - "
                        f"NWWS updated {seconds_since_update:.1f}s ago"
                    )
                    return False

                # A cancellation often clears only SOME areas while the event
                # continues in others under the same ETN — a WFO's WCN clearing a
                # watch's counties, or a warning follow-up (SVS) whose storm has
                # left one county while it continues for the rest. Subtract the
                # cleared areas and keep the alert alive; only fall through to full
                # removal when none remain. Warnings are polygon-based, so also
                # adopt the shrunk polygon from the follow-up if it carries one, so
                # the map matches the (now smaller) county list.
                if alert.cancelled_areas:
                    remaining = sorted(set(existing.affected_areas or []) - set(alert.cancelled_areas))
                    if remaining:
                        existing.affected_areas = remaining
                        existing.display_locations = get_display_locations(remaining)
                        if existing.vtec and alert.vtec:
                            existing.vtec.action = alert.vtec.action
                        is_zone_based = existing.significance in (
                            AlertSignificance.WATCH, AlertSignificance.ADVISORY
                        )
                        if alert.polygon and not is_zone_based:
                            existing.polygon = alert.polygon
                        existing.mark_updated()
                        audit_logger.info(
                            f"PARTIAL_CANCEL | {alert.product_id} | "
                            f"Cleared: {alert.cancelled_areas} | Remaining: {len(remaining)} | "
                            f"Incoming: {json.dumps(_alert_summary(alert))}"
                        )
                        logger.info(
                            f"Alert {alert.product_id}: cleared {len(alert.cancelled_areas)} "
                            f"areas, {len(remaining)} remain"
                        )
                        self._notify_updated(existing)
                        return True
                    # else: no areas remain → fall through to full removal

                # Audit log the cancellation (remove_alert will also log, but we want the incoming data)
                audit_logger.info(
                    f"CANCEL | {alert.product_id} | "
                    f"Incoming: {json.dumps(_alert_summary(alert))} | "
                    f"Existing: {json.dumps(_alert_summary(existing))}"
                )
                # Pop directly to avoid double audit logging
                cancelled_alert = self._alerts.pop(alert.product_id, None)
                if cancelled_alert:
                    logger.info(f"Cancelled alert: {alert.product_id}")
                    self._notify_removed(cancelled_alert)
                return True

            # Log the VTEC action being processed
            if alert.vtec:
                logger.debug(f"Processing VTEC action {alert.vtec.action.value} for {alert.product_id}")

            # Capture before state for audit + change detection
            existing_before = _alert_summary(existing)
            sig_before = _change_signature(existing)

            # Update VTEC action on the existing alert (CON, EXT, etc.)
            # This ensures the stored alert reflects the latest action
            if alert.vtec and existing.vtec:
                existing.vtec.action = alert.vtec.action

            # Update existing alert
            existing.headline = alert.headline or existing.headline
            existing.description = alert.description or existing.description
            existing.instruction = alert.instruction or existing.instruction

            # Handle expiration time - keep the LATER time unless this is an EXT action
            # Different CON products for the same alert may have different expiration times
            # for different zones, so we keep the latest to ensure full coverage
            if alert.expiration_time:
                if alert.vtec and alert.vtec.action.value == "EXT":
                    # EXT explicitly extends time - always use the new time
                    existing.expiration_time = alert.expiration_time
                elif existing.expiration_time:
                    # Keep the later expiration time
                    existing.expiration_time = max(existing.expiration_time, alert.expiration_time)
                else:
                    existing.expiration_time = alert.expiration_time

            # Improved threat merging - update if new data has ANY meaningful threat info
            new_threat = alert.threat
            if (new_threat.has_tornado or
                new_threat.max_wind_gust_mph or
                new_threat.max_hail_size_inches or
                new_threat.snow_amount_max_inches or
                new_threat.flash_flood_detection):
                # Merge threat data instead of replacing
                if new_threat.tornado_detection:
                    existing.threat.tornado_detection = new_threat.tornado_detection
                if new_threat.tornado_damage_threat:
                    existing.threat.tornado_damage_threat = new_threat.tornado_damage_threat
                if new_threat.max_wind_gust_mph and (not existing.threat.max_wind_gust_mph or
                    new_threat.max_wind_gust_mph > existing.threat.max_wind_gust_mph):
                    existing.threat.max_wind_gust_mph = new_threat.max_wind_gust_mph
                if new_threat.max_hail_size_inches and (not existing.threat.max_hail_size_inches or
                    new_threat.max_hail_size_inches > existing.threat.max_hail_size_inches):
                    existing.threat.max_hail_size_inches = new_threat.max_hail_size_inches
                if new_threat.snow_amount_max_inches:
                    existing.threat.snow_amount_min_inches = new_threat.snow_amount_min_inches
                    existing.threat.snow_amount_max_inches = new_threat.snow_amount_max_inches
                if new_threat.flash_flood_detection:
                    existing.threat.flash_flood_detection = new_threat.flash_flood_detection
                    existing.threat.flash_flood_damage_threat = new_threat.flash_flood_damage_threat
                if new_threat.storm_motion:
                    existing.threat.storm_motion = new_threat.storm_motion

            # Merge affected_areas.
            areas_before_set = set(existing.affected_areas or [])
            if alert.affected_areas:
                if alert.source == "api" and existing.source == "api":
                    # The NWS API returns the COMPLETE current area set on every
                    # poll, so replace — a shrinking watch then drops counties it
                    # no longer covers. Gated to API-managed alerts so NWWS's
                    # authoritative incremental state (which IS subset-per-product)
                    # keeps the growing union below.
                    existing.affected_areas = sorted(set(alert.affected_areas))
                else:
                    # NWWS issues multiple products per event covering different
                    # areas (e.g. western/eastern halves) → accumulate the union.
                    existing.affected_areas = sorted(areas_before_set | set(alert.affected_areas))

            # A follow-up product can continue the event for some areas while
            # clearing others in the same transmission — a WCN continuing some
            # watch counties, or a warning SVS that CANs the county the storm has
            # left while CONtinuing the rest. Subtract any cleared areas (the union
            # above only ever grows the set, so without this a county the NWS
            # dropped would stay filled — and every widget with it — until the
            # whole event expires). Applies to warnings too: the polygon is
            # replaced below, and this keeps the county list in step with it.
            if alert.cancelled_areas:
                existing.affected_areas = sorted(set(existing.affected_areas or []) - set(alert.cancelled_areas))
                if not existing.affected_areas:
                    # Every area cleared → remove the whole alert.
                    self._alerts.pop(alert.product_id, None)
                    audit_logger.info(
                        f"FULLY_CLEARED | {alert.product_id} | "
                        f"Incoming: {json.dumps(_alert_summary(alert))}"
                    )
                    logger.info(f"Alert {alert.product_id}: all areas cleared, removing")
                    self._notify_removed(existing)
                    return True

            # Merge issuing_offices for watches (watches share ETN across offices)
            if alert.significance == AlertSignificance.WATCH and alert.sender_office:
                if not existing.issuing_offices:
                    existing.issuing_offices = [existing.sender_office] if existing.sender_office else []
                if alert.sender_office not in existing.issuing_offices:
                    existing.issuing_offices.append(alert.sender_office)
                    # Update sender_name to show all offices
                    office_names = [get_wfo_name(o) for o in existing.issuing_offices]
                    existing.sender_name = " | ".join(office_names)
                    logger.info(f"Merged watch from {alert.sender_office}: now {len(existing.issuing_offices)} offices")

            # Regenerate display_locations whenever the area set actually changed
            # (grew, shrank, or was replaced).
            if set(existing.affected_areas or []) != areas_before_set:
                existing.display_locations = get_display_locations(existing.affected_areas)
                logger.debug(f"affected_areas changed: now {len(existing.affected_areas or [])} zones")

            # Update polygon if new alert has one
            # For storm-based warnings (CON products): REPLACE polygon because it represents
            # the current storm location, not a history.
            # For zone-based alerts (watches/advisories): DON'T replace, because the map
            # renders zone fills from affected_areas, not from the polygon field.
            if alert.polygon:
                is_zone_based = (
                    existing.significance == AlertSignificance.WATCH or
                    existing.significance == AlertSignificance.ADVISORY
                )
                if is_zone_based:
                    # Don't replace zone-based polygons — zone fills come from affected_areas
                    pass
                else:
                    existing.polygon = alert.polygon

            # Skip the re-broadcast + broadcast-graphic regeneration when nothing
            # a client renders actually changed. The API poll re-adds every active
            # alert every interval, so without this each warning re-broadcasts and
            # may re-trigger a NEXRAD download on a fixed cadence for no reason.
            if _change_signature(existing) == sig_before:
                audit_logger.info(f"NOCHANGE | {alert.product_id} | re-add with no field change")
                return True

            existing.mark_updated()

            # Audit log the merge with before/after state
            existing_after = _alert_summary(existing)
            vtec_action = alert.vtec.action.value if alert.vtec else "UPDATE"
            audit_logger.info(
                f"MERGE | {alert.product_id} | Action: {vtec_action} | "
                f"Incoming: {json.dumps(_alert_summary(alert))} | "
                f"Before: {json.dumps(existing_before)} | "
                f"After: {json.dumps(existing_after)}"
            )

            logger.info(f"Updated alert: {alert.product_id}")
            self._notify_updated(existing)
            return True

        else:
            # Add new alert
            if alert.status == AlertStatus.CANCELLED:
                # Don't add cancelled alerts that don't exist
                audit_logger.info(
                    f"IGNORE_CANCEL | {alert.product_id} | "
                    f"Reason: No existing alert to cancel | "
                    f"Incoming: {json.dumps(_alert_summary(alert))}"
                )
                logger.debug(f"Ignoring cancellation for unknown alert: {alert.product_id}")
                return False

            # Log if we're creating an alert from a CON/EXT/EXA/EXB (missed the original NEW)
            if alert.vtec and alert.vtec.action.value in ("CON", "EXT", "EXA", "EXB"):
                logger.info(
                    f"Creating alert from {alert.vtec.action.value} action (missed original NEW): "
                    f"{alert.product_id}"
                )

            # Initialize issuing_offices for watches
            if alert.significance == AlertSignificance.WATCH and alert.sender_office:
                if not alert.issuing_offices:
                    alert.issuing_offices = [alert.sender_office]

            self._alerts[alert.product_id] = alert
            action_str = f" [{alert.vtec.action.value}]" if alert.vtec else ""

            # Audit log the new alert
            vtec_action = alert.vtec.action.value if alert.vtec else "NEW"
            audit_logger.info(
                f"ADD | {alert.product_id} | Action: {vtec_action} | "
                f"Alert: {json.dumps(_alert_summary(alert))}"
            )

            logger.info(f"Added alert{action_str}: {alert.product_id} ({alert.event_name})")
            self._notify_added(alert)

            # Track in recent products
            self._add_to_recent(alert)

            return True

    def remove_alert(self, product_id: str, reason: str = "REMOVED") -> bool:
        """
        Remove an alert by product_id.

        Args:
            product_id: Alert product ID
            reason: Reason for removal (REMOVED, EXPIRED, CANCELLED)

        Returns:
            True if alert was removed, False if not found
        """
        alert = self._alerts.pop(product_id, None)
        if alert:
            # Audit log the removal
            audit_logger.info(
                f"{reason} | {product_id} | "
                f"Alert: {json.dumps(_alert_summary(alert))}"
            )
            logger.info(f"Removed alert: {product_id}")
            self._notify_removed(alert)
            return True
        return False

    def reconcile_api_alerts(self, active_ids: set, miss_threshold: int = 2) -> int:
        """Remove API-sourced alerts that have dropped out of the NWS API active
        feed — an out-of-band cancellation we never received as a CAN/EXP.

        An alert must be absent for ``miss_threshold`` consecutive reconciliations
        before removal, so a single transient/partial feed response doesn't wipe
        active alerts. NWWS-sourced alerts are never reaped here — NWWS delivers
        their cancellations directly, and the API lags it. Call this only with a
        confirmed non-empty active feed.

        Returns the number of alerts removed.
        """
        removed = 0
        for alert in list(self._alerts.values()):
            if alert.source != "api" or alert.product_id in active_ids:
                self._api_missing_counts.pop(alert.product_id, None)
                continue
            misses = self._api_missing_counts.get(alert.product_id, 0) + 1
            if misses >= miss_threshold:
                self._api_missing_counts.pop(alert.product_id, None)
                self.remove_alert(alert.product_id, reason="RECONCILED")
                removed += 1
            else:
                self._api_missing_counts[alert.product_id] = misses
        # Drop counters for alerts that are no longer present at all.
        for pid in [p for p in self._api_missing_counts if p not in self._alerts]:
            self._api_missing_counts.pop(pid, None)
        return removed

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

        for product_id, alert in list(self._alerts.items()):
            if alert.expiration_time and alert.expiration_time <= now:
                expired_ids.append(product_id)
            elif alert.expiration_time is None:
                # Backstop: an alert with no parsed end time never trips the check
                # above, so a missed cancellation would keep it forever. Purge it
                # once it is older than a generous max age (longer than any real
                # watch/warning) using the issue/parse time as the reference.
                ref = alert.issued_time or alert.parsed_at
                if ref and (now - ref) > timedelta(hours=NO_EXPIRY_MAX_AGE_HOURS):
                    expired_ids.append(product_id)

        for product_id in expired_ids:
            alert = self._alerts.pop(product_id, None)
            if alert:
                alert.mark_expired()
                # Audit log the expiration
                audit_logger.info(
                    f"EXPIRED | {product_id} | "
                    f"Expiration: {alert.expiration_time.isoformat() if alert.expiration_time else 'None'} | "
                    f"Alert: {json.dumps(_alert_summary(alert))}"
                )
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

            # Write atomically: a crash/kill mid-write must not truncate the live
            # file (a truncated active_alerts.json fails to load → all alerts lost).
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(path)

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
                        # Audit log restored alert
                        audit_logger.info(
                            f"RESTORED | {alert.product_id} | "
                            f"Alert: {json.dumps(_alert_summary(alert))}"
                        )
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
    # Setup the audit logger for detailed alert operation logging
    setup_audit_logger()

    manager = get_alert_manager()
    manager.load_from_file()

    # Run cleanup immediately to remove any expired alerts from the file
    expired_count = manager.cleanup_expired()
    if expired_count > 0:
        logger.info(f"Cleaned up {expired_count} expired alerts on startup")
        manager.save_to_file()  # Save cleaned state

    await manager.start_cleanup_task()


async def stop_alert_manager():
    """Stop the alert manager and save state."""
    global _manager
    if _manager:
        await _manager.stop_cleanup_task()
        _manager.save_to_file()
        _manager = None
