"""
Alert data model for Alert Dashboard V2.
Represents a weather alert with all parsed data.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Optional


class AlertPriority(IntEnum):
    """Alert priority levels (lower number = higher priority)."""
    TORNADO_WARNING = 1
    SEVERE_THUNDERSTORM_WARNING = 2
    TORNADO_WATCH = 3
    FLASH_FLOOD_WARNING = 4
    SEVERE_THUNDERSTORM_WATCH = 5
    WINTER_STORM_WARNING = 6
    BLIZZARD_WARNING = 7
    ICE_STORM_WARNING = 8
    FLASH_FLOOD_WATCH = 9
    WINTER_STORM_WATCH = 10
    WIND_CHILL_WARNING = 11
    SPECIAL_WEATHER_STATEMENT = 12
    WINTER_WEATHER_ADVISORY = 13
    OTHER = 99


class AlertStatus(str, Enum):
    """Alert status values."""
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    UPDATED = "updated"


class AlertSignificance(str, Enum):
    """VTEC significance codes."""
    WARNING = "W"
    WATCH = "A"
    ADVISORY = "Y"
    STATEMENT = "S"
    OUTLOOK = "O"
    SYNOPSIS = "N"
    FORECAST = "F"


class VTECAction(str, Enum):
    """VTEC action codes."""
    NEW = "NEW"       # New event
    CON = "CON"       # Continuing event (no changes)
    EXT = "EXT"       # Extended in time
    EXA = "EXA"       # Expanded in area
    EXB = "EXB"       # Extended and expanded
    UPG = "UPG"       # Upgraded (e.g., watch to warning)
    CAN = "CAN"       # Cancelled
    EXP = "EXP"       # Expired
    COR = "COR"       # Correction
    ROU = "ROU"       # Routine (marine forecasts)


# Phenomenon codes and their display names
PHENOMENON_NAMES = {
    "TO": "Tornado",
    "SV": "Severe Thunderstorm",
    "FF": "Flash Flood",
    "FA": "Areal Flood",
    "FL": "Flood",
    "WS": "Winter Storm",
    "BZ": "Blizzard",
    "IS": "Ice Storm",
    "LE": "Lake Effect Snow",
    "WW": "Winter Weather",
    "WC": "Wind Chill",
    "EC": "Extreme Cold",
    "HT": "Heat",
    "EH": "Excessive Heat",
    "FG": "Dense Fog",
    "SM": "Dense Smoke",
    "HW": "High Wind",
    "EW": "Extreme Wind",
    "WI": "Wind",
    "DS": "Dust Storm",
    "FR": "Frost",
    "FZ": "Freeze",
    "HZ": "Hard Freeze",
    "AS": "Air Stagnation",
    "CF": "Coastal Flood",
    "LS": "Lakeshore Flood",
    "SU": "High Surf",
    "RP": "Rip Current",
    "BW": "Brisk Wind",
    "SC": "Small Craft",
    "SW": "Small Craft Wind",
    "RB": "Small Craft Rough Bar",
    "SI": "Small Craft Seas",
    "GL": "Gale",
    "SE": "Hazardous Seas",
    "SR": "Storm",
    "HF": "Hurricane Force Wind",
    "TR": "Tropical Storm",
    "HU": "Hurricane",
    "TY": "Typhoon",
    "SS": "Storm Surge",
    "TS": "Tsunami",
    "MA": "Marine",
    "SQ": "Snow Squall",
    "AF": "Ashfall",
    "LO": "Low Water",
    "ZF": "Freezing Fog",
    "ZR": "Freezing Rain",
    "UP": "Ice Accretion",
    "ZY": "Freezing Spray",
    "FW": "Fire Weather",
    "RF": "Red Flag",
    "EQ": "Earthquake",
    "VO": "Volcano",
    "AV": "Avalanche",
    "SPS": "Special Weather Statement",
}

# Priority mapping for phenomenon codes
PHENOMENON_PRIORITIES = {
    "TO": AlertPriority.TORNADO_WARNING,
    "SV": AlertPriority.SEVERE_THUNDERSTORM_WARNING,
    "FF": AlertPriority.FLASH_FLOOD_WARNING,
    "WS": AlertPriority.WINTER_STORM_WARNING,
    "BZ": AlertPriority.BLIZZARD_WARNING,
    "IS": AlertPriority.ICE_STORM_WARNING,
    "WC": AlertPriority.WIND_CHILL_WARNING,
    "SPS": AlertPriority.SPECIAL_WEATHER_STATEMENT,
    "WW": AlertPriority.WINTER_WEATHER_ADVISORY,
}


@dataclass
class StormMotion:
    """Storm motion data extracted from alert."""
    direction_degrees: Optional[int] = None  # 0-360, direction storm is moving TO
    direction_from: Optional[str] = None     # Cardinal direction (FROM, e.g., "SW")
    speed_mph: Optional[int] = None          # Speed in MPH
    speed_kts: Optional[int] = None          # Speed in knots

    @property
    def is_valid(self) -> bool:
        """Check if motion data is valid."""
        return self.direction_degrees is not None and self.speed_mph is not None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "direction_degrees": self.direction_degrees,
            "direction_from": self.direction_from,
            "speed_mph": self.speed_mph,
            "speed_kts": self.speed_kts,
        }


@dataclass
class ThreatData:
    """Threat information extracted from alert."""
    # Tornado
    tornado_detection: Optional[str] = None  # "RADAR INDICATED", "OBSERVED", etc.
    tornado_damage_threat: Optional[str] = None  # "CONSIDERABLE", "CATASTROPHIC"

    # Wind
    max_wind_gust_mph: Optional[int] = None
    max_wind_gust_kts: Optional[int] = None
    wind_damage_threat: Optional[str] = None  # "CONSIDERABLE", "DESTRUCTIVE"

    # Hail
    max_hail_size_inches: Optional[float] = None
    hail_damage_threat: Optional[str] = None

    # Winter weather
    snow_amount_min_inches: Optional[float] = None
    snow_amount_max_inches: Optional[float] = None
    ice_accumulation_inches: Optional[float] = None

    # Flood
    flash_flood_detection: Optional[str] = None
    flash_flood_damage_threat: Optional[str] = None

    # Storm motion
    storm_motion: Optional[StormMotion] = None

    @property
    def has_tornado(self) -> bool:
        """Check if tornado threat exists."""
        return self.tornado_detection is not None

    @property
    def has_significant_wind(self) -> bool:
        """Check if significant wind threat (>= 70 mph)."""
        return self.max_wind_gust_mph is not None and self.max_wind_gust_mph >= 70

    @property
    def has_significant_hail(self) -> bool:
        """Check if significant hail threat (>= 1 inch)."""
        return self.max_hail_size_inches is not None and self.max_hail_size_inches >= 1.0

    @property
    def is_pds(self) -> bool:
        """Check if this is a Particularly Dangerous Situation."""
        return (
            self.tornado_damage_threat in ("CONSIDERABLE", "CATASTROPHIC") or
            self.wind_damage_threat in ("CONSIDERABLE", "DESTRUCTIVE", "CATASTROPHIC") or
            self.hail_damage_threat in ("CONSIDERABLE", "CATASTROPHIC")
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result = {
            "tornado_detection": self.tornado_detection,
            "tornado_damage_threat": self.tornado_damage_threat,
            "max_wind_gust_mph": self.max_wind_gust_mph,
            "max_wind_gust_kts": self.max_wind_gust_kts,
            "wind_damage_threat": self.wind_damage_threat,
            "max_hail_size_inches": self.max_hail_size_inches,
            "hail_damage_threat": self.hail_damage_threat,
            "snow_amount_min_inches": self.snow_amount_min_inches,
            "snow_amount_max_inches": self.snow_amount_max_inches,
            "ice_accumulation_inches": self.ice_accumulation_inches,
            "flash_flood_detection": self.flash_flood_detection,
            "flash_flood_damage_threat": self.flash_flood_damage_threat,
        }
        if self.storm_motion:
            result["storm_motion"] = self.storm_motion.to_dict()
        return result


@dataclass
class VTECInfo:
    """Parsed VTEC string information."""
    product_class: str = "O"  # O=Operational, T=Test, E=Experimental
    action: VTECAction = VTECAction.NEW
    office: str = ""          # WFO code (e.g., "KCLE")
    phenomenon: str = ""      # 2-char phenomenon code
    significance: AlertSignificance = AlertSignificance.WARNING
    event_tracking_number: int = 0
    begin_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    raw_vtec: str = ""        # Original VTEC string

    @property
    def is_cancellation(self) -> bool:
        """Check if this VTEC represents a cancellation."""
        return self.action in (VTECAction.CAN, VTECAction.EXP)

    @property
    def is_update(self) -> bool:
        """Check if this VTEC represents an update."""
        return self.action in (VTECAction.CON, VTECAction.EXT, VTECAction.EXA, VTECAction.EXB, VTECAction.UPG, VTECAction.COR)

    @property
    def is_new(self) -> bool:
        """Check if this is a new event."""
        return self.action == VTECAction.NEW

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "product_class": self.product_class,
            "action": self.action.value,
            "office": self.office,
            "phenomenon": self.phenomenon,
            "significance": self.significance.value,
            "event_tracking_number": self.event_tracking_number,
            "begin_time": self.begin_time.isoformat() if self.begin_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "raw_vtec": self.raw_vtec,
        }


@dataclass
class Alert:
    """
    Complete weather alert data model.

    This represents a fully parsed alert from either the NWS API or NWWS-OI.
    """
    # Identification
    product_id: str = ""              # Unique ID: "{phenomenon}.{office}.{event_number}"
    message_id: Optional[str] = None  # CAP/API message ID
    source: str = "unknown"           # "nwws" or "api"

    # VTEC information
    vtec: Optional[VTECInfo] = None

    # Alert classification
    phenomenon: str = ""              # 2-char code (TO, SV, FF, etc.)
    significance: AlertSignificance = AlertSignificance.WARNING
    event_name: str = ""              # Full event name (e.g., "Tornado Warning")
    headline: str = ""                # Alert headline
    description: str = ""             # Full alert description
    instruction: str = ""             # Safety instructions

    # Timing
    issued_time: Optional[datetime] = None
    effective_time: Optional[datetime] = None
    onset_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None  # When the EVENT ends
    message_expires: Optional[datetime] = None  # When the MESSAGE stops being distributed

    # Geography
    affected_areas: list[str] = field(default_factory=list)  # UGC codes
    fips_codes: list[str] = field(default_factory=list)      # 5-digit FIPS codes
    display_locations: str = ""                               # Human-readable location string
    polygon: list[list[float]] = field(default_factory=list) # [[lat, lon], ...] coordinates
    centroid: Optional[tuple[float, float]] = None           # (lat, lon) center point

    # Office information
    sender_office: str = ""           # WFO code (e.g., "CLE")
    sender_name: str = ""             # Full office name

    # Threat data
    threat: ThreatData = field(default_factory=ThreatData)

    # Status
    status: AlertStatus = AlertStatus.ACTIVE
    priority: AlertPriority = AlertPriority.OTHER

    # Metadata
    raw_text: str = ""                # Original raw alert text
    parsed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    update_count: int = 0             # Number of times this alert has been updated

    def __post_init__(self):
        """Post-initialization processing."""
        # Set priority based on phenomenon if not already set
        if self.priority == AlertPriority.OTHER and self.phenomenon:
            self.priority = PHENOMENON_PRIORITIES.get(
                self.phenomenon,
                AlertPriority.OTHER
            )
            # Adjust for watches vs warnings
            if self.significance == AlertSignificance.WATCH:
                if self.phenomenon == "TO":
                    self.priority = AlertPriority.TORNADO_WATCH
                elif self.phenomenon == "SV":
                    self.priority = AlertPriority.SEVERE_THUNDERSTORM_WATCH
                elif self.phenomenon == "FF":
                    self.priority = AlertPriority.FLASH_FLOOD_WATCH
                elif self.phenomenon == "WS":
                    self.priority = AlertPriority.WINTER_STORM_WATCH

        # Set event name if not provided
        if not self.event_name and self.phenomenon:
            base_name = PHENOMENON_NAMES.get(self.phenomenon, "Unknown")
            significance_suffix = {
                AlertSignificance.WARNING: "Warning",
                AlertSignificance.WATCH: "Watch",
                AlertSignificance.ADVISORY: "Advisory",
                AlertSignificance.STATEMENT: "Statement",
            }.get(self.significance, "")
            self.event_name = f"{base_name} {significance_suffix}".strip()

    @property
    def is_active(self) -> bool:
        """Check if alert is currently active."""
        if self.status != AlertStatus.ACTIVE:
            return False
        if self.expiration_time is None:
            return True
        return datetime.now(timezone.utc) < self.expiration_time

    @property
    def is_expired(self) -> bool:
        """Check if alert has expired."""
        if self.expiration_time is None:
            return False
        return datetime.now(timezone.utc) >= self.expiration_time

    @property
    def is_watch(self) -> bool:
        """Check if this is a watch."""
        return self.significance == AlertSignificance.WATCH

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning."""
        return self.significance == AlertSignificance.WARNING

    @property
    def is_high_priority(self) -> bool:
        """Check if this is a high priority alert (tornado/severe warning)."""
        return self.priority <= AlertPriority.FLASH_FLOOD_WARNING

    @property
    def time_remaining_seconds(self) -> Optional[int]:
        """Get seconds until expiration."""
        if self.expiration_time is None:
            return None
        delta = self.expiration_time - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds()))

    @property
    def time_remaining_str(self) -> str:
        """Get human-readable time remaining."""
        seconds = self.time_remaining_seconds
        if seconds is None:
            return "Unknown"
        if seconds <= 0:
            return "Expired"
        hours, remainder = divmod(seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def mark_updated(self) -> None:
        """Mark this alert as updated."""
        self.last_updated = datetime.now(timezone.utc)
        self.update_count += 1

    def mark_expired(self) -> None:
        """Mark this alert as expired."""
        self.status = AlertStatus.EXPIRED
        self.mark_updated()

    def mark_cancelled(self) -> None:
        """Mark this alert as cancelled."""
        self.status = AlertStatus.CANCELLED
        self.mark_updated()

    def to_dict(self) -> dict[str, Any]:
        """Convert alert to dictionary for JSON serialization."""
        return {
            "product_id": self.product_id,
            "message_id": self.message_id,
            "source": self.source,
            "vtec": self.vtec.to_dict() if self.vtec else None,
            "phenomenon": self.phenomenon,
            "significance": self.significance.value,
            "event_name": self.event_name,
            "headline": self.headline,
            "description": self.description,
            "instruction": self.instruction,
            "issued_time": self.issued_time.isoformat() if self.issued_time else None,
            "effective_time": self.effective_time.isoformat() if self.effective_time else None,
            "onset_time": self.onset_time.isoformat() if self.onset_time else None,
            "expiration_time": self.expiration_time.isoformat() if self.expiration_time else None,
            "message_expires": self.message_expires.isoformat() if self.message_expires else None,
            "affected_areas": self.affected_areas,
            "fips_codes": self.fips_codes,
            "display_locations": self.display_locations,
            "polygon": self.polygon,
            "centroid": list(self.centroid) if self.centroid else None,
            "sender_office": self.sender_office,
            "sender_name": self.sender_name,
            "threat": self.threat.to_dict(),
            "status": self.status.value,
            "priority": self.priority.value,
            "is_active": self.is_active,
            "is_high_priority": self.is_high_priority,
            "time_remaining": self.time_remaining_str,
            "parsed_at": self.parsed_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "update_count": self.update_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alert":
        """Create Alert from dictionary."""
        # Parse datetime fields
        datetime_fields = [
            "issued_time", "effective_time", "onset_time",
            "expiration_time", "message_expires", "parsed_at", "last_updated"
        ]
        for field_name in datetime_fields:
            if data.get(field_name):
                data[field_name] = datetime.fromisoformat(data[field_name])

        # Parse enum fields
        if data.get("significance"):
            data["significance"] = AlertSignificance(data["significance"])
        if data.get("status"):
            data["status"] = AlertStatus(data["status"])
        if data.get("priority"):
            data["priority"] = AlertPriority(data["priority"])

        # Parse nested objects
        if data.get("vtec"):
            vtec_data = data["vtec"]
            if vtec_data.get("action"):
                vtec_data["action"] = VTECAction(vtec_data["action"])
            if vtec_data.get("significance"):
                vtec_data["significance"] = AlertSignificance(vtec_data["significance"])
            if vtec_data.get("begin_time"):
                vtec_data["begin_time"] = datetime.fromisoformat(vtec_data["begin_time"])
            if vtec_data.get("end_time"):
                vtec_data["end_time"] = datetime.fromisoformat(vtec_data["end_time"])
            data["vtec"] = VTECInfo(**vtec_data)

        if data.get("threat"):
            threat_data = data["threat"]
            if threat_data.get("storm_motion"):
                threat_data["storm_motion"] = StormMotion(**threat_data["storm_motion"])
            data["threat"] = ThreatData(**threat_data)

        # Handle centroid tuple
        if data.get("centroid"):
            data["centroid"] = tuple(data["centroid"])

        # Remove computed fields that aren't in __init__
        data.pop("is_active", None)
        data.pop("is_high_priority", None)
        data.pop("time_remaining", None)

        return cls(**data)

    def __hash__(self):
        """Hash based on product_id."""
        return hash(self.product_id)

    def __eq__(self, other):
        """Equality based on product_id."""
        if not isinstance(other, Alert):
            return False
        return self.product_id == other.product_id
