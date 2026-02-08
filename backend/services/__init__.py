"""Backend services for Alert Dashboard V2."""

from .alert_manager import (
    AlertManager,
    get_alert_manager,
    start_alert_manager,
    stop_alert_manager,
)
from .message_broker import (
    MessageBroker,
    MessageType,
    get_message_broker,
)
from .nws_api_client import (
    NWSAPIClient,
    NWSAPIError,
    get_nws_client,
    close_nws_client,
)
from .nwws_client import (
    NWWSClient,
    NWWSAlertHandler,
    get_nwws_handler,
    start_nwws_handler,
    stop_nwws_handler,
)
from .zone_geometry_service import (
    ZoneGeometryService,
    get_zone_geometry_service,
    start_zone_geometry_service,
    stop_zone_geometry_service,
)
from .ugc_service import (
    load_ugc_map,
    get_ugc_name,
    get_display_locations,
    get_county_names_list,
    is_ugc_map_loaded,
)
from .lsr_service import (
    LSRService,
    StormReport,
    get_lsr_service,
    start_lsr_service,
    stop_lsr_service,
    LSR_TYPE_COLORS,
)
from .odot_service import (
    ODOTService,
    ODOTCamera,
    RoadSensor,
    get_odot_service,
    start_odot_service,
    stop_odot_service,
)
from .spc_service import (
    SPCService,
    OutlookData,
    OutlookPolygon,
    MesoscaleDiscussion,
    RISK_COLORS,
    RISK_NAMES,
    get_spc_service,
    start_spc_service,
    stop_spc_service,
)
from .wind_gusts_service import (
    WindGustsService,
    WindGustReport,
    get_wind_gusts_service,
    start_wind_gusts_service,
    stop_wind_gusts_service,
    GUST_THRESHOLD_SIGNIFICANT,
    GUST_THRESHOLD_SEVERE,
    GUST_THRESHOLD_ADVISORY,
    DEFAULT_GUST_STATES,
)
from .llm_service import (
    LLMService,
    ChatMessage,
    LLMResponse,
    get_llm_service,
    start_llm_service,
    stop_llm_service,
    build_full_context,
)
from .google_chat_service import (
    GoogleChatService,
    get_google_chat_service,
    start_google_chat_service,
    stop_google_chat_service,
    send_alert_to_google_chat,
)

__all__ = [
    # Alert Manager
    "AlertManager",
    "get_alert_manager",
    "start_alert_manager",
    "stop_alert_manager",
    # Message Broker
    "MessageBroker",
    "MessageType",
    "get_message_broker",
    # NWS API Client
    "NWSAPIClient",
    "NWSAPIError",
    "get_nws_client",
    "close_nws_client",
    # NWWS Client
    "NWWSClient",
    "NWWSAlertHandler",
    "get_nwws_handler",
    "start_nwws_handler",
    "stop_nwws_handler",
    # Zone Geometry
    "ZoneGeometryService",
    "get_zone_geometry_service",
    "start_zone_geometry_service",
    "stop_zone_geometry_service",
    # UGC Service
    "load_ugc_map",
    "get_ugc_name",
    "get_display_locations",
    "get_county_names_list",
    "is_ugc_map_loaded",
    # LSR Service
    "LSRService",
    "StormReport",
    "get_lsr_service",
    "start_lsr_service",
    "stop_lsr_service",
    "LSR_TYPE_COLORS",
    # ODOT Service
    "ODOTService",
    "ODOTCamera",
    "RoadSensor",
    "get_odot_service",
    "start_odot_service",
    "stop_odot_service",
    # SPC Service
    "SPCService",
    "OutlookData",
    "OutlookPolygon",
    "MesoscaleDiscussion",
    "RISK_COLORS",
    "RISK_NAMES",
    "get_spc_service",
    "start_spc_service",
    "stop_spc_service",
    # LLM Service
    "LLMService",
    "ChatMessage",
    "LLMResponse",
    "get_llm_service",
    "start_llm_service",
    "stop_llm_service",
    "build_full_context",
    # Google Chat Service
    "GoogleChatService",
    "get_google_chat_service",
    "start_google_chat_service",
    "stop_google_chat_service",
    "send_alert_to_google_chat",
]
