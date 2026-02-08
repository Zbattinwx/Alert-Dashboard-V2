"""
Chase Log Service for Alert Dashboard V2.

Automatically records chase journey data (waypoints, events) for
post-chase review. Each chase day produces a JSON file in data/chase_logs/.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in miles between two points."""
    import math
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class ChaseLogService:
    """Records chase journey for post-chase review."""

    def __init__(self, data_dir: Path):
        self._log_dir = data_dir / "chase_logs"
        self._radar_dir = self._log_dir / "radar"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._radar_dir.mkdir(parents=True, exist_ok=True)

        self._active_session: Optional[dict] = None
        self._session_file: Optional[Path] = None
        self._waypoint_interval = 10  # seconds between saved waypoints
        self._last_waypoint_time: float = 0
        self._save_interval = 30  # seconds between disk writes
        self._last_save_time: float = 0
        self._dirty = False

    def start_session(self, chaser_name: str) -> dict:
        """Start a new chase session or resume today's."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._session_file = self._log_dir / f"{today}_chase.json"

        # Resume existing session if it exists
        if self._session_file.exists():
            try:
                with open(self._session_file, "r") as f:
                    self._active_session = json.load(f)
                logger.info(f"Resumed chase log session: {today}")
                return self._active_session
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load existing chase log, starting fresh: {e}")

        # New session
        self._active_session = {
            "date": today,
            "chaser_name": chaser_name,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "end_time": None,
            "waypoints": [],
            "events": [],
            "stats": {
                "total_miles": 0,
                "max_speed_mph": 0,
                "warnings_entered": 0,
                "radar_snapshots": 0,
            },
        }
        self._save_to_disk()
        logger.info(f"Started new chase log session: {today}")
        return self._active_session

    def log_waypoint(self, lat: float, lon: float, speed: Optional[float],
                     heading: Optional[float], timestamp: Optional[str] = None):
        """Add a waypoint to the active session (throttled)."""
        import time

        now = time.time()
        if now - self._last_waypoint_time < self._waypoint_interval:
            return

        if not self._active_session:
            # Auto-start session with generic name
            self.start_session("Chaser")

        self._last_waypoint_time = now
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        waypoint = {"t": ts, "lat": round(lat, 6), "lon": round(lon, 6)}
        if speed is not None:
            waypoint["spd"] = round(speed, 1)
        if heading is not None:
            waypoint["hdg"] = round(heading, 1)

        # Update stats
        stats = self._active_session["stats"]
        if speed is not None and speed > stats["max_speed_mph"]:
            stats["max_speed_mph"] = round(speed, 1)

        # Calculate distance from last waypoint
        wps = self._active_session["waypoints"]
        if wps:
            last = wps[-1]
            dist = _haversine_miles(last["lat"], last["lon"], lat, lon)
            # Only count reasonable distances (filter GPS jumps)
            if dist < 5:
                stats["total_miles"] = round(stats["total_miles"] + dist, 2)

        wps.append(waypoint)
        self._dirty = True

        # Periodic save to disk
        if now - self._last_save_time > self._save_interval:
            self._save_to_disk()

    def log_event(self, event_type: str, data: dict):
        """Log an event (entered_polygon, radar_snapshot, etc.)."""
        if not self._active_session:
            return

        event = {
            "t": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "data": data,
        }
        self._active_session["events"].append(event)

        # Update event-specific stats
        stats = self._active_session["stats"]
        if event_type == "entered_polygon":
            stats["warnings_entered"] = stats.get("warnings_entered", 0) + 1
        elif event_type == "radar_snapshot":
            stats["radar_snapshots"] = stats.get("radar_snapshots", 0) + 1

        self._dirty = True
        self._save_to_disk()

    def end_session(self):
        """Close the active session."""
        if not self._active_session:
            return

        self._active_session["end_time"] = datetime.now(timezone.utc).isoformat()
        self._save_to_disk()
        logger.info(
            f"Chase log session ended: {len(self._active_session['waypoints'])} waypoints, "
            f"{self._active_session['stats']['total_miles']} miles"
        )
        self._active_session = None
        self._session_file = None

    def get_session(self, date: str) -> Optional[dict]:
        """Get a chase log by date (YYYY-MM-DD)."""
        path = self._log_dir / f"{date}_chase.json"
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def list_sessions(self) -> list[dict]:
        """List all chase log sessions (metadata only)."""
        sessions = []
        for path in sorted(self._log_dir.glob("*_chase.json"), reverse=True):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                sessions.append({
                    "date": data.get("date", path.stem.replace("_chase", "")),
                    "chaser_name": data.get("chaser_name", "Unknown"),
                    "start_time": data.get("start_time"),
                    "end_time": data.get("end_time"),
                    "waypoint_count": len(data.get("waypoints", [])),
                    "event_count": len(data.get("events", [])),
                    "stats": data.get("stats", {}),
                })
            except (json.JSONDecodeError, IOError):
                continue
        return sessions

    def get_session_geojson(self, date: str) -> Optional[dict]:
        """Export a chase log as GeoJSON LineString."""
        session = self.get_session(date)
        if not session or not session.get("waypoints"):
            return None

        coordinates = [
            [wp["lon"], wp["lat"]] for wp in session["waypoints"]
        ]

        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coordinates,
                    },
                    "properties": {
                        "date": session["date"],
                        "chaser_name": session.get("chaser_name", "Unknown"),
                        "total_miles": session.get("stats", {}).get("total_miles", 0),
                        "max_speed_mph": session.get("stats", {}).get("max_speed_mph", 0),
                    },
                }
            ],
        }

    @property
    def active_session(self) -> Optional[dict]:
        return self._active_session

    @property
    def radar_dir(self) -> Path:
        return self._radar_dir

    def _save_to_disk(self):
        """Write active session to disk."""
        import time
        if not self._active_session or not self._session_file:
            return
        try:
            with open(self._session_file, "w") as f:
                json.dump(self._active_session, f, indent=None, separators=(",", ":"))
            self._dirty = False
            self._last_save_time = time.time()
        except IOError as e:
            logger.error(f"Failed to save chase log: {e}")


# Singleton
_service: Optional[ChaseLogService] = None


def get_chase_log_service() -> ChaseLogService:
    """Get the singleton Chase Log service instance."""
    global _service
    if _service is None:
        from ..config import get_settings
        settings = get_settings()
        _service = ChaseLogService(settings.data_dir)
    return _service
