"""
Assistant routes.

Lifted out of backend/main.py, which held 174 routes and startup orchestration
in one 6,000-line file.

Service imports below are `from ..services...` -- two dots. One resolved to
`backend` while these lived in main.py and would resolve to `backend.routers`
here. Because the imports sit inside function bodies, a missed one registers
fine and fails only when the endpoint is called.
"""

import logging

from fastapi import APIRouter
from ..config import get_settings
from ..services import (
    DEFAULT_GUST_STATES,
    build_full_context,
    get_alert_manager,
    get_llm_service,
    get_spc_service,
    get_wind_gusts_service,
)
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from pydantic import Field
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()


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


@router.get("/api/assistant/status")
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
@router.post("/api/assistant/chat")
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
@router.post("/api/assistant/analyze")
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
@router.get("/api/assistant/history")
async def get_chat_history():
    """Get conversation history."""
    settings = get_settings()

    if not settings.llm_enabled:
        return {"history": [], "message": "LLM assistant is disabled"}

    llm_service = get_llm_service()
    return {
        "history": llm_service.get_history(),
    }
@router.delete("/api/assistant/history")
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
@router.get("/api/assistant/insight")
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
