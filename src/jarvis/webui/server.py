"""🖥️ Control centre HTTP server.

Serves the control centre from inside the daemon process, so every view
reads the same live objects the voice path is using: no IPC, no mirrored
state, no second process to keep in step.

The interface can approve tool execution and rewrite ``config.json``, so a
loopback port is not automatically safe: any page open in the same browser
can post to it. Three guards stand in front of every request.

    Host allowlist   On loopback only local names are served, which stops a
                     hostile domain that resolves to 127.0.0.1 (DNS
                     rebinding) from reaching the API.
    Write header     Every mutating method must carry ``X-Jarvis-UI``. A
                     plain HTML form cannot set a custom header, so classic
                     cross-site form posts are refused.
    Token            Once the bind address leaves loopback the allowlist can
                     no longer enumerate valid names, so a token guards every
                     request instead.

No response carries cross-origin headers.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory

from jarvis.debug import debug_log
from jarvis.security.web_confirm import get_web_confirmations


UI_HEADER = "X-Jarvis-UI"
TOKEN_HEADER = "X-Jarvis-Token"
TOKEN_QUERY = "t"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
TOKEN_BYTES = 16

STATIC_DIR = Path(__file__).parent / "static"


@dataclass(frozen=True)
class WebUIConfig:
    """Everything the server needs to bind and guard itself."""
    host: str
    port: int
    token: str

    @property
    def is_loopback(self) -> bool:
        return is_loopback_host(self.host)

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        base = f"http://{host}:{self.port}"
        return f"{base}/?{TOKEN_QUERY}={self.token}" if self.token else base


def is_loopback_host(host: str) -> bool:
    """Whether a bind address or Host header names this machine only."""
    name = (host or "").strip().strip("[]").lower()
    if name in LOOPBACK_NAMES or name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def resolve_token(bind_host: str, configured: str) -> str:
    """Return the token this run should demand.

    Loopback needs none: reaching the port already means reaching the
    machine. Anything wider mints one when the config has none, so opening
    the interface to the network never silently opens it to the network's
    other occupants.
    """
    configured = (configured or "").strip()
    if configured:
        return configured
    if is_loopback_host(bind_host):
        return ""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _request_host_name() -> str:
    """The host part of the request's Host header, without its port."""
    raw = (request.headers.get("Host") or "").strip()
    if raw.startswith("["):  # IPv6 literal, e.g. [::1]:5055
        return raw.split("]")[0].lstrip("[")
    return raw.rsplit(":", 1)[0] if ":" in raw else raw


def _supplied_token() -> str:
    return (
        request.headers.get(TOKEN_HEADER)
        or request.args.get(TOKEN_QUERY)
        or ""
    ).strip()


def create_app(cfg: WebUIConfig) -> Flask:
    """Build the control centre application with its guards installed."""
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    app.config["JARVIS_WEBUI"] = cfg

    @app.before_request
    def _guard():  # noqa: ANN202 - Flask hook
        if cfg.is_loopback and not is_loopback_host(_request_host_name()):
            debug_log(f"webui refused a foreign host header: {_request_host_name()}", "webui")
            return jsonify(error="host not allowed"), 403

        if cfg.token:
            supplied = _supplied_token()
            if not supplied or not hmac.compare_digest(supplied, cfg.token):
                debug_log("webui refused a request without a valid token", "webui")
                return jsonify(error="token required"), 403

        if request.method not in SAFE_METHODS and request.headers.get(UI_HEADER) != "1":
            debug_log(f"webui refused a headerless write to {request.path}", "webui")
            return jsonify(error=f"{UI_HEADER} header required"), 403

        return None

    @app.after_request
    def _no_sharing(response):  # noqa: ANN202 - Flask hook
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.route("/")
    def _index():  # noqa: ANN202 - Flask hook
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/api/health")
    def _health():  # noqa: ANN202 - Flask hook
        return jsonify(ok=True, port=cfg.port, host=cfg.host)

    from .api import register_blueprints

    register_blueprints(app)
    return app


class WebUIServer:
    """A control centre bound to a port, running on its own thread."""

    def __init__(self, cfg: WebUIConfig) -> None:
        self.cfg = cfg
        self._server = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return self.cfg.url

    def start(self) -> None:
        from werkzeug.serving import make_server

        app = create_app(self.cfg)
        # threaded=True: server-sent event streams hold a connection open for
        # as long as the page is, so a single-threaded server would answer
        # nothing else while the control centre is watching.
        self._server = make_server(
            self.cfg.host, self.cfg.port, app, threaded=True,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="jarvis-webui",
            daemon=True,
        )
        self._thread.start()
        # The confirmation channel is only honest about being available
        # while there is a page that could show a request.
        get_web_confirmations().set_serving(True)
        debug_log(f"webui listening on {self.cfg.host}:{self.cfg.port}", "webui")

    def stop(self, timeout: float = 3.0) -> None:
        get_web_confirmations().set_serving(False)
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception as exc:
                debug_log(f"webui shutdown failed: {exc}", "webui")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None
        debug_log("webui stopped", "webui")
