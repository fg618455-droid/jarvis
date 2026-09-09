"""
Tests for the control centre's microphone socket.

A WebSocket upgrade is a GET, so the write-header guard does not cover it,
and WebSockets are exempt from CORS: any page in the browser can open one
against a loopback port. This socket dispatches speech to an assistant with
tool access, so it carries its own origin check rather than inheriting the
ordinary request guards.
"""

import pytest

from jarvis.webui.api.voice import origin_allowed


class TestOriginPolicy:
    """Who is allowed to open the microphone socket."""

    def test_a_page_from_the_control_centre_is_allowed(self):
        assert origin_allowed("http://127.0.0.1:5055", "127.0.0.1", 5055) is True
        assert origin_allowed("http://localhost:5055", "127.0.0.1", 5055) is True

    def test_a_page_from_another_site_is_refused(self):
        """The attack this check exists for: a hostile tab dialling loopback."""
        assert origin_allowed("https://evil.example", "127.0.0.1", 5055) is False
        assert origin_allowed("http://evil.example:5055", "127.0.0.1", 5055) is False

    def test_a_different_loopback_port_is_refused(self):
        """Another local service is not this one."""
        assert origin_allowed("http://127.0.0.1:9999", "127.0.0.1", 5055) is False

    def test_a_missing_origin_is_allowed(self):
        """Non-browser clients send no Origin and cannot be tricked into one."""
        assert origin_allowed(None, "127.0.0.1", 5055) is True
        assert origin_allowed("", "127.0.0.1", 5055) is True

    def test_a_null_origin_is_refused(self):
        """'null' is what a sandboxed frame sends. Treat it as untrusted."""
        assert origin_allowed("null", "127.0.0.1", 5055) is False

    @pytest.mark.parametrize("origin", [
        "http://127.0.0.1:5055.evil.example",
        "http://127.0.0.1:5055@evil.example",
        "http://[::1]:5055",
    ])
    def test_lookalike_origins_are_refused_unless_genuinely_loopback(self, origin):
        allowed = origin_allowed(origin, "127.0.0.1", 5055)
        # ::1 is genuine loopback on the right port; the other two only look it.
        assert allowed is (origin == "http://[::1]:5055")

    def test_off_loopback_binds_compare_against_the_bound_host(self):
        """Reached over the LAN, the page's origin is that same address."""
        assert origin_allowed("http://192.168.1.10:5055", "192.168.1.10", 5055) is True
        assert origin_allowed("http://192.168.1.11:5055", "192.168.1.10", 5055) is False
