from jarvis.tools.external.mcp_preflight import preflight_mcp_config


def test_endpoint_invalid_port_returns_safe_error():
    from jarvis.tools.external.mcp_preflight import _endpoint_available

    assert _endpoint_available("https://example.com:bad/mcp").code == "invalid_config"


def test_endpoint_timeout_is_classified(monkeypatch):
    from jarvis.tools.external.mcp_preflight import _endpoint_available

    def timeout(*args, **kwargs):
        raise TimeoutError("sensitive underlying details")

    monkeypatch.setattr("jarvis.tools.external.mcp_preflight.socket.getaddrinfo", timeout)
    result = _endpoint_available("https://example.com/mcp")
    assert result.code == "endpoint_unreachable"
    assert "timed out" in result.reason
    assert "sensitive" not in result.reason


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


def test_live_preflight_rejects_a_literal_private_remote_address(monkeypatch):
    monkeypatch.setattr("jarvis.tools.external.mcp_preflight.shutil.which", lambda _: "npx")
    result = preflight_mcp_config({
        "command": "npx",
        "args": ["-y", "mcp-remote@0.8.3", "https://127.0.0.1/mcp"],
    }, live=True)

    assert not result.available
    assert result.code == "invalid_config"


def test_live_preflight_rejects_a_hostname_that_resolves_private(monkeypatch):
    monkeypatch.setattr("jarvis.tools.external.mcp_preflight.shutil.which", lambda _: "npx")
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_preflight.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("10.0.0.5", 443))],
    )
    result = preflight_mcp_config({
        "command": "npx",
        "args": ["-y", "mcp-remote@0.8.3", "https://mcp.example/mcp"],
    }, live=True)

    assert not result.available
    assert result.code == "invalid_config"


def test_live_preflight_does_not_follow_remote_redirects(monkeypatch):
    monkeypatch.setattr("jarvis.tools.external.mcp_preflight.shutil.which", lambda _: "npx")
    import urllib.error

    monkeypatch.setattr(
        "jarvis.tools.external.mcp_preflight.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 302, "redirect", {"Location": "https://127.0.0.1"}, None,
            )

    monkeypatch.setattr(
        "jarvis.tools.external.mcp_preflight.urllib.request.build_opener",
        lambda *_args: Opener(),
    )
    result = preflight_mcp_config({
        "command": "npx",
        "args": ["-y", "mcp-remote@0.8.3", "https://mcp.example/mcp"],
    }, live=True)

    assert not result.available
    assert result.code == "endpoint_unreachable"
