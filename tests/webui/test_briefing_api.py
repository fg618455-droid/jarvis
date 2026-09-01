"""What is on today, read rather than waited for.

The assistant already has a morning briefing: once per local day, at a
configured time, it reads the School branch and says the result out loud.
That is the right shape for a thing that finds you, and the wrong shape for
a thing you want to check. Speech has happened or it has not, it cannot be
re-read, and before the trigger time it does not exist at all.

So the deck asks the same source the same question, and the two share
everything that matters: the same School branch, the same generator, the
same persona. What differs is only what a screen can do that a speaker
cannot. The items are extracted deterministically and cost nothing, so a
widget can show them on every reading. The prose costs a CHAT-tier call, so
it is generated only when asked for and then cached for the rest of the day.

The rule underneath all of it: this endpoint never invents a school. An
empty branch reads as empty, and a generation that fails says so.
"""

import json

import pytest

from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}
WRITE_HEADERS = {**HEADERS, "X-Jarvis-UI": "1"}


SCHOOL = {
    "branch": "school",
    "nodes": [
        {"name": "Maths", "description": "Exam on Friday", "data": "Chapters 4 to 6"},
        {"name": "English", "description": "", "data": "Read two chapters by Tuesday"},
        {"name": "", "description": "", "data": ""},
    ],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    # `db_path` is named explicitly. The summary is cached in the memory
    # database, so a config that leaves it at the default would have these
    # tests writing today's briefing into the real assistant's memory.
    config_path.write_text(json.dumps({
        "_config_version": 3,
        "db_path": str(tmp_path / "jarvis.db"),
    }), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def school(monkeypatch):
    """The School branch, without a database behind it."""
    from jarvis.webui.api import briefing

    monkeypatch.setattr(briefing, "read_school_branch", lambda *a, **k: SCHOOL)
    return SCHOOL


@pytest.fixture
def empty_school(monkeypatch):
    from jarvis.webui.api import briefing

    monkeypatch.setattr(
        briefing, "read_school_branch", lambda *a, **k: {"branch": "school", "nodes": []},
    )


class TestReadingWhatIsOn:
    def test_the_branch_becomes_items_without_a_model(self, client, school, monkeypatch):
        from jarvis.webui.api import briefing

        def refuse(*args, **kwargs):
            raise AssertionError("reading the briefing must not call a model")

        monkeypatch.setattr(briefing, "generate_morning_briefing", refuse)

        body = client.get("/api/briefing", headers=HEADERS).get_json()

        assert body["available"] is True
        assert [item["title"] for item in body["items"]] == ["Maths", "English"]

    def test_an_item_carries_what_is_known_about_it(self, client, school):
        body = client.get("/api/briefing", headers=HEADERS).get_json()

        maths = body["items"][0]
        assert "Exam on Friday" in maths["note"]
        assert "Chapters 4 to 6" in maths["note"]

    def test_an_empty_branch_reads_as_empty_rather_than_as_a_failure(
        self, client, empty_school,
    ):
        response = client.get("/api/briefing", headers=HEADERS)

        assert response.status_code == 200
        body = response.get_json()
        assert body["available"] is False
        assert body["items"] == []

    def test_it_reports_the_spoken_briefing_it_shares_a_source_with(self, client, school):
        body = client.get("/api/briefing", headers=HEADERS).get_json()

        assert body["spoken"]["enabled"] is False
        assert body["spoken"]["time"] == "07:00"

    def test_no_summary_exists_until_one_is_asked_for(self, client, school):
        body = client.get("/api/briefing", headers=HEADERS).get_json()

        assert body["summary"] == ""


class TestAskingForProse:
    def test_refreshing_generates_a_summary(self, client, school, monkeypatch):
        from jarvis.webui.api import briefing

        monkeypatch.setattr(
            briefing, "generate_morning_briefing",
            lambda snapshot, cfg, day: "Maths exam on Friday, two chapters of English.",
        )

        body = client.post("/api/briefing/refresh", headers=WRITE_HEADERS).get_json()

        assert body["summary"] == "Maths exam on Friday, two chapters of English."

    def test_the_summary_is_read_back_without_generating_it_again(
        self, client, school, monkeypatch,
    ):
        from jarvis.webui.api import briefing

        calls = []

        def generate(snapshot, cfg, day):
            calls.append(day)
            return "Generated once."

        monkeypatch.setattr(briefing, "generate_morning_briefing", generate)
        client.post("/api/briefing/refresh", headers=WRITE_HEADERS)

        body = client.get("/api/briefing", headers=HEADERS).get_json()

        assert body["summary"] == "Generated once."
        assert len(calls) == 1, "reading the cached summary called the model again"

    def test_the_generator_is_the_one_the_spoken_briefing_uses(self, client, school):
        """Two briefings phrased by two prompts would disagree about the day."""
        from jarvis.memory import morning_briefing
        from jarvis.webui.api import briefing

        assert briefing.generate_morning_briefing is morning_briefing.generate_morning_briefing

    def test_a_generation_that_fails_says_so_rather_than_inventing_one(
        self, client, school, monkeypatch,
    ):
        from jarvis.webui.api import briefing

        monkeypatch.setattr(
            briefing, "generate_morning_briefing", lambda *args: None,
        )

        response = client.post("/api/briefing/refresh", headers=WRITE_HEADERS)

        assert response.status_code == 503
        assert response.get_json()["error"]

    def test_an_empty_branch_is_never_sent_to_a_model(
        self, client, empty_school, monkeypatch,
    ):
        from jarvis.webui.api import briefing

        def refuse(*args, **kwargs):
            raise AssertionError("an empty branch reached the model")

        monkeypatch.setattr(briefing, "generate_morning_briefing", refuse)

        response = client.post("/api/briefing/refresh", headers=WRITE_HEADERS)

        assert response.status_code == 409
        assert response.get_json()["error"]

    def test_refreshing_needs_the_interface_header(self, client, school):
        response = client.post("/api/briefing/refresh", headers=HEADERS)

        assert response.status_code == 403
