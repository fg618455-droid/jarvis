"""A caller's token cap is about the answer, not about the thinking.

Every fast-tier caller asks for a short answer and pays for it with a
small ``max_tokens``: the tool router wants a comma-separated list, the
enrichment extractor wants a few keywords. A model with a reasoning
channel spends that budget thinking instead, stops at the cap, and hands
back an empty ``content`` with the whole allowance burned in
``reasoning_content``.

Measured on ``openai/gpt-oss-20b`` at the router's cap: ``finish_reason:
length``, ``content: ''``, 50 completion tokens, all of them reasoning.
The route then looked empty, the chain fell through to the next route
running the same model family, and the router fell open to the entire
tool catalogue on every single turn.

The fix belongs under every fast caller rather than at one call site, so
these tests drive it through the backend.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _response(*, content="", reasoning=None, finish_reason="stop"):
    """One OpenAI-shape chat completion, as a stubbed requests response."""
    from unittest.mock import MagicMock

    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }
    resp.raise_for_status = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=None)
    return resp


def _caps(mock_post):
    """The ``max_tokens`` asked for on each call, in order."""
    return [call.kwargs["json"].get("max_tokens") for call in mock_post.call_args_list]


class TestThinkingDoesNotEatTheAnswer:
    @patch("jarvis.llm.requests.post")
    def test_a_model_that_thinks_past_the_cap_still_answers(self, mock_post):
        """The measured failure: the whole cap goes to reasoning and the
        answer never gets written. Asking again with room for both is the
        difference between a working router and one that gives up."""
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.side_effect = [
            _response(
                content="",
                reasoning="We need to respond with a comma-separated list of",
                finish_reason="length",
            ),
            _response(content="getWeather, webSearch"),
        ]
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        answer = backend.direct("thinker", "sys", "user", max_tokens=50)

        assert answer == "getWeather, webSearch"

    @patch("jarvis.llm.requests.post")
    def test_the_second_attempt_leaves_room_for_the_thinking(self, mock_post):
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.side_effect = [
            _response(content="", reasoning="thinking...", finish_reason="length"),
            _response(content="none"),
        ]
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")
        backend.direct("thinker", "sys", "user", max_tokens=50)

        asked_first, asked_again = _caps(mock_post)
        assert asked_first == 50
        assert asked_again > 50

    @patch("jarvis.llm.requests.post")
    def test_a_model_known_to_think_is_given_room_from_the_start(self, mock_post):
        """Paying the extra round trip once per model is a warm-up cost.
        Paying it on every turn is a latency bug: the fast tier exists to
        answer inside four seconds."""
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.side_effect = [
            _response(content="", reasoning="thinking...", finish_reason="length"),
            _response(content="first answer"),
            _response(content="second answer"),
        ]
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")
        backend.direct("thinker", "sys", "user", max_tokens=50)

        assert backend.direct("thinker", "sys", "user", max_tokens=50) == "second answer"
        assert len(mock_post.call_args_list) == 3
        assert _caps(mock_post)[2] > 50

    @patch("jarvis.llm.requests.post")
    def test_what_one_model_needs_is_not_assumed_of_another(self, mock_post):
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.side_effect = [
            _response(content="", reasoning="thinking...", finish_reason="length"),
            _response(content="answer"),
            _response(content="plain answer"),
        ]
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")
        backend.direct("thinker", "sys", "user", max_tokens=50)
        backend.direct("plain-model", "sys", "user", max_tokens=50)

        assert _caps(mock_post)[2] == 50


class TestTheRetryIsNarrowlyEarned:
    @patch("jarvis.llm.requests.post")
    def test_a_model_that_answers_within_the_cap_is_asked_once(self, mock_post):
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.return_value = _response(content="webSearch")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.direct("plain", "sys", "user", max_tokens=50) == "webSearch"
        assert len(mock_post.call_args_list) == 1

    @patch("jarvis.llm.requests.post")
    def test_an_empty_reply_with_no_thinking_behind_it_stays_empty(self, mock_post):
        """Nothing to recover and nothing to blame on the cap: this is an
        ordinary empty response the chain already knows how to walk past."""
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.return_value = _response(content="", finish_reason="length")
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.direct("plain", "sys", "user", max_tokens=50) is None
        assert len(mock_post.call_args_list) == 1

    @patch("jarvis.llm.requests.post")
    def test_a_caller_that_set_no_cap_is_not_second_guessed(self, mock_post):
        """Without a cap the model stopped for its own reasons, so there is
        no cap to widen and nothing to retry."""
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.return_value = _response(
            content="", reasoning="thinking...", finish_reason="length"
        )
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        backend.direct("thinker", "sys", "user")

        assert len(mock_post.call_args_list) == 1

    @patch("jarvis.llm.requests.post")
    def test_a_second_empty_reply_is_not_retried_again(self, mock_post):
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.side_effect = [
            _response(content="", reasoning="thinking...", finish_reason="length"),
            _response(content="", reasoning="still thinking...", finish_reason="length"),
        ]
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        assert backend.direct("thinker", "sys", "user", max_tokens=50) is None
        assert len(mock_post.call_args_list) == 2

    @patch("jarvis.llm.requests.post")
    def test_the_widened_cap_is_reported_once(self, mock_post):
        from jarvis.llm import OpenAICompatibleBackend

        mock_post.side_effect = [
            _response(content="", reasoning="thinking...", finish_reason="length"),
            _response(content="ok"),
        ]
        backend = OpenAICompatibleBackend("http://localhost:1234/v1")

        with patch("jarvis.llm.openai_compatible.debug_log") as logged:
            backend.direct("thinker", "sys", "user", max_tokens=50)

        assert any(
            "reasoning" in str(call.args[0]).lower()
            for call in logged.call_args_list
        )
