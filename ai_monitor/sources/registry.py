"""Provider source adapters: abstract base and concrete implementations.

Each adapter fetches raw provider data for one source and returns a dict.
The state worker normalizes that dict via ``models.normalize_observation``.
"""

from __future__ import annotations

import http.client
import json
import math
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Config


class CollectionError(Exception):
    """A collector-level error with a spec error category."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class SourceAdapter(ABC):
    """Base class for per-source data collection."""

    @abstractmethod
    def fetch(self, config: "Config") -> dict[str, Any]:
        """Fetch raw provider data.

        Returns a raw dict on success. Raises ``CollectionError`` on failure.
        """
        raise NotImplementedError


class UnavailableAdapter(SourceAdapter):
    """Placeholder adapter for sources not yet wired."""

    def fetch(self, config: "Config") -> dict[str, Any]:
        raise CollectionError("unavailable", "source adapter not implemented")


# ---------------------------------------------------------------------------
# Antigravity (agy CLI, session-scoped)
# ---------------------------------------------------------------------------


class AntigravityAdapter(SourceAdapter):
    def fetch(self, config: "Config") -> dict[str, Any]:
        if not config.antigravity_bin:
            raise CollectionError("unavailable", "agy binary not found")
        try:
            proc = subprocess.run(
                [config.antigravity_bin, "usage", "--json"],
                capture_output=True,
                text=True,
                timeout=config.antigravity_timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            raise CollectionError("unavailable", "agy binary not found")
        except subprocess.TimeoutExpired:
            raise CollectionError("timeout", "agy usage timed out")

        if proc.returncode != 0:
            raise CollectionError("unavailable", proc.stderr.strip()[:200])
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CollectionError("unknown", f"agy output not JSON: {exc}")


# ---------------------------------------------------------------------------
# OpenRouter (account summary + per-key)
# ---------------------------------------------------------------------------


def _openrouter_request(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError subclasses URLError, so it must be handled first.
        if exc.code == 401:
            raise CollectionError("authentication", "openrouter authentication failed")
        if exc.code == 403:
            # Most commonly a non-management key calling /api/v1/credits.
            raise CollectionError("authorization", "openrouter authorization failed")
        if exc.code == 429:
            raise CollectionError("rate_limited", "openrouter rate limit")
        raise CollectionError("internal", f"openrouter HTTP {exc.code}")
    except urllib.error.URLError as exc:
        # Connect-phase timeouts are wrapped in URLError by urllib, so the
        # bare TimeoutError branch below only catches read-phase timeouts.
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise CollectionError("timeout", "openrouter request timed out")
        raise CollectionError("network", str(exc.reason))
    except TimeoutError:
        raise CollectionError("timeout", "openrouter request timed out")
    except OSError as exc:
        # RemoteDisconnected and other socket-level resets (OSError subclasses).
        raise CollectionError("network", str(exc))
    except http.client.HTTPException as exc:
        # Truncated or malformed HTTP framing (e.g. IncompleteRead). A retry
        # may succeed, so classify it as a retryable bad response.
        raise CollectionError("invalid_response", f"openrouter protocol error: {exc}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CollectionError("invalid_response", "openrouter returned non-JSON")


def _usd(value: Any) -> Optional[float]:
    """Coerce a provider USD value to a finite float, or None when absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _data_object(payload: Any) -> dict[str, Any]:
    """Return the ``data`` object of an OpenRouter response.

    Raises ``invalid_response`` when the shape is not the documented envelope.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise CollectionError(
            "invalid_response", "openrouter response missing data object"
        )
    return payload["data"]


def parse_openrouter_account_credits(payload: Any) -> list[dict[str, Any]]:
    """Map ``/api/v1/credits`` into an account-scope budget record.

    Remaining credits are derived **only** when both ``total_credits`` and
    ``total_usage`` are present; otherwise remaining stays ``None`` (never a
    fabricated balance). Account and key usage are never combined.
    """
    data = _data_object(payload)
    total = _usd(data.get("total_credits"))
    usage = _usd(data.get("total_usage"))
    remaining = total - usage if total is not None and usage is not None else None
    return [
        {
            "id": "account-credits",
            "scope": "account",
            "total_credits_usd": total,
            "total_usage_usd": usage,
            "remaining_usd": remaining,
            "limit_usd": None,
            "used_usd": None,
            "resets_at": None,
        }
    ]


def parse_openrouter_key_usage(payload: Any) -> list[dict[str, Any]]:
    """Map ``/api/v1/key`` into a key-scope budget record.

    Absent fields stay ``None``; the remaining amount is only what the
    provider reports (``limit_remaining``), never inferred.
    """
    data = _data_object(payload)
    return [
        {
            "id": "current-key",
            "scope": "key",
            "total_credits_usd": None,
            "total_usage_usd": None,
            "remaining_usd": _usd(data.get("limit_remaining")),
            "limit_usd": _usd(data.get("limit")),
            "used_usd": _usd(data.get("usage")),
            "resets_at": None,
        }
    ]


class OpenRouterAccountAdapter(SourceAdapter):
    """Account credit balance from the Provisioning API.

    ``/api/v1/credits`` requires a management/provisioning key. When
    ``openrouter_management_key`` is unset, fall back to the inference key:
    the request may then fail with 403 (surfaced as ``authorization``) while
    the current-key stream keeps working independently.
    """

    def fetch(self, config: "Config") -> dict[str, Any]:
        key = config.openrouter_management_key or config.openrouter_api_key
        if not key:
            raise CollectionError("not_configured", "openrouter api key not configured")
        payload = _openrouter_request(
            config.openrouter_base_url.rstrip("/") + "/api/v1/credits",
            {"Authorization": f"Bearer {key}"},
            config.openrouter_timeout_seconds,
        )
        return {"status": "success", "budgets": parse_openrouter_account_credits(payload)}


class OpenRouterKeyAdapter(SourceAdapter):
    """Current-key usage and limits."""

    def fetch(self, config: "Config") -> dict[str, Any]:
        if not config.openrouter_api_key:
            raise CollectionError("not_configured", "openrouter api key not configured")
        payload = _openrouter_request(
            config.openrouter_base_url.rstrip("/") + "/api/v1/key",
            {"Authorization": f"Bearer {config.openrouter_api_key}"},
            config.openrouter_timeout_seconds,
        )
        return {"status": "success", "budgets": parse_openrouter_key_usage(payload)}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def default_registry() -> Dict[str, type[SourceAdapter]]:
    # Imported lazily: ``codex`` depends on this module for CollectionError
    # and SourceAdapter, so a module-level import here would be circular.
    from .codex import CodexAdapter

    return {
        "codex": CodexAdapter,
        "antigravity": AntigravityAdapter,
        "openrouter-account": OpenRouterAccountAdapter,
        "openrouter-key": OpenRouterKeyAdapter,
    }


def build_registry(
    overrides: Dict[str, type[SourceAdapter]] | None = None,
) -> Dict[str, type[SourceAdapter]]:
    reg = default_registry()
    if overrides:
        reg.update(overrides)
    return reg
