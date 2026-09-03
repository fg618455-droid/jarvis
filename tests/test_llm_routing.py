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
    def __init__(self, *, direct_result=None, direct_error=None, stream=None,
                 chat_result=None, chat_error=None):
        self.direct_result = direct_result
        self.direct_error = direct_error
        self.stream = stream
        self.chat_result = chat_result
        self.chat_error = chat_error
        self.calls = 0
        self.chat_calls = 0
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
             tools=None, thinking=False, on_token=None):
        self.chat_calls += 1
        self.models.append(chat_model)
        if self.chat_error:
            raise self.chat_error
        return self.chat_result

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


def _settings_with_route(**overrides):
    route = {
        "name": "cloud",
        "provider": "openai_compatible",
        "base_url": "https://cloud.test/v1",
        "api_key": "",
        "api_key_env": "",
        "model": "cloud-model",
        "tier": "chat",
        "timeout_sec": 120.0,
        "enabled": True,
        "capabilities": ["chat", "stream", "tools"],
    }
    route.update(overrides)
    return SimpleNamespace(
        llm_routes=[route],
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="big-chat-model",
        llm_chat_model="cloud-chat-effective",
        fast_model="cloud-fast-effective",
        local_fast_model="tiny-fast-model",
        llm_provider="ollama",
    )


def _local(routes, tier):
    from jarvis.llm.route import RoutedBackend

    return next(
        route for route in routes
        if route.tier is tier and RoutedBackend._is_local(route)
    )


def test_a_disabled_route_leaves_the_local_chain_as_if_it_were_absent():
    """A route the user switched off must not reshape the local fallback.

    Otherwise switching a remote route off silently swaps the fast tier onto
    the big chat model and clamps every local call to the short fallback
    timeout, which turns each classification pass into a guaranteed miss.
    """
    from jarvis.llm.factory import get_llm_backend

    with_disabled = get_llm_backend(_settings_with_route(enabled=False)).routes
    without_any = get_llm_backend(SimpleNamespace(
        llm_routes=[],
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="big-chat-model",
        llm_chat_model="big-chat-model",
        fast_model="tiny-fast-model",
        llm_provider="ollama",
    )).routes

    for tier in (Tier.FAST, Tier.CHAT):
        disabled_local = _local(with_disabled, tier)
        plain_local = _local(without_any, tier)
        assert disabled_local.model == plain_local.model
        assert disabled_local.timeout_sec == plain_local.timeout_sec


def test_the_local_fast_route_runs_the_explicit_local_fallback_model():
    """A remote effective FAST name must never be sent to local Ollama."""
    from jarvis.llm.factory import get_llm_backend

    routes = get_llm_backend(_settings_with_route()).routes

    assert _local(routes, Tier.FAST).model == "tiny-fast-model"


def test_route_models_remain_authoritative_for_the_effective_fast_chain():
    from jarvis.llm import get_llm_backend, resolve_model

    settings = _settings_with_route(tier="fast", model="remote-fast-model")
    routes = get_llm_backend(settings).routes_for(Tier.FAST)

    assert routes[0].model == "remote-fast-model"
    assert _local(routes, Tier.FAST).model == "tiny-fast-model"
    assert resolve_model(settings, Tier.FAST) == "cloud-fast-effective"


def test_a_local_fallback_gets_room_to_load_a_cold_model():
    """Ollama evicts models, so a first call pays a page-in of many seconds.

    A fallback timeout shorter than that load turns the local route into a
    route that can never answer.
    """
    from jarvis.llm.factory import get_llm_backend

    routes = get_llm_backend(_settings_with_route()).routes

    assert _local(routes, Tier.FAST).timeout_sec >= 30.0
    assert _local(routes, Tier.CHAT).timeout_sec >= 30.0


def test_a_local_route_carries_the_configured_model_residency():
    """The residency the warmup asks for must reach every later request too."""
    from jarvis.llm.factory import get_llm_backend, OLLAMA_KEEP_ALIVE

    routes = get_llm_backend(_settings_with_route()).routes

    for tier in (Tier.FAST, Tier.CHAT):
        assert _local(routes, tier).keep_alive == OLLAMA_KEEP_ALIVE


def test_low_power_mode_hands_the_gpu_back_between_turns():
    from jarvis.llm.factory import get_llm_backend, LOW_POWER_OLLAMA_KEEP_ALIVE

    settings = _settings_with_route()
    settings.low_power_mode = True

    routes = get_llm_backend(settings).routes

    assert _local(routes, Tier.FAST).keep_alive == LOW_POWER_OLLAMA_KEEP_ALIVE


def test_a_remote_route_leaves_residency_to_its_own_server():
    """`keep_alive` is an Ollama knob; a remote endpoint must not be sent one."""
    from jarvis.llm.factory import get_llm_backend

    routes = get_llm_backend(_settings_with_route()).routes
    cloud = next(route for route in routes if route.name == "cloud")

    assert cloud.keep_alive == ""


class TestPreferredProviderRouting:
    """``chat(..., preferred_provider=...)`` promotes matching routes to the
    front of the attempt order for that one call, without discarding the
    rest of the chain — the manual-override and automatic-routing feature
    both ride this, and both need the existing fail-soft chain to still
    catch a promoted route that turns out to be unavailable."""

    def test_preferred_provider_is_tried_first(self, tmp_path):
        local = _route("local", provider="ollama")
        cloud = _route("cloud", provider="claude_subscription")
        local_backend = _Backend(chat_result={"message": {"content": "local"}})
        cloud_backend = _Backend(chat_result={"message": {"content": "cloud"}})
        router = _router(tmp_path, [local, cloud], {
            local: local_backend,
            cloud: cloud_backend,
        })

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                              preferred_provider="claude_subscription")

        assert result == {"message": {"content": "cloud"}}
        assert cloud_backend.chat_calls == 1
        assert local_backend.chat_calls == 0

    def test_falls_through_to_the_rest_of_the_chain_when_preferred_fails(self, tmp_path):
        """The promoted route existing but failing (e.g. claude-agent-sdk not
        installed) must not end the turn — it continues through the normal
        chain exactly as an unpromoted failure would."""
        local = _route("local", provider="ollama")
        cloud = _route("cloud", provider="claude_subscription")
        local_backend = _Backend(chat_result={"message": {"content": "local"}})
        cloud_backend = _Backend(chat_error=ProviderError("claude-agent-sdk is not installed"))
        router = _router(tmp_path, [cloud, local], {
            cloud: cloud_backend,
            local: local_backend,
        })

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                              preferred_provider="claude_subscription")

        assert result == {"message": {"content": "local"}}
        assert cloud_backend.chat_calls == 1
        assert local_backend.chat_calls == 1

    def test_preferred_provider_not_configured_falls_back_to_normal_order(self, tmp_path):
        """Forcing a provider that has no route in the chain at all must
        behave exactly like no preference was set — this is the "unavailable"
        case the manual override's fail-open depends on."""
        local = _route("local", provider="ollama")
        local_backend = _Backend(chat_result={"message": {"content": "local"}})
        router = _router(tmp_path, [local], {local: local_backend})

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                              preferred_provider="claude_subscription")

        assert result == {"message": {"content": "local"}}
        assert local_backend.chat_calls == 1

    def test_no_preferred_provider_keeps_configured_chain_order(self, tmp_path):
        first = _route("first", provider="claude_subscription")
        second = _route("second", provider="ollama")
        first_backend = _Backend(chat_result={"message": {"content": "first"}})
        second_backend = _Backend(chat_result={"message": {"content": "second"}})
        router = _router(tmp_path, [first, second], {
            first: first_backend,
            second: second_backend,
        })

        result = router.chat("chat", [{"role": "user", "content": "hi"}])

        assert result == {"message": {"content": "first"}}
        assert first_backend.chat_calls == 1
        assert second_backend.chat_calls == 0


class TestAnEmptyCompletionIsNotAnAnswer:
    """A route that returns a well-formed response carrying nothing has not
    answered. The chain must keep walking, exactly as it does for a route
    that raised or returned nothing at all: an emptiness handed back to the
    caller ends the turn on a route that never spoke."""

    def test_empty_content_falls_through_to_the_next_route(self, tmp_path):
        first = _route("first")
        second = _route("second")
        second_backend = _Backend(chat_result={"message": {"content": "real answer"}})
        router = _router(tmp_path, [first, second], {
            first: _Backend(chat_result={"message": {"role": "assistant", "content": ""}}),
            second: second_backend,
        })

        result = router.chat("chat", [{"role": "user", "content": "hi"}])

        assert result == {"message": {"content": "real answer"}}
        assert second_backend.chat_calls == 1

    def test_openai_shape_empty_completion_falls_through(self, tmp_path):
        """Hermes answers a tool-bearing turn with an OpenAI-shaped
        completion whose content is empty and whose token counts are zero."""
        first = _route("first")
        second = _route("second")
        second_backend = _Backend(chat_result={"message": {"content": "real answer"}})
        router = _router(tmp_path, [first, second], {
            first: _Backend(chat_result={
                "choices": [{"message": {"role": "assistant", "content": ""}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }),
            second: second_backend,
        })

        result = router.chat("chat", [{"role": "user", "content": "hi"}])

        assert result == {"message": {"content": "real answer"}}
        assert second_backend.chat_calls == 1

    def test_a_tool_call_without_text_is_an_answer(self, tmp_path):
        """The usual shape of a tool turn: no prose, one call. It must own
        the answer rather than being mistaken for silence."""
        first = _route("first")
        second = _route("second")
        tool_response = {"message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "webSearch", "arguments": {}}}],
        }}
        second_backend = _Backend(chat_result={"message": {"content": "must not run"}})
        router = _router(tmp_path, [first, second], {
            first: _Backend(chat_result=tool_response),
            second: second_backend,
        })

        assert router.chat("chat", [{"role": "user", "content": "hi"}]) == tool_response
        assert second_backend.chat_calls == 0

    def test_a_thinking_only_turn_is_an_answer(self, tmp_path):
        """Reasoning with no prose yet is the model working, not silence.
        The engine lets that turn continue; the router must not take the
        decision away from it."""
        first = _route("first")
        second = _route("second")
        thinking = {"message": {"role": "assistant", "content": "",
                                "thinking": "let me work through this"}}
        second_backend = _Backend(chat_result={"message": {"content": "must not run"}})
        router = _router(tmp_path, [first, second], {
            first: _Backend(chat_result=thinking),
            second: second_backend,
        })

        assert router.chat("chat", [{"role": "user", "content": "hi"}]) == thinking
        assert second_backend.chat_calls == 0

    def test_an_all_empty_chain_returns_nothing_rather_than_an_empty_shell(self, tmp_path):
        """With every route silent the caller must see the exhausted chain,
        not a response object it will read as a finished reply."""
        first = _route("first")
        second = _route("second")
        router = _router(tmp_path, [first, second], {
            first: _Backend(chat_result={"message": {"content": ""}}),
            second: _Backend(chat_result={"message": {"content": "   "}}),
        })

        assert router.chat("chat", [{"role": "user", "content": "hi"}]) is None


class TestAnEmptyLaneSaysSoRatherThanLookingLikeAFailure:
    """A chain with no candidate at all is not the same as one whose
    candidates all failed, and the difference is what a user needs to see.

    A tool-bearing call only considers routes advertising ``tools``, which
    can be a much shorter list than the tier's. When cooldowns take the
    last of them, the walk ends before it starts: no request is made, no
    route is marked failed, and nothing in the logs distinguishes that
    from a model with nothing to say.
    """

    def _capture(self, monkeypatch):
        from jarvis.llm import route as route_mod

        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            route_mod, "debug_log",
            lambda message, category="debug": seen.append((message, category)),
        )
        return seen

    def test_a_tool_call_with_no_tool_capable_route_is_logged(self, tmp_path, monkeypatch):
        chat_only = Route(
            **{**_route("chat-only").__dict__, "capabilities": frozenset({"chat"})}
        )
        seen = self._capture(monkeypatch)
        router = _router(tmp_path, [chat_only], {chat_only: _Backend()})

        result = router.chat(
            "chat", [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "createEvent"}}],
        )

        assert result is None
        assert any("tools" in message for message, _ in seen)

    def test_a_chain_with_candidates_does_not_log_an_empty_lane(self, tmp_path, monkeypatch):
        route = _route("ordinary")
        seen = self._capture(monkeypatch)
        router = _router(tmp_path, [route], {
            route: _Backend(chat_result={"message": {"content": "answer"}}),
        })

        router.chat("chat", [{"role": "user", "content": "hi"}])

        assert not any("no enabled" in message for message, _ in seen)
