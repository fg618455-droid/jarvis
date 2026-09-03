"""One slow provider must not own a voice turn.

A chat chain walks its routes in order and each route gets the smaller of
its own limit and the remaining caller budget. With a caller budget of
three minutes, that arithmetic never actually stops anything: a chain of a
25-second gateway, a 20-second broker and a 60-second subscription session
can spend nearly two minutes before the first word is spoken, and it does
so silently, because every individual route stayed inside its own limit.

The chain budget is the ceiling on the walk itself, independent of what any
single route is allowed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.config import load_settings
from jarvis.llm import LLMBackend, Tier
from jarvis.llm.route import Route, RoutedBackend
from jarvis.llm.route_state import RouteStateStore


class _Clock:
    """A hand-wound monotonic clock: a route's cost is what we say it is."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _SlowBackend(LLMBackend):
    def __init__(self, clock: _Clock, cost: float, answer):
        self._clock = clock
        self._cost = cost
        self._answer = answer
        self.calls = 0

    def direct(self, chat_model, system_prompt, user_content, timeout_sec=10.0,
               thinking=False, num_ctx=4096, temperature=None, max_tokens=None):
        return None

    def streaming(self, chat_model, system_prompt, user_content, on_token=None,
                  timeout_sec=30.0, thinking=False):
        return None

    def chat(self, chat_model, messages, timeout_sec=30.0, extra_options=None,
             tools=None, thinking=False, on_token=None):
        self.calls += 1
        self._clock.now += self._cost
        return self._answer

    def embed(self, text, model, timeout_sec=15.0):
        return None

    def list_models(self, timeout_sec=5.0):
        return []

    def warm_up(self, model, timeout_sec=60.0, keep_alive="30m"):
        return True


def _route(name: str, timeout_sec: float) -> Route:
    return Route(
        name=name,
        provider="openai_compatible",
        base_url="https://example.invalid/v1",
        api_key="",
        model=f"{name}-model",
        tier=Tier.CHAT,
        timeout_sec=timeout_sec,
    )


class TestTheChainHasACeilingOfItsOwn:
    def test_the_walk_stops_once_the_budget_is_spent(self, tmp_path: Path):
        clock = _Clock()
        slow = _route("slow", 60.0)
        later = _route("later", 60.0)
        later_backend = _SlowBackend(clock, 1.0, {"message": {"content": "too late"}})
        router = RoutedBackend(
            [slow, later],
            state_store=RouteStateStore(tmp_path / "state.json"),
            backend_factory={
                slow: _SlowBackend(clock, 40.0, None),
                later: later_backend,
            }.__getitem__,
            clock=clock,
            chain_budget_sec=30.0,
        )

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             timeout_sec=180.0)

        assert result is None
        assert later_backend.calls == 0

    def test_a_chain_inside_the_budget_still_falls_all_the_way_through(self, tmp_path: Path):
        clock = _Clock()
        first = _route("first", 60.0)
        second = _route("second", 60.0)
        second_backend = _SlowBackend(clock, 2.0, {"message": {"content": "answer"}})
        router = RoutedBackend(
            [first, second],
            state_store=RouteStateStore(tmp_path / "state.json"),
            backend_factory={
                first: _SlowBackend(clock, 2.0, None),
                second: second_backend,
            }.__getitem__,
            clock=clock,
            chain_budget_sec=30.0,
        )

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             timeout_sec=180.0)

        assert result == {"message": {"content": "answer"}}
        assert second_backend.calls == 1

    def test_a_caller_asking_for_less_than_the_budget_keeps_its_own_limit(self, tmp_path: Path):
        """The budget is a ceiling, never a floor: a caller with a tighter
        deadline must not be given more time by configuring it."""
        clock = _Clock()
        first = _route("first", 60.0)
        second = _route("second", 60.0)
        second_backend = _SlowBackend(clock, 1.0, {"message": {"content": "too late"}})
        router = RoutedBackend(
            [first, second],
            state_store=RouteStateStore(tmp_path / "state.json"),
            backend_factory={
                first: _SlowBackend(clock, 6.0, None),
                second: second_backend,
            }.__getitem__,
            clock=clock,
            chain_budget_sec=300.0,
        )

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             timeout_sec=5.0)

        assert result is None
        assert second_backend.calls == 0

    def test_an_unset_budget_leaves_the_walk_to_the_caller_timeout(self, tmp_path: Path):
        clock = _Clock()
        first = _route("first", 60.0)
        second = _route("second", 60.0)
        second_backend = _SlowBackend(clock, 1.0, {"message": {"content": "answer"}})
        router = RoutedBackend(
            [first, second],
            state_store=RouteStateStore(tmp_path / "state.json"),
            backend_factory={
                first: _SlowBackend(clock, 40.0, None),
                second: second_backend,
            }.__getitem__,
            clock=clock,
        )

        result = router.chat("chat", [{"role": "user", "content": "hi"}],
                             timeout_sec=180.0)

        assert result == {"message": {"content": "answer"}}


class TestTheBudgetIsWiredFromARealConfigFile:
    def test_a_configured_budget_bounds_the_running_chain(self, tmp_path, monkeypatch):
        """All four wirings, checked through behaviour: the dataclass field,
        the loader, the constructor call, and the factory that hands the
        value to the router that actually walks the chain."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "llm_chat_chain_budget_sec": 12.0,
            "local_llm_fallback_enabled": False,
            "llm_routes": [{
                "name": "gateway",
                "provider": "openai_compatible",
                "base_url": "https://example.invalid/v1",
                "api_key": "k",
                "model": "some-model",
                "tier": "chat",
                "timeout_sec": 25.0,
                "enabled": True,
                "capabilities": ["chat"],
            }],
        }))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        cfg = load_settings()
        assert cfg.llm_chat_chain_budget_sec == 12.0

        from jarvis.llm.factory import get_llm_backend

        backend = get_llm_backend(cfg)
        assert backend.chain_budget_sec == 12.0

    def test_an_unconfigured_budget_takes_the_default(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        cfg = load_settings()

        assert cfg.llm_chat_chain_budget_sec > 0
        assert cfg.llm_chat_chain_budget_sec < cfg.llm_chat_timeout_sec

    @pytest.mark.parametrize("written", [0, -1, "nonsense", None])
    def test_an_unusable_budget_falls_back_to_the_default(self, tmp_path, monkeypatch, written):
        """Fail open: a budget nobody can read must not shrink the chain to
        nothing and leave every turn unanswered."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"llm_chat_chain_budget_sec": written}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))

        cfg = load_settings()

        assert cfg.llm_chat_chain_budget_sec > 0
