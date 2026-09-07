"""One setting, one place.

A value that can be changed from two screens has two truths about it as
soon as one of them is stale, and the reader has no way of knowing which
screen they were last on. These tests hold the rule as a mechanism rather
than as a list of keys, so a field added later is held to it too.

The registry these read is the same one the Qt settings window builds its
form from, so what is asserted here is true of both surfaces at once.
"""

import json
from collections import Counter

import pytest

from jarvis.config_metadata import CATEGORIES, CATEGORY_DETAILS, FIELD_METADATA
from jarvis.webui.server import WebUIConfig, create_app


HEADERS = {"Host": "127.0.0.1:5055"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"_config_version": 3}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    return app.test_client()


class TestNoKeyIsOfferedTwice:
    """The form is built from the registry, so a duplicate there is a
    duplicate on screen."""

    def test_no_config_key_appears_twice_in_the_registry(self):
        counted = Counter(meta.key for meta in FIELD_METADATA)
        assert [key for key, n in counted.items() if n > 1] == []

    def test_the_page_offers_each_key_from_exactly_one_category(self, client):
        payload = client.get("/api/settings", headers=HEADERS).get_json()
        homes = {}
        for field in payload["fields"]:
            homes.setdefault(field["key"], set()).add(field["category"])
        assert {key: cats for key, cats in homes.items() if len(cats) > 1} == {}

    def test_a_switch_with_a_live_control_is_not_repeated_as_a_field(self):
        """The passive record's switch asks for consent and names the backend
        that will see the room. A plain checkbox beside it is the same switch
        with the question taken out, and whichever of the two was used last
        is the one the other misreports."""
        assert "passive_capture_enabled" not in {meta.key for meta in FIELD_METADATA}


class TestEveryCategoryEarnsItsPlace:
    def test_no_category_is_offered_with_nothing_in_it(self, client):
        """The page filters a category with neither fields nor an editor, so
        one listed here is a row that can never be reached."""
        payload = client.get("/api/settings", headers=HEADERS).get_json()
        carried = {field["category"] for field in payload["fields"]}
        empty = [
            category["key"]
            for category in payload["categories"]
            if category["key"] not in carried and not category.get("embed")
        ]
        assert empty == []

    def test_every_field_names_a_category_that_exists(self):
        known = {key for key, _ in CATEGORIES}
        assert sorted({m.category for m in FIELD_METADATA} - known) == []

    def test_every_detail_describes_a_category_that_exists(self):
        known = {key for key, _ in CATEGORIES}
        assert sorted(set(CATEGORY_DETAILS) - known) == []


class TestEveryFieldSitsUnderAHeading:
    """Both forms render `section` as a heading. A category where some fields
    carry one and others do not reads as a list that lost its first heading."""

    def test_a_category_labels_all_of_its_fields_or_none(self):
        sectioned = {}
        for meta in FIELD_METADATA:
            sectioned.setdefault(meta.category, set()).add(bool(meta.section))
        assert {cat: flags for cat, flags in sectioned.items() if len(flags) > 1} == {}


class TestWhereThingsLive:
    def _category_of(self, key):
        return next(meta.category for meta in FIELD_METADATA if meta.key == key)

    # Ollama's settings are the memory model, the embedding model and the
    # server they run on. It has no reply-side settings any more: FAST and
    # CHAT are answered by configured routes alone.
    @pytest.mark.parametrize("key", [
        "ollama_base_url", "ollama_chat_model", "ollama_embed_model",
    ])
    def test_the_local_provider_lives_with_the_providers(self, key):
        assert self._category_of(key) == "providers"

    @pytest.mark.parametrize("key", [
        "telegram_bot_token", "telegram_chat_id",
        "telegram_api_base_url", "telegram_chat_enabled",
    ])
    def test_telegram_is_a_channel_rather_than_a_policy(self, key):
        assert self._category_of(key) == "channels"

    def test_the_confirmation_channels_stay_with_the_policy(self):
        """Which channels may ask is the gate's own rule, not Telegram's
        credentials."""
        assert self._category_of("security_confirm_channels") == "security"

    def test_the_mcp_servers_category_carries_its_editor(self, client):
        payload = client.get("/api/settings", headers=HEADERS).get_json()
        mcps = next(c for c in payload["categories"] if c["key"] == "mcps")
        assert mcps.get("embed") == "mcp-servers"
