"""Behaviour tests for editing config.json from the control centre.

Two invariants carry over from the Qt settings window, because both write
the same file: only non-default values are stored, and keys the registry
does not describe survive untouched. A third is new here, because a browser
tab can be left open where a desktop dialog would not: a credential is
writable but never readable.
"""

import json

import pytest

from jarvis.config import get_default_config
from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}
WRITE_HEADERS = {**HEADERS, "X-Jarvis-UI": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "whisper_language": "de",
        "telegram_bot_token": "12345:SECRETVALUE",
        "mcps": {"rube": {"command": "npx", "args": ["-y", "mcp-remote"]}},
        "_config_version": 3,
    }), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    test_client = app.test_client()
    test_client.config_path = config_path
    return test_client


def _stored(client) -> dict:
    return json.loads(client.config_path.read_text(encoding="utf-8"))


def _field(body, key):
    return next(f for f in body["fields"] if f["key"] == key)


class TestReading:
    def test_every_registry_field_is_offered(self, client):
        from jarvis.config_metadata import FIELD_METADATA

        body = client.get("/api/settings", headers=HEADERS).get_json()

        assert {f["key"] for f in body["fields"]} == {m.key for m in FIELD_METADATA}

    def test_a_configured_value_is_shown(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()

        assert _field(body, "whisper_language")["value"] == "de"

    def test_a_model_the_registry_never_heard_of_is_still_offered(self, client):
        """A choice field must be able to show what is actually configured.

        The supported-model list is a curated shortlist, not the set of
        models a local runtime can serve: a custom Modelfile or a tag built
        on this machine will never appear in it. A select that cannot show
        the running model reports the wrong one, which is the opposite of
        what this page is for.
        """
        stored = _stored(client)
        stored["ollama_chat_model"] = "qwen2.5:7b-ctx8k"
        client.config_path.write_text(json.dumps(stored), encoding="utf-8")

        field = _field(client.get("/api/settings", headers=HEADERS).get_json(),
                       "ollama_chat_model")

        assert field["value"] == "qwen2.5:7b-ctx8k"
        assert "qwen2.5:7b-ctx8k" in [c["value"] for c in field["choices"]]

    def test_a_known_model_is_offered_once(self, client):
        """Showing the configured value must not duplicate a listed one."""
        from jarvis.config_metadata import SUPPORTED_CHAT_MODELS

        known = next(iter(SUPPORTED_CHAT_MODELS))
        stored = _stored(client)
        stored["ollama_chat_model"] = known
        client.config_path.write_text(json.dumps(stored), encoding="utf-8")

        field = _field(client.get("/api/settings", headers=HEADERS).get_json(),
                       "ollama_chat_model")

        assert [c["value"] for c in field["choices"]].count(known) == 1

    def test_an_unset_field_falls_back_to_its_default(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()

        assert _field(body, "webui_port")["value"] == get_default_config()["webui_port"]
        assert _field(body, "webui_port")["is_default"] is True

    def test_a_field_says_how_to_render_it(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()

        level = _field(body, "security_level")
        assert level["type"] == "choice"
        assert {choice["value"] for choice in level["choices"]} == {"critical", "paranoid", "off"}

    def test_fields_that_only_take_effect_after_a_restart_say_so(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()

        assert _field(body, "whisper_language")["restart_required"] is True


class TestSecrets:
    def test_a_stored_credential_is_never_returned(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()

        token = _field(body, "telegram_bot_token")
        assert "SECRETVALUE" not in json.dumps(body)
        assert token["value"].endswith("ALUE")
        assert token["is_set"] is True

    def test_sending_the_mask_back_leaves_the_credential_alone(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()
        masked = _field(body, "telegram_bot_token")["value"]

        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"telegram_bot_token": masked}})

        assert _stored(client)["telegram_bot_token"] == "12345:SECRETVALUE"

    def test_a_new_credential_replaces_the_old_one(self, client):
        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"telegram_bot_token": "999:NEW"}})

        assert _stored(client)["telegram_bot_token"] == "999:NEW"

    def test_the_brave_search_api_key_is_treated_as_a_secret(self, client):
        """Brave's key is a paid-API credential like the Telegram bot token —
        it must never be readable in plain text from a settings page left
        open in a browser tab."""
        stored = _stored(client)
        stored["brave_search_api_key"] = "BSAsecretvalue12345"
        client.config_path.write_text(json.dumps(stored), encoding="utf-8")

        body = client.get("/api/settings", headers=HEADERS).get_json()
        field = _field(body, "brave_search_api_key")

        assert field["type"] == "password"
        assert "BSAsecretvalue12345" not in json.dumps(body)
        assert field["value"].endswith("2345")
        assert field["is_secret"] is True


class TestWriting:
    def test_a_changed_value_is_stored(self, client):
        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"whisper_language": "nl"}})

        assert _stored(client)["whisper_language"] == "nl"

    def test_a_value_set_back_to_its_default_is_removed(self, client):
        default = get_default_config()["whisper_language"]

        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"whisper_language": default}})

        assert "whisper_language" not in _stored(client)

    def test_keys_the_registry_does_not_describe_survive(self, client):
        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"whisper_language": "nl"}})

        stored = _stored(client)
        assert stored["mcps"]["rube"]["command"] == "npx"
        assert stored["_config_version"] == 3

    def test_a_number_outside_its_bounds_is_brought_back_inside(self, client):
        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"webui_port": 99}})

        assert _stored(client)["webui_port"] == 1024

    def test_a_boolean_arrives_as_a_boolean(self, client):
        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"webui_enabled": False}})

        assert _stored(client)["webui_enabled"] is False

    def test_a_list_field_accepts_a_list(self, client):
        client.put("/api/settings", headers=WRITE_HEADERS,
                   json={"changes": {"security_confirm_channels": ["web", "voice"]}})

        assert _stored(client)["security_confirm_channels"] == ["web", "voice"]

    def test_cloud_tts_chain_is_described_as_structured_fields(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()
        field = _field(body, "tts_cloud_providers")

        assert field["type"] == "object_list"
        assert [item["key"] for item in field["item_fields"]] == [
            "name", "provider", "api_key_env", "voice_id", "model",
            "enabled", "timeout_sec",
        ]
        provider = next(item for item in field["item_fields"] if item["key"] == "provider")
        assert provider["type"] == "choice"
        assert {choice["value"] for choice in provider["choices"]} == {
            "fish_audio", "elevenlabs",
        }
        defaults = {item["key"]: item["default"] for item in field["item_fields"]}
        assert defaults["enabled"] is True
        assert defaults["timeout_sec"] == 10.0

    def test_cloud_tts_metadata_never_resolves_environment_credentials(
        self, client, monkeypatch,
    ):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "must-never-reach-settings")
        stored = _stored(client)
        stored["tts_cloud_providers"] = [{
            "name": "ElevenLabs", "provider": "elevenlabs",
            "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "voice-1",
            "model": "eleven_multilingual_v2", "enabled": True,
            "timeout_sec": 8.5,
        }]
        client.config_path.write_text(json.dumps(stored), encoding="utf-8")

        raw = client.get("/api/settings", headers=HEADERS).get_data(as_text=True)

        assert "ELEVENLABS_API_KEY" in raw
        assert "must-never-reach-settings" not in raw

    def test_cloud_tts_chain_accepts_provider_objects(self, client):
        providers = [{
            "name": "ElevenLabs",
            "provider": "elevenlabs",
            "api_key_env": "ELEVENLABS_API_KEY",
            "voice_id": "voice-1",
            "model": "eleven_multilingual_v2",
            "enabled": True,
            "timeout_sec": 8.5,
        }]

        response = client.put(
            "/api/settings", headers=WRITE_HEADERS,
            json={"changes": {"tts_cloud_providers": providers}},
        )

        assert response.status_code == 200
        assert _stored(client)["tts_cloud_providers"] == providers

    def test_llm_category_points_to_the_route_chain_that_takes_precedence(self, client):
        body = client.get("/api/settings", headers=HEADERS).get_json()
        category = next(item for item in body["categories"] if item["key"] == "llm")

        assert category["action_href"] == "#/llm-routes"
        assert "route chain" in category["description"].lower()

    def test_the_answer_names_what_needs_a_restart(self, client):
        body = client.put("/api/settings", headers=WRITE_HEADERS,
                          json={"changes": {"whisper_language": "nl"}}).get_json()

        assert body["written"] == ["whisper_language"]
        assert body["restart_required"] == ["whisper_language"]


class TestRefusals:
    def test_an_unknown_key_is_refused_rather_than_written(self, client):
        response = client.put("/api/settings", headers=WRITE_HEADERS,
                              json={"changes": {"not_a_setting": 1}})

        assert response.status_code == 400
        assert "not_a_setting" not in _stored(client)

    def test_a_malformed_body_is_refused(self, client):
        response = client.put("/api/settings", headers=WRITE_HEADERS, json={"changes": "nope"})

        assert response.status_code == 400

    def test_writing_needs_the_write_header(self, client):
        response = client.put("/api/settings", headers=HEADERS,
                              json={"changes": {"whisper_language": "nl"}})

        assert response.status_code == 403
        assert _stored(client)["whisper_language"] == "de"
