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
    """Saved state of a cell for change detection across scans."""
    threat_level: str
    severity_score: int
    # Rotation / tornado
    rotation_detected: bool
    low_level_meso_detected: bool
    tvs_detected: bool
    debris_signature: bool
    # Phase 2: kinematic wind signatures
    downburst_detected: bool = False
    marc_signature_detected: bool = False
    straight_line_wind_detected: bool = False
    strong_wind_detected: bool = False
    rij_detected: bool = False
    # Phase 3: enhanced dual-pol
    tbss_detected: bool = False
    # Phase 5: BWER + MESH
    bwer_detected: bool = False
    mesh_large: bool = False        # MESH crosses MESH_LARGE_HAIL_MM (44 mm / 1.75")


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
            mesh = getattr(cell, "mesh_mm", None)
            self._snapshots[cell.cell_id] = _CellSnapshot(
                threat_level=cell.threat_level,
                severity_score=cell.severity_score,
                rotation_detected=cell.rotation_detected,
                low_level_meso_detected=getattr(cell, "low_level_meso_detected", False),
                tvs_detected=cell.tvs_detected,
                debris_signature=cell.debris_signature,
                downburst_detected=getattr(cell, "downburst_detected", False),
                marc_signature_detected=getattr(cell, "marc_signature_detected", False),
                straight_line_wind_detected=getattr(cell, "straight_line_wind_detected", False),
                strong_wind_detected=getattr(cell, "strong_wind_detected", False),
                rij_detected=getattr(cell, "rij_detected", False),
                tbss_detected=getattr(cell, "tbss_detected", False),
                bwer_detected=getattr(cell, "bwer_detected", False),
                mesh_large=bool(mesh is not None and mesh >= 44.0),
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
                continue

            prev_order = THREAT_ORDER.get(prev.threat_level, 0)
            cur_order  = THREAT_ORDER.get(cell.threat_level, 0)

            triggered = False
            cur_llm = getattr(cell, "low_level_meso_detected", False)
            if cur_order > prev_order:
                triggered = True  # Threat level escalated
            elif not prev.rotation_detected and cell.rotation_detected:
                triggered = True  # Newly developed mesocyclone
            elif not prev.low_level_meso_detected and cur_llm:
                triggered = True  # Rotation descended into boundary layer
            elif not prev.tvs_detected and cell.tvs_detected:
                triggered = True  # New tornado vortex signature
            elif not prev.debris_signature and cell.debris_signature:
                triggered = True  # TDS — possible tornado on the ground
            # Phase 2: kinematic wind signatures
            elif not prev.downburst_detected and getattr(cell, "downburst_detected", False):
                triggered = True  # New downburst divergence signature
            elif not prev.marc_signature_detected and getattr(cell, "marc_signature_detected", False):
                triggered = True  # MARC — strong updraft inflow developing
            elif not prev.rij_detected and getattr(cell, "rij_detected", False):
                triggered = True  # Rear-inflow jet confirmed in bow echo
            elif (not prev.straight_line_wind_detected and
                  getattr(cell, "straight_line_wind_detected", False)):
                triggered = True  # Severe straight-line wind swath developed
            elif (not prev.strong_wind_detected and
                  getattr(cell, "strong_wind_detected", False) and
                  not getattr(cell, "straight_line_wind_detected", False)):
                triggered = True  # Sub-severe but notable wind swath developed
            # Phase 3: enhanced dual-pol
            elif not prev.tbss_detected and getattr(cell, "tbss_detected", False):
                triggered = True  # TBSS — large hail physically confirmed
            # Phase 5: BWER + MESH
            elif not prev.bwer_detected and getattr(cell, "bwer_detected", False):
                triggered = True  # BWER — intense updraft signature
            elif not prev.mesh_large:
                cur_mesh = getattr(cell, "mesh_mm", None)
                if cur_mesh is not None and cur_mesh >= 44.0:
                    triggered = True  # MESH crossed significant-severe threshold

            if triggered:
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

            # Tornado / rotation — highest priority
            if cell.debris_signature:
                depth = getattr(cell, "tds_tilt_count", 0)
                flags.append(
                    f"DEBRIS SIGNATURE ({depth}-tilt depth) — tornado likely on ground"
                )
            if cell.tvs_detected:
                flags.append("TORNADO VORTEX SIGNATURE")
            elif cell.rotation_detected and cell.rotation_velocity_ms:
                if getattr(cell, "low_level_meso_detected", False):
                    base = getattr(cell, "rotation_base_km", None)
                    base_str = f", base {base:.1f} km" if base is not None else ""
                    flags.append(
                        f"LOW-LEVEL MESO {cell.rotation_velocity_ms:.0f} m/s{base_str}"
                    )
                else:
                    peak = getattr(cell, "max_rot_height_km", None)
                    peak_str = f" @ {peak:.1f} km" if peak is not None else ""
                    flags.append(
                        f"mid-level rotation {cell.rotation_velocity_ms:.0f} m/s{peak_str}"
                    )

            # Hail — MESH (Witt 1998) is the preferred size estimate
            mesh_mm = getattr(cell, "mesh_mm", None)
            if mesh_mm is not None and mesh_mm >= 76.0:
                flags.append(f"GIANT HAIL ~{mesh_mm:.0f} mm (MESH)")
            elif mesh_mm is not None and mesh_mm >= 44.0:
                flags.append(f"LARGE HAIL ~{mesh_mm:.0f} mm (MESH)")
            elif getattr(cell, "tbss_detected", False):
                flags.append("THREE-BODY SCATTER SPIKE — large hail confirmed aloft")
            elif mesh_mm is not None and mesh_mm >= 19.0:
                flags.append(f"hail ~{mesh_mm:.0f} mm (MESH)")
            elif cell.hail_indicated:
                dbz = f" ({cell.hail_max_dbz:.0f} dBZ)" if cell.hail_max_dbz else ""
                flags.append(f"hail indicated{dbz}")

            # BWER — strong updraft signature
            if getattr(cell, "bwer_detected", False):
                over = getattr(cell, "bwer_overhang_dbz", None)
                over_str = f" ({over:.0f} dBZ overhang)" if over else ""
                flags.append(f"BWER{over_str}")

            # Phase 2: kinematic wind
            if getattr(cell, "downburst_detected", False):
                dv = getattr(cell, "downburst_delta_v_ms", None)
                dv_str = f" ΔV={dv:.0f} m/s" if dv else ""
                flags.append(f"DOWNBURST{dv_str}")
            if getattr(cell, "marc_signature_detected", False):
                cv = getattr(cell, "marc_convergence_ms", None)
                cv_str = f" ({cv:.0f} m/s convergence)" if cv else ""
                flags.append(f"MARC signature{cv_str}")
            if getattr(cell, "rij_detected", False):
                flags.append("REAR-INFLOW JET — bow echo wind threat elevated")
            elif getattr(cell, "straight_line_wind_detected", False):
                v = getattr(cell, "max_wind_velocity_ms", None)
                v_str = f" {v:.0f} m/s ({v * 1.944:.0f} kt)" if v else ""
                flags.append(f"SEVERE straight-line winds{v_str}")
            elif getattr(cell, "strong_wind_detected", False):
                v = getattr(cell, "max_wind_velocity_ms", None)
                v_str = f" {v:.0f} m/s ({v * 1.944:.0f} kt)" if v else ""
                flags.append(f"strong sub-severe wind swath{v_str}")

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
