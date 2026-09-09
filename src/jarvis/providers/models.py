"""Evidence-backed model catalogue shared by provider adapters."""

from __future__ import annotations

from dataclasses import dataclass

from .base import ModelInfo


@dataclass(frozen=True)
class ModelCatalog:
    """Models known to a provider and whether it supports enumeration."""

    enumerable: bool
    models: tuple[ModelInfo, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))
