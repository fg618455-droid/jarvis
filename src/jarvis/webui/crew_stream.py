"""📡 One crew reading, taken for everyone who is watching.

Mission Control shows a machine that is not this one. Every open page used
to ask the NAS for itself, so two tabs meant twice the traffic to a device
that is often asleep, and no page could tell how old the answer it was
looking at had become.

The daemon takes the reading instead and publishes it on the same event bus
the phase indicator already rides. One reading serves every page, arrives
without being asked for, and carries the moment it was taken.

That puts a thread inside the daemon that talks to the network, which is
only acceptable while it stays quiet. It contacts nothing unless a crew
endpoint is configured *and* at least one page is listening: a machine with
the control centre closed makes no outbound request at all.
"""

from __future__ import annotations

import threading

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.runtime import EventBus, get_event_bus

from .api.crew import crew_snapshot


# Slow enough that a sleeping NAS is not woken on our account, quick enough
# that a finished task shows up while it is still interesting.
POLL_INTERVAL_SEC = 10.0


class CrewPoller:
    """Reads the crew endpoint on a timer and publishes what it finds."""

    def __init__(
        self, interval_sec: float = POLL_INTERVAL_SEC, bus: EventBus | None = None,
    ) -> None:
        self.interval_sec = interval_sec
        self.bus = bus if bus is not None else get_event_bus()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tick(self) -> None:
        """Take one reading, if anyone is waiting for one.

        Never raises. A poller that dies on a bad reply would take Mission
        Control offline until the daemon restarts, and the view already has
        an honest way to say the NAS is not answering.
        """
        try:
            if self.bus.subscriber_count == 0:
                return
            cfg = load_settings()
            if not cfg.crew_api_url:
                return
            self.bus.publish("crew", crew_snapshot(cfg))
        except Exception as error:  # noqa: BLE001 - a reading is never worth a crash
            debug_log(f"the crew reading failed: {error}", "webui")

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="jarvis-crew-poller", daemon=True,
        )
        self._thread.start()
        debug_log(f"crew poller reading every {self.interval_sec:g}s", "webui")

    def _run(self) -> None:
        # Waiting first rather than last lets stop() interrupt the interval
        # instead of being made to sit through it.
        while not self._stop.wait(self.interval_sec):
            self.tick()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        debug_log("crew poller stopped", "webui")
