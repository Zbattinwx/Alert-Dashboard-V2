"""
Agent Tool Registry for Alert Dashboard V2.

Defines the tool registry and tool handlers that wrap existing
weather services for use by the AI agent via Ollama tool calling.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..config import get_settings

logger = logging.getLogger(__name__)

# Maximum characters per tool result to stay within context limits
MAX_TOOL_RESULT_CHARS = 2000


@dataclass
class ToolDefinition:
    """Definition of a tool the agent can call."""

    name: str
    description: str
    parameters: dict  # JSON Schema for parameters
    handler: Callable[..., Awaitable[str]]

    def to_ollama_format(self) -> dict:
        """Convert to Ollama /api/chat tools format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registry of tools available to the AI agent."""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition):
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.debug(f"Registered agent tool: {tool.name}")

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_ollama_tools(self) -> list[dict]:
        """Return all tool definitions in Ollama format."""
        return [t.to_ollama_format() for t in self._tools.values()]

    def list_tools(self) -> list[dict]:
        """Return tool info for API responses."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict) -> str:
        """
        Execute a tool by name with the given arguments.

        Returns the tool result as a string, truncated if necessary.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Unknown tool '{name}'. Available tools: {list(self._tools.keys())}"

        try:
            result = await tool.handler(**arguments)
            if len(result) > MAX_TOOL_RESULT_CHARS:
                result = result[:MAX_TOOL_RESULT_CHARS] + "\n... (truncated)"
            return result
        except TypeError as e:
            logger.error(f"Tool '{name}' argument error: {e}")
            return f"Error: Invalid arguments for '{name}': {e}"
        except Exception as e:
            logger.exception(f"Tool '{name}' execution error: {e}")
            return f"Error executing '{name}': {e}"

    @property
    def tool_count(self) -> int:
        return len(self._tools)


# ---------------------------------------------------------------------------
# Tool handler implementations
# Each handler wraps an existing service and returns a readable string.
# ---------------------------------------------------------------------------


def _truncate_list(items: list[str], max_items: int = 15) -> list[str]:
    """Truncate a list and add a count of remaining items."""
    if len(items) <= max_items:
        return items
    return items[:max_items] + [f"... and {len(items) - max_items} more"]


async def handle_get_active_alerts(
    state: str = "",
    phenomenon: str = "",
) -> str:
    """Get active weather alerts with optional filters."""
    from .alert_manager import get_alert_manager

    manager = get_alert_manager()

    if state:
        alerts = manager.get_alerts_by_state(state.upper())
    elif phenomenon:
        alerts = manager.get_alerts_by_phenomenon(phenomenon.upper())
    else:
        alerts = manager.get_alerts_sorted()

    if not alerts:
        filter_desc = ""
        if state:
            filter_desc = f" for state {state.upper()}"
        elif phenomenon:
            filter_desc = f" for phenomenon {phenomenon.upper()}"
        return f"No active weather alerts{filter_desc}."

    lines = [f"Found {len(alerts)} active alerts:"]
    for alert in alerts[:15]:
        locations = alert.display_locations or ", ".join(alert.affected_areas[:3])
        threat_info = []
        if alert.threat.tornado_detection:
            threat_info.append(f"Tornado: {alert.threat.tornado_detection}")
        if alert.threat.max_wind_gust_mph:
            threat_info.append(f"Wind: {alert.threat.max_wind_gust_mph} mph")
        if alert.threat.max_hail_size_inches:
            threat_info.append(f'Hail: {alert.threat.max_hail_size_inches}"')

        detail = f"  - {alert.event_name} | {locations}"
        if threat_info:
            detail += f" | {', '.join(threat_info)}"
        if alert.expiration_time:
            try:
                exp = alert.expiration_time
                if isinstance(exp, str):
                    exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                remaining = exp - datetime.now(timezone.utc)
                mins = int(remaining.total_seconds() / 60)
                if mins > 0:
                    detail += f" | Expires in {mins}m"
            except Exception:
                pass
        lines.append(detail)

    if len(alerts) > 15:
        lines.append(f"... and {len(alerts) - 15} more alerts")

    return "\n".join(lines)


async def handle_get_alert_counts() -> str:
    """Get counts of alerts by type."""
    from .alert_manager import get_alert_manager

    manager = get_alert_manager()
    counts = manager.get_counts_by_type()

    if not counts:
        return "No active alerts. All clear."

    lines = [f"Alert counts ({sum(counts.values())} total):"]
    for phenomenon, count in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"  - {phenomenon}: {count}")
    return "\n".join(lines)


async def handle_get_alert_statistics() -> str:
    """Get overall alert statistics."""
    from .alert_manager import get_alert_manager

    manager = get_alert_manager()
    stats = manager.get_statistics()

    lines = ["Alert Statistics:"]
    lines.append(f"  Total active: {stats.get('total_active', 0)}")
    lines.append(f"  Warnings: {stats.get('warnings', 0)}")
    lines.append(f"  Watches: {stats.get('watches', 0)}")
    lines.append(f"  Advisories: {stats.get('advisories', 0)}")

    by_type = stats.get("by_type", {})
    if by_type:
        lines.append("  By type:")
        for phen, count in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"    {phen}: {count}")

    return "\n".join(lines)


async def handle_get_spc_outlook(
    outlook_type: str = "day1_categorical",
) -> str:
    """Get SPC outlook data."""
    from .spc_service import get_spc_service

    spc = get_spc_service()

    valid_keys = [
        "day1_categorical", "day1_tornado", "day1_wind", "day1_hail",
        "day2_categorical", "day2_tornado", "day2_wind", "day2_hail",
        "day3_categorical",
    ]

    if outlook_type not in valid_keys:
        return f"Invalid outlook type '{outlook_type}'. Valid options: {', '.join(valid_keys)}"

    outlook = await spc.fetch_outlook(outlook_type)
    if not outlook:
        return f"No data available for {outlook_type}."

    lines = [f"SPC {outlook_type.replace('_', ' ').title()}:"]
    if outlook.valid_time:
        lines.append(f"  Valid: {outlook.valid_time}")
    if outlook.expire_time:
        lines.append(f"  Expires: {outlook.expire_time}")

    if outlook.polygons:
        lines.append(f"  Risk areas ({len(outlook.polygons)}):")
        for poly in outlook.polygons:
            lines.append(f"    - {poly.risk_name} ({poly.risk_level})")
    else:
        lines.append("  No risk areas defined.")

    return "\n".join(lines)


async def handle_get_mesoscale_discussions() -> str:
    """Get active SPC mesoscale discussions."""
    from .spc_service import get_spc_service

    spc = get_spc_service()
    mds = await spc.fetch_mesoscale_discussions()

    if not mds:
        return "No active SPC Mesoscale Discussions."

    lines = [f"Active Mesoscale Discussions ({len(mds)}):"]
    for md in mds[:10]:
        lines.append(f"  - MD #{md.md_number}: {md.title}")
        if md.affected_states:
            lines.append(f"    States: {', '.join(md.affected_states)}")
        if md.description:
            desc = md.description[:200]
            if len(md.description) > 200:
                desc += "..."
            lines.append(f"    {desc}")
    return "\n".join(lines)


async def handle_get_storm_reports(
    state: str = "",
    report_type: str = "",
    hours: int = 24,
) -> str:
    """Get local storm reports."""
    from .lsr_service import get_lsr_service

    lsr = get_lsr_service()
    settings = get_settings()

    states = [state.upper()] if state else settings.filter_states
    reports = await lsr.fetch_reports(states=states, hours=hours)

    if report_type:
        reports = [r for r in reports if r.report_type.upper() == report_type.upper()]

    if not reports:
        filter_desc = ""
        if state:
            filter_desc += f" in {state.upper()}"
        if report_type:
            filter_desc += f" of type {report_type}"
        return f"No storm reports{filter_desc} in the last {hours} hours."

    # Group by type for summary
    by_type: dict[str, list] = {}
    for r in reports:
        by_type.setdefault(r.report_type, []).append(r)

    lines = [f"Storm Reports ({len(reports)} total, last {hours}h):"]
    for rtype, type_reports in sorted(by_type.items()):
        lines.append(f"\n  {rtype} ({len(type_reports)}):")
        for r in type_reports[:5]:
            mag = f" [{r.magnitude}]" if r.magnitude else ""
            lines.append(f"    - {r.city}, {r.state}{mag}")
            if r.remark:
                remark = r.remark[:100]
                if len(r.remark) > 100:
                    remark += "..."
                lines.append(f"      {remark}")
        if len(type_reports) > 5:
            lines.append(f"    ... and {len(type_reports) - 5} more {rtype} reports")

    return "\n".join(lines)


async def handle_get_wind_gusts(
    state: str = "",
    hours: int = 1,
    limit: int = 15,
) -> str:
    """Get recent wind gust observations."""
    from .wind_gusts_service import get_wind_gusts_service

    wg = get_wind_gusts_service()
    settings = get_settings()

    states = [state.upper()] if state else settings.filter_states
    gusts = await wg.fetch_gusts(states=states, hours=hours, limit=limit)

    if not gusts:
        return f"No significant wind gusts reported in the last {hours} hour(s)."

    lines = [f"Wind Gusts (last {hours}h, top {len(gusts)}):"]
    for g in gusts:
        time_str = g.valid_time.strftime("%I:%M %p") if g.valid_time else ""
        lines.append(f"  - {g.city}, {g.state}: {g.gust_mph} mph ({g.severity}) {time_str}")

    return "\n".join(lines)


async def handle_get_road_conditions() -> str:
    """Get ODOT road sensor data."""
    from .odot_service import get_odot_service

    odot = get_odot_service()
    sensors = await odot.fetch_sensors()

    if not sensors:
        return "No road sensor data available."

    # Filter to sensors with interesting data
    notable = [
        s for s in sensors
        if s.surface_temp is not None and s.surface_temp <= 32
        or (s.surface_condition and s.surface_condition.lower() not in ("dry", ""))
    ]

    if not notable:
        lines = [f"Road Sensors ({len(sensors)} total):"]
        lines.append("  All sensors reporting normal/dry conditions.")
        # Show a few anyway
        for s in sensors[:5]:
            parts = [f"  - {s.location}"]
            if s.air_temp is not None:
                parts.append(f"Air: {s.air_temp}°F")
            if s.surface_temp is not None:
                parts.append(f"Surface: {s.surface_temp}°F")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    lines = [f"Road Conditions ({len(notable)} sensors with notable conditions):"]
    for s in notable[:15]:
        parts = [f"  - {s.location}"]
        if s.surface_temp is not None:
            parts.append(f"Surface: {s.surface_temp}°F")
        if s.surface_condition:
            parts.append(f"Condition: {s.surface_condition}")
        if s.air_temp is not None:
            parts.append(f"Air: {s.air_temp}°F")
        lines.append(" | ".join(parts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


def build_default_tool_registry() -> ToolRegistry:
    """Build the default tool registry with all weather tools."""
    registry = ToolRegistry()

    registry.register(ToolDefinition(
        name="get_active_alerts",
        description="Get active weather alerts. Can filter by state code (e.g., OH, IN) or phenomenon code (e.g., TO for tornado, SV for severe thunderstorm, FF for flash flood, WS for winter storm, BZ for blizzard).",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Two-letter state code to filter by (e.g., OH, IN, IL)",
                },
                "phenomenon": {
                    "type": "string",
                    "description": "Phenomenon code to filter by (e.g., TO, SV, FF, WS, BZ)",
                },
            },
            "required": [],
        },
        handler=handle_get_active_alerts,
    ))

    registry.register(ToolDefinition(
        name="get_alert_counts",
        description="Get a quick summary of how many alerts are active, grouped by type. Useful for a quick overview before diving into details.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=handle_get_alert_counts,
    ))

    registry.register(ToolDefinition(
        name="get_alert_statistics",
        description="Get detailed alert statistics including counts of warnings, watches, and advisories broken down by type.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=handle_get_alert_statistics,
    ))

    registry.register(ToolDefinition(
        name="get_spc_outlook",
        description="Get SPC (Storm Prediction Center) outlook data. Shows risk levels and areas for severe weather. Types: day1_categorical, day1_tornado, day1_wind, day1_hail, day2_categorical, day2_tornado, day2_wind, day2_hail, day3_categorical.",
        parameters={
            "type": "object",
            "properties": {
                "outlook_type": {
                    "type": "string",
                    "description": "Outlook type key (default: day1_categorical). Options: day1_categorical, day1_tornado, day1_wind, day1_hail, day2_categorical, day2_tornado, day2_wind, day2_hail, day3_categorical",
                    "enum": [
                        "day1_categorical", "day1_tornado", "day1_wind", "day1_hail",
                        "day2_categorical", "day2_tornado", "day2_wind", "day2_hail",
                        "day3_categorical",
                    ],
                },
            },
            "required": [],
        },
        handler=handle_get_spc_outlook,
    ))

    registry.register(ToolDefinition(
        name="get_mesoscale_discussions",
        description="Get active SPC Mesoscale Discussions (MDs). These indicate areas where severe weather development is being monitored by the Storm Prediction Center.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=handle_get_mesoscale_discussions,
    ))

    registry.register(ToolDefinition(
        name="get_storm_reports",
        description="Get Local Storm Reports (LSRs) - confirmed reports of tornadoes, hail, damaging winds, flooding, and other severe weather. Can filter by state, type, and time window.",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Two-letter state code to filter by (e.g., OH)",
                },
                "report_type": {
                    "type": "string",
                    "description": "Report type to filter by (e.g., TORNADO, HAIL, TSTM WND GST, FLASH FLOOD)",
                },
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to look (default: 24)",
                },
            },
            "required": [],
        },
        handler=handle_get_storm_reports,
    ))

    registry.register(ToolDefinition(
        name="get_wind_gusts",
        description="Get recent wind gust observations from ASOS weather stations. Shows the strongest gusts with location and severity.",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Two-letter state code (e.g., OH)",
                },
                "hours": {
                    "type": "integer",
                    "description": "How many hours back to look (default: 1)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 15)",
                },
            },
            "required": [],
        },
        handler=handle_get_wind_gusts,
    ))

    registry.register(ToolDefinition(
        name="get_road_conditions",
        description="Get ODOT road sensor data including surface temperature, road conditions (dry, wet, ice, snow), and air temperature. Useful for winter weather situations.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=handle_get_road_conditions,
    ))

    logger.info(f"Agent tool registry built with {registry.tool_count} tools")
    return registry
