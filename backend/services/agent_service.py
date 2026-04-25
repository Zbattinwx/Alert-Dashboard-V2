"""
AI Agent Service for Alert Dashboard V2.

Implements a tool-calling agent loop using Ollama.
Supports both native Ollama tool calling AND a text-based fallback
for models that output tool calls as JSON in their response text.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from ..config import get_settings
from .agent_tools import ToolRegistry, build_default_tool_registry

logger = logging.getLogger(__name__)


def _build_tool_list_for_prompt(registry: ToolRegistry) -> str:
    """Build a human-readable tool list for the system prompt."""
    lines = []
    for tool_def in registry.get_ollama_tools():
        fn = tool_def["function"]
        name = fn["name"]
        desc = fn["description"]
        params = fn.get("parameters", {}).get("properties", {})
        param_parts = []
        for pname, pinfo in params.items():
            param_parts.append(f'{pname}: {pinfo.get("description", "")}')
        param_str = ", ".join(param_parts) if param_parts else "no parameters"
        lines.append(f"- {name}({param_str}): {desc}")
    return "\n".join(lines)


AGENT_SYSTEM_PROMPT_TEMPLATE = """You are a weather operations AI agent for a severe weather alert dashboard.

You have access to tools that query real-time weather data. You MUST call tools to get data before answering -- NEVER guess or fabricate weather information.

## How to Call Tools
To call a tool, output ONLY a JSON block like this (no other text before it):
<tool_call>
{{"name": "tool_name", "arguments": {{"param": "value"}}}}
</tool_call>

You can call multiple tools by outputting multiple <tool_call> blocks.
After you receive tool results, write your final answer using that data.
Do NOT describe what tools you would call -- just call them directly.

## Available Tools
{tool_list}

## Your Personality
You are direct, knowledgeable, and slightly sassy. You take severe weather seriously but keep things conversational. You're the experienced meteorologist who reads the data before speaking.

## Tool Usage Guidelines
- For current conditions: call get_active_alerts
- For severe weather questions: also call get_storm_reports and get_spc_outlook
- For winter weather: also call get_road_conditions and get_wind_gusts
- For a full briefing: call get_active_alerts, get_spc_outlook, get_storm_reports, and get_wind_gusts
- Summarize tool results naturally for the user -- don't dump raw data

## Safety Priority
Always lead with life-threatening information:
1. Tornado warnings and emergencies FIRST
2. Severe thunderstorm warnings with destructive potential
3. Flash flood warnings
4. Winter storm warnings and blizzard warnings
5. Everything else

## Response Style
- Be concise but thorough
- Use plain language, explain meteorological terms
- When there's no severe weather, say so confidently
- NEVER fabricate alerts, storm reports, or weather data"""


@dataclass
class ToolCallRecord:
    """Record of a single tool call made during agent execution."""

    tool: str
    arguments: dict
    result: str = ""
    status: str = "executing"  # executing, success, error
    duration_ms: float = 0


@dataclass
class AgentResponse:
    """Complete response from the agent."""

    content: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    rounds: int = 0
    model: str = ""
    total_duration_ms: float = 0


def _parse_text_tool_calls(text: str) -> list[dict]:
    """
    Parse tool calls from model text output.

    Supports multiple formats:
    1. <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    2. ```json\n{"name": "...", "arguments": {...}}\n```
    3. Bare JSON {"name": "...", "arguments": {...}} on its own line
    """
    tool_calls = []

    # Pattern 1: <tool_call> tags
    tag_pattern = re.compile(
        r'<tool_call>\s*(\{.*?\})\s*</tool_call>',
        re.DOTALL
    )
    for match in tag_pattern.finditer(text):
        try:
            data = json.loads(match.group(1))
            if "name" in data:
                tool_calls.append(data)
        except json.JSONDecodeError:
            continue

    if tool_calls:
        return tool_calls

    # Pattern 2: ```json blocks with tool-like content
    code_pattern = re.compile(
        r'```(?:json)?\s*(\{[^`]*?"name"\s*:\s*"[^"]+?"[^`]*?\})\s*```',
        re.DOTALL
    )
    for match in code_pattern.finditer(text):
        try:
            data = json.loads(match.group(1))
            if "name" in data:
                tool_calls.append(data)
        except json.JSONDecodeError:
            continue

    if tool_calls:
        return tool_calls

    # Pattern 3: Bare JSON objects with "name" key on their own
    bare_pattern = re.compile(
        r'\{[^{}]*?"name"\s*:\s*"(\w+)"[^{}]*?"arguments"\s*:\s*\{[^{}]*?\}[^{}]*?\}',
        re.DOTALL
    )
    for match in bare_pattern.finditer(text):
        try:
            data = json.loads(match.group(0))
            if "name" in data:
                tool_calls.append(data)
        except json.JSONDecodeError:
            continue

    return tool_calls


def _strip_tool_calls_from_text(text: str) -> str:
    """Remove tool call markup from text, leaving only the conversational parts."""
    # Remove <tool_call> blocks
    text = re.sub(r'<tool_call>\s*\{.*?\}\s*</tool_call>', '', text, flags=re.DOTALL)
    # Remove ```json tool blocks
    text = re.sub(r'```(?:json)?\s*\{[^`]*?"name"\s*:[^`]*?\}\s*```', '', text, flags=re.DOTALL)
    # Clean up extra whitespace
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


class AgentService:
    """
    AI Agent with tool-calling capabilities.

    Supports both Ollama native tool calling and text-based fallback
    for models that don't support native tool calling.
    """

    def __init__(
        self,
        ollama_host: str,
        model: str,
        tool_registry: ToolRegistry,
        max_tool_rounds: int = 5,
        tool_timeout: int = 30,
        llm_timeout: int = 120,
    ):
        self.host = ollama_host.rstrip("/")
        self.model = model
        self.tools = tool_registry
        self.max_tool_rounds = max_tool_rounds
        self.tool_timeout = tool_timeout
        self.llm_timeout = llm_timeout
        self._conversation_history: list[dict[str, Any]] = []
        self._is_available: Optional[bool] = None
        self._last_health_check: Optional[datetime] = None

        # Build the system prompt with tool descriptions baked in
        tool_list = _build_tool_list_for_prompt(tool_registry)
        self._system_prompt = AGENT_SYSTEM_PROMPT_TEMPLATE.format(tool_list=tool_list)

    async def check_health(self, force: bool = False) -> bool:
        """Check if Ollama is running and the agent model is available."""
        if not force and self._last_health_check:
            elapsed = (datetime.now(timezone.utc) - self._last_health_check).total_seconds()
            if elapsed < 30 and self._is_available is not None:
                return self._is_available

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.host}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status != 200:
                        self._is_available = False
                        self._last_health_check = datetime.now(timezone.utc)
                        return False

                    data = await response.json()
                    models = [m.get("name", "") for m in data.get("models", [])]
                    model_base = self.model.split(":")[0]
                    self._is_available = any(
                        self.model in m or m.startswith(model_base)
                        for m in models
                    )

                    if not self._is_available:
                        logger.warning(
                            f"Agent model {self.model} not found. Available: {models}"
                        )
                    else:
                        logger.debug(f"Agent health check passed. Model {self.model} available.")

                    self._last_health_check = datetime.now(timezone.utc)
                    return self._is_available

        except Exception as e:
            logger.warning(f"Agent health check failed: {e}")
            self._is_available = False
            self._last_health_check = datetime.now(timezone.utc)
            return False

    async def run(self, user_message: str, include_history: bool = True) -> AgentResponse:
        """
        Run the agent loop for a user message.

        1. Build messages (system + history + user)
        2. Call Ollama (with native tools if supported)
        3. Check for tool calls (native or text-based)
        4. Execute tools, feed results back, loop
        5. Return final text response with tool call log
        """
        start_time = time.time()
        tool_calls_log: list[ToolCallRecord] = []

        # Build initial messages
        messages = self._build_messages(user_message, include_history)

        for round_num in range(self.max_tool_rounds):
            # Call Ollama
            try:
                response_data = await self._call_ollama(messages)
            except Exception as e:
                logger.error(f"Ollama call failed on round {round_num + 1}: {e}")
                return AgentResponse(
                    content=f"I had trouble connecting to my brain. Error: {e}",
                    tool_calls=tool_calls_log,
                    rounds=round_num + 1,
                    model=self.model,
                    total_duration_ms=(time.time() - start_time) * 1000,
                )

            message = response_data.get("message", {})
            native_tool_calls = message.get("tool_calls")
            content = message.get("content", "")

            # Determine tool calls: native or text-based fallback
            parsed_calls = []

            if native_tool_calls:
                # Native Ollama tool calling worked
                for tc in native_tool_calls:
                    fn = tc.get("function", {})
                    parsed_calls.append({
                        "name": fn.get("name", "unknown"),
                        "arguments": fn.get("arguments", {}),
                    })
                logger.info(f"Round {round_num + 1}: {len(parsed_calls)} native tool calls")
            elif content:
                # Try text-based fallback parsing
                parsed_calls = _parse_text_tool_calls(content)
                if parsed_calls:
                    logger.info(
                        f"Round {round_num + 1}: {len(parsed_calls)} text-parsed tool calls"
                    )

            # If no tool calls found, this is the final response
            if not parsed_calls:
                final_content = content or "I wasn't able to generate a response."
                self._save_to_history(user_message, final_content)
                return AgentResponse(
                    content=final_content,
                    tool_calls=tool_calls_log,
                    rounds=round_num + 1,
                    model=response_data.get("model", self.model),
                    total_duration_ms=(time.time() - start_time) * 1000,
                )

            # Add assistant message to conversation
            messages.append({
                "role": "assistant",
                "content": content,
            })

            # Execute each tool call
            tool_results = []
            for tc_data in parsed_calls:
                fn_name = tc_data.get("name", "unknown")
                fn_args = tc_data.get("arguments", {})

                # Ensure arguments is a dict
                if isinstance(fn_args, str):
                    try:
                        fn_args = json.loads(fn_args)
                    except json.JSONDecodeError:
                        fn_args = {}

                record = ToolCallRecord(
                    tool=fn_name,
                    arguments=fn_args,
                    status="executing",
                )
                tool_calls_log.append(record)

                logger.info(f"Agent calling tool: {fn_name}({fn_args})")

                # Execute with timeout
                tool_start = time.time()
                try:
                    result = await asyncio.wait_for(
                        self.tools.execute(fn_name, fn_args),
                        timeout=self.tool_timeout,
                    )
                    record.result = result
                    record.status = "success"
                except asyncio.TimeoutError:
                    record.result = f"Tool '{fn_name}' timed out after {self.tool_timeout}s"
                    record.status = "error"
                    result = record.result
                except Exception as e:
                    record.result = f"Tool error: {e}"
                    record.status = "error"
                    result = record.result

                record.duration_ms = (time.time() - tool_start) * 1000
                logger.info(
                    f"Tool {fn_name} completed in {record.duration_ms:.0f}ms "
                    f"({record.status})"
                )

                tool_results.append(f"[{fn_name} result]\n{result}")

            # Add all tool results as a single user message
            # (text-based fallback doesn't use "tool" role)
            combined_results = "\n\n".join(tool_results)
            messages.append({
                "role": "user",
                "content": f"Here are the tool results:\n\n{combined_results}\n\nNow provide your answer based on this data. Do NOT call any more tools unless you need additional information.",
            })

        # Exhausted all rounds -- do one final call without tools to get a summary
        try:
            response_data = await self._call_ollama(messages, include_tools=False)
            final_content = response_data.get("message", {}).get("content", "")
        except Exception:
            final_content = ""

        final_content = final_content or "I used all my available tool rounds but gathered the data above."
        self._save_to_history(user_message, final_content)
        return AgentResponse(
            content=final_content,
            tool_calls=tool_calls_log,
            rounds=self.max_tool_rounds,
            model=self.model,
            total_duration_ms=(time.time() - start_time) * 1000,
        )

    async def _call_ollama(
        self, messages: list[dict], include_tools: bool = True
    ) -> dict:
        """Call Ollama /api/chat, optionally with tool definitions."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        if include_tools:
            payload["tools"] = self.tools.get_ollama_tools()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.host}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.llm_timeout),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"Ollama API error {response.status}: {error_text}")
                return await response.json()

    def _build_messages(
        self, user_message: str, include_history: bool
    ) -> list[dict[str, Any]]:
        """Build the messages list for an Ollama call."""
        messages: list[dict[str, Any]] = []

        # System prompt (includes tool descriptions for text-based fallback)
        messages.append({
            "role": "system",
            "content": self._system_prompt,
        })

        # Conversation history (last 10 exchanges = 20 messages)
        if include_history and self._conversation_history:
            messages.extend(self._conversation_history[-20:])

        # Current user message
        messages.append({
            "role": "user",
            "content": user_message,
        })

        return messages

    def _save_to_history(self, user_message: str, assistant_response: str):
        """Save a completed exchange to conversation history."""
        self._conversation_history.append({
            "role": "user",
            "content": user_message,
        })
        self._conversation_history.append({
            "role": "assistant",
            "content": assistant_response,
        })

        # Trim history to prevent unbounded growth
        max_history = 40  # 20 exchanges
        if len(self._conversation_history) > max_history:
            self._conversation_history = self._conversation_history[-max_history:]

    async def analyze_storm_cells(self, cells: list) -> str:
        """
        One-shot LLM call to generate a brief proactive notification about notable storm cells.
        Does not use the tool-calling loop — cell data is passed directly as context.
        """
        import json

        summaries = []
        for cell in cells:
            s: dict = {
                "cell_id": cell.cell_id,
                "threat_level": cell.threat_level,
                "severity_score": cell.severity_score,
                "trend": cell.trend,
                "max_dbz": round(cell.max_reflectivity_dbz),
                "rotation": cell.rotation_detected,
            }
            if cell.rotation_velocity_ms is not None:
                s["rotation_velocity_ms"] = round(cell.rotation_velocity_ms)
            if cell.tvs_detected:
                s["tvs_detected"] = True
            if cell.debris_signature:
                s["debris_signature"] = True
            if cell.hail_indicated:
                s["hail_indicated"] = True
            if cell.motion_speed_kph > 0:
                s["motion"] = (
                    f"{cell.motion_direction_deg:.0f}° at {cell.motion_speed_kph:.0f} kph"
                )
            summaries.append(s)

        prompt = (
            "Storm monitoring alert. The following storm cell(s) have notable new developments "
            "the operator may not yet be aware of. Write a concise 2-3 sentence notification. "
            "Lead with the most dangerous element. Be specific and direct — no hedging phrases "
            "like 'it appears' or 'I notice'.\n\n"
            f"Cells:\n{json.dumps(summaries, indent=2)}"
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a proactive severe weather monitoring agent. "
                    "Generate brief, direct notifications about storm developments for a human operator."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response_data = await self._call_ollama(messages, include_tools=False)
            content = response_data.get("message", {}).get("content", "").strip()
            return content
        except Exception as e:
            logger.error(f"analyze_storm_cells LLM call failed: {e}")
            return ""

    def clear_history(self):
        """Clear conversation history."""
        self._conversation_history.clear()
        logger.debug("Agent conversation history cleared")

    def get_history(self) -> list[dict[str, Any]]:
        """Get conversation history."""
        return [
            {
                "role": msg["role"],
                "content": msg["content"],
            }
            for msg in self._conversation_history
        ]

    def get_status(self) -> dict[str, Any]:
        """Get agent status info."""
        return {
            "model": self.model,
            "host": self.host,
            "is_available": self._is_available,
            "last_health_check": (
                self._last_health_check.isoformat()
                if self._last_health_check
                else None
            ),
            "history_length": len(self._conversation_history),
            "tool_count": self.tools.tool_count,
            "max_tool_rounds": self.max_tool_rounds,
        }


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_service: Optional[AgentService] = None


def get_agent_service() -> AgentService:
    """Get the global AgentService instance."""
    global _service
    if _service is None:
        settings = get_settings()
        _service = AgentService(
            ollama_host=settings.ollama_host,
            model=settings.agent_model,
            tool_registry=build_default_tool_registry(),
            max_tool_rounds=settings.agent_max_tool_rounds,
            tool_timeout=settings.agent_tool_timeout,
            llm_timeout=settings.llm_timeout,
        )
    return _service


async def start_agent_service() -> bool:
    """Start the agent service and verify it's working."""
    global _service
    settings = get_settings()

    if not settings.agent_enabled:
        logger.info("Agent service is disabled in settings")
        return False

    _service = AgentService(
        ollama_host=settings.ollama_host,
        model=settings.agent_model,
        tool_registry=build_default_tool_registry(),
        max_tool_rounds=settings.agent_max_tool_rounds,
        tool_timeout=settings.agent_tool_timeout,
        llm_timeout=settings.llm_timeout,
    )

    is_healthy = await _service.check_health(force=True)
    if is_healthy:
        logger.info(
            f"Agent service started with model {_service.model} "
            f"({_service.tools.tool_count} tools)"
        )
    else:
        logger.warning("Agent service started but model is not available in Ollama")

    return is_healthy


async def stop_agent_service():
    """Stop the agent service."""
    global _service
    if _service:
        _service.clear_history()
        _service = None
    logger.info("Agent service stopped")
