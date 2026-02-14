"""
Main Application Entry Point for Alert Dashboard V2.

This module initializes and runs the FastAPI backend server, including:
- REST API endpoints for alerts and status
- WebSocket endpoint for real-time updates
- Service lifecycle management (startup/shutdown)
- Integration of all backend services
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Handle both direct execution and module execution
try:
    from .config import get_settings, Settings
    from .models.alert import Alert
    from .services import (
        get_alert_manager, start_alert_manager, stop_alert_manager,
        get_message_broker, MessageType,
        get_nws_client, close_nws_client,
        get_nwws_handler, start_nwws_handler, stop_nwws_handler,
        get_zone_geometry_service, start_zone_geometry_service, stop_zone_geometry_service,
        load_ugc_map,
        get_lsr_service, start_lsr_service, stop_lsr_service, LSR_TYPE_COLORS, StormReport,
        get_odot_service, start_odot_service, stop_odot_service,
        get_spc_service, start_spc_service, stop_spc_service, RISK_COLORS, RISK_NAMES,
        get_wind_gusts_service, start_wind_gusts_service, stop_wind_gusts_service,
        GUST_THRESHOLD_SIGNIFICANT, GUST_THRESHOLD_SEVERE, GUST_THRESHOLD_ADVISORY,
        DEFAULT_GUST_STATES,
        get_llm_service, start_llm_service, stop_llm_service, build_full_context,
        get_google_chat_service, start_google_chat_service, stop_google_chat_service,
        get_spotter_network_service, start_spotter_network_service, stop_spotter_network_service,
        get_chase_log_service,
        get_radar_service,
        get_nwws_products_service, start_nwws_products_service, stop_nwws_products_service,
    )
except ImportError:
    # Direct execution: python backend/main.py
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.config import get_settings, Settings
    from backend.models.alert import Alert
    from backend.services import (
        get_alert_manager, start_alert_manager, stop_alert_manager,
        get_message_broker, MessageType,
        get_nws_client, close_nws_client,
        get_nwws_handler, start_nwws_handler, stop_nwws_handler,
        get_zone_geometry_service, start_zone_geometry_service, stop_zone_geometry_service,
        load_ugc_map,
        get_lsr_service, start_lsr_service, stop_lsr_service, LSR_TYPE_COLORS, StormReport,
        get_odot_service, start_odot_service, stop_odot_service,
        get_spc_service, start_spc_service, stop_spc_service, RISK_COLORS, RISK_NAMES,
        get_wind_gusts_service, start_wind_gusts_service, stop_wind_gusts_service,
        GUST_THRESHOLD_SIGNIFICANT, GUST_THRESHOLD_SEVERE, GUST_THRESHOLD_ADVISORY,
        DEFAULT_GUST_STATES,
        get_llm_service, start_llm_service, stop_llm_service, build_full_context,
        get_google_chat_service, start_google_chat_service, stop_google_chat_service,
        get_spotter_network_service, start_spotter_network_service, stop_spotter_network_service,
        get_chase_log_service,
        get_radar_service,
        get_nwws_products_service, start_nwws_products_service, stop_nwws_products_service,
    )

logger = logging.getLogger(__name__)

# In-memory chaser position tracking
_chaser_positions: dict[str, dict] = {}


# =============================================================================
# Application Lifecycle
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.

    Handles startup and shutdown of all services.
    """
    logger.info("Starting Alert Dashboard V2...")

    # Startup
    await startup_services()

    yield

    # Shutdown
    await shutdown_services()
    logger.info("Alert Dashboard V2 stopped")


async def startup_services():
    """Initialize and start all backend services."""
    settings = get_settings()

    # 0. Load UGC map for county/zone name lookups
    if load_ugc_map():
        logger.info("UGC map loaded successfully")
    else:
        logger.warning("Failed to load UGC map - county names may not be available")

    # 1. Start Zone Geometry Service (for caching polygons)
    await start_zone_geometry_service()
    logger.info("Zone Geometry Service started")

    # 2. Start Alert Manager (loads persisted alerts)
    await start_alert_manager()
    logger.info("Alert Manager started")

    # 2b. Repopulate zone geometry for persisted alerts
    # This ensures zone-based alerts (watches) have complete geometry
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()
    persisted_alerts = alert_manager.get_all_alerts()
    if persisted_alerts:
        repopulated = await zone_service.populate_multiple_alerts(persisted_alerts)
        if repopulated > 0:
            logger.info(f"Repopulated zone geometry for {repopulated} persisted alerts")
            # Save updated alerts back to file
            alert_manager.save_to_file()

    # 3. Wire up Alert Manager callbacks to Message Broker
    wire_alert_callbacks()

    # 3b. Register chaser tracking handler
    register_chaser_handler()

    # 4. Fetch initial alerts from NWS API
    await fetch_initial_alerts()

    # 4b. Mark Google Chat startup complete - now only truly NEW alerts will trigger notifications
    google_chat_service = get_google_chat_service()
    google_chat_service.mark_startup_complete()

    # 5. Start NWWS handler for real-time alerts (if configured)
    if settings.nwws_username and settings.nwws_password:
        await start_nwws_handler()

        # Wire NWWS alerts to Alert Manager with zone geometry population
        nwws_handler = get_nwws_handler()
        alert_manager = get_alert_manager()
        zone_service = get_zone_geometry_service()

        def on_nwws_alert(alert: Alert):
            """Handle NWWS alert: populate geometry then add to manager."""
            async def process_alert():
                # Always try to populate zone geometry for alerts with affected areas
                # The populate_alert_geometry method handles the logic of when to
                # actually populate (always for zone-based alerts like watches)
                if alert.affected_areas:
                    await zone_service.populate_alert_geometry(alert)
                # Add to alert manager
                alert_manager.add_alert(alert)

            # Run async task
            asyncio.create_task(process_alert())

        nwws_handler.add_alert_callback(on_nwws_alert)

        # Wire NWWS products service to capture ALL raw products (monitoring + AFD)
        await start_nwws_products_service()
        products_service = get_nwws_products_service()
        nwws_handler.add_raw_callback(products_service.on_raw_product)
        logger.info("NWWS Products service wired to raw callback")

        logger.info("NWWS Handler started")
    else:
        logger.warning("NWWS credentials not configured - using API-only mode")
        await start_nwws_products_service()  # Start anyway for API fallback

    # 6. Start periodic API polling (backup to NWWS)
    asyncio.create_task(api_polling_loop())

    # 7. Start LSR Service
    await start_lsr_service()
    logger.info("LSR Service started")

    # 8. Start ODOT Service
    await start_odot_service()
    logger.info("ODOT Service started")

    # 9. Start SPC Service
    await start_spc_service()
    logger.info("SPC Service started")

    # 10. Start Wind Gusts Service
    await start_wind_gusts_service()
    logger.info("Wind Gusts Service started")

    # 11. Start LLM Service (optional - may not be available)
    llm_available = await start_llm_service()
    if llm_available:
        logger.info("LLM Service started and available")
    else:
        logger.warning("LLM Service not available - Ollama may not be running")

    # 12. Start Google Chat Service (optional - disabled by default)
    google_chat_enabled = await start_google_chat_service()
    if google_chat_enabled:
        logger.info("Google Chat notification service started")
    else:
        logger.info("Google Chat notifications disabled or not configured")

    # 13. Start Spotter Network Service (optional)
    if settings.spotter_network_enabled:
        sn_started = await start_spotter_network_service()
        if sn_started:
            logger.info("Spotter Network service started")
        else:
            logger.warning("Spotter Network service failed to start")
    else:
        logger.info("Spotter Network integration disabled")

    logger.info("All services started successfully")


async def shutdown_services():
    """Gracefully shutdown all services."""
    logger.info("Shutting down services...")

    # Stop in reverse order
    await stop_spotter_network_service()
    await stop_google_chat_service()
    await stop_llm_service()
    await stop_wind_gusts_service()
    await stop_spc_service()
    await stop_odot_service()
    await stop_lsr_service()
    await stop_nwws_products_service()
    await stop_nwws_handler()
    await stop_alert_manager()
    await stop_zone_geometry_service()
    await close_nws_client()

    logger.info("All services stopped")


def wire_alert_callbacks():
    """Connect Alert Manager events to WebSocket broadcasts and notifications."""
    alert_manager = get_alert_manager()
    broker = get_message_broker()
    google_chat_service = get_google_chat_service()

    async def on_alert_added(alert: Alert):
        # Broadcast to WebSocket clients
        await broker.broadcast_alert_new(alert)
        # Send Google Chat notification (only for new alerts, not updates)
        await google_chat_service.notify_new_alert(alert)

    async def on_alert_updated(alert: Alert):
        await broker.broadcast_alert_update(alert)

    async def on_alert_removed(alert: Alert):
        await broker.broadcast_alert_remove(alert)

    # Wrap async callbacks for sync AlertManager
    def sync_added(alert: Alert):
        asyncio.create_task(on_alert_added(alert))

    def sync_updated(alert: Alert):
        asyncio.create_task(on_alert_updated(alert))

    def sync_removed(alert: Alert):
        asyncio.create_task(on_alert_removed(alert))

    alert_manager.on_alert_added(sync_added)
    alert_manager.on_alert_updated(sync_updated)
    alert_manager.on_alert_removed(sync_removed)

    logger.info("Alert callbacks wired to message broker")


def register_chaser_handler():
    """Register WebSocket handler for chaser position updates."""
    broker = get_message_broker()

    async def handle_chaser_position(connection, data: dict):
        """Handle incoming chaser GPS position."""
        client_id = connection.client_id
        position = {
            "client_id": client_id,
            "name": data.get("name", "Chaser"),
            "lat": data.get("lat"),
            "lon": data.get("lon"),
            "heading": data.get("heading"),
            "speed": data.get("speed"),
            "accuracy": data.get("accuracy"),
            "last_update": datetime.now(timezone.utc).isoformat(),
        }
        _chaser_positions[client_id] = position
        # Broadcast to all connected clients
        await broker._broadcast(MessageType.CHASER_POSITION, position)

        # Log to chase log
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is not None and lon is not None:
            chase_log = get_chase_log_service()
            if not chase_log.active_session:
                chase_log.start_session(data.get("name", "Chaser"))
            chase_log.log_waypoint(
                lat=lat,
                lon=lon,
                speed=data.get("speed"),
                heading=data.get("heading"),
            )

            # Server-side polygon detection + radar snapshot trigger
            asyncio.create_task(_check_polygon_and_capture(
                lat, lon, data.get("name", "Chaser"), chase_log
            ))

    broker.register_handler(MessageType.CHASER_POSITION_UPDATE, handle_chaser_position)
    logger.info("Chaser position handler registered")


async def _check_polygon_and_capture(
    lat: float, lon: float, chaser_name: str, chase_log
):
    """Check if chaser is inside any warning polygon and capture radar if so."""
    try:
        from shapely.geometry import Point, Polygon as ShapelyPolygon

        point = Point(lon, lat)  # Shapely uses (x, y) = (lon, lat)
        alert_manager = get_alert_manager()
        alerts = alert_manager.get_active_alerts()

        for alert in alerts:
            if not alert.polygon or len(alert.polygon) < 3:
                continue
            try:
                # Alert polygons are [[lat, lon], ...] — convert to [(lon, lat), ...]
                ring = [(coord[1], coord[0]) for coord in alert.polygon]
                poly = ShapelyPolygon(ring)
                if poly.contains(point):
                    # Chaser is inside this warning polygon — capture radar
                    radar = get_radar_service()
                    filename = await radar.capture_radar_snapshot(
                        lat, lon, label=chaser_name
                    )
                    if filename:
                        chase_log.log_event("radar_snapshot", {
                            "file": filename,
                            "alert": f"{alert.phenomenon} {alert.significance}",
                            "event": alert.event_type or alert.headline,
                        })
                        chase_log.log_event("entered_polygon", {
                            "alert": alert.headline or f"{alert.event_type}",
                            "id": alert.id,
                        })
                        logger.info(
                            f"Radar snapshot triggered: {chaser_name} inside "
                            f"{alert.event_type or alert.headline}"
                        )
                    break  # One capture per position update is enough
            except Exception as e:
                logger.debug(f"Polygon check error for alert {alert.id}: {e}")
                continue
    except ImportError:
        logger.warning("Shapely not installed - polygon detection disabled")
    except Exception as e:
        logger.error(f"Polygon detection error: {e}")


async def fetch_initial_alerts():
    """Fetch current alerts from NWS API on startup."""
    settings = get_settings()

    try:
        client = get_nws_client()
        alerts = await client.fetch_and_parse_alerts(states=settings.filter_states)

        # Populate zone geometry for alerts without polygons
        zone_service = get_zone_geometry_service()
        await zone_service.populate_multiple_alerts(alerts)

        # Add to alert manager
        alert_manager = get_alert_manager()
        added = 0
        for alert in alerts:
            if alert_manager.add_alert(alert):
                added += 1

        logger.info(f"Loaded {added} initial alerts from NWS API")

    except Exception as e:
        logger.error(f"Failed to fetch initial alerts: {e}")


async def api_polling_loop():
    """Background task to periodically poll NWS API for alerts."""
    settings = get_settings()
    interval = settings.api_poll_interval_seconds

    logger.info(f"Starting API polling loop (interval: {interval}s)")

    while True:
        try:
            await asyncio.sleep(interval)
            await fetch_initial_alerts()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in API polling loop: {e}")


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="Alert Dashboard V2",
    description="NWS Weather Alert Dashboard Backend",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# Static Files (Frontend)
# =============================================================================

# Path to the frontend build directory
FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"

# Path to the widgets directory
WIDGETS_DIR = Path(__file__).parent.parent / "widgets"

# Mount static files if the build directory exists
if FRONTEND_DIR.exists():
    # Serve static assets (js, css, images) from /assets
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    logger.info(f"Serving frontend static files from {FRONTEND_DIR}")

# Mount widgets directory for streaming widgets
if WIDGETS_DIR.exists():
    app.mount("/widgets", StaticFiles(directory=WIDGETS_DIR, html=True), name="widgets")
    logger.info(f"Serving widgets from {WIDGETS_DIR}")


# =============================================================================
# REST API Endpoints
# =============================================================================

@app.get("/")
async def root():
    """Root endpoint - serves frontend or API info."""
    # If frontend build exists, serve index.html
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    # Otherwise return API info
    return {
        "name": "Alert Dashboard V2",
        "version": "2.0.0",
        "status": "running",
    }


@app.get("/index.html")
async def serve_index():
    """Serve the frontend index.html."""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Frontend not built. Run 'npm run build' in frontend directory.")


@app.get("/tbf_logo.png")
async def serve_logo():
    """Serve the TBF logo."""
    logo_path = FRONTEND_DIR / "tbf_logo.png"
    if logo_path.exists():
        return FileResponse(logo_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")


@app.get("/favicon.ico")
async def serve_favicon():
    """Serve favicon."""
    favicon_path = FRONTEND_DIR / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path)
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    alert_manager = get_alert_manager()
    broker = get_message_broker()
    nwws_handler = get_nwws_handler()

    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "alert_manager": {
                "active_alerts": alert_manager.alert_count,
            },
            "websocket": {
                "connected_clients": broker.connection_count,
            },
            "nwws": {
                "connected": nwws_handler.is_connected if nwws_handler else False,
            },
        },
    }


@app.get("/api/alerts")
async def get_alerts(
    state: Optional[str] = Query(None, description="Filter by state code (e.g., OH)"),
    phenomenon: Optional[str] = Query(None, description="Filter by phenomenon code (e.g., TO)"),
    priority: bool = Query(True, description="Sort by priority"),
):
    """
    Get all active alerts.

    Returns list of active weather alerts, optionally filtered.
    """
    alert_manager = get_alert_manager()

    if state:
        alerts = alert_manager.get_alerts_by_state(state)
    elif phenomenon:
        alerts = alert_manager.get_alerts_by_phenomenon(phenomenon)
    else:
        alerts = alert_manager.get_alerts_sorted(by_priority=priority)

    return {
        "count": len(alerts),
        "alerts": [alert.to_dict() for alert in alerts],
    }


@app.get("/api/alerts/{product_id}")
async def get_alert(product_id: str):
    """Get a specific alert by product ID."""
    alert_manager = get_alert_manager()
    alert = alert_manager.get_alert(product_id)

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return alert.to_dict()


@app.delete("/api/alerts/{product_id}")
async def clear_alert_manual(product_id: str):
    """Manually clear an alert by product ID."""
    alert_manager = get_alert_manager()

    if alert_manager.remove_alert(product_id, reason="MANUAL"):
        return {"success": True, "message": f"Alert {product_id} cleared manually"}
    
    raise HTTPException(status_code=404, detail="Alert not found or already removed")


@app.get("/api/stats")
async def get_stats():
    """Get alert statistics."""
    alert_manager = get_alert_manager()
    return alert_manager.get_statistics()


@app.get("/api/map/zones")
async def get_map_zones():
    """
    Get zone-based map data for rendering.

    Returns individual zone geometries with their highest priority alert,
    allowing the map to render each zone only once with the correct color.
    """
    from .services.zone_geometry_service import get_zone_geometry_service

    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()
    alerts = alert_manager.get_alerts_sorted(by_priority=True)

    # Polygon-based phenomena that should NOT render zone fills
    # These are storm-based warnings that use polygon geometry instead of county/zone fills
    POLYGON_ALERT_PHENOMENA = {'TO', 'SV', 'FF', 'SQ', 'SPS'}

    # Priority for significance (lower = higher priority)
    SIGNIFICANCE_PRIORITY = {
        'W': 1,  # Warning
        'A': 2,  # Watch
        'Y': 3,  # Advisory
        'S': 4,  # Statement
    }

    # Phenomenon priority within same significance
    PHENOMENON_PRIORITY = {
        'TO': 1, 'SV': 2, 'FF': 3, 'SQ': 4, 'EW': 5, 'BZ': 6,
        'IS': 7, 'WS': 8, 'HW': 9, 'LE': 10, 'WC': 11,
        'FL': 12, 'WW': 20, 'WI': 21, 'FG': 22,
    }

    def get_alert_priority(alert):
        sig = alert.significance.value if hasattr(alert.significance, 'value') else alert.significance
        sig_priority = SIGNIFICANCE_PRIORITY.get(sig, 99)
        phen_priority = PHENOMENON_PRIORITY.get(alert.phenomenon, 50)
        return sig_priority * 100 + phen_priority

    # Build zone -> alert mapping (highest priority wins)
    zone_to_alert: dict[str, dict] = {}

    for alert in alerts:
        if not alert.affected_areas:
            continue

        # Skip polygon-based alerts that have actual polygons - they render as storm polygons only
        if alert.phenomenon in POLYGON_ALERT_PHENOMENA and alert.polygon:
            continue

        priority = get_alert_priority(alert)
        alert_dict = alert.to_dict()

        for zone_id in alert.affected_areas:
            # Check if this alert beats the current winner
            if zone_id not in zone_to_alert or priority < zone_to_alert[zone_id]['priority']:
                zone_to_alert[zone_id] = {
                    'priority': priority,
                    'alert': alert_dict,
                }

    # Collect unique zones and fetch geometries
    zone_ids = list(zone_to_alert.keys())

    if not zone_ids:
        return {"zones": [], "alert_types": []}

    # Fetch all zone geometries
    geometries = await zone_service.fetch_multiple_zones(zone_ids)

    # Build response with zones that have geometry
    zones = []
    for zone_id in zone_ids:
        geometry = geometries.get(zone_id)
        if geometry and zone_id in zone_to_alert:
            alert_info = zone_to_alert[zone_id]['alert']
            zones.append({
                'zone_id': zone_id,
                'geometry': geometry,
                'alert': {
                    'product_id': alert_info['product_id'],
                    'phenomenon': alert_info['phenomenon'],
                    'significance': alert_info['significance'],
                    'event_name': alert_info['event_name'],
                    'headline': alert_info.get('headline'),
                    'expiration_time': alert_info.get('expiration_time'),
                    'sender_office': alert_info.get('sender_office'),
                    'display_locations': alert_info.get('display_locations'),
                },
            })

    # Get unique alert types for filter buttons
    alert_types = {}
    for alert in alerts:
        key = alert.phenomenon
        if key not in alert_types:
            sig = alert.significance.value if hasattr(alert.significance, 'value') else alert.significance
            alert_types[key] = {
                'phenomenon': key,
                'significance': sig,
                'event_name': alert.event_name,
                'count': 1,
            }
        else:
            alert_types[key]['count'] += 1

    return {
        "zones": zones,
        "alert_types": list(alert_types.values()),
        "total_zones": len(zones),
    }


@app.get("/api/recent")
async def get_recent_products(limit: int = Query(20, ge=1, le=100)):
    """Get list of recently received products."""
    alert_manager = get_alert_manager()
    return {
        "products": alert_manager.get_recent_products(limit=limit),
    }


@app.get("/api/status")
async def get_system_status():
    """Get detailed system status."""
    settings = get_settings()
    alert_manager = get_alert_manager()
    broker = get_message_broker()
    nwws_handler = get_nwws_handler()
    zone_service = get_zone_geometry_service()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": "production" if not settings.debug else "development",
        "services": {
            "alerts": {
                "total": alert_manager.alert_count,
                "statistics": alert_manager.get_statistics(),
            },
            "websocket": {
                "clients": broker.connection_count,
                "client_ids": broker.get_all_client_ids(),
            },
            "nwws": {
                "enabled": bool(settings.nwws_username),
                "connected": nwws_handler.is_connected if nwws_handler else False,
            },
            "zone_cache": zone_service.get_cache_stats(),
        },
        "config": {
            "filter_states": settings.filter_states,
            "api_poll_interval": settings.api_poll_interval_seconds,
        },
    }


# =============================================================================
# LSR (Local Storm Reports) Endpoints
# =============================================================================

@app.get("/api/lsr")
async def get_storm_reports(
    hours: int = Query(24, ge=1, le=168, description="Lookback period in hours"),
    report_type: Optional[str] = Query(None, description="Filter by report type"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get Local Storm Reports from Iowa State Mesonet.

    Returns tornado, hail, wind, flood, and other severe weather reports.
    """
    lsr_service = get_lsr_service()
    settings = get_settings()

    # Fetch reports
    reports = await lsr_service.fetch_reports(
        states=settings.filter_states,
        hours=hours,
        force_refresh=refresh,
    )

    # Filter by type if specified
    if report_type:
        reports = [r for r in reports if r.report_type.upper() == report_type.upper()]

    return {
        "count": len(reports),
        "reports": [r.to_dict() for r in reports],
        "type_colors": LSR_TYPE_COLORS,
    }


@app.get("/api/lsr/stats")
async def get_lsr_stats():
    """Get LSR statistics."""
    lsr_service = get_lsr_service()
    return lsr_service.get_statistics()


@app.get("/api/lsr/types")
async def get_lsr_types():
    """Get available LSR types and their colors."""
    return {
        "types": list(LSR_TYPE_COLORS.keys()),
        "colors": LSR_TYPE_COLORS,
    }


# =============================================================================
# Viewer Report Models
# =============================================================================

class ViewerReportSubmission(BaseModel):
    """Model for viewer report submission from dashboard."""
    report_type: str = Field(..., description="Report type (TORNADO, HAIL, etc.)")
    lat: float = Field(..., description="Latitude")
    lon: float = Field(..., description="Longitude")
    magnitude: Optional[str] = Field(None, description="Magnitude (e.g., '1.00 INCH', '65 MPH')")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    location: Optional[str] = Field(None, description="Human-readable location")
    submitter: Optional[str] = Field("Anonymous", description="Submitter name")


class WebsiteReportSubmission(BaseModel):
    """Model for storm report submission from website."""
    type: str = Field(..., description="Report type")
    location: str = Field(..., description="Location description")
    magnitude: Optional[str] = Field(None, description="Magnitude")
    datetime: Optional[str] = Field(None, description="Report datetime (ISO format)")
    latitude: float = Field(..., description="Latitude")
    longitude: float = Field(..., description="Longitude")
    notes: Optional[str] = Field(None, description="Additional notes")
    name: Optional[str] = Field("Anonymous", description="Submitter name")
    recaptcha: Optional[str] = Field(None, description="reCAPTCHA response token")


# =============================================================================
# Viewer Report Endpoints
# =============================================================================

@app.get("/api/lsr/all")
async def get_all_storm_reports(
    hours: int = Query(24, ge=1, le=168, description="Lookback period in hours"),
    report_type: Optional[str] = Query(None, description="Filter by report type"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all storm reports (official + viewer).

    Official reports are filtered by state settings, viewer reports are always included.
    """
    lsr_service = get_lsr_service()
    settings = get_settings()

    # Fetch official reports (filtered by state)
    await lsr_service.fetch_reports(
        states=settings.filter_states,
        hours=hours,
        force_refresh=refresh,
    )

    # Get all reports (official filtered by state + all viewer reports)
    reports = lsr_service.get_all_reports(states=settings.filter_states)

    # Filter by type if specified
    if report_type:
        reports = [r for r in reports if r.report_type.upper() == report_type.upper()]

    return {
        "count": len(reports),
        "reports": [r.to_dict() for r in reports],
        "viewer_count": sum(1 for r in reports if r.is_viewer),
        "type_colors": LSR_TYPE_COLORS,
    }


@app.get("/api/lsr/viewer")
async def get_viewer_reports():
    """Get all viewer-submitted reports."""
    lsr_service = get_lsr_service()
    reports = lsr_service.get_manual_reports()

    return {
        "count": len(reports),
        "reports": [r.to_dict() for r in reports],
    }


@app.post("/api/lsr/viewer")
async def submit_viewer_report(report: ViewerReportSubmission):
    """
    Submit a storm report from the dashboard.

    Used for manual report entry by dashboard users.
    """
    lsr_service = get_lsr_service()

    # Normalize report type
    report_type = report.report_type.upper()

    # Create StormReport
    storm_report = StormReport(
        id=f"viewer_{uuid.uuid4().hex[:12]}",
        report_type=report_type,
        magnitude=report.magnitude,
        lat=report.lat,
        lon=report.lon,
        valid_time=datetime.now(timezone.utc).isoformat(),
        remark=report.remarks or "",
        location_text=report.location or "",
        submitter=report.submitter or "Anonymous",
        is_viewer=True,
        source="VIEWER",
    )

    lsr_service.add_manual_report(storm_report)

    # Broadcast to WebSocket clients
    broker = get_message_broker()
    await broker.broadcast(
        MessageType.SYSTEM_STATUS,
        {
            "event": "viewer_report_added",
            "report": storm_report.to_dict(),
        }
    )

    return {
        "success": True,
        "report": storm_report.to_dict(),
    }


@app.delete("/api/lsr/viewer/{report_id}")
async def remove_viewer_report(report_id: str):
    """Remove a viewer-submitted report by ID."""
    lsr_service = get_lsr_service()

    if lsr_service.remove_manual_report(report_id):
        # Broadcast removal to WebSocket clients
        broker = get_message_broker()
        await broker.broadcast(
            MessageType.SYSTEM_STATUS,
            {
                "event": "viewer_report_removed",
                "report_id": report_id,
            }
        )
        return {"success": True, "message": f"Report {report_id} removed"}

    raise HTTPException(status_code=404, detail="Viewer report not found")


@app.delete("/api/lsr/viewer")
async def clear_viewer_reports():
    """Clear all viewer-submitted reports."""
    lsr_service = get_lsr_service()
    lsr_service.clear_manual_reports()

    # Broadcast to WebSocket clients
    broker = get_message_broker()
    await broker.broadcast(
        MessageType.SYSTEM_STATUS,
        {
            "event": "viewer_reports_cleared",
        }
    )

    return {"success": True, "message": "All viewer reports cleared"}


@app.post("/api/submit_storm_report")
async def submit_website_report(report: WebsiteReportSubmission, request: Request):
    """
    Submit a storm report from the public website (belparkmedia.com).

    This endpoint accepts reports from the public submission form.
    Includes optional reCAPTCHA validation.
    """
    settings = get_settings()
    lsr_service = get_lsr_service()

    # Validate reCAPTCHA if configured
    recaptcha_secret = getattr(settings, 'recaptcha_secret_key', None)
    if recaptcha_secret and report.recaptcha:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://www.google.com/recaptcha/api/siteverify",
                    data={
                        "secret": recaptcha_secret,
                        "response": report.recaptcha,
                        "remoteip": request.client.host if request.client else None,
                    }
                ) as resp:
                    result = await resp.json()
                    if not result.get("success"):
                        raise HTTPException(status_code=400, detail="reCAPTCHA validation failed")
        except aiohttp.ClientError as e:
            logger.warning(f"reCAPTCHA verification error: {e}")
            # Continue anyway if verification service is down

    # Normalize report type
    type_mapping = {
        "TORNADO": "TORNADO",
        "HAIL": "HAIL",
        "WIND": "TSTM WND GST",
        "FLOODING": "FLASH FLOOD",
        "SNOW": "SNOW",
        "WINTER": "SNOW",
        "TROPICAL": "TROPICAL",
        "OTHER": "OTHER",
    }
    report_type = type_mapping.get(report.type.upper(), report.type.upper())

    # Parse datetime
    valid_time = datetime.now(timezone.utc).isoformat()
    if report.datetime:
        try:
            # Try parsing ISO format
            parsed = datetime.fromisoformat(report.datetime.replace('Z', '+00:00'))
            valid_time = parsed.isoformat()
        except ValueError:
            pass

    # Create StormReport
    storm_report = StormReport(
        id=f"website_{uuid.uuid4().hex[:12]}",
        report_type=report_type,
        magnitude=report.magnitude,
        lat=report.latitude,
        lon=report.longitude,
        valid_time=valid_time,
        remark=report.notes or "",
        location_text=report.location,
        submitter=report.name or "Anonymous",
        is_viewer=True,
        source="VIEWER",
    )

    lsr_service.add_manual_report(storm_report)

    # Broadcast to WebSocket clients
    broker = get_message_broker()
    await broker.broadcast(
        MessageType.SYSTEM_STATUS,
        {
            "event": "viewer_report_added",
            "report": storm_report.to_dict(),
            "source": "website",
        }
    )

    logger.info(f"Website storm report submitted: {report_type} at {report.location}")

    return {
        "success": True,
        "message": "Storm report submitted successfully",
        "report_id": storm_report.id,
    }


# =============================================================================
# ODOT (Ohio DOT) Endpoints
# =============================================================================

@app.get("/api/odot/cameras")
async def get_odot_cameras(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all ODOT traffic cameras.

    Returns camera locations with live image URLs.
    """
    odot_service = get_odot_service()
    cameras = await odot_service.fetch_cameras(force_refresh=refresh)

    return {
        "count": len(cameras),
        "cameras": [c.to_dict() for c in cameras],
    }


@app.get("/api/odot/sensors")
async def get_odot_sensors(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all ODOT road weather sensors.

    Returns sensor data including pavement and air temperatures.
    """
    odot_service = get_odot_service()
    sensors = await odot_service.fetch_sensors(force_refresh=refresh)

    return {
        "count": len(sensors),
        "sensors": [s.to_dict() for s in sensors],
    }


@app.get("/api/odot/cold-sensors")
async def get_cold_sensors(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get sensors with cold pavement (below threshold).

    Returns sensors sorted by temperature (coldest first).
    """
    settings = get_settings()
    odot_service = get_odot_service()

    # Ensure we have fresh data
    await odot_service.fetch_sensors(force_refresh=refresh)

    cold_sensors = odot_service.get_cold_sensors()
    freezing_sensors = odot_service.get_freezing_sensors()

    # Sort by pavement temperature (coldest first)
    cold_sensors.sort(key=lambda s: s.pavement_temp if s.pavement_temp is not None else 100)

    return {
        "count": len(cold_sensors),
        "freezing_count": len(freezing_sensors),
        "cold_threshold": settings.cold_pavement_threshold,
        "freezing_threshold": settings.freezing_pavement_threshold,
        "sensors": [s.to_dict() for s in cold_sensors],
    }


@app.get("/api/odot/cameras-in-alerts")
async def get_cameras_in_alerts(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get cameras that are inside active weather alert polygons.

    Only checks alerts matching the configured camera_alert_phenomena.
    """
    settings = get_settings()
    odot_service = get_odot_service()
    alert_manager = get_alert_manager()

    # Ensure we have fresh camera data
    await odot_service.fetch_cameras(force_refresh=refresh)

    # Get all active alerts with polygons
    alerts = alert_manager.get_alerts_sorted()
    alert_dicts = [a.to_dict() for a in alerts]

    # Find cameras in alerts
    cameras_in_alerts = odot_service.find_cameras_in_alerts(
        alert_dicts,
        phenomena_filter=settings.camera_alert_phenomena
    )

    return {
        "count": len(cameras_in_alerts),
        "phenomena_filter": settings.camera_alert_phenomena,
        "cameras": [c.to_dict() for c in cameras_in_alerts],
    }


@app.get("/api/odot/stats")
async def get_odot_stats():
    """Get ODOT service statistics."""
    odot_service = get_odot_service()
    return odot_service.get_statistics()


# =============================================================================
# SPC (Storm Prediction Center) Endpoints
# =============================================================================

@app.get("/api/spc/outlooks")
async def get_spc_outlooks(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get all SPC convective outlooks (Day 1-3).

    Returns categorical outlooks with risk level polygons.
    """
    spc_service = get_spc_service()

    if refresh:
        await spc_service.fetch_all_outlooks(force_refresh=True)
    else:
        # Fetch day1 categorical if not cached
        await spc_service.fetch_outlook("day1_categorical")

    outlooks = {}
    for key in ["day1_categorical", "day2_categorical", "day3_categorical"]:
        outlook = spc_service._outlooks.get(key)
        if outlook:
            outlooks[key] = outlook.to_dict()

    return {
        "outlooks": outlooks,
        "risk_colors": RISK_COLORS,
        "risk_names": RISK_NAMES,
    }


@app.get("/api/spc/outlook/{outlook_key}")
async def get_spc_outlook(
    outlook_key: str,
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get a specific SPC outlook.

    Valid outlook_key values:
    - day1_categorical, day2_categorical, day3_categorical
    - day1_tornado, day1_wind, day1_hail
    """
    spc_service = get_spc_service()
    outlook = await spc_service.fetch_outlook(outlook_key, force_refresh=refresh)

    if not outlook:
        raise HTTPException(status_code=404, detail=f"Outlook '{outlook_key}' not found or unavailable")

    return {
        "outlook": outlook.to_dict(),
        "risk_colors": RISK_COLORS,
        "risk_names": RISK_NAMES,
    }


@app.get("/api/spc/day1")
async def get_spc_day1(
    include_probabilities: bool = Query(False, description="Include probabilistic outlooks"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get Day 1 SPC outlooks.

    Returns categorical outlook and optionally tornado/wind/hail probabilities.
    """
    spc_service = get_spc_service()

    # Always fetch categorical
    categorical = await spc_service.fetch_outlook("day1_categorical", force_refresh=refresh)

    result = {
        "categorical": categorical.to_dict() if categorical else None,
        "risk_colors": RISK_COLORS,
        "risk_names": RISK_NAMES,
    }

    if include_probabilities:
        tornado = await spc_service.fetch_outlook("day1_tornado", force_refresh=refresh)
        wind = await spc_service.fetch_outlook("day1_wind", force_refresh=refresh)
        hail = await spc_service.fetch_outlook("day1_hail", force_refresh=refresh)

        result["tornado"] = tornado.to_dict() if tornado else None
        result["wind"] = wind.to_dict() if wind else None
        result["hail"] = hail.to_dict() if hail else None

    return result


@app.get("/api/spc/mesoscale-discussions")
async def get_mesoscale_discussions(
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get current SPC Mesoscale Discussions.

    Mesoscale discussions are filtered to states matching filter_states setting.
    """
    settings = get_settings()
    spc_service = get_spc_service()

    mds = await spc_service.fetch_mesoscale_discussions(force_refresh=refresh)

    # Filter by configured states
    filtered_mds = spc_service.filter_mds_by_states(mds, settings.filter_states)

    return {
        "count": len(filtered_mds),
        "total_count": len(mds),
        "filter_states": settings.filter_states,
        "discussions": [md.to_dict() for md in filtered_mds],
    }


@app.get("/api/spc/state-images")
async def get_spc_state_images(
    day: int = Query(1, ge=1, le=3, description="Outlook day (1-3)"),
):
    """
    Get state-specific SPC outlook image URLs.

    Returns image URLs for each state in filter_states setting.
    """
    settings = get_settings()
    spc_service = get_spc_service()

    state_images = spc_service.get_state_outlook_urls(settings.filter_states, day=day)

    return {
        "day": day,
        "states": settings.filter_states,
        "images": state_images,
    }


@app.get("/api/spc/risk-at-point")
async def get_risk_at_point(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    outlook_key: str = Query("day1_categorical", description="Which outlook to check"),
):
    """
    Get the highest risk level at a specific point.

    Useful for checking what risk level affects a specific location.
    """
    spc_service = get_spc_service()

    # Ensure we have the outlook data
    await spc_service.fetch_outlook(outlook_key)

    risk = spc_service.get_highest_risk_for_point(lat, lon, outlook_key)

    if not risk:
        return {
            "lat": lat,
            "lon": lon,
            "outlook_key": outlook_key,
            "risk": None,
            "message": "No risk at this location",
        }

    return {
        "lat": lat,
        "lon": lon,
        "outlook_key": outlook_key,
        "risk": risk.to_dict(),
    }


@app.get("/api/spc/discussion")
async def get_spc_discussion(
    day: int = Query(1, ge=1, le=3, description="Outlook day (1-3)"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get SPC outlook discussion text.

    Returns the official SPC discussion text for the specified day.
    """
    spc_service = get_spc_service()
    text = await spc_service.fetch_discussion(day=day, force_refresh=refresh)

    if not text:
        raise HTTPException(status_code=404, detail=f"Day {day} discussion not available")

    return {
        "day": day,
        "text": text,
        "url": f"https://www.spc.noaa.gov/products/outlook/day{day}otlk.html",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/spc/stats")
async def get_spc_stats():
    """Get SPC service statistics."""
    spc_service = get_spc_service()
    return spc_service.get_statistics()


# =============================================================================
# Wind Gusts Endpoints
# =============================================================================

@app.get("/api/wind-gusts")
async def get_wind_gusts(
    hours: int = Query(1, ge=1, le=24, description="Lookback period in hours"),
    limit: int = Query(15, ge=1, le=100, description="Maximum number of results"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get top wind gust observations from ASOS stations.

    Returns wind gusts from Iowa State Mesonet for configured filter_states.
    """
    settings = get_settings()
    wind_service = get_wind_gusts_service()

    # Use filter_states or defaults if empty
    states_to_use = settings.filter_states if settings.filter_states else DEFAULT_GUST_STATES

    gusts = await wind_service.fetch_gusts(
        states=states_to_use,
        hours=hours,
        limit=limit,
        force_refresh=refresh,
    )

    # Group by state for frontend display
    gusts_by_state = wind_service.get_gusts_by_state(gusts)

    return {
        "count": len(gusts),
        "filter_states": states_to_use,
        "thresholds": {
            "significant": GUST_THRESHOLD_SIGNIFICANT,
            "severe": GUST_THRESHOLD_SEVERE,
            "advisory": GUST_THRESHOLD_ADVISORY,
        },
        "gusts": [g.to_dict() for g in gusts],
        "by_state": {
            state: [g.to_dict() for g in state_gusts]
            for state, state_gusts in gusts_by_state.items()
        },
    }


@app.get("/api/wind-gusts/by-state")
async def get_wind_gusts_by_state(
    hours: int = Query(1, ge=1, le=24, description="Lookback period in hours"),
    limit_per_state: int = Query(5, ge=1, le=50, description="Maximum results per state"),
    refresh: bool = Query(False, description="Force refresh from API"),
):
    """
    Get wind gusts organized by state.

    Returns gusts grouped by state with a per-state limit.
    """
    settings = get_settings()
    wind_service = get_wind_gusts_service()

    # Use filter_states or defaults if empty
    states_to_use = settings.filter_states if settings.filter_states else DEFAULT_GUST_STATES

    # Fetch all gusts (higher limit to allow per-state filtering)
    gusts = await wind_service.fetch_gusts(
        states=states_to_use,
        hours=hours,
        limit=100,
        force_refresh=refresh,
    )

    # Group by state and limit each
    gusts_by_state = wind_service.get_gusts_by_state(gusts)
    result = {}
    total = 0

    for state in states_to_use:
        if state in gusts_by_state:
            state_gusts = gusts_by_state[state][:limit_per_state]
            result[state] = [g.to_dict() for g in state_gusts]
            total += len(state_gusts)
        else:
            result[state] = []

    return {
        "count": total,
        "filter_states": states_to_use,
        "thresholds": {
            "significant": GUST_THRESHOLD_SIGNIFICANT,
            "severe": GUST_THRESHOLD_SEVERE,
            "advisory": GUST_THRESHOLD_ADVISORY,
        },
        "by_state": result,
    }


@app.get("/api/wind-gusts/stats")
async def get_wind_gusts_stats():
    """Get wind gusts service statistics."""
    wind_service = get_wind_gusts_service()
    return wind_service.get_statistics()


# =============================================================================
# NWWS Products Feed Endpoints
# =============================================================================

@app.get("/api/nwws/products")
async def get_nwws_products_feed(
    limit: int = Query(50, ge=1, le=500, description="Number of products to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    product_type: Optional[str] = Query(None, description="Filter by product type (e.g., SVS, FFW, AFD)"),
    office: Optional[str] = Query(None, description="Filter by office code (e.g., CLE)"),
):
    """Get recent NWWS products for monitoring NWWS connection health."""
    service = get_nwws_products_service()
    nwws_handler = get_nwws_handler()

    products = service.get_products(
        limit=limit,
        offset=offset,
        product_type=product_type,
        office=office,
    )

    return {
        "count": len(products),
        "total_received": service.get_product_count(),
        "nwws_connected": nwws_handler.is_connected if nwws_handler else False,
        "products": products,
    }


@app.get("/api/nwws/stats")
async def get_nwws_products_stats():
    """Get NWWS products service statistics."""
    service = get_nwws_products_service()
    nwws_handler = get_nwws_handler()

    stats = service.get_statistics()
    stats["nwws_connected"] = nwws_handler.is_connected if nwws_handler else False
    return stats


# =============================================================================
# AFD (Area Forecast Discussion) Endpoints
# =============================================================================

@app.get("/api/afd")
async def get_afd_offices():
    """Get list of offices with available AFDs."""
    service = get_nwws_products_service()
    offices = service.get_afd_offices()

    return {
        "count": len(offices),
        "offices": offices,
    }


@app.get("/api/afd/{office}")
async def get_afd(
    office: str,
    index: int = Query(0, ge=0, le=4, description="AFD index (0=latest, up to 4)"),
    fallback: bool = Query(True, description="Fetch from NWS API if not cached"),
):
    """Get AFD for a specific office. Checks NWWS cache first, then NWS API."""
    service = get_nwws_products_service()

    # Try NWWS cache first
    afd = service.get_afd(office, index=index)

    if afd:
        return {
            "source": "nwws",
            "afd": afd,
        }

    # Fallback to NWS API
    if fallback and index == 0:
        afd = await service.fetch_afd_from_api(office)
        if afd:
            return {
                "source": "api",
                "afd": afd,
            }

    raise HTTPException(
        status_code=404,
        detail=f"No AFD available for office '{office.upper()}'"
    )


# =============================================================================
# LLM Assistant Endpoints
# =============================================================================

class ChatRequest(BaseModel):
    """Request model for chat endpoint."""
    message: str = Field(..., description="User message to send to assistant")
    context: Optional[str] = Field(None, description="Optional additional context")
    include_history: bool = Field(True, description="Include conversation history")


class AnalyzeAlertRequest(BaseModel):
    """Request model for alert analysis."""
    alert_text: str = Field(..., description="Full alert text to analyze")
    alert_type: str = Field(..., description="Type of alert (e.g., 'Tornado Warning')")
    locations: list[str] = Field(default=[], description="Affected locations")
    context: Optional[str] = Field(None, description="Additional context")


@app.get("/api/assistant/status")
async def get_assistant_status():
    """
    Get LLM assistant status.

    Returns whether Ollama is running and the model is available.
    """
    settings = get_settings()

    if not settings.llm_enabled:
        return {
            "enabled": False,
            "available": False,
            "message": "LLM assistant is disabled in settings",
        }

    llm_service = get_llm_service()
    is_available = await llm_service.check_health()

    return {
        "enabled": True,
        "available": is_available,
        "model": llm_service.model,
        "host": llm_service.host,
        "statistics": llm_service.get_statistics(),
    }


@app.post("/api/assistant/chat")
async def assistant_chat(request: ChatRequest):
    """
    Send a message to the LLM assistant.

    Returns the assistant's response.
    """
    settings = get_settings()

    if not settings.llm_enabled:
        raise HTTPException(status_code=503, detail="LLM assistant is disabled")

    llm_service = get_llm_service()

    # Check if service is available
    is_available = await llm_service.check_health()
    if not is_available:
        raise HTTPException(
            status_code=503,
            detail="LLM service not available. Make sure Ollama is running."
        )

    # Build comprehensive context with all current weather data
    context = request.context
    if not context:
        settings = get_settings()
        alert_manager = get_alert_manager()
        alerts = alert_manager.get_alerts_sorted()

        # Get SPC data if available
        spc_data = None
        try:
            spc_service = get_spc_service()
            if spc_service:
                spc_data = {
                    "day1_categorical": None,
                    "mesoscale_discussions": [],
                }
                # Try to get cached SPC data
                try:
                    day1 = await spc_service.get_day1_outlooks()
                    if day1:
                        spc_data["day1_categorical"] = day1.get("categorical")
                except Exception:
                    pass
                try:
                    mds = await spc_service.get_mesoscale_discussions()
                    if mds:
                        spc_data["mesoscale_discussions"] = [
                            {"md_number": md.md_number, "title": md.title}
                            for md in mds.discussions[:3]
                        ]
                except Exception:
                    pass
        except Exception:
            pass

        # Get recent wind gusts if available
        wind_gusts = None
        try:
            wind_service = get_wind_gusts_service()
            if wind_service:
                states = settings.filter_states if settings.filter_states else DEFAULT_GUST_STATES
                wind_gusts = await wind_service.fetch_gusts(states=states, hours=1, limit=5)
        except Exception:
            pass

        # Build comprehensive context
        context = build_full_context(
            alerts=alerts,
            spc_data=spc_data,
            wind_gusts=wind_gusts,
            filter_states=settings.filter_states,
        )

    # Log context for debugging
    logger.info(f"LLM chat context ({len(alerts)} alerts): {context[:500]}..." if len(context) > 500 else f"LLM chat context ({len(alerts)} alerts): {context}")

    try:
        response = await llm_service.chat(
            message=request.message,
            context=context,
            include_history=request.include_history,
        )

        return {
            "success": True,
            "response": response.content,
            "model": response.model,
            "duration_ms": response.duration_ms,
        }

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/assistant/analyze")
async def analyze_alert(request: AnalyzeAlertRequest):
    """
    Analyze a weather alert and provide insights.

    Returns AI-generated analysis of the alert.
    """
    settings = get_settings()

    if not settings.llm_enabled:
        raise HTTPException(status_code=503, detail="LLM assistant is disabled")

    llm_service = get_llm_service()

    is_available = await llm_service.check_health()
    if not is_available:
        raise HTTPException(
            status_code=503,
            detail="LLM service not available. Make sure Ollama is running."
        )

    try:
        analysis = await llm_service.analyze_alert(
            alert_text=request.alert_text,
            alert_type=request.alert_type,
            locations=request.locations,
            additional_context=request.context,
        )

        return {
            "success": True,
            "analysis": analysis,
        }

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/assistant/history")
async def get_chat_history():
    """Get conversation history."""
    settings = get_settings()

    if not settings.llm_enabled:
        return {"history": [], "message": "LLM assistant is disabled"}

    llm_service = get_llm_service()
    return {
        "history": llm_service.get_history(),
    }


@app.delete("/api/assistant/history")
async def clear_chat_history():
    """Clear conversation history."""
    settings = get_settings()

    if not settings.llm_enabled:
        return {"success": True, "message": "LLM assistant is disabled"}

    llm_service = get_llm_service()
    llm_service.clear_history()

    return {
        "success": True,
        "message": "Conversation history cleared",
    }


@app.get("/api/assistant/insight")
async def get_quick_insight(
    insight_type: str = Query("general", description="Type of insight: general, wind, pattern, safety"),
):
    """
    Generate a quick insight based on current conditions.

    Returns a brief AI-generated insight.
    """
    settings = get_settings()

    if not settings.llm_enabled:
        raise HTTPException(status_code=503, detail="LLM assistant is disabled")

    llm_service = get_llm_service()

    is_available = await llm_service.check_health()
    if not is_available:
        raise HTTPException(
            status_code=503,
            detail="LLM service not available. Make sure Ollama is running."
        )

    # Build comprehensive data summary
    alert_manager = get_alert_manager()
    alerts = alert_manager.get_alerts_sorted()

    # Get wind gusts for wind-specific insight or general context
    wind_gusts = None
    try:
        wind_service = get_wind_gusts_service()
        if wind_service:
            states = settings.filter_states if settings.filter_states else DEFAULT_GUST_STATES
            wind_gusts = await wind_service.fetch_gusts(states=states, hours=1, limit=5)
    except Exception:
        pass

    # Use comprehensive context for better insights
    data_summary = build_full_context(
        alerts=alerts,
        wind_gusts=wind_gusts if insight_type == "wind" else None,
        filter_states=settings.filter_states,
    )

    try:
        insight = await llm_service.generate_insight(
            data_summary=data_summary,
            insight_type=insight_type,
        )

        return {
            "success": True,
            "insight_type": insight_type,
            "insight": insight,
        }

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Widget Configuration Endpoints
# =============================================================================

# =============================================================================
# Debug Endpoints (Zone Geometry)
# =============================================================================

@app.get("/api/debug/alerts-summary")
async def debug_alerts_summary():
    """
    Debug endpoint to see all alerts with their polygon counts.
    """
    alert_manager = get_alert_manager()
    alerts = alert_manager.get_alerts_sorted()

    summary = []
    for alert in alerts:
        polygon_count = 0
        if alert.polygon:
            # Check if multi-polygon
            if alert.polygon and len(alert.polygon) > 0:
                if isinstance(alert.polygon[0], list) and len(alert.polygon[0]) > 0:
                    if isinstance(alert.polygon[0][0], list):
                        # Multi-polygon format
                        polygon_count = len(alert.polygon)
                    else:
                        # Single polygon format (list of [lat, lon])
                        polygon_count = 1

        summary.append({
            "product_id": alert.product_id,
            "event_name": alert.event_name,
            "significance": alert.significance.value if alert.significance else None,
            "affected_areas_count": len(alert.affected_areas or []),
            "polygon_count": polygon_count,
            "has_polygon": polygon_count > 0,
        })

    return {
        "alert_count": len(summary),
        "alerts": summary,
    }


@app.get("/api/debug/alert/{product_id}/geometry")
async def debug_alert_geometry(product_id: str):
    """
    Debug endpoint to inspect an alert's zone geometry.

    Returns detailed info about the alert's polygon, affected_areas,
    and what zones are in the cache.
    """
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()

    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Check each zone in affected_areas
    zone_details = []
    for ugc in (alert.affected_areas or []):
        zone_type = zone_service.get_zone_type(ugc)
        cached = zone_service._get_from_cache(ugc)
        zone_details.append({
            "ugc": ugc,
            "zone_type": zone_type,
            "in_cache": cached is not None,
            "cached_polygon_count": len(cached) if cached else 0,
        })

    # Count total polygons in current alert
    polygon_info = None
    if alert.polygon:
        if isinstance(alert.polygon[0][0], list):
            # Multi-polygon: [[[[lat, lon], ...]], [[[lat, lon], ...]]]
            polygon_info = {
                "format": "multi-polygon",
                "polygon_count": len(alert.polygon),
                "first_polygon_points": len(alert.polygon[0]) if alert.polygon else 0,
            }
        else:
            # Single polygon or flat list: [[lat, lon], ...]
            polygon_info = {
                "format": "single-polygon or flat",
                "point_count": len(alert.polygon),
            }

    return {
        "product_id": product_id,
        "event_name": alert.event_name,
        "significance": alert.significance.value if alert.significance else None,
        "affected_areas_count": len(alert.affected_areas or []),
        "affected_areas": alert.affected_areas,
        "zone_details": zone_details,
        "polygon_info": polygon_info,
        "cache_stats": zone_service.get_cache_stats(),
    }


@app.delete("/api/debug/zone-cache")
async def debug_clear_zone_cache(
    delete_file: bool = Query(False, description="Also delete the cache file on disk"),
):
    """
    Debug endpoint to clear the zone geometry cache.

    This forces a fresh fetch from the NWS API on next request.
    """
    zone_service = get_zone_geometry_service()
    settings = get_settings()
    stats_before = zone_service.get_cache_stats()

    zone_service.clear_cache()

    file_deleted = False
    cache_file = settings.data_dir / "zone_geometry_cache.json"
    if delete_file and cache_file.exists():
        try:
            cache_file.unlink()
            file_deleted = True
            logger.info(f"Deleted zone geometry cache file: {cache_file}")
        except Exception as e:
            logger.error(f"Failed to delete cache file: {e}")

    stats_after = zone_service.get_cache_stats()

    return {
        "success": True,
        "message": "Zone geometry cache cleared",
        "file_deleted": file_deleted,
        "cache_file": str(cache_file),
        "before": stats_before,
        "after": stats_after,
    }


@app.post("/api/debug/alert/{product_id}/add-zones")
async def debug_add_zones(product_id: str, zones: str = Query(..., description="Comma-separated zone codes")):
    """
    Debug endpoint to manually add zones to an alert and repopulate geometry.

    Use this when NWS issues multiple products for the same event covering different areas.
    """
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()

    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Parse zone list
    new_zones = [z.strip().upper() for z in zones.split(",") if z.strip()]

    # Merge with existing
    existing_zones = set(alert.affected_areas or [])
    added_zones = [z for z in new_zones if z not in existing_zones]
    existing_zones.update(new_zones)
    alert.affected_areas = sorted(list(existing_zones))

    # Fetch geometry for all zones (including new ones)
    all_polygons = []
    fetch_results = []

    for ugc in alert.affected_areas:
        zone_type = zone_service.get_zone_type(ugc)
        if zone_type:
            geometry = await zone_service.fetch_zone_geometry(ugc)
            fetch_results.append({
                "ugc": ugc,
                "zone_type": zone_type,
                "polygon_count": len(geometry) if geometry else 0,
                "is_new": ugc in added_zones,
            })
            if geometry:
                all_polygons.extend(geometry)

    # Update alert
    alert.polygon = all_polygons
    alert_manager.save_to_file()

    # Broadcast update
    broker = get_message_broker()
    await broker.broadcast_alert_update(alert)

    return {
        "product_id": product_id,
        "zones_added": added_zones,
        "total_zones": len(alert.affected_areas),
        "total_polygons": len(all_polygons),
        "fetch_results": fetch_results,
    }


@app.post("/api/debug/alert/{product_id}/repopulate")
async def debug_repopulate_geometry(product_id: str, force: bool = Query(True)):
    """
    Debug endpoint to manually repopulate zone geometry for an alert.

    Returns detailed debug info about what was fetched.
    """
    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()

    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Store original polygon info
    original_polygon_count = len(alert.polygon) if alert.polygon else 0

    # Clear existing polygon if forcing
    if force:
        alert.polygon = []

    # Manually fetch each zone and track results
    fetch_results = []
    all_polygons = []

    for ugc in (alert.affected_areas or []):
        zone_type = zone_service.get_zone_type(ugc)
        if zone_type:
            geometry = await zone_service.fetch_zone_geometry(ugc)
            fetch_results.append({
                "ugc": ugc,
                "zone_type": zone_type,
                "polygon_count": len(geometry) if geometry else 0,
                "success": geometry is not None,
            })
            if geometry:
                all_polygons.extend(geometry)

    # Update alert
    alert.polygon = all_polygons

    # Save the alert
    alert_manager.save_to_file()

    # Broadcast update
    broker = get_message_broker()
    await broker.broadcast_alert_update(alert)

    return {
        "product_id": product_id,
        "event_name": alert.event_name,
        "original_polygon_count": original_polygon_count,
        "new_polygon_count": len(all_polygons),
        "zones_processed": len(fetch_results),
        "zones_with_geometry": sum(1 for r in fetch_results if r["success"]),
        "fetch_results": fetch_results,
    }


# ==================== Settings Endpoints ====================


class PhenomenaSettingsUpdate(BaseModel):
    """Request model for updating phenomena settings."""
    target_phenomena: list[str] = Field(..., description="List of phenomenon codes to enable")


# Phenomenon categories for the settings UI
_PHENOMENON_CATEGORIES = {
    "Severe": ["TO", "SV", "EW", "SQ", "SPS"],
    "Flood": ["FF", "FA", "FL"],
    "Winter": ["WS", "BZ", "IS", "LE", "WW", "WC", "CW", "ZR"],
    "Wind": ["HW", "WI"],
    "Heat": ["EH", "HT"],
    "Fire": ["FW", "RF"],
    "Fog & Visibility": ["FG", "SM", "ZF"],
    "Freeze & Frost": ["FZ", "HZ", "FR", "EC"],
    "Marine": ["MA", "SC", "SW", "GL", "SE", "SR", "HF", "BW", "RB", "SI", "ZY"],
    "Tropical": ["TR", "HU", "TY", "SS"],
    "Other": ["DS", "AS", "CF", "LS", "SU", "RP", "TS", "AF", "LO", "UP", "EQ", "VO", "AV"],
}


@app.get("/api/settings/phenomena")
async def get_phenomena_settings():
    """Get all available phenomena and their current enabled/disabled state."""
    try:
        from .models.alert import PHENOMENON_NAMES
        from .config.settings import _load_user_overrides
    except ImportError:
        from backend.models.alert import PHENOMENON_NAMES
        from backend.config.settings import _load_user_overrides

    settings = get_settings()
    active_phenomena = set(p.upper() for p in settings.target_phenomena)

    # Build grouped response
    categories = {}
    assigned = set()
    for cat_name, codes in _PHENOMENON_CATEGORIES.items():
        items = []
        for code in codes:
            if code in PHENOMENON_NAMES and code not in assigned:
                items.append({
                    "code": code,
                    "name": PHENOMENON_NAMES[code],
                    "enabled": code in active_phenomena,
                })
                assigned.add(code)
        if items:
            categories[cat_name] = items

    # Catch any unassigned phenomena
    for code, name in PHENOMENON_NAMES.items():
        if code not in assigned and code != "SPS":
            categories.setdefault("Other", []).append({
                "code": code,
                "name": name,
                "enabled": code in active_phenomena,
            })

    overrides = _load_user_overrides()

    return {
        "categories": categories,
        "active_phenomena": sorted(active_phenomena),
        "using_overrides": "target_phenomena" in overrides,
    }


@app.post("/api/settings/phenomena")
async def update_phenomena_settings(update: PhenomenaSettingsUpdate):
    """Update which phenomena are monitored. Saves to user_settings.json and reloads."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings

    # Normalize codes
    normalized = [code.upper() for code in update.target_phenomena if code.strip()]

    if not normalized:
        raise HTTPException(status_code=400, detail="At least one phenomenon must be enabled")

    # Load existing overrides, update, save
    overrides = _load_user_overrides()
    overrides["target_phenomena"] = normalized
    _save_user_overrides(overrides)

    # Reload settings for immediate effect
    new_settings = reload_settings()

    logger.info(f"Settings updated: {len(normalized)} phenomena enabled")

    return {
        "success": True,
        "active_phenomena": sorted(new_settings.target_phenomena),
        "message": f"{len(normalized)} phenomena enabled",
    }


@app.post("/api/settings/phenomena/reset")
async def reset_phenomena_settings():
    """Reset phenomena settings to .env defaults."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings, _USER_SETTINGS_FILE
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings, _USER_SETTINGS_FILE

    overrides = _load_user_overrides()
    overrides.pop("target_phenomena", None)

    if overrides:
        _save_user_overrides(overrides)
    elif _USER_SETTINGS_FILE.exists():
        _USER_SETTINGS_FILE.unlink()

    new_settings = reload_settings()

    logger.info("Settings reset to defaults")

    return {
        "success": True,
        "active_phenomena": sorted(new_settings.target_phenomena),
        "message": "Reset to default settings",
    }


# ==================== Widget Endpoints ====================


@app.get("/api/widgets/config")
async def get_widget_config():
    """
    Get widget configuration.

    Returns filter states and other settings for streaming widgets.
    """
    settings = get_settings()

    return {
        "filter_states": settings.filter_states,
        "target_phenomena": settings.target_phenomena,
        "websocket_url": "/ws",
        "themes": ["classic", "atmospheric", "storm-chaser", "meteorologist", "winter"],
        "widgets": {
            "ticker": {
                "url": "/widgets/ticker.html",
                "description": "Alert ticker widget for streams (no sponsor)",
            },
            "ticker_sponsored": {
                "url": "/widgets/ticker-sponsored.html",
                "description": "Alert ticker widget with sponsor slot",
            },
        },
    }


@app.get("/api/widgets/sponsors")
async def get_widget_sponsors():
    """
    Get sponsor configuration for widgets.

    Returns list of sponsors for the sponsored ticker widget.
    Currently returns default sponsor; can be extended to load from database/config.
    """
    return {
        "sponsors": [
            {
                "type": "text",
                "content": "Weather Dashboard",
                "subtext": "Powered by NWS",
            }
        ],
    }


# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time alert updates.

    Clients connect here to receive:
    - New alerts (alert_new)
    - Updated alerts (alert_update)
    - Removed alerts (alert_remove)
    - System status updates
    """
    broker = get_message_broker()
    client_id = await broker.connect(websocket)

    try:
        # Send current alerts on connect
        alert_manager = get_alert_manager()
        alerts = alert_manager.get_alerts_sorted()
        await broker.send_to_client_by_id(
            client_id,
            MessageType.ALERT_BULK,
            {
                "count": len(alerts),
                "alerts": [a.to_dict() for a in alerts],
            }
        )

        # Handle incoming messages
        while True:
            try:
                message = await websocket.receive_text()
                await broker.handle_message(client_id, message)
            except WebSocketDisconnect:
                break

    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
    finally:
        # Clean up chaser position if this was a chase mode client
        if client_id in _chaser_positions:
            del _chaser_positions[client_id]
            await broker._broadcast(MessageType.CHASER_DISCONNECT, {
                "client_id": client_id
            })
            # End chase log session if no more active chasers from WebSocket
            ws_chasers = [k for k in _chaser_positions if not k.startswith("spotter_network_")]
            if not ws_chasers:
                chase_log = get_chase_log_service()
                if chase_log.active_session:
                    chase_log.end_session()
            logger.info(f"Chaser disconnected: {client_id}")
        await broker.disconnect(client_id)


# =============================================================================
# Chaser Tracking API
# =============================================================================

@app.get("/api/chasers")
async def get_chasers():
    """Get all active chaser positions."""
    return {"chasers": list(_chaser_positions.values())}


# =============================================================================
# Chase Log API
# =============================================================================

@app.get("/api/chase-logs")
async def list_chase_logs():
    """List all chase log sessions."""
    service = get_chase_log_service()
    return {"sessions": service.list_sessions()}


@app.get("/api/chase-logs/{date}")
async def get_chase_log(date: str):
    """Get a specific chase log by date (YYYY-MM-DD)."""
    service = get_chase_log_service()
    session = service.get_session(date)
    if not session:
        raise HTTPException(status_code=404, detail=f"No chase log for {date}")
    return session


@app.get("/api/chase-logs/{date}/geojson")
async def get_chase_log_geojson(date: str):
    """Export a chase log as GeoJSON LineString."""
    service = get_chase_log_service()
    geojson = service.get_session_geojson(date)
    if not geojson:
        raise HTTPException(status_code=404, detail=f"No chase log for {date}")
    return geojson


# =============================================================================
# SPA Catch-All Route (must be after all API routes)
# =============================================================================

@app.get("/obs")
@app.get("/obs/{path:path}")
async def serve_obs_overlay(path: str = ""):
    """Serve the frontend for OBS overlay route."""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Frontend not built")


@app.get("/chase")
@app.get("/chase/{path:path}")
async def serve_chase_mode(path: str = ""):
    """Serve the frontend for Chase Mode route."""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Frontend not built")


# =============================================================================
# Error Handlers
# =============================================================================

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Run the application using uvicorn."""
    import uvicorn

    # Import setup_logging here to handle both module and direct execution
    try:
        from .utils.logging import setup_logging
    except ImportError:
        from backend.utils.logging import setup_logging

    settings = get_settings()

    # Initialize file logging with rotation
    # Log files: logs/alert_dashboard.log, .log.1, .log.2, etc.
    # Rotates when file reaches max_size_mb, keeps backup_count old files
    setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir if settings.log_to_file else None,
        max_bytes=settings.log_max_size_mb * 1024 * 1024,
        backup_count=settings.log_backup_count,
        console_output=settings.log_to_console,
    )

    logger.info(f"Starting Alert Dashboard V2 on {settings.host}:{settings.port}")
    if settings.log_to_file:
        logger.info(f"Log files: {settings.log_dir}/alert_dashboard.log")

    # Exclude logs and data directories from file watching to prevent feedback loops
    reload_excludes = ["logs", "data", "*.log", "__pycache__"]

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        reload_excludes=reload_excludes if settings.debug else None,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
