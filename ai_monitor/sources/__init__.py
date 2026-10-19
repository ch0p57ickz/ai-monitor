"""Source adapter registry and collection primitives."""

from .codex import CodexAdapter
from .registry import (
    AntigravityAdapter,
    CollectionError,
    OpenRouterAccountAdapter,
    OpenRouterKeyAdapter,
    SourceAdapter,
    build_registry,
    default_registry,
)

__all__ = [
    "AntigravityAdapter",
    "CodexAdapter",
    "CollectionError",
    "OpenRouterAccountAdapter",
    "OpenRouterKeyAdapter",
    "SourceAdapter",
    "build_registry",
    "default_registry",
]
