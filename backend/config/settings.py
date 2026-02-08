"""
Application settings for Alert Dashboard V2.
Uses Pydantic for validation and environment variable loading.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Get project root (parent of backend directory)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_USER_SETTINGS_FILE = _PROJECT_ROOT / "data" / "user_settings.json"


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Environment
    environment: str = Field(default="development", description="development, staging, or production")
    debug: bool = Field(default=False, description="Enable debug mode")
    log_level: str = Field(default="INFO", description="Logging level")

    # Logging settings
    log_to_file: bool = Field(default=True, description="Enable file logging")
    log_dir: Path = Field(default=Path("logs"), description="Directory for log files")
    log_max_size_mb: int = Field(default=10, description="Max log file size in MB before rotation")
    log_backup_count: int = Field(default=5, description="Number of backup log files to keep")
    log_to_console: bool = Field(default=True, description="Also output logs to console")

    # Branding
    brand: str = Field(default="default", description="Brand configuration to use (e.g., 'onw', 'battinfront')")

    # Server settings
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="HTTP server port")
    websocket_port: int = Field(default=8765, description="WebSocket server port")

    # NWWS-OI (Weather Wire) credentials
    nwws_username: Optional[str] = Field(default=None, description="NWWS-OI username")
    nwws_password: Optional[str] = Field(default=None, description="NWWS-OI password")
    nwws_server: str = Field(default="nwws-oi.weather.gov", description="NWWS-OI server")
    nwws_resource: str = Field(default="nwws", description="NWWS-OI resource")

    # NWS API settings
    nws_api_base_url: str = Field(default="https://api.weather.gov", description="NWS API base URL")
    nws_api_user_agent: str = Field(default="AlertDashboardV2/2.0", description="User agent for NWS API")
    nws_api_timeout: int = Field(default=30, description="NWS API request timeout in seconds")
    nws_api_retry_count: int = Field(default=3, description="Number of retries for NWS API")

    # Alert source configuration
    alert_source: str = Field(default="nwws", description="Primary alert source: 'nwws' or 'api'")
    use_api_fallback: bool = Field(default=True, description="Use NWS API as fallback when NWWS fails")

    # Geographic filtering
    filter_states: list[str] = Field(
        default=["OH", "IN", "IL"],
        description="States to include in alerts"
    )
    filter_offices: list[str] = Field(
        default=[],
        description="NWS offices to filter (empty = all)"
    )
    filter_ugc_codes: list[str] = Field(
        default=[],
        description="Specific UGC codes to include (empty = all)"
    )

    # Alert type filtering - which phenomena to show on dashboard
    # Empty list = show ALL alerts (not recommended for NWWS)
    # Common codes:
    #   Tornado: TO, TOR, TOA (warning, warning alt, watch)
    #   Severe Thunderstorm: SV, SVR, SVS, SVA (warning, warning alt, statement, watch)
    #   Flash Flood: FF, FFW, FFS, FFA (warning, warning alt, statement, watch)
    #   Flood: FL, FLW, FLS, FLA (warning, warning alt, statement, watch)
    #   Winter Storm: WS, WSW, WSA (warning, warning alt, watch)
    #   Blizzard: BZ (warning)
    #   Ice Storm: IS (warning)
    #   Lake Effect Snow: LE (warning)
    #   Winter Weather: WW (advisory)
    #   Wind Chill: WC (warning/advisory)
    #   High Wind: HW (warning)
    #   Special Weather Statement: SPS
    target_phenomena: list[str] = Field(
        default=[
            # Tornado
            "TO", "TOR", "TOA",
            # Severe Thunderstorm
            "SV", "SVR", "SVS", "SVA",
            # Flash Flood
            "FF", "FFW", "FFS", "FFA",
            # Flood
            "FL", "FLW", "FLS", "FLA",
            # Winter Storm
            "WS", "WSW", "WSA",
            # Blizzard
            "BZ",
            # Ice Storm
            "IS",
            # Lake Effect Snow
            "LE",
            # Winter Weather Advisory
            "WW",
            # Wind Chill
            "WC",
            # Cold Weather
            "CW",
            # High Wind
            "HW",
            # Special Weather Statement (thunderstorm-related only)
            "SPS",
        ],
        description="Phenomena codes to show on dashboard (empty = all)"
    )

    # Alert expiration
    default_alert_lifetime_minutes: int = Field(
        default=60,
        description="Default lifetime for alerts without expiration"
    )
    alert_cleanup_interval_seconds: int = Field(
        default=60,
        description="Interval for cleaning up expired alerts"
    )

    # API polling
    api_poll_interval_seconds: int = Field(
        default=300,
        description="Interval for polling NWS API (seconds, default 5 min)"
    )

    # Zone geometry caching
    cache_zone_geometries: bool = Field(default=True, description="Cache zone geometries")
    zone_cache_ttl_hours: int = Field(default=24, description="Zone geometry cache TTL")

    # Dashboard password (optional)
    dashboard_password: Optional[str] = Field(default=None, description="Dashboard access password")

    # Data persistence
    data_dir: Path = Field(default=Path("data"), description="Directory for data files")
    persist_alerts: bool = Field(default=True, description="Persist active alerts on shutdown")

    # ODOT (Ohio DOT) API settings
    odot_api_key: Optional[str] = Field(
        default="775df0cb-3d4c-4c66-953c-9e3c8a8ed27c",
        description="ODOT OHGO API key"
    )
    odot_api_base_url: str = Field(
        default="https://publicapi.ohgo.com/api/v1",
        description="ODOT OHGO API base URL"
    )
    odot_cache_ttl_seconds: int = Field(
        default=300,
        description="ODOT data cache TTL (5 minutes)"
    )

    # Camera-in-alert settings - which alert types should trigger camera display
    camera_alert_phenomena: list[str] = Field(
        default=["TO", "SV", "SVR"],
        description="Alert phenomena that trigger camera-in-alert detection"
    )

    # Cold pavement thresholds
    cold_pavement_threshold: int = Field(
        default=40,
        description="Threshold for 'cold' pavement warning (Fahrenheit)"
    )
    freezing_pavement_threshold: int = Field(
        default=32,
        description="Threshold for 'freezing' pavement warning (Fahrenheit)"
    )

    # LLM Assistant Configuration (Ollama)
    llm_enabled: bool = Field(
        default=True,
        description="Enable LLM assistant features"
    )
    ollama_host: str = Field(
        default="http://localhost:11434",
        description="Ollama API host URL"
    )
    ollama_model: str = Field(
        default="gemma3:4b",
        description="Ollama model to use for inference"
    )
    llm_timeout: int = Field(
        default=120,
        description="LLM request timeout in seconds"
    )

    # Google Chat Notifications
    google_chat_enabled: bool = Field(
        default=False,
        description="Enable Google Chat alert notifications (default: OFF)"
    )
    google_chat_webhook_url: Optional[str] = Field(
        default=None,
        description="Google Chat webhook URL for sending alerts"
    )
    google_chat_phenomena: list[str] = Field(
        default=["TO", "SV", "FF"],
        description="Alert phenomena to send to Google Chat (TO=Tornado, SV=Severe, FF=Flash Flood)"
    )

    @field_validator("filter_states", mode="before")
    @classmethod
    def parse_states(cls, v):
        """Parse states from JSON array, comma-separated string, or list."""
        if isinstance(v, str):
            # Handle JSON array format: ["OH", "IN"]
            v = v.strip()
            if v.startswith("["):
                import json
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [s.strip().upper() for s in parsed if s]
                except json.JSONDecodeError:
                    pass
            # Handle comma-separated format: OH, IN, IL
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v] if v else []

    @field_validator("filter_offices", "filter_ugc_codes", mode="before")
    @classmethod
    def parse_list(cls, v):
        """Parse list from comma-separated string or list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v if v else []

    @field_validator("target_phenomena", "camera_alert_phenomena", "google_chat_phenomena", mode="before")
    @classmethod
    def parse_phenomena(cls, v):
        """Parse phenomena from comma-separated string or list."""
        if isinstance(v, str):
            # Handle comma-separated string
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v] if v else []

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v):
        """Validate log level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_v = v.upper()
        if upper_v not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return upper_v

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment.lower() == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.environment.lower() == "development"


def _load_user_overrides() -> dict:
    """Load user settings overrides from data/user_settings.json."""
    if _USER_SETTINGS_FILE.exists():
        try:
            with open(_USER_SETTINGS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load user settings: {e}")
    return {}


def _save_user_overrides(overrides: dict) -> None:
    """Save user settings overrides to data/user_settings.json."""
    _USER_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_USER_SETTINGS_FILE, "w") as f:
        json.dump(overrides, f, indent=2)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance, with user overrides applied."""
    settings = Settings()
    overrides = _load_user_overrides()
    if "target_phenomena" in overrides:
        settings.target_phenomena = [
            p.upper() for p in overrides["target_phenomena"]
        ]
        logger.info(
            f"Applied user override: {len(settings.target_phenomena)} target phenomena"
        )
    return settings


def reload_settings() -> Settings:
    """Reload settings (clears cache)."""
    get_settings.cache_clear()
    return get_settings()
