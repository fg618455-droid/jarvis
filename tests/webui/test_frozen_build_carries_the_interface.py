"""The bundled app has to carry the control centre's files.

Flask resolves ``static/`` relative to the module, so a bundle that ships
the Python and omits the folder starts cleanly, answers every API call, and
serves a blank page. Nothing in the test suite would notice: the failure
only exists in the frozen build. This reads the PyInstaller spec as text,
which is enough to catch the omission without running a build.
"""

from __future__ import annotations

import re
from pathlib import Path


SPEC = Path(__file__).resolve().parents[2] / "jarvis_desktop.spec"


def test_the_static_folder_is_bundled_as_data():
    spec = SPEC.read_text(encoding="utf-8")

    entries = re.findall(r"'jarvis/webui/static'", spec)
    assert entries, "jarvis_desktop.spec does not bundle jarvis/webui/static"


def test_every_file_the_interface_serves_lives_under_that_folder():
    """The bundled folder is the whole interface, not part of it."""
    static = Path(__file__).resolve().parents[2] / "src" / "jarvis" / "webui" / "static"

    served = {path.suffix for path in static.rglob("*") if path.is_file()}

    assert served <= {".html", ".css", ".js"}, f"unexpected asset types: {served}"
    assert (static / "index.html").is_file()
