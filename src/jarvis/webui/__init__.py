"""🖥️ Jarvis control centre.

A local web interface for the whole assistant: live state, memory, tools,
security, technical readings, and settings. The daemon serves it in-process
so every view reads live objects rather than a copy.
"""

from __future__ import annotations

import webbrowser
from typing import Optional

from jarvis.debug import debug_log

from .server import WebUIConfig, WebUIMode, WebUIServer, create_app, resolve_token


__all__ = [
    "WebUIConfig",
    "WebUIMode",
    "WebUIServer",
    "create_app",
    "resolve_token",
    "start_from_settings",
]


def start_from_settings(cfg) -> Optional[WebUIServer]:
    """Start the control centre for a loaded ``Settings``.

    Returns the running server, or ``None`` when it is switched off or the
    port cannot be bound. A control centre that fails to come up never takes
    the daemon down with it: the assistant's job is answering, not serving
    dashboards.
    """
    if not getattr(cfg, "webui_enabled", True):
        print("🖥️ Control centre disabled", flush=True)
        return None

    webui_cfg = WebUIConfig(
        host=cfg.webui_bind_host,
        port=cfg.webui_port,
        token=resolve_token(cfg.webui_bind_host, cfg.webui_token),
        mode=WebUIMode.DAEMON_ATTACHED,
    )
    server = WebUIServer(webui_cfg)
    try:
        server.start()
    except OSError as exc:
        print(f"🖥️ Control centre could not bind port {webui_cfg.port}: {exc}", flush=True)
        debug_log(f"webui bind failed: {exc}", "webui")
        return None

    print(f"🖥️ Control centre: {server.url}", flush=True)
    if not webui_cfg.is_loopback:
        print("     🔑 Reachable from this network. The token above is required.", flush=True)

    if getattr(cfg, "webui_open_browser", False):
        try:
            webbrowser.open(server.url)
        except Exception as exc:
            debug_log(f"webui browser open failed: {exc}", "webui")

    return server
