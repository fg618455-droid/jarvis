"""The desktop package keeps optional GUI imports lazy."""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


def test_package_import_does_not_load_the_qt_application():
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, desktop_app; "
            "raise SystemExit('desktop_app.app' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr


def test_public_desktop_exports_resolve_lazily():
    import desktop_app
    from desktop_app.app import ControlCentreWindow

    assert desktop_app.ControlCentreWindow is ControlCentreWindow
