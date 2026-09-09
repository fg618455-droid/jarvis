"""The exchange itself: what was said, and typing something back.

A typed turn goes through the same reply engine as a spoken one, tools,
memory, and the security gate included. It is the same assistant, reached
through a keyboard instead of a microphone, which makes it the honest way
to try something without speaking out loud.
"""

from __future__ import annotations

import sys

from flask import Blueprint, Response, jsonify, request

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.listening.conversation_mode import (
    conversation_mode_active,
    set_conversation_mode,
)
from jarvis.runtime import Phase, get_recorder, get_runtime_state, set_phase, set_phase_if


bp = Blueprint("conversation", __name__, url_prefix="/api")

MAX_TEXT_CHARS = 4000


@bp.route("/conversation")
def conversation() -> Response:
    """Recent turns with their transcripts, tools, replies, and timings."""
    try:
        limit = int(request.args.get("limit", 30))
    except (TypeError, ValueError):
        limit = 30
    return jsonify({
        "turns": get_recorder().history(limit=max(1, min(limit, 200))),
        "discarded": get_runtime_state().snapshot()["discarded"],
        "conversation_mode": conversation_mode_active(),
    })


@bp.route("/conversation/mode", methods=["POST"])
def conversation_mode() -> Response:
    """Hold the follow-up window open, or let it close again.

    While it is open every utterance counts as addressed to Jarvis, so a
    question needs no wake word. Saying so out loud ends it too: the intent
    judge decides what counts as asking Jarvis to stop, which is why no
    list of stop words appears anywhere near this.

    A request that reaches no listener answers 409 rather than reporting a
    mode it did not set. Standalone there is no voice loop to hold open.
    """
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled", False))

    reached = set_conversation_mode(enabled)
    active = conversation_mode_active()
    debug_log(
        f"conversation mode {'on' if enabled else 'off'} from the control centre, "
        f"{'reached the listener' if reached else 'nothing was listening'}",
        "webui",
    )
    if not reached:
        return jsonify(
            active=active,
            error="nothing is listening, so there is no conversation to hold open",
        ), 409
    return jsonify(active=active)


@bp.route("/chat", methods=["POST"])
def chat() -> Response:
    """Put text through the reply engine and return what came back."""
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    speak = bool(payload.get("speak", False))

    if not text:
        return jsonify(error="text is required"), 400
    if len(text) > MAX_TEXT_CHARS:
        return jsonify(error=f"text exceeds {MAX_TEXT_CHARS} characters"), 400

    # One turn at a time, across every way in. Voice, the desktop chat window
    # and this endpoint all run the reply engine against the same dialogue
    # memory, and the daemon owns the lock that serialises them. A person
    # typing cannot outrun a turn, so a busy lock is refused rather than
    # queued.
    from jarvis.daemon import chat_query_lock

    if not chat_query_lock().acquire(blocking=False):
        return jsonify(error="a reply is already being written"), 409

    recorder = get_recorder()
    state = get_runtime_state()
    trace = recorder.begin(source="text")
    trace.transcript = text
    set_phase(Phase.THINKING)

    database = None
    try:
        from jarvis.memory.db import Database
        from jarvis.reply.engine import run_reply_engine

        cfg = load_settings()
        database = Database(cfg.db_path, cfg.sqlite_vss_path)
        reply = run_reply_engine(database, cfg, None, text, _dialogue_memory(cfg))
    except Exception as exc:
        debug_log(f"typed turn failed: {exc}", "webui")
        state.record_error(f"typed turn failed: {exc}")
        recorder.finish(error=str(exc))
        set_phase_if(Phase.THINKING, Phase.IDLE)
        return jsonify(error=str(exc)), 500
    finally:
        if database is not None:
            database.close()
        chat_query_lock().release()

    record = recorder.finish(reply=reply)
    set_phase_if(Phase.THINKING, Phase.IDLE)

    if speak and reply:
        _speak(reply)

    return jsonify({"reply": reply, "turn": record})


def _dialogue_memory(cfg):
    """The conversation a typed turn joins.

    With the daemon running that is the spoken conversation, so a question
    typed after one spoken aloud still has its context. Standalone, the
    control centre keeps a conversation of its own for the same reason.

    The daemon is looked up rather than imported: if it is running in this
    process its module is already loaded, and if it is not there is no
    spoken conversation to join.
    """
    daemon = sys.modules.get("jarvis.daemon")
    running = daemon.get_dialogue_memory() if daemon is not None else None
    if running is not None:
        return running

    global _standalone_memory
    if _standalone_memory is None:
        from jarvis.memory.conversation import DialogueMemory

        _standalone_memory = DialogueMemory(
            inactivity_timeout=cfg.dialogue_memory_timeout,
            max_interactions=20,
        )
    return _standalone_memory


_standalone_memory = None


def _speak(reply: str) -> None:
    """Say a typed reply aloud through the running engine, if there is one."""
    daemon = sys.modules.get("jarvis.daemon")
    engine = daemon.get_tts_engine() if daemon is not None else None
    if engine is None or not getattr(engine, "enabled", False):
        debug_log("typed turn asked for speech with no engine running", "webui")
        return
    try:
        engine.speak(reply)
    except Exception as exc:
        debug_log(f"speaking a typed reply failed: {exc}", "webui")
