"""Common, user-safe contracts for tool execution.

The result is deliberately richer than a boolean.  Tool callers need to know
whether retrying is sensible and diagnostics need a stable, non-localised
classification without ever having to parse a user-facing error string.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4


class ToolErrorCode(str, Enum):
    """Stable outcome codes shared by built-ins and MCP tools."""

    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    INVALID_CONFIG = "invalid_config"
    INVALID_ARGUMENT = "invalid_argument"
    EXECUTION_FAILED = "execution_failed"


@dataclass
class ToolExecutionResult:
    """Result object for tool execution."""
    success: bool
    reply_text: Optional[str]
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    phase: str = "execution"
    retryable: bool = False
    technical_details: Optional[str] = None
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep legacy results truthful without breaking existing tools."""
        if self.success:
            self.error_code = None
            self.retryable = False
        elif self.error_code is None:
            self.error_code = ToolErrorCode.EXECUTION_FAILED.value

    @classmethod
    def failure(
        cls,
        code: ToolErrorCode | str,
        user_message: str,
        *,
        phase: str = "execution",
        retryable: bool = False,
        technical_details: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> "ToolExecutionResult":
        """Create an explicit failure with a safe user description."""
        return cls(
            success=False,
            reply_text=user_message,
            error_message=user_message,
            error_code=code.value if isinstance(code, ToolErrorCode) else str(code),
            phase=phase,
            retryable=retryable,
            technical_details=technical_details,
            correlation_id=correlation_id or uuid4().hex,
        )
