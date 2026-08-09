"""Behaviour tests for stripping the context line out of spoken replies.

The engine appends a `[Context: ...]` line to the system message so the model
knows the current time and location. Small models frequently copy that line
verbatim into their answer, which then gets read aloud. The scrub removes it.
"""

import pytest

from jarvis.reply.engine import strip_context_echo


class TestStripContextEcho:
    def test_trailing_echo_is_removed(self):
        reply = (
            "Gern geschehen. Noch etwas für Sie?\n\n"
            "[Context: Current local time: Sunday, August 09, 2026 at 11:45 "
            "W. Europe Daylight Time.]"
        )

        assert strip_context_echo(reply) == "Gern geschehen. Noch etwas für Sie?"

    def test_inline_echo_on_one_line_is_removed(self):
        reply = "Es ist 11:45 Uhr. [Context: Current local time: 11:45.]"

        assert strip_context_echo(reply) == "Es ist 11:45 Uhr."

    def test_multiple_echoes_are_removed(self):
        reply = "Alles klar. [Context: a] [Context: b]"

        assert strip_context_echo(reply) == "Alles klar."

    def test_multiline_echo_is_removed(self):
        reply = "Hallo.\n\n[Context: Current local time: 11:45.\nLocation: Bayern.]"

        assert strip_context_echo(reply) == "Hallo."

    def test_reply_without_echo_is_untouched(self):
        reply = "Morgen wird es sonnig, ungefähr 22 Grad."

        assert strip_context_echo(reply) == reply

    def test_unrelated_brackets_are_preserved(self):
        """Only our own marker is stripped, not any bracketed text."""
        reply = "Der Kurs steht bei 42 [Quelle: Tagesschau]."

        assert strip_context_echo(reply) == reply

    def test_echo_only_reply_keeps_original(self):
        """Never turn a reply into nothing; an empty answer is worse than a leak."""
        reply = "[Context: Current local time: 11:45.]"

        assert strip_context_echo(reply) == reply

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_input_is_passed_through(self, value):
        assert strip_context_echo(value) == value
