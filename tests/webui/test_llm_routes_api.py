"""Control centre API for inspecting and editing LLM route chains."""

from __future__ import annotations

import json


def _write_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "_config_version": 4,
        "ollama_chat_model": "local-model",
        "llm_routes": [{
            "name": "cloud-chat",
            "provider": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "api_key": "synthetic-credential",
            "model": "served-model",
            "tier": "chat",
            "timeout_sec": 4.0,
        }],
    }))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    monkeypatch.setenv("JARVIS_LLM_ROUTE_STATE_PATH", str(tmp_path / "route-state.json"))
    return path


def test_get_routes_masks_keys_and_makes_no_outbound_request(
    api_client, tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch)
    requested = []
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: requested.append(args))
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: requested.append(args))

    response = api_client.get("/api/llm/routes")

    assert response.status_code == 200
    raw = response.get_data(as_text=True)
    assert "synthetic-credential" not in raw
    route = response.get_json()["chains"]["chat"][0]
    assert route["masked_key"].endswith("tial")
    assert route["active"] is True
    assert requested == []


def test_put_routes_preserves_a_masked_key(api_client, tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch)
    current = api_client.get("/api/llm/routes").get_json()["chains"]["chat"][0]
    route = {
        "name": current["name"],
        "provider": current["provider"],
        "base_url": current["base_url"],
        "api_key": current["masked_key"],
        "api_key_env": "ROUTE_API_KEY",
        "model": "replacement-model",
        "tier": current["tier"],
        "timeout_sec": current["timeout_sec"],
        "enabled": False,
        "capabilities": ["chat", "stream"],
    }

    response = api_client.put("/api/llm/routes", json={"routes": [route]})

    assert response.status_code == 200
    stored = json.loads(path.read_text())["llm_routes"][0]
    assert stored["api_key"] == "synthetic-credential"
    assert stored["api_key_env"] == "ROUTE_API_KEY"
    assert stored["model"] == "replacement-model"
    assert stored["enabled"] is False
    assert stored["capabilities"] == ["chat", "stream"]


def test_route_api_and_debug_log_never_emit_clear_key(
    api_client, tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch)
    logged = []
    monkeypatch.setattr("jarvis.webui.api.llm.debug_log", lambda message, area: logged.append(message))

    response = api_client.get("/api/llm/routes")

    assert "synthetic-credential" not in response.get_data(as_text=True)
    assert all("synthetic-credential" not in message for message in logged)


def test_get_routes_reports_the_default_chat_backend_override(
    api_client, tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch)

    response = api_client.get("/api/llm/routes")

    assert response.get_json()["chat_backend_override"] == "auto"


def test_put_chat_backend_override_persists_and_is_reported_back(
    api_client, tmp_path, monkeypatch
):
    path = _write_config(tmp_path, monkeypatch)

    response = api_client.put(
        "/api/llm/routes/chat-backend-override",
        json={"chat_backend_override": "claude_subscription"},
    )

    assert response.status_code == 200
    assert response.get_json()["chat_backend_override"] == "claude_subscription"
    stored = json.loads(path.read_text())
    assert stored["chat_backend_override"] == "claude_subscription"

    follow_up = api_client.get("/api/llm/routes")
    assert follow_up.get_json()["chat_backend_override"] == "claude_subscription"


def test_put_chat_backend_override_blank_resets_to_auto(
    api_client, tmp_path, monkeypatch
):
    path = _write_config(tmp_path, monkeypatch)
    api_client.put(
        "/api/llm/routes/chat-backend-override",
        json={"chat_backend_override": "ollama"},
    )

    response = api_client.put(
        "/api/llm/routes/chat-backend-override",
        json={"chat_backend_override": ""},
    )

    assert response.get_json()["chat_backend_override"] == "auto"
    stored = json.loads(path.read_text())
    assert stored["chat_backend_override"] == "auto"


def test_put_chat_backend_override_logs_the_change(
    api_client, tmp_path, monkeypatch
):
    _write_config(tmp_path, monkeypatch)
    logged = []
    monkeypatch.setattr("jarvis.webui.api.llm.debug_log", lambda message, area: logged.append(message))

    api_client.put(
        "/api/llm/routes/chat-backend-override",
        json={"chat_backend_override": "claude_subscription"},
    )

    assert any("claude_subscription" in message for message in logged)
