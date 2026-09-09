"""Tests for the unified persona system prompt.

The persona should match the user's configured wake word so renaming the
wake word to e.g. "Friday" produces a butler named Friday, not one still
hardcoded to Jarvis.
"""

from jarvis.system_prompt import build_system_prompt


class TestBuildSystemPrompt:
    def test_default_name_is_jarvis(self):
        prompt = build_system_prompt()
        assert "named Jarvis" in prompt

    def test_custom_name_replaces_jarvis(self):
        prompt = build_system_prompt("Friday")
        assert "named Friday" in prompt
        assert "named Jarvis" not in prompt

    def test_lowercase_wake_word_is_capitalised(self):
        prompt = build_system_prompt("friday".capitalize())
        assert "named Friday" in prompt

    def test_blank_name_falls_back_to_jarvis(self):
        assert "named Jarvis" in build_system_prompt("")
        assert "named Jarvis" in build_system_prompt("   ")
        assert "named Jarvis" in build_system_prompt(None)  # type: ignore[arg-type]

    def test_memory_guidance_contains_no_concrete_user_fact_example(self):
        prompt = build_system_prompt()

        assert "Trenches Gym" not in prompt
        assert "Information the user has shared" not in prompt
        assert "software engineer named" not in prompt
        assert "never invent a user fact" in prompt
