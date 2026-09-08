"""Real subprocess tests: no mocked MCP session or worker."""
from pathlib import Path
import sys
import time
import pytest
from jarvis.tools.external.mcp_client import MCPClient, MCPServerSessionError
from jarvis.tools.external.mcp_runtime import shutdown_runtime, get_runtime

@pytest.fixture
def peer(tmp_path):
    marker = tmp_path / 'starts.txt'
    cfg = {'transport': 'stdio', 'command': sys.executable,
           'args': ['-B', str(Path(__file__).parent / 'fixtures' / 'mcp_disposable_server.py'), str(marker)],
           'timeout_sec': 5.0}
    shutdown_runtime()
    try:
        yield cfg, marker
    finally:
        shutdown_runtime()

def test_real_discovery_tool_error_and_single_crash_reconnect(peer):
    cfg, marker = peer
    client = MCPClient({'disposable': cfg})
    assert len(client.list_tools('disposable')) == 4
    assert client.invoke_tool('disposable', 'tool_error', {})['isError']
    assert not client.invoke_tool('disposable', 'echo', {})['isError']
    assert len(marker.read_text().splitlines()) == 1
    assert not client.invoke_tool('disposable', 'crash_once', {})['isError']
    assert len(marker.read_text().splitlines()) == 2

def test_real_repeated_crash_stops_after_one_retry(peer):
    cfg, marker = peer
    client = MCPClient({'disposable': cfg})
    with pytest.raises(MCPServerSessionError):
        client.invoke_tool('disposable', 'crash_always', {})
    assert len(marker.read_text().splitlines()) == 2

def test_real_idle_and_removal_close_worker(peer):
    cfg, marker = peer
    cfg['idle_timeout_sec'] = 0.1
    client = MCPClient({'disposable': cfg})
    client.list_tools('disposable')
    worker = get_runtime()._workers['disposable']
    deadline = time.monotonic() + 5.0
    while worker.alive and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not worker.alive
    client.list_tools('disposable')
    assert len(marker.read_text().splitlines()) == 2
    worker = get_runtime()._workers['disposable']
    get_runtime().reconfigure_servers({})
    assert not worker.alive
