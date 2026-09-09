"""Behaviour of catalogued cloud routes imported from FCC credentials."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from jarvis.llm import Tier, get_llm_backend, resolve_model
from jarvis.llm.route import Route, RoutedBackend
from jarvis.llm.route_catalogue import ENDPOINTS
from jarvis.llm.route_state import RouteStateStore
from scripts.import_fcc_keys import build_routes


_ROUTE_NAMES = ("gemini", "openrouter")


def _endpoint(name: str):
    return next(endpoint for endpoint in ENDPOINTS if endpoint.name == name)


def _settings(routes):
    return SimpleNamespace(
        llm_routes=routes,
        llm_provider="ollama",
        llm_chat_model="local-chat-model",
        fast_model="local-fast-model",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="local-chat-model",
    )


class _Response:
    def __init__(self, *, status_code=200, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


def test_openrouter_catalogue_connection_metadata():
    endpoint = _endpoint("openrouter")

    assert endpoint.base_url == "https://openrouter.ai/api/v1"
    assert endpoint.key_env == "OPENROUTER_API_KEY"
    assert endpoint.model_env == "FCC_SMOKE_MODEL_OPEN_ROUTER"


def test_gemini_and_openrouter_import_into_the_chat_chain_only():
    endpoints = [_endpoint(name) for name in _ROUTE_NAMES]
    values = {}
    results = []
    for endpoint in endpoints:
        advertised_model = f"advertised-by-{endpoint.name}"
        values[endpoint.key_env] = f"credential-for-{endpoint.name}"
        values[endpoint.model_env] = advertised_model
        results.append({"name": endpoint.name, "models": [advertised_model]})

    routes = build_routes(values, results)

    for endpoint in endpoints:
        matches = [route for route in routes if route["name"] == endpoint.name]
        assert [(route["tier"], route["model"]) for route in matches] == [
            ("chat", values[endpoint.model_env])
        ]


@pytest.mark.parametrize("endpoint_name", _ROUTE_NAMES)
def test_catalogued_cloud_route_cannot_enter_the_private_chain(endpoint_name):
    endpoint = _endpoint(endpoint_name)
    configured = {
        "name": endpoint.name,
        "provider": "openai_compatible",
        "base_url": endpoint.base_url,
        "api_key": "",
        "api_key_env": endpoint.key_env,
        "model": "configured-model",
        "tier": "private",
        "timeout_sec": 4.0,
    }

    backend = get_llm_backend(_settings([configured]))

    assert [route.name for route in backend.routes_for(Tier.PRIVATE)] == [
        "local-private"
    ]
    assert endpoint.name not in {route.name for route in backend.routes}


@pytest.mark.parametrize("endpoint_name", _ROUTE_NAMES)
def test_environment_credential_stays_out_of_config_and_repr(
    endpoint_name, monkeypatch,
):
    endpoint = _endpoint(endpoint_name)
    secret = f"runtime-secret-for-{endpoint.name}"
    monkeypatch.setenv(endpoint.key_env, secret)
    configured = {
        "name": endpoint.name,
        "provider": "openai_compatible",
        "base_url": endpoint.base_url,
        "api_key": "",
        "api_key_env": endpoint.key_env,
        "model": "configured-model",
        "tier": "chat",
        "timeout_sec": 4.0,
    }
    settings = _settings([configured])
    backend = get_llm_backend(settings)
    route = next(
        route for route in backend.routes_for(Tier.CHAT)
        if route.name == endpoint.name
    )

    def respond(*_args, **kwargs):
        assert kwargs["headers"]["Authorization"] == f"Bearer {secret}"
        return _Response(body={
            "choices": [{"message": {"role": "assistant", "content": "resolved"}}]
        })

    monkeypatch.setattr("jarvis.llm.openai_compatible.requests.post", respond)

    assert secret not in repr(settings.llm_routes)
    assert secret not in repr(route)
    assert backend.direct(
        resolve_model(settings, Tier.CHAT), "system", "user"
    ) == "resolved"


@pytest.mark.parametrize("endpoint_name", _ROUTE_NAMES)
def test_rate_limit_enters_the_documented_cooldown(
    endpoint_name, monkeypatch, tmp_path,
):
    endpoint = _endpoint(endpoint_name)
    route = Route(
        name=endpoint.name,
        provider="openai_compatible",
        base_url=endpoint.base_url,
        api_key="",
        api_key_env=endpoint.key_env,
        model="configured-model",
        tier=Tier.CHAT,
        timeout_sec=4.0,
    )
    monkeypatch.setattr(
        "jarvis.llm.openai_compatible.requests.post",
        lambda *_args, **_kwargs: _Response(
            status_code=429,
            headers={"Retry-After": "17"},
        ),
    )
    router = RoutedBackend(
        [route],
        state_store=RouteStateStore(tmp_path / "route-state.json"),
    )

    assert router.direct(
        resolve_model(_settings([]), Tier.CHAT), "system", "user"
    ) is None
    status = router.route_status()["chat"][0]
    assert status["last_error"] == "RateLimitedError"
    assert status["blocked_until"] is not None
