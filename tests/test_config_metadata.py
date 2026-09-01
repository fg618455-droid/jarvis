"""Tests for the config field metadata registry.

The registry is the single source both settings interfaces build their
forms from, so a field with a bad category, a missing default, or a choice
list that has drifted from the config makes a setting unreachable in both.
"""

import re

from jarvis.config import get_default_config
from jarvis.config_metadata import (
    CATEGORIES,
    CLOUD_TTS_PROVIDER_FIELD_METADATA,
    FIELD_METADATA,
    FieldMeta,
    LLM_ROUTE_FIELD_METADATA,
    _build_field_metadata,
    _is_default_value,
)


class TestFieldMetadata:
    """Tests for the config field metadata registry."""

    def test_all_fields_reference_valid_categories(self):
        """Every field's category must appear in CATEGORIES."""
        valid_cats = {key for key, _ in CATEGORIES}
        for fm in FIELD_METADATA:
            assert fm.category in valid_cats, (
                f"Field '{fm.key}' references unknown category '{fm.category}'"
            )

    def test_all_fields_reference_existing_config_keys(self):
        """Every field key must exist in get_default_config()."""
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            assert fm.key in defaults, (
                f"Field '{fm.key}' not found in default config"
            )

    def test_route_api_key_is_password_type(self):
        """Route credentials must render masked in every settings surface."""
        api_key = next(
            field for field in LLM_ROUTE_FIELD_METADATA if field.key == "api_key"
        )
        assert api_key.field_type == "password"

    def test_cloud_tts_metadata_stores_only_the_environment_name(self):
        keys = {field.key for field in CLOUD_TTS_PROVIDER_FIELD_METADATA}
        assert keys == {
            "name", "provider", "api_key_env", "voice_id", "model",
            "enabled", "timeout_sec",
        }
        assert "api_key" not in keys

    def test_cloud_tts_provider_schema_has_safe_editor_defaults(self):
        fields = {field.key: field for field in CLOUD_TTS_PROVIDER_FIELD_METADATA}

        assert fields["provider"].field_type == "choice"
        assert {value for value, _label in fields["provider"].choices} == {
            "fish_audio", "elevenlabs",
        }
        assert fields["enabled"].default_value is True
        assert fields["timeout_sec"].default_value == 10.0
        assert fields["api_key_env"].default_value == ""

    def test_no_duplicate_keys(self):
        """Each config key should appear at most once in the metadata."""
        keys = [fm.key for fm in FIELD_METADATA]
        assert len(keys) == len(set(keys)), (
            f"Duplicate keys: {[k for k in keys if keys.count(k) > 1]}"
        )

    def test_field_types_are_valid(self):
        """All field_type values must be from the allowed set."""
        valid_types = {
            "bool", "int", "float", "str", "choice", "device", "list",
            "object_list", "password",
        }
        for fm in FIELD_METADATA:
            assert fm.field_type in valid_types, (
                f"Field '{fm.key}' has invalid type '{fm.field_type}'"
            )

    def test_choice_fields_have_choices(self):
        """Fields with type 'choice' must have a non-empty choices list."""
        for fm in FIELD_METADATA:
            if fm.field_type == "choice":
                assert fm.choices and len(fm.choices) > 0, (
                    f"Choice field '{fm.key}' has no choices defined"
                )

    def test_numeric_fields_have_bounds(self):
        """Numeric fields (int/float) should have min and max defined."""
        for fm in FIELD_METADATA:
            if fm.field_type in ("int", "float") and not fm.nullable:
                assert fm.min_val is not None, (
                    f"Numeric field '{fm.key}' missing min_val"
                )
                assert fm.max_val is not None, (
                    f"Numeric field '{fm.key}' missing max_val"
                )

    def test_labels_are_nonempty(self):
        """Every field must have a non-empty label."""
        for fm in FIELD_METADATA:
            assert fm.label.strip(), f"Field '{fm.key}' has empty label"

    def test_descriptions_are_nonempty(self):
        """Every field must have a non-empty description."""
        for fm in FIELD_METADATA:
            assert fm.description.strip(), f"Field '{fm.key}' has empty description"

    def test_build_returns_consistent_results(self):
        """_build_field_metadata() should return the same structure on repeated calls."""
        a = _build_field_metadata()
        b = _build_field_metadata()
        assert len(a) == len(b)
        for fa, fb in zip(a, b):
            assert fa.key == fb.key
            assert fa.category == fb.category

    def test_low_power_mode_is_exposed_as_feature_toggle(self):
        """Low-power mode should be available without hand-editing config.json."""
        field = next((fm for fm in FIELD_METADATA if fm.key == "low_power_mode"), None)
        assert field is not None
        assert field.category == "features"
        assert field.field_type == "bool"


class TestLocalAISettings:
    """General settings expose only local runtime and behaviour controls.

    Effective providers, credentials, route models, backend override, and
    crew route selection have one authoritative editor at ``#/llm-routes``.
    """

    def _field(self, key):
        for fm in FIELD_METADATA:
            if fm.key == key:
                return fm
        return None

    def test_local_ai_has_its_own_category(self):
        cat_keys = [k for k, _ in CATEGORIES]
        assert "local_ai" in cat_keys
        assert "llm" not in cat_keys

    def test_route_authoritative_fields_are_not_duplicated(self):
        routed = {
            "llm_provider", "llm_base_url", "llm_api_key", "llm_chat_model",
            "embedding_provider", "embedding_base_url", "embedding_api_key",
            "embedding_model", "chat_backend_override", "crew_chat_agent",
        }
        present = {fm.key for fm in FIELD_METADATA}
        assert not routed & present

    def test_local_ai_follows_the_runtime_pipeline(self):
        expected_sections = {
            "ollama_chat_model": "Local models",
            "local_fast_model": "Local models",
            "ollama_embed_model": "Local models",
            "ollama_base_url": "Local models",
            "llm_chat_timeout_sec": "Timeouts",
            "llm_tools_timeout_sec": "Timeouts",
            "llm_embedding_timeout_sec": "Timeouts",
            "llm_profile_select_timeout_sec": "Timeouts",
            "intent_judge_timeout_sec": "Timeouts",
            "llm_thinking_enabled": "Thinking and behaviour",
            "intent_judge_thinking_enabled": "Thinking and behaviour",
        }
        for key, section in expected_sections.items():
            field = self._field(key)
            assert field is not None
            assert field.category == "local_ai"
            assert field.section == section


class TestSpeechSettings:
    """Capture, recognition, and playback mirror the real pipeline."""

    def test_pipeline_categories_are_explicit(self):
        categories = {key for key, _label in CATEGORIES}
        assert {"speech_input", "speech_recognition", "speech_output"} <= categories
        assert not {"voice_input", "wake", "whisper", "vad", "tts",
                    "piper", "chatterbox", "kokoro"} & categories

    def test_capture_and_vad_fields_are_speech_input(self):
        by_key = {field.key: field for field in FIELD_METADATA}
        keys = {
            "voice_device", "sample_rate", "voice_min_energy", "wake_word",
            "wake_fuzzy_ratio", "vad_enabled", "vad_aggressiveness",
            "endpoint_silence_ms", "max_utterance_ms",
        }
        assert {by_key[key].category for key in keys} == {"speech_input"}

    def test_whisper_fields_are_speech_recognition(self):
        fields = [field for field in FIELD_METADATA if field.key.startswith("whisper_")]
        assert fields
        assert {field.category for field in fields} == {"speech_recognition"}
        assert {field.section for field in fields} == {"Whisper"}

    def test_output_engines_are_labelled_sections(self):
        by_key = {field.key: field for field in FIELD_METADATA}
        expected = {
            "tts_output_device": "Common output",
            "tts_cloud_providers": "Cloud chain",
            "tts_piper_length_scale": "Piper",
            "tts_chatterbox_device": "Chatterbox",
            "tts_kokoro_voice": "Kokoro",
        }
        for key, section in expected.items():
            assert by_key[key].category == "speech_output"
            assert by_key[key].section == section


class TestCloudTTSField:
    def test_provider_chain_is_an_editable_structured_field(self):
        field = next(
            (item for item in FIELD_METADATA if item.key == "tts_cloud_providers"),
            None,
        )

        assert field is not None
        assert field.category == "speech_output"
        assert field.section == "Cloud chain"
        assert field.field_type == "object_list"
        assert field.item_fields == CLOUD_TTS_PROVIDER_FIELD_METADATA


class TestMinimalConfigInvariant:
    """``_is_default_value`` decides whether a field is omitted from
    config.json. An emptied nullable provider field (reads back as None)
    whose default is an empty string must be omitted, not persisted as null."""

    def test_value_equal_to_default_is_omitted(self):
        assert _is_default_value("ollama", "ollama") is True

    def test_changed_value_is_kept(self):
        assert _is_default_value("openai_compatible", "ollama") is False

    def test_emptied_field_with_empty_string_default_is_omitted(self):
        # llm_base_url etc.: default "", user clears it -> _get_value returns None
        assert _is_default_value(None, "") is True

    def test_emptied_field_with_none_default_is_omitted(self):
        assert _is_default_value(None, None) is True

    def test_set_value_over_empty_default_is_kept(self):
        assert _is_default_value("http://localhost:1234/v1", "") is False

    def test_none_over_nonempty_default_is_kept(self):
        # A nullable field whose default is a real value, cleared by the user,
        # is a genuine change and must be written.
        assert _is_default_value(None, "gemma4:e2b") is False


class TestCategories:
    """Tests for category definitions."""

    def test_no_duplicate_category_keys(self):
        """Category keys should be unique."""
        keys = [k for k, _ in CATEGORIES]
        assert len(keys) == len(set(keys))

    def test_every_category_has_fields(self):
        """Every defined category should have at least one field.

        The 'mcps' category uses a custom page, not FIELD_METADATA, so it's excluded.
        """
        cats_with_fields = {fm.category for fm in FIELD_METADATA}
        custom_page_categories = {"mcps"}
        for key, label in CATEGORIES:
            if key in custom_page_categories:
                continue
            assert key in cats_with_fields, (
                f"Category '{key}' ({label}) has no fields"
            )

    def test_mcps_category_exists(self):
        """The MCP Servers category must be present in the sidebar."""
        cat_keys = [k for k, _ in CATEGORIES]
        assert "mcps" in cat_keys


class TestDefaultValueTypes:
    """Verify that default values match the declared field types."""

    def test_bool_defaults_are_bool(self):
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "bool":
                val = defaults.get(fm.key)
                assert isinstance(val, bool), (
                    f"Field '{fm.key}' default {val!r} is not bool"
                )

    def test_int_defaults_are_numeric(self):
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "int" and not fm.nullable:
                val = defaults.get(fm.key)
                assert isinstance(val, (int, float)), (
                    f"Field '{fm.key}' default {val!r} is not numeric"
                )

    def test_float_defaults_are_numeric(self):
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "float":
                val = defaults.get(fm.key)
                assert isinstance(val, (int, float)), (
                    f"Field '{fm.key}' default {val!r} is not numeric"
                )

    def test_choice_defaults_are_in_choices(self):
        """Default values for choice fields must be one of the valid choices."""
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "choice" and fm.choices:
                val = str(defaults.get(fm.key))
                valid_values = [c[0] for c in fm.choices]
                assert val in valid_values, (
                    f"Field '{fm.key}' default '{val}' not in choices {valid_values}"
                )




class TestACategoryNamesItselfInWords:
    """A settings category is a word, not a picture of one.

    The interface already says what a thing is with type: one accent, one
    type ladder, one icon set drawn in the current colour. A pictograph in
    the label ignores all three. It arrives in whatever palette the reader's
    font vendor chose, it cannot be restyled by a theme, and there are more
    categories than there are obvious pictures for them, so the set repeats
    itself and two unrelated categories end up wearing the same glyph.

    The label is asserted rather than the rendering because the label is
    what every surface reads: strip it here and every consumer is clean.
    """

    # Pictographs and their modifiers, rather than a list of the ones that
    # happen to be in the file today. Any language's letters, marks and
    # punctuation pass; a picture does not.
    PICTOGRAPHS = re.compile(
        "[\U0001f000-\U0001faff\u2190-\u2bff\ufe0f\u200d]",
    )

    def test_no_category_label_carries_a_pictograph(self):
        offenders = {
            key: label
            for key, label in CATEGORIES
            if self.PICTOGRAPHS.search(label)
        }

        assert not offenders, f"category labels carrying pictographs: {offenders}"

    def test_every_category_is_named_and_named_once(self):
        """Two categories wearing one glyph is what made this worth asserting."""
        labels = [label for _, label in CATEGORIES]

        assert all(label.strip() for label in labels), "a category with no name"
        assert len(set(labels)) == len(labels), (
            f"two categories share a name: {sorted(labels)}"
        )
