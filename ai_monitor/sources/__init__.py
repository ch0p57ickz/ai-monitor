"""Source adapter registry and collection primitives."""

from .registry import (
    AntigravityAdapter,
    CodexAdapter,
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
