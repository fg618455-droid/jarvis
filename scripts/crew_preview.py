"""🎛️ Mission Control against a stand-in NAS, for looking at it in a browser.

Serves a crew endpoint shaped exactly like the one on the NAS
(`docs-felix/nas-scripts/mission-control/crew-api.py`) with invented
activity, points a throwaway config at it, and starts the control centre.
A new entry appears every few seconds so the arrival glow and the freshness
reading can be watched rather than argued about.

    python scripts/crew_preview.py [--port 5099] [--quiet]

Nothing here ships with the app and nothing writes to the real config.
"""

from __future__ import annotations

import sys

# The emoji this output uses outlive the console's default codepage on
# Windows. Reconfigured in place, as the control centre's own entry point
# does, and before anything is printed.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import argparse
import json
import os
import random
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


AGENTS = ["JARVIS", "DEV", "RESEARCH", "ASSISTANT", "SCHULE", "SCRIBE"]
MODELS = ["gpt-5.6-sol", "gemini-2.5-flash", "stepfun/step-3.7-flash:free"]
TASKS = [
    "Fixed the SchulOS heartbeat cron job and pushed the change to develop",
    "Compared three local speech-to-text approaches and wrote the results to the vault",
    "Summarised this week's timetable changes into the school dashboard",
    "Drafted the release notes for the FamList shopping list app",
    "Checked whether the fallback chain hands over to Gemini when the primary is throttled",
    "Rebuilt the nightly backup manifest after the NAS restarted",
    "Reviewed the pull request against the offline-first rule and left two comments",
]
STATUSES = ["success"] * 6 + ["partial"] * 2 + ["failure"]

_entries: list[dict] = []
_lock = threading.Lock()


def _seed(rng: random.Random) -> None:
    """Fourteen days of history, thinning out towards the older end."""
    now = datetime.now(timezone.utc)
    identifier = 1
    for days_ago in range(13, -1, -1):
        for _ in range(rng.randint(0, 6 if days_ago < 7 else 3)):
            when = now - timedelta(
                days=days_ago, hours=rng.randint(0, 22), minutes=rng.randint(0, 59),
            )
            _entries.append({
                "id": identifier,
                "agent_name": rng.choice(AGENTS),
                "task_description": rng.choice(TASKS),
                "model_used": rng.choice(MODELS),
                "status": rng.choice(STATUSES),
                "created_at": when.isoformat(timespec="seconds"),
            })
            identifier += 1
    _entries.sort(key=lambda entry: entry["created_at"])
    for position, entry in enumerate(_entries, start=1):
        entry["id"] = position


def _keep_working(rng: random.Random, every_sec: float) -> None:
    """Log a new task now and then, so the page has something to receive."""
    while True:
        time.sleep(every_sec)
        with _lock:
            _entries.append({
                "id": (_entries[-1]["id"] + 1) if _entries else 1,
                "agent_name": rng.choice(AGENTS),
                "task_description": rng.choice(TASKS),
                "model_used": rng.choice(MODELS),
                "status": rng.choice(STATUSES),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path.startswith("/health"):
            return self._send({"ok": True})
        if not self.path.startswith("/agent_logs"):
            return self._send({"error": "not found"}, code=404)
        with _lock:
            newest_first = list(reversed(_entries))
        self._send({"entries": newest_first})

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002 - stdlib signature
        return


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5099, help="control centre port")
    parser.add_argument("--interval", type=float, default=6.0, help="seconds between new tasks")
    parser.add_argument("--seed", type=int, default=20260822, help="keeps the invented data stable")
    parser.add_argument("--quiet", action="store_true", help="no new tasks while it runs")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    _seed(rng)

    crew_port = _free_port()
    nas = HTTPServer(("127.0.0.1", crew_port), _Handler)
    threading.Thread(target=nas.serve_forever, daemon=True).start()

    if not args.quiet:
        threading.Thread(
            target=_keep_working, args=(rng, args.interval), daemon=True,
        ).start()

    config_dir = Path(tempfile.mkdtemp(prefix="jarvis-crew-preview-"))
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({
        "crew_api_url": f"http://127.0.0.1:{crew_port}",
        "crew_api_key": "preview",
        "webui_port": args.port,
        "webui_bind_host": "127.0.0.1",
    }), encoding="utf-8")
    os.environ["JARVIS_CONFIG_PATH"] = str(config_path)

    print("🎛️ Mission Control preview")
    print(f"     🛰️ stand-in NAS on port {crew_port}, {len(_entries)} entries seeded")
    print(f"     📄 throwaway config at {config_path}")
    if not args.quiet:
        print(f"     ⏱️ a new task every {args.interval:g}s")

    from jarvis.webui.__main__ import main as serve

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
