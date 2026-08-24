"""Tests for fetch web page tool."""

import pytest
from unittest.mock import Mock, patch
import requests

from src.jarvis.tools.builtin.fetch_web_page import FetchWebPageTool
from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.types import ToolExecutionResult


def _make_response_mock(**attrs) -> Mock:
    """Build a Mock that doubles as a requests response.

    Defaults to "not a redirect" with empty headers so tests that don't
    care about redirects don't have to spell that out — a bare ``Mock()``
    attribute is truthy, which would otherwise make every response look
    like a redirect to the manual redirect-walk in the tool under test.
    """
    defaults = {"is_redirect": False, "is_permanent_redirect": False, "headers": {}}
    defaults.update(attrs)
    resp = Mock(**defaults)
    resp.__enter__ = Mock(return_value=resp)
    resp.__exit__ = Mock(return_value=False)
    return resp


class TestFetchWebPageTool:
    """Test fetch web page tool functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = FetchWebPageTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_tool_properties(self):
        """Test tool metadata properties."""
        assert self.tool.name == "fetchWebPage"
        assert "fetch" in self.tool.description.lower()
        assert self.tool.inputSchema["type"] == "object"
        assert "url" in self.tool.inputSchema["required"]

    def test_run_no_args(self):
        """Test fetch web page with no arguments."""
        result = self.tool.run(None, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "url" in result.reply_text.lower()

    def test_run_empty_url(self):
        """Test fetch web page with empty URL."""
        args = {"url": ""}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "url" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_success(self, mock_get):
        """Test successful web page fetch."""
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            content=b'<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            headers={'content-type': 'text/html'},
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        args = {"url": "https://example.com"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "example.com" in result.reply_text
        self.context.user_print.assert_called()

    @patch('requests.get')
    def test_run_success_without_beautifulsoup(self, mock_get):
        """Test successful web page fetch without BeautifulSoup."""
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><body>Raw content</body></html>',
            content=b'<html><body>Raw content</body></html>',
            headers={'content-type': 'text/html'},
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        with patch('builtins.__import__', side_effect=ImportError):
            args = {"url": "https://example.com"}
            result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "raw content" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_http_error(self, mock_get):
        """Test fetch web page with HTTP error."""
        mock_response = _make_response_mock(status_code=404)
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        args = {"url": "https://example.com/notfound"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "failed to fetch" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_request_error(self, mock_get):
        """Test fetch web page with network error."""
        mock_get.side_effect = requests.exceptions.RequestException("Network error")

        args = {"url": "https://example.com"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "failed to fetch" in result.reply_text.lower()

    def test_run_invalid_url(self):
        """Test fetch web page with invalid URL."""
        args = {"url": "not-a-url"}
        result = self.tool.run(args, self.context)
        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "resolved" in result.reply_text.lower() or "url" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_with_links_extraction(self, mock_get):
        """Test fetch web page including link extraction when include_links=True."""
        html = (
            '<html><head><title>Links Page</title></head>'
            '<body><p>Intro</p>'
            '<a href="/relative">Relative Link</a>'
            '<a href="https://absolute.test/page">Absolute Link</a>'
            '<a href="mailto:test@example.com">Mail</a>'
            '</body></html>'
        )
        mock_response = _make_response_mock(
            status_code=200,
            text=html,
            content=html.encode(),
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        args = {"url": "https://example.com", "include_links": True}
        result = self.tool.run(args, self.context)
        assert result.success is True
        assert isinstance(result, ToolExecutionResult)
        assert "Links found on page" in result.reply_text
        # relative link should be resolved to absolute
        assert "https://example.com/relative" in result.reply_text
        assert "absolute.test" in result.reply_text


class TestRedirectValidation:
    """SSRF: a redirect hop must be validated before it is requested.

    ``allow_redirects=True`` hands redirect-following to the requests
    library itself, so any hop between the first request and the final
    response has already been connected to by the time our code gets a
    chance to inspect it. The fetch must walk redirects manually
    (``allow_redirects=False``) and re-validate every hop's target before
    following it — the same pattern ``webSearch`` already uses.
    """

    def setup_method(self):
        self.tool = FetchWebPageTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    @patch('requests.get')
    def test_redirects_are_not_delegated_to_requests(self, mock_get):
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            content=b'<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        self.tool.run({"url": "https://example.com"}, self.context)

        assert mock_get.call_args.kwargs.get("allow_redirects") is False

    @patch('requests.get')
    def test_a_redirect_to_a_public_host_is_followed_to_its_content(self, mock_get):
        hop1 = _make_response_mock(
            status_code=302,
            is_redirect=True,
            headers={"Location": "https://1.1.1.1/final"},
        )
        hop2 = _make_response_mock(
            status_code=200,
            text='<html><head><title>Final</title></head><body><p>Landed here</p></body></html>',
            content=b'<html><head><title>Final</title></head><body><p>Landed here</p></body></html>',
            raise_for_status=Mock(),
            url="https://1.1.1.1/final",
        )
        mock_get.side_effect = [hop1, hop2]

        result = self.tool.run({"url": "https://example.com"}, self.context)

        assert result.success is True
        assert "Landed here" in result.reply_text
        assert mock_get.call_count == 2
        assert mock_get.call_args_list[1].args[0] == "https://1.1.1.1/final"

    @patch('requests.get')
    def test_a_redirect_to_a_private_address_is_refused_before_being_requested(self, mock_get):
        hop1 = _make_response_mock(
            status_code=302,
            is_redirect=True,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )
        # Only one response is queued. If the fix regresses and the tool
        # follows the disallowed hop anyway, the mock raises StopIteration
        # instead of silently connecting — proving the second request was
        # never made rather than just that its content was discarded.
        mock_get.side_effect = [hop1]

        result = self.tool.run({"url": "https://example.com"}, self.context)

        assert result.success is False
        assert mock_get.call_count == 1
        assert "not allowed" in result.reply_text.lower()

    @patch('requests.get')
    def test_a_redirect_loop_gives_up_instead_of_hanging(self, mock_get):
        from src.jarvis.tools.builtin.fetch_web_page import _MAX_REDIRECTS

        loop_hop = _make_response_mock(
            status_code=302,
            is_redirect=True,
            headers={"Location": "https://example.com/loop"},
        )
        mock_get.side_effect = [loop_hop] * (_MAX_REDIRECTS + 1)

        result = self.tool.run({"url": "https://example.com"}, self.context)

        assert mock_get.call_count == _MAX_REDIRECTS + 1
        assert result.success is False
