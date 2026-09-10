"""Diagnostic view of what each subscription provider can currently do."""

from __future__ import annotations

import sys
from typing import Callable

from .base import (
    AuthStatus,
    HealthReport,
    ModelCatalog,
    NotSupported,
    ProviderAdapter,
    UsageSnapshot,
)
from .registry import get_provider, list_providers


UNKNOWN = "no answer from the provider"


def _describe_auth(status: AuthStatus) -> list[str]:
    if not status.logged_in:
        detail = status.detail or "not signed in to a subscription"
        return [f"  🔒 Auth      {detail}"]
    parts = [status.method or "unknown method"]
    if status.account:
        parts.append(status.account)
    if status.plan:
        parts.append(f"plan {status.plan}")
    return [f"  🔓 Auth      signed in: {', '.join(parts)}"]


def _describe_models(catalogue: ModelCatalog) -> list[str]:
    if not catalogue.models:
        reason = (
            "none confirmed yet" if not catalogue.enumerable else "provider returned none"
        )
        listing = "cannot list models" if not catalogue.enumerable else "can list models"
        return [f"  🧠 Models    {reason} ({listing})"]
    lines = [f"  🧠 Models    {len(catalogue.models)} known"]
    for model in catalogue.models[:5]:
        lines.append(f"       • {model.id} ({model.source})")
    if len(catalogue.models) > 5:
        lines.append(f"       • and {len(catalogue.models) - 5} more")
    return lines


def _describe_usage(snapshot: UsageSnapshot) -> list[str]:
    if not snapshot.available:
        return [f"  📊 Usage     not available: {snapshot.detail or UNKNOWN}"]
    figures = []
    if snapshot.remaining is not None and snapshot.limit is not None:
        figures.append(f"{snapshot.remaining:.0f} of {snapshot.limit:.0f} left")
    if snapshot.input_tokens is not None:
        figures.append(f"{snapshot.input_tokens} in / {snapshot.output_tokens} out")
    if snapshot.resets_at:
        figures.append(f"resets {snapshot.resets_at}")
    return [f"  📊 Usage     {', '.join(figures) if figures else snapshot.detail or UNKNOWN}"]


def _describe_health(report: HealthReport) -> list[str]:
    if not report.reachable:
        return [f"  💔 Health    unreachable: {report.detail or UNKNOWN}"]
    latency = f" in {report.latency_ms:.0f} ms" if report.latency_ms is not None else ""
    return [f"  💚 Health    reachable{latency}: {report.detail or 'no detail'}"]


def _describe_sessions(adapter: ProviderAdapter) -> list[str]:
    sessions = adapter.list_sessions()
    if isinstance(sessions, NotSupported):
        return [f"  🗂️  Sessions  not supported: {sessions.reason}"]
    owned = sum(1 for session in sessions if session.owned)
    return [f"  🗂️  Sessions  {len(sessions)} visible, {owned} owned by Jarvis"]


def _probe(label: str, read: Callable[[], list[str]]) -> list[str]:
    """Report what a provider says, or why it could not say anything."""

    try:
        return read()
    except Exception as error:  # noqa: BLE001 - a diagnostic never hides a failure
        return [f"  ⚠️  {label:<9} failed: {type(error).__name__}: {error}"]


def report(provider: str) -> list[str]:
    """Build the diagnostic lines for one provider."""

    lines = [f"🔌 {provider}"]
    try:
        adapter = get_provider(provider)
    except Exception as error:  # noqa: BLE001 - an absent CLI is a normal outcome
        lines.append(f"  ⚠️  Adapter   could not start: {type(error).__name__}: {error}")
        return lines

    lines.extend(_probe("Auth", lambda: _describe_auth(adapter.auth_status())))
    lines.extend(_probe("Models", lambda: _describe_models(adapter.list_models())))
    lines.extend(_probe("Usage", lambda: _describe_usage(adapter.usage())))
    lines.extend(_probe("Health", lambda: _describe_health(adapter.health())))
    lines.extend(_probe("Sessions", lambda: _describe_sessions(adapter)))
    close = getattr(adapter, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing must not mask the report
            pass
    return lines


def status(providers: tuple[str, ...] = ()) -> list[str]:
    """Build the diagnostic report for every requested provider."""

    chosen = providers or list_providers()
    lines = ["🛰️  Subscription provider status", ""]
    for provider in chosen:
        lines.extend(report(provider))
        lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        # A Windows console defaults to cp1252, which cannot encode the emojis.
        reconfigure(encoding="utf-8", errors="replace")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "status":
        print("🛰️  Usage: python -m jarvis.providers.cli status [provider ...]")
        return 2
    for line in status(tuple(arguments[1:])):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
