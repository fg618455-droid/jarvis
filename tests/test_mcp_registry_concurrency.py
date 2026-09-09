"""Configuration publication and refresh must preserve writer order."""
import threading
from concurrent.futures import ThreadPoolExecutor
from jarvis.tools import registry
from jarvis.tools.external.mcp_runtime import shutdown_runtime


def test_slow_discovery_cannot_restore_removed_server(monkeypatch):
    entered, release, removing = threading.Event(), threading.Event(), threading.Event()
    def discover(config):
        if config:
            entered.set()
            assert release.wait(5)
        return ({name: registry.ToolSpec(name, name) for name in config}, {}, {})
    monkeypatch.setattr(registry, 'discover_mcp_tools_detailed', discover)
    def remove():
        removing.set()
        return registry.reconfigure_mcp_tools({}, verbose=False)
    try:
        with ThreadPoolExecutor(2) as pool:
            old = pool.submit(registry.reconfigure_mcp_tools, {'old': {'command': 'synthetic'}})
            assert entered.wait(2)
            newer = pool.submit(remove)
            try:
                assert removing.wait(2)
                # Cache readers remain available while discovery is waiting.
                registry.get_cached_mcp_tools()
            finally:
                release.set()
            old.result(timeout=5)
            assert newer.result(timeout=5) == ({}, {})
        assert registry.refresh_mcp_tools(verbose=False) == ({}, {})
    finally:
        release.set()
        registry.reconfigure_mcp_tools({}, verbose=False)
        shutdown_runtime()
