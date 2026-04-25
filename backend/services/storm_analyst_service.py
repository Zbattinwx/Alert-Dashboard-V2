"""
Storm Analyst Service.

Proactively monitors storm cells after each radar scan and generates
agent-powered notifications when notable developments are detected
(new high-score cells, threat level escalation, new rotation/TVS/debris).

Rate-limited to avoid notification spam.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Minimum seconds between any proactive notification
NOTIFICATION_COOLDOWN_SEC = 180  # 3 minutes

# Minimum severity score for a brand-new cell to trigger a notification
NEW_CELL_NOTIFY_SCORE = 45

# Threat level ordering (higher = more dangerous)
THREAT_ORDER = {
    "minimal": 0,
    "moderate": 1,
    "significant": 2,
    "severe": 3,
    "extreme": 4,
}


@dataclass
class _CellSnapshot:
    """Saved state of a cell for change detection."""
    threat_level: str
    rotation_detected: bool
    tvs_detected: bool
    debris_signature: bool
    severity_score: int


class StormAnalystService:
    """Monitors storm cells and fires proactive notifications on notable changes."""

    def __init__(self):
        self._snapshots: dict[str, _CellSnapshot] = {}
        self._last_notification: Optional[datetime] = None
        # async (content: str, cells: list[dict], notification_id: str, timestamp: str) -> None
        self._on_notification: Optional[Callable] = None
        # async (cells: list[TrackedStormCell]) -> str
        self._agent_analyze: Optional[Callable] = None

    def set_broadcast_callback(self, callback: Callable) -> None:
        """Set the callback used to broadcast a notification via WebSocket."""
        self._on_notification = callback

    def set_agent_callback(self, callback: Callable) -> None:
        """Set the callback that calls the LLM to generate notification text."""
        self._agent_analyze = callback

    async def process_cells(self, cells: list) -> None:
        """
        Called after each storm tracking update.
        Detects notable changes, calls the agent, and fires a notification.
        """
        if not cells:
            return

        # Rate limit: enforce minimum gap between notifications
        now = datetime.now(timezone.utc)
        if self._last_notification is not None:
            elapsed = (now - self._last_notification).total_seconds()
            if elapsed < NOTIFICATION_COOLDOWN_SEC:
                return

        notable = self._detect_changes(cells)
        if not notable:
            return

        # Update snapshots for all cells (so we detect future changes correctly)
        for cell in cells:
            self._snapshots[cell.cell_id] = _CellSnapshot(
                threat_level=cell.threat_level,
                rotation_detected=cell.rotation_detected,
                tvs_detected=cell.tvs_detected,
                debris_signature=cell.debris_signature,
                severity_score=cell.severity_score,
            )

        content = await self._generate_content(notable)
        if not content:
            return

        self._last_notification = now

        if self._on_notification:
            await self._on_notification(
                content=content,
                cells=[c.to_dict() for c in notable],
                notification_id=str(uuid.uuid4()),
                timestamp=now.isoformat(),
            )

    def _detect_changes(self, cells: list) -> list:
        """Return cells that have undergone notable new developments."""
        notable = []
        for cell in cells:
            if cell.scan_count < 0:  # Dissipating — skip
                continue

            prev = self._snapshots.get(cell.cell_id)

            if prev is None:
                # Brand-new cell: notify if score is significant enough
                if cell.severity_score >= NEW_CELL_NOTIFY_SCORE:
                    notable.append(cell)
            else:
                prev_order = THREAT_ORDER.get(prev.threat_level, 0)
                cur_order = THREAT_ORDER.get(cell.threat_level, 0)

                if cur_order > prev_order:
                    # Threat level escalated
                    notable.append(cell)
                elif not prev.rotation_detected and cell.rotation_detected:
                    # Newly developed rotation
                    notable.append(cell)
                elif not prev.tvs_detected and cell.tvs_detected:
                    # New tornado vortex signature
                    notable.append(cell)
                elif not prev.debris_signature and cell.debris_signature:
                    # New debris signature (possible tornado on ground)
                    notable.append(cell)

        return notable

    async def _generate_content(self, cells: list) -> str:
        """Generate notification text via agent or simple fallback."""
        if self._agent_analyze is not None:
            try:
                result = await self._agent_analyze(cells)
                if result:
                    return result
            except Exception as e:
                logger.error(f"Storm analyst agent call failed: {e}")
        return self._fallback_content(cells)

    @staticmethod
    def _fallback_content(cells: list) -> str:
        """Basic notification text when LLM is unavailable."""
        parts = []
        for cell in cells:
            flags = []
            if cell.debris_signature:
                flags.append("DEBRIS SIGNATURE — possible tornado on ground")
            if cell.tvs_detected:
                flags.append("TORNADO VORTEX SIGNATURE")
            elif cell.rotation_detected:
                rot = (
                    f" at {cell.rotation_velocity_ms:.0f} m/s"
                    if cell.rotation_velocity_ms
                    else ""
                )
                flags.append(f"rotation detected{rot}")
            if cell.hail_indicated:
                flags.append("hail indicated")

            flag_str = " | ".join(flags) if flags else cell.threat_level.upper()
            parts.append(
                f"Cell {cell.cell_id}: {cell.threat_level.upper()} "
                f"(score {cell.severity_score}) — {flag_str}, "
                f"moving {cell.motion_direction_deg:.0f}° at {cell.motion_speed_kph:.0f} kph"
            )

        return "Storm development detected: " + " / ".join(parts)

    def cleanup_stale(self, active_ids: set) -> None:
        """Remove saved snapshots for cells that no longer exist."""
        stale = [cid for cid in self._snapshots if cid not in active_ids]
        for cid in stale:
            del self._snapshots[cid]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_analyst: Optional[StormAnalystService] = None


def get_storm_analyst_service() -> Optional[StormAnalystService]:
    return _analyst


def create_storm_analyst_service() -> StormAnalystService:
    global _analyst
    _analyst = StormAnalystService()
    return _analyst
