"""Connecting MCP servers from the control centre.

`mcps` is a dictionary of opaque launch descriptions rather than a field in
the settings registry, so it cannot ride `PUT /api/settings`: that endpoint
rejects any key the registry does not describe, and the registry describes
scalars and lists of uniform objects, not a map of arbitrary command lines.
It gets an endpoint of its own, and that endpoint carries over the two rules
the settings endpoint enforces, because both write the same file: only
non-default values are stored, and keys nothing here describes survive
untouched.

The third rule matters more here than anywhere else in the interface. An MCP
server's `env` is where its credentials live: a personal access token, an API
key, a session cookie. Those are writable and never readable, the same way a
bot token is, and a masked value sent back unchanged has to leave the stored
secret alone. A settings page that quietly replaced a token with eight
bullets and a save button would be a very effective way of destroying it.
"""

import json

import pytest

from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}
WRITE_HEADERS = {**HEADERS, "X-Jarvis-UI": "1"}

STORED = {
    "whisper_language": "de",
    "mcps": {
        "schulos": {
            "command": "npx",
            "args": ["-y", "schulos-mcp"],
            "env": {"SCHULOS_TOKEN": "abcd1234SECRET"},
            "timeout_sec": 30,
        },
        "chrome": {
            "command": "npx",
            "args": ["chrome-devtools-mcp"],
        },
    },
    "_config_version": 3,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(STORED), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    test_client = app.test_client()
    test_client.config_path = config_path
    return test_client


def _stored(client) -> dict:
    return json.loads(client.config_path.read_text(encoding="utf-8"))


def _server(body, name):
    return next(server for server in body["servers"] if server["name"] == name)


def _put(client, servers):
    return client.put(
        "/api/mcp/servers", json={"servers": servers}, headers=WRITE_HEADERS,
    )


class TestReading:
    def test_every_configured_server_is_listed(self, client):
        body = client.get("/api/mcp/servers", headers=HEADERS).get_json()

        assert {server["name"] for server in body["servers"]} == {"schulos", "chrome"}

    def test_a_server_carries_how_it_is_launched(self, client):
        server = _server(client.get("/api/mcp/servers", headers=HEADERS).get_json(), "schulos")

        assert server["command"] == "npx"
        assert server["args"] == ["-y", "schulos-mcp"]
        assert server["timeout_sec"] == 30

    def test_a_server_says_whether_it_connected(self, client):
        server = _server(client.get("/api/mcp/servers", headers=HEADERS).get_json(), "chrome")

        assert server["connected"] is False
        assert server["tool_count"] == 0

    def test_a_credential_is_never_returned_in_clear(self, client):
        server = _server(client.get("/api/mcp/servers", headers=HEADERS).get_json(), "schulos")

        value = server["env"]["SCHULOS_TOKEN"]
        assert "SECRET" not in value
        assert value.endswith("CRET"), "the last four are shown so a key is recognisable"
        assert value.startswith("•")

    def test_the_editor_is_told_what_shape_a_server_has(self, client):
        body = client.get("/api/mcp/servers", headers=HEADERS).get_json()

        keys = {field["key"] for field in body["server_fields"]}
        assert {"name", "command", "args", "timeout_sec", "idle_timeout_sec"} <= keys


class TestWriting:
    def test_editing_a_server_rewrites_only_that_server(self, client):
        body = client.get("/api/mcp/servers", headers=HEADERS).get_json()
        servers = body["servers"]
        _server({"servers": servers}, "chrome")["args"] = ["chrome-devtools-mcp", "--headless"]

        assert _put(client, servers).status_code == 200
        stored = _stored(client)["mcps"]
        assert stored["chrome"]["args"] == ["chrome-devtools-mcp", "--headless"]
        assert stored["schulos"]["args"] == ["-y", "schulos-mcp"]

    def test_a_masked_credential_returned_unchanged_leaves_the_secret_alone(self, client):
        servers = client.get("/api/mcp/servers", headers=HEADERS).get_json()["servers"]

        assert _put(client, servers).status_code == 200

        assert _stored(client)["mcps"]["schulos"]["env"]["SCHULOS_TOKEN"] == "abcd1234SECRET"

    def test_a_replaced_credential_is_written(self, client):
        servers = client.get("/api/mcp/servers", headers=HEADERS).get_json()["servers"]
        _server({"servers": servers}, "schulos")["env"]["SCHULOS_TOKEN"] = "a-new-token"

        assert _put(client, servers).status_code == 200

        assert _stored(client)["mcps"]["schulos"]["env"]["SCHULOS_TOKEN"] == "a-new-token"

    def test_adding_a_server_connects_it(self, client):
        servers = client.get("/api/mcp/servers", headers=HEADERS).get_json()["servers"]
        servers.append({
            "name": "notes", "command": "uvx", "args": ["notes-mcp"],
            "env": {}, "timeout_sec": None, "idle_timeout_sec": 60,
        })

        assert _put(client, servers).status_code == 200

        stored = _stored(client)["mcps"]["notes"]
        assert stored["command"] == "uvx"
        assert stored["idle_timeout_sec"] == 60

    def test_removing_a_server_removes_it(self, client):
        servers = [
            server
            for server in client.get("/api/mcp/servers", headers=HEADERS).get_json()["servers"]
            if server["name"] != "chrome"
        ]

        assert _put(client, servers).status_code == 200

        assert set(_stored(client)["mcps"]) == {"schulos"}

    def test_removing_every_server_leaves_no_empty_key_behind(self, client):
        """An empty map is the default, and a default is not written."""
        assert _put(client, []).status_code == 200

        assert "mcps" not in _stored(client)

    def test_settings_this_endpoint_does_not_own_survive(self, client):
        _put(client, [])

        assert _stored(client)["whisper_language"] == "de"
        assert _stored(client)["_config_version"] == 3

    def test_a_server_with_no_command_is_refused(self, client):
        response = _put(client, [{"name": "broken", "command": "  ", "args": []}])

        assert response.status_code == 400
        assert "command" in response.get_json()["error"]

    def test_a_server_with_no_name_is_refused(self, client):
        response = _put(client, [{"name": "", "command": "npx", "args": []}])

        assert response.status_code == 400

    def test_two_servers_with_one_name_are_refused(self, client):
        response = _put(client, [
            {"name": "same", "command": "npx", "args": []},
            {"name": "same", "command": "uvx", "args": []},
        ])

        assert response.status_code == 400
        assert response.get_json()["error"]
        # The refusal is total: nothing is half-written.
        assert set(_stored(client)["mcps"]) == {"schulos", "chrome"}

    def test_renaming_a_server_keeps_its_credential(self, client):
        """A rename is an edit, not a new server with a lost token."""
        servers = client.get("/api/mcp/servers", headers=HEADERS).get_json()["servers"]
        _server({"servers": servers}, "schulos")["name"] = "school"

        assert _put(client, servers).status_code == 200

        stored = _stored(client)["mcps"]
        assert "schulos" not in stored
        assert stored["school"]["env"]["SCHULOS_TOKEN"] == "abcd1234SECRET"

    def test_a_write_without_the_interface_header_is_refused(self, client):
        response = client.put("/api/mcp/servers", json={"servers": []}, headers=HEADERS)

        assert response.status_code == 403
