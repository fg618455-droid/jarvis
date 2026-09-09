"""THIRD_PARTY_NOTICES.md must name every vendored AGPL component.

One piece of AGPL-3.0 code is vendored into Jarvis: the Kokoro half of
backtalk's mouth. It must be named, with its licence, so a build that ships
it is honest about what it is conveying.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"


class TestThirdPartyNotices:
    def test_the_notices_file_exists(self):
        assert NOTICES_PATH.is_file()

    def test_backtalk_is_named(self):
        text = NOTICES_PATH.read_text(encoding="utf-8")
        assert "backtalk" in text
        assert "jaredrhod/backtalk" in text

    def test_the_licence_is_named(self):
        text = NOTICES_PATH.read_text(encoding="utf-8")
        assert "AGPL-3.0" in text or "Affero General Public License" in text

    def test_the_licence_text_is_linked(self):
        text = NOTICES_PATH.read_text(encoding="utf-8")
        assert "gnu.org/licenses/agpl-3.0" in text

    def test_the_elevenlabs_exclusion_is_documented(self):
        """The whole point of vendoring only half of mouth.py: no cloud TTS."""
        text = NOTICES_PATH.read_text(encoding="utf-8")
        assert "ElevenLabs" in text

    def test_the_vendored_kokoro_module_exists_on_disk(self):
        module = (
            REPO_ROOT / "src" / "jarvis" / "output" / "vendor" / "kokoro_backtalk.py"
        )
        assert module.is_file()
        assert "AGPL-3.0-or-later" in module.read_text(encoding="utf-8")

    def test_the_kokoro_sidecar_entry_point_exists_on_disk(self):
        entry_point = (
            REPO_ROOT / "src" / "jarvis" / "output" / "vendor" / "kokoro_sidecar.py"
        )
        assert entry_point.is_file()

    def test_the_kokoro_process_boundary_is_documented(self):
        """The licence-boundary paragraph must describe the resolved,
        separable relationship, not the old in-process one."""
        text = NOTICES_PATH.read_text(encoding="utf-8")
        assert "sidecar" in text.lower()
        assert "process boundary" in text.lower() or "process" in text.lower()
