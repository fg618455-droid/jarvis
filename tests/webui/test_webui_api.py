"""Behaviour tests for the control centre's API.

Each endpoint is exercised through a real request, because the guards, the
blueprint wiring, and the JSON shape are all things a direct function call
would skip.
"""

import json
from pathlib import Path

import pytest

from jarvis.runtime import get_recorder, get_runtime_state
from jarvis.runtime.state import Phase
from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}
WRITE_HEADERS = {**HEADERS, "X-Jarvis-UI": "1"}


@pytest.fixture
def client():
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(autouse=True)
def _clean_runtime():
    get_recorder().abandon()
    get_recorder().clear()
    get_recorder().use_journal(None)
    get_runtime_state().reset()
    yield
    get_recorder().abandon()
    get_recorder().clear()
    get_recorder().use_journal(None)
    get_runtime_state().reset()


def _record_turn(transcript="hallo", reply="hi", source="voice"):
    recorder = get_recorder()
    trace = recorder.begin(source=source)
    trace.transcript = transcript
    with trace.stage("stt"):
        pass
    trace.record_tool("webSearch", duration_ms=12.0, ok=True)
    return recorder.finish(reply=reply)


class TestStatus:
    def test_status_reports_the_current_phase(self, client):
        get_runtime_state().set_phase(Phase.THINKING)

        body = client.get("/api/status", headers=HEADERS).get_json()

        assert body["phase"] == "thinking"
        assert body["uptime_seconds"] >= 0
        assert body["daemon_running"] is True

    def test_standalone_status_does_not_claim_live_daemon_state(self):
        from jarvis.webui.server import WebUIConfig, create_app

        app = create_app(WebUIConfig(
            host="127.0.0.1", port=5055, token="", standalone=True,
        ))
        app.config.update(TESTING=True)
        response = app.test_client().get(
            "/api/status", headers={"Host": "127.0.0.1:5055"},
        )
        body = response.get_json()

        assert body["daemon_running"] is False
        assert body["phase"] is None
        assert body["uptime_seconds"] is None
        assert body["passive"]["enabled"] is False
        assert body["conversation"] == {"active": False}
        assert body["models"] == {}

    def test_turns_returns_the_history(self, client):
        _record_turn(transcript="wie spät ist es")

        body = client.get("/api/turns", headers=HEADERS).get_json()

        assert [t["transcript"] for t in body["turns"]] == ["wie spät ist es"]

    def test_a_limit_narrows_the_history(self, client):
        for index in range(5):
            _record_turn(transcript=str(index))

        body = client.get("/api/turns?limit=2", headers=HEADERS).get_json()

        assert [t["transcript"] for t in body["turns"]] == ["3", "4"]

    def test_a_nonsense_limit_does_not_break_the_call(self, client):
        _record_turn()

        assert client.get("/api/turns?limit=abc", headers=HEADERS).status_code == 200


class TestTurnExport:
    def test_the_export_carries_a_column_per_stage(self, client):
        _record_turn()

        response = client.get("/api/turns/export.csv", headers=HEADERS)

        header = response.get_data(as_text=True).splitlines()[0]
        assert "stt_ms" in header
        assert response.headers["Content-Disposition"].endswith("jarvis-turns.csv")

    def test_an_empty_history_still_exports_a_header(self, client):
        response = client.get("/api/turns/export.csv", headers=HEADERS)

        assert response.get_data(as_text=True).startswith("turn_id,")


class TestEvents:
    def test_the_stream_opens_with_the_current_state(self, client):
        get_runtime_state().set_phase(Phase.SPEAKING)

        response = client.get("/api/events", headers=HEADERS)
        first = next(response.response).decode("utf-8")

        assert first.startswith("event: status")
        assert json.loads(first.split("data: ", 1)[1])["phase"] == "speaking"
        response.close()

    def test_standalone_stream_opens_with_empty_live_state(self):
        from jarvis.webui.server import WebUIConfig, create_app

        app = create_app(WebUIConfig(
            host="127.0.0.1", port=5055, token="", standalone=True,
        ))
        response = app.test_client().get(
            "/api/events", headers={"Host": "127.0.0.1:5055"},
        )
        first = next(response.response).decode("utf-8")

        assert json.loads(first.split("data: ", 1)[1])["daemon_running"] is False
        response.close()

    def test_the_stream_is_declared_as_events(self, client):
        response = client.get("/api/events", headers=HEADERS)

        assert response.mimetype == "text/event-stream"
        response.close()


class TestConversation:
    def test_the_conversation_carries_turns_and_discards(self, client):
        _record_turn()
        get_runtime_state().count_discard("vad")

        body = client.get("/api/conversation", headers=HEADERS).get_json()

        assert len(body["turns"]) == 1
        assert body["discarded"] == {"vad": 1}

    def test_the_switch_turns_the_conversation_on_and_reports_it(self, client):
        """The button is the whole point: no wake word per question."""
        from jarvis.listening.conversation_mode import register_conversation_controller
        from jarvis.listening.state_manager import StateManager

        manager = StateManager(hot_window_seconds=0.05, echo_tolerance=0.01)
        register_conversation_controller(manager)
        try:
            response = client.post("/api/conversation/mode", headers=WRITE_HEADERS,
                                   json={"enabled": True})

            assert response.status_code == 200
            assert response.get_json()["active"] is True
            assert manager.is_conversation_active is True

            client.post("/api/conversation/mode", headers=WRITE_HEADERS,
                        json={"enabled": False})

            assert manager.is_conversation_active is False
        finally:
            register_conversation_controller(None)
            manager.stop()

    def test_the_switch_says_so_when_nothing_is_listening(self, client):
        """Standalone, there is no voice loop to hold a conversation open.

        Reporting success would leave a page showing a mode that is not
        running anywhere.
        """
        from jarvis.listening.conversation_mode import register_conversation_controller

        register_conversation_controller(None)

        response = client.post("/api/conversation/mode", headers=WRITE_HEADERS,
                               json={"enabled": True})

        assert response.status_code == 409
        assert response.get_json()["active"] is False

    def test_the_conversation_view_says_whether_the_mode_is_on(self, client):
        body = client.get("/api/conversation", headers=HEADERS).get_json()

        assert body["conversation_mode"] is False

    def test_flipping_the_switch_needs_the_write_header(self, client):
        response = client.post("/api/conversation/mode", headers=HEADERS,
                               json={"enabled": True})

        assert response.status_code == 403


class TestChat:
    @pytest.fixture(autouse=True)
    def _own_database(self, tmp_path, monkeypatch):
        """Point a typed turn at a throwaway database.

        Without this the test opens the real one, which is both a side
        effect on the user's data and a source of failures that depend on
        what else the run has open.
        """
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"db_path": str(tmp_path / "jarvis.db")}), encoding="utf-8",
        )
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    def test_empty_text_is_refused(self, client):
        response = client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "   "})

        assert response.status_code == 400

    def test_overlong_text_is_refused(self, client):
        response = client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "x" * 5000})

        assert response.status_code == 400

    def test_typing_goes_through_the_reply_engine(self, client, monkeypatch):
        seen = {}

        def _fake_engine(db, cfg, tts, text, dialogue_memory, language=None):
            seen["text"] = text
            return "Es ist drei Uhr."

        monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", _fake_engine)

        body = client.post(
            "/api/chat", headers=WRITE_HEADERS, json={"text": "wie spät ist es"},
        ).get_json()

        assert seen["text"] == "wie spät ist es"
        assert body["reply"] == "Es ist drei Uhr."
        assert body["turn"]["source"] == "text"

    def test_a_typed_turn_lands_in_the_history(self, client, monkeypatch):
        monkeypatch.setattr(
            "jarvis.reply.engine.run_reply_engine",
            lambda *a, **k: "Ja.",
        )

        client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "bist du da"})

        assert get_recorder().history()[0]["reply"] == "Ja."

    def test_a_failing_engine_answers_with_the_failure(self, client, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("model gone")

        monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", _boom)

        response = client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "hallo"})

        assert response.status_code == 500
        assert "model gone" in response.get_json()["error"]

    def test_typing_needs_the_write_header(self, client):
        response = client.post("/api/chat", headers=HEADERS, json={"text": "hallo"})

        assert response.status_code == 403

    def test_typing_waits_for_a_turn_already_running(self, client, monkeypatch):
        """A spoken turn and a typed one must not run the engine at once.

        Voice, the desktop chat window and the control centre all reach the
        same reply engine against the same dialogue memory. The daemon owns
        the one lock that serialises them, so a typed turn submitted while a
        spoken one is in flight is refused rather than run alongside it.
        """
        import jarvis.daemon as daemon

        monkeypatch.setattr(
            "jarvis.reply.engine.run_reply_engine",
            lambda *a, **k: "sollte nie laufen",
        )

        with daemon.query_lock():
            response = client.post(
                "/api/chat", headers=WRITE_HEADERS, json={"text": "hallo"},
            )

        assert response.status_code == 409

    def test_the_lock_is_free_again_after_a_typed_turn(self, client, monkeypatch):
        """A finished turn must not leave the shared lock held."""
        import jarvis.daemon as daemon

        monkeypatch.setattr(
            "jarvis.reply.engine.run_reply_engine",
            lambda *a, **k: "fertig",
        )

        client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "hallo"})

        with daemon.query_lock():
            pass


class TestOneConversation:
    """Typed turns join the spoken conversation rather than starting one.

    This is the invariant the whole feature rests on: a question asked out
    loud and a follow-up typed into the control centre are one exchange, so
    the follow-up can say "and that one?" and be understood. It holds only
    if the endpoint runs the engine against the daemon's own dialogue
    memory, which is the object the voice path writes into.
    """

    @pytest.fixture(autouse=True)
    def _own_database(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"db_path": str(tmp_path / "chat.db")}), encoding="utf-8",
        )
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    @pytest.fixture
    def running_daemon(self, monkeypatch):
        """A daemon whose dialogue memory the endpoint should find and use."""
        import jarvis.daemon as daemon
        from jarvis.memory.conversation import DialogueMemory

        memory = DialogueMemory(inactivity_timeout=300, max_interactions=20)
        monkeypatch.setattr(daemon, "_global_dialogue_memory", memory, raising=False)
        return memory

    def test_a_typed_turn_uses_the_daemons_dialogue_memory(
        self, client, monkeypatch, running_daemon,
    ):
        seen = {}

        def _engine(db, cfg, tts, text, dialogue_memory, language=None, **kwargs):
            seen["dialogue_memory"] = dialogue_memory
            return "Physik, sagten Sie."

        monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", _engine)

        client.post(
            "/api/chat", headers=WRITE_HEADERS, json={"text": "welches Fach war das"},
        )

        assert seen["dialogue_memory"] is running_daemon

    def test_a_typed_follow_up_sees_what_was_said_aloud(
        self, client, monkeypatch, running_daemon,
    ):
        """The spoken turn is in the history the typed turn is answered from."""
        running_daemon.add_interaction(
            "welches Fach mag ich am liebsten",
            "Sie sagten, Physik.",
        )
        seen = {}

        def _engine(db, cfg, tts, text, dialogue_memory, language=None, **kwargs):
            seen["history"] = dialogue_memory.get_recent_messages()
            return "Physik."

        monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", _engine)

        client.post(
            "/api/chat", headers=WRITE_HEADERS, json={"text": "und das nochmal?"},
        )

        spoken = " ".join(message["content"] for message in seen["history"])
        assert "Physik" in spoken

    def test_a_spoken_follow_up_sees_what_was_typed(
        self, client, monkeypatch, running_daemon,
    ):
        """The other direction: typing leaves its turn where voice will read it."""
        def _engine(db, cfg, tts, text, dialogue_memory, language=None, **kwargs):
            dialogue_memory.add_interaction(text, "Notiert: Ihr Hund heißt Bello.")
            return "Notiert: Ihr Hund heißt Bello."

        monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", _engine)

        client.post(
            "/api/chat",
            headers=WRITE_HEADERS,
            json={"text": "mein Hund heißt Bello"},
        )

        # What the voice path would read on its next turn.
        typed = " ".join(
            message["content"] for message in running_daemon.get_recent_messages()
        )
        assert "Bello" in typed

    def test_without_a_daemon_the_control_centre_keeps_its_own_conversation(
        self, client, monkeypatch,
    ):
        """Standalone there is no spoken conversation to join, but context still holds."""
        import jarvis.daemon as daemon

        monkeypatch.setattr(daemon, "_global_dialogue_memory", None, raising=False)
        seen = []

        def _engine(db, cfg, tts, text, dialogue_memory, language=None, **kwargs):
            seen.append(dialogue_memory)
            dialogue_memory.add_interaction(text, "Gut.")
            return "Gut."

        monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", _engine)

        client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "erste Frage"})
        client.post("/api/chat", headers=WRITE_HEADERS, json={"text": "zweite Frage"})

        assert seen[0] is not None
        assert seen[1] is seen[0], "both turns must share one standalone conversation"


class TestTools:
    def test_every_builtin_tool_is_listed(self, client):
        from jarvis.tools.registry import BUILTIN_TOOLS

        body = client.get("/api/tools", headers=HEADERS).get_json()

        names = {tool["name"] for tool in body["tools"]}
        assert set(BUILTIN_TOOLS) <= names

    def test_a_tool_says_where_it_came_from(self, client):
        body = client.get("/api/tools", headers=HEADERS).get_json()

        assert {tool["origin"] for tool in body["tools"]} <= {"builtin", "mcp"}

    def test_recent_use_is_attached_to_the_tool(self, client):
        _record_turn()

        body = client.get("/api/tools", headers=HEADERS).get_json()

        web_search = next(t for t in body["tools"] if t["name"] == "webSearch")
        assert web_search["last_use"]["ok"] is True


class TestSecurity:
    def test_the_overview_names_the_level_and_channels(self, client):
        body = client.get("/api/security", headers=HEADERS).get_json()

        assert body["level"] in body["levels"]
        assert isinstance(body["channels"], list)

    def test_deciding_without_a_request_is_refused(self, client):
        response = client.post(
            "/api/security/decide", headers=WRITE_HEADERS,
            json={"request_id": "nope", "approved": True},
        )

        assert response.status_code == 404

    def test_a_decision_needs_a_request_id(self, client):
        response = client.post(
            "/api/security/decide", headers=WRITE_HEADERS, json={"approved": True},
        )

        assert response.status_code == 400


class TestSystem:
    def test_the_reading_names_the_models_in_use(self, client):
        body = client.get("/api/system", headers=HEADERS).get_json()

        assert body["models"]["chat"]
        assert body["speech_recognition"]["backend"]
        assert body["process"]["pid"] > 0

    def test_paths_are_reported_with_whether_they_exist(self, client):
        body = client.get("/api/system", headers=HEADERS).get_json()

        labels = {entry["label"] for entry in body["paths"]}
        assert {"config", "database"} <= labels
        assert all("exists" in entry for entry in body["paths"])

    def test_cloud_speech_reading_names_chain_and_local_fallback(
        self, client, tmp_path, monkeypatch,
    ):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "_config_version": 5,
            "tts_engine": "cloud",
            "tts_local_fallback_engine": "kokoro",
            "tts_kokoro_voice": "bm_lewis",
            "tts_cloud_providers": [{
                "name": "ElevenLabs", "provider": "elevenlabs",
                "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "voice-1",
                "model": "eleven_multilingual_v2", "enabled": True,
                "timeout_sec": 8.5,
            }],
        }), encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

        output = client.get("/api/system", headers=HEADERS).get_json()["speech_output"]

        assert output["engine"] == "cloud"
        assert output["cloud_providers"] == [{
            "name": "ElevenLabs", "provider": "elevenlabs",
            "model": "eleven_multilingual_v2", "enabled": True,
        }]
        assert output["local_fallback_engine"] == "kokoro"
        assert output["model"] == "bm_lewis"
        assert output["language"] == "British English"

    def test_the_config_named_is_the_one_this_run_reads(self, client, tmp_path, monkeypatch):
        """A side-by-side run must not be told it is editing the real file."""
        elsewhere = tmp_path / "elsewhere" / "config.json"
        elsewhere.parent.mkdir()
        elsewhere.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(elsewhere))

        body = client.get("/api/system", headers=HEADERS).get_json()

        config = next(entry for entry in body["paths"] if entry["label"] == "config")
        assert Path(config["path"]) == elsewhere

    def test_restart_requires_the_write_header(self, client):
        response = client.post("/api/system/restart", headers=HEADERS)

        assert response.status_code == 403

    def test_restart_asks_the_daemon_to_restart(self, client, monkeypatch):
        from unittest.mock import MagicMock

        mock_request_restart = MagicMock()
        monkeypatch.setattr("jarvis.daemon.request_restart", mock_request_restart)

        response = client.post("/api/system/restart", headers=WRITE_HEADERS)

        assert response.status_code == 200
        assert response.get_json() == {"restarting": True}
        mock_request_restart.assert_called_once()

    def test_standalone_control_centre_refuses_a_fake_restart(self):
        from jarvis.webui.server import WebUIConfig, create_app

        app = create_app(WebUIConfig(
            host="127.0.0.1", port=5055, token="", standalone=True,
        ))
        response = app.test_client().post(
            "/api/system/restart",
            headers={"Host": "127.0.0.1:5055", "X-Jarvis-UI": "1"},
        )

        assert response.status_code == 409
        assert response.get_json() == {"error": "no daemon is running"}


class TestTheStreamCarriesEveryEvent:
    """The browser subscribes to named events, one listener per kind.

    `EventSource` delivers a named event only to a listener registered for
    that name, so a kind the runtime publishes but the client never names is
    dropped in silence: no error, no warning, just an indicator that never
    lights up. The list is therefore a contract, and this test is what keeps
    it in step with the publishers.
    """

    def _published_kinds(self):
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "src" / "jarvis"
        kinds = set()
        for path in source.rglob("*.py"):
            kinds |= set(re.findall(r'publish\(\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
        return kinds

    def _subscribed_kinds(self):
        import re
        from pathlib import Path

        client = (Path(__file__).resolve().parents[2]
                  / "src" / "jarvis" / "webui" / "static" / "js" / "sse.js")
        block = re.search(r"for \(const kind of \[(.*?)\]", client.read_text(encoding="utf-8"),
                          re.DOTALL)
        assert block, "sse.js no longer lists the kinds it subscribes to"
        return set(re.findall(r'"([a-z_]+)"', block.group(1)))

    def test_every_published_kind_is_subscribed(self):
        missing = self._published_kinds() - self._subscribed_kinds()

        assert not missing, (
            f"the control centre never listens for {sorted(missing)}, so those "
            "events are dropped in the browser without any sign of it"
        )
