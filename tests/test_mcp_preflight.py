from jarvis.tools.external.mcp_preflight import preflight_mcp_config


def test_mcp_preflight_rejects_floating_npx_packages():
    result = preflight_mcp_config({"command": "npx", "args": ["-y", "server@latest"]})
    assert not result.available
    assert result.code == "invalid_config"


def test_mcp_preflight_accepts_pinned_npx_package():
    result = preflight_mcp_config({"command": "npx", "args": ["-y", "server@1.2.3"]})
    assert result.available
