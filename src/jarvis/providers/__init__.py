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
from .hermes import HermesAdapter, HermesProtocolError, HermesTimeoutError
from .models import ModelCatalog
from .registry import get_provider, list_providers
from .claude import ClaudeAdapter, ClaudeProcessError, ClaudeTimeoutError
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
    "ClaudeAdapter",
    "ClaudeProcessError",
    "ClaudeTimeoutError",
    "CodexAdapter",
    "CodexProtocolError",
    "CodexTimeoutError",
    "CodexTransportState",
    "HealthReport",
    "HermesAdapter",
    "HermesProtocolError",
    "HermesTimeoutError",
    "ModelInfo",
    "ModelCatalog",
    "get_provider",
    "list_providers",
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
