"""Tests for the config field metadata registry.

The registry is the single source both settings interfaces build their
forms from, so a field with a bad category, a missing default, or a choice
list that has drifted from the config makes a setting unreachable in both.
"""

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


class TestLLMProviderFields:
    """The settings UI must expose the provider-aware LLM config so a user
    can select an OpenAI-compatible backend without editing config.json by
    hand."""

    def _field(self, key):
        for fm in FIELD_METADATA:
            if fm.key == key:
                return fm
        return None

    def test_provider_settings_share_the_llm_category(self):
        """Connection settings and model settings describe one decision."""
        cat_keys = [k for k, _ in CATEGORIES]
        assert "llm" in cat_keys
        assert "llm_provider" not in cat_keys

    def test_provider_fields_present(self):
        """All eight provider-aware config keys are surfaced."""
        expected = {
            "llm_provider", "llm_base_url", "llm_api_key", "llm_chat_model",
            "embedding_provider", "embedding_base_url", "embedding_api_key",
            "embedding_model",
        }
        present = {fm.key for fm in FIELD_METADATA}
        missing = expected - present
        assert not missing, f"Provider fields missing from settings UI: {missing}"

    def test_provider_fields_live_in_the_llm_category(self):
        """The provider connection and model fields form one LLM page."""
        for key in (
            "llm_provider", "llm_base_url", "llm_api_key", "llm_chat_model",
            "embedding_provider", "embedding_base_url", "embedding_api_key",
            "embedding_model",
        ):
            fm = self._field(key)
            assert fm is not None and fm.category == "llm", (
                f"'{key}' should be in the 'llm' category"
            )

    def test_llm_provider_choices_match_config(self):
        """The provider dropdown offers exactly the values the config loader
        accepts ('ollama', 'openai_compatible')."""
        fm = self._field("llm_provider")
        assert fm is not None and fm.field_type == "choice"
        values = {v for v, _ in (fm.choices or [])}
        assert values == {"ollama", "openai_compatible"}

    def test_embedding_provider_offers_inherit_option(self):
        """embedding_provider includes the empty 'same as chat provider'
        option plus the two concrete providers."""
        fm = self._field("embedding_provider")
        assert fm is not None and fm.field_type == "choice"
        values = {v for v, _ in (fm.choices or [])}
        assert "" in values, "must offer an inherit-from-chat-provider option"
        assert {"ollama", "openai_compatible"} <= values

    def test_api_key_fields_are_password_type(self):
        """API keys must use the password field type so they render masked."""
        for key in ("llm_api_key", "embedding_api_key"):
            fm = self._field(key)
            assert fm is not None and fm.field_type == "password", (
                f"'{key}' should be a password field"
            )

    def test_model_fields_are_freetext(self):
        """The provider model fields are free text — an OpenAI-compatible
        server's model name is not in the Ollama SUPPORTED_CHAT_MODELS
        catalogue, so a choice dropdown would be wrong."""
        for key in ("llm_chat_model", "embedding_model"):
            fm = self._field(key)
            assert fm is not None and fm.field_type == "str", (
                f"'{key}' should be a free-text str field"
            )

    def test_connection_fields_are_nullable(self):
        """Connection/credential/model fields are nullable so leaving them
        empty falls back to the Ollama settings and keeps config.json minimal."""
        for key in (
            "llm_base_url", "llm_api_key", "llm_chat_model",
            "embedding_base_url", "embedding_api_key", "embedding_model",
        ):
            fm = self._field(key)
            assert fm is not None and fm.nullable, f"'{key}' should be nullable"


class TestSpeechSettings:
    """Input capture, wake detection and transcription are one pipeline."""

    def test_input_pipeline_has_one_category(self):
        categories = {key for key, _label in CATEGORIES}
        assert "speech_input" in categories
        assert not {"voice_input", "wake", "whisper", "vad"} & categories

    def test_input_pipeline_fields_share_that_category(self):
        by_key = {field.key: field for field in FIELD_METADATA}
        keys = {
            "voice_device", "sample_rate", "voice_min_energy", "wake_word",
            "wake_fuzzy_ratio", "whisper_model", "whisper_backend",
            "whisper_device", "whisper_compute_type", "whisper_vad",
            "whisper_min_confidence", "whisper_no_speech_threshold",
            "whisper_min_language_probability", "whisper_language",
            "vad_enabled", "vad_aggressiveness", "endpoint_silence_ms",
            "max_utterance_ms",
        }
        assert {by_key[key].category for key in keys} == {"speech_input"}

    def test_speaker_device_belongs_to_speech_output(self):
        field = next(item for item in FIELD_METADATA if item.key == "tts_output_device")
        assert field.category == "tts"


class TestCloudTTSField:
    def test_provider_chain_is_an_editable_structured_field(self):
        field = next(
            (item for item in FIELD_METADATA if item.key == "tts_cloud_providers"),
            None,
        )

        assert field is not None
        assert field.category == "tts"
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


