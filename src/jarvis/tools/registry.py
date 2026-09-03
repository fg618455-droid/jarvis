from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List
import sys
import re
import requests
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os

from .builtin.screenshot import ScreenshotTool
from .builtin.web_search import WebSearchTool
from .builtin.local_files import LocalFilesTool
from .builtin.vault_search import VaultSearchTool
from .builtin.fetch_web_page import FetchWebPageTool
from .builtin.nutrition.log_meal import LogMealTool
from .builtin.nutrition.fetch_meals import FetchMealsTool
from .builtin.nutrition.delete_meal import DeleteMealTool
from .builtin.refresh_mcp_tools import RefreshMCPToolsTool
from .builtin.weather import WeatherTool
from .builtin.time_tool import TimeTool
from .builtin.conversation_mode import ConversationModeTool
from .builtin.stop import StopTool
from .builtin.open_on_computer import OpenOnComputerTool
from .builtin.tool_search import ToolSearchTool
from .builtin.ask_crew import AskCrewTool
from .builtin.browser_interact import BrowserInteractTool
from .builtin.desktop_interact import DesktopInteractTool
from .builtin.system_manager import SystemManagerTool
from .builtin.memory_provenance import MemoryProvenanceTool
from .builtin.exam_countdown import ExamCountdownTool
from .types import ToolExecutionResult
from ..config import Settings
from .external.mcp_client import MCPClient
from .external.mcp_preflight import preflight_mcp_config
from ..debug import debug_log
from ..runtime import Phase, record_tool, set_phase_if


# Registry of all builtin tools
BUILTIN_TOOLS = {
    "screenshot": ScreenshotTool(),
    "webSearch": WebSearchTool(),
    "localFiles": LocalFilesTool(),
    "fetchWebPage": FetchWebPageTool(),
    "logMeal": LogMealTool(),
    "fetchMeals": FetchMealsTool(),
    "deleteMeal": DeleteMealTool(),
    "refreshMCPTools": RefreshMCPToolsTool(),
    "getWeather": WeatherTool(),
    "getTime": TimeTool(),
    "stop": StopTool(),
    "setConversationMode": ConversationModeTool(),
    "openOnComputer": OpenOnComputerTool(),
    "toolSearchTool": ToolSearchTool(),
    "askCrew": AskCrewTool(),
    "memoryProvenance": MemoryProvenanceTool(),
    "getExamCountdown": ExamCountdownTool(),
}


def configure_vault_search_tool(cfg) -> None:
    """Expose vaultSearch only while a valid vault is configured."""
    if getattr(cfg, "obsidian_vault_path", None):
        BUILTIN_TOOLS["vaultSearch"] = VaultSearchTool()
    else:
        BUILTIN_TOOLS.pop("vaultSearch", None)


def configure_computer_interaction_tools(cfg) -> None:
    """Register semantic computer control only after explicit opt-in."""
    if getattr(cfg, "computer_interaction_enabled", False):
        if not isinstance(BUILTIN_TOOLS.get("browserInteract"), BrowserInteractTool):
            BUILTIN_TOOLS["browserInteract"] = BrowserInteractTool()
        if not isinstance(BUILTIN_TOOLS.get("desktopInteract"), DesktopInteractTool):
            BUILTIN_TOOLS["desktopInteract"] = DesktopInteractTool()
    else:
        browser_tool = BUILTIN_TOOLS.pop("browserInteract", None)
        if isinstance(browser_tool, BrowserInteractTool):
            browser_tool.close()
        BUILTIN_TOOLS.pop("desktopInteract", None)


def configure_system_management_tool(cfg) -> None:
    """Register structured system management only after explicit opt-in."""
    if getattr(cfg, "system_management_enabled", False):
        if not isinstance(BUILTIN_TOOLS.get("systemManager"), SystemManagerTool):
            BUILTIN_TOOLS["systemManager"] = SystemManagerTool()
    else:
        BUILTIN_TOOLS.pop("systemManager", None)

# Global MCP tools cache
_mcp_tools_cache: Dict[str, "ToolSpec"] = {}
_mcp_tools_cache_lock = threading.Lock()
_mcp_config_cache: Dict[str, Any] = {}
_mcp_errors_cache: Dict[str, str] = {}


def initialize_mcp_tools(mcps_config: Dict[str, Any], verbose: bool = True) -> Tuple[Dict[str, "ToolSpec"], Dict[str, str]]:
    """
    Initialize MCP tools cache at startup.

    Args:
        mcps_config: MCP server configuration
        verbose: Whether to print status messages

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    global _mcp_tools_cache, _mcp_config_cache, _mcp_errors_cache

    with _mcp_tools_cache_lock:
        _mcp_config_cache = mcps_config or {}
        _mcp_tools_cache, errors = discover_mcp_tools(mcps_config)
        _mcp_errors_cache = errors

        if verbose and _mcp_tools_cache:
            debug_log(f"MCP tools cache initialized with {len(_mcp_tools_cache)} tools", "mcp")

        return _mcp_tools_cache.copy(), errors


def get_cached_mcp_tools() -> Dict[str, "ToolSpec"]:
    """Get cached MCP tools without rediscovering."""
    with _mcp_tools_cache_lock:
        return _mcp_tools_cache.copy()


def get_cached_mcp_errors() -> Dict[str, str]:
    """Return the latest per-server discovery failures for status views."""
    with _mcp_tools_cache_lock:
        return _mcp_errors_cache.copy()


def refresh_mcp_tools(verbose: bool = True) -> Tuple[Dict[str, "ToolSpec"], Dict[str, str]]:
    """
    Refresh MCP tools cache by rediscovering all tools.

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    global _mcp_tools_cache, _mcp_errors_cache

    with _mcp_tools_cache_lock:
        if not _mcp_config_cache:
            debug_log("No MCP config cached, skipping refresh", "mcp")
            _mcp_errors_cache = {}
            return {}, {}

        if verbose:
            print("🔄 Refreshing MCP tools...", flush=True)

        _mcp_tools_cache, errors = discover_mcp_tools(_mcp_config_cache)
        _mcp_errors_cache = errors

        if verbose:
            print(f"  ✅ Found {len(_mcp_tools_cache)} MCP tools", flush=True)

        debug_log(f"MCP tools cache refreshed with {len(_mcp_tools_cache)} tools", "mcp")
        return _mcp_tools_cache.copy(), errors


def is_mcp_cache_initialized() -> bool:
    """Check if MCP tools cache has been initialized."""
    with _mcp_tools_cache_lock:
        return len(_mcp_config_cache) > 0 or len(_mcp_tools_cache) > 0



# ToolSpec for MCP compatibility
@dataclass(frozen=True)
class ToolSpec:
    name: str  # canonical tool identifier (camelCase)
    description: str  # Human-readable description (matches MCP format)
    inputSchema: Optional[Dict[str, Any]] = None  # JSON Schema for arguments (matches MCP format)


def discover_mcp_tools(mcps_config: Dict[str, Any]) -> Tuple[Dict[str, ToolSpec], Dict[str, str]]:
    """Discover all tools from configured MCP servers and create ToolSpec entries for them.

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    if not mcps_config:
        return {}, {}

    try:
        client = MCPClient(mcps_config)
        discovered_tools = {}
        errors: Dict[str, str] = {}

        for server_name in mcps_config.keys():
            try:
                preflight = preflight_mcp_config(mcps_config[server_name])
                if not preflight.available:
                    errors[server_name] = preflight.reason
                    debug_log(f"MCP server '{server_name}' disabled: {preflight.reason}", "mcp")
                    continue
                tools = client.list_tools(server_name)
                for tool_info in tools:
                    tool_name = tool_info.get("name")
                    if not tool_name:
                        continue

                    # Create a unique tool name: server__toolname
                    full_tool_name = f"{server_name}__{tool_name}"

                    # Create a ToolSpec for this MCP tool
                    description = tool_info.get("description", f"Tool from {server_name} MCP server")
                    input_schema = tool_info.get("inputSchema", {"type": "object", "properties": {}, "required": []})
                    discovered_tools[full_tool_name] = ToolSpec(
                        name=full_tool_name,
                        description=description,
                        inputSchema=input_schema
                    )

            except BaseException as e:
                # ExceptionGroups (from anyio TaskGroup) wrap the real cause;
                # extract the first sub-exception for a useful error message.
                cause = e
                if hasattr(e, "exceptions") and e.exceptions:
                    cause = e.exceptions[0]
                detail = str(cause) or type(cause).__name__
                debug_log(f"Failed to discover tools from MCP server '{server_name}': {detail}", "mcp")
                errors[server_name] = detail
                continue

        return discovered_tools, errors

    except Exception as e:
        detail = str(e) or type(e).__name__
        debug_log(f"Failed to discover MCP tools: {detail}", "mcp")
        return {}, {"_global": detail}


def generate_tools_json_schema(allowed_tools: Optional[List[str]] = None, mcp_tools: Optional[Dict[str, ToolSpec]] = None) -> List[Dict[str, Any]]:
    """
    Generate tools in OpenAI-compatible JSON schema format for native tool calling.

    This format is supported by Ollama for models with native tool calling support
    (Llama 3.1+, Llama 3.2, Qwen 3, Mistral, etc.).

    Returns a list of tool definitions in this format:
    [
        {
            "type": "function",
            "function": {
                "name": "toolName",
                "description": "Tool description",
                "parameters": {
                    "type": "object",
                    "properties": {...},
                    "required": [...]
                }
            }
        }
    ]
    """
    names = list(allowed_tools or list(BUILTIN_TOOLS.keys()))
    tools: List[Dict[str, Any]] = []

    # Add built-in tools
    for tool_name in names:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            continue

        tool_def = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema or {"type": "object", "properties": {}, "required": []},
            }
        }
        tools.append(tool_def)

    # Add discovered MCP tools
    if mcp_tools:
        for tool_name, spec in mcp_tools.items():
            if tool_name in names:  # Only include if allowed
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.inputSchema or {"type": "object", "properties": {}, "required": []},
                    }
                }
                tools.append(tool_def)

    return tools


def generate_tools_description(allowed_tools: Optional[List[str]] = None, mcp_tools: Optional[Dict[str, ToolSpec]] = None) -> str:
    """Produce a compact tool help string for the system prompt using OpenAI standard format."""
    names = list(allowed_tools or list(BUILTIN_TOOLS.keys()))
    lines: List[str] = []
    lines.append("Tool-use protocol: Use the tool_calls field in your response:")
    lines.append('tool_calls: [{"id": "call_<id>", "type": "function", "function": {"name": "<toolName>", "arguments": "<json_string>"}}]')
    lines.append("\nAvailable tools and when to use them:")

    # Add built-in tools
    for tool_name in names:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            continue
        lines.append(f"\n{tool.name}: {tool.description}")
        if tool.inputSchema:
            # Extract a simple parameter summary from the JSON schema
            props = tool.inputSchema.get("properties", {})
            required = tool.inputSchema.get("required", [])
            param_descriptions = []
            for prop_name, prop_def in props.items():
                prop_type = prop_def.get("type", "any")
                is_required = prop_name in required
                req_marker = " (required)" if is_required else ""
                param_descriptions.append(f"{prop_name}: {prop_type}{req_marker}")
            if param_descriptions:
                lines.append(f"Input: {', '.join(param_descriptions)}")

    # Add discovered MCP tools
    if mcp_tools:
        for tool_name, spec in mcp_tools.items():
            if tool_name in names:  # Only include if allowed
                lines.append(f"\n{spec.name}: {spec.description}")
                if spec.inputSchema:
                    # Extract a simple parameter summary from the JSON schema
                    props = spec.inputSchema.get("properties", {})
                    required = spec.inputSchema.get("required", [])
                    param_descriptions = []
                    for prop_name, prop_def in props.items():
                        prop_type = prop_def.get("type", "any")
                        is_required = prop_name in required
                        req_marker = " (required)" if is_required else ""
                        param_descriptions.append(f"{prop_name}: {prop_type}{req_marker}")
                    if param_descriptions:
                        lines.append(f"Input: {', '.join(param_descriptions)}")

    return "\n".join(lines)

def _normalize_time_range(args: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    since: Optional[str] = None
    until: Optional[str] = None
    if args and isinstance(args, dict):
        try:
            since_val = args.get("since_utc")
            since = str(since_val) if since_val else None
        except Exception:
            since = None
        try:
            until_val = args.get("until_utc")
            until = str(until_val) if until_val else None
        except Exception:
            until = None
    if since is None and until is None:
        # Default last 24h
        return (now - timedelta(days=1)).isoformat(), now.isoformat()
    if since is None and until is not None:
        # backfill 24h prior to until
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except Exception:
            until_dt = now
        return (until_dt - timedelta(days=1)).isoformat(), until_dt.isoformat()
    if since is not None and until is None:
        return since, now.isoformat()
    return since or (now - timedelta(days=1)).isoformat(), until or now.isoformat()


def run_tool_with_retries(
    db,
    cfg: Settings,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    system_prompt: str,
    original_prompt: str,
    redacted_text: str,
    max_retries: int = 1,
    language: Optional[str] = None,
    deadline: Optional[Any] = None,
    memory_snippets: Optional[List[Any]] = None,
) -> ToolExecutionResult:
    """Run one tool through the security gate and time it.

    Every tool the assistant runs, builtin or MCP, passes through here, so
    this is where a turn learns what it called and how long each call took.

    ``deadline`` (a ``jarvis.llm.route.RequestDeadline``, optional) is the
    reply turn's overall budget. Tools that make their own blocking LLM or
    network call read it via ``ToolContext.bounded_timeout`` so that call
    can't outlive the turn just because its own configured ceiling (e.g.
    ``llm_tools_timeout_sec``, 300s by default) is far larger than what
    actually remains.
    """
    begun = time.perf_counter()
    set_phase_if(Phase.THINKING, Phase.TOOL)
    try:
        result = _run_tool_with_retries(
            db, cfg, tool_name, tool_args, system_prompt,
            original_prompt, redacted_text, max_retries, language,
            deadline,
            memory_snippets,
        )
    except Exception as exc:
        record_tool(
            (tool_name or "").strip(),
            (time.perf_counter() - begun) * 1000.0,
            ok=False,
            error=str(exc),
        )
        raise
    finally:
        set_phase_if(Phase.TOOL, Phase.THINKING)
    record_tool(
        (tool_name or "").strip(),
        (time.perf_counter() - begun) * 1000.0,
        ok=result.success,
        error=result.error_message,
        confirmed=None if result.success else _was_denied(result),
    )
    return result


def _was_denied(result: ToolExecutionResult) -> Optional[bool]:
    """Whether a failed result was a refusal rather than a fault."""
    message = result.error_message or ""
    return False if "denied by security confirmation" in message else None


def _run_tool_with_retries(
    db,
    cfg: Settings,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    system_prompt: str,
    original_prompt: str,
    redacted_text: str,
    max_retries: int = 1,
    language: Optional[str] = None,
    deadline: Optional[Any] = None,
    memory_snippets: Optional[List[Any]] = None,
) -> ToolExecutionResult:
    # Normalize tool name to canonical camelCase
    raw_name = (tool_name or "").strip()
    name = raw_name

    # Friendly user print helper (non-debug only)
    def _user_print(message: str) -> None:
        # 4-space indent: tool messages happen INSIDE an agentic-loop
        # turn. The turn header (`  🔁 Turn N/M`) sits at 2 spaces, so
        # per-tool activity nests one level deeper for visual hierarchy.
        if not getattr(cfg, "voice_debug", False):
            try:
                print(f"    {message}")
            except Exception:
                pass

    def _security_allows_tool() -> bool:
        from jarvis.security.gate import SecurityGate

        try:
            gate = SecurityGate.get_or_create(cfg)
            return gate.confirm(raw_name, tool_args or {})
        except Exception as exc:
            debug_log(f"security gate failed closed for {raw_name}: {exc}", "security")
            return False

    def _denied_result() -> ToolExecutionResult:
        _user_print(f"🔒 {raw_name} was denied by the security gate.")
        return ToolExecutionResult(
            success=False,
            reply_text=None,
            error_message="Tool execution denied by security confirmation.",
        )

    # Check if tool name is a discovered MCP tool (server__toolname format)
    if "__" in raw_name:
        server_name, mcp_tool_name = raw_name.split("__", 1)
        mcps_config = getattr(cfg, "mcps", {})
        if mcps_config and server_name in mcps_config:
            if not _security_allows_tool():
                return _denied_result()
            try:
                preflight = preflight_mcp_config(mcps_config[server_name])
                if not preflight.available:
                    return ToolExecutionResult.failure(
                        preflight.code, preflight.reason, phase="preflight"
                    )
                if MCPClient is None:
                    return ToolExecutionResult.failure("unsupported", "MCP support is not installed.", phase="preflight")

                client = MCPClient(mcps_config)
                result = client.invoke_tool(server_name=server_name, tool_name=mcp_tool_name, arguments=tool_args or {})
                is_error = bool(result.get("isError", False))
                text = result.get("text") or None
                if is_error:
                    return ToolExecutionResult.failure("execution_failed", text or "The MCP tool reported an error.",
                        technical_details=text)
                if not text:
                    return ToolExecutionResult.failure("execution_failed", "The MCP tool returned no usable result.",
                        technical_details="empty MCP response")
                return ToolExecutionResult(success=True, reply_text=text)
            except Exception as e:
                detail = str(e) or type(e).__name__
                # ``reply_text`` remains the safe user-facing contract while
                # ``error_message`` preserves the legacy diagnostic detail
                # for the planner and existing callers.
                return ToolExecutionResult(
                    success=False,
                    reply_text=f"MCP tool '{raw_name}' could not run.",
                    error_message=detail,
                    error_code="execution_failed",
                    technical_details=detail,
                )

    # Check builtin tools first
    if name in BUILTIN_TOOLS:
        if not _security_allows_tool():
            return _denied_result()
        tool = BUILTIN_TOOLS[name]
        return tool.execute(
            db=db,
            cfg=cfg,
            tool_args=tool_args,
            system_prompt=system_prompt,
            original_prompt=original_prompt,
            redacted_text=redacted_text,
            max_retries=max_retries,
            user_print=_user_print,
            language=language,
            deadline=deadline,
            memory_snippets=memory_snippets,
        )

    # Unknown tool
    debug_log(f"unknown tool requested: {tool_name}", "tools")
    return ToolExecutionResult.failure("invalid_config", f"Unknown tool: {tool_name}", phase="routing")


