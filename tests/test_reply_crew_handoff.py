"""Automatic local-to-crew handoff behaviour in the reply engine."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from jarvis.runtime.telemetry import get_recorder
from jarvis.tools.types import ToolExecutionResult


def _reply(text: str) -> dict:
    return {"message": {"role": "assistant", "content": text}}


@pytest.fixture(autouse=True)
def _clean_turn_recorder():
    recorder = get_recorder()
    recorder.abandon()
    recorder.clear()
    recorder.use_journal(None)
    yield
    recorder.abandon()
    recorder.clear()
    recorder.use_journal(None)


def _crew_ready(mock_config) -> None:
    mock_config.planner_enabled = False
    mock_config.memory_digest_enabled = False
    mock_config.telegram_bot_token = "token"
    mock_config.crew_telegram_chat_id = "-100123"
    mock_config.crew_handoff_enabled = True


def _begin_elapsed_turn(elapsed_sec: float):
    trace = get_recorder().begin(source="text")
    trace._origin = time.perf_counter() - elapsed_sec
    return trace


from src.jarvis.tools.builtin.ask_crew import spoken_acknowledgement


def _delegated_result() -> ToolExecutionResult:
    return ToolExecutionResult(
        success=True,
        reply_text=(
            "Delegated to jarvis. They will post the result in the crew "
            "channel or the shared vault once done."
        ),
    )


def test_a_tool_heavy_turn_hands_off_at_three_seconds(
    mock_config, db, dialogue_memory,
):
    """No local prose and real tools remaining is not close to done."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    trace = _begin_elapsed_turn(3.1)
    local_chat = MagicMock(return_value=_reply("local answer"))
    tool_runner = MagicMock(return_value=_delegated_result())

    with patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(engine_mod, "chat_with_messages", local_chat), \
         patch.object(engine_mod, "run_tool_with_retries", tool_runner):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="investigate this in depth",
            dialogue_memory=dialogue_memory,
            quiet=True,
        )

    assert result == spoken_acknowledgement("jarvis")
    local_chat.assert_not_called()
    assert tool_runner.call_args.kwargs["tool_name"] == "askCrew"
    assert tool_runner.call_args.kwargs["tool_args"] == {
        "agent": "jarvis",
        "task": "investigate this in depth",
    }
    handoff = [stage for stage in trace.stages if stage.name == "crew_handoff"]
    assert len(handoff) == 1
    assert handoff[0].start_ms >= 3000.0


def test_a_reply_only_turn_may_finish_locally_between_three_and_five_seconds(
    mock_config, db, dialogue_memory,
):
    """A positive no-tool route is the explicit close-to-done signal."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    _begin_elapsed_turn(3.1)
    tool_runner = MagicMock(return_value=_delegated_result())

    def local_chat(**kwargs):
        assert 0.0 < kwargs["timeout_sec"] <= 2.0
        return _reply("local answer")

    with patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(engine_mod, "chat_with_messages", side_effect=local_chat), \
         patch.object(engine_mod, "run_tool_with_retries", tool_runner):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="answer briefly",
            dialogue_memory=dialogue_memory,
            quiet=True,
        )

    assert result == "local answer"
    tool_runner.assert_not_called()


def test_a_local_answer_finishing_after_five_seconds_is_discarded(
    mock_config, db, dialogue_memory,
):
    """The hard cutoff owns the turn even if a local answer arrives just after it."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    _begin_elapsed_turn(4.9)
    tool_runner = MagicMock(return_value=_delegated_result())

    def late_local_chat(**kwargs):
        assert 0.0 < kwargs["timeout_sec"] <= 0.2
        time.sleep(0.15)
        return _reply("fully formed local answer")

    with patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(engine_mod, "chat_with_messages", side_effect=late_local_chat), \
         patch.object(engine_mod, "run_tool_with_retries", tool_runner):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="answer briefly",
            dialogue_memory=dialogue_memory,
            quiet=True,
        )

    assert result == spoken_acknowledgement("jarvis")
    assert "fully formed local answer" not in result
    assert tool_runner.call_count == 1


def test_a_complete_local_answer_arriving_before_five_seconds_wins_the_turn(
    mock_config, db, dialogue_memory,
):
    """Natural-language content is done, even if the router exposed tools."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    _begin_elapsed_turn(2.9)
    tool_runner = MagicMock(return_value=_delegated_result())

    def completed_local_chat(**kwargs):
        assert 0.0 < kwargs["timeout_sec"] <= 0.2
        time.sleep(0.15)
        return _reply("complete local answer")

    with patch.object(
        engine_mod, "select_tools", return_value=["webSearch", "stop"]
    ), patch.object(
        engine_mod, "chat_with_messages", side_effect=completed_local_chat
    ), patch.object(
        engine_mod, "run_tool_with_retries", tool_runner
    ):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="answer with what you know",
            dialogue_memory=dialogue_memory,
            quiet=True,
        )

    assert result == "complete local answer"
    tool_runner.assert_not_called()


def test_automatic_handoff_is_off_by_default(
    mock_config, db, dialogue_memory,
):
    """crew_handoff_enabled defaults False: a slow turn just keeps running locally.

    Even with a fully configured crew transport, the deadline must not fire
    askCrew on its own until this flag is turned on: askCrew's confirmation
    wait is not yet bounded to the deadline, so an unattended escalation can
    sit on the full confirmation timeout before falling through to a refusal
    instead of an answer.
    """
    from jarvis.reply import engine as engine_mod

    mock_config.planner_enabled = False
    mock_config.memory_digest_enabled = False
    mock_config.telegram_bot_token = "token"
    mock_config.crew_telegram_chat_id = "-100123"
    assert mock_config.crew_handoff_enabled is False

    _begin_elapsed_turn(9.0)  # past both the 3s checkpoint and the 5s cutoff
    tool_runner = MagicMock(return_value=_delegated_result())

    with patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(
             engine_mod, "chat_with_messages", return_value=_reply("local answer"),
         ), \
         patch.object(engine_mod, "run_tool_with_retries", tool_runner):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="investigate this in depth",
            dialogue_memory=dialogue_memory,
            quiet=True,
        )

    assert result == "local answer"
    assert not any(
        call.kwargs.get("tool_name") == "askCrew" for call in tool_runner.call_args_list
    )


def test_crew_handoff_enabled_defaults_false_from_a_real_config_file(
    tmp_path, monkeypatch,
):
    """load_settings() must wire the flag, not just accept it as a stray key."""
    import json

    from jarvis.config import load_settings

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"telegram_bot_token": "tok", "crew_telegram_chat_id": "-100123"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    assert load_settings().crew_handoff_enabled is False

    config_path.write_text(
        json.dumps({"crew_handoff_enabled": True}), encoding="utf-8",
    )
    assert load_settings().crew_handoff_enabled is True


def test_router_and_planner_share_the_three_second_checkpoint(
    mock_config, db, dialogue_memory,
):
    """Pre-flight model calls cannot each claim a fresh full timeout."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    mock_config.planner_enabled = True
    _begin_elapsed_turn(1.0)
    router_timeouts: list[float] = []
    planner_timeouts: list[float] = []

    def route(**kwargs):
        router_timeouts.append(kwargs["llm_timeout_sec"])
        return ["webSearch", "stop"]

    def plan(**kwargs):
        planner_timeouts.append(kwargs["timeout_sec"])
        return ["Reply to the user."]

    with patch.object(engine_mod, "select_tools", side_effect=route), \
         patch.object(engine_mod, "plan_query", side_effect=plan), \
         patch.object(
             engine_mod,
             "chat_with_messages",
             return_value=_reply("local answer"),
         ):
        result = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="explain this reasonably detailed question without guessing anything",
            dialogue_memory=dialogue_memory,
            quiet=True,
        )

    assert result == "local answer"
    assert router_timeouts and 0.0 < router_timeouts[0] <= 2.0
    assert planner_timeouts and 0.0 < planner_timeouts[0] <= 2.0


def test_the_spoken_handoff_carries_no_instructions_meant_for_the_model(
    mock_config, db, dialogue_memory,
):
    """The tool result is written for a model that will rewrite it. The
    automatic handoff delivers its acknowledgement straight to the
    speakers, so it needs words written for the person listening."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    _begin_elapsed_turn(3.1)
    tool_runner = MagicMock(return_value=_delegated_result())

    with patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(engine_mod, "chat_with_messages",
                      MagicMock(return_value=_reply("local answer"))), \
         patch.object(engine_mod, "run_tool_with_retries", tool_runner):
        result = engine_mod.run_reply_engine(
            db=db, cfg=mock_config, tts=None,
            text="investigate this in depth",
            dialogue_memory=dialogue_memory, quiet=True,
        )

    lowered = result.lower()
    assert "do not say" not in lowered
    assert "tell the user" not in lowered


def test_the_spoken_handoff_says_the_answer_will_not_arrive_here(
    mock_config, db, dialogue_memory,
):
    """A user told only that the crew is working on it waits in this
    conversation for something that arrives somewhere else entirely."""
    from jarvis.reply import engine as engine_mod

    _crew_ready(mock_config)
    _begin_elapsed_turn(3.1)

    with patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(engine_mod, "chat_with_messages",
                      MagicMock(return_value=_reply("local answer"))), \
         patch.object(engine_mod, "run_tool_with_retries",
                      MagicMock(return_value=_delegated_result())):
        result = engine_mod.run_reply_engine(
            db=db, cfg=mock_config, tts=None,
            text="investigate this in depth",
            dialogue_memory=dialogue_memory, quiet=True,
        )

    lowered = result.lower()
    assert "not here" in lowered or "not in this conversation" in lowered
    assert "vault" in lowered
