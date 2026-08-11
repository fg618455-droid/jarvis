"""Observable behaviour of the tiered LLM fallback router."""

from __future__ import annotations

import io
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from jarvis.llm import (
    AuthError,
    LLMBackend,
    ProviderError,
    RateLimitedError,
    Tier,
    ToolsNotSupportedError,
)
from jarvis.llm.route import RequestDeadline, Route, RoutedBackend
from jarvis.llm.route_state import RouteStateStore


def test_probe_cli_supports_a_windows_cp1252_console(monkeypatch):
    from jarvis.llm import probe

    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(probe, "load_fcc_values", dict)

    assert probe.main() == 1
    output.flush()
    assert "FCC environment not found" in raw.getvalue().decode("utf-8")


class _Backend(LLMBackend):
    def __init__(self, *, direct_result=None, direct_error=None, stream=None):
        self.direct_result = direct_result
        self.direct_error = direct_error
        self.stream = stream
        self.calls = 0
        self.models = []
        self.warmed = []

    def direct(self, chat_model, system_prompt, user_content, timeout_sec=10.0,
               thinking=False, num_ctx=4096, temperature=None, max_tokens=None):
        self.calls += 1
        self.models.append(chat_model)
        if self.direct_error:
            raise self.direct_error
        return self.direct_result

    def streaming(self, chat_model, system_prompt, user_content, on_token=None,
                  timeout_sec=30.0, thinking=False):
        self.calls += 1
        if callable(self.stream):
            return self.stream(on_token)
        if isinstance(self.stream, BaseException):
            raise self.stream
        return self.stream

    def chat(self, chat_model, messages, timeout_sec=30.0, extra_options=None,
             tools=None, thinking=False):
        return None

    def embed(self, text, model, timeout_sec=15.0):
        return None

    def list_models(self, timeout_sec=5.0):
        return []

    def warm_up(self, model, timeout_sec=60.0, keep_alive="30m"):
        self.warmed.append(model)
        return True


def _route(name: str, *, provider: str = "openai_compatible") -> Route:
    return Route(
        name=name,
        provider=provider,
        base_url=(
            "http://127.0.0.1:9/v1"
            if provider == "ollama"
            else "https://example.invalid/v1"
        ),
        api_key="",
        model=f"{name}-model",
        tier=Tier.CHAT,
        timeout_sec=4.0,
    )


def _router(tmp_path: Path, routes, backends) -> RoutedBackend:
    state = RouteStateStore(tmp_path / "llm-routes-state.json")
    return RoutedBackend(routes, state_store=state, backend_factory=backends.__getitem__)


class _DelayedBackend(_Backend):
    def __init__(self, delay: float, value: str):
        super().__init__()
        self.delay = delay
        self.value = value

    def streaming(self, chat_model, system_prompt, user_content, on_token=None,
                  timeout_sec=30.0, thinking=False):
        self.calls += 1
        self.models.append(chat_model)
        time.sleep(self.delay)
        if on_token:
            on_token(self.value)
        return self.value


def test_rate_limit_switches_to_next_route_and_returns_normal_result(tmp_path):
    first = _route("first")
    second = _route("second")
    router = _router(tmp_path, [first, second], {
        first: _Backend(direct_error=RateLimitedError(retry_after=30)),
        second: _Backend(direct_result="answer"),
    })

    assert router.direct("chat", "system", "user") == "answer"


def test_auth_failure_drops_route_for_the_rest_of_the_run(tmp_path):
    first = _route("first")
    second = _route("second")
    first_backend = _Backend(direct_error=AuthError())
    second_backend = _Backend(direct_result="answer")
    router = _router(tmp_path, [first, second], {
        first: first_backend,
        second: second_backend,
    })

    assert router.direct("chat", "system", "user") == "answer"
    assert router.direct("chat", "system", "user") == "answer"
    assert first_backend.calls == 1


def test_dead_cloud_route_falls_back_to_local_ollama(tmp_path):
    cloud = _route("cloud")
    local = _route("local", provider="ollama")
    router = _router(tmp_path, [cloud, local], {
        cloud: _Backend(direct_error=ProviderError()),
        local: _Backend(direct_result="local answer"),
    })

    assert router.direct("chat", "system", "user") == "local answer"


def test_every_route_dead_returns_none(tmp_path):
    first = _route("first")
    local = _route("local", provider="ollama")
    router = _router(tmp_path, [first, local], {
        first: _Backend(direct_error=ProviderError()),
        local: _Backend(direct_result=None),
    })

    assert router.direct("chat", "system", "user") is None


def test_tools_not_supported_is_not_a_routing_signal(tmp_path):
    first = _route("first")
    second = _route("second")
    second_backend = _Backend(direct_result="must not run")
    router = _router(tmp_path, [first, second], {
        first: _Backend(direct_error=ToolsNotSupportedError()),
        second: second_backend,
    })

    with pytest.raises(ToolsNotSupportedError):
        router.direct("chat", "system", "user")
    assert second_backend.calls == 0


def test_streaming_failure_after_first_token_does_not_switch_route(tmp_path):
    first = _route("first")
    second = _route("second")

    def partial_then_fail(on_token):
        on_token("partial")
        raise RateLimitedError(retry_after=30)

    second_backend = _Backend(stream="replacement")
    router = _router(tmp_path, [first, second], {
        first: _Backend(stream=partial_then_fail),
        second: second_backend,
    })
    seen = []

    assert router.streaming("chat", "system", "user", on_token=seen.append) is None
    assert seen == ["partial"]
    assert second_backend.calls == 0


def test_cooldown_survives_router_restart(tmp_path):
    first = _route("first")
    local = _route("local", provider="ollama")
    state_path = tmp_path / "llm-routes-state.json"
    first_backend = _Backend(direct_error=RateLimitedError(retry_after=120))
    local_backend = _Backend(direct_result="local")
    router = RoutedBackend(
        [first, local],
        state_store=RouteStateStore(state_path),
        backend_factory={first: first_backend, local: local_backend}.__getitem__,
    )
    assert router.direct("chat", "system", "user") == "local"

    untouched_after_restart = _Backend(direct_error=AssertionError("blocked route was retried"))
    restarted = RoutedBackend(
        [first, local],
        state_store=RouteStateStore(state_path),
        backend_factory={first: untouched_after_restart, local: local_backend}.__getitem__,
    )
    assert restarted.direct("chat", "system", "user") == "local"
    assert untouched_after_restart.calls == 0


def test_streaming_falls_forward_when_local_makes_no_progress(tmp_path):
    local = _route("local", provider="ollama")
    cloud = _route("cloud")
    seen = []
    router = RoutedBackend(
        [cloud, local],
        state_store=RouteStateStore(tmp_path / "state.json"),
        backend_factory={
            local: _DelayedBackend(0.08, "late local"),
            cloud: _DelayedBackend(0.0, "cloud answer"),
        }.__getitem__,
        local_progress_sec=0.01,
    )

    result = router.streaming(
        "chat", "system", "user", on_token=seen.append,
        deadline=RequestDeadline.after(1.0),
    )
    time.sleep(0.1)

    assert result == "cloud answer"
    assert seen == ["cloud answer"]


def test_streaming_route_that_starts_owns_the_answer(tmp_path):
    local = _route("local", provider="ollama")
    cloud = _route("cloud")
    cloud_backend = _DelayedBackend(0.0, "cloud answer")
    router = RoutedBackend(
        [local, cloud],
        state_store=RouteStateStore(tmp_path / "state.json"),
        backend_factory={
            local: _DelayedBackend(0.01, "local answer"),
            cloud: cloud_backend,
        }.__getitem__,
        local_progress_sec=0.1,
    )

    assert router.streaming(
        "chat", "system", "user", deadline=RequestDeadline.after(1.0)
    ) == "local answer"
    assert cloud_backend.calls == 0


def test_disabled_or_incapable_routes_are_not_selected(tmp_path):
    disabled = Route(
        **{**_route("disabled").__dict__, "enabled": False}
    )
    no_stream = Route(
        **{**_route("no-stream").__dict__, "capabilities": frozenset({"chat"})}
    )
    local = _route("local", provider="ollama")
    skipped = _Backend(stream=AssertionError("route should be skipped"))
    router = RoutedBackend(
        [disabled, no_stream, local],
        state_store=RouteStateStore(tmp_path / "state.json"),
        backend_factory={
            disabled: skipped,
            no_stream: skipped,
            local: _Backend(stream="local answer"),
        }.__getitem__,
    )

    assert router.streaming("chat", "system", "user") == "local answer"


def test_factory_preserves_config_order_and_keeps_local_fallback():
    from jarvis.llm.factory import get_llm_backend

    settings = SimpleNamespace(
        llm_routes=[{
            "name": "cloud",
            "provider": "openai_compatible",
            "base_url": "https://cloud.test/v1",
            "api_key": "",
            "api_key_env": "",
            "model": "cloud-model",
            "tier": "chat",
            "timeout_sec": 4.0,
            "enabled": True,
            "capabilities": ["chat", "stream", "tools"],
        }],
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="local-model",
        llm_chat_model="cloud-model",
        fast_model="",
        llm_provider="ollama",
    )

    routes = get_llm_backend(settings).routes_for(Tier.CHAT)
    assert [route.name for route in routes] == ["cloud", "local-chat"]


def test_environment_credential_is_resolved_without_entering_route_state(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_API_KEY", "runtime-secret")
    route = Route(
        **{**_route("cloud").__dict__, "api_key_env": "CLOUD_API_KEY"}
    )
    router = RoutedBackend([route], state_store=RouteStateStore(tmp_path / "state.json"))

    assert router._backend(route)._api_key == "runtime-secret"
    assert "runtime-secret" not in repr(route)
    router._state.record_hit(route)
    assert "runtime-secret" not in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_request_deadline_uses_a_monotonic_shared_budget():
    now = [10.0]
    clock = lambda: now[0]
    deadline = RequestDeadline.after(3.0, clock=clock)

    now[0] = 11.25
    assert deadline.remaining(clock=clock) == pytest.approx(1.75)
    assert deadline.expired(clock=clock) is False

    now[0] = 13.0
    assert deadline.remaining(clock=clock) == 0.0
    assert deadline.expired(clock=clock) is True


def test_each_fallback_route_uses_its_configured_model(tmp_path):
    first = _route("first")
    second = _route("second")
    first_backend = _Backend(direct_error=ProviderError())
    second_backend = _Backend(direct_result="answer")
    router = _router(tmp_path, [first, second], {
        first: first_backend,
        second: second_backend,
    })

    assert router.direct("unrelated-model", "system", "user") == "answer"
    assert first_backend.models == ["first-model"]
    assert second_backend.models == ["second-model"]


def test_warm_up_reaches_the_active_and_local_runtimes(tmp_path):
    cloud = Route(
        **{**_route("cloud").__dict__, "base_url": "https://cloud.test/v1"}
    )
    local = _route("local", provider="ollama")
    cloud_backend = _Backend()
    local_backend = _Backend()
    router = _router(tmp_path, [cloud, local], {
        cloud: cloud_backend,
        local: local_backend,
    })

    assert router.warm_up("chat", timeout_sec=5.0) is True
    assert cloud_backend.warmed == ["cloud-model"]
    assert local_backend.warmed == ["local-model"]


def test_remote_route_never_borrows_the_single_endpoint_key():
    from jarvis.llm.factory import get_llm_backend

    settings = SimpleNamespace(
        llm_routes=[{
            "name": "cloud",
            "provider": "openai_compatible",
            "base_url": "https://cloud.test/v1",
            "api_key": "",
            "api_key_env": "NOT_SET_ANYWHERE",
            "model": "cloud-model",
            "tier": "chat",
            "timeout_sec": 4.0,
            "enabled": True,
            "capabilities": ["chat", "stream", "tools"],
        }],
        llm_api_key="single-endpoint-secret",
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:1234/v1",
        llm_chat_model="legacy-model",
        fast_model="fast-model",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="local-model",
    )

    router = get_llm_backend(settings)
    cloud = router.routes_for(Tier.CHAT)[0]
    assert router._backend(cloud)._api_key is None


def test_route_chains_keep_embeddings_on_loopback_ollama():
    from jarvis.llm import OllamaBackend, get_embedding_backend

    settings = SimpleNamespace(
        llm_routes=[{"tier": "chat"}],
        ollama_base_url="http://192.0.2.4:11434",
    )

    backend = get_embedding_backend(settings)
    assert isinstance(backend, OllamaBackend)
    assert backend.base_url == "http://127.0.0.1:11434"
