"""
Tests for choosing which sound card Jarvis speaks through.

PortAudio lists the same physical device once per host API, and its idea of
"default" can be a host API that accepts the stream, reports success and
plays into something nobody can hear. Speech is then produced correctly and
silently lost, with no error anywhere. Naming the device is the cure, so
resolving that name has to be predictable.
"""

from unittest.mock import MagicMock, patch

import pytest

from jarvis.output.tts import _resolve_output_device


def _devices():
    """The shape query_devices returns: outputs repeated per host API."""
    return [
        {"name": "Mikrofon (ZTD39 Device)", "max_output_channels": 0},
        {"name": "Lautsprecher (Realtek(R) Audio)", "max_output_channels": 2},
        {"name": "Kopfhörer (Wireless Controller)", "max_output_channels": 2},
    ]


class TestResolvingTheOutputDevice:
    @pytest.mark.parametrize("unset", [None, "", "default", "system"])
    def test_an_unset_setting_leaves_the_choice_to_portaudio(self, unset):
        """Empty keeps the old behaviour rather than guessing a device."""
        assert _resolve_output_device(unset) is None

    def test_an_index_is_used_as_given(self):
        """Host APIs are distinguished only by index, so indices must pass through."""
        assert _resolve_output_device("14") == 14
        assert _resolve_output_device(" 14 ") == 14

    def test_a_name_matches_an_output_device(self):
        with patch("sounddevice.query_devices", return_value=_devices()):
            assert _resolve_output_device("Realtek") == 1
            assert _resolve_output_device("wireless controller") == 2

    def test_an_input_only_device_never_matches(self):
        """Speaking into the microphone's own index would be silent again."""
        with patch("sounddevice.query_devices", return_value=_devices()):
            assert _resolve_output_device("ZTD39") is None

    def test_an_unmatched_name_falls_back_rather_than_failing(self, capsys):
        """Speech on the wrong card beats no speech, but say so once."""
        with patch("sounddevice.query_devices", return_value=_devices()):
            assert _resolve_output_device("Nonexistent Speakers") is None
        assert "Nonexistent Speakers" in capsys.readouterr().out

    def test_a_broken_audio_stack_does_not_stop_speech(self):
        """query_devices raising must not take the assistant down with it."""
        with patch("sounddevice.query_devices", side_effect=OSError("PortAudio gone")):
            assert _resolve_output_device("Realtek") is None


class TestTheStreamHonoursTheSetting:
    def test_the_configured_device_reaches_the_output_stream(self):
        """The setting is worthless unless it arrives where the sound does."""
        from jarvis.output.tts import PiperTTS

        with patch("sounddevice.query_devices", return_value=_devices()):
            engine = PiperTTS(enabled=False, output_device="Realtek")

        assert engine._output_device == 1
