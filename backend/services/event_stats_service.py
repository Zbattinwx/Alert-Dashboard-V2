"""
Event Statistics Service for Alert Dashboard V2.

Tracks cumulative alert counts, peak activity, and a timeline of key events
for the current monitoring session. Resets on demand to start a new "event."
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Phenomena we track individually in the stats
TRACKED_PHENOMENA = {
    "TO": "Tornado",
    "SV": "Severe T-Storm",
    "FF": "Flash Flood",
    "EW": "Extreme Wind",
    "BZ": "Blizzard",
    "IS": "Ice Storm",
    "WS": "Winter Storm",
    "HW": "High Wind",
    "SQ": "Snow Squall",
    "LE": "Lake Effect Snow",
}

# Significance labels
SIG_LABELS = {"W": "Warning", "A": "Watch", "Y": "Advisory", "S": "Statement"}

# Maximum timeline events to keep
MAX_TIMELINE = 100


@dataclass
class TimelineEvent:
    """A single notable event in the session timeline."""
    time: datetime
    event_type: str          # "new_alert", "alert_updated", "alert_expired", "session_reset"
    event_name: str          # Human-readable name
    phenomenon: str = ""
    significance: str = ""
    location: str = ""
    is_emergency: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat(),
            "event_type": self.event_type,
            "event_name": self.event_name,
            "phenomenon": self.phenomenon,
            "significance": self.significance,
            "location": self.location,
            "is_emergency": self.is_emergency,
        }


class EventStatsService:
    """Tracks session-scoped event statistics."""

    def __init__(self):
        self.reset()

    def reset(self):
        """Start a new event session. Clears all counters and timeline."""
        self.session_start: datetime = datetime.now(timezone.utc)
        # Cumulative counts: {phenomenon: {significance: count}}
        self.issued: dict[str, dict[str, int]] = {}
        # Peak concurrent active alert count
        self.peak_concurrent: int = 0
        self.current_concurrent: int = 0
        # Peak values observed from threat data
        self.max_hail_in: Optional[float] = None
        self.max_wind_mph: Optional[float] = None
        self.tornado_emergency_count: int = 0
        self.pds_count: int = 0
        # Timeline
        self.timeline: list[TimelineEvent] = []
        self._add_timeline("session_reset", "Session started", "")
        logger.info("Event stats session reset")

    def _add_timeline(self, event_type: str, event_name: str, location: str,
                      phenomenon: str = "", significance: str = "",
                      is_emergency: bool = False):
        ev = TimelineEvent(
            time=datetime.now(timezone.utc),
            event_type=event_type,
            event_name=event_name,
            phenomenon=phenomenon,
            significance=significance,
            location=location,
            is_emergency=is_emergency,
        )
        self.timeline.append(ev)
        if len(self.timeline) > MAX_TIMELINE:
            self.timeline.pop(0)

    def on_alert_added(self, alert: Any):
        """Called when a new alert is added. Alert is an Alert model instance."""
        phenomenon = getattr(alert, "phenomenon", "") or ""
        significance = ""
        sig = getattr(alert, "significance", None)
        if sig is not None:
            significance = sig.value if hasattr(sig, "value") else str(sig)

        # Count issued
        if phenomenon not in self.issued:
            self.issued[phenomenon] = {}
        sig_key = significance or "?"
        self.issued[phenomenon][sig_key] = self.issued[phenomenon].get(sig_key, 0) + 1

        # Update concurrent high-water mark
        self.current_concurrent += 1
        if self.current_concurrent > self.peak_concurrent:
            self.peak_concurrent = self.current_concurrent

        # Check for emergencies / PDS
        event_name = getattr(alert, "event_name", "") or ""
        is_emergency = "emergency" in event_name.lower()
        threat = getattr(alert, "threat", None)
        if threat:
            tornado_threat = getattr(threat, "tornado_damage_threat", None)
            if tornado_threat == "CATASTROPHIC":
                is_emergency = True
            # Update max hail
            max_hail = getattr(threat, "max_hail_size", None)
            if max_hail:
                try:
                    hail_val = float(max_hail)
                    if self.max_hail_in is None or hail_val > self.max_hail_in:
                        self.max_hail_in = hail_val
                except (ValueError, TypeError):
                    pass
            # Update max wind
            max_wind = getattr(threat, "max_wind_gust", None)
            if max_wind:
                try:
                    wind_val = float(max_wind)
                    if self.max_wind_mph is None or wind_val > self.max_wind_mph:
                        self.max_wind_mph = wind_val
                except (ValueError, TypeError):
                    pass

        if is_emergency:
            self.tornado_emergency_count += 1

        # Check for PDS (particularly dangerous situation)
        raw_text = getattr(alert, "raw_text", "") or ""
        if "THIS IS A PARTICULARLY DANGEROUS SITUATION" in raw_text.upper():
            self.pds_count += 1

        # Add to timeline (only for significant alerts)
        sig_label = SIG_LABELS.get(significance, significance)
        phen_label = TRACKED_PHENOMENA.get(phenomenon, phenomenon)
        location = getattr(alert, "display_locations", "") or ""
        if not location:
            areas = getattr(alert, "affected_areas", []) or []
            location = ", ".join(areas[:3]) if areas else ""

        if significance in ("W", "A") and phenomenon:
            self._add_timeline(
                "new_alert",
                f"New {phen_label} {sig_label}" + (" (EMERGENCY)" if is_emergency else ""),
                location,
                phenomenon=phenomenon,
                significance=significance,
                is_emergency=is_emergency,
            )

    def on_alert_removed(self, alert: Any):
        """Called when an alert is removed/expired."""
        self.current_concurrent = max(0, self.current_concurrent - 1)
        phenomenon = getattr(alert, "phenomenon", "") or ""
        significance = ""
        sig = getattr(alert, "significance", None)
        if sig is not None:
            significance = sig.value if hasattr(sig, "value") else str(sig)

        if significance == "W" and phenomenon in TRACKED_PHENOMENA:
            status = getattr(alert, "status", None)
            status_str = status.value if hasattr(status, "value") else str(status)
            event_name = getattr(alert, "event_name", "") or ""
            sig_label = SIG_LABELS.get(significance, significance)
            phen_label = TRACKED_PHENOMENA.get(phenomenon, phenomenon)
            location = getattr(alert, "display_locations", "") or ""
            self._add_timeline(
                "alert_expired",
                f"{phen_label} {sig_label} {status_str}",
                location,
                phenomenon=phenomenon,
                significance=significance,
            )

    def get_stats(self, lsr_stats: Optional[dict] = None) -> dict[str, Any]:
        """Return current stats as a JSON-serializable dict."""
        now = datetime.now(timezone.utc)
        duration_s = int((now - self.session_start).total_seconds())
        hours, remainder = divmod(duration_s, 3600)
        minutes, _ = divmod(remainder, 60)

        # Build per-phenomenon summary
        by_phenomenon = []
        for phen, sig_counts in self.issued.items():
            phen_label = TRACKED_PHENOMENA.get(phen, phen)
            total = sum(sig_counts.values())
            by_phenomenon.append({
                "phenomenon": phen,
                "label": phen_label,
                "total": total,
                "by_significance": sig_counts,
            })
        by_phenomenon.sort(key=lambda x: -x["total"])

        return {
            "session_start": self.session_start.isoformat(),
            "session_duration": f"{hours}h {minutes:02d}m",
            "session_duration_s": duration_s,
            "total_issued": sum(sum(s.values()) for s in self.issued.values()),
            "current_active": self.current_concurrent,
            "peak_concurrent": self.peak_concurrent,
            "tornado_emergency_count": self.tornado_emergency_count,
            "pds_count": self.pds_count,
            "max_hail_in": self.max_hail_in,
            "max_wind_mph": self.max_wind_mph,
            "by_phenomenon": by_phenomenon,
            "timeline": [e.to_dict() for e in reversed(self.timeline)],  # newest first
            "lsr": lsr_stats or {},
        }


# Global singleton
_service: Optional[EventStatsService] = None


def get_event_stats_service() -> EventStatsService:
    global _service
    if _service is None:
        _service = EventStatsService()
    return _service
