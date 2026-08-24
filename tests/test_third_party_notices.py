"""THIRD_PARTY_NOTICES.md must name every vendored AGPL component.

Two pieces of AGPL-3.0 code are vendored into Jarvis: the ai-visualizer face
and the Kokoro half of backtalk's mouth. Both must be named, with their
licence, so a build that ships them is honest about what it is conveying.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"


class TestThirdPartyNotices:
    def test_the_notices_file_exists(self):
        assert NOTICES_PATH.is_file()

    def test_ai_visualizer_is_named(self):
        text = NOTICES_PATH.read_text(encoding="utf-8")
        assert "ai-visualizer" in text
        assert "jaredrhod/ai-visualizer" in text

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

    def test_the_vendored_visualizer_files_exist_on_disk(self):
        vendor = REPO_ROOT / "src" / "jarvis" / "webui" / "visualizer" / "vendor"
        assert (vendor / "index.html").is_file()
        assert (vendor / "core.js").is_file()
        for face in ("board", "radial", "rain", "neural"):
            assert (vendor / "faces" / face / "index.html").is_file()

    def test_the_vendored_kokoro_module_exists_on_disk(self):
        module = (
            REPO_ROOT / "src" / "jarvis" / "output" / "vendor" / "kokoro_backtalk.py"
        )
        assert module.is_file()
        assert "AGPL-3.0-or-later" in module.read_text(encoding="utf-8")
