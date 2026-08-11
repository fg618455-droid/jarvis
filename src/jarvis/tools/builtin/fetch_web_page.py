"""Safe, bounded web-page fetching for tool use.

Web pages are untrusted input.  The fetch policy blocks local/private targets,
re-checks every redirect, and limits both time and bytes before extraction.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolErrorCode, ToolExecutionResult

_MAX_BYTES = 2_000_000
_TIMEOUT = (5, 15)


def _validate_public_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute http or https address.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    hostname = parsed.hostname.rstrip(".")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("The host name could not be resolved.") from exc
    if not addresses:
        raise ValueError("The host name did not resolve to a public address.")
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        if not address.is_global:
            raise ValueError("Local, private, or reserved network targets are not allowed.")
    return value


def _read_limited(response: requests.Response) -> bytes:
    response_headers = getattr(response, "headers", {})
    header = response_headers.get("content-length", "") if hasattr(response_headers, "get") else ""
    try:
        if header and int(header) > _MAX_BYTES:
            raise ValueError("The page is larger than the 2 MB safety limit.")
    except ValueError:
        raise
    except Exception:
        pass
    chunks: list[bytes] = []
    total = 0
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        try:
            for chunk in iterator(chunk_size=64 * 1024):
                if not isinstance(chunk, bytes):
                    continue
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise ValueError("The page exceeded the 2 MB safety limit.")
                chunks.append(chunk)
        except TypeError:
            # Lightweight test/custom response adapters may expose a mock
            # method rather than an iterable; use their content fallback.
            chunks = []
        if chunks:
            return b"".join(chunks)
    # Compatibility for lightweight mocked responses and older adapters.
    content = response.content
    if len(content) > _MAX_BYTES:
        raise ValueError("The page exceeded the 2 MB safety limit.")
    return content


class FetchWebPageTool(Tool):
    @property
    def name(self) -> str:
        return "fetchWebPage"

    @property
    def description(self) -> str:
        return "Fetch a public web page and extract untrusted text content."

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {
            "url": {"type": "string", "description": "Public http(s) URL to fetch"},
            "include_links": {"type": "boolean", "description": "Include public page links"},
        }, "required": ["url"]}

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        context.user_print("🌐 Fetching page content…")
        if not isinstance(args, dict) or not str(args.get("url", "")).strip():
            return ToolExecutionResult.failure(ToolErrorCode.INVALID_ARGUMENT,
                "fetchWebPage requires a public URL.", phase="validation")
        url = str(args["url"]).strip()
        if "://" not in url:
            url = "https://" + url
        include_links = bool(args.get("include_links", False))
        try:
            _validate_public_url(url)
        except ValueError as exc:
            return ToolExecutionResult.failure(ToolErrorCode.INVALID_ARGUMENT, str(exc), phase="validation")

        headers = {
            "User-Agent": "Jarvis/1.0 safe-page-fetcher",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.5",
        }
        try:
            debug_log(f"fetchWebPage: fetching host={urlparse(url).hostname}", "web")
            with requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True, stream=True) as response:
                # Verify every redirect target, not just the final destination.
                history = getattr(response, "history", [])
                if not isinstance(history, (list, tuple)):
                    history = []
                for hop in [*history, response]:
                    hop_url = getattr(hop, "url", "")
                    if isinstance(hop_url, str) and hop_url:
                        _validate_public_url(hop_url)
                response.raise_for_status()
                content_bytes = _read_limited(response)
                final_url = getattr(response, "url", url)
                if not isinstance(final_url, str):
                    final_url = url
        except requests.exceptions.Timeout as exc:
            return ToolExecutionResult.failure(ToolErrorCode.TIMEOUT, "Fetching the page timed out.",
                retryable=True, technical_details=type(exc).__name__)
        except requests.exceptions.RequestException as exc:
            return ToolExecutionResult.failure(ToolErrorCode.UNAVAILABLE, "Failed to fetch the page.",
                retryable=True, technical_details=type(exc).__name__)
        except ValueError as exc:
            return ToolExecutionResult.failure(ToolErrorCode.INVALID_ARGUMENT, str(exc), phase="validation")

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content_bytes, "html.parser")
            for element in soup(["script", "style", "meta", "link", "noscript"]):
                element.decompose()
            title_tag = soup.find("title")
            title = title_tag.get_text(" ", strip=True) if title_tag else ""
            lines = [line.strip() for line in soup.get_text("\n").splitlines() if len(line.strip()) > 3]
            content = "\n".join(dict.fromkeys(lines))[:50_000]
            parts = ([f"**Title:** {title}"] if title else []) + [
                f"**URL:** {final_url}",
                "**Untrusted web content:**\n" + content,
            ]
            if include_links:
                links = []
                for link in soup.find_all("a", href=True):
                    href, label = link["href"].strip(), link.get_text(" ", strip=True)
                    resolved = urljoin(final_url, href)
                    if label and len(label) > 3 and urlparse(resolved).scheme in {"http", "https"}:
                        links.append(f"• {label}: {resolved}")
                if links:
                    parts.append("**Links found on page:**\n" + "\n".join(links[:20]))
            reply = "\n\n".join(parts)
        except ImportError:
            reply = f"**URL:** {final_url}\n**Untrusted raw content:**\n" + content_bytes.decode("utf-8", errors="replace")[:10_000]
        except Exception as exc:
            return ToolExecutionResult.failure(ToolErrorCode.EXECUTION_FAILED,
                "The page was fetched but could not be extracted.", technical_details=type(exc).__name__)

        if not content_bytes or not reply.strip():
            return ToolExecutionResult.failure(ToolErrorCode.EXECUTION_FAILED,
                "The page returned no usable content.", technical_details="empty response")
        context.user_print("✅ Page content fetched.")
        return ToolExecutionResult(success=True, reply_text=reply, phase="extraction")
