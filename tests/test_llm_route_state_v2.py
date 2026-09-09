from __future__ import annotations

import json

from jarvis.llm import ProviderError, RateLimitedError, Tier
from jarvis.llm.route import RequestDeadline, Route, RoutedBackend
from jarvis.llm.route_state import RouteStateStore, route_state_key


def _route(model="model-a"):
    return Route(
        name="cloud", provider="openai_compatible",
        base_url="https://api.example/v1", api_key="synthetic",
        model=model, tier=Tier.CHAT, timeout_sec=3.0,
    )


class _Backend:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def direct(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.value


def test_exactly_one_attempt_and_failure_per_started_call(tmp_path):
    route = _route()
    state = RouteStateStore(tmp_path / "state.json")
    router = RoutedBackend(
        [route], state_store=state,
        backend_factory=lambda _: _Backend(error=ProviderError()),
    )

    assert router.direct("chat", "s", "u") is None
    status = state.status(route)
    assert status["attempts"] == 1
    assert status["provider_failures"] == 1
    assert status["empty_responses"] == 0
    assert state.chain_status()["chat"]["exhaustions"] == 1


def test_cooldown_skip_and_deadline_skip_are_not_provider_failures(tmp_path):
    now = [100.0]
    route = _route()
    state = RouteStateStore(tmp_path / "state.json", now=lambda: now[0])
    state.record_attempt(route)
    state.record_provider_failure(route, RateLimitedError(retry_after=60))
    router = RoutedBackend(
        [route], state_store=state,
        backend_factory=lambda _: _Backend(value="must not run"),
        clock=lambda: now[0],
    )

    assert router.direct("chat", "s", "u") is None
    status = state.status(route)
    assert status["provider_failures"] == 1
    assert status["cooldown_skips"] == 1

    state.reset(route)
    deadline = RequestDeadline(ends_at=99.0)
    assert router._run("chat", lambda *_: "x", deadline=deadline) is None
    status = state.status(route)
    assert status["attempts"] == 0
    assert status["provider_failures"] == 0
    assert status["deadline_skips"] == 1


def test_empty_response_has_its_own_counter(tmp_path):
    route = _route()
    state = RouteStateStore(tmp_path / "state.json")
    router = RoutedBackend(
        [route], state_store=state,
        backend_factory=lambda _: _Backend(value=None),
    )

    assert router.direct("chat", "s", "u") is None
    status = state.status(route)
    assert status["attempts"] == 1
    assert status["empty_responses"] == 1
    assert status["provider_failures"] == 0


def test_route_key_changes_with_model_and_contains_no_route_details():
    first = route_state_key(_route("model-a"))
    second = route_state_key(_route("model-b"))

    assert first != second
    assert "model" not in first
    assert "api.example" not in first


def test_old_bom_state_is_read_tolerantly(tmp_path):
    route = _route()
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "routes": {route_state_key(route): {"hits": 2, "failures": 3}},
    }), encoding="utf-8-sig")

    status = RouteStateStore(path).status(route)

    assert status["successes"] == 2
    assert status["provider_failures"] == 3
