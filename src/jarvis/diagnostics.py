"""Dependency-light diagnostics and redacted JSONL event logging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Iterable
from uuid import uuid4


_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|token|password|secret)", re.I)
_BEARER = re.compile(r"\bBearer\s+[^\s]+", re.I)


def redact(value: Any) -> Any:
    """Recursively remove likely credentials from diagnostic values."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _BEARER.sub("Bearer [redacted]", value)
    return value


@dataclass(frozen=True)
class CapabilityStatus:
    name: str
    status: str
    cause: str = ""
    recommendation: str = ""
    correlation_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class DiagnosticReport:
    correlation_id: str
    created_at: str
    capabilities: tuple[CapabilityStatus, ...]

    @property
    def healthy(self) -> bool:
        return all(item.status == "available" for item in self.capabilities)

    def to_dict(self) -> dict[str, Any]:
        return redact(asdict(self))


class JsonlDiagnosticLogger:
    """Append safe metadata locally with bounded file rotation."""

    def __init__(self, path: str | Path, *, max_bytes: int = 1_000_000, backups: int = 3) -> None:
        self.path = Path(path)
        self.max_bytes = max(1, max_bytes)
        self.backups = max(0, backups)

    def _rotate(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
            for index in range(self.backups, 0, -1):
                suffix = "" if index == 1 else f".{index - 1}"
                source = self.path.with_suffix(self.path.suffix + suffix)
                target = self.path.with_suffix(self.path.suffix + f".{index}")
                if source.exists():
                    source.replace(target)
        except OSError:
            pass

    def event(self, event: str, **fields: Any) -> str:
        correlation_id = str(fields.pop("correlation_id", "") or uuid4().hex)
        row = redact({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "correlation_id": correlation_id,
            **fields,
        })
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass
        return correlation_id


def run_diagnostics(checks: Iterable[Callable[[], CapabilityStatus]]) -> DiagnosticReport:
    """Run independent checks without letting one failure hide the others."""
    report_id = uuid4().hex
    statuses: list[CapabilityStatus] = []
    for check in checks:
        try:
            status = check()
        except Exception as error:
            status = CapabilityStatus(
                name=getattr(check, "__name__", "unknown"),
                status="unavailable",
                cause=type(error).__name__,
                recommendation="Check the diagnostic log and configuration.",
                correlation_id=report_id,
            )
        statuses.append(status)
    return DiagnosticReport(
        report_id,
        datetime.now(timezone.utc).isoformat(),
        tuple(statuses),
    )


def run_local_diagnostics(settings: Any) -> DiagnosticReport:
    """Check local configuration without starting devices or subprocesses."""
    def provider() -> CapabilityStatus:
        from .llm.factory import get_llm_backend

        try:
            get_llm_backend(settings)
            return CapabilityStatus(
                "llm_chat", "available",
                recommendation="Use 'Test provider' for a live request.",
            )
        except ValueError as error:
            return CapabilityStatus(
                "llm_chat", "invalid_config", str(error),
                "Choose a supported provider and model.",
            )

    def screen() -> CapabilityStatus:
        if not shutil.which("tesseract"):
            return CapabilityStatus(
                "screen_ocr", "unsupported", "Tesseract is not on PATH.",
                "Install Tesseract, then run diagnostics again.",
            )
        return CapabilityStatus("screen_ocr", "available")

    def mcps() -> CapabilityStatus:
        from .tools.external.mcp_preflight import preflight_mcp_config

        failures = []
        for name, config in (getattr(settings, "mcps", {}) or {}).items():
            result = preflight_mcp_config(config) if isinstance(config, dict) else None
            if result is None or not result.available:
                failures.append(f"{name}: {result.reason if result else 'invalid configuration'}")
        if failures:
            return CapabilityStatus(
                "mcp", "invalid_config", "; ".join(failures),
                "Fix or disable the listed MCP servers.",
            )
        return CapabilityStatus("mcp", "available")

    return run_diagnostics((provider, screen, mcps))


def export_redacted_report(report: DiagnosticReport, destination: str | Path) -> Path:
    """Write a support-safe report without prompts, logs, or secrets."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
