"""Provider-neutral contracts for subscription-backed agent runs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Mapping


ProviderId = Literal["claude", "codex", "hermes"]
ModelSource = Literal["api", "verified-probe", "provider-default", "unknown"]
CapabilityProfile = Literal["read_only", "project_dev", "automation", "unrestricted"]
RunStatus = Literal["ok", "error", "cancelled", "quota", "timeout"]
RunEventKind = Literal[
    "run.started",
    "turn.started",
    "text.delta",
    "thinking",
    "tool.call",
    "tool.result",
    "approval.needed",
    "needs_you",
    "usage.delta",
    "run.finished",
]

MODEL_SOURCES = frozenset({"api", "verified-probe", "provider-default", "unknown"})
CAPABILITY_PROFILES = frozenset({"read_only", "project_dev", "automation", "unrestricted"})
RUN_STATUSES = frozenset({"ok", "error", "cancelled", "quota", "timeout"})
EVENT_REQUIRED_FIELDS: Mapping[str, frozenset[str]] = {
    "run.started": frozenset({"run_id", "provider", "model", "session_id", "cwd", "ts"}),
    "turn.started": frozenset({"turn_id"}),
    "text.delta": frozenset({"text"}),
    "thinking": frozenset({"text"}),
    "tool.call": frozenset({"name", "args_redacted", "tool_id"}),
    "tool.result": frozenset({"tool_id", "ok", "summary", "bytes"}),
    "approval.needed": frozenset({"kind", "detail", "options"}),
    "needs_you": frozenset({"question"}),
    "usage.delta": frozenset({"input", "output", "cached"}),
    "run.finished": frozenset({"status", "reason"}),
}


@dataclass(frozen=True)
class RunSpec:
    """Inputs for a new provider run.

    Provider-specific adapters translate only the populated, supported fields
    into their native process protocol.
    """

    prompt: str
    model: str | None = None
    cwd: str | None = None
    system_prompt: str | None = None
    capability_profile: CapabilityProfile = "read_only"
    mcp_config: str | None = None
    allowed_tools: tuple[str, ...] = ()
    additional_directories: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.capability_profile not in CAPABILITY_PROFILES:
            raise ValueError(f"Unsupported capability profile: {self.capability_profile}")


@dataclass(frozen=True)
class RunHandle:
    """Stable identity and pinned execution choices for a provider run."""

    run_id: str
    provider: ProviderId
    session_id: str
    model: str | None
    cwd: str | None
    model_source: ModelSource = "unknown"


@dataclass(frozen=True)
class AuthStatus:
    """Subscription authentication state reported by a provider CLI."""

    logged_in: bool = False
    method: str | None = None
    account: str | None = None
    plan: str | None = None
    expires_at: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    """A model identity obtained from an explicit, auditable source."""

    id: str
    source: ModelSource
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.source not in MODEL_SOURCES:
            raise ValueError(f"Unsupported model source: {self.source}")


@dataclass(frozen=True)
class Capabilities:
    """Provider features that are known to be supported for a model."""

    tools: bool = False
    mcp: bool = False
    streaming: bool = False
    images: bool = False
    structured_output: bool = False
    steering: bool = False


@dataclass(frozen=True)
class UsageSnapshot:
    """A provider usage reading, or an explicit unavailable state."""

    available: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    limit: float | None = None
    remaining: float | None = None
    resets_at: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class HealthReport:
    """Reachability and measured latency for a provider CLI."""

    reachable: bool = False
    latency_ms: float | None = None
    detail: str | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class SessionInfo:
    """A provider session, including whether JARVIS may control it."""

    session_id: str
    provider: ProviderId
    owned: bool = False
    title: str | None = None
    model: str | None = None
    cwd: str | None = None
    status: str | None = None
    started_at: str | None = None


@dataclass(frozen=True)
class NotSupported:
    """Explicit result for a capability the provider does not implement."""

    capability: str
    reason: str
    supported: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True)
class RunEvent:
    """One validated event in the provider-neutral run stream."""

    kind: RunEventKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        required = EVENT_REQUIRED_FIELDS.get(self.kind)
        if required is None:
            raise ValueError(f"Unsupported provider event: {self.kind}")

        missing = required.difference(self.payload)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Provider event {self.kind} is missing fields: {names}")

        if self.kind == "run.finished":
            status = self.payload["status"]
            if status not in RUN_STATUSES:
                raise ValueError(f"Unsupported run status: {status}")

        object.__setattr__(self, "payload", dict(self.payload))


from .models import ModelCatalog


class ProviderAdapter(ABC):
    """Run-oriented interface implemented by each subscription provider."""

    @abstractmethod
    def id(self) -> ProviderId:
        """Return the stable provider identifier."""

    @abstractmethod
    def auth_status(self) -> AuthStatus:
        """Return subscription authentication state, failing closed."""

    @abstractmethod
    def list_models(self) -> ModelCatalog:
        """Return models learned from provider-backed evidence."""

    @abstractmethod
    def capabilities(self, model: str | None) -> Capabilities:
        """Return known capabilities for ``model`` with false defaults."""

    @abstractmethod
    def start_run(self, spec: RunSpec) -> RunHandle:
        """Start a run and pin its provider, model and session."""

    @abstractmethod
    def stream(self, run: RunHandle) -> Iterator[RunEvent]:
        """Yield normalised events for ``run`` until a terminal event."""

    @abstractmethod
    def steer(self, run: RunHandle, text: str) -> bool | NotSupported:
        """Send a user message to an active run."""

    @abstractmethod
    def interrupt(self, run: RunHandle) -> bool | NotSupported:
        """Interrupt an active run."""

    @abstractmethod
    def resume(self, session_id: str) -> RunHandle | NotSupported:
        """Resume a provider session under JARVIS control."""

    @abstractmethod
    def fork(self, session_id: str) -> RunHandle | NotSupported:
        """Fork a provider session into a JARVIS-owned run."""

    @abstractmethod
    def list_sessions(self) -> list[SessionInfo] | NotSupported:
        """Return both owned and external provider sessions."""

    @abstractmethod
    def usage(self) -> UsageSnapshot:
        """Return usage or an explicit unavailable snapshot."""

    @abstractmethod
    def health(self) -> HealthReport:
        """Return reachability and measured latency."""
