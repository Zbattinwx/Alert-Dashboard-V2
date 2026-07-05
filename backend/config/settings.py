"""
Application settings for Alert Dashboard V2.
Uses Pydantic for validation and environment variable loading.
"""

import json
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Get project root (parent of backend directory)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_USER_SETTINGS_FILE = _PROJECT_ROOT / "data" / "user_settings.json"

# .env resolution. When frozen (PyInstaller), __file__ lives in the unpacked
# bundle's temp dir, so the project .env isn't reachable — and we deliberately do
# NOT bake the .env (it holds the NWWS password) into the distributed exe. Read it
# from beside the executable and the working directory instead, so a packaged
# dashboard still picks up NWWS credentials. Without this the frozen backend has
# no credentials and silently falls back to slow API-only alert polling (a
# tornado warning takes ~2 min via the API vs. instant over NWWS). pydantic also
# reads OS environment variables, so those keep working regardless.
if getattr(sys, "frozen", False):
    _ENV_FILE: tuple[str, ...] = (
        str(Path(sys.executable).parent / ".env"),
        str(Path.cwd() / ".env"),
    )
else:
    _ENV_FILE = (str(_PROJECT_ROOT / ".env"),)


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
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

    # Self-update (standalone Windows deployment only). The dashboard checks this
    # manifest for a newer packaged build and, on operator confirmation, downloads
    # + verifies + applies it in place. See backend/services/update_service.py.
    dashboard_update_enabled: bool = Field(
        default=True, description="Enable the in-dashboard self-updater (packaged Windows build)"
    )
    dashboard_update_manifest_url: str = Field(
        default="https://github.com/Zbattinwx/dashboard-releases/releases/latest/download/latest.json",
        description="URL of the update manifest (version + sha256 + zip URL)",
    )
    dashboard_update_check_interval_minutes: int = Field(
        default=30, description="How often to re-check the update manifest (minutes)"
    )

    # NWWS-OI (Weather Wire) credentials
    nwws_username: Optional[str] = Field(default=None, description="NWWS-OI username")
    nwws_password: Optional[str] = Field(default=None, description="NWWS-OI password")
    nwws_server: str = Field(default="nwws-oi.weather.gov", description="NWWS-OI server")
    nwws_resource: str = Field(default="nwws", description="NWWS-OI resource")

    # Stadia Maps API key — when set, broadcast graphics use the same
    # "alidade_smooth_dark" basemap as the radar (server-side raster tiles
    # require a key). Empty falls back to the keyless CARTO dark basemap.
    stadia_api_key: str = Field(default="", description="Stadia Maps API key for broadcast-graphic basemap")

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
    # Per-state county filter: {state_code: [county UGC codes]}.  When a monitored
    # state has a non-empty list, only alerts touching those counties are kept;
    # a state absent from this map (or mapped to []) keeps all its counties.
    filter_counties: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-state county UGC filter; empty/absent state = all counties"
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

    # 511-family camera API keys by state code (register a free key per state,
    # e.g. https://511ny.org/developers). Env: CAMERAS_511_KEYS='{"NY":"<key>"}'.
    cameras_511_keys: dict[str, str] = Field(
        default_factory=dict,
        description="511 GetCameras API keys keyed by state code, e.g. {'NY': '...'}"
    )
    cameras_511_cache_ttl_seconds: int = Field(
        default=300,
        description="511 camera list cache TTL (5 minutes)"
    )

    # CARS-program GraphQL camera states (keyless; live HLS). Empty list disables.
    # Known members: CO, IN, IA, KS, MA, MN, NE. None = all known states.
    cameras_cars_states: Optional[list[str]] = Field(
        default=None,
        description="CARS GraphQL state codes to fetch cameras from (None = all known)"
    )
    cameras_cars_cache_ttl_seconds: int = Field(
        default=900,
        description="CARS camera list cache TTL (15 minutes; locations are near-static)"
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
        description="Ollama API host URL",
        validation_alias="OLLAMA_API_URL",
    )
    ollama_model: str = Field(
        default="gemma3:4b",
        description="Ollama model to use for inference"
    )
    llm_timeout: int = Field(
        default=120,
        description="LLM request timeout in seconds"
    )

    # AI Agent Configuration (tool-calling agent using Ollama)
    agent_enabled: bool = Field(
        default=True,
        description="Enable AI agent with tool calling"
    )
    agent_model: str = Field(
        default="qwen2.5:7b",
        description="Ollama model for agent (must support tool calling)"
    )
    agent_max_tool_rounds: int = Field(
        default=5,
        description="Maximum tool-call rounds per user message"
    )
    agent_tool_timeout: int = Field(
        default=30,
        description="Timeout per tool execution in seconds"
    )

    # Remote Access
    caddy_enabled: bool = Field(default=False, description="Enable Caddy reverse proxy")
    domain: str = Field(default="localhost", description="Domain name for remote access")

    # Spotter Network Integration
    spotter_network_enabled: bool = Field(default=False, description="Enable Spotter Network position polling")
    spotter_network_username: Optional[str] = Field(default=None, description="Spotter Network username")
    spotter_network_password: Optional[str] = Field(default=None, description="Spotter Network password")
    spotter_network_marker_id: Optional[int] = Field(default=None, description="Spotter Network marker ID")
    spotter_network_poll_interval: int = Field(default=30, description="Spotter Network poll interval in seconds")

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

    # Social Media - Facebook
    fb_enabled: bool = Field(default=False, description="Enable Facebook posting")
    fb_page_id: str = Field(default="885594544634534", description="Facebook Page ID")
    fb_access_token: Optional[str] = Field(default=None, description="Facebook Page Access Token")

    # Social Media - Bluesky
    bsky_enabled: bool = Field(default=False, description="Enable Bluesky posting")
    bsky_handle: str = Field(default="zbattin.bsky.social", description="Bluesky handle")
    bsky_app_password: Optional[str] = Field(default=None, description="Bluesky app password")

    # NEXRAD Level 2 Radar
    nexrad_enabled: bool = Field(default=False, description="Enable Level 2 NEXRAD radar processing")
    nexrad_default_site: str = Field(default="KILN", description="Default NEXRAD site ICAO code (e.g., KILN for Wilmington OH)")
    nexrad_poll_interval: int = Field(default=10, description="Seconds between checking for new volume scans (most polls are a single S3 LIST and key compare, so a tight interval is cheap)")
    nexrad_history_count: int = Field(default=10, description="Number of past volume scans to keep in memory")
    nexrad_grid_resolution_km: float = Field(default=1.0, description="Grid resolution in km (increase for lower-power hardware)")
    nexrad_max_range_km: int = Field(default=230, description="Maximum radar range in km for rendering")

    # NEXRAD chunks bucket — near-real-time partial volume scans from
    # `unidata-nexrad-level2-chunks`.  Runs alongside the archive bucket
    # pipeline; both broadcast the same RadarFrame/VolumeScanData shape.
    # Default OFF so the new path can be enabled per deployment after it's
    # been verified against live data.
    nexrad_chunks_enabled: bool = Field(default=False, description="Enable the near-real-time chunks-bucket ingestion path (runs alongside archive)")
    nexrad_chunks_poll_interval: int = Field(default=10, description="Seconds between chunks-bucket LIST polls")
    nexrad_chunks_min_chunks_for_partial: int = Field(default=5, description="Render a partial-volume scan once this many chunks (counting the S header) have arrived. Lower = faster but fewer tilts; 5 ≈ first 3-4 tilts")
    nexrad_chunks_render_on_complete: bool = Field(default=True, description="Also render once at end-of-volume (E chunk) for the complete tilt set")
    nexrad_chunks_partial_refresh_chunks: int = Field(default=4, description="Re-broadcast a partial after at least this many additional chunks have arrived since the last partial (0 disables refresh). Keeps the displayed scan current during long VCPs.")
    nexrad_chunks_partial_refresh_min_interval_s: int = Field(default=60, description="Minimum seconds between successive partial re-broadcasts of the same volume — protects CPU since each render is ~10s.")

    # Live QA reporter — in-process per-scan storm cell QA logging.  Runs as
    # an additional callback on the storm tracking service when NEXRAD is on.
    live_qa_enabled: bool = Field(default=True, description="Run the in-process live QA reporter alongside the storm tracking service")
    live_qa_log_training_data: bool = Field(default=False, description="Append every cell to data/training_data.jsonl for ML training")
    live_qa_min_score: int = Field(default=30, description="Suppress cells below this severity score from QA log output (flagged cells always print)")
    live_qa_verbose: bool = Field(default=False, description="Show detailed rotation/structure block for every notable cell")

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

    @field_validator("filter_counties", mode="before")
    @classmethod
    def parse_counties(cls, v):
        """Parse per-state county map from a JSON string or dict."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                return {}
        if isinstance(v, dict):
            return {
                str(k).upper(): [str(c).upper() for c in (codes or [])]
                for k, codes in v.items()
            }
        return {}

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


# --- User-supplied NWWS-OI credentials -------------------------------------
# These are entered by the END USER in the radar app (we deliberately don't ship
# a .env with our own credentials). They must persist in a USER-WRITABLE location
# that survives restarts and app updates — the install dir (Program Files) is not
# writable, so we use the per-user app-data dir, not beside the exe.
def _nwws_creds_file() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "TheBattinFrontRadar" / "nwws_credentials.json"


def load_nwws_credentials() -> Optional[dict]:
    """Return {'username','password'} from the user creds file, or None."""
    f = _nwws_creds_file()
    if f.exists():
        try:
            with open(f, "r") as fh:
                d = json.load(fh)
            if d.get("username") and d.get("password"):
                return {"username": d["username"], "password": d["password"]}
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load NWWS credentials: {e}")
    return None


def save_nwws_credentials(username: str, password: str) -> None:
    """Persist (or, with blank values, clear) the user's NWWS-OI credentials."""
    f = _nwws_creds_file()
    if not username or not password:
        try:
            f.unlink(missing_ok=True)  # clear → fall back to NWS API
        except OSError as e:
            logger.warning(f"Failed to clear NWWS credentials: {e}")
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "w") as fh:
        json.dump({"username": username, "password": password}, fh, indent=2)
    # Best-effort: keep the file readable only by the owner.
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


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
    if "filter_states" in overrides:
        settings.filter_states = [
            s.upper() for s in overrides["filter_states"]
        ]
        logger.info(
            f"Applied user override: {len(settings.filter_states)} filter states"
        )
    if "filter_counties" in overrides:
        raw = overrides["filter_counties"] or {}
        if isinstance(raw, dict):
            settings.filter_counties = {
                str(k).upper(): [str(c).upper() for c in (codes or [])]
                for k, codes in raw.items()
            }
            logger.info(
                f"Applied user override: county filter for {len(settings.filter_counties)} state(s)"
            )
    # General settings overrides
    _GENERAL_OVERRIDE_FIELDS = [
        "nexrad_enabled", "nexrad_default_site",
        "llm_enabled", "agent_enabled",
        "google_chat_enabled",
    ]
    for field in _GENERAL_OVERRIDE_FIELDS:
        if field in overrides:
            setattr(settings, field, overrides[field])
            logger.info(f"Applied user override: {field} = {overrides[field]}")
    # User-supplied NWWS-OI credentials override any .env/env values (in a
    # distributed build there is no .env, so this is the only source). Never log
    # the values.
    creds = load_nwws_credentials()
    if creds:
        settings.nwws_username = creds["username"]
        settings.nwws_password = creds["password"]
        logger.info("Applied user-supplied NWWS credentials")
    return settings


def reload_settings() -> Settings:
    """Reload settings (clears cache)."""
    get_settings.cache_clear()
    return get_settings()
