"""The answer on screen while it is still being written.

The reply engine already hands finished sentences to the speech path as they
arrive, so the wait ends at the first spoken word rather than at the last
written one. A reader watching the control centre is waiting on exactly the
same thing, and a page that showed the answer only once the whole turn was
over would make them wait longer than a listener does.

So the engine asks its backend for the text as it is written whether or not
anything is going to speak it, and publishes each piece. Speech is what it
additionally does when someone asked for sound.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.runtime import get_event_bus, get_recorder


def _mock_response(content, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"message": message}


def deltas(subscription, timeout: float = 1.0) -> list[str]:
    """Every piece of reply a watcher saw, in order."""
    pieces = []
    for event in subscription.listen(timeout=timeout):
        if event is None:
            break
        if event["kind"] == "reply":
            pieces.append(event["data"]["delta"])
    return pieces


@pytest.fixture(autouse=True)
def _hermetic():
    """Keep the engine off the network, and off the next test's turn."""
    get_recorder().abandon()
    with patch("jarvis.reply.engine.plan_query", return_value=[]), \
         patch("jarvis.reply.engine.select_tools", return_value=["stop"]), \
         patch("jarvis.reply.engine.extract_search_params_for_memory",
               return_value={"keywords": []}):
        yield
    get_recorder().abandon()


class TestTheReplyIsPublishedAsItIsWritten:
    def test_a_watcher_sees_the_answer_grow(self, mock_config, db, dialogue_memory):
        from jarvis.reply.engine import run_reply_engine

        def mock_chat(*args, on_token=None, **kwargs):
            on_token("Das Wetter ")
            on_token("ist gut.")
            return _mock_response("Das Wetter ist gut.")

        trace = get_recorder().begin(source="text")
        with get_event_bus().subscribe() as subscription, \
             patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="wie ist das wetter",
                dialogue_memory=dialogue_memory,
            )
            seen = deltas(subscription)

        assert "".join(seen) == "Das Wetter ist gut."
        assert trace.turn_id

    def test_it_is_streamed_even_when_nothing_will_speak_it(
        self, mock_config, db, dialogue_memory,
    ):
        """A typed turn has no speech path and still has a reader waiting."""
        from jarvis.reply.engine import run_reply_engine

        seen = {}

        def mock_chat(*args, on_token=None, **kwargs):
            seen["on_token"] = on_token
            return _mock_response("Hallo.")

        get_recorder().begin(source="text")
        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="hallo",
                dialogue_memory=dialogue_memory,
            )

        assert seen["on_token"] is not None

    def test_structured_output_is_never_shown(self, mock_config, db, dialogue_memory):
        """A text-shaped tool call is for the parser, on screen as in speech."""
        from jarvis.reply.engine import run_reply_engine

        def mock_chat(*args, on_token=None, **kwargs):
            on_token('{"tool": "getWeather", ')
            on_token('"args": {}}')
            return _mock_response('{"tool": "getWeather", "args": {}}')

        get_recorder().begin(source="text")
        with get_event_bus().subscribe() as subscription, \
             patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="wetter",
                dialogue_memory=dialogue_memory,
            )

            assert deltas(subscription) == []

    def test_a_watcher_that_is_not_there_costs_the_reply_nothing(
        self, mock_config, db, dialogue_memory,
    ):
        """Nobody watching is the normal case, not a degraded one."""
        from jarvis.reply.engine import run_reply_engine

        def mock_chat(*args, on_token=None, **kwargs):
            on_token("Alles gut.")
            return _mock_response("Alles gut.")

        get_recorder().begin(source="text")
        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            reply = run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="hallo",
                dialogue_memory=dialogue_memory,
            )

        assert reply == "Alles gut."
