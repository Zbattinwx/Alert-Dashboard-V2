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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings, Settings
from .models.alert import Alert
from .services import (
    # Alert Manager
    get_alert_manager,
    start_alert_manager,
    stop_alert_manager,
    # Message Broker
    get_message_broker,
    MessageType,
    # NWS API Client
    get_nws_client,
    close_nws_client,
    # NWWS Client
    get_nwws_handler,
    start_nwws_handler,
    stop_nwws_handler,
    # Zone Geometry
    get_zone_geometry_service,
    start_zone_geometry_service,
    stop_zone_geometry_service,
)

logger = logging.getLogger(__name__)


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

    # 1. Start Zone Geometry Service (for caching polygons)
    await start_zone_geometry_service()
    logger.info("Zone Geometry Service started")

    # 2. Start Alert Manager (loads persisted alerts)
    await start_alert_manager()
    logger.info("Alert Manager started")

    # 3. Wire up Alert Manager callbacks to Message Broker
    wire_alert_callbacks()

    # 4. Fetch initial alerts from NWS API
    await fetch_initial_alerts()

    # 5. Start NWWS handler for real-time alerts (if configured)
    if settings.nwws_username and settings.nwws_password:
        await start_nwws_handler()

        # Wire NWWS alerts to Alert Manager
        nwws_handler = get_nwws_handler()
        alert_manager = get_alert_manager()
        nwws_handler.add_alert_callback(alert_manager.add_alert)

        logger.info("NWWS Handler started")
    else:
        logger.warning("NWWS credentials not configured - using API-only mode")

    # 6. Start periodic API polling (backup to NWWS)
    asyncio.create_task(api_polling_loop())

    logger.info("All services started successfully")


async def shutdown_services():
    """Gracefully shutdown all services."""
    logger.info("Shutting down services...")

    # Stop in reverse order
    await stop_nwws_handler()
    await stop_alert_manager()
    await stop_zone_geometry_service()
    await close_nws_client()

    logger.info("All services stopped")


def wire_alert_callbacks():
    """Connect Alert Manager events to WebSocket broadcasts."""
    alert_manager = get_alert_manager()
    broker = get_message_broker()

    async def on_alert_added(alert: Alert):
        await broker.broadcast_alert_new(alert)

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

# Mount static files if the build directory exists
if FRONTEND_DIR.exists():
    # Serve static assets (js, css, images) from /assets
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    logger.info(f"Serving frontend static files from {FRONTEND_DIR}")


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


@app.get("/api/stats")
async def get_stats():
    """Get alert statistics."""
    alert_manager = get_alert_manager()
    return alert_manager.get_statistics()


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
        await broker.disconnect(client_id)


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

    settings = get_settings()

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
