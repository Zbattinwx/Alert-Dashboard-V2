"""
Application settings for Alert Dashboard V2.
Uses Pydantic for validation and environment variable loading.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Environment
    environment: str = Field(default="development", description="development, staging, or production")
    debug: bool = Field(default=False, description="Enable debug mode")
    log_level: str = Field(default="INFO", description="Logging level")

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
        default=["OH", "IN", "MI", "KY", "WV", "PA"],
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

    @field_validator("filter_states", mode="before")
    @classmethod
    def parse_states(cls, v):
        """Parse states from comma-separated string or list."""
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v]

    @field_validator("filter_offices", "filter_ugc_codes", mode="before")
    @classmethod
    def parse_list(cls, v):
        """Parse list from comma-separated string or list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v if v else []

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


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Reload settings (clears cache)."""
    get_settings.cache_clear()
    return get_settings()
