"""Run-oriented subscription provider contracts."""

from .auth import AuthenticationManager, scrub_provider_environment
from .capabilities import ProviderCapabilityRegistry
from .base import (
    AuthStatus,
    Capabilities,
    HealthReport,
    ModelInfo,
    NotSupported,
    ProviderAdapter,
    RunEvent,
    RunHandle,
    RunSpec,
    SessionInfo,
    UsageSnapshot,
)
from .models import ModelCatalog
from .codex import (
    CodexAdapter,
    CodexProtocolError,
    CodexTimeoutError,
    CodexTransportState,
)

__all__ = [
    "AuthStatus",
    "AuthenticationManager",
    "Capabilities",
    "CodexAdapter",
    "CodexProtocolError",
    "CodexTimeoutError",
    "CodexTransportState",
    "HealthReport",
    "ModelInfo",
    "ModelCatalog",
    "NotSupported",
    "ProviderAdapter",
    "ProviderCapabilityRegistry",
    "RunEvent",
    "RunHandle",
    "RunSpec",
    "SessionInfo",
    "UsageSnapshot",
    "scrub_provider_environment",
]
