"""UTF-8 console setup must only mutate interpreter-owned streams."""

from __future__ import annotations

import gc
import io
import sys
from unittest.mock import patch


def test_console_fix_reconfigures_real_streams_in_place():
    from jarvis.console import force_utf8_console

    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    with (
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "__stdout__", stdout),
        patch.object(sys, "stderr", stderr),
        patch.object(sys, "__stderr__", stderr),
    ):
        force_utf8_console()
        assert sys.stdout is stdout
        assert sys.stderr is stderr

    gc.collect()
    assert not stdout.closed
    assert not stderr.closed
    assert stdout.encoding.lower().replace("-", "") == "utf8"
    assert stderr.encoding.lower().replace("-", "") == "utf8"


def test_console_fix_leaves_replaced_capture_streams_untouched():
    from jarvis.console import force_utf8_console

    captured_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    captured_stderr = io.StringIO()
    with (
        patch.object(sys, "stdout", captured_stdout),
        patch.object(sys, "stderr", captured_stderr),
    ):
        force_utf8_console()
        assert sys.stdout is captured_stdout
        assert sys.stderr is captured_stderr

    assert captured_stdout.encoding.lower() == "cp1252"
    assert not captured_stdout.closed
