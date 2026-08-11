"""Behaviour tests for the control centre's request guards.

The control centre can approve tool execution and rewrite the config, so a
loopback port is not automatically safe: any page open in the same browser
can post to it. Three guards stand in the way, and each is tested from the
outside, through real requests.
"""

import pytest

from jarvis.webui.server import WebUIConfig, create_app, resolve_token


def _client(host="127.0.0.1", port=5055, token=""):
    app = create_app(WebUIConfig(host=host, port=port, token=token))
    app.config.update(TESTING=True)
    return app.test_client()


class TestHostAllowlist:
    """Blocks DNS rebinding: a hostile name resolving to 127.0.0.1."""

    @pytest.mark.parametrize("host", [
        "127.0.0.1:5055",
        "localhost:5055",
        "127.0.0.1",
        "[::1]:5055",
    ])
    def test_a_local_name_is_served(self, host):
        response = _client().get("/api/health", headers={"Host": host})

        assert response.status_code == 200

    @pytest.mark.parametrize("host", [
        "evil.example",
        "jarvis.attacker.test:5055",
        "192.168.178.20:5055",
    ])
    def test_any_other_name_is_refused(self, host):
        response = _client().get("/api/health", headers={"Host": host})

        assert response.status_code == 403

    def test_binding_to_the_network_accepts_the_machine_s_own_name(self):
        """Reaching it from a phone means the Host header is the LAN address.

        The allowlist cannot enumerate those, so the token takes over as the
        guard as soon as the server leaves loopback.
        """
        client = _client(host="0.0.0.0", token="tok")

        response = client.get("/api/health?t=tok", headers={"Host": "192.168.178.20:5055"})

        assert response.status_code == 200


class TestMutationHeader:
    """Blocks classic CSRF: a plain HTML form cannot set a custom header."""

    def test_a_write_without_the_header_is_refused(self):
        response = _client().post("/api/health", headers={"Host": "127.0.0.1:5055"})

        assert response.status_code == 403

    def test_a_write_with_the_header_gets_past_the_guard(self):
        response = _client().post("/api/health", headers={
            "Host": "127.0.0.1:5055",
            "X-Jarvis-UI": "1",
        })

        # 405: the guard let it through and routing rejected the method.
        assert response.status_code == 405

    def test_reading_never_needs_the_header(self):
        response = _client().get("/api/health", headers={"Host": "127.0.0.1:5055"})

        assert response.status_code == 200


class TestToken:
    def test_loopback_needs_no_token(self):
        response = _client().get("/api/health", headers={"Host": "127.0.0.1:5055"})

        assert response.status_code == 200

    def test_leaving_loopback_demands_the_token(self):
        client = _client(host="0.0.0.0", token="tok")

        response = client.get("/api/health", headers={"Host": "127.0.0.1:5055"})

        assert response.status_code == 403

    @pytest.mark.parametrize("supply", ["header", "query"])
    def test_the_right_token_is_accepted_either_way(self, supply):
        client = _client(host="0.0.0.0", token="tok")
        headers = {"Host": "127.0.0.1:5055"}
        path = "/api/health"
        if supply == "header":
            headers["X-Jarvis-Token"] = "tok"
        else:
            path += "?t=tok"

        response = client.get(path, headers=headers)

        assert response.status_code == 200

    def test_a_wrong_token_is_refused(self):
        client = _client(host="0.0.0.0", token="tok")

        response = client.get("/api/health?t=nope", headers={"Host": "127.0.0.1:5055"})

        assert response.status_code == 403


class TestTokenResolution:
    def test_loopback_keeps_running_without_a_token(self):
        assert resolve_token("127.0.0.1", "") == ""

    def test_leaving_loopback_mints_one_when_the_config_has_none(self):
        token = resolve_token("0.0.0.0", "")

        assert len(token) >= 16

    def test_two_starts_do_not_share_a_minted_token(self):
        assert resolve_token("0.0.0.0", "") != resolve_token("0.0.0.0", "")

    def test_a_configured_token_is_kept(self):
        assert resolve_token("0.0.0.0", "mine") == "mine"


class TestNoCrossOrigin:
    def test_no_response_invites_another_origin(self):
        response = _client().get("/api/health", headers={"Host": "127.0.0.1:5055"})

        assert "Access-Control-Allow-Origin" not in response.headers


class TestHealth:
    def test_health_reports_the_running_server(self):
        response = _client().get("/api/health", headers={"Host": "127.0.0.1:5055"})

        assert response.get_json()["ok"] is True
