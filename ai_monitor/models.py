"""Normalized observation model and source definitions.

Every provider adapter produces an :class:`Observation`. The API and the
database both consume the normalized form. Raw provider payloads are NEVER
persisted (spec: "SHALL NOT persist ... complete provider payloads").

Key invariants enforced here:
- Unknown values are ``None`` (JSON null), never inferred zeros or full marks.
- ``collected_at`` is always set; ``observed_at`` may be null when the
  provider does not supply its own timestamp.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .redact import mask_secrets, redact_text, redact_value


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with second precision and 'Z' suffix."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class SourceStatus(str, Enum):
    """Status of a single observation fetch.

    SUCCESS      — fetch returned usable data.
    UNAVAILABLE  — source is known to be not present on this host.
    ERROR        — fetch failed; ``error_category`` holds the specific reason.
    """

    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ErrorCategory(str, Enum):
    """Classification of a collection failure.

    These map to the spec's bounded retry policy and the health endpoint's
    ``errorCategory`` field.
    """

    NOT_CONFIGURED = "not_configured"
    NOT_AVAILABLE = "not_available"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"
    INTERNAL = "internal"


# ---------------------------------------------------------------------------
# Pace
# ---------------------------------------------------------------------------

def unknown_pace() -> dict[str, Any]:
    """Return the canonical 'unknown' pace object (always an object, never null)."""
    return {"classification": "unknown", "basis": "none", "expectedUsedPercent": None}


# ---------------------------------------------------------------------------
# Quota window
# ---------------------------------------------------------------------------

@dataclass
class QuotaWindow:
    """A single quota window (e.g. a 1-hour rate limit or a weekly window).

    All numeric fields are ``float`` or ``None``. ``None`` means the
    provider did not report it — the API renders "unknown", never 0/100.
    """

    id: str  # opaque string
    label: str | None = None
    scope: str | None = None  # e.g. "rate_limit", "weekly"
    group: str | None = None
    model: str | None = None
    used_percent: float | None = None
    remaining_percent: float | None = None
    duration_minutes: float | None = None
    resets_at: str | None = None  # ISO-8601 UTC or None
    pace: dict[str, Any] = field(default_factory=unknown_pace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "group": self.group,
            "model": self.model,
            "usedPercent": self.used_percent,
            "remainingPercent": self.remaining_percent,
            "durationMinutes": self.duration_minutes,
            "resetsAt": self.resets_at,
            "pace": self.pace,
        }


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@dataclass
class Budget:
    """A spending budget (OpenRouter account credits or key budget).

    USD values are serialized as decimal strings or null in the API.
    """

    id: str  # opaque string
    scope: str  # "account" or "key"
    total_credits_usd: float | None = None
    total_usage_usd: float | None = None
    remaining_usd: float | None = None
    limit_usd: float | None = None
    used_usd: float | None = None
    resets_at: str | None = None  # ISO-8601 UTC or None
    pace: dict[str, Any] = field(default_factory=unknown_pace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "totalCreditsUsd": self._usd_str(self.total_credits_usd),
            "totalUsageUsd": self._usd_str(self.total_usage_usd),
            "remainingUsd": self._usd_str(self.remaining_usd),
            "limitUsd": self._usd_str(self.limit_usd),
            "usedUsd": self._usd_str(self.used_usd),
            "resetsAt": self.resets_at,
            "pace": self.pace,
        }

    @staticmethod
    def _usd_str(value: float | str | None) -> str | None:
        """Format a USD amount as a minimal decimal string.

        Tolerant of a value that is already a serialized decimal string: on
        restart, ``_initial_seed`` rebuilds ``Budget`` objects from stored
        rows whose USD fields are the strings this method produced, so the
        serializer must be idempotent (``"7.5"`` -> ``"7.5"``) rather than
        formatting the string as a float.
        """
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if not math.isfinite(number):
            return None
        # Format as a minimal decimal string: 5.0 -> "5", 12.5 -> "12.5", 3.14159 -> "3.1416"
        if number == int(number):
            return str(int(number))
        return f"{number:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """A single normalized snapshot from one source.

    ``quota_windows`` and ``budgets`` are lists (may be empty). The API
    serializes these directly; the DB stores them as JSON.
    """

    source: str  # stable source ID, e.g. "codex", "openrouter-key"
    provider: str  # "codex", "antigravity", "openrouter"
    collected_at: str  # always set, ISO-8601 UTC
    observed_at: str | None = None  # provider's own timestamp, if any
    account_id: str | None = None  # opaque account identifier or None
    status: SourceStatus = SourceStatus.SUCCESS
    quota_windows: list[QuotaWindow] = field(default_factory=list)
    budgets: list[Budget] = field(default_factory=list)
    error_category: ErrorCategory | None = None
    notes: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Full JSON-serializable form used by the API and DB layer."""
        d: dict[str, Any] = {
            "provider": self.provider,
            "source": self.source,
            "accountId": self.account_id,
            "observedAt": self.observed_at,
            "collectedAt": self.collected_at,
            "status": self.status.value,
            "quotaWindows": [w.to_dict() for w in self.quota_windows],
            "budgets": [b.to_dict() for b in self.budgets],
        }
        if self.error_category is not None:
            d["errorCategory"] = self.error_category.value
        return d


def normalize_observation(source: SourceDef, raw: dict[str, Any]) -> Observation:
    """Build a normalized Observation from an adapter's raw payload.

    The raw payload is a dict produced by ``SourceAdapter.fetch()`` with the
    shape::

        {
            "status": "success" | "unavailable" | "error",
            "observed_at": str | None,
            "account_id": str | None,
            "quota_windows": [{"id": str, ...}],
            "budgets": [{"id": str, ...}],
            "error_category": str | None,
            "notes": dict | None,
        }

    Unknown values remain ``None`` — never inferred zeros or full marks.

    This is also the single choke point for provider text: ``label``, ``group``,
    ``model``, ``scope`` and the reset timestamps come from the payload and are
    redacted before they can be stored or served, and a provider-derived
    identifier gets its configured credentials masked exactly. ``account_id`` is
    left alone because it is our own digest, and the heuristic redactor would
    flatten every account into one identity.
    """
    status_str = raw.get("status", "error")
    if status_str == "success":
        status = SourceStatus.SUCCESS
    elif status_str == "unavailable":
        status = SourceStatus.UNAVAILABLE
    else:
        status = SourceStatus.ERROR

    # Parse quota windows.
    quota_windows: list[QuotaWindow] = []
    for w in raw.get("quota_windows") or []:
        quota_windows.append(QuotaWindow(
            id=_provider_id(w.get("id", "")),
            label=_provider_text(w.get("label")),
            scope=_provider_text(w.get("scope")),
            group=_provider_text(w.get("group")),
            model=_provider_text(w.get("model")),
            used_percent=_to_float(w.get("used_percent")),
            remaining_percent=_to_float(w.get("remaining_percent")),
            duration_minutes=_to_float(w.get("duration_minutes")),
            resets_at=_provider_text(w.get("resets_at")),
            pace=w.get("pace") or unknown_pace(),
        ))

    # Parse budgets.
    budgets: list[Budget] = []
    for b in raw.get("budgets") or []:
        budgets.append(Budget(
            id=_provider_id(b.get("id", "")),
            scope=_provider_text(b.get("scope", "account")),
            total_credits_usd=_to_float(b.get("total_credits_usd")),
            total_usage_usd=_to_float(b.get("total_usage_usd")),
            remaining_usd=_to_float(b.get("remaining_usd")),
            limit_usd=_to_float(b.get("limit_usd")),
            used_usd=_to_float(b.get("used_usd")),
            resets_at=_provider_text(b.get("resets_at")),
            pace=b.get("pace") or unknown_pace(),
        ))

    # Parse error category.
    error_category: ErrorCategory | None = None
    if raw.get("error_category"):
        try:
            error_category = ErrorCategory(raw["error_category"])
        except ValueError:
            error_category = ErrorCategory.INTERNAL

    return Observation(
        source=source.source,
        provider=source.provider,
        collected_at=utc_now_iso(),
        observed_at=raw.get("observed_at"),
        account_id=raw.get("account_id"),
        status=status,
        quota_windows=quota_windows,
        budgets=budgets,
        error_category=error_category,
        # Notes are persisted and may be surfaced later, so redact them at this
        # single choke point (no credentials, tokens, emails, or full payloads).
        notes=redact_value(raw.get("notes")) if raw.get("notes") else None,
    )


def _provider_text(value: Any) -> Any:
    """Redact provider-supplied display text before it is persisted.

    Provider payloads are untrusted input that ends up in the database and in
    HTTP responses, and the spec is unconditional: neither may carry a
    credential. The fields below are free text or timestamps a provider chose,
    so they are run through the redactor at this single choke point.
    """
    if value is None:
        return None
    return redact_text(value)


def _provider_id(value: Any) -> Any:
    """Mask configured credentials in a provider-derived identifier.

    Exact matches only. The heuristic rules also mask opaque strings, and these
    identifiers key window and budget identity: applying them here would turn
    two distinct 64-character hex ids into the same ``[redacted]`` and merge
    unrelated buckets.
    """
    if value is None:
        return None
    return mask_secrets(value)


def _to_float(value: Any) -> float | None:
    """Coerce to float; return None for None/empty/invalid/NaN/inf."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


# ---------------------------------------------------------------------------
# Source definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceDef:
    """Static definition of a usage source.

    One source = one independently polled unit. OpenRouter has two:
    ``openrouter-account`` and ``openrouter-key``.
    """

    source: str  # stable source ID
    provider: str  # provider family
    label: str
    window: str  # "rolling_1h", "weekly", "monthly", "n/a"
    cadence_seconds: int


# ---------------------------------------------------------------------------
# Default source definitions (spec: 4 stable source IDs)
# ---------------------------------------------------------------------------

SOURCE_DEFS: list[SourceDef] = [
    SourceDef(
        source="codex",
        provider="codex",
        label="Codex",
        window="rolling_1h",
        cadence_seconds=60,
    ),
    SourceDef(
        source="antigravity",
        provider="antigravity",
        label="Antigravity",
        window="n/a",
        cadence_seconds=60,
    ),
    SourceDef(
        source="openrouter-account",
        provider="openrouter",
        label="OpenRouter Account",
        window="monthly",
        cadence_seconds=300,
    ),
    SourceDef(
        source="openrouter-key",
        provider="openrouter",
        label="OpenRouter Key",
        window="monthly",
        cadence_seconds=300,
    ),
]


def get_source_def(source_id: str) -> SourceDef | None:
    for d in SOURCE_DEFS:
        if d.source == source_id:
            return d
    return None
