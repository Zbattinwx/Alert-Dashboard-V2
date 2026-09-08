"""
Agent routes.

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
    get_agent_service,
)
from fastapi import HTTPException
from fastapi import Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/agent/status")
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


@router.post("/api/agent/chat")
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


@router.get("/api/agent/tools")
async def list_agent_tools():
    """List all tools available to the AI agent."""
    agent = get_agent_service()
    return {"tools": agent.tools.list_tools()}


@router.get("/api/agent/history")
async def get_agent_history():
    """Get agent conversation history."""
    agent = get_agent_service()
    return {"history": agent.get_history()}


@router.delete("/api/agent/history")
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

