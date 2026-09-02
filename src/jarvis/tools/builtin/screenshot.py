"""Cross-platform screenshot OCR with explicit, honest outcomes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult


def _capture_to(path: str) -> tuple[bool, str]:
    """Capture one screen to *path*, returning a user-safe failure reason."""
    if sys.platform == "darwin":
        binary = shutil.which("screencapture")
        if not binary:
            return False, "macOS screen capture is not available."
        completed = subprocess.run([binary, "-i", path], capture_output=True, timeout=45)
        if completed.returncode:
            return False, "Screen capture was cancelled or macOS denied Screen Recording permission."
        return os.path.exists(path), "No screenshot was created."

    # Pillow's ImageGrab is the preferred Windows path and also supports most
    # X11 desktops.  Import it lazily so a missing optional capture dependency
    # does not prevent the rest of Jarvis from starting.
    try:
        from PIL import ImageGrab  # type: ignore
        image = ImageGrab.grab()
        image.save(path, "PNG")
        return True, ""
    except PermissionError:
        return False, "Screen capture permission was denied by the operating system."
    except Exception as image_error:
        if sys.platform.startswith("linux"):
            for command in (("gnome-screenshot", "-f", path), ("scrot", path)):
                binary = shutil.which(command[0])
                if not binary:
                    continue
                try:
                    completed = subprocess.run([binary, *command[1:]], capture_output=True, timeout=30)
                    if completed.returncode == 0 and os.path.exists(path):
                        return True, ""
                except Exception:
                    continue
        return False, f"No supported screen capture adapter is available ({type(image_error).__name__})."


class ScreenshotTool(Tool):
    """Capture a screen and return OCR text; this is not visual object detection."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return "Capture screen text with OCR. Use only if readable screen text will materially help."

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        context.user_print("📸 Capturing a screenshot for OCR…")
        if not shutil.which("tesseract"):
            context.user_print("⚠️ OCR is not installed.")
            return ToolExecutionResult.failure(
                ToolErrorCode.UNSUPPORTED,
                "Screen OCR is unavailable because Tesseract is not installed.",
                phase="preflight",
                technical_details="tesseract executable was not found on PATH",
            )
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:
            return ToolExecutionResult.failure(
                ToolErrorCode.UNSUPPORTED,
                "Screen OCR dependencies are not installed.", phase="preflight",
                technical_details=type(exc).__name__,
            )

        with tempfile.TemporaryDirectory(prefix="jarvis_ocr_") as tmpdir:
            image_path = str(Path(tmpdir) / "shot.png")
            try:
                captured, reason = _capture_to(image_path)
            except subprocess.TimeoutExpired:
                return ToolExecutionResult.failure(
                    ToolErrorCode.TIMEOUT, "Screen capture timed out.", retryable=True,
                    technical_details="capture adapter timed out",
                )
            if not captured:
                code = ToolErrorCode.PERMISSION_DENIED if "permission" in reason.lower() else ToolErrorCode.UNAVAILABLE
                context.user_print("⚠️ Screenshot could not be captured.")
                return ToolExecutionResult.failure(code, reason, phase="capture", technical_details=reason)
            try:
                with Image.open(image_path) as image:
                    text = (pytesseract.image_to_string(image) or "").strip()
            except PermissionError as exc:
                return ToolExecutionResult.failure(ToolErrorCode.PERMISSION_DENIED,
                    "OCR could not read the captured screen.", technical_details=type(exc).__name__)
            except Exception as exc:
                return ToolExecutionResult.failure(ToolErrorCode.EXECUTION_FAILED,
                    "OCR failed while reading the screenshot.", technical_details=type(exc).__name__)

        if not text:
            context.user_print("⚠️ No readable text was found on the captured screen.")
            return ToolExecutionResult.failure(
                ToolErrorCode.EXECUTION_FAILED,
                "The screenshot was captured, but OCR found no readable text.",
                phase="ocr", technical_details="empty OCR output",
            )
        debug_log(f"screenshot: ocr_chars={len(text)}", "screenshot")
        context.user_print("✅ Screen text captured.")
        return ToolExecutionResult(success=True, reply_text=text, phase="ocr")
