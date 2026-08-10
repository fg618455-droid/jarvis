"""Run the control centre on its own, without the daemon.

Useful for looking at memory, settings, and stored telemetry while the
assistant is not running. Live state is empty in this mode: nothing is
listening, so there is nothing to report.
"""

from __future__ import annotations

import sys

from jarvis.config import load_settings

from .server import WebUIConfig, WebUIServer, resolve_token


def main() -> int:
    cfg = load_settings()
    webui_cfg = WebUIConfig(
        host=cfg.webui_bind_host,
        port=cfg.webui_port,
        token=resolve_token(cfg.webui_bind_host, cfg.webui_token),
    )
    server = WebUIServer(webui_cfg)
    server.start()

    print("🖥️ Jarvis control centre (standalone, no daemon)", flush=True)
    print(f"     🌐 {server.url}", flush=True)
    print("     ⏹️ Press Ctrl+C to stop", flush=True)

    try:
        while True:
            import time
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n🔄 Stopping control centre...", flush=True)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
