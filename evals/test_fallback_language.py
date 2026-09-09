"""
Canned Fallback Language Evaluations (Live)

The reply engine writes two sentences itself: the malformed-output guard and
the empty-reply backstop. They exist precisely because the model produced
nothing usable, so the prompt's language rule cannot reach them, and they are
written in English. A Piper voice that speaks German then reads an English
apology aloud, at the moment the user is already being let down.

`in_the_voices_language` renders them into the language the voice names. The
rendering is done by the fast model, so the thing worth measuring is not that
the code path runs (the unit tests cover that) but whether a small local model
actually produces the named language, and whether it leaves words of the
original behind.

Run: EVAL_JUDGE_MODEL=gemma4:e2b ./scripts/run_evals.sh test_fallback_language
"""

import json

import pytest

from conftest import requires_judge_llm
from helpers import MockConfig, call_judge_llm

from jarvis.reply.fallbacks import forget_renderings, in_the_voices_language


# The complete set the engine can emit. Rendering is cached per message, so
# each of these is asked for exactly once in a real run.
CANNED_MESSAGES = [
    pytest.param(
        "I had trouble understanding that request. Could you try rephrasing it?",
        id="malformed-guard",
    ),
    pytest.param(
        "Sorry, I had trouble processing that. Could you try again?",
        id="empty-reply-backstop",
    ),
]

# Two languages, so a pass says the mechanism follows the voice rather than
# that the model happens to know German.
VOICES = [
    pytest.param("de_DE-thorsten-medium", "German", id="german-voice"),
    pytest.param("fr_FR-siwis-medium", "French", id="french-voice"),
]


def _voice(tmp_path, name: str, language: str) -> str:
    """A Piper voice on disk: the model file and the sidecar it is read from."""
    model = tmp_path / f"{name}.onnx"
    model.write_bytes(b"not-a-real-model")
    (tmp_path / f"{name}.onnx.json").write_text(
        json.dumps({
            "audio": {"sample_rate": 22050},
            "language": {"name_english": language},
        }),
        encoding="utf-8",
    )
    return str(model)


def _detected_language(text: str) -> str:
    verdict = call_judge_llm(
        system_prompt=(
            "You identify the language a piece of text is written in. "
            "Answer with the English name of the language and nothing else "
            "(for example: German, English, French, Turkish)."
        ),
        user_prompt=f"What language is this written in?\n\n{text}",
    )
    return (verdict or "").strip().splitlines()[0].strip(" .\"'").lower() if verdict else ""


@pytest.mark.eval
class TestTheCannedFallbackReachesTheVoicesLanguage:
    @requires_judge_llm
    @pytest.mark.parametrize("message", CANNED_MESSAGES)
    @pytest.mark.parametrize("voice_name,language", VOICES)
    def test_the_message_arrives_in_the_voices_language(
        self, tmp_path, message, voice_name, language
    ):
        forget_renderings()
        cfg = MockConfig()
        cfg.tts_engine = "piper"
        cfg.tts_piper_model_path = _voice(tmp_path, voice_name, language)

        rendered = in_the_voices_language(cfg, message)

        assert rendered != message, (
            f"The {language} voice was left with the English original, so either "
            f"the fast model failed or the rendering was refused.\nGot: {rendered}"
        )
        detected = _detected_language(rendered)
        assert language.lower() in detected, (
            f"A {language} voice must not read out {detected!r}.\n"
            f"Rendered: {rendered}"
        )

    @requires_judge_llm
    @pytest.mark.parametrize("message", CANNED_MESSAGES)
    def test_nothing_of_the_original_is_left_behind(self, tmp_path, message):
        """Half-translated is worse than untranslated.

        A small model asked to translate will sometimes carry a word of the
        source through. One English word inside a German sentence is read
        aloud as an English word, which sounds like a fault rather than a
        message.
        """
        forget_renderings()
        cfg = MockConfig()
        cfg.tts_engine = "piper"
        cfg.tts_piper_model_path = _voice(tmp_path, "de_DE-thorsten-medium", "German")

        rendered = in_the_voices_language(cfg, message)

        verdict = call_judge_llm(
            system_prompt=(
                "You check translations for untranslated leftovers. Answer "
                "CLEAN if every word belongs to the target language, or "
                "LEFTOVER followed by the offending words if any word was "
                "left in the source language. Answer with nothing else."
            ),
            user_prompt=(
                f"Target language: German\nSource: {message}\n"
                f"Translation: {rendered}"
            ),
        )

        assert "clean" in (verdict or "").strip().lower(), (
            f"The judge found untranslated words: {verdict}\nRendered: {rendered}"
        )
