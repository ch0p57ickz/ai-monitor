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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Config


class CollectionError(Exception):
    """A collector-level error with a spec error category.

    ``retry_after`` carries a provider-supplied minimum delay in seconds
    (for example an HTTP ``Retry-After`` header). The scheduler treats it as
    an uncapped minimum, so a longer provider delay is never shortened by the
    local backoff cap.
    """

    def __init__(
        self, category: str, message: str, retry_after: Optional[float] = None
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


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
# Antigravity (agy CLI, version-sensitive, session-scoped)
# ---------------------------------------------------------------------------

# Scope aliases seen across Antigravity variants. Values not listed here are
# normalized to lowercase and kept as-is, so an unverified scope is not
# silently relabelled as one of the scopes we know.
_ANTIGRAVITY_SCOPE_ALIASES = {
    "session": "session",
    "sessions": "session",
    "conversation": "session",
    "conversations": "session",
    "weekly": "weekly",
    "week": "weekly",
    "group": "group",
    "team": "group",
    "org": "group",
    "organization": "group",
    "workspace": "group",
    "model": "model",
}

# Markers that mean the installed variant does not expose a usage interface at
# all. These are permanent for the install, so they map to `unavailable`.
# Only explicit rejection phrases count: a bare usage banner can also be printed
# by a transient crash, which must stay retryable.
_ANTIGRAVITY_INCOMPATIBLE_MARKERS = (
    "unknown command",
    "unknown subcommand",
    "unrecognized",
    "invalid command",
    "invalid choice",
    "no such command",
    "unexpected argument",
    "unrecognized arguments",
)

_ANTIGRAVITY_BUCKET_KEYS = (
    "buckets",
    "quota_windows",
    "quotaWindows",
    "windows",
    "quotas",
    "usage",
)


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first key present in ``mapping`` with a non-``None`` value.

    Unlike an ``or`` chain, this distinguishes a reported ``0`` from a missing
    key, which keeps "unknown" separate from a genuine zero. A key whose value
    is explicitly ``null`` does not mask a later populated alias.
    """
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _antigravity_bucket_entries(payload: Any) -> list[Any]:
    """Collect candidate bucket entries from an Antigravity payload.

    Accepts a bare list, or a mapping with a bucket container that is either a
    list of entries or a mapping of bucket name to entry (the name is then used
    as a fallback scope).
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    for key in _ANTIGRAVITY_BUCKET_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return value
        if isinstance(value, dict):
            entries: list[Any] = []
            for name, entry in value.items():
                if isinstance(entry, dict):
                    merged = dict(entry)
                    merged.setdefault("scope", name)
                    entries.append(merged)
            if entries:
                return entries
    return []


def parse_antigravity_usage(payload: Any) -> list[dict[str, Any]]:
    """Map an Antigravity usage payload into quota windows.

    Only buckets the payload actually reports become windows: a session-only
    variant never gains a fabricated weekly window. Reported group, model, and
    scope values are retained, and absent numeric fields stay ``None``.

    Raises ``unavailable`` when the payload exposes no quota buckets at all,
    which means the installed variant is not a compatible source.
    """
    windows: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, entry in enumerate(_antigravity_bucket_entries(payload)):
        if not isinstance(entry, dict):
            continue

        raw_scope = _first_present(entry, ("scope", "kind", "type", "bucket", "window"))
        if raw_scope is None:
            scope = None
        else:
            scope = _ANTIGRAVITY_SCOPE_ALIASES.get(
                str(raw_scope).strip().lower(), str(raw_scope).strip().lower()
            )

        group = _as_str(
            _first_present(
                entry, ("group", "team", "org", "organization", "workspace", "dataset")
            )
        )
        model = _as_str(_first_present(entry, ("model", "model_id", "modelId")))
        label = _as_str(_first_present(entry, ("label", "name", "title")))
        identifier = _as_str(_first_present(entry, ("id", "key")))

        parts = [scope or "bucket", group, model]
        if label:
            parts.append(label)
        if not any((scope, group, model, label)):
            parts.append(str(index))

        # Windows are keyed by id downstream (history and pace tracking), so a
        # collision would merge unrelated buckets. Tie-break on the entry index.
        window_id = f"antigravity:{identifier or ':'.join(str(p) for p in parts if p)}"
        if window_id in used_ids:
            window_id = f"{window_id}#{index}"
        used_ids.add(window_id)

        windows.append(
            {
                "id": window_id,
                "label": label
                or " · ".join(str(p) for p in (scope, group, model) if p)
                or f"bucket {index}",
                "scope": scope,
                "group": group,
                "model": model,
                "used_percent": _finite_float(
                    _first_present(
                        entry,
                        (
                            "used_percent",
                            "usedPercent",
                            "usagePercent",
                            "usage_percent",
                            "percent",
                        ),
                    )
                ),
                "remaining_percent": _finite_float(
                    _first_present(
                        entry,
                        ("remaining_percent", "remainingPercent", "percentRemaining"),
                    )
                ),
                "duration_minutes": _finite_float(
                    _first_present(
                        entry,
                        ("duration_minutes", "durationMinutes", "window_minutes"),
                    )
                ),
                "resets_at": _as_str(
                    _first_present(entry, ("resets_at", "resetsAt", "reset_at"))
                ),
            }
        )

    if not windows:
        raise CollectionError(
            "unavailable", "antigravity variant exposes no quota buckets"
        )
    return windows


class AntigravityAdapter(SourceAdapter):
    """Collect quota buckets from the locally installed ``agy`` variant.

    The interface is version-sensitive. When the installed variant has no
    usage subcommand, or reports no quota buckets, the adapter raises
    ``unavailable`` so the source stays visible but disabled, without
    affecting Codex or OpenRouter.
    """

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

        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        if proc.returncode != 0:
            if any(marker in combined for marker in _ANTIGRAVITY_INCOMPATIBLE_MARKERS):
                raise CollectionError(
                    "unavailable",
                    "antigravity usage interface not supported by this variant",
                )
            raise CollectionError(
                "internal",
                f"agy usage failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}",
            )

        if not proc.stdout.strip():
            raise CollectionError("unavailable", "agy variant reports no usage data")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CollectionError("invalid_response", f"agy output not JSON: {exc}")

        windows = parse_antigravity_usage(payload)
        return {
            "status": "success",
            "quota_windows": windows,
            "notes": {"scopes": sorted({w["scope"] for w in windows if w["scope"]})},
        }


# ---------------------------------------------------------------------------
# OpenRouter (account summary + per-key)
# ---------------------------------------------------------------------------


def _retry_after_seconds(headers: Any) -> Optional[float]:
    """Parse an HTTP ``Retry-After`` header into a non-negative delay.

    Accepts both the delta-seconds and the HTTP-date form. Returns ``None``
    when the header is absent, unparseable, or already in the past.
    """
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


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
        retry_after = _retry_after_seconds(getattr(exc, "headers", None))
        if exc.code == 401:
            raise CollectionError(
                "authentication", "openrouter authentication failed", retry_after
            )
        if exc.code == 403:
            # Most commonly a non-management key calling /api/v1/credits.
            raise CollectionError(
                "authorization", "openrouter authorization failed", retry_after
            )
        if exc.code == 429:
            raise CollectionError(
                "rate_limited", "openrouter rate limit", retry_after
            )
        raise CollectionError(
            "internal", f"openrouter HTTP {exc.code}", retry_after
        )
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


def _finite_float(value: Any) -> Optional[float]:
    """Coerce a reported USD amount or percentage to a finite float.

    Returns ``None`` when the value is absent, non-numeric, boolean, or
    non-finite, so callers can keep "unknown" distinct from a reported zero.
    """
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
    total = _finite_float(data.get("total_credits"))
    usage = _finite_float(data.get("total_usage"))
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
            "remaining_usd": _finite_float(data.get("limit_remaining")),
            "limit_usd": _finite_float(data.get("limit")),
            "used_usd": _finite_float(data.get("usage")),
            # A reset timestamp enables exact budget pace; the period label
            # alone does not (usage-snapshots spec: a monthly label without a
            # timestamp must not become an exact boundary).
            "resets_at": _as_str(
                _first_present(
                    data, ("limit_reset_at", "limitResetAt", "resets_at", "reset_at")
                )
            ),
            "reset_period": _as_str(
                _first_present(data, ("limit_reset", "limitReset"))
            ),
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
