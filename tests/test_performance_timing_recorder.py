"""Behaviour guard for the live LLM timing recorder."""

from types import SimpleNamespace
from unittest.mock import patch

from tests.performance.timing_recorder import TimingRecorder


def test_recorder_sees_the_reply_engines_backend_chat_call():
    """The performance harness records the current backend abstraction."""
    from jarvis.reply import engine as engine_mod

    cfg = SimpleNamespace(llm_chat_model="local-model")

    class Backend:
        def chat(self, model, messages, **kwargs):
            return {"message": {"content": "ready"}}

    with patch.object(engine_mod, "get_llm_backend", return_value=Backend()):
        with TimingRecorder() as recorder:
            engine_mod.chat_with_messages(
                cfg,
                [{"role": "user", "content": "hello"}],
            )

    assert len(recorder.calls) == 1
    assert recorder.calls[0].model == "local-model"
    assert recorder.calls[0].prompt_chars == len("hello")


def test_pipeline_fixture_sets_the_effective_chat_model(monkeypatch):
    """The report label and the model sent to the backend must agree."""
    from tests.performance import test_pipeline_timings as timings

    monkeypatch.setattr(timings, "PERF_MODEL", "chosen-model")
    monkeypatch.setattr(
        timings, "PERF_FAST_MODEL", "chosen-fast-model", raising=False
    )

    cfg = timings._make_cfg()

    assert cfg.llm_chat_model == "chosen-model"
    assert cfg.fast_model == "chosen-fast-model"


def test_recorder_sees_routed_direct_calls_used_by_preflight_contexts():
    """Router, planner, and enrichment all enter through routed direct()."""
    from jarvis.llm.route import RoutedBackend

    backend = RoutedBackend([])

    with TimingRecorder() as recorder:
        backend.direct("fast-model", "system", "query")

    assert len(recorder.calls) == 1
    assert recorder.calls[0].model == "fast-model"
    assert recorder.calls[0].prompt_chars == len("systemquery")
