from jarvis.tools.external.mcp_preflight import preflight_mcp_config


def test_mcp_preflight_rejects_floating_npx_packages():
    result = preflight_mcp_config({"command": "npx", "args": ["-y", "server@latest"]})
    assert not result.available
    assert result.code == "invalid_config"


def test_mcp_preflight_accepts_pinned_npx_package():
    result = preflight_mcp_config({"command": "npx", "args": ["-y", "server@1.2.3"]})
    assert result.available


def test_mcp_preflight_accepts_a_pinned_package_that_takes_its_own_arguments():
    result = preflight_mcp_config({
        "command": "npx",
        "args": ["-y", "mcp-remote@0.2.1", "https://example.invalid/mcp"],
    })
    assert result.available


def test_mcp_preflight_rejects_an_unpinned_package_that_takes_its_own_arguments():
    result = preflight_mcp_config({
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://example.invalid/mcp"],
    })
    assert not result.available
    assert result.code == "invalid_config"


def test_mcp_preflight_rejects_an_unpinned_scoped_package():
    result = preflight_mcp_config({"command": "npx", "args": ["-y", "@scope/server"]})
    assert not result.available
    assert result.code == "invalid_config"


def test_mcp_preflight_accepts_a_pinned_scoped_package():
    result = preflight_mcp_config({"command": "npx", "args": ["-y", "@scope/server@1.2.3"]})
    assert result.available
