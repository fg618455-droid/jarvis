from __future__ import annotations

from typing import Any, Iterator

import pytest

from jarvis.providers import cli
from jarvis.providers.base import (
    AuthStatus,
    Capabilities,
    HealthReport,
    ModelInfo,
    NotSupported,
    RunEvent,
    RunHandle,
    RunSpec,
    SessionInfo,
    UsageSnapshot,
)
from jarvis.providers.models import ModelCatalog
from jarvis.providers.registry import get_provider, list_providers


class FakeAdapter:
    """An adapter whose every answer the test decides."""

    def __init__(
        self,
        *,
        auth: AuthStatus | None = None,
        catalogue: ModelCatalog | None = None,
        usage: UsageSnapshot | None = None,
        health: HealthReport | None = None,
        sessions: Any = None,
        failing: str | None = None,
    ) -> None:
        self._auth = auth or AuthStatus()
        self._catalogue = catalogue or ModelCatalog(enumerable=False)
        self._usage = usage or UsageSnapshot()
        self._health = health or HealthReport()
        self._sessions = sessions if sessions is not None else []
        self._failing = failing
        self.closed = False

    def _maybe_fail(self, name: str) -> None:
        if self._failing == name:
            raise RuntimeError(f"{name} exploded")

    def id(self) -> str:
        return "fake"

    def auth_status(self) -> AuthStatus:
        self._maybe_fail("auth_status")
        return self._auth

    def list_models(self) -> ModelCatalog:
        self._maybe_fail("list_models")
        return self._catalogue

    def capabilities(self, model: str | None) -> Capabilities:
        return Capabilities()

    def start_run(self, spec: RunSpec) -> RunHandle:
        raise NotImplementedError

    def stream(self, run: RunHandle) -> Iterator[RunEvent]:
        raise NotImplementedError

    def steer(self, run: RunHandle, text: str) -> bool:
        return False

    def interrupt(self, run: RunHandle) -> bool:
        return False

    def resume(self, session_id: str) -> NotSupported:
        return NotSupported("resume", "not here")

    def fork(self, session_id: str) -> NotSupported:
        return NotSupported("fork", "not here")

    def list_sessions(self) -> Any:
        self._maybe_fail("list_sessions")
        return self._sessions

    def usage(self) -> UsageSnapshot:
        self._maybe_fail("usage")
        return self._usage

    def health(self) -> HealthReport:
        self._maybe_fail("health")
        return self._health

    def close(self) -> None:
        self.closed = True


def install(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> None:
    monkeypatch.setattr(cli, "get_provider", lambda provider: adapter)
    monkeypatch.setattr(cli, "list_providers", lambda: ("fake",))


@pytest.mark.unit
def test_the_registry_builds_every_known_provider() -> None:
    assert set(list_providers()) == {"claude", "codex", "hermes"}
    for provider in list_providers():
        assert get_provider(provider).id() == provider


@pytest.mark.unit
def test_an_unknown_provider_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="Unsupported provider: gpt5"):
        get_provider("gpt5")


@pytest.mark.unit
def test_absent_evidence_is_reported_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing known must read as nothing known, never as a zero or a guess."""

    install(monkeypatch, FakeAdapter())

    output = "\n".join(cli.status())

    assert "not signed in to a subscription" in output
    assert "none confirmed yet (cannot list models)" in output
    assert "Usage     not available" in output
    assert "Health    unreachable" in output


@pytest.mark.unit
def test_real_readings_are_reported_with_their_source(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        FakeAdapter(
            auth=AuthStatus(logged_in=True, method="claude.ai", account="a@b.c", plan="pro"),
            catalogue=ModelCatalog(
                enumerable=True, models=(ModelInfo("m-1", "api"), ModelInfo("m-2", "verified-probe"))
            ),
            usage=UsageSnapshot(available=True, limit=100.0, remaining=42.0),
            health=HealthReport(reachable=True, latency_ms=12.4, detail="up"),
            sessions=[SessionInfo("s-1", "codex", owned=True), SessionInfo("s-2", "codex")],
        ),
    )

    output = "\n".join(cli.status())

    assert "signed in: claude.ai, a@b.c, plan pro" in output
    assert "m-1 (api)" in output
    assert "m-2 (verified-probe)" in output
    assert "42 of 100 left" in output
    assert "reachable in 12 ms" in output
    assert "2 visible, 1 owned by Jarvis" in output


@pytest.mark.unit
@pytest.mark.parametrize(
    "failing", ["auth_status", "list_models", "usage", "health", "list_sessions"]
)
def test_one_broken_reading_does_not_hide_the_others(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """A diagnostic that dies on the first error diagnoses nothing."""

    install(monkeypatch, FakeAdapter(failing=failing))

    lines = cli.status()
    output = "\n".join(lines)

    assert "exploded" in output
    assert len([line for line in lines if line.startswith("  ")]) == 5


@pytest.mark.unit
def test_an_adapter_that_cannot_start_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(provider: str) -> Any:
        raise FileNotFoundError("claude is not available on PATH")

    monkeypatch.setattr(cli, "get_provider", explode)
    monkeypatch.setattr(cli, "list_providers", lambda: ("fake",))

    output = "\n".join(cli.status())

    assert "could not start" in output
    assert "not available on PATH" in output


@pytest.mark.unit
def test_a_session_listing_the_provider_refuses_is_stated(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeAdapter(sessions=NotSupported("list_sessions", "no listing here")))

    assert "not supported: no listing here" in "\n".join(cli.status())


@pytest.mark.unit
def test_the_adapter_is_closed_after_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    install(monkeypatch, adapter)

    cli.status()

    assert adapter.closed is True


@pytest.mark.unit
def test_only_the_status_command_is_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert cli.main(["something-else"]) == 2
    assert "Usage:" in capsys.readouterr().out
