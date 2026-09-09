"""A failed cold handshake must not leak an uncached worker."""

import threading

import pytest

from jarvis.tools.external import mcp_runtime


def test_failed_start_shuts_down_worker_before_propagating(monkeypatch):
    stopped = []

    class Worker:
        def __init__(self, *args):
            pass

        def start(self):
            raise TimeoutError("handshake expired")

        def shutdown(self):
            stopped.append(True)

    monkeypatch.setattr(mcp_runtime, "_ServerWorker", Worker)
    runtime = object.__new__(mcp_runtime._PersistentMCPRuntime)
    runtime._workers_lock = threading.Lock()
    runtime._workers = {}
    runtime._server_locks = {}
    runtime._config_generation = 0
    runtime.closed = False
    runtime._loop = object()

    with pytest.raises(TimeoutError, match="handshake expired"):
        runtime._get_worker("test", {})

    assert stopped == [True]
    assert runtime._workers == {}


def test_late_failure_does_not_evict_replacement_worker():
    from unittest.mock import Mock

    runtime = mcp_runtime._PersistentMCPRuntime()
    old, replacement = Mock(), Mock()
    try:
        runtime._workers["server"] = replacement
        runtime._drop_worker("server", old)
        assert runtime._workers["server"] is replacement
        replacement.shutdown.assert_not_called()
    finally:
        runtime.shutdown()


def test_removed_server_cannot_be_restarted_by_stale_caller(monkeypatch):
    from unittest.mock import Mock

    constructor = Mock()
    monkeypatch.setattr(mcp_runtime, "_ServerWorker", constructor)
    runtime = mcp_runtime._PersistentMCPRuntime()
    try:
        runtime.reconfigure_servers({})
        with pytest.raises(RuntimeError, match="no longer active"):
            runtime._get_worker("removed", {"command": "old"})
        constructor.assert_not_called()
    finally:
        runtime.shutdown()


def test_slow_server_start_does_not_block_other_server(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    class Worker:
        def __init__(self, loop, name, config):
            self.name, self.config, self.alive = name, config, True
        def start(self):
            if self.name == "slow":
                entered.set()
                assert release.wait(5)
        def shutdown(self):
            self.alive = False
    monkeypatch.setattr(mcp_runtime, "_ServerWorker", Worker)
    runtime = mcp_runtime._PersistentMCPRuntime()
    try:
        with ThreadPoolExecutor(2) as pool:
            slow = pool.submit(runtime._get_worker, "slow", {})
            assert entered.wait(2)
            fast = pool.submit(runtime._get_worker, "fast", {})
            try:
                assert fast.result(timeout=2).name == "fast"
            finally:
                release.set()
            assert slow.result(timeout=2).name == "slow"
    finally:
        release.set()
        runtime.shutdown()
