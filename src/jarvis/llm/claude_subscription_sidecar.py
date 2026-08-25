"""Standalone Claude Agent SDK sidecar using newline-delimited JSON pipes."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from typing import Any, Callable

import claude_agent_sdk as _sdk


def _status_from(value: Any) -> int | None:
    status = getattr(value, "api_error_status", None)
    return status if isinstance(status, int) else None


async def _generate(request: dict, emit: Callable[[dict], None]) -> None:
    """Handle one generation without allowing failures to stop the loop."""
    request_id = request.get("id")
    model = request.get("model")
    system_prompt = request.get("system_prompt")
    prompt = request.get("prompt")
    streaming = request.get("stream") is True
    if not isinstance(request_id, int) or not all(
        isinstance(value, str) for value in (model, system_prompt, prompt)
    ):
        emit({"type": "error", "id": request_id, "status": None})
        return

    async def _deny_all_tool_use(tool_name, tool_input, context):
        emit({
            "type": "tool_denied",
            "id": request_id,
            "tool_name": str(tool_name)[:200],
        })
        return _sdk.PermissionResultDeny(
            message="text-generation-only backend: tool use is not permitted"
        )

    options = _sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt or None,
        permission_mode="default",
        tools=[],
        can_use_tool=_deny_all_tool_use,
        setting_sources=[],
        mcp_servers={},
        cwd=tempfile.gettempdir(),
        max_turns=1,
        include_partial_messages=streaming,
    )
    client = _sdk.ClaudeSDKClient(options=options)
    text_parts: list[str] = []
    result_message = None
    try:
        await client.connect()
        await client.query(prompt)
        async for message in client.receive_response():
            kind = type(message).__name__
            if kind == "AssistantMessage":
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text:
                        text_parts.append(text)
            elif kind == "StreamEvent" and streaming:
                event = getattr(message, "event", {}) or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {}) or {}
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text")
                        if isinstance(chunk, str) and chunk:
                            emit({
                                "type": "chunk",
                                "id": request_id,
                                "text": chunk,
                            })
            elif kind == "ResultMessage":
                result_message = message
                break
    except Exception as error:
        emit({"type": "error", "id": request_id, "status": _status_from(error)})
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if result_message is not None and bool(
        getattr(result_message, "is_error", False)
    ):
        emit({
            "type": "error",
            "id": request_id,
            "status": _status_from(result_message),
        })
        return
    emit({"type": "result", "id": request_id, "text": "".join(text_parts)})


def _emit_stdout(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    _emit_stdout({"type": "ready"})
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            _emit_stdout({"type": "error", "id": None, "status": None})
            continue
        if not isinstance(request, dict):
            _emit_stdout({"type": "error", "id": None, "status": None})
            continue
        if request.get("cmd") == "shutdown":
            break
        if request.get("cmd") != "generate":
            _emit_stdout({
                "type": "error",
                "id": request.get("id"),
                "status": None,
            })
            continue
        try:
            asyncio.run(_generate(request, _emit_stdout))
        except Exception:
            _emit_stdout({
                "type": "error",
                "id": request.get("id"),
                "status": None,
            })


if __name__ == "__main__":
    main()
