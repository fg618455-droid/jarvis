"""
Tests for desktop_app.headless_launcher's testable (non-Qt) logic.

The startup orchestration itself (splash, Ollama checks, subprocess) is
integration glue verified by running it; only the pure helper is unit tested
here.
"""

from types import SimpleNamespace

from desktop_app.headless_launcher import _control_centre_url


class TestControlCentreUrl:
    def test_uses_configured_host_and_port(self):
        cfg = SimpleNamespace(webui_bind_host="127.0.0.1", webui_port=5055)
        assert _control_centre_url(cfg) == "http://127.0.0.1:5055"

    def test_rewrites_wildcard_bind_host_to_loopback(self):
        cfg = SimpleNamespace(webui_bind_host="0.0.0.0", webui_port=5055)
        assert _control_centre_url(cfg) == "http://127.0.0.1:5055"

    def test_falls_back_to_defaults_when_unset(self):
        cfg = SimpleNamespace(webui_bind_host="", webui_port=0)
        assert _control_centre_url(cfg) == "http://127.0.0.1:5055"

    def test_uses_a_custom_port(self):
        cfg = SimpleNamespace(webui_bind_host="127.0.0.1", webui_port=6100)
        assert _control_centre_url(cfg) == "http://127.0.0.1:6100"
