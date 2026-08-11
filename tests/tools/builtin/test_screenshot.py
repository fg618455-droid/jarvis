"""Tests for screenshot tool."""

import pytest
from unittest.mock import Mock, patch
import sys

from src.jarvis.tools.builtin.screenshot import ScreenshotTool
from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.types import ToolExecutionResult


class TestScreenshotTool:
    """Test screenshot tool functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = ScreenshotTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_tool_properties(self):
        """Test tool metadata properties."""
        assert self.tool.name == "screenshot"
        assert "capture" in self.tool.description.lower()
        assert self.tool.inputSchema["type"] == "object"
        assert self.tool.inputSchema["required"] == []

    @patch('src.jarvis.tools.builtin.screenshot._capture_to', return_value=(True, ''))
    @patch('shutil.which')
    def test_run_success(self, mock_which, _capture):
        """Test successful screenshot capture with inlined OCR logic."""
        # Lightweight stubs so dynamic imports succeed without heavy deps.
        # Saved and restored so the stubs don't leak into later tests that
        # import the real pytesseract / PIL modules.
        _saved_modules = {name: sys.modules.get(name) for name in
                          ("pytesseract", "PIL", "PIL.Image")}
        class _StubImgCtx:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        class _StubImage:
            @staticmethod
            def open(*a, **k):
                return _StubImgCtx()

        sys.modules['pytesseract'] = type('StubTess', (), {
            'image_to_string': staticmethod(lambda *a, **k: 'Sample OCR text')
        })
        sys.modules['PIL'] = type('StubPIL', (), {'Image': _StubImage})
        sys.modules['PIL.Image'] = _StubImage

        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "tesseract" else None

        try:
            result = self.tool.run({}, self.context)
        finally:
            for _name, _saved in _saved_modules.items():
                if _saved is None:
                    sys.modules.pop(_name, None)
                else:
                    sys.modules[_name] = _saved

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert result.reply_text == 'Sample OCR text'
        self.context.user_print.assert_called()

    @patch('shutil.which', return_value=None)
    def test_run_without_ocr_dependency_reports_unsupported(self, _which):
        """A missing OCR binary must never be reported as successful capture."""
        result = self.tool.run({}, self.context)
        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert result.error_code == "unsupported"
