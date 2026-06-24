"""
Event Statistics Service for Alert Dashboard V2.

Tracks cumulative alert counts, peak activity, and a timeline of key events.
State is persisted to disk and restored on quick reboots (< 10 minutes).
A rolling history log powers the 24h and 7d historical views.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

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

SIG_LABELS = {"W": "Warning", "A": "Watch", "Y": "Advisory", "S": "Statement"}

MAX_TIMELINE = 100
PERSIST_THRESHOLD_SECONDS = 600  # 10 minutes
MAX_HISTORY_DAYS = 7


def _data_dir() -> Path:
    try:
        try:
            from .settings import get_settings
        except ImportError:
            from backend.config.settings import get_settings
        return get_settings().data_dir
    except Exception:
        return Path("data")


@dataclass
class TimelineEvent:
    time: datetime
    event_type: str
    event_name: str
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
    """Tracks session-scoped event statistics with disk persistence."""

    def __init__(self):
        self.recovered_from_restart: bool = False
        if not self._try_load_persisted():
            self._fresh_start()

    def _fresh_start(self):
        self.session_start: datetime = datetime.now(timezone.utc)
        self.issued: dict[str, dict[str, int]] = {}
        self.peak_concurrent: int = 0
        self.current_concurrent: int = 0
        self.max_hail_in: Optional[float] = None
        self.max_wind_mph: Optional[float] = None
        self.tornado_emergency_count: int = 0
        self.pds_count: int = 0
        self.timeline: list[TimelineEvent] = []
        self._add_timeline("session_reset", "Session started", "")

    def reset(self):
        """Start a new event session — clears all counters and timeline."""
        self.recovered_from_restart = False
        self._fresh_start()
        self._persist()
        logger.info("Event stats session reset")

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _state_file(self) -> Path:
        return _data_dir() / "event_stats_state.json"

    def _history_file(self) -> Path:
        return _data_dir() / "alert_history.json"

    def _persist(self):
        """Write current session state to disk."""
        try:
            data = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "session_start": self.session_start.isoformat(),
                "issued": self.issued,
                "peak_concurrent": self.peak_concurrent,
                "current_concurrent": self.current_concurrent,
                "max_hail_in": self.max_hail_in,
                "max_wind_mph": self.max_wind_mph,
                "tornado_emergency_count": self.tornado_emergency_count,
                "pds_count": self.pds_count,
                "timeline": [e.to_dict() for e in self.timeline],
            }
            path = self._state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f)
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"Could not persist event stats state: {e}")

    def _try_load_persisted(self) -> bool:
        """Load state from disk if it is less than PERSIST_THRESHOLD_SECONDS old."""
        path = self._state_file()
        if not path.exists():
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            saved_at = datetime.fromisoformat(data["saved_at"])
            age_s = (datetime.now(timezone.utc) - saved_at).total_seconds()
            if age_s > PERSIST_THRESHOLD_SECONDS:
                return False
            self.session_start = datetime.fromisoformat(data["session_start"])
            self.issued = data.get("issued", {})
            self.peak_concurrent = data.get("peak_concurrent", 0)
            self.current_concurrent = data.get("current_concurrent", 0)
            self.max_hail_in = data.get("max_hail_in")
            self.max_wind_mph = data.get("max_wind_mph")
            self.tornado_emergency_count = data.get("tornado_emergency_count", 0)
            self.pds_count = data.get("pds_count", 0)
            self.timeline = [
                TimelineEvent(
                    time=datetime.fromisoformat(e["time"]),
                    event_type=e["event_type"],
                    event_name=e["event_name"],
                    phenomenon=e.get("phenomenon", ""),
                    significance=e.get("significance", ""),
                    location=e.get("location", ""),
                    is_emergency=e.get("is_emergency", False),
                )
                for e in data.get("timeline", [])
            ]
            self.recovered_from_restart = True
            logger.info(f"Recovered event stats session from {age_s:.0f}s ago")
            return True
        except Exception as e:
            logger.warning(f"Could not load persisted event stats: {e}")
            return False

    def _log_to_history(
        self,
        event: str,
        phenomenon: str,
        significance: str,
        location: str,
        is_emergency: bool,
        is_pds: bool,
        max_hail_in: Optional[float],
        max_wind_mph: Optional[float],
    ):
        """Append one record to the rolling alert history log."""
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "phenomenon": phenomenon,
            "significance": significance,
            "location": location,
            "is_emergency": is_emergency,
            "is_pds": is_pds,
            "max_hail_in": max_hail_in,
            "max_wind_mph": max_wind_mph,
        }
        path = self._history_file()
        try:
            history: list[dict] = []
            if path.exists():
                try:
                    with open(path) as f:
                        history = json.load(f)
                except Exception:
                    history = []
            history.append(record)
            # Trim to MAX_HISTORY_DAYS
            cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_HISTORY_DAYS)
            history = [r for r in history if datetime.fromisoformat(r["time"]) >= cutoff]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(history, f)
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"Could not write alert history: {e}")

    # ------------------------------------------------------------------
    # Event callbacks
    # ------------------------------------------------------------------

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
        phenomenon = getattr(alert, "phenomenon", "") or ""
        significance = ""
        sig = getattr(alert, "significance", None)
        if sig is not None:
            significance = sig.value if hasattr(sig, "value") else str(sig)

        if phenomenon not in self.issued:
            self.issued[phenomenon] = {}
        sig_key = significance or "?"
        self.issued[phenomenon][sig_key] = self.issued[phenomenon].get(sig_key, 0) + 1

        self.current_concurrent += 1
        if self.current_concurrent > self.peak_concurrent:
            self.peak_concurrent = self.current_concurrent

        event_name = getattr(alert, "event_name", "") or ""
        # Canonical PDS / Tornado Emergency detection (single source of truth on
        # the Alert model, replacing the old per-service raw_text scans).
        is_emergency = bool(getattr(alert, "is_tornado_emergency", False))
        is_pds = bool(getattr(alert, "is_pds", False))
        hail_val: Optional[float] = None
        wind_val: Optional[float] = None

        threat = getattr(alert, "threat", None)
        if threat:
            max_hail = getattr(threat, "max_hail_size", None)
            if max_hail:
                try:
                    hail_val = float(max_hail)
                    if self.max_hail_in is None or hail_val > self.max_hail_in:
                        self.max_hail_in = hail_val
                except (ValueError, TypeError):
                    pass
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
        if is_pds:
            self.pds_count += 1

        location = getattr(alert, "display_locations", "") or ""
        if not location:
            areas = getattr(alert, "affected_areas", []) or []
            location = ", ".join(areas[:3]) if areas else ""

        if significance in ("W", "A") and phenomenon:
            sig_label = SIG_LABELS.get(significance, significance)
            phen_label = TRACKED_PHENOMENA.get(phenomenon, phenomenon)
            self._add_timeline(
                "new_alert",
                f"New {phen_label} {sig_label}" + (" (EMERGENCY)" if is_emergency else ""),
                location,
                phenomenon=phenomenon,
                significance=significance,
                is_emergency=is_emergency,
            )

        self._log_to_history("added", phenomenon, significance, location,
                              is_emergency, is_pds, hail_val, wind_val)
        self._persist()

    def on_alert_removed(self, alert: Any):
        self.current_concurrent = max(0, self.current_concurrent - 1)
        phenomenon = getattr(alert, "phenomenon", "") or ""
        significance = ""
        sig = getattr(alert, "significance", None)
        if sig is not None:
            significance = sig.value if hasattr(sig, "value") else str(sig)

        location = getattr(alert, "display_locations", "") or ""
        if significance == "W" and phenomenon in TRACKED_PHENOMENA:
            status = getattr(alert, "status", None)
            status_str = status.value if hasattr(status, "value") else str(status)
            sig_label = SIG_LABELS.get(significance, significance)
            phen_label = TRACKED_PHENOMENA.get(phenomenon, phenomenon)
            self._add_timeline(
                "alert_expired",
                f"{phen_label} {sig_label} {status_str}",
                location,
                phenomenon=phenomenon,
                significance=significance,
            )

        self._log_to_history("removed", phenomenon, significance, location,
                              False, False, None, None)
        self._persist()

    # ------------------------------------------------------------------
    # Stats output
    # ------------------------------------------------------------------

    def get_stats(self, lsr_stats: Optional[dict] = None) -> dict[str, Any]:
        """Return current session stats."""
        now = datetime.now(timezone.utc)
        duration_s = int((now - self.session_start).total_seconds())
        hours, remainder = divmod(duration_s, 3600)
        minutes, _ = divmod(remainder, 60)

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
            "window": "session",
            "session_start": self.session_start.isoformat(),
            "session_duration": f"{hours}h {minutes:02d}m",
            "session_duration_s": duration_s,
            "recovered_from_restart": self.recovered_from_restart,
            "total_issued": sum(sum(s.values()) for s in self.issued.values()),
            "current_active": self.current_concurrent,
            "peak_concurrent": self.peak_concurrent,
            "tornado_emergency_count": self.tornado_emergency_count,
            "pds_count": self.pds_count,
            "max_hail_in": self.max_hail_in,
            "max_wind_mph": self.max_wind_mph,
            "by_phenomenon": by_phenomenon,
            "timeline": [e.to_dict() for e in reversed(self.timeline)],
            "lsr": lsr_stats or {},
        }

    def compute_historical_stats(self, hours: int, lsr_stats: Optional[dict] = None) -> dict[str, Any]:
        """Compute stats from the rolling history log for the last `hours` hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        path = self._history_file()

        history: list[dict] = []
        if path.exists():
            try:
                with open(path) as f:
                    history = json.load(f)
            except Exception:
                pass

        events = sorted(
            (r for r in history if datetime.fromisoformat(r["time"]) >= cutoff),
            key=lambda r: r["time"],
        )

        issued: dict[str, dict[str, int]] = {}
        tornado_emergency_count = 0
        pds_count = 0
        max_hail_in: Optional[float] = None
        max_wind_mph: Optional[float] = None
        concurrent = 0
        peak_concurrent = 0

        for r in events:
            phenomenon = r.get("phenomenon", "")
            significance = r.get("significance", "")
            if r["event"] == "added":
                if phenomenon not in issued:
                    issued[phenomenon] = {}
                sig_key = significance or "?"
                issued[phenomenon][sig_key] = issued[phenomenon].get(sig_key, 0) + 1
                concurrent += 1
                if concurrent > peak_concurrent:
                    peak_concurrent = concurrent
                if r.get("is_emergency"):
                    tornado_emergency_count += 1
                if r.get("is_pds"):
                    pds_count += 1
                h = r.get("max_hail_in")
                if h is not None:
                    if max_hail_in is None or h > max_hail_in:
                        max_hail_in = h
                w = r.get("max_wind_mph")
                if w is not None:
                    if max_wind_mph is None or w > max_wind_mph:
                        max_wind_mph = w
            elif r["event"] == "removed":
                concurrent = max(0, concurrent - 1)

        by_phenomenon = []
        for phen, sig_counts in issued.items():
            phen_label = TRACKED_PHENOMENA.get(phen, phen)
            total = sum(sig_counts.values())
            by_phenomenon.append({
                "phenomenon": phen,
                "label": phen_label,
                "total": total,
                "by_significance": sig_counts,
            })
        by_phenomenon.sort(key=lambda x: -x["total"])

        label = f"Last {hours} Hours" if hours < 48 else f"Last {hours // 24} Days"

        return {
            "window": f"{hours}h",
            "window_label": label,
            "total_issued": sum(sum(s.values()) for s in issued.values()),
            "current_active": self.current_concurrent,
            "peak_concurrent": peak_concurrent,
            "tornado_emergency_count": tornado_emergency_count,
            "pds_count": pds_count,
            "max_hail_in": max_hail_in,
            "max_wind_mph": max_wind_mph,
            "by_phenomenon": by_phenomenon,
            "timeline": [],
            "lsr": lsr_stats or {},
        }


_service: Optional[EventStatsService] = None


def get_event_stats_service() -> EventStatsService:
    global _service
    if _service is None:
        _service = EventStatsService()
    return _service
