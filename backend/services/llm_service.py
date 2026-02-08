"""
LLM Service for Alert Dashboard V2.

Provides integration with Ollama for local LLM inference.
Uses gemma3:4b model for weather analysis and chat.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import aiohttp

from ..config import get_settings

logger = logging.getLogger(__name__)


# Enhanced system prompt with comprehensive weather knowledge
WEATHER_ASSISTANT_SYSTEM_PROMPT = """You are a meteorological assistant for a weather alert dashboard.

## CRITICAL RULE - READ THIS FIRST
You MUST ONLY report information that is EXPLICITLY listed in the "CURRENT WEATHER DATA" section below.
- If there are NO tornado warnings in the data, say "No tornado warnings are active"
- If there are NO severe thunderstorm warnings, say so
- NEVER invent, assume, or hallucinate alerts that are not listed
- If the data shows "No active weather alerts", that means conditions are calm - say so
- ONLY mention specific threats (tornado, hail, wind) if they appear in the actual alert data

## Your Role
Answer questions using ONLY the data provided below. Be accurate and factual.

## Alert Severity Hierarchy (most to least severe)
IMMEDIATE LIFE THREATS:
- Tornado Emergency: Confirmed violent tornado threatening populated area - TAKE COVER IMMEDIATELY
- Tornado Warning with "OBSERVED" or "CONFIRMED": Tornado on the ground
- Tornado Warning with "RADAR INDICATED": Strong rotation detected, tornado likely
- PDS (Particularly Dangerous Situation): Unusually severe event expected
- Severe Thunderstorm Warning with "DESTRUCTIVE": 80+ mph winds and/or 2.75"+ hail

HIGH PRIORITY:
- Tornado Watch: Conditions favorable for tornadoes in the next few hours
- Severe Thunderstorm Warning: 58+ mph winds and/or 1"+ hail
- Flash Flood Warning: Life-threatening flooding occurring or imminent
- Blizzard Warning: Sustained 35+ mph winds with heavy snow

MODERATE:
- Severe Thunderstorm Watch: Conditions favorable for severe storms
- Winter Storm Warning: Significant snow/ice expected
- Wind Advisory: Sustained 30+ mph or gusts 45+ mph
- Flood Warning: Flooding occurring or imminent

LOWER:
- Winter Weather Advisory, Frost Advisory, Dense Fog Advisory, etc.

## Threat Tags to Watch For
- "TORNADO...OBSERVED" or "TORNADO...CONFIRMED" = Tornado spotted
- "TORNADO...RADAR INDICATED" = Strong rotation on radar
- "TORNADO DAMAGE THREAT...CATASTROPHIC" = Violent tornado
- "TORNADO DAMAGE THREAT...CONSIDERABLE" = Significant tornado
- "DESTRUCTIVE" damage threat = Extremely dangerous storm
- "CONSIDERABLE" damage threat = Significant damage expected
- Hail size: 1" (quarter), 1.75" (golf ball), 2.75" (baseball), 4"+ (softball)
- Wind gusts: 60 mph (damaging), 70 mph (destructive), 80+ mph (catastrophic)

## SPC Outlook Risk Levels (categorical)
- HIGH (Magenta): Rare - significant severe weather outbreak expected
- MDT/Moderate (Red): Major severe weather expected
- ENH/Enhanced (Orange): Numerous severe storms expected
- SLGT/Slight (Yellow): Scattered severe storms possible
- MRGL/Marginal (Green): Isolated severe storms possible
- TSTM (Light Green): General thunderstorms, non-severe

## SPC Probabilistic Outlooks (tornado/wind/hail)
These show the probability of hazardous weather within 25 miles of a point:
- 2% (Green): Low probability
- 5% (Brown): Slight chance
- 10% (Yellow): Notable probability
- 15% (Red): Elevated probability
- 30%+ (Magenta/Purple): High probability
- SIGPROB/Hatched areas: Significant severe potential (EF2+ tornado, 75+ mph wind, 2"+ hail)

## Response Guidelines
1. BE DIRECT: Answer what the user asked. If they ask "what warnings are active?", list them.
2. BE SPECIFIC: Use actual data - mention specific wind speeds, hail sizes, locations, times.
3. PRIORITIZE SAFETY: Always mention life-threatening conditions first.
4. USE PLAIN LANGUAGE: Explain meteorological terms when you use them.
5. BE CONCISE: 2-3 sentences for simple questions, more detail only when needed.
6. ACKNOWLEDGE UNCERTAINTY: If data is limited, say so.

## When No Severe Weather
If the data shows no active alerts or only minor advisories, say so clearly. Don't fabricate severe weather.

## FINAL REMINDER
Before answering, CHECK THE DATA BELOW. Only report what is actually listed. If asked about tornado activity and there are no tornado warnings in the data, your answer MUST be "No tornado warnings are active" or similar. Never make up alerts."""


def format_time_relative(iso_string: Optional[str]) -> str:
    """Format ISO time string as relative time (e.g., '2 hours ago', 'in 30 minutes')."""
    if not iso_string:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        diff = dt - now
        total_seconds = diff.total_seconds()

        if abs(total_seconds) < 60:
            return "just now"

        minutes = abs(total_seconds) / 60
        hours = minutes / 60

        if total_seconds > 0:  # Future
            if hours >= 1:
                return f"in {int(hours)}h {int(minutes % 60)}m"
            return f"in {int(minutes)}m"
        else:  # Past
            if hours >= 1:
                return f"{int(hours)}h {int(minutes % 60)}m ago"
            return f"{int(minutes)}m ago"
    except Exception:
        return "unknown"


def format_time_short(iso_string: Optional[str]) -> str:
    """Format ISO time string as short time (e.g., '3:45 PM')."""
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        # Convert to local time
        local_dt = dt.astimezone()
        # Use %I for 12-hour clock, strip leading zero manually for cross-platform
        time_str = local_dt.strftime("%I:%M %p")
        if time_str.startswith("0"):
            time_str = time_str[1:]
        return time_str
    except Exception:
        return ""


def build_alert_context(alerts: list, include_details: bool = True) -> str:
    """
    Build comprehensive alert context for the LLM.

    Args:
        alerts: List of Alert objects
        include_details: Whether to include threat details

    Returns:
        Formatted string with alert information
    """
    if not alerts:
        return "No active weather alerts at this time."

    # Group alerts by severity/type
    life_threatening = []
    warnings = []
    watches = []
    advisories = []

    for alert in alerts:
        event_upper = (alert.event_name or "").upper()
        threat = alert.threat

        # Check for life-threatening conditions
        is_life_threat = (
            "TORNADO" in event_upper and "WARNING" in event_upper or
            threat.tornado_detection in ["OBSERVED", "CONFIRMED"] or
            threat.tornado_damage_threat in ["CATASTROPHIC", "CONSIDERABLE"] or
            threat.wind_damage_threat == "DESTRUCTIVE" or
            "EMERGENCY" in event_upper
        )

        if is_life_threat:
            life_threatening.append(alert)
        elif "WARNING" in event_upper:
            warnings.append(alert)
        elif "WATCH" in event_upper:
            watches.append(alert)
        else:
            advisories.append(alert)

    lines = []
    lines.append(f"=== CURRENT WEATHER DATA ({len(alerts)} active alerts) ===")
    lines.append(f"Time: {datetime.now().strftime('%I:%M %p on %B %d, %Y')}")
    lines.append("")

    # Explicitly state what IS NOT present to prevent hallucinations
    alert_types_present = set(a.event_name for a in alerts if a.event_name)
    has_tornado_warning = any("TORNADO" in t.upper() and "WARNING" in t.upper() for t in alert_types_present)
    has_svr_warning = any("SEVERE THUNDERSTORM" in t.upper() and "WARNING" in t.upper() for t in alert_types_present)

    if not has_tornado_warning:
        lines.append("** NO TORNADO WARNINGS ACTIVE **")
    if not has_svr_warning:
        lines.append("** NO SEVERE THUNDERSTORM WARNINGS ACTIVE **")
    if not has_tornado_warning or not has_svr_warning:
        lines.append("")

    # Life-threatening alerts first
    if life_threatening:
        lines.append("🚨 LIFE-THREATENING ALERTS:")
        for alert in life_threatening:
            lines.append(_format_alert_detail(alert, include_details))
        lines.append("")

    # Warnings
    if warnings:
        lines.append("⚠️ ACTIVE WARNINGS:")
        for alert in warnings:
            lines.append(_format_alert_detail(alert, include_details))
        lines.append("")

    # Watches
    if watches:
        lines.append("👁️ ACTIVE WATCHES:")
        for alert in watches:
            lines.append(_format_alert_detail(alert, include_details))
        lines.append("")

    # Advisories (summarize if many)
    if advisories:
        if len(advisories) <= 3:
            lines.append("📋 ADVISORIES:")
            for alert in advisories:
                lines.append(_format_alert_detail(alert, include_details=False))
        else:
            # Summarize advisories by type
            by_type = {}
            for alert in advisories:
                event = alert.event_name or "Advisory"
                by_type[event] = by_type.get(event, 0) + 1
            lines.append(f"📋 ADVISORIES ({len(advisories)} total):")
            for event, count in by_type.items():
                lines.append(f"  - {event}: {count}")

    return "\n".join(lines)


def _format_alert_detail(alert, include_details: bool = True) -> str:
    """Format a single alert with details."""
    threat = alert.threat
    parts = []

    # Event name and locations
    locations = alert.display_locations or ", ".join(alert.affected_areas[:5])
    parts.append(f"  • {alert.event_name}")
    parts.append(f"    Location: {locations}")

    # Timing
    issued = format_time_short(alert.issued_time)
    expires = format_time_relative(alert.expiration_time)
    if issued:
        parts.append(f"    Issued: {issued} | Expires: {expires}")

    if include_details:
        # Tornado info
        if threat.tornado_detection:
            tornado_info = f"TORNADO {threat.tornado_detection}"
            if threat.tornado_damage_threat:
                tornado_info += f" - {threat.tornado_damage_threat} DAMAGE THREAT"
            parts.append(f"    🌪️ {tornado_info}")

        # Wind info
        wind_parts = []
        if threat.sustained_wind_min_mph and threat.sustained_wind_max_mph:
            wind_parts.append(f"Wind: {threat.sustained_wind_min_mph}-{threat.sustained_wind_max_mph} mph")
        if threat.max_wind_gust_mph:
            wind_parts.append(f"Gusts: {threat.max_wind_gust_mph} mph")
        if threat.wind_damage_threat:
            wind_parts.append(f"({threat.wind_damage_threat})")
        if wind_parts:
            parts.append(f"    💨 {' | '.join(wind_parts)}")

        # Hail info
        if threat.max_hail_size_inches:
            hail_info = f"Hail: {threat.max_hail_size_inches}\""
            if threat.max_hail_size_inches >= 2.75:
                hail_info += " (baseball+)"
            elif threat.max_hail_size_inches >= 1.75:
                hail_info += " (golf ball)"
            elif threat.max_hail_size_inches >= 1.0:
                hail_info += " (quarter)"
            if threat.hail_damage_threat:
                hail_info += f" - {threat.hail_damage_threat}"
            parts.append(f"    🧊 {hail_info}")

        # Winter weather info
        if threat.snow_amount_max_inches:
            snow_min = threat.snow_amount_min_inches or 0
            parts.append(f"    ❄️ Snow: {snow_min}-{threat.snow_amount_max_inches}\"")
        if threat.ice_accumulation_inches:
            parts.append(f"    🧊 Ice: {threat.ice_accumulation_inches}\"")

        # Issuing office
        if alert.sender_name:
            parts.append(f"    Office: {alert.sender_name}")

    return "\n".join(parts)


def build_spc_context(spc_data: Optional[dict]) -> str:
    """
    Build SPC outlook context for the LLM.

    Args:
        spc_data: Dictionary with SPC outlook data

    Returns:
        Formatted string with SPC information
    """
    if not spc_data:
        return ""

    lines = []
    lines.append("=== SPC OUTLOOK DATA ===")

    # Day 1 categorical
    if spc_data.get("day1_categorical"):
        cat = spc_data["day1_categorical"]
        if cat.get("polygons"):
            highest_risk = cat["polygons"][0] if cat["polygons"] else None
            if highest_risk:
                lines.append(f"Day 1 Outlook: {highest_risk.get('risk_name', 'Unknown')}")

    # Day 1 probabilistic
    for prob_type in ["tornado", "wind", "hail"]:
        key = f"day1_{prob_type}"
        if spc_data.get(key):
            data = spc_data[key]
            if data.get("polygons"):
                highest = data["polygons"][0]
                lines.append(f"Day 1 {prob_type.title()}: {highest.get('risk_name', 'Unknown')}")

    # Mesoscale discussions
    if spc_data.get("mesoscale_discussions"):
        mds = spc_data["mesoscale_discussions"]
        if mds:
            lines.append(f"Active Mesoscale Discussions: {len(mds)}")
            for md in mds[:3]:
                lines.append(f"  - MD #{md.get('md_number')}: {md.get('title', 'No title')}")

    return "\n".join(lines) if len(lines) > 1 else ""


def build_full_context(
    alerts: list,
    spc_data: Optional[dict] = None,
    wind_gusts: Optional[list] = None,
    filter_states: Optional[list[str]] = None,
) -> str:
    """
    Build complete context for LLM including all available data.

    Args:
        alerts: List of Alert objects
        spc_data: Optional SPC outlook data
        wind_gusts: Optional list of recent wind gusts
        filter_states: States the user is monitoring

    Returns:
        Complete formatted context string
    """
    parts = []

    # Monitoring info
    if filter_states:
        parts.append(f"Monitoring: {', '.join(filter_states)}")
        parts.append("")

    # Alerts
    parts.append(build_alert_context(alerts))

    # SPC data
    spc_context = build_spc_context(spc_data)
    if spc_context:
        parts.append("")
        parts.append(spc_context)

    # Wind gusts
    if wind_gusts:
        parts.append("")
        parts.append("=== RECENT WIND GUSTS ===")
        for gust in wind_gusts[:5]:
            parts.append(f"  • {gust.city}, {gust.state}: {gust.gust_mph} mph ({gust.severity})")

    return "\n".join(parts)


@dataclass
class ChatMessage:
    """Single chat message."""
    role: str  # "user", "assistant", or "system"
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API calls."""
        return {
            "role": self.role,
            "content": self.content,
        }


@dataclass
class LLMResponse:
    """Response from LLM."""
    content: str
    model: str
    done: bool
    total_duration_ns: Optional[int] = None
    prompt_eval_count: Optional[int] = None
    eval_count: Optional[int] = None

    @property
    def duration_ms(self) -> Optional[float]:
        """Get duration in milliseconds."""
        if self.total_duration_ns:
            return self.total_duration_ns / 1_000_000
        return None


class LLMService:
    """Service for interacting with Ollama LLM."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "gemma3:4b",
        timeout: int = 120,
    ):
        """
        Initialize LLM service.

        Args:
            host: Ollama API host URL
            model: Model to use for inference
            timeout: Request timeout in seconds
        """
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._conversation_history: list[ChatMessage] = []
        self._is_available: Optional[bool] = None
        self._last_health_check: Optional[datetime] = None

    async def check_health(self, force: bool = False) -> bool:
        """
        Check if Ollama is running and the model is available.

        Args:
            force: Force a fresh check even if recently checked

        Returns:
            True if service is healthy, False otherwise
        """
        # Cache health check for 30 seconds unless forced
        if not force and self._last_health_check:
            elapsed = (datetime.now(timezone.utc) - self._last_health_check).total_seconds()
            if elapsed < 30 and self._is_available is not None:
                return self._is_available

        try:
            async with aiohttp.ClientSession() as session:
                # Check if Ollama is running
                async with session.get(
                    f"{self.host}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        self._is_available = False
                        self._last_health_check = datetime.now(timezone.utc)
                        logger.warning(f"Ollama health check failed: HTTP {response.status}")
                        return False

                    data = await response.json()
                    models = [m.get("name", "") for m in data.get("models", [])]

                    # Check if our model is available (handle tag variations)
                    model_available = any(
                        self.model in m or m.startswith(self.model.split(":")[0])
                        for m in models
                    )

                    if not model_available:
                        logger.warning(
                            f"Model {self.model} not found. Available: {models}"
                        )
                        self._is_available = False
                    else:
                        self._is_available = True
                        logger.debug(f"Ollama health check passed. Model {self.model} available.")

                    self._last_health_check = datetime.now(timezone.utc)
                    return self._is_available

        except asyncio.TimeoutError:
            logger.warning("Ollama health check timed out")
            self._is_available = False
            self._last_health_check = datetime.now(timezone.utc)
            return False
        except aiohttp.ClientError as e:
            logger.warning(f"Ollama health check failed: {e}")
            self._is_available = False
            self._last_health_check = datetime.now(timezone.utc)
            return False
        except Exception as e:
            logger.exception(f"Unexpected error in health check: {e}")
            self._is_available = False
            self._last_health_check = datetime.now(timezone.utc)
            return False

    async def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        context: Optional[str] = None,
        include_history: bool = True,
    ) -> LLMResponse:
        """
        Send a chat message and get a response.

        Args:
            message: User message
            system_prompt: Optional system prompt (uses default if not provided)
            context: Optional additional context (e.g., current alerts, weather data)
            include_history: Whether to include conversation history

        Returns:
            LLMResponse with the assistant's reply
        """
        # Build messages list
        messages: list[dict[str, str]] = []

        # Add system prompt with clear data boundaries
        system = system_prompt or WEATHER_ASSISTANT_SYSTEM_PROMPT
        if context:
            system = f"{system}\n\n--- CURRENT WEATHER DATA (ONLY USE THIS DATA) ---\n{context}\n--- END OF DATA ---"
        else:
            system = f"{system}\n\n--- CURRENT WEATHER DATA ---\nNo data available.\n--- END OF DATA ---"
        messages.append({"role": "system", "content": system})

        # Add conversation history if requested
        if include_history:
            for msg in self._conversation_history[-10:]:  # Last 10 messages
                messages.append(msg.to_dict())

        # Add current message
        messages.append({"role": "user", "content": message})

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.host}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                    },
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Ollama chat error: {response.status} - {error_text}")
                        raise RuntimeError(f"Ollama API error: {response.status}")

                    data = await response.json()

                    assistant_content = data.get("message", {}).get("content", "")

                    # Store in history
                    self._conversation_history.append(
                        ChatMessage(role="user", content=message)
                    )
                    self._conversation_history.append(
                        ChatMessage(role="assistant", content=assistant_content)
                    )

                    return LLMResponse(
                        content=assistant_content,
                        model=data.get("model", self.model),
                        done=data.get("done", True),
                        total_duration_ns=data.get("total_duration"),
                        prompt_eval_count=data.get("prompt_eval_count"),
                        eval_count=data.get("eval_count"),
                    )

        except asyncio.TimeoutError:
            logger.error(f"Ollama chat timed out after {self.timeout}s")
            raise RuntimeError("LLM request timed out")
        except aiohttp.ClientError as e:
            logger.error(f"Ollama chat connection error: {e}")
            raise RuntimeError(f"LLM connection error: {e}")

    async def chat_stream(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        context: Optional[str] = None,
        include_history: bool = True,
    ) -> AsyncIterator[str]:
        """
        Send a chat message and stream the response.

        Args:
            message: User message
            system_prompt: Optional system prompt
            context: Optional additional context
            include_history: Whether to include conversation history

        Yields:
            Response chunks as they arrive
        """
        # Build messages list
        messages: list[dict[str, str]] = []

        # Add system prompt with clear data boundaries
        system = system_prompt or WEATHER_ASSISTANT_SYSTEM_PROMPT
        if context:
            system = f"{system}\n\n--- CURRENT WEATHER DATA (ONLY USE THIS DATA) ---\n{context}\n--- END OF DATA ---"
        else:
            system = f"{system}\n\n--- CURRENT WEATHER DATA ---\nNo data available.\n--- END OF DATA ---"
        messages.append({"role": "system", "content": system})

        # Add conversation history if requested
        if include_history:
            for msg in self._conversation_history[-10:]:
                messages.append(msg.to_dict())

        # Add current message
        messages.append({"role": "user", "content": message})

        full_response = ""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.host}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Ollama stream error: {response.status} - {error_text}")
                        raise RuntimeError(f"Ollama API error: {response.status}")

                    async for line in response.content:
                        if not line:
                            continue

                        try:
                            import json
                            data = json.loads(line.decode())
                            chunk = data.get("message", {}).get("content", "")
                            if chunk:
                                full_response += chunk
                                yield chunk

                            if data.get("done", False):
                                break
                        except json.JSONDecodeError:
                            continue

            # Store in history after complete
            self._conversation_history.append(
                ChatMessage(role="user", content=message)
            )
            self._conversation_history.append(
                ChatMessage(role="assistant", content=full_response)
            )

        except asyncio.TimeoutError:
            logger.error(f"Ollama stream timed out after {self.timeout}s")
            raise RuntimeError("LLM request timed out")
        except aiohttp.ClientError as e:
            logger.error(f"Ollama stream connection error: {e}")
            raise RuntimeError(f"LLM connection error: {e}")

    async def analyze_alert(
        self,
        alert_text: str,
        alert_type: str,
        locations: list[str],
        additional_context: Optional[str] = None,
    ) -> str:
        """
        Analyze a weather alert and provide insights.

        Args:
            alert_text: Full alert text
            alert_type: Type of alert (e.g., "Tornado Warning")
            locations: List of affected locations
            additional_context: Optional additional context

        Returns:
            Analysis and insights about the alert
        """
        prompt = f"""Analyze this weather alert and provide a brief insight (2-3 sentences max):

Alert Type: {alert_type}
Affected Areas: {', '.join(locations)}

Alert Text:
{alert_text[:1500]}  # Truncate for context window

Provide:
1. Key takeaway (what's most important)
2. Any safety recommendations
3. Note any concerning patterns or escalation potential"""

        if additional_context:
            prompt += f"\n\nAdditional context:\n{additional_context}"

        response = await self.chat(
            message=prompt,
            include_history=False,  # Fresh analysis each time
        )
        return response.content

    async def generate_insight(
        self,
        data_summary: str,
        insight_type: str = "general",
    ) -> str:
        """
        Generate an insight based on data summary.

        Args:
            data_summary: Summary of current data/conditions
            insight_type: Type of insight to generate

        Returns:
            Generated insight text
        """
        prompts = {
            "general": "Give a quick summary of current weather conditions. What's most important to know right now? Be direct and specific.",
            "wind": "What are the current wind conditions? Highlight any concerning gusts or sustained winds. Be specific about speeds and locations.",
            "pattern": "What patterns do you see in the current weather situation? Are conditions improving, worsening, or stable?",
            "safety": "Based on current conditions, what safety actions should people take? Be specific and actionable.",
        }

        prompt = f"{prompts.get(insight_type, prompts['general'])}\n\nCurrent data:\n{data_summary}"

        response = await self.chat(
            message=prompt,
            include_history=False,
        )
        return response.content

    def clear_history(self):
        """Clear conversation history."""
        self._conversation_history.clear()
        logger.debug("Conversation history cleared")

    def get_history(self) -> list[dict[str, Any]]:
        """Get conversation history as list of dicts."""
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
            }
            for msg in self._conversation_history
        ]

    def get_statistics(self) -> dict[str, Any]:
        """Get service statistics."""
        return {
            "model": self.model,
            "host": self.host,
            "is_available": self._is_available,
            "last_health_check": self._last_health_check.isoformat() if self._last_health_check else None,
            "history_length": len(self._conversation_history),
        }


# Global service instance
_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Get the global LLMService instance."""
    global _service
    if _service is None:
        settings = get_settings()
        _service = LLMService(
            host=getattr(settings, 'ollama_host', 'http://localhost:11434'),
            model=getattr(settings, 'ollama_model', 'gemma3:4b'),
            timeout=getattr(settings, 'llm_timeout', 120),
        )
    return _service


async def start_llm_service() -> bool:
    """
    Start the LLM service and verify it's working.

    Returns:
        True if service started and is healthy, False otherwise
    """
    global _service
    settings = get_settings()

    # Check if LLM is enabled
    if not getattr(settings, 'llm_enabled', True):
        logger.info("LLM service is disabled in settings")
        return False

    _service = LLMService(
        host=getattr(settings, 'ollama_host', 'http://localhost:11434'),
        model=getattr(settings, 'ollama_model', 'gemma3:4b'),
        timeout=getattr(settings, 'llm_timeout', 120),
    )

    # Check health
    is_healthy = await _service.check_health(force=True)
    if is_healthy:
        logger.info(f"LLM service started with model {_service.model}")
    else:
        logger.warning("LLM service started but Ollama is not available")

    return is_healthy


async def stop_llm_service():
    """Stop the LLM service."""
    global _service
    if _service:
        _service.clear_history()
        _service = None
    logger.info("LLM service stopped")
