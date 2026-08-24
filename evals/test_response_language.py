"""
Response Language Evaluations (Live)

The prompt is written in English. Left to itself a small model answers in
English no matter what the user wrote, which makes the assistant unusable
for anyone who does not speak it. Two rules keep that from happening:

- a Piper voice whose metadata names a language pins replies to it, so the
  spoken output and the text agree
- with no such voice (speech off, a non-Piper engine, text chat, or a voice
  path that points at nothing) the model is told to mirror the user

The second rule is what these evals cover. It is the one that carries text
chat, where there is no voice to derive a language from at all.

Run: EVAL_JUDGE_MODEL=qwen2.5:7b ./scripts/run_evals.sh test_response_language
"""

import pytest

from conftest import requires_judge_llm
from helpers import assert_not_fallback_reply, call_judge_llm


# Queries a German speaker would actually type into the chat window. Each is
# short: brevity is where the drift to English is worst, because there is
# little user text to anchor the model.
#
# All three answer from the model alone. Queries that pull in tools or the
# memory digest are deliberately excluded: those inject English text into the
# context and swamp the language rule, so a failure would say nothing about
# whether the rule works.
GERMAN_QUERIES = [
    pytest.param("Hallo, wie geht es dir?", id="greeting"),
    pytest.param("Was kannst du alles?", id="capabilities"),
    pytest.param("Wie fühlst du dich heute?", id="small-talk"),
]

# A second language, so the rule is shown to be about mirroring the user
# rather than about German in particular.
OTHER_LANGUAGE_QUERIES = [
    pytest.param("Bonjour, comment vas-tu ?", "French", id="french"),
    pytest.param("Merhaba, nasılsın?", "Turkish", id="turkish"),
]


def _detected_language(text: str) -> str:
    """Ask the judge what language a reply is written in.

    A word list would be a hardcoded language pattern, which this codebase
    avoids; the judge generalises to any language the assistant might meet.
    """
    verdict = call_judge_llm(
        system_prompt=(
            "You identify the language a piece of text is written in. "
            "Answer with the English name of the language and nothing else "
            "(for example: German, English, French, Turkish)."
        ),
        user_prompt=f"What language is this written in?\n\n{text}",
    )
    return (verdict or "").strip().splitlines()[0].strip(" .\"'").lower() if verdict else ""


def _assert_reply_language(response, expected: str, query: str) -> None:
    assert response, f"No reply produced for {query!r}"
    assert_not_fallback_reply(response, context=f"query={query!r}")

    detected = _detected_language(response)
    assert expected.lower() in detected, (
        f"Reply to {query!r} should be in {expected}, judge said {detected!r}.\n"
        f"Reply: {response}"
    )


class TestReplyMirrorsTheUsersLanguage:
    """With no voice naming a language, the reply follows the user."""

    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query", GERMAN_QUERIES)
    def test_german_query_gets_german_reply(
        self, query, mock_config, eval_db, eval_dialogue_memory,
    ):
        """The regression Felix hit: German in, English out.

        ``mock_config`` has ``tts_enabled=False`` and no Piper model path,
        which is exactly the shape text chat runs in, and exactly the shape
        that used to produce no language rule at all.
        """
        from jarvis.reply.engine import run_reply_engine

        response = run_reply_engine(
            db=eval_db,
            cfg=mock_config,
            tts=None,
            text=query,
            dialogue_memory=eval_dialogue_memory,
        )

        print(f"\n🗣️ Query: {query}\n💬 Reply: {response}")
        _assert_reply_language(response, "German", query)

    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query,expected", OTHER_LANGUAGE_QUERIES)
    def test_reply_follows_whatever_language_was_used(
        self, query, expected, mock_config, eval_db, eval_dialogue_memory,
    ):
        """Mirroring is general, not a German special case."""
        from jarvis.reply.engine import run_reply_engine

        response = run_reply_engine(
            db=eval_db,
            cfg=mock_config,
            tts=None,
            text=query,
            dialogue_memory=eval_dialogue_memory,
        )

        print(f"\n🗣️ Query: {query}\n💬 Reply: {response}")
        _assert_reply_language(response, expected, query)

    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query", GERMAN_QUERIES)
    def test_german_query_survives_an_english_conversation(
        self, query, mock_config, eval_db, eval_dialogue_memory,
    ):
        """The drift case, and the one that bites in practice.

        A clean German query on an empty history is easy: the model mirrors
        it unprompted. What breaks is a conversation that has already gone
        English, because every prior turn in the hot window then pulls the
        next reply that way too. One English answer is enough to start it,
        and the voice path produces that as readily as the chat window.
        """
        from jarvis.reply.engine import run_reply_engine

        for user_text, assistant_text in [
            ("what can you do?", "I can look things up, remember what you tell me, and keep track of your day."),
            ("nice, thanks", "Happy to help. Ask away whenever you like."),
        ]:
            eval_dialogue_memory.add_interaction(user_text, assistant_text)

        response = run_reply_engine(
            db=eval_db,
            cfg=mock_config,
            tts=None,
            text=query,
            dialogue_memory=eval_dialogue_memory,
        )

        print(f"\n🗣️ Query (after English history): {query}\n💬 Reply: {response}")
        _assert_reply_language(response, "German", query)

    @pytest.mark.eval
    @requires_judge_llm
    def test_a_named_voice_still_wins_over_mirroring(
        self, tmp_path, mock_config, eval_db, eval_dialogue_memory,
    ):
        """A German voice keeps German replies even for an English query.

        This is the spoken-output contract: the voice can only pronounce one
        language, so the reply has to be in it regardless of the user.
        """
        import json

        from jarvis.reply.engine import run_reply_engine

        model = tmp_path / "de_DE-thorsten-medium.onnx"
        model.write_bytes(b"not-a-real-model")
        (tmp_path / "de_DE-thorsten-medium.onnx.json").write_text(
            json.dumps({"language": {"code": "de_DE", "name_english": "German"}}),
            encoding="utf-8",
        )
        mock_config.tts_engine = "piper"
        mock_config.tts_piper_model_path = str(model)

        query = "Tell me something interesting."
        response = run_reply_engine(
            db=eval_db,
            cfg=mock_config,
            tts=None,
            text=query,
            dialogue_memory=eval_dialogue_memory,
        )

        print(f"\n🗣️ Query: {query}\n💬 Reply: {response}")
        _assert_reply_language(response, "German", query)

    @pytest.mark.eval
    @requires_judge_llm
    def test_a_kokoro_voice_still_wins_over_mirroring(
        self, mock_config, eval_db, eval_dialogue_memory,
    ):
        """The same contract as a named Piper voice, for Kokoro's voice-name
        scheme rather than a metadata sidecar: a Japanese Kokoro voice keeps
        Japanese replies even for an English query."""
        from jarvis.reply.engine import run_reply_engine

        mock_config.tts_engine = "kokoro"
        mock_config.tts_kokoro_voice = "jf_alpha"

        query = "Tell me something interesting."
        response = run_reply_engine(
            db=eval_db,
            cfg=mock_config,
            tts=None,
            text=query,
            dialogue_memory=eval_dialogue_memory,
        )

        print(f"\n🗣️ Query: {query}\n💬 Reply: {response}")
        _assert_reply_language(response, "Japanese", query)
