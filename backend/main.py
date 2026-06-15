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
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Handle both direct execution and module execution
try:
    from .config import get_settings, Settings, get_brand_config
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
        get_511_service,
        get_cars_service,
        get_spc_service, start_spc_service, stop_spc_service, RISK_COLORS, RISK_NAMES,
        get_wind_gusts_service, start_wind_gusts_service, stop_wind_gusts_service,
        GUST_THRESHOLD_SIGNIFICANT, GUST_THRESHOLD_SEVERE, GUST_THRESHOLD_ADVISORY,
        DEFAULT_GUST_STATES,
        get_llm_service, start_llm_service, stop_llm_service, build_full_context,
        get_google_chat_service, start_google_chat_service, stop_google_chat_service,
        get_spotter_network_service, start_spotter_network_service, stop_spotter_network_service,
        get_social_media_service, start_social_media_service, stop_social_media_service,
        get_chase_log_service,
        get_radar_service,
        get_nwws_products_service, start_nwws_products_service, stop_nwws_products_service,
        get_agent_service, start_agent_service, stop_agent_service,
    )
    from .services.nexrad_service import get_nexrad_service, start_nexrad_service, stop_nexrad_service
    from .services.storm_tracking_service import get_storm_tracking_service, start_storm_tracking_service, stop_storm_tracking_service
    from .services.nexrad_sites import NEXRAD_SITES, get_nearest_sites
    from .services.glm_service import get_glm_service, start_glm_service, stop_glm_service
    from .services.mrms_service import get_mrms_service, start_mrms_service, stop_mrms_service
except ImportError:
    # Direct execution: python backend/main.py
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.config import get_settings, Settings, get_brand_config
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
        get_511_service,
        get_cars_service,
        get_spc_service, start_spc_service, stop_spc_service, RISK_COLORS, RISK_NAMES,
        get_wind_gusts_service, start_wind_gusts_service, stop_wind_gusts_service,
        GUST_THRESHOLD_SIGNIFICANT, GUST_THRESHOLD_SEVERE, GUST_THRESHOLD_ADVISORY,
        DEFAULT_GUST_STATES,
        get_llm_service, start_llm_service, stop_llm_service, build_full_context,
        get_google_chat_service, start_google_chat_service, stop_google_chat_service,
        get_spotter_network_service, start_spotter_network_service, stop_spotter_network_service,
        get_social_media_service, start_social_media_service, stop_social_media_service,
        get_chase_log_service,
        get_radar_service,
        get_nwws_products_service, start_nwws_products_service, stop_nwws_products_service,
        get_agent_service, start_agent_service, stop_agent_service,
    )
    from backend.services.nexrad_service import get_nexrad_service, start_nexrad_service, stop_nexrad_service
    from backend.services.storm_tracking_service import get_storm_tracking_service, start_storm_tracking_service, stop_storm_tracking_service
    from backend.services.nexrad_sites import NEXRAD_SITES, get_nearest_sites
    from backend.services.glm_service import get_glm_service, start_glm_service, stop_glm_service
    from backend.services.mrms_service import get_mrms_service, start_mrms_service, stop_mrms_service

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

    # 11b. Start Agent Service (tool-calling AI agent)
    agent_available = await start_agent_service()
    if agent_available:
        logger.info("Agent Service started and available")
    else:
        logger.warning("Agent Service not available - check Ollama and agent model")

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

    # 14. Start Social Media Service (optional)
    social_media_started = await start_social_media_service()
    if social_media_started:
        logger.info("Social media service started")
    else:
        logger.info("Social media service started (no platforms configured)")

    # 15. Start NEXRAD Radar Service (optional)
    if settings.nexrad_enabled:
        nexrad_started = await start_nexrad_service()
        if nexrad_started:
            logger.info(f"NEXRAD radar service started (site: {settings.nexrad_default_site})")

            # Start storm tracking and wire to radar
            storm_started = await start_storm_tracking_service()
            if storm_started:
                nexrad_svc = get_nexrad_service()
                storm_svc = get_storm_tracking_service()
                broker = get_message_broker()

                # Wire radar frame broadcasts
                async def on_radar_frames(frames):
                    for frame in frames:
                        # Binary frame for WebGL clients
                        if frame.binary_data:
                            await broker.broadcast_radar_frame_binary(frame.binary_data)
                        # JSON metadata for status pill / non-binary clients
                        await broker.broadcast_radar_frame(frame.to_dict())

                # ── Optional extras: proactive analyst, agent LLM, live QA ──
                # Best-effort.  A failure constructing or wiring any of these
                # must NOT prevent the core storm-cell tracking + broadcast
                # wiring below from running — otherwise `on_volume_ready` is
                # never set, the gridder never spawns, and the dashboard shows
                # zero storm cells even with severe weather on the radar.
                analyst = None
                live_qa = None
                try:
                    from .services.storm_analyst_service import create_storm_analyst_service
                    from .services.agent_service import get_agent_service as _get_agent_svc
                    from .services.live_qa_service import create_live_qa_reporter
                    analyst = create_storm_analyst_service()

                    # Live QA reporter — in-process per-scan QA log + optional
                    # training-data collection.  Auto-starts when live_qa_enabled.
                    if settings.live_qa_enabled:
                        from pathlib import Path as _Path
                        log_file = (
                            _Path(settings.data_dir) / "training_data.jsonl"
                            if settings.live_qa_log_training_data else None
                        )
                        live_qa = create_live_qa_reporter(
                            log_file=log_file,
                            min_score=settings.live_qa_min_score,
                            verbose=settings.live_qa_verbose,
                        )

                    # Give the analyst a callback to reach the agent LLM
                    _agent_svc = _get_agent_svc()
                    if _agent_svc is not None:
                        async def _agent_analyze(cells):
                            return await _agent_svc.analyze_storm_cells(cells)
                        analyst.set_agent_callback(_agent_analyze)

                    # Give the analyst a callback to broadcast via WebSocket
                    async def _broadcast_notification(content, cells, notification_id, timestamp):
                        await broker.broadcast_agent_notification({
                            "id": notification_id,
                            "content": content,
                            "cells": cells,
                            "timestamp": timestamp,
                        })
                    analyst.set_broadcast_callback(_broadcast_notification)
                except Exception as e:
                    analyst = None
                    live_qa = None
                    logger.error(
                        "Storm analyst / live-QA extras failed to initialize — "
                        "storm cell tracking will still run, analyst disabled: "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )

                async def on_cells_updated(cells):
                    await broker.broadcast_storm_cells([c.to_dict() for c in cells])
                    if analyst is not None:
                        try:
                            await analyst.process_cells(cells)
                            analyst.cleanup_stale({c.cell_id for c in cells})
                        except Exception as e:
                            logger.warning(f"Storm analyst processing failed: {e}")
                    if live_qa is not None:
                        try:
                            await live_qa.on_cells(cells)
                        except Exception as e:
                            logger.warning(f"Live QA on_cells failed: {e}")

                async def on_systems_updated(systems):
                    await broker.broadcast_mcs_systems([s.to_dict() for s in systems])

                # Wire radar status broadcasts
                async def on_radar_status(status):
                    await broker.broadcast_radar_status(status.to_dict())

                # ── Critical wiring — always runs (independent of extras) ──
                nexrad_svc.on_frame_ready = on_radar_frames
                nexrad_svc.on_volume_ready = storm_svc.process_volume
                nexrad_svc.on_status_change = on_radar_status
                storm_svc.on_cells_updated = on_cells_updated
                storm_svc.on_systems_updated = on_systems_updated

                # Push mean storm motion to the renderer so the SRV product
                # can subtract the storm's radial component at each ray.
                async def on_motion_update(direction_deg, speed_kph):
                    nexrad_svc.set_storm_motion(direction_deg, speed_kph)
                storm_svc.on_motion_update = on_motion_update

                logger.info("Storm tracking service started and wired to radar")

                # Start GLM lightning service and wire to broker + storm tracker
                try:
                    logger.info("Starting GLM lightning service...")
                    glm_started = await start_glm_service()
                    if glm_started:
                        glm_svc = get_glm_service()

                        async def on_new_flashes(flashes):
                            broker = get_message_broker()
                            payload = [
                                {"lat": f.lat, "lon": f.lon,
                                 "energy": f.energy, "timestamp": f.timestamp}
                                for f in flashes
                            ]
                            await broker.broadcast_lightning_strikes(payload)

                        glm_svc.on_new_flashes = on_new_flashes
                        storm_svc.set_glm_service(glm_svc)
                        logger.info("GLM lightning service started and wired")
                    else:
                        logger.info("GLM lightning service not available (boto3/netCDF4 not installed)")
                except Exception as e:
                    logger.error(f"GLM lightning service failed to start: {e}", exc_info=True)

                # Optional near-real-time chunks-bucket ingestion path.
                # Runs alongside the archive-bucket pipeline; gated behind
                # `nexrad_chunks_enabled` setting (default OFF).
                if settings.nexrad_chunks_enabled:
                    try:
                        from .services.nexrad_chunks_service import (
                            start_nexrad_chunks_service,
                        )
                        chunks_svc = await start_nexrad_chunks_service(
                            nexrad_svc, settings
                        )
                        if chunks_svc:
                            logger.info("NEXRAD chunks-bucket service running alongside archive")
                    except Exception as e:
                        logger.error(
                            f"NEXRAD chunks service failed to start: {e}", exc_info=True
                        )
            else:
                logger.warning("Storm tracking service failed to start")
        else:
            logger.warning("NEXRAD radar service failed to start (missing dependencies?)")
    else:
        logger.info("NEXRAD radar disabled (set NEXRAD_ENABLED=true to enable)")

    # Start MRMS composite reflectivity (independent of NEXRAD; requires pygrib)
    try:
        logger.info("Starting MRMS composite reflectivity service...")
        await start_mrms_service()
        mrms_svc = get_mrms_service()
        if mrms_svc and mrms_svc.available:
            logger.info("MRMS service started")
        else:
            logger.info("MRMS service disabled (install pygrib via conda to enable)")
    except Exception as e:
        logger.error(f"MRMS service failed to start: {e}", exc_info=True)

    # Start MRMS rotation tracks + azimuthal shear ingester — feeds the
    # rotation classifier with multi-radar fused rotation values per cell.
    # Safe to skip silently if eccodes isn't installed.
    try:
        from .services.mrms_rotation_service import start_mrms_rotation_service
        logger.info("Starting MRMS rotation tracks service...")
        rot_svc = await start_mrms_rotation_service()
        if rot_svc and rot_svc.available:
            logger.info("MRMS rotation service started")
        else:
            logger.info("MRMS rotation service disabled (eccodes unavailable)")
    except Exception as e:
        logger.error(
            f"MRMS rotation service failed to start: {e}", exc_info=True,
        )

    logger.info("All services started successfully")

    # Backfill broadcast graphics for any active alerts that were restored from
    # disk (load_from_file doesn't fire on_alert_added, so they'd be missed).
    asyncio.create_task(_backfill_broadcast_graphics())


async def shutdown_services():
    """Gracefully shutdown all services."""
    logger.info("Shutting down services...")

    # Stop in reverse order
    try:
        from .services.mrms_rotation_service import stop_mrms_rotation_service
        await stop_mrms_rotation_service()
    except Exception:
        pass
    await stop_mrms_service()
    await stop_glm_service()
    try:
        from .services.nexrad_chunks_service import stop_nexrad_chunks_service
        await stop_nexrad_chunks_service()
    except Exception:
        pass
    await stop_storm_tracking_service()
    await stop_nexrad_service()
    await stop_social_media_service()
    await stop_spotter_network_service()
    await stop_google_chat_service()
    await stop_agent_service()
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


async def _fetch_zone_polygons_for_alert(alert) -> "list[list[list[float]]] | None":
    """
    For zone/county-based alerts (no precise polygon), fetch county geometries
    from the zone_geometry_service and return a flat list of polygon rings.
    """
    if alert.polygon and len(alert.polygon) >= 3:
        # Distinguish a genuine flat polygon ([[lat,lon], ...]) from the nested
        # county-ring structure populate_alert_geometry stores in alert.polygon
        # ([[ring1_pts], [ring2_pts], ...]). In the flat case, alert.polygon[0][0]
        # is a float (latitude). In the nested case it's a list (a coord pair).
        first = alert.polygon[0]
        if first and isinstance(first[0], (int, float)):
            return None   # genuine precise polygon — no need for zone geoms
        # Nested zone structure — return it directly as zone_polygons
        return alert.polygon  # type: ignore[return-value]
    if not getattr(alert, "affected_areas", None):
        return None
    try:
        from backend.services.zone_geometry_service import get_zone_geometry_service
        zone_svc = get_zone_geometry_service()
        result = await zone_svc.fetch_multiple_zones(alert.affected_areas)
        flat: list[list[list[float]]] = []
        for zone_polys in result.values():
            if zone_polys:
                flat.extend(zone_polys)
        return flat or None
    except Exception as e:
        logger.debug(f"Zone geometry fetch failed for {alert.product_id}: {e}")
        return None


def _prune_stale_broadcast_graphics(active_ids: set[str]) -> int:
    """Delete saved broadcast graphics whose alert is no longer active.

    Files are named <safe_product_id>.png / .json (plus an optional
    <safe_product_id>_confirmed.png for PDS/confirmed tornado graphics).  Any
    file whose base id isn't in ``active_ids`` is removed.
    """
    import re as _re
    if not _GRAPHICS_DIR.exists():
        return 0
    removed = 0
    for f in _GRAPHICS_DIR.iterdir():
        if not f.is_file():
            continue
        stem = f.stem
        if stem.endswith("_confirmed"):
            stem = stem[: -len("_confirmed")]
        if stem not in active_ids:
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                logger.debug(f"Could not prune stale graphic {f.name}: {e}")
    if removed:
        logger.info(f"Pruned {removed} stale broadcast graphic file(s) on startup")
    return removed


async def _backfill_broadcast_graphics():
    """On startup, prune stale graphics then generate any missing for active alerts."""
    await asyncio.sleep(5)  # let services finish initialising
    import re as _re
    alert_mgr = get_alert_manager()

    # Prune graphics for alerts that are no longer active before backfilling.
    active_ids = {
        _re.sub(r"[^\w\-.]", "_", a.product_id)
        for a in alert_mgr.get_all_alerts() if a.is_active
    }
    _prune_stale_broadcast_graphics(active_ids)

    graphic_phenomena = {"TO", "SV", "FF", "FA", "FL", "BZ", "WS", "IS", "EW", "HW"}
    for alert in alert_mgr.get_all_alerts():
        if not alert.is_active or alert.phenomenon not in graphic_phenomena:
            continue
        import re as _re
        safe_id = _re.sub(r"[^\w\-.]", "_", alert.product_id)
        img_path = _GRAPHICS_DIR / f"{safe_id}.png"
        if img_path.exists():
            continue  # already have one
        logger.info(f"Backfilling broadcast graphic for {alert.product_id}")
        asyncio.create_task(_auto_generate_broadcast_graphic(alert))


async def _auto_generate_broadcast_graphic(alert: Alert):
    """Background task: generate + save a broadcast graphic for a new alert."""
    try:
        import math as _math_ag
        settings = get_settings()
        brand = get_brand_config(settings.brand)

        # Resolve best radar frame (same cascade as the API endpoint)
        radar_frame = None
        nexrad_svc_ag = None
        centroid_ag = None
        nearest_site_ag = None
        try:
            nexrad_svc_ag = get_nexrad_service()
            from backend.services.nexrad_sites import NEXRAD_SITES as _NS_ag
            sender = getattr(alert, "sender_office", "").upper().lstrip("K")
            centroid_ag = getattr(alert, "centroid", None)
            candidate = "K" + sender if sender else ""
            if candidate not in _NS_ag and centroid_ag:
                best_id, best_d = "", float("inf")
                for sid, info in _NS_ag.items():
                    dlat = info["lat"] - centroid_ag[0]
                    dlon = (info["lon"] - centroid_ag[1]) * _math_ag.cos(_math_ag.radians(centroid_ag[0]))
                    d = dlat * dlat + dlon * dlon
                    if d < best_d:
                        best_d, best_id = d, sid
                candidate = best_id
            nearest_site_ag = candidate

            # Tier 1a: cached frame for the WFO/nearest site
            if candidate:
                hist = nexrad_svc_ag.get_frame_history("reflectivity", count=1, site=candidate)
                if hist:
                    radar_frame = hist[-1]
                    logger.info(f"Auto graphic: using cached {candidate} frame")

            # Tier 1b: active site if within ~250 km
            if radar_frame is None and centroid_ag:
                frames = nexrad_svc_ag.get_latest_frames()
                cframe = frames.get("reflectivity") or frames.get("Reflectivity")
                if cframe:
                    asite = nexrad_svc_ag.active_site
                    sinfo = _NS_ag.get(asite, {})
                    if sinfo:
                        dlat = sinfo["lat"] - centroid_ag[0]
                        dlon = (sinfo["lon"] - centroid_ag[1]) * _math_ag.cos(_math_ag.radians(centroid_ag[0]))
                        if _math_ag.sqrt(dlat * dlat + dlon * dlon) < 2.25:
                            radar_frame = cframe
                            logger.info(f"Auto graphic: using active-site {asite} frame")
        except Exception:
            pass

        # Live/cached frames carry raw binary polar data (RDRF) with no image_path,
        # which the Pillow map renderer can't overlay — so it would silently fall back
        # to public composite tiles.  Rasterize OUR binary frame to a PNG so the
        # graphic uses our Level-2 radar.
        if radar_frame is not None and getattr(radar_frame, "image_path", None) is None \
                and getattr(radar_frame, "binary_data", None) and nexrad_svc_ag:
            try:
                loop_bin = asyncio.get_event_loop()
                rendered = await loop_bin.run_in_executor(
                    None, lambda: nexrad_svc_ag.render_binary_frame_to_image(radar_frame)
                )
                if rendered is not None:
                    radar_frame = rendered
                    logger.info("Auto graphic: rasterized cached binary frame for overlay")
                else:
                    logger.info("Auto graphic: binary rasterize returned None; will try oneshot/tiles")
                    radar_frame = None
            except Exception as _e_bin:
                logger.debug(f"Auto graphic binary rasterize failed: {_e_bin}")
                radar_frame = None

        # Tier 1c: one-off Level-2 download from nearest NEXRAD site
        if radar_frame is None and centroid_ag and nexrad_svc_ag and nearest_site_ag:
            try:
                loop_ag = asyncio.get_event_loop()
                site_to_fetch = nearest_site_ag
                logger.info(f"Auto graphic: oneshot download from {site_to_fetch}")
                radar_frame = await loop_ag.run_in_executor(
                    None, lambda: nexrad_svc_ag.oneshot_frame(site_to_fetch)
                )
                if radar_frame:
                    logger.info(f"Auto graphic: oneshot {site_to_fetch} succeeded")
                else:
                    logger.info(f"Auto graphic: oneshot {site_to_fetch} returned None, falling back to tiles")
            except Exception as _e_ag:
                logger.debug(f"Auto graphic oneshot failed: {_e_ag}")

        try:
            from .services.alert_broadcast_graphic_service import (
                generate_alert_broadcast_graphic as _gen,
                generate_tornado_confirmed_graphic as _gen_tor,
            )
        except ImportError:
            from backend.services.alert_broadcast_graphic_service import (
                generate_alert_broadcast_graphic as _gen,
                generate_tornado_confirmed_graphic as _gen_tor,
            )

        meteorologist = getattr(brand, "meteorologist_name", None) or ""
        zone_polys = await _fetch_zone_polygons_for_alert(alert)

        loop = asyncio.get_event_loop()
        png_bytes = await loop.run_in_executor(
            None,
            lambda: _gen(
                alert=alert,
                radar_frame=radar_frame,
                zone_polygons=zone_polys,
                brand_name=brand.name,
                meteorologist_name=meteorologist,
            )
        )

        import re as _re, json as _json
        _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = _re.sub(r"[^\w\-.]", "_", alert.product_id)
        img_path = _GRAPHICS_DIR / f"{safe_id}.png"
        img_path.write_bytes(png_bytes)
        meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
        meta_path.write_text(_json.dumps({
            "product_id": alert.product_id,
            "event_name": alert.event_name,
        }))
        logger.info(f"Auto-generated broadcast graphic for {alert.product_id} -> {safe_id}.png")

        # Also generate the confirmed/PDS tornado graphic for qualifying warnings
        threat_ag = getattr(alert, "threat", None)
        tor_det_ag = (getattr(threat_ag, "tornado_detection", None) or "").upper()
        dmg_ag = (getattr(threat_ag, "tornado_damage_threat", None) or "").upper()
        is_confirmed = (
            alert.phenomenon == "TO" and (
                tor_det_ag == "OBSERVED" or
                dmg_ag in ("CONSIDERABLE", "CATASTROPHIC") or
                "particularly dangerous" in (getattr(alert, "description", "") or "").lower()
            )
        )
        if is_confirmed:
            logger.info(f"Generating confirmed tornado graphic for {alert.product_id}")
            try:
                tor_bytes = await loop.run_in_executor(
                    None,
                    lambda: _gen_tor(
                        alert=alert,
                        radar_frame=radar_frame,
                        zone_polygons=zone_polys,
                        brand_name=brand.name,
                        meteorologist_name=meteorologist,
                    )
                )
                tor_path = _GRAPHICS_DIR / f"{safe_id}_confirmed.png"
                tor_path.write_bytes(tor_bytes)
                logger.info(f"Saved confirmed tornado graphic: {safe_id}_confirmed.png")
            except Exception as _e_tor:
                logger.warning(f"Confirmed tornado graphic failed: {_e_tor}")

    except Exception as e:
        logger.exception(f"Auto broadcast graphic failed for {alert.product_id}: {e}")


_zone_broadcast_task: "Optional[asyncio.Task]" = None


def _schedule_zone_broadcast(delay: float = 0.4):
    """
    Debounced rebuild + WebSocket broadcast of the full map-zone payload.

    A single alert change can fan out into many callbacks in quick succession
    (e.g. a Watch County Notification touching a dozen counties). Coalesce them
    into one zone rebuild so we resolve geometry and broadcast `alert_zones`
    just once per burst. The short delay also lets just-issued alerts finish
    resolving their zone geometry before we build the payload.
    """
    global _zone_broadcast_task
    broker = get_message_broker()

    async def _run():
        try:
            await asyncio.sleep(delay)
            payload = await build_map_zones()
            await broker.broadcast_alert_zones(payload)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Zone broadcast failed: {e}")

    if _zone_broadcast_task and not _zone_broadcast_task.done():
        _zone_broadcast_task.cancel()
    _zone_broadcast_task = asyncio.create_task(_run())


def wire_alert_callbacks():
    """Connect Alert Manager events to WebSocket broadcasts and notifications."""
    alert_manager = get_alert_manager()
    broker = get_message_broker()
    google_chat_service = get_google_chat_service()

    try:
        from .services.event_stats_service import get_event_stats_service
    except ImportError:
        from backend.services.event_stats_service import get_event_stats_service

    event_stats = get_event_stats_service()

    async def on_alert_added(alert: Alert):
        # Broadcast to WebSocket clients
        await broker.broadcast_alert_new(alert)
        # Push refreshed zone geometry so zone-fill alerts (esp. watches, which
        # have no storm polygon) render on maps immediately, not at next poll.
        _schedule_zone_broadcast()
        # Send Google Chat notification (only for new alerts, not updates)
        await google_chat_service.notify_new_alert(alert)
        # Auto-generate broadcast graphic for warnings
        if alert.phenomenon in ("TO", "SV", "FF", "FA", "FL", "BZ", "WS", "IS", "EW", "HW"):
            asyncio.create_task(_auto_generate_broadcast_graphic(alert))

    async def on_alert_updated(alert: Alert):
        await broker.broadcast_alert_update(alert)
        # A watch's per-county areas often arrive via an update (WCN) shortly
        # after the initial product — rebroadcast zones so they appear at once.
        _schedule_zone_broadcast()
        # Regenerate graphic on update so it reflects the latest threat info
        # (e.g. radar-indicated → observed upgrade, new radar frame, etc.)
        if alert.phenomenon in ("TO", "SV", "FF", "FA", "FL", "BZ", "WS", "IS", "EW", "HW"):
            asyncio.create_task(_auto_generate_broadcast_graphic(alert))

    async def on_alert_removed(alert: Alert):
        await broker.broadcast_alert_remove(alert)
        # Clear the zone fill for the removed alert right away.
        _schedule_zone_broadcast()
        # Delete saved graphics for this alert so the gallery doesn't fill up
        import re as _re2
        safe_id = _re2.sub(r"[^\w\-.]", "_", alert.product_id)
        for suffix in ("", "_confirmed"):
            for ext in (".png", ".json"):
                p = _GRAPHICS_DIR / f"{safe_id}{suffix}{ext}"
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        logger.debug(f"Deleted graphics for expired alert {alert.product_id}")

    # Wrap async callbacks for sync AlertManager
    def sync_added(alert: Alert):
        event_stats.on_alert_added(alert)
        asyncio.create_task(on_alert_added(alert))

    def sync_updated(alert: Alert):
        asyncio.create_task(on_alert_updated(alert))

    def sync_removed(alert: Alert):
        event_stats.on_alert_removed(alert)
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

    # Radar site change via WebSocket
    async def handle_radar_set_site(client_id: str, data: dict):
        site_id = data.get("site_id", "")
        svc = get_nexrad_service()
        if svc and site_id:
            try:
                await svc.set_active_site(site_id)
            except ValueError as e:
                logger.warning(f"Invalid radar site from {client_id}: {e}")

    broker.register_handler(MessageType.RADAR_SET_SITE, handle_radar_set_site)


async def _check_polygon_and_capture(
    lat: float, lon: float, chaser_name: str, chase_log
):
    """Check if chaser is inside any warning polygon and capture radar if so."""
    try:
        from shapely.geometry import Point, Polygon as ShapelyPolygon

        point = Point(lon, lat)  # Shapely uses (x, y) = (lon, lat)
        alert_manager = get_alert_manager()
        alerts = alert_manager.get_all_alerts()

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
# Regex covers: localhost on any port, 127.0.0.1 on any port,
# any 192.168.x.x address (LAN) on any port, and the Tauri desktop app origin
# (https://tauri.localhost on Windows WebView2, tauri://localhost elsewhere) —
# without it the bundled radar app's fetches (cameras, MRMS) are CORS-blocked.
# allow_credentials=False is required when using a wildcard/regex origin
# (the CORS spec forbids credentials=True with non-specific origins).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?|http://192\.168\.\d+\.\d+(:\d+)?|https?://tauri\.localhost|tauri://localhost",
    allow_credentials=False,
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

# Path to custom alert sounds
SOUNDS_DIR = Path("data/sounds")
SOUNDS_DIR.mkdir(parents=True, exist_ok=True)

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

# Mount custom sounds directory
app.mount("/data/sounds", StaticFiles(directory=SOUNDS_DIR), name="sounds")


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


@app.post("/api/alerts/{product_id}/impact-scan")
async def scan_alert_impact(product_id: str, push: bool = True, force: bool = False):
    """Scan a warning polygon for at-risk places via OpenStreetMap/Overpass.

    Returns the categorized impacted places (towns, mobile home parks, schools,
    hospitals/care). When ``push`` is true and the scan found anything, the
    result is also broadcast to the on-stream impact widget.
    """
    alert_manager = get_alert_manager()
    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    if not alert.polygon or len(alert.polygon) < 3:
        raise HTTPException(status_code=422, detail="Alert has no polygon to scan")

    try:
        from .services.osm_impact_service import get_osm_impact_service
    except ImportError:
        from backend.services.osm_impact_service import get_osm_impact_service

    service = get_osm_impact_service()
    result = await service.scan(
        product_id, alert.polygon, event_name=alert.event_name, force=force
    )

    if push and result.get("total", 0) > 0:
        broker = get_message_broker()
        await broker.broadcast_impact_places(result)

    return result


@app.post("/api/impact/clear")
async def clear_impact_overlay():
    """Hide the impact panel on all stream widgets."""
    broker = get_message_broker()
    await broker.broadcast_impact_clear()
    return {"success": True}


@app.post("/api/alerts/{product_id}/focus")
async def focus_alert_on_map(product_id: str):
    """Tell map clients (the radar app) to zoom to and flash this alert.

    Broadcasts the full alert dict so clients have the polygon, centroid, and
    detail fields without another lookup.
    """
    alert_manager = get_alert_manager()
    alert = alert_manager.get_alert(product_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    broker = get_message_broker()
    await broker.broadcast_focus_alert(alert.to_dict())
    return {"success": True}


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


@app.get("/api/event-stats")
async def get_event_stats(window: str = "session"):
    """Get event statistics. window: 'session' | '24h' | '7d'"""
    try:
        from .services.event_stats_service import get_event_stats_service
    except ImportError:
        from backend.services.event_stats_service import get_event_stats_service
    try:
        from .services.lsr_service import get_lsr_service
    except ImportError:
        from backend.services.lsr_service import get_lsr_service

    event_stats = get_event_stats_service()
    lsr_svc = get_lsr_service()

    lsr_hours = 168 if window == "7d" else 24

    try:
        settings = get_settings()
        all_reports = await lsr_svc.fetch_reports(
            states=settings.filter_states, hours=lsr_hours
        )
        lsr_by_type: dict[str, int] = {}
        max_hail_lsr: Optional[float] = None
        max_wind_lsr: Optional[float] = None
        for r in all_reports:
            rtype = getattr(r, "report_type", "") or ""
            lsr_by_type[rtype] = lsr_by_type.get(rtype, 0) + 1
            if "hail" in rtype.lower():
                mag = getattr(r, "magnitude", None)
                if mag:
                    try:
                        v = float(str(mag).replace('"', "").strip())
                        if max_hail_lsr is None or v > max_hail_lsr:
                            max_hail_lsr = v
                    except (ValueError, TypeError):
                        pass
            if "wind" in rtype.lower():
                mag = getattr(r, "magnitude", None)
                if mag:
                    try:
                        v = float(str(mag).replace("mph", "").replace("MPH", "").strip())
                        if max_wind_lsr is None or v > max_wind_lsr:
                            max_wind_lsr = v
                    except (ValueError, TypeError):
                        pass
        lsr_summary = {
            "total": len(all_reports),
            "by_type": lsr_by_type,
            "max_hail_in": max_hail_lsr,
            "max_wind_mph": max_wind_lsr,
            "tornado_reports": sum(v for k, v in lsr_by_type.items() if "tornado" in k.lower()),
            "hail_reports": sum(v for k, v in lsr_by_type.items() if "hail" in k.lower()),
            "wind_reports": sum(v for k, v in lsr_by_type.items() if "wind" in k.lower()),
        }
    except Exception as e:
        logger.warning(f"Could not fetch LSR stats: {e}")
        lsr_summary = {}

    if window == "24h":
        return event_stats.compute_historical_stats(24, lsr_stats=lsr_summary)
    elif window == "7d":
        return event_stats.compute_historical_stats(168, lsr_stats=lsr_summary)
    else:
        return event_stats.get_stats(lsr_stats=lsr_summary)


@app.post("/api/event-stats/reset")
async def reset_event_stats():
    """Reset event statistics to start a new event session."""
    try:
        from .services.event_stats_service import get_event_stats_service
    except ImportError:
        from backend.services.event_stats_service import get_event_stats_service

    event_stats = get_event_stats_service()
    event_stats.reset()
    return {"success": True, "message": "Event session reset", "session_start": event_stats.session_start.isoformat()}


async def build_map_zones() -> dict:
    """
    Build zone-based map data for rendering.

    Returns individual zone geometries with their highest priority alert,
    allowing the map to render each zone only once with the correct color.

    Shared by the GET /api/map/zones endpoint (polling) and the WebSocket
    `alert_zones` push (broadcast the instant an alert changes), so both paths
    produce an identical payload.
    """
    from .services.zone_geometry_service import get_zone_geometry_service

    alert_manager = get_alert_manager()
    zone_service = get_zone_geometry_service()
    alerts = alert_manager.get_alerts_sorted(by_priority=True)

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

        # Any alert with its own polygon (storm-based warnings: TOR, SVR, FFW, FL.W
        # with LAT...LON, SPS, etc.) renders as a polygon overlay on the frontend
        # — skip it from zone fills to prevent visual overlap with the broader
        # county/zone shape. Watches (significance A) always use zone fills because
        # their "polygon" is a convective outline box, not a precise warning area.
        sig = alert.significance.value if hasattr(alert.significance, 'value') else alert.significance
        if alert.polygon and sig != 'A':
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


@app.get("/api/map/zones")
async def get_map_zones():
    """Get zone-based map data for rendering (polling endpoint)."""
    return await build_map_zones()


# Current on-air radar product, shown by the stream's upper-third overlay.
# Driven by the AutoHotkey passthrough hotkeys (same keys that switch the radar
# app), so the label always matches what's on screen.
_current_radar_product = "reflectivity"


@app.get("/api/stream/radar-product")
async def stream_radar_product(value: Optional[str] = Query(None, alias="set")):
    """
    Get or set the on-air radar product label for the stream overlay.

    - `GET /api/stream/radar-product`            → current product
    - `GET /api/stream/radar-product?set=velocity` → set it + broadcast to overlays

    GET-to-set keeps it trivial to call from AutoHotkey / curl on localhost.
    """
    global _current_radar_product
    if value is not None:
        _current_radar_product = value.strip().lower()
        await get_message_broker().broadcast_radar_product(_current_radar_product)
    return {"product": _current_radar_product}


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


@app.get("/api/brand")
async def get_brand():
    """Get active brand configuration for the frontend + radar app (white-label)."""
    settings = get_settings()
    brand = get_brand_config(settings.brand)
    return {
        "name": brand.name,
        "short_name": brand.short_name,
        "tagline": brand.tagline,
        "logo": brand.logo,
        "logo_url": "/api/brand/logo",  # the radar app loads the active brand logo here
        "logo_is_wordmark": brand.logo_is_wordmark,
        "colors": brand.colors.model_dump(),  # semantic palette; clients map to their own CSS vars
        "website_url": brand.website_url,
        "social_twitter": brand.social_twitter,
        "css_overrides": brand.css_overrides,
    }


@app.get("/api/brand/logo")
async def get_brand_logo():
    """Serve the active brand's logo (white-label), with default/TBF fallback."""
    settings = get_settings()
    brand = get_brand_config(settings.brand)
    path = brand.get_asset_path(brand.logo, Path("config/brands"))
    if path.exists():
        return FileResponse(path)
    fallback = FRONTEND_DIR / "tbf_logo.png"
    if fallback.exists():
        return FileResponse(fallback, media_type="image/png")
    raise HTTPException(status_code=404, detail="Brand logo not found")


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


@app.get("/api/proxy/spc-image")
async def proxy_spc_image(
    url: str = Query(..., description="Full SPC image URL"),
):
    """Proxy generic SPC images (like day1otlk.gif) to bypass hotlink protection."""
    from fastapi.responses import Response
    
    if not url.startswith("https://www.spc.noaa.gov/"):
        raise HTTPException(status_code=400, detail="Only spc.noaa.gov URLs allowed")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "Referer": "https://www.spc.noaa.gov/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                },
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail=f"SPC returned {resp.status}")
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/gif")
        return Response(content=data, media_type=content_type, headers={"Cache-Control": "public, max-age=300"})
    except Exception as e:
        logger.error(f"SPC image proxy fetch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch from SPC: {e}")




@app.get("/api/proxy/mesoanalysis")
async def proxy_mesoanalysis(
    sector: str = Query("19", description="Numeric sector code (19=CONUS, 16=NE, etc.)"),
    param: str = Query(..., description="Parameter code (scp, stpc, ...)"),
):
    """
    Proxy SPC Mesoanalysis images to avoid hotlink/CORS blocking.
    Fetches from spc.noaa.gov and returns the image directly.
    """
    import re
    import io
    from fastapi.responses import Response

    # Validate inputs to prevent SSRF — sector is 1-2 digits, param is alphanumeric
    if not re.fullmatch(r"\d{1,2}", sector) or not re.fullmatch(r"[a-z0-9]{1,8}", param):
        raise HTTPException(status_code=400, detail="Invalid sector or param")

    spc_url = f"https://www.spc.noaa.gov/exper/mesoanalysis/s{sector}/{param}/{param}.gif"
    logger.debug(f"Proxying mesoanalysis: {spc_url}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                spc_url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "Referer": "https://www.spc.noaa.gov/",
                    "User-Agent": "Mozilla/5.0 (compatible; AlertDashboard/2.0)",
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"SPC mesoanalysis returned {resp.status} for {spc_url}")
                    raise HTTPException(status_code=502, detail=f"SPC returned {resp.status}")
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/gif")
        return Response(content=data, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=300"})
    except aiohttp.ClientError as e:
        logger.error(f"Mesoanalysis proxy fetch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch from SPC: {e}")


@app.get("/api/lsr/summary-graphic")
async def get_lsr_summary_graphic(
    hours: int = Query(24, ge=1, le=168, description="Lookback hours"),
    title: Optional[str] = Query(None, description="Graphic title override"),
    save: bool = Query(False, description="Also save to alert graphics gallery"),
):
    """
    Generate a server-side rendered LSR damage survey summary graphic (PNG).

    Returns a PNG image showing storm report markers on a coordinate map
    with a stats panel. Suitable for recap posts and on-stream display.
    """
    from fastapi.responses import StreamingResponse
    import io

    try:
        from .services.lsr_graphic_service import generate_lsr_summary_graphic
    except ImportError:
        from backend.services.lsr_graphic_service import generate_lsr_summary_graphic

    lsr_service = get_lsr_service()
    settings = get_settings()
    brand = get_brand_config(settings.brand)

    reports = await lsr_service.fetch_reports(states=settings.filter_states, hours=hours)

    if title is None:
        title = f"Storm Report Summary — {', '.join(settings.filter_states) if settings.filter_states else 'All States'}"

    try:
        png_bytes = generate_lsr_summary_graphic(
            reports=reports,
            title=title,
            hours=hours,
            brand_name=brand.name,
            states=settings.filter_states or None,
        )
    except Exception as e:
        logger.exception(f"LSR summary graphic generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Graphic generation failed: {e}")

    if save:
        import re as _re
        import json as _json
        from datetime import datetime, timezone
        _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_id = f"lsr_summary_{ts}"
        img_path = _GRAPHICS_DIR / f"{safe_id}.png"
        img_path.write_bytes(png_bytes)
        meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
        meta_path.write_text(_json.dumps({"product_id": safe_id, "event_name": title}))
        logger.info(f"Saved LSR summary graphic: {safe_id}.png")

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="lsr_summary.png"'},
    )


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
# Unified traffic cameras (OHGO snapshots + 511-family live HLS)
# =============================================================================

@app.get("/api/cameras")
async def get_all_cameras(
    refresh: bool = Query(False, description="Force refresh from upstream APIs"),
):
    """
    Unified traffic-camera list for the radar app.

    Merges OHGO (Ohio, 5s JPEG snapshots), the 511-family states, and the CARS
    GraphQL states (CO/IN/IA/KS/MA/MN/NE) — both with live HLS video. Each camera
    carries image_url and/or video_url so the app can show a refreshing snapshot
    or play the live stream (hls.js) directly.
    """
    odot_service = get_odot_service()
    svc_511 = get_511_service()
    svc_cars = get_cars_service()
    odot_cams, cams_511, cams_cars = await asyncio.gather(
        odot_service.fetch_cameras(force_refresh=refresh),
        svc_511.fetch_all(force_refresh=refresh),
        svc_cars.fetch_all(force_refresh=refresh),
    )

    cameras: list[dict] = []
    for c in odot_cams:
        cameras.append({
            "id": f"oh-{c.id}",
            "source": "OHGO",
            "state": "OH",
            "location": c.location,
            "latitude": c.latitude,
            "longitude": c.longitude,
            "image_url": c.image_url,
            "video_url": "",
            "description": c.description,
        })
    cameras.extend(c.to_dict() for c in cams_511)
    cameras.extend(c.to_dict() for c in cams_cars)

    return {"count": len(cameras), "cameras": cameras}


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
        # CIG (Conditional Intensity Group) overlays
        cig_torn = await spc_service.fetch_outlook("day1_cigtorn", force_refresh=refresh)
        cig_wind = await spc_service.fetch_outlook("day1_cigwind", force_refresh=refresh)
        cig_hail = await spc_service.fetch_outlook("day1_cighail", force_refresh=refresh)

        result["tornado"] = tornado.to_dict() if tornado else None
        result["wind"] = wind.to_dict() if wind else None
        result["hail"] = hail.to_dict() if hail else None
        result["cig_tornado"] = cig_torn.to_dict() if cig_torn and cig_torn.polygons else None
        result["cig_wind"] = cig_wind.to_dict() if cig_wind and cig_wind.polygons else None
        result["cig_hail"] = cig_hail.to_dict() if cig_hail and cig_hail.polygons else None

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
# ASOS / METAR Surface Observations Endpoints
# =============================================================================

try:
    from .services.asos_service import get_asos_service
except ImportError:
    from backend.services.asos_service import get_asos_service


@app.get("/api/asos/observations")
async def get_asos_observations(
    states: Optional[str] = Query(None, description="Comma-separated state codes, e.g. OH,IN. Defaults to filter_states."),
    hours: int = Query(1, ge=1, le=3, description="Lookback hours (1-3)"),
    force_refresh: bool = Query(False),
):
    """
    Fetch latest ASOS surface observations for configured or specified states.

    Returns the most recent observation per station (temp, dewpoint, wind, visibility, sky).
    """
    state_list = None
    if states:
        state_list = [s.strip().upper() for s in states.split(",") if s.strip()]

    svc = get_asos_service()
    obs = await svc.fetch_observations(states=state_list, hours=hours, force_refresh=force_refresh)
    by_state = svc.get_by_state(obs)

    return {
        "count": len(obs),
        "states": list(by_state.keys()),
        "by_state": {
            state: [o.to_dict() for o in state_obs]
            for state, state_obs in by_state.items()
        },
        "observations": [o.to_dict() for o in obs],
    }


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


@app.get("/api/afd/{office}/headlines")
async def get_afd_headlines(
    office: str,
    count: int = Query(4, ge=1, le=6, description="Number of headlines to extract"),
):
    """Extract weather headlines from an AFD for social media graphics."""
    service = get_nwws_products_service()

    # Try NWWS cache first
    afd = service.get_afd(office)

    # Fallback to API
    if not afd:
        afd = await service.fetch_afd_from_api(office)

    if not afd:
        raise HTTPException(
            status_code=404,
            detail=f"No AFD available for office '{office.upper()}'"
        )

    headlines = await service.extract_headlines_llm(afd, max_headlines=count)

    return {
        "office": afd.get("office", office.upper()),
        "wfo_name": afd.get("wfo_name", ""),
        "received_at": afd.get("received_at", ""),
        "headlines": headlines,
    }


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
# AI Agent Endpoints (Tool-Calling Agent)
# =============================================================================


@app.get("/api/agent/status")
async def agent_status():
    """Get AI agent status and availability."""
    settings = get_settings()

    if not settings.agent_enabled:
        return {
            "enabled": False,
            "available": False,
            "model": settings.agent_model,
        }

    agent = get_agent_service()
    is_available = await agent.check_health()

    return {
        "enabled": True,
        "available": is_available,
        **agent.get_status(),
    }


@app.post("/api/agent/chat")
async def agent_chat(request: Request):
    """
    Send a message to the AI agent with tool-calling capabilities.

    The agent can use weather tools to query real-time data before responding.
    Returns the response along with a log of all tool calls made.
    """
    settings = get_settings()

    if not settings.agent_enabled:
        raise HTTPException(status_code=503, detail="AI agent is disabled")

    agent = get_agent_service()
    is_available = await agent.check_health()
    if not is_available:
        raise HTTPException(
            status_code=503,
            detail="AI agent not available. Make sure Ollama is running with the agent model."
        )

    body = await request.json()
    message = body.get("message", "").strip()
    include_history = body.get("include_history", True)

    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    try:
        response = await agent.run(message, include_history=include_history)
        return {
            "success": True,
            "response": response.content,
            "tool_calls": [
                {
                    "tool": tc.tool,
                    "arguments": tc.arguments,
                    "result": tc.result,
                    "status": tc.status,
                    "duration_ms": tc.duration_ms,
                }
                for tc in response.tool_calls
            ],
            "rounds": response.rounds,
            "model": response.model,
            "duration_ms": response.total_duration_ms,
        }
    except Exception as e:
        logger.exception(f"Agent chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/agent/tools")
async def list_agent_tools():
    """List all tools available to the AI agent."""
    agent = get_agent_service()
    return {"tools": agent.tools.list_tools()}


@app.get("/api/agent/history")
async def get_agent_history():
    """Get agent conversation history."""
    agent = get_agent_service()
    return {"history": agent.get_history()}


@app.delete("/api/agent/history")
async def clear_agent_history():
    """Clear agent conversation history."""
    agent = get_agent_service()
    agent.clear_history()
    return {"success": True, "message": "Agent history cleared"}


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


# ==================== NEXRAD Radar Endpoints ====================


class RadarSiteRequest(BaseModel):
    """Request body for setting active radar site."""
    site_id: str


@app.get("/api/radar/status")
async def get_radar_status():
    """Get current NEXRAD radar service status."""
    settings = get_settings()
    if not settings.nexrad_enabled:
        return {"enabled": False, "active_site": None, "active_sites": [], "last_update": None, "processing": False}
    svc = get_nexrad_service()
    if not svc:
        return {"enabled": False, "active_site": None, "active_sites": [], "last_update": None, "processing": False}
    return svc.status.to_dict()


@app.get("/api/radar/chunks/diagnostic")
async def get_radar_chunks_diagnostic():
    """Per-site chunks-bucket pipeline diagnostics.

    Empty diagnostics block if the chunks path is disabled — no error.
    """
    settings = get_settings()
    if not settings.nexrad_chunks_enabled:
        return {
            "enabled": False,
            "reason": "nexrad_chunks_enabled = false",
            "diagnostics": {},
        }
    try:
        from .services.nexrad_chunks_service import get_nexrad_chunks_service
        svc = get_nexrad_chunks_service()
    except Exception as e:
        return {"enabled": False, "reason": f"load error: {e}", "diagnostics": {}}
    if svc is None:
        return {"enabled": False, "reason": "service not started", "diagnostics": {}}
    return {
        "enabled":        True,
        "poll_interval_s": svc._poll_interval,
        "min_partial":    svc._min_partial,
        "render_on_complete": svc._render_on_complete,
        "now_utc":        datetime.now(timezone.utc).isoformat(),
        "diagnostics":    svc.diagnostics,
    }


@app.get("/api/radar/diagnostic")
async def get_radar_diagnostic():
    """Per-site pipeline diagnostics for latency debugging.

    Returns, per active site, the latest scan we're showing, the latest scan
    available on S3, and a breakdown of stage timings (list/download/parse/
    dealias/render/grid).  When complaints come in like "the scan is 12 min
    old" this endpoint tells you whether the upstream is slow, the network
    is slow, or our pipeline is slow.
    """
    settings = get_settings()
    if not settings.nexrad_enabled:
        return {"enabled": False, "active_sites": [], "diagnostics": {}}
    svc = get_nexrad_service()
    if not svc:
        return {"enabled": False, "active_sites": [], "diagnostics": {}}

    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)

    # VCP-duration estimates from NEXRAD VCP catalog.  Used to compute the
    # archive-bucket "irreducible floor": you cannot show a scan before the
    # last tilt has been collected.  Tilt count is a coarse proxy because
    # SAILS/MRLE inject extra low-level passes, but it's good enough to tell
    # the user whether their lag is pipeline-induced or VCP-induced.
    def _vcp_duration_seconds(tilt_count):
        if not tilt_count or tilt_count <= 0:
            return None
        if tilt_count <= 5:   return 600   # VCP 31/32 clear-air (10 min)
        if tilt_count <= 7:   return 420   # VCP 35 clear-air (7 min)
        if tilt_count <= 9:   return 360   # VCP 21/121 (6 min)
        if tilt_count <= 14:  return 270   # VCP 12/211/212 base (4.5 min)
        if tilt_count <= 15:  return 360   # VCP 215 (6 min)
        if tilt_count <= 17:  return 300   # 212/215 + SAILSx3 (~5 min)
        return 360                          # severe + SAILS + MRLE, ~6 min

    diag_out = {}
    for site, d in svc.diagnostics.items():
        entry = dict(d)
        # Recompute "live" ages so they reflect now, not when diag was written
        showing = entry.get("showing_scan_ts")
        if showing:
            try:
                entry["showing_age_s_live"] = round(
                    (now - _dt.fromisoformat(showing)).total_seconds(), 1
                )
            except (ValueError, TypeError):
                pass
        s3_ts = entry.get("latest_available_ts")
        if s3_ts:
            try:
                entry["latest_available_age_s_live"] = round(
                    (now - _dt.fromisoformat(s3_ts)).total_seconds(), 1
                )
            except (ValueError, TypeError):
                pass

        # Latency budget breakdown — surfaces whether the user's pipeline
        # overhead is meaningful relative to the unavoidable VCP duration.
        tilts = entry.get("last_tilt_count")
        vcp_s = _vcp_duration_seconds(tilts)
        if vcp_s is not None:
            entry["vcp_estimated_duration_s"] = vcp_s
            # Archive floor ≈ VCP duration + S3 propagation (30s) + poll discovery
            poll_int = getattr(svc, "_poll_interval", 10) or 10
            entry["archive_bucket_floor_s"] = vcp_s + 30 + poll_int
            # How much we add on top of the floor
            stage_sum = sum(
                float(entry.get(k, 0) or 0)
                for k in (
                    "last_list_duration_s",
                    "last_download_duration_s",
                    "last_parse_duration_s",
                    "last_dealias_duration_s",
                    "last_render_duration_s",
                )
            )
            entry["pipeline_overhead_s"] = round(stage_sum, 2)
            # Headline number: if showing_age_s_live ≈ archive_bucket_floor_s,
            # the lag is the radar, not us.  If it's much higher, we've got
            # work to do.
            if "showing_age_s_live" in entry:
                entry["excess_over_archive_floor_s"] = round(
                    entry["showing_age_s_live"] - entry["archive_bucket_floor_s"], 1
                )

        diag_out[site] = entry
    return {
        "enabled":        True,
        "active_sites":   svc.active_sites,
        "poll_interval_s": getattr(svc, "_poll_interval", None),
        "now_utc":        now.isoformat(),
        "diagnostics":    diag_out,
    }


@app.get("/api/radar/sites")
async def get_radar_sites(lat: Optional[float] = None, lon: Optional[float] = None):
    """Get list of NEXRAD radar sites, optionally sorted by distance from a point."""
    if lat is not None and lon is not None:
        return get_nearest_sites(lat, lon, count=20)
    # Return all sites
    sites = []
    for site_id, info in NEXRAD_SITES.items():
        sites.append({
            "id": site_id,
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "state": info["state"],
        })
    sites.sort(key=lambda s: (s["state"], s["name"]))
    return sites


@app.get("/api/radar/frame/{product}")
async def get_radar_frame(product: str):
    """Get the latest radar frame(s) for a product — one per active site."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    from .services.nexrad_service import RADAR_PRODUCTS as _RP
    if product not in _RP:
        raise HTTPException(status_code=400, detail=f"Unknown product: {product}")

    frames = svc.get_latest_frames_for_product(product)
    return [f.to_dict() for f in frames]


@app.get("/api/radar/frames/{product}")
async def get_radar_frame_history(product: str, count: int = 10, site: str | None = None):
    """Get frame history for animation. Optional ?site= for a specific active site."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    from .services.nexrad_service import RADAR_PRODUCTS as _RP
    if product not in _RP:
        raise HTTPException(status_code=400, detail=f"Unknown product: {product}")

    frames = svc.get_frame_history(product, count, site=site)
    return [f.to_dict() for f in frames]


@app.post("/api/radar/site")
async def set_radar_site(request: RadarSiteRequest):
    """Replace all active sites with one site (backward compat)."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    try:
        await svc.set_active_site(request.site_id)
        return {"status": "ok", "active_sites": svc.active_sites}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/radar/sites/add")
async def add_radar_site(request: RadarSiteRequest):
    """Add a site to the active radar set (max 3)."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    try:
        await svc.add_site(request.site_id)
        return {"status": "ok", "active_sites": svc.active_sites}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/radar/sites/remove")
async def remove_radar_site(request: RadarSiteRequest):
    """Remove a site from the active radar set."""
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not enabled")

    try:
        await svc.remove_site(request.site_id)
        return {"status": "ok", "active_sites": svc.active_sites}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/radar/systems")
async def get_mcs_systems():
    """Get all currently detected MCS/QLCS systems."""
    svc = get_storm_tracking_service()
    if not svc:
        return []
    return [s.to_dict() for s in svc.tracked_systems]


@app.get("/api/radar/cells")
async def get_storm_cells():
    """Get all currently tracked storm cells."""
    svc = get_storm_tracking_service()
    if not svc:
        return []
    return [c.to_dict() for c in svc.tracked_cells]


@app.get("/api/radar/cell/{cell_id}")
async def get_storm_cell(cell_id: str):
    """Get detailed info for a specific storm cell."""
    svc = get_storm_tracking_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Storm tracking service not enabled")

    cell = svc.get_cell(cell_id)
    if not cell:
        raise HTTPException(status_code=404, detail=f"Cell not found: {cell_id}")
    return cell.to_dict()


# Serve radar images as static files (legacy oneshot_frame / social graphics)
@app.get("/api/radar/images/{site}/{filename}")
async def serve_radar_image(site: str, filename: str):
    """Serve rendered radar images (WebP or PNG) — used by social graphic pipeline."""
    settings = get_settings()
    image_path = Path(settings.data_dir) / "radar" / site / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    media_type = "image/webp" if filename.endswith(".webp") else "image/png"
    return FileResponse(
        str(image_path),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/radar/cached/{site}")
async def get_cached_radar_frame(site: str):
    """Return the latest cached reflectivity binary for any NEXRAD site.

    Returns 404 if the site has never been loaded this session.  The frontend
    uses this to show a stale-but-instant frame when switching sites while
    the fresh download runs in the background.
    """
    from fastapi.responses import Response as FastResponse
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not running")
    binary = svc.get_cached_frame(site)
    if not binary:
        raise HTTPException(status_code=404, detail="No cached frame for this site")
    return FastResponse(
        content=binary,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/radar/binary/{site}/{product}/{frame_id}")
async def get_radar_binary_frame(site: str, product: str, frame_id: str):
    """Return a cached binary radar frame by ID (RDRF wire format) for history scrubber."""
    from fastapi.responses import Response as FastResponse
    svc = get_nexrad_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Radar service not running")
    frame = svc.get_frame_by_id(site, product, frame_id)
    if not frame or not frame.binary_data:
        raise HTTPException(status_code=404, detail="Frame not found")
    return FastResponse(
        content=frame.binary_data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/mrms/status")
async def get_mrms_status():
    """MRMS service status — also tells the frontend whether pygrib is available."""
    svc = get_mrms_service()
    if not svc:
        return {"available": False, "reason": "service not started"}
    return {
        "available": svc.available,
        "has_data":  svc.latest_png is not None,
        "timestamp": svc.latest_timestamp,
        "reason":    None if svc.available else "install pygrib via: conda install -c conda-forge eccodes pygrib",
    }


@app.get("/api/mrms/frames")
async def list_mrms_frames():
    """List all cached MRMS frames (oldest first, up to 30 = ~60 min)."""
    svc = get_mrms_service()
    if not svc or not svc.available:
        raise HTTPException(status_code=503, detail="MRMS service not available")
    return svc.get_frame_list()


@app.get("/api/mrms/frame/{ts}")
async def get_mrms_frame_by_ts(ts: str):
    """Return a specific MRMS frame binary by timestamp (YYYYMMDD-HHMMSS)."""
    from fastapi.responses import Response as FastResponse
    svc = get_mrms_service()
    if not svc or not svc.available:
        raise HTTPException(status_code=503, detail="MRMS service not available")
    binary = svc.get_frame_binary(ts)
    if not binary:
        raise HTTPException(status_code=404, detail=f"Frame {ts} not in cache")
    return FastResponse(
        content=binary,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/mrms/binary")
async def get_mrms_binary():
    """Return the latest MRMS grid as a compact binary for the WebGL custom layer."""
    from fastapi.responses import Response as FastResponse
    svc = get_mrms_service()
    if not svc or not svc.available or svc.latest_binary is None:
        raise HTTPException(status_code=503, detail="MRMS binary not available")
    return FastResponse(
        content=svc.latest_binary,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/mrms/latest.png")
async def get_mrms_png():
    """Latest MRMS composite reflectivity as a RGBA PNG (CONUS, ~1.5 MB)."""
    from fastapi.responses import Response as FastResponse
    svc = get_mrms_service()
    if not svc or not svc.available or svc.latest_png is None:
        raise HTTPException(status_code=503, detail="MRMS data not available")
    return FastResponse(
        content=svc.latest_png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/hrrr/sounding.png")
async def get_hrrr_sounding(lat: float, lon: float):
    """Full SHARPpy/SounderPy HRRR F00 point sounding (nearest BUFKIT site) as PNG.

    Heavy (~30 s first render for a site; cached per-site after) — the radar app
    shows an instant quick-look while this builds.
    """
    from fastapi.responses import Response as FastResponse
    from .services.hrrr_service import get_hrrr_service

    svc = get_hrrr_service()
    loop = asyncio.get_event_loop()
    try:
        png, station = await loop.run_in_executor(None, svc.get_sounding_png, lat, lon)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"HRRR sounding unavailable: {e}")
    return FastResponse(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Bufkit-Station": station},
    )


@app.get("/api/glm/status")
async def get_glm_status():
    """Debug endpoint: check GLM lightning service and probe S3 directly."""
    from datetime import datetime, timedelta, timezone as tz
    svc = get_glm_service()
    if not svc:
        return {"running": False, "reason": "service not started"}

    # Probe S3 at multiple levels to find where data actually lives
    s3_results = {}
    s3_error = None
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED), region_name="us-east-1")
        now = datetime.now(tz.utc)
        doy = now.timetuple().tm_yday

        # Level 1: top-level GLM prefix in noaa-goes18
        r = s3.list_objects_v2(Bucket="noaa-goes18", Prefix="GLM-L2-LCFA/", Delimiter="/", MaxKeys=5)
        s3_results["top_level"] = {
            "prefixes": [p["Prefix"] for p in r.get("CommonPrefixes", [])],
            "key_count": r.get("KeyCount", 0),
        }

        # Level 2: year/doy
        r2 = s3.list_objects_v2(Bucket="noaa-goes18", Prefix=f"GLM-L2-LCFA/{now.year}/{doy:03d}/", Delimiter="/", MaxKeys=5)
        s3_results["day_level"] = {
            "prefix": f"GLM-L2-LCFA/{now.year}/{doy:03d}/",
            "prefixes": [p["Prefix"] for p in r2.get("CommonPrefixes", [])],
            "key_count": r2.get("KeyCount", 0),
        }

        # Level 3: current hour files
        for hour_offset in [0, -1, -2]:
            t = now + timedelta(hours=hour_offset)
            prefix = f"GLM-L2-LCFA/{t.year}/{doy:03d}/{t.hour:02d}/"
            r3 = s3.list_objects_v2(Bucket="noaa-goes18", Prefix=prefix, MaxKeys=3)
            objects = r3.get("Contents", [])
            s3_results[f"hour_{t.hour:02d}"] = {
                "prefix": prefix,
                "key_count": r3.get("KeyCount", 0),
                "sample": objects[-1]["Key"] if objects else None,
            }
            if objects:
                break

    except Exception as e:
        s3_error = str(e)

    return {
        "running": svc._running,
        "last_file": svc._last_file_key,
        "flash_window_count": len(svc._flashes),
        "s3_probe": s3_results,
        "s3_error": s3_error,
    }


@app.get("/api/radar/gate")
async def get_radar_gate():
    """Get current reflectivity gate threshold (dBZ)."""
    service = get_nexrad_service()
    return {"gate_dbz": service._gate_dbz}


class RadarGateRequest(BaseModel):
    gate_dbz: float = Field(..., ge=-20, le=40, description="Reflectivity gate threshold in dBZ")


@app.post("/api/radar/gate")
async def set_radar_gate(request: RadarGateRequest):
    """Set reflectivity gate threshold and re-render current scan."""
    service = get_nexrad_service()
    await service.set_gate_dbz(request.gate_dbz)
    return {"gate_dbz": service._gate_dbz}


# ==================== Social Media Endpoints ====================


class SocialPostRequest(BaseModel):
    """Request body for posting to social media."""
    platforms: list[str]
    message: str
    images: list[str] = []  # Base64-encoded images
    alt_text: str = "Weather graphic from The Battin Front"


class GenerateTextRequest(BaseModel):
    """Request body for generating post text from alert/LSR data."""
    source_type: str  # "alert" or "lsr"
    source_id: Optional[str] = None
    source_data: Optional[dict] = None
    template: str = "default"


@app.get("/api/social/status")
async def social_media_status():
    """Check if social media services are configured and available."""
    service = get_social_media_service()
    return service.get_status()


@app.post("/api/social/post")
async def social_media_post(request: SocialPostRequest):
    """Post to one or more social media platforms."""
    service = get_social_media_service()

    # Decode base64 images to bytes
    images = None
    if request.images:
        import base64 as b64
        images = []
        for img_str in request.images:
            # Strip data URI prefix if present
            if ";base64," in img_str:
                img_str = img_str.split(";base64,", 1)[1]
            images.append(b64.b64decode(img_str))

    result = await service.post(
        platforms=request.platforms,
        message=request.message,
        images=images,
        alt_text=request.alt_text,
    )
    return result


@app.post("/api/social/generate-text")
async def social_media_generate_text(request: GenerateTextRequest):
    """Generate post text from alert or LSR data using templates."""
    service = get_social_media_service()

    if request.source_type == "alert":
        if request.source_id:
            alert_manager = get_alert_manager()
            alert = alert_manager.get_alert(request.source_id)
            if not alert:
                raise HTTPException(status_code=404, detail="Alert not found")
            text = service.generate_alert_text(alert.to_dict(), request.template)
        elif request.source_data:
            text = service.generate_alert_text(request.source_data, request.template)
        else:
            raise HTTPException(status_code=400, detail="source_id or source_data required")
    elif request.source_type == "lsr":
        if not request.source_data:
            raise HTTPException(status_code=400, detail="source_data required for LSR")
        reports = request.source_data.get("reports", [request.source_data])
        text = service.generate_lsr_text(reports, request.template)
    else:
        raise HTTPException(status_code=400, detail="source_type must be 'alert' or 'lsr'")

    return {"text": text, "template": request.template}


@app.get("/api/social/history")
async def social_media_history():
    """Get recent post history."""
    service = get_social_media_service()
    return {"posts": service.get_post_history()}


@app.get("/api/social/templates")
async def social_media_templates():
    """Get available post templates."""
    try:
        from .services.social_media.templates import ALERT_TEMPLATES, LSR_TEMPLATES
    except ImportError:
        from backend.services.social_media.templates import ALERT_TEMPLATES, LSR_TEMPLATES
    return {
        "alert_templates": list(ALERT_TEMPLATES.keys()),
        "lsr_templates": list(LSR_TEMPLATES.keys()),
    }


# ==================== Settings Endpoints ====================


class PhenomenaSettingsUpdate(BaseModel):
    """Request model for updating phenomena settings."""
    target_phenomena: list[str] = Field(..., description="List of phenomenon codes to enable")


class TickerFilterUpdate(BaseModel):
    """Request model for updating ticker excluded alert types."""
    excluded_types: list[str] = Field(..., description="List of phenomenon+significance keys to exclude (e.g. 'TO_A' for Tornado Watch)")


class StatesSettingsUpdate(BaseModel):
    """Request model for updating monitored states."""
    filter_states: list[str] = Field(..., description="List of US state abbreviations to monitor (e.g. ['OH', 'KS'])")


class GeneralSettingsUpdate(BaseModel):
    """Request model for updating general dashboard settings."""
    nexrad_enabled: bool | None = None
    nexrad_default_site: str | None = None
    llm_enabled: bool | None = None
    agent_enabled: bool | None = None
    google_chat_enabled: bool | None = None


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


# ==================== Ticker Filter Settings ====================


@app.get("/api/settings/ticker")
async def get_ticker_settings():
    """Get ticker filter settings — which alert types are excluded from the ticker."""
    try:
        from .config.settings import _load_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides

    overrides = _load_user_overrides()
    excluded = overrides.get("ticker_excluded_types", [])

    return {
        "excluded_types": excluded,
    }


@app.post("/api/settings/ticker")
async def update_ticker_settings(update: TickerFilterUpdate):
    """Update which alert types are excluded from the ticker."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides

    normalized = sorted(set(t.upper() for t in update.excluded_types))

    overrides = _load_user_overrides()
    overrides["ticker_excluded_types"] = normalized
    _save_user_overrides(overrides)

    logger.info(f"Ticker filter updated: {len(normalized)} types excluded")

    return {
        "success": True,
        "excluded_types": normalized,
        "message": f"{len(normalized)} alert types excluded from ticker",
    }


# ==================== States Settings ====================

# All US states + DC
_ALL_STATES = {
    "AK": "Alaska", "AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DC": "Dist. of Columbia",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "MA": "Massachusetts",
    "MD": "Maryland", "ME": "Maine", "MI": "Michigan", "MN": "Minnesota",
    "MO": "Missouri", "MS": "Mississippi", "MT": "Montana", "NC": "North Carolina",
    "ND": "North Dakota", "NE": "Nebraska", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NV": "Nevada", "NY": "New York", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VA": "Virginia", "VT": "Vermont", "WA": "Washington",
    "WI": "Wisconsin", "WV": "West Virginia", "WY": "Wyoming",
}


@app.get("/api/settings/states")
async def get_states_settings():
    """Get all US states and their current monitored/unmonitored state."""
    try:
        from .config.settings import _load_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides

    settings = get_settings()
    active_states = set(s.upper() for s in settings.filter_states)
    overrides = _load_user_overrides()

    states_list = [
        {"code": code, "name": name, "enabled": code in active_states}
        for code, name in sorted(_ALL_STATES.items())
    ]

    return {
        "states": states_list,
        "active_states": sorted(active_states),
        "using_overrides": "filter_states" in overrides,
    }


@app.post("/api/settings/states")
async def update_states_settings(update: StatesSettingsUpdate):
    """Update which states are monitored. Saves to user_settings.json and reloads."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings

    normalized = [s.upper() for s in update.filter_states if s.strip()]
    invalid = [s for s in normalized if s not in _ALL_STATES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown state codes: {', '.join(invalid)}")
    if not normalized:
        raise HTTPException(status_code=400, detail="At least one state must be selected")

    overrides = _load_user_overrides()
    overrides["filter_states"] = normalized
    _save_user_overrides(overrides)
    new_settings = reload_settings()

    logger.info(f"States updated: monitoring {len(normalized)} states")
    return {
        "success": True,
        "active_states": sorted(new_settings.filter_states),
        "message": f"Monitoring {len(normalized)} state(s)",
    }


@app.post("/api/settings/states/reset")
async def reset_states_settings():
    """Reset state filter to .env defaults."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings, _USER_SETTINGS_FILE
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings, _USER_SETTINGS_FILE

    overrides = _load_user_overrides()
    overrides.pop("filter_states", None)
    if overrides:
        _save_user_overrides(overrides)
    elif _USER_SETTINGS_FILE.exists():
        _USER_SETTINGS_FILE.unlink()
    new_settings = reload_settings()

    return {
        "success": True,
        "active_states": sorted(new_settings.filter_states),
        "message": "Reset to default states",
    }


# ==================== County Filter ====================


class CountiesSettingsUpdate(BaseModel):
    """Request model for updating per-state county filters."""
    filter_counties: dict[str, list[str]] = Field(
        ..., description="Map of state code -> list of county UGC codes to keep ([] = all)"
    )


@app.get("/api/settings/counties")
async def get_counties_settings():
    """List counties for each monitored state and the user's current selection.

    For a state with no selection (empty list), all of its counties are active.
    """
    try:
        from .config.settings import _load_user_overrides
        from .services.ugc_service import get_counties_for_state
    except ImportError:
        from backend.config.settings import _load_user_overrides
        from backend.services.ugc_service import get_counties_for_state

    settings = get_settings()
    overrides = _load_user_overrides()
    filter_counties = settings.filter_counties or {}

    states_out: dict[str, dict] = {}
    for state in sorted(settings.filter_states):
        st = state.upper()
        selected = set(filter_counties.get(st) or [])
        counties = [
            {"code": c["code"], "name": c["name"], "enabled": c["code"] in selected}
            for c in get_counties_for_state(st)
        ]
        states_out[st] = {
            "state_name": _ALL_STATES.get(st, st),
            "counties": counties,
            # No explicit selection => every county is active (no narrowing).
            "all_selected": len(selected) == 0,
        }

    return {
        "states": states_out,
        "using_overrides": "filter_counties" in overrides,
    }


@app.post("/api/settings/counties")
async def update_counties_settings(update: CountiesSettingsUpdate):
    """Update the per-state county filter. Saves to user_settings.json and reloads."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings

    # Normalize and drop states whose selection is empty (empty = all counties).
    normalized: dict[str, list[str]] = {}
    for state, codes in update.filter_counties.items():
        cleaned = [c.strip().upper() for c in codes if c and c.strip()]
        if cleaned:
            normalized[state.upper()] = sorted(set(cleaned))

    overrides = _load_user_overrides()
    if normalized:
        overrides["filter_counties"] = normalized
    else:
        overrides.pop("filter_counties", None)
    _save_user_overrides(overrides)
    new_settings = reload_settings()

    total = sum(len(v) for v in new_settings.filter_counties.values())
    logger.info(f"County filter updated: {len(new_settings.filter_counties)} state(s), {total} counties")
    return {
        "success": True,
        "filter_counties": new_settings.filter_counties,
        "message": (
            f"County filter active for {len(new_settings.filter_counties)} state(s)"
            if new_settings.filter_counties else "County filter cleared (all counties)"
        ),
    }


@app.post("/api/settings/counties/reset")
async def reset_counties_settings():
    """Clear the county filter (monitor all counties in each state)."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings, _USER_SETTINGS_FILE
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings, _USER_SETTINGS_FILE

    overrides = _load_user_overrides()
    overrides.pop("filter_counties", None)
    if overrides:
        _save_user_overrides(overrides)
    elif _USER_SETTINGS_FILE.exists():
        _USER_SETTINGS_FILE.unlink()
    reload_settings()

    return {"success": True, "message": "County filter cleared (all counties)"}


# ==================== General Settings ====================


@app.get("/api/settings/general")
async def get_general_settings():
    """Get general dashboard settings (NEXRAD site, LLM/agent toggles, etc.)."""
    try:
        from .config.settings import _load_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides

    settings = get_settings()
    overrides = _load_user_overrides()
    general_keys = {"nexrad_enabled", "nexrad_default_site", "llm_enabled", "agent_enabled", "google_chat_enabled"}

    return {
        "nexrad_enabled": settings.nexrad_enabled,
        "nexrad_default_site": settings.nexrad_default_site,
        "llm_enabled": settings.llm_enabled,
        "agent_enabled": settings.agent_enabled,
        "google_chat_enabled": settings.google_chat_enabled,
        "using_overrides": bool(general_keys & set(overrides.keys())),
    }


@app.post("/api/settings/general")
async def update_general_settings(update: GeneralSettingsUpdate):
    """Update general dashboard settings. Only provided (non-None) fields are changed."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides, reload_settings
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides, reload_settings

    overrides = _load_user_overrides()
    changed = []

    if update.nexrad_enabled is not None:
        overrides["nexrad_enabled"] = update.nexrad_enabled
        changed.append("nexrad_enabled")
    if update.nexrad_default_site is not None:
        site = update.nexrad_default_site.strip().upper()
        if len(site) != 4:
            raise HTTPException(status_code=400, detail="NEXRAD site must be a 4-character ICAO code (e.g. KTWX)")
        overrides["nexrad_default_site"] = site
        changed.append("nexrad_default_site")
    if update.llm_enabled is not None:
        overrides["llm_enabled"] = update.llm_enabled
        changed.append("llm_enabled")
    if update.agent_enabled is not None:
        overrides["agent_enabled"] = update.agent_enabled
        changed.append("agent_enabled")
    if update.google_chat_enabled is not None:
        overrides["google_chat_enabled"] = update.google_chat_enabled
        changed.append("google_chat_enabled")

    if not changed:
        raise HTTPException(status_code=400, detail="No settings provided to update")

    _save_user_overrides(overrides)
    new_settings = reload_settings()

    logger.info(f"General settings updated: {', '.join(changed)}")
    return {
        "success": True,
        "nexrad_enabled": new_settings.nexrad_enabled,
        "nexrad_default_site": new_settings.nexrad_default_site,
        "llm_enabled": new_settings.llm_enabled,
        "agent_enabled": new_settings.agent_enabled,
        "google_chat_enabled": new_settings.google_chat_enabled,
        "message": f"Updated: {', '.join(changed)}",
    }


# ==================== Sound Settings Endpoints ====================

_SOUND_EVENT_TYPES = {"tornado_warning", "severe_warning", "alert_update"}
_ALLOWED_SOUND_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
_SOUND_DEFAULTS = {
    "tornado_warning": {"volume": 0.8, "label": "Tornado Warning"},
    "severe_warning": {"volume": 0.7, "label": "Severe / Flash Flood Warning"},
    "alert_update": {"volume": 0.5, "label": "Alert Update"},
}


def _get_sound_config() -> dict:
    """Load sound config from user_settings.json, merging with defaults."""
    try:
        from .config.settings import _load_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides
    overrides = _load_user_overrides()
    saved = overrides.get("sounds", {})
    result = {}
    for event_type, defaults in _SOUND_DEFAULTS.items():
        entry = saved.get(event_type, {})
        custom_file = entry.get("custom_file")
        # Verify the file still exists on disk
        if custom_file and not (SOUNDS_DIR / custom_file).exists():
            custom_file = None
        result[event_type] = {
            "label": defaults["label"],
            "volume": entry.get("volume", defaults["volume"]),
            "custom_file": custom_file,
            "custom_url": f"/data/sounds/{custom_file}" if custom_file else None,
        }
    return result


@app.get("/api/settings/sounds")
async def get_sound_settings():
    """Get current sound configuration (volumes + custom file assignments)."""
    return {"sounds": _get_sound_config()}


class SoundVolumeUpdate(BaseModel):
    sounds: dict  # {event_type: {volume: float}}


@app.post("/api/settings/sounds")
async def update_sound_volumes(update: SoundVolumeUpdate):
    """Save volume levels for each sound event type."""
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides

    overrides = _load_user_overrides()
    sounds = overrides.get("sounds", {})

    for event_type, settings in update.sounds.items():
        if event_type not in _SOUND_EVENT_TYPES:
            continue
        if event_type not in sounds:
            sounds[event_type] = {}
        volume = float(settings.get("volume", sounds[event_type].get("volume", _SOUND_DEFAULTS[event_type]["volume"])))
        sounds[event_type]["volume"] = max(0.0, min(1.0, volume))

    overrides["sounds"] = sounds
    _save_user_overrides(overrides)
    return {"success": True, "sounds": _get_sound_config()}


@app.post("/api/settings/sounds/{event_type}/upload")
async def upload_sound_file(event_type: str, file: UploadFile = File(...)):
    """Upload a custom sound file for a given event type."""
    if event_type not in _SOUND_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown event type '{event_type}'. Must be one of: {', '.join(_SOUND_EVENT_TYPES)}")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SOUND_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{suffix}'. Use: {', '.join(_ALLOWED_SOUND_EXTENSIONS)}")

    dest_filename = f"{event_type}{suffix}"
    dest_path = SOUNDS_DIR / dest_filename

    try:
        with open(dest_path, "wb") as out:
            shutil.copyfileobj(file.file, out)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    # Update config
    try:
        from .config.settings import _load_user_overrides, _save_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides

    overrides = _load_user_overrides()
    sounds = overrides.get("sounds", {})
    if event_type not in sounds:
        sounds[event_type] = {"volume": _SOUND_DEFAULTS[event_type]["volume"]}
    sounds[event_type]["custom_file"] = dest_filename
    overrides["sounds"] = sounds
    _save_user_overrides(overrides)

    logger.info(f"Custom sound uploaded for '{event_type}': {dest_filename}")
    return {
        "success": True,
        "event_type": event_type,
        "custom_file": dest_filename,
        "custom_url": f"/data/sounds/{dest_filename}",
    }


@app.delete("/api/settings/sounds/{event_type}")
async def reset_sound_to_default(event_type: str):
    """Remove custom sound for an event type, reverting to synthesized chime."""
    if event_type not in _SOUND_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown event type '{event_type}'")

    try:
        from .config.settings import _load_user_overrides, _save_user_overrides
    except ImportError:
        from backend.config.settings import _load_user_overrides, _save_user_overrides

    overrides = _load_user_overrides()
    sounds = overrides.get("sounds", {})
    if event_type in sounds:
        old_file = sounds[event_type].get("custom_file")
        if old_file:
            old_path = SOUNDS_DIR / old_file
            if old_path.exists():
                old_path.unlink()
        sounds[event_type].pop("custom_file", None)
    overrides["sounds"] = sounds
    _save_user_overrides(overrides)

    return {"success": True, "event_type": event_type, "message": "Reverted to built-in chime"}


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
            "alert_card": {
                "url": "/widgets/alert-card.html",
                "description": "Alert popup card for OBS overlays",
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
# Alert Graphics
# =============================================================================

_GRAPHICS_DIR = Path(__file__).parent.parent / "data" / "alert_graphics"


@app.post("/api/alert-graphics/save")
async def save_alert_graphic(request: Request):
    """Save a generated alert graphic PNG (base64) to disk."""
    import base64 as _base64
    data = await request.json()
    product_id = data.get("product_id", "").strip()
    event_name = data.get("event_name", "")
    image_data: str = data.get("image_data", "")

    if not product_id or not image_data:
        raise HTTPException(status_code=400, detail="Missing product_id or image_data")

    # Strip data URL prefix if present
    if "," in image_data:
        image_data = image_data.split(",", 1)[1]

    try:
        img_bytes = _base64.b64decode(image_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)

    # Safe filename: replace anything that isn't alphanumeric, dash, or dot
    import re as _re
    safe_id = _re.sub(r"[^\w\-.]", "_", product_id)
    img_path = _GRAPHICS_DIR / f"{safe_id}.png"
    img_path.write_bytes(img_bytes)

    # Save metadata sidecar so we can show event_name in the gallery
    meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
    import json as _json
    meta_path.write_text(_json.dumps({"product_id": product_id, "event_name": event_name}))

    return {"status": "saved", "product_id": product_id}


@app.get("/api/alert-graphics")
async def list_alert_graphics():
    """List all saved alert graphics, newest first."""
    import json as _json
    if not _GRAPHICS_DIR.exists():
        return {"graphics": []}

    graphics = []
    for img_path in sorted(
        _GRAPHICS_DIR.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        product_id = img_path.stem
        event_name = product_id
        meta_path = img_path.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = _json.loads(meta_path.read_text())
                product_id = meta.get("product_id", product_id)
                event_name = meta.get("event_name", event_name)
            except Exception:
                pass
        graphics.append({
            "product_id": product_id,
            "event_name": event_name,
            "url": f"/api/alert-graphics/image/{img_path.name}",
            "created_at": datetime.fromtimestamp(img_path.stat().st_mtime, timezone.utc).isoformat(),
        })
    return {"graphics": graphics}


@app.get("/api/alert-graphics/image/{filename}")
async def get_alert_graphic_image(filename: str):
    """Serve a saved alert graphic PNG."""
    import re as _re
    if not _re.match(r"^[\w\-.]+\.png$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    img_path = _GRAPHICS_DIR / filename
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Graphic not found")
    return FileResponse(str(img_path), media_type="image/png")


@app.delete("/api/alert-graphics/{product_id}")
async def delete_alert_graphic(product_id: str):
    """Delete a saved alert graphic."""
    import re as _re
    safe_id = _re.sub(r"[^\w\-.]", "_", product_id)
    img_path = _GRAPHICS_DIR / f"{safe_id}.png"
    meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
    if img_path.exists():
        img_path.unlink()
    if meta_path.exists():
        meta_path.unlink()
    return {"status": "deleted"}


# =============================================================================
# Headline Graphic Endpoints
# =============================================================================

class HeadlineGraphicRequest(BaseModel):
    event_type: str = "DEFAULT"      # e.g. TO_W, SV_W, EMERGENCY, END
    headline: str                     # Large primary text
    subtitle: str = ""               # Secondary line (location, etc.)
    body: str = ""                   # Detail paragraph
    issued_by: str = ""
    expires: str = ""
    is_emergency: bool = False
    save: bool = False               # Save to alert graphics gallery


@app.post("/api/graphics/headline")
async def generate_headline_graphic(req: HeadlineGraphicRequest):
    """
    Generate a full-screen 1920x1080 broadcast headline graphic (PNG).

    Returns the image as a PNG stream and optionally saves it to the gallery.
    """
    from fastapi.responses import StreamingResponse
    import io

    try:
        from .services.headline_graphic_service import generate_headline_graphic as _gen
    except ImportError:
        from backend.services.headline_graphic_service import generate_headline_graphic as _gen

    settings = get_settings()
    brand = get_brand_config(settings.brand)

    try:
        png_bytes = _gen(
            event_type=req.event_type,
            headline=req.headline,
            subtitle=req.subtitle,
            body=req.body,
            brand_name=brand.name,
            brand_tagline=getattr(brand, "tagline", ""),
            issued_by=req.issued_by,
            expires=req.expires,
            is_emergency=req.is_emergency,
        )
    except Exception as e:
        logger.exception(f"Headline graphic generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Graphic generation failed: {e}")

    if req.save:
        import re as _re
        import json as _json
        from datetime import datetime, timezone
        _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_id = f"headline_{req.event_type}_{ts}"
        img_path = _GRAPHICS_DIR / f"{safe_id}.png"
        img_path.write_bytes(png_bytes)
        meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
        meta_path.write_text(_json.dumps({
            "product_id": safe_id,
            "event_name": req.headline,
        }))
        logger.info(f"Saved headline graphic: {safe_id}.png")

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="headline.png"'},
    )


@app.get("/api/graphics/headline/event-types")
async def get_headline_event_types():
    """List available headline graphic event types and their styles."""
    try:
        from .services.headline_graphic_service import PHENOMENON_STYLES
    except ImportError:
        from backend.services.headline_graphic_service import PHENOMENON_STYLES

    return {
        "event_types": [
            {"id": k, "label": v["label"], "accent": v["accent"]}
            for k, v in PHENOMENON_STYLES.items()
        ]
    }


# =============================================================================
# Alert Broadcast Graphic Endpoints
# =============================================================================

@app.get("/api/graphics/alert/{product_id}")
async def generate_alert_broadcast_graphic(
    product_id: str,
    save: bool = Query(False, description="Save to alert graphics gallery"),
):
    """
    Generate a broadcast-quality 1920x1080 alert graphic for the given alert.

    Returns a PNG image stream and optionally saves it to the gallery.
    """
    from fastapi.responses import StreamingResponse
    import io as _io
    import asyncio as _asyncio

    alert_mgr = get_alert_manager()
    alert = None
    for a in alert_mgr.get_all_alerts():
        if a.product_id == product_id:
            alert = a
            break
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert '{product_id}' not found")

    settings = get_settings()
    brand = get_brand_config(settings.brand)

    # ── Radar frame resolution (cascading) ────────────────────────────────────
    # Tier 1a: check if the WFO's nearest NEXRAD site is already cached locally.
    # Most WFO codes map directly to "K" + code (e.g. IND→KIND). For any that
    # don't, we find the nearest site by great-circle distance to the centroid.
    radar_frame = None
    try:
        import math as _math
        nexrad_svc = get_nexrad_service()
        from backend.services.nexrad_sites import NEXRAD_SITES as _NS

        sender = getattr(alert, "sender_office", "").upper().lstrip("K")  # strip leading K if present
        centroid = getattr(alert, "centroid", None)

        def _nearest_nexrad_site(lat: float, lon: float) -> str:
            """Return the NEXRAD site ID closest to (lat, lon)."""
            best_id, best_d = "", float("inf")
            for sid, info in _NS.items():
                dlat = info["lat"] - lat
                dlon = (info["lon"] - lon) * _math.cos(_math.radians(lat))
                d = dlat * dlat + dlon * dlon
                if d < best_d:
                    best_d, best_id = d, sid
            return best_id

        # Try "K" + WFO first (covers ~90 % of WFOs)
        candidate = "K" + sender if sender else ""
        if candidate not in _NS and centroid:
            candidate = _nearest_nexrad_site(centroid[0], centroid[1])

        # Tier 1a: already in the service cache for the correct site
        if candidate:
            history = nexrad_svc.get_frame_history("reflectivity", count=1, site=candidate)
            if history:
                radar_frame = history[-1]
                logger.info(f"Broadcast graphic using cached {candidate} radar frame")

        # Tier 1b: active site — only if it's within ~250 km of the alert centroid.
        # A distant radar gives poor coverage and IEM composite is better in that case.
        if radar_frame is None and centroid:
            frames = nexrad_svc.get_latest_frames()
            candidate_frame = frames.get("reflectivity") or frames.get("Reflectivity")
            if candidate_frame:
                active_site = nexrad_svc.active_site
                site_info = _NS.get(active_site, {})
                if site_info:
                    dlat = site_info["lat"] - centroid[0]
                    dlon = (site_info["lon"] - centroid[1]) * _math.cos(_math.radians(centroid[0]))
                    dist_deg = _math.sqrt(dlat * dlat + dlon * dlon)
                    if dist_deg < 2.25:  # ~250 km
                        radar_frame = candidate_frame
                        logger.info(f"Broadcast graphic using active-site {active_site} ({dist_deg:.1f}° away)")
                    else:
                        logger.info(f"Active site {active_site} is {dist_deg:.1f}° away — using tile radar instead")

    except Exception as _e:
        logger.debug(f"Radar frame lookup failed, will use tile fallback: {_e}")

    # Live/cached frames are binary-only (no image_path); rasterize OUR binary
    # frame so the Pillow renderer overlays our Level-2 radar instead of tiles.
    if radar_frame is not None and getattr(radar_frame, "image_path", None) is None \
            and getattr(radar_frame, "binary_data", None):
        try:
            nexrad_svc_b = get_nexrad_service()
            loop_b = _asyncio.get_event_loop()
            rendered_b = await loop_b.run_in_executor(
                None, lambda: nexrad_svc_b.render_binary_frame_to_image(radar_frame)
            )
            radar_frame = rendered_b  # None falls through to oneshot/tiles below
            if rendered_b is not None:
                logger.info("Broadcast graphic: rasterized cached binary frame for overlay")
        except Exception as _eb:
            logger.debug(f"Binary frame rasterize failed: {_eb}")
            radar_frame = None

    # Tier 1c: one-off Level-2 download from the nearest NEXRAD site.
    # This runs if no cached frame was close enough — downloads fresh data from AWS (~15-30s).
    if radar_frame is None and centroid:
        try:
            import asyncio as _asyncio2
            nearest_site = _nearest_nexrad_site(centroid[0], centroid[1])
            if nearest_site:
                logger.info(f"Broadcast graphic: oneshot download from {nearest_site}")
                loop2 = _asyncio2.get_event_loop()
                radar_frame = await loop2.run_in_executor(
                    None, lambda: nexrad_svc.oneshot_frame(nearest_site)
                )
                if radar_frame:
                    logger.info(f"Broadcast graphic: oneshot {nearest_site} succeeded")
                else:
                    logger.info(f"Broadcast graphic: oneshot {nearest_site} returned no frame, falling back to tiles")
        except Exception as _e2:
            logger.debug(f"Oneshot radar download failed: {_e2}")

    # If radar_frame is still None here, _render_map_panel will automatically
    # fall back to IEM composite tiles → RainViewer.

    try:
        from .services.alert_broadcast_graphic_service import generate_alert_broadcast_graphic as _gen
    except ImportError:
        from backend.services.alert_broadcast_graphic_service import generate_alert_broadcast_graphic as _gen

    meteorologist = getattr(brand, "meteorologist_name", None) or ""
    zone_polys = await _fetch_zone_polygons_for_alert(alert)

    try:
        loop = _asyncio.get_event_loop()
        png_bytes = await loop.run_in_executor(
            None,
            lambda: _gen(
                alert=alert,
                radar_frame=radar_frame,
                zone_polygons=zone_polys,
                brand_name=brand.name,
                meteorologist_name=meteorologist,
            )
        )
    except Exception as e:
        logger.exception(f"Alert broadcast graphic failed: {e}")
        raise HTTPException(status_code=500, detail=f"Graphic generation failed: {e}")

    if save:
        import re as _re
        import json as _json
        _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = _re.sub(r"[^\w\-.]", "_", product_id)
        img_path = _GRAPHICS_DIR / f"{safe_id}.png"
        img_path.write_bytes(png_bytes)
        meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
        meta_path.write_text(_json.dumps({
            "product_id": product_id,
            "event_name": alert.event_name,
        }))
        logger.info(f"Saved broadcast graphic: {safe_id}.png")

    return StreamingResponse(
        _io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product_id}.png"'},
    )


@app.get("/api/graphics/alert/{product_id}/save")
async def save_alert_broadcast_graphic(product_id: str):
    """Generate and save a broadcast graphic for the alert, return gallery metadata."""
    from fastapi.responses import JSONResponse
    import re as _re
    import json as _json
    import io as _io
    import asyncio as _asyncio

    alert_mgr = get_alert_manager()
    alert = None
    for a in alert_mgr.get_all_alerts():
        if a.product_id == product_id:
            alert = a
            break
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert '{product_id}' not found")

    settings = get_settings()
    brand = get_brand_config(settings.brand)

    radar_frame = None
    try:
        nexrad_svc = get_nexrad_service()
        from backend.services.nexrad_sites import NEXRAD_SITES as _NS2
        sender2 = getattr(alert, "sender_office", "").upper().lstrip("K")
        centroid2 = getattr(alert, "centroid", None)
        candidate2 = "K" + sender2 if sender2 else ""
        if candidate2 not in _NS2 and centroid2:
            import math as _math2
            best_id2, best_d2 = "", float("inf")
            for sid, info in _NS2.items():
                dlat = info["lat"] - centroid2[0]
                dlon = (info["lon"] - centroid2[1]) * _math2.cos(_math2.radians(centroid2[0]))
                d = dlat * dlat + dlon * dlon
                if d < best_d2:
                    best_d2, best_id2 = d, sid
            candidate2 = best_id2
        if candidate2:
            hist2 = nexrad_svc.get_frame_history("reflectivity", count=1, site=candidate2)
            if hist2:
                radar_frame = hist2[-1]
        if radar_frame is None and centroid2:
            frames2 = nexrad_svc.get_latest_frames()
            cframe2 = frames2.get("reflectivity") or frames2.get("Reflectivity")
            if cframe2:
                import math as _math3
                asite2 = nexrad_svc.active_site
                sinfo2 = _NS2.get(asite2, {})
                if sinfo2:
                    dlat2 = sinfo2["lat"] - centroid2[0]
                    dlon2 = (sinfo2["lon"] - centroid2[1]) * _math3.cos(_math3.radians(centroid2[0]))
                    if _math3.sqrt(dlat2*dlat2 + dlon2*dlon2) < 2.25:
                        radar_frame = cframe2
    except Exception:
        pass

    # Rasterize OUR binary frame to a PNG so the graphic overlays Level-2 radar
    # instead of falling back to composite tiles.
    if radar_frame is not None and getattr(radar_frame, "image_path", None) is None \
            and getattr(radar_frame, "binary_data", None):
        try:
            loop_bs = _asyncio.get_event_loop()
            radar_frame = await loop_bs.run_in_executor(
                None, lambda: get_nexrad_service().render_binary_frame_to_image(radar_frame)
            )
        except Exception:
            radar_frame = None

    # Tier 1c: one-off Level-2 download from the nearest NEXRAD site
    if radar_frame is None and centroid2:
        try:
            import asyncio as _asyncio3
            import math as _math4
            nexrad_svc2 = get_nexrad_service()
            from backend.services.nexrad_sites import NEXRAD_SITES as _NS3
            best3, bd3 = "", float("inf")
            for sid3, info3 in _NS3.items():
                dlat3 = info3["lat"] - centroid2[0]
                dlon3 = (info3["lon"] - centroid2[1]) * _math4.cos(_math4.radians(centroid2[0]))
                d3 = dlat3*dlat3 + dlon3*dlon3
                if d3 < bd3:
                    bd3, best3 = d3, sid3
            if best3:
                logger.info(f"Save endpoint: oneshot download from {best3}")
                loop3 = _asyncio3.get_event_loop()
                radar_frame = await loop3.run_in_executor(
                    None, lambda: nexrad_svc2.oneshot_frame(best3)
                )
                if radar_frame:
                    logger.info(f"Save endpoint: oneshot {best3} succeeded")
        except Exception:
            pass

    try:
        from .services.alert_broadcast_graphic_service import generate_alert_broadcast_graphic as _gen
    except ImportError:
        from backend.services.alert_broadcast_graphic_service import generate_alert_broadcast_graphic as _gen

    meteorologist = getattr(brand, "meteorologist_name", None) or ""
    zone_polys2 = await _fetch_zone_polygons_for_alert(alert)

    loop = _asyncio.get_event_loop()
    png_bytes = await loop.run_in_executor(
        None,
        lambda: _gen(
            alert=alert,
            radar_frame=radar_frame,
            zone_polygons=zone_polys2,
            brand_name=brand.name,
            meteorologist_name=meteorologist,
        )
    )

    _GRAPHICS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = _re.sub(r"[^\w\-.]", "_", product_id)
    img_path = _GRAPHICS_DIR / f"{safe_id}.png"
    img_path.write_bytes(png_bytes)
    meta_path = _GRAPHICS_DIR / f"{safe_id}.json"
    meta_path.write_text(_json.dumps({
        "product_id": product_id,
        "event_name": alert.event_name,
    }))

    return {"status": "saved", "product_id": product_id,
            "url": f"/api/alert-graphics/image/{safe_id}.png"}


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
