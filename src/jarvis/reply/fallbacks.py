"""🗣️ The canned replies the model never writes, in the voice's language.

Almost everything the assistant says is written by the model, which the
reply prompt tells which language to answer in. The malformed-output guard
is the exception: it fires precisely because the model produced nothing
usable, so its message is ours, and ours is written in English. A German
voice then reads an English apology aloud.

The language is never guessed. It comes from the configured voice's own
metadata, the same source the reply prompt uses, so a Dutch or Turkish
voice needs no code change and no language appears anywhere in this file.
With no voice to name a language, text chat included, the English original
stands: a wrong language is worse than a familiar one.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from jarvis.debug import debug_log
from jarvis.llm import get_llm_backend
from jarvis.llm.tiers import Tier, resolve_model
from jarvis.output.tts import resolve_kokoro_voice_language, resolve_voice_language


# One rendering per language per message, for the lifetime of the process.
# These messages are a closed set, so the model is asked once and the
# failure path is never on the critical path twice.
_renderings: dict[tuple[str, str], str] = {}
_lock = threading.Lock()

# The guard already fired once by the time this runs, so the wait is on top
# of a turn that has gone wrong. Long enough to survive a cold model, since
# the alternative is the English original rather than a better sentence, and
# short enough that the pause stays a pause rather than a hang.
RENDER_TIMEOUT_SEC = 20.0


def in_the_voices_language(
    cfg,
    message: str,
    *,
    render: Optional[Callable[[object, str, str], Optional[str]]] = None,
) -> str:
    """Return ``message`` in the language the configured voice speaks.

    Returns it unchanged when no voice names a language, and when the
    rendering fails for any reason: an English sentence the user can read
    beats a half-translated one they cannot.
    """
    language = _voice_language(cfg)
    if not language:
        return message

    key = (language, message)
    with _lock:
        cached = _renderings.get(key)
    if cached:
        return cached

    try:
        rendered = (render or _ask_the_fast_model)(cfg, language, message)
    except Exception as exc:
        debug_log(f"fallback rendering failed, keeping the original: {exc}", "reply")
        return message

    rendered = (rendered or "").strip()
    if not rendered:
        return message

    with _lock:
        _renderings[key] = rendered
    debug_log(f"canned fallback rendered into {language}", "reply")
    return rendered


def forget_renderings() -> None:
    """Drop what has been rendered so far. Used when the voice changes."""
    with _lock:
        _renderings.clear()


def _voice_language(cfg) -> Optional[str]:
    engine = getattr(cfg, "tts_engine", "piper")
    if engine == "piper":
        return resolve_voice_language(getattr(cfg, "tts_piper_model_path", None))
    if engine == "kokoro":
        return resolve_kokoro_voice_language(getattr(cfg, "tts_kokoro_voice", None))
    return None


def _ask_the_fast_model(cfg, language: str, message: str) -> Optional[str]:
    """Translate one short sentence, and nothing else.

    The fast tier because this is a plain rendering, not reasoning, and it
    is already warm. The instruction forbids commentary because anything
    beyond the sentence itself would be spoken aloud along with it.
    """
    response = get_llm_backend(cfg).chat(
        resolve_model(cfg, Tier.FAST),
        [
            {
                "role": "system",
                "content": (
                    f"Translate the user's message into {language}. Every word "
                    f"of your reply must be {language}: leave nothing in the "
                    "original language. Reply with the translation only, and "
                    "with no quotes, no commentary, and no notes."
                ),
            },
            {"role": "user", "content": message},
        ],
        timeout_sec=RENDER_TIMEOUT_SEC,
        # A fixed set of sentences deserves a fixed rendering. Sampling
        # makes the wording a lottery whose winner is then cached for the
        # rest of the run.
        extra_options={"temperature": 0.0},
    )
    if not response:
        return None
    return (response.get("message") or {}).get("content")
