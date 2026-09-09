"""The AGPL-3.0 Kokoro code stays on its own side of a process boundary.

jarvis.output.vendor.kokoro_backtalk (vendored from backtalk) and the
kokoro package it wraps must only ever be imported by
jarvis.output.vendor.kokoro_sidecar, the subprocess entry point. Everything
else in the main daemon process, including jarvis.output.tts.KokoroTTS and
jarvis.output.kokoro_sidecar_client, reaches Kokoro only over the sidecar's
stdio pipe. A static source scan catches a regression here even if a test
that happens to mock the sidecar client would not.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "jarvis"

# The only files allowed to import kokoro_backtalk / the kokoro package: the
# vendored module itself, and the sidecar entry point that runs it. Both
# live on the sidecar side of the process boundary.
ALLOWED_IMPORTERS = {
    SRC / "output" / "vendor" / "kokoro_backtalk.py",
    SRC / "output" / "vendor" / "kokoro_sidecar.py",
}
ALLOWED_IMPORTER = SRC / "output" / "vendor" / "kokoro_sidecar.py"  # the entry point, for the sanity check below


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # `from .kokoro_backtalk import x` inside vendor/ resolves with
            # a leading dot and no module prefix beyond the name itself;
            # ast gives us "kokoro_backtalk" directly in that case already.
    return names


def _mentions_kokoro_backtalk_or_package(names: set[str]) -> bool:
    return any(
        name == "kokoro" or name.startswith("kokoro.") or "kokoro_backtalk" in name
        for name in names
    )


class TestKokoroStaysBehindTheSidecarBoundary:
    def test_only_the_sidecar_entry_point_imports_kokoro_backtalk_or_kokoro(self):
        offenders = []
        for path in SRC.rglob("*.py"):
            if path in ALLOWED_IMPORTERS:
                continue
            if _mentions_kokoro_backtalk_or_package(_imported_names(path)):
                offenders.append(str(path.relative_to(SRC.parent.parent)))

        assert not offenders, (
            f"these files import kokoro_backtalk or the kokoro package "
            f"directly, outside the sidecar process boundary: {offenders}"
        )

    def test_the_sidecar_entry_point_itself_imports_kokoro_backtalk(self):
        """A sanity check on the scan above: the allowed importer really
        does import it, so a rename would fail this test rather than
        silently making the boundary test above vacuous."""
        names = _imported_names(ALLOWED_IMPORTER)
        assert _mentions_kokoro_backtalk_or_package(names)

    def test_kokoro_tts_module_does_not_import_kokoro_backtalk(self):
        names = _imported_names(SRC / "output" / "tts.py")
        assert not _mentions_kokoro_backtalk_or_package(names)

    def test_the_sidecar_client_does_not_import_kokoro_backtalk(self):
        names = _imported_names(SRC / "output" / "kokoro_sidecar_client.py")
        assert not _mentions_kokoro_backtalk_or_package(names)
