from __future__ import annotations

import json

import pytest
import requests

from jarvis.llm import (
    AuthError,
    BillingError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    Tier,
    ToolsNotSupportedError,
)
from jarvis.llm.probe import classify_probe_error, load_fcc_values, probe_route
from jarvis.llm.route import Route


def _route(**overrides):
    values = {
        "name": "mistral",
        "provider": "openai_compatible",
        "base_url": "https://api.example/v1",
        "api_key": "",
        "api_key_env": "PROBE_KEY",
        "model": "model-a",
        "tier": Tier.FAST,
        "timeout_sec": 2.0,
        "capabilities": frozenset({"chat", "stream", "tools"}),
    }
    values.update(overrides)
    return Route(**values)


class _Backend:
    calls = 0

    def list_models(self, timeout_sec):
        self.calls += 1
        return ["model-a", "model-b"]

    def direct(self, *args, **kwargs):
        self.calls += 1
        return "visible"

    def streaming(self, *args, on_token=None, **kwargs):
        self.calls += 1
        on_token("first")
        return "first"

    def chat(self, *args, **kwargs):
        self.calls += 1
        return {"message": {"tool_calls": [{
            "function": {"name": "probe_echo", "arguments": "{\"value\":\"ok\"}"}
        }]}}


def test_blank_credential_is_unconfigured_and_never_called():
    backend = _Backend()
    result = probe_route(_route(), backend=backend, environ={"PROBE_KEY": "   "})

    assert result["configured"] is False
    assert result["credential"] == {"required": True, "present": False, "source": "environment"}
    assert backend.calls == 0


def test_probe_reports_each_capability_without_response_content():
    backend = _Backend()
    result = probe_route(_route(), backend=backend, environ={"PROBE_KEY": "secret"})

    assert result["model_listing"]["ok"] is True
    assert result["model_exact"] is True
    assert result["chat"]["ok"] is True
    assert result["stream"]["ok"] is True
    assert result["stream"]["ttft_ms"] is not None
    assert result["tools"]["native"] is True
    assert result["import_candidate"] is True
    serialized = json.dumps(result)
    assert "secret" not in serialized
    assert "visible" not in serialized


def test_subscription_probe_uses_text_tool_fallback():
    class Subscription(_Backend):
        def list_models(self, timeout_sec):
            raise AssertionError("subscription routes do not list models")

        def chat(self, *args, **kwargs):
            raise ToolsNotSupportedError()

        def direct(self, *args, **kwargs):
            self.calls += 1
            if "Return only JSON" in args[1]:
                return '{"tool":"probe_echo","arguments":{"value":"ok"}}'
            return "visible"

    result = probe_route(
        _route(
            provider="claude_subscription",
            base_url="claude-cli",
            api_key_env="",
            tier=Tier.CHAT,
        ),
        backend=Subscription(),
        environ={},
    )

    assert result["model_listing"]["supported"] is False
    assert result["tools"]["native"] is False
    assert result["tools"]["text_fallback"] is True
    assert result["ok"] is True
    assert result["import_candidate"] is False


@pytest.mark.parametrize(("error", "expected"), [
    (AuthError(), "auth"),
    (BillingError(), "billing"),
    (QuotaExhaustedError(), "quota"),
    (RateLimitedError(), "quota"),
    (ModelUnavailableError(), "model_missing"),
    (TimeoutError(), "timeout"),
    (requests.ConnectionError(), "transport"),
    ("empty response", "empty_response"),
])
def test_probe_error_classes_are_stable(error, expected):
    assert classify_probe_error(error) == expected


def test_fcc_loader_omits_blank_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PRESENT=value\nBLANK=   \nEMPTY=\n", encoding="utf-8")

    assert load_fcc_values(env) == {"PRESENT": "value"}
