from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.llm.backend import LLMBackend
from jarvis.llm.runtime import LLMRuntime


class _Backend(LLMBackend):
    def __init__(self, label):
        self.label = label

    def direct(self, *args, **kwargs):
        return self.label

    def streaming(self, *args, **kwargs):
        return self.label

    def chat(self, *args, **kwargs):
        return {"message": {"content": self.label}}

    def embed(self, *args, **kwargs):
        return None

    def list_models(self, *args, **kwargs):
        return []


def _settings(label):
    return SimpleNamespace(
        label=label,
        llm_routes=[{"name": label}],
        chat_backend_override="auto",
        crew_chat_agent="",
        crew_api_url="",
        crew_api_key="",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_chat_model="private",
        low_power_mode=False,
    )


def test_inflight_snapshot_keeps_old_backend_and_next_turn_gets_new_one():
    runtime = LLMRuntime(builder=lambda settings: _Backend(settings.label))
    old_turn = runtime.install(_settings("old"))

    new_generation = runtime.reconfigure(lambda: _settings("new"), lambda: None)

    assert old_turn.backend.direct() == "old"
    assert runtime.snapshot().backend.direct() == "new"
    assert new_generation.number == old_turn.number + 1


def test_failed_build_keeps_generation_and_calls_rollback():
    rolled_back = []

    def builder(settings):
        if settings.label == "broken":
            raise ValueError("invalid route")
        return _Backend(settings.label)

    runtime = LLMRuntime(builder=builder)
    original = runtime.install(_settings("working"))

    with pytest.raises(ValueError):
        runtime.reconfigure(
            lambda: _settings("broken"),
            lambda: rolled_back.append(True),
        )

    assert rolled_back == [True]
    assert runtime.snapshot() is original


def test_same_settings_reuses_active_generation():
    calls = []
    runtime = LLMRuntime(builder=lambda settings: calls.append(settings.label) or _Backend(settings.label))
    settings = _settings("same")

    first = runtime.install(settings)
    second = runtime.install(settings)

    assert second is first
    assert calls == ["same"]


@pytest.mark.parametrize("method", ["direct", "streaming", "chat"])
def test_parallel_turn_keeps_generation_through_reconfiguration(method):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    runtime = LLMRuntime(builder=lambda settings: _Backend(settings.label))
    settings = _settings("old")
    first = runtime.install(settings)
    settings.label = "mutated by caller"
    assert first.settings.label == "old"

    def turn():
        generation = runtime.snapshot()
        entered.set()
        assert release.wait(5)
        return getattr(generation.backend, method)()

    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(turn)
            assert entered.wait(2)
            runtime.reconfigure(lambda: _settings("new"), lambda: None)
            release.set()
            expected_old = {"message": {"content": "old"}} if method == "chat" else "old"
            expected_new = {"message": {"content": "new"}} if method == "chat" else "new"
            assert pending.result(timeout=2) == expected_old
            assert getattr(runtime.snapshot().backend, method)() == expected_new
    finally:
        release.set()
        runtime.shutdown()


def test_environment_credential_rotation_builds_new_adapter(monkeypatch):
    import os
    from jarvis.llm.route import Route, RoutedBackend
    from jarvis.llm.tiers import Tier

    settings = _settings("cloud")
    settings.llm_routes = [{"api_key_env": "JARVIS_TEST_ROTATION_KEY"}]
    route = Route("cloud", "openai_compatible", "https://example.com/v1", "", "model", Tier.CHAT, 10.0,
                  api_key_env="JARVIS_TEST_ROTATION_KEY")
    def builder(settings):
        return RoutedBackend([route], backend_factory=lambda route: _Backend(os.environ[route.api_key_env]))
    runtime = LLMRuntime(builder=builder)
    try:
        monkeypatch.setenv("JARVIS_TEST_ROTATION_KEY", "first")
        old = runtime.install(settings)
        monkeypatch.setenv("JARVIS_TEST_ROTATION_KEY", "second")
        new = runtime.install(settings)
        assert new.number == old.number + 1
        assert old.backend._backend(route).direct() == "first"
        assert new.backend._backend(route).direct() == "second"
    finally:
        runtime.shutdown()
