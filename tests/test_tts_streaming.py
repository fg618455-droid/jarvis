"""Speaking a reply while the rest of it is still being written.

The wait a user feels ends when sound starts, not when the text is finished,
so the engine hands the speech path one sentence at a time. These tests pin
the three pieces that makes possible: an utterance queue where each item
carries its own callbacks, a segmenter that only releases finished sentences,
and the engine's decision about when a token stream is safe to speak.
"""

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# The utterance queue
# ---------------------------------------------------------------------------


class TestQueuedUtterancesKeepTheirOwnCallbacks:
    """Callbacks belong to the utterance, not to the engine.

    A streamed reply queues several sentences before the first has finished
    playing. Callbacks held on the engine would be overwritten by each new
    sentence, so the caller that asked to be told when *its* speech started
    would be told about someone else's, or never told at all.
    """

    def _tts(self):
        from src.jarvis.output.tts import PiperTTS

        return PiperTTS(enabled=True, model_path="/fake/model.onnx")

    def test_a_later_utterance_does_not_steal_an_earlier_callback(self):
        tts = self._tts()
        first, second = MagicMock(), MagicMock()

        tts.speak("First sentence.", completion_callback=first)
        tts.speak("Second sentence.", completion_callback=second)

        queued = [tts._q.get_nowait(), tts._q.get_nowait()]
        assert queued[0].completion_callback is first
        assert queued[1].completion_callback is second

    def test_each_utterance_carries_its_own_audio_start_callback(self):
        tts = self._tts()
        started = MagicMock()

        tts.speak("First sentence.", audio_start_callback=started)
        tts.speak("Second sentence.")

        queued = [tts._q.get_nowait(), tts._q.get_nowait()]
        assert queued[0].audio_start_callback is started
        assert queued[1].audio_start_callback is None

    def test_the_text_is_still_preprocessed_for_speech(self):
        tts = self._tts()

        tts.speak("**bold** words")

        assert tts._q.get_nowait().text == "bold words"


class TestEndOfReplyMarker:
    """A streamed reply does not know its last sentence until it ends.

    The caller closes the reply with a marker rather than guessing, so the
    "speech finished" callback runs after everything queued ahead of it.
    """

    def test_the_marker_runs_its_callback_after_the_queued_speech(self):
        from src.jarvis.output.tts import PiperTTS

        tts = PiperTTS(enabled=True, model_path="/fake/model.onnx")
        order = []

        with patch.object(PiperTTS, "_speak_once",
                          side_effect=lambda utterance: order.append(utterance.text)):
            tts.start()
            tts.speak("One.")
            tts.speak("Two.")
            tts.end_of_reply(completion_callback=lambda: order.append("done"))
            deadline = time.monotonic() + 5.0
            while order[-1:] != ["done"] and time.monotonic() < deadline:
                time.sleep(0.01)
            tts.stop()

        assert order == ["One.", "Two.", "done"]

    def test_the_marker_is_silent_when_nothing_was_spoken(self):
        from src.jarvis.output.tts import PiperTTS

        tts = PiperTTS(enabled=False)
        tts.end_of_reply(completion_callback=MagicMock())

        assert tts._q.empty()


# ---------------------------------------------------------------------------
# The segmenter
# ---------------------------------------------------------------------------


class TestSpeakableSegments:
    """Only finished sentences are released; a half-written one waits."""

    def _feed(self, chunks, flush=True):
        from jarvis.reply.speech_stream import SpeechSegmenter

        segmenter = SpeechSegmenter()
        out = []
        for chunk in chunks:
            out.extend(segmenter.feed(chunk))
        if flush:
            out.extend(segmenter.flush())
        return out

    def test_a_finished_sentence_is_released_at_once(self):
        assert self._feed(["Hallo Felix.", " Wie"], flush=False) == ["Hallo Felix."]

    def test_a_half_written_sentence_is_held_back(self):
        assert self._feed(["Das Wetter ist"], flush=False) == []

    def test_flush_releases_whatever_is_left(self):
        assert self._feed(["Das Wetter ist gut"]) == ["Das Wetter ist gut"]

    def test_a_sentence_split_across_chunks_comes_out_whole(self):
        assert self._feed(["Das Wetter ", "ist ", "gut."], flush=False) == ["Das Wetter ist gut."]

    def test_several_sentences_in_one_chunk_are_released_in_order(self):
        assert self._feed(["Erstens. Zweitens! Drittens?"], flush=False) == [
            "Erstens.", "Zweitens!", "Drittens?",
        ]

    @pytest.mark.parametrize("terminator", ["。", "！", "？", "۔", "।"])
    def test_sentence_terminators_outside_latin_script_end_a_sentence(self, terminator):
        assert self._feed([f"这是一句话{terminator}", "又"], flush=False) == [
            f"这是一句话{terminator}"
        ]

    def test_a_decimal_point_does_not_end_a_sentence(self):
        assert self._feed(["Es sind 21.5 Grad drau"], flush=False) == []

    def test_nothing_is_released_twice(self):
        from jarvis.reply.speech_stream import SpeechSegmenter

        segmenter = SpeechSegmenter()
        first = segmenter.feed("Eins. Zwei.")
        rest = segmenter.feed(" Drei.") + segmenter.flush()

        assert first == ["Eins.", "Zwei."]
        assert rest == ["Drei."]

    def test_flush_on_an_empty_stream_says_nothing(self):
        from jarvis.reply.speech_stream import SpeechSegmenter

        assert SpeechSegmenter().flush() == []

    def test_whitespace_only_output_is_never_spoken(self):
        assert self._feed(["   ", "\n\n"]) == []


class TestSegmentsWorthSpeaking:
    """Structured output is for the parser, never for the speakers."""

    @pytest.mark.parametrize("opening", ['{"name"', "[{", "```json", "```"])
    def test_a_stream_that_opens_as_structured_output_is_not_spoken(self, opening):
        from jarvis.reply.speech_stream import SpeechSegmenter

        segmenter = SpeechSegmenter()
        segmenter.feed(opening)

        assert segmenter.is_speakable is False

    def test_prose_stays_speakable(self):
        from jarvis.reply.speech_stream import SpeechSegmenter

        segmenter = SpeechSegmenter()
        segmenter.feed("Das Wetter ist gut.")

        assert segmenter.is_speakable is True

    def test_an_unspeakable_stream_releases_nothing(self):
        from jarvis.reply.speech_stream import SpeechSegmenter

        segmenter = SpeechSegmenter()

        assert segmenter.feed('{"tool": "getWeather"}') == []
        assert segmenter.flush() == []

    def test_a_brace_later_in_the_sentence_does_not_silence_the_reply(self):
        from jarvis.reply.speech_stream import SpeechSegmenter

        segmenter = SpeechSegmenter()

        assert segmenter.feed("Die Datei heisst {name}.") == ["Die Datei heisst {name}."]


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _mock_response(content, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"message": message}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep the engine off the network for these tests."""
    with patch("jarvis.reply.engine.plan_query", return_value=[]), \
         patch("jarvis.reply.engine.select_tools", return_value=["stop"]), \
         patch("jarvis.reply.engine.extract_search_params_for_memory",
               return_value={"keywords": []}):
        yield


class TestTheEngineSpeaksWhileItWrites:
    def test_a_finished_sentence_reaches_the_speech_path_before_the_reply_ends(
        self, mock_config, db, dialogue_memory,
    ):
        """The whole point: the first sentence is out before the last is written."""
        from jarvis.reply.engine import run_reply_engine

        spoken = []
        order = []

        def mock_chat(*args, on_token=None, **kwargs):
            assert on_token is not None, "engine must ask for the text as it arrives"
            on_token("Das Wetter ist gut. ")
            order.append(("first sentence spoken", list(spoken)))
            on_token("Zieh eine Jacke an.")
            return _mock_response("Das Wetter ist gut. Zieh eine Jacke an.")

        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            reply = run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="wie ist das wetter",
                dialogue_memory=dialogue_memory,
                on_speech_segment=spoken.append,
            )

        assert order[0][1] == ["Das Wetter ist gut."]
        assert spoken == ["Das Wetter ist gut.", "Zieh eine Jacke an."]
        assert reply == "Das Wetter ist gut. Zieh eine Jacke an."

    def test_the_tail_of_the_reply_is_still_spoken(
        self, mock_config, db, dialogue_memory,
    ):
        """A reply that ends without punctuation must not be swallowed."""
        from jarvis.reply.engine import run_reply_engine

        spoken = []

        def mock_chat(*args, on_token=None, **kwargs):
            on_token("Alles klar")
            return _mock_response("Alles klar")

        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="hallo",
                dialogue_memory=dialogue_memory, on_speech_segment=spoken.append,
            )

        assert spoken == ["Alles klar"]

    def test_nothing_is_spoken_twice_when_the_loop_runs_again(
        self, mock_config, db, dialogue_memory,
    ):
        """A tool turn and the answer turn are two streams, not one."""
        from jarvis.reply.engine import run_reply_engine

        spoken = []
        calls = []

        def mock_chat(*args, on_token=None, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                on_token("")
                return _mock_response("", [{
                    "id": "call_1",
                    "function": {"name": "getWeather", "arguments": {}},
                }])
            on_token("Es sind 20 Grad.")
            return _mock_response("Es sind 20 Grad.")

        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat), \
             patch("jarvis.reply.engine.select_tools",
                   return_value=["getWeather", "stop"]), \
             patch("jarvis.reply.engine.run_tool_with_retries") as tool:
            from jarvis.tools.types import ToolExecutionResult
            tool.return_value = ToolExecutionResult(success=True, reply_text="20C")
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="wetter",
                dialogue_memory=dialogue_memory, on_speech_segment=spoken.append,
            )

        assert spoken == ["Es sind 20 Grad."]

    def test_a_reply_that_opens_as_structured_output_is_never_spoken(
        self, mock_config, db, dialogue_memory,
    ):
        """Text-shaped tool calls are for the parser, not for the speakers."""
        from jarvis.reply.engine import run_reply_engine

        spoken = []

        def mock_chat(*args, on_token=None, **kwargs):
            on_token('{"tool": "getWeather", ')
            on_token('"args": {}}')
            return _mock_response('{"tool": "getWeather", "args": {}}')

        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="wetter",
                dialogue_memory=dialogue_memory, on_speech_segment=spoken.append,
            )

        assert spoken == []

    def test_without_a_listener_the_reply_is_not_streamed(
        self, mock_config, db, dialogue_memory,
    ):
        """Nothing waiting on early text means nothing to gain from streaming."""
        from jarvis.reply.engine import run_reply_engine

        seen = {}

        def mock_chat(*args, on_token=None, **kwargs):
            seen["on_token"] = on_token
            return _mock_response("Hallo.")

        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="hallo",
                dialogue_memory=dialogue_memory,
            )

        assert seen["on_token"] is None

    def test_a_speech_path_that_fails_does_not_cost_the_user_the_answer(
        self, mock_config, db, dialogue_memory,
    ):
        from jarvis.reply.engine import run_reply_engine

        def mock_chat(*args, on_token=None, **kwargs):
            on_token("Alles gut.")
            return _mock_response("Alles gut.")

        def explode(_segment):
            raise RuntimeError("no sound card")

        with patch("jarvis.reply.engine.chat_with_messages", side_effect=mock_chat):
            reply = run_reply_engine(
                db=db, cfg=mock_config, tts=None, text="hallo",
                dialogue_memory=dialogue_memory, on_speech_segment=explode,
            )

        assert reply == "Alles gut."


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


class TestTheListenerSpeaksEachSentenceAsItLands:
    """The listener owns the speech path, so it is what turns the engine's
    sentences into sound and closes the reply when there are no more."""

    def _listener(self, tts):
        from jarvis.listening.listener import VoiceListener

        listener = VoiceListener.__new__(VoiceListener)
        listener.tts = tts
        listener.echo_detector = MagicMock()
        # Registering speech clears the capture buffers, which a listener
        # built without its audio devices does not have.
        listener.track_tts_start = MagicMock()
        return listener

    def test_each_sentence_is_spoken_the_moment_it_arrives(self):
        tts = MagicMock()
        tts.enabled = True
        listener = self._listener(tts)

        speak = listener._speech_segment_sink()
        speak("Das Wetter ist gut.")
        speak("Zieh eine Jacke an.")

        assert [call.args[0] for call in tts.speak.call_args_list] == [
            "Das Wetter ist gut.", "Zieh eine Jacke an.",
        ]

    def test_only_the_first_sentence_reports_the_end_of_the_wait(self):
        """The felt wait ends when sound starts, which happens once."""
        tts = MagicMock()
        tts.enabled = True
        listener = self._listener(tts)
        started = MagicMock()

        speak = listener._speech_segment_sink(on_audio_start=started)
        speak("Erste.")
        speak("Zweite.")

        assert tts.speak.call_args_list[0].kwargs["audio_start_callback"] is started
        assert tts.speak.call_args_list[1].kwargs["audio_start_callback"] is None

    def test_echo_suppression_is_given_everything_said_so_far(self):
        """Jarvis must not answer its own voice coming back down the mic.

        The detector remembers one text, so it has to be the whole reply as
        it grows, not just the sentence most recently queued.
        """
        tts = MagicMock()
        tts.enabled = True
        listener = self._listener(tts)

        speak = listener._speech_segment_sink()
        speak("Das Wetter ist gut.")
        speak("Zieh eine Jacke an.")

        assert [call.args[0] for call in listener.track_tts_start.call_args_list] == [
            "Das Wetter ist gut.",
            "Das Wetter ist gut. Zieh eine Jacke an.",
        ]

    def test_a_disabled_speech_path_is_not_asked_to_speak(self):
        tts = MagicMock()
        tts.enabled = False
        listener = self._listener(tts)

        listener._speech_segment_sink()("Hallo.")

        tts.speak.assert_not_called()
