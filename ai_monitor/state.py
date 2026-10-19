"""In-memory worker: per-source polling, cadence, bounded retries, and JSON payloads."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

from . import db as _db
from .config import Config
from .models import (
    SOURCE_DEFS,
    Budget,
    Observation,
    QuotaWindow,
    SourceDef,
    SourceStatus,
    get_source_def,
    normalize_observation,
)
from .pace import PaceSettings, enrich_observation
from .sources.registry import CollectionError, SourceAdapter, default_registry

if TYPE_CHECKING:  # pragma: no cover
    pass

log = logging.getLogger(__name__)

MAX_RETRY_STREAK = 5

#: Error categories permitted by the widget-usage-api spec for /health.
_ALLOWED_ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "authorization",
        "rate_limited",
        "timeout",
        "network",
        "invalid_response",
        "unsupported",
        "not_configured",
        "internal",
    }
)


def _normalize_error_category(category: Optional[str]) -> Optional[str]:
    """Map an internal error category onto the spec enum.

    Adapters use a few internal categories (``unavailable``, ``unknown``).
    ``unavailable`` is carried by the source ``status`` field, so it maps to
    ``null``; any other unrecognized value becomes ``internal`` so /health
    never emits a value outside the contract.
    """
    if category is None:
        return None
    if category in ("unavailable", "not_available"):
        return None
    if category in _ALLOWED_ERROR_CATEGORIES:
        return category
    return "internal"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _age_seconds(collected_at: str, now_iso: str) -> float:
    """Seconds between ``collected_at`` and ``now_iso``.

    Returns infinity when either timestamp is unparseable, so a corrupt row
    is reported as stale rather than silently as fresh.
    """
    try:
        c = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        n = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return max(0.0, (n - c).total_seconds())
    except (ValueError, AttributeError):
        return float("inf")


def _iso_plus_seconds(timestamp: str, seconds: float) -> Optional[str]:
    """Return ``timestamp + seconds`` as a UTC ISO-8601 ``Z`` string."""
    try:
        base = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    result = base + timedelta(seconds=seconds)
    return result.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


# Error categories that require operator action: the service must not keep
# retrying aggressively; an operator needs to fix the credential or
# configuration first.
_OPERATOR_ACTION_CATEGORIES = frozenset({"authentication", "authorization", "not_configured"})


@dataclass
class _SourceWorker:
    """Tracks per-source polling state for one source."""

    source: SourceDef
    adapter: SourceAdapter
    status: SourceStatus = SourceStatus.UNAVAILABLE
    last_attempt: Optional[str] = None
    last_success: Optional[str] = None
    last_error_category: Optional[str] = None
    last_observation: Optional[Observation] = None
    _last_polled_at: Optional[float] = None
    _consecutive_failures: int = 0
    _stale_after: int = 3600

    def poll_due(self) -> bool:
        if self._last_polled_at is None:
            return True
        return (time.monotonic() - self._last_polled_at) >= self.source.cadence_seconds

    def mark_polled(self) -> None:
        self._last_polled_at = time.monotonic()


class UsageWorker:
    """Single scheduler thread that owns per-source cadence and JSON payloads."""

    @staticmethod
    def _health_status(worker: "_SourceWorker") -> str:
        """Map worker state to the health status vocabulary.

        ``healthy`` — last poll succeeded.
        ``retrying`` — last poll failed transiently (timeout, network,
        rate_limited, invalid_response, internal, unknown); the scheduler
        keeps retrying with capped backoff.
        ``operator_action`` — the failure category indicates a credential
        or configuration problem that retrying cannot fix.
        ``unavailable`` — the source is known to be absent on this host.
        """
        if worker.status == SourceStatus.SUCCESS:
            return "healthy"
        if worker.status == SourceStatus.UNAVAILABLE:
            return "unavailable"
        if worker.last_error_category in ("unavailable", "not_available"):
            return "unavailable"
        if worker.last_error_category in _OPERATOR_ACTION_CATEGORIES:
            return "operator_action"
        return "retrying"

    def __init__(
        self,
        *,
        config: Config,
        db: "_db.Database",
        registry: Optional[Dict[str, type[SourceAdapter]]] = None,
        version: str = "unknown",
    ) -> None:
        self._config = config
        self._db = db
        self._version = version
        self._registry = registry or default_registry()
        self._pace_settings = PaceSettings()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._workers: Dict[str, _SourceWorker] = {}

        for source_def in SOURCE_DEFS:
            self._db.upsert_source(source_def)
            adapter_cls = self._registry.get(source_def.source)
            if adapter_cls is None:
                log.warning("no adapter registered for %s; will report unavailable", source_def.source)
                continue
            self._workers[source_def.source] = _SourceWorker(
                source=source_def,
                adapter=adapter_cls(),
                _stale_after=int(config.stale_after(source_def.source)),
            )

        self._initial_seed()

    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------

    def _initial_seed(self) -> None:
        """Seed worker state from the latest successful observation per source."""
        try:
            rows = self._db.latest_per_source()
        except Exception:  # noqa: BLE001
            return
        for row in rows:
            source_key = row["source"]
            if row["status"] != "success" or source_key not in self._workers:
                continue
            # Rebuild a lightweight Observation from the stored row
            source_def = get_source_def(source_key)
            if source_def is None:
                continue
            quota_windows = [
                QuotaWindow(
                    id=w.get("id", ""),
                    label=w.get("label"),
                    scope=w.get("scope"),
                    group=w.get("group"),
                    model=w.get("model"),
                    used_percent=w.get("usedPercent"),
                    remaining_percent=w.get("remainingPercent"),
                    duration_minutes=w.get("durationMinutes"),
                    resets_at=w.get("resetsAt"),
                    pace=w.get("pace"),
                )
                for w in row.get("quota_windows") or []
            ]
            budgets = [
                Budget(
                    id=b.get("id", ""),
                    scope=b.get("scope", "account"),
                    total_credits_usd=b.get("totalCreditsUsd"),
                    total_usage_usd=b.get("totalUsageUsd"),
                    remaining_usd=b.get("remainingUsd"),
                    limit_usd=b.get("limitUsd"),
                    used_usd=b.get("usedUsd"),
                    resets_at=b.get("resetsAt"),
                    pace=b.get("pace"),
                )
                for b in row.get("budgets") or []
            ]
            obs = Observation(
                source=source_key,
                provider=row.get("provider", source_def.provider),
                collected_at=row.get("collected_at"),
                observed_at=row.get("observed_at"),
                account_id=row.get("account_id"),
                status=SourceStatus.SUCCESS,
                quota_windows=quota_windows,
                budgets=budgets,
                error_category=None,
                notes=row.get("notes"),
            )
            self._workers[source_key].last_observation = obs
            self._workers[source_key].last_success = row.get("collected_at")
            self._workers[source_key].status = SourceStatus.SUCCESS

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Poll all sources once at startup
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            try:
                self._poll_source(worker)
            except Exception:  # noqa: BLE001
                log.exception(
                    "initial poll for %s raised an unexpected error",
                    worker.source.source,
                )

        last_retention = time.monotonic()
        retention_interval = 86400.0  # run retention at most once per day

        while not self._stop.is_set():
            with self._lock:
                workers = list(self._workers.values())
            for worker in workers:
                if self._stop.is_set():
                    break
                if worker.poll_due():
                    worker.mark_polled()
                    try:
                        self._poll_source(worker)
                    except Exception:  # noqa: BLE001
                        # A single source must never take down the whole
                        # scheduler. Log and keep polling the other sources;
                        # this source's state is left as-is and retried next
                        # cycle (spec: independent per-source polling).
                        log.exception(
                            "poll for %s raised an unexpected error; "
                            "continuing with other sources",
                            worker.source.source,
                        )

            # Periodic retention sweep
            now_mono = time.monotonic()
            if now_mono - last_retention >= retention_interval:
                last_retention = now_mono
                try:
                    deleted = self._db.apply_retention(
                        retention_days=self._config.retention_days
                    )
                    if deleted:
                        log.info("retention sweep deleted %d old observations", deleted)
                except Exception:  # noqa: BLE001
                    log.exception("retention sweep failed")

            self._stop.wait(timeout=1.0)

    def _poll_source(self, worker: _SourceWorker) -> None:
        """Poll one source. Updates worker state and DB."""
        now = _now_iso()
        worker._last_polled_at = time.monotonic()

        # Backoff gate: after MAX_RETRY_STREAK consecutive failures, reset the
        # streak so the next poll attempt is made (capped retry, not permanent).
        if worker._consecutive_failures >= MAX_RETRY_STREAK:
            log.debug(
                "source %s: retrying after %d consecutive failures",
                worker.source.source,
                worker._consecutive_failures,
            )
            worker._consecutive_failures = 0

        with self._lock:
            worker.last_attempt = now

        raw: dict[str, Any] | None = None
        error_category: str | None = None
        try:
            raw = worker.adapter.fetch(self._config)
        except CollectionError as exc:
            error_category = exc.category
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error polling %s", worker.source.source)
            error_category = "unknown"

        if error_category is not None and raw is None:
            with self._lock:
                worker.status = SourceStatus.ERROR
                worker.last_error_category = error_category
                worker._consecutive_failures += 1
            log.warning(
                "source %s: collection failed (%s) — consecutive failures: %d",
                worker.source.source,
                error_category,
                worker._consecutive_failures,
            )
            return

        # Normalize
        if raw is not None:
            # Enrich quota windows with pace using prior same-window
            # observations (reported spend-rate) or the window's own
            # reset/duration (estimated elapsed fraction). The current
            # row has not been committed yet, so the history is prior
            # rows only.
            history = self._window_history(
                worker.source.source, account_id=raw.get("account_id")
            )
            raw = enrich_observation(
                worker.source, raw, self._pace_settings, history, now_iso=now
            )
            obs = normalize_observation(worker.source, raw)
            obs.collected_at = now
        else:
            obs = normalize_observation(worker.source, {})
            obs.status = SourceStatus.UNAVAILABLE

        self._db.insert_observation(worker.source, obs)

        with self._lock:
            worker.last_observation = obs
            worker.last_success = now
            worker.status = obs.status
            worker.last_error_category = None
            worker._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Pace history
    # ------------------------------------------------------------------

    def _window_history(
        self,
        source_key: str,
        account_id: Optional[str] = None,
        limit: int = 24,
        hours: float = 48.0,
    ) -> dict[str, list[dict[str, Any]]]:
        """Build the per-window history for pace calculation.

        Maps a quota-window ``id`` to its list of prior normalized rows, each
        carrying ``collected_at`` and ``used_percent``. Rows are restricted
        to this source's *most recent* successful snapshots, and — when the
        current poll reports an ``account_id`` — to snapshots from the *same
        account* (spec: same-account history), so a reported series can never
        mix accounts.

        ``newest_first=True`` is essential: an ascending ``LIMIT`` would
        return the *oldest* rows in the lookback window, making the recent
        tail (and therefore the ``reported`` basis) permanently unreachable
        after the first hour of uptime. The account filter is applied in SQL
        so foreign-account rows cannot starve the same-account history.

        Note: ``db.history`` returns camelCase row dicts (``quotaWindows``,
        ``usedPercent``, ``collectedAt``, ``accountId``), matching the API
        serialization.
        """
        try:
            rows, _ = self._db.history(
                source=source_key,
                account_id=account_id,
                hours=hours,
                limit=limit,
                newest_first=True,
            )
        except Exception:  # noqa: BLE001
            return {}
        history: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for w in row.get("quotaWindows") or []:
                wid = w.get("id")
                used = w.get("usedPercent")
                if not wid or used is None:
                    continue
                history.setdefault(wid, []).append(
                    {"collected_at": row.get("collectedAt"), "used_percent": used}
                )
        return history

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------

    def health_payload(self) -> dict[str, Any]:
        """Build the /health payload.

        Source states use the spec vocabulary: ``healthy``, ``retrying``,
        ``operator_action``, or ``unavailable``. ``serviceStatus`` is
        ``healthy`` only when every source is healthy.
        """
        now_iso = _now_iso()
        with self._lock:
            sources = []
            for source in SOURCE_DEFS:
                worker = self._workers.get(source.source)
                if worker is None:
                    status = "unavailable"
                    error_category: Optional[str] = None
                    last_attempt: Optional[str] = None
                    last_success: Optional[str] = None
                else:
                    status = self._health_status(worker)
                    error_category = _normalize_error_category(worker.last_error_category)
                    last_attempt = worker.last_attempt
                    last_success = worker.last_success
                sources.append(
                    {
                        "id": source.source,
                        "provider": source.provider,
                        "status": status,
                        "lastAttemptAt": last_attempt,
                        "lastSuccessAt": last_success,
                        "errorCategory": error_category,
                    }
                )

            service_status = (
                "healthy" if all(s["status"] == "healthy" for s in sources) else "degraded"
            )

        return {
            "schemaVersion": 1,
            "generatedAt": now_iso,
            "serviceStatus": service_status,
            "sources": sources,
        }

    def usage_payload(self) -> dict[str, Any]:
        """Build the cached current-usage payload (spec: ``observations``).

        Only sources with at least one successful collection appear, ordered
        by source ID. Entries are the source's latest successful observation
        in the shared observation shape.
        """
        now_iso = _now_iso()
        with self._lock:
            observations = []
            for source in SOURCE_DEFS:
                worker = self._workers.get(source.source)
                if worker is None:
                    continue
                entry = self._usage_entry_locked(worker, now_iso)
                if entry is not None:
                    observations.append(entry)
        observations.sort(key=lambda e: e["source"])

        return {
            "schemaVersion": 1,
            "generatedAt": now_iso,
            "observations": observations,
        }

    def _observation_entry(
        self,
        *,
        provider: str,
        source: str,
        account_id: Optional[str],
        observed_at: Optional[str],
        collected_at: str,
        quota_windows: list[dict[str, Any]],
        budgets: list[dict[str, Any]],
        stale_after: float,
        now_iso: str,
    ) -> dict[str, Any]:
        """Assemble one observation in the shared API shape (line 41 of the spec).

        ``staleAfter`` is serialized as a timestamp string (collection time
        plus two configured intervals), matching the spec's timestamp rule.
        """
        age = _age_seconds(collected_at, now_iso)
        return {
            "provider": provider,
            "source": source,
            "accountId": account_id,
            "observedAt": observed_at,
            "collectedAt": collected_at,
            "staleAfter": _iso_plus_seconds(collected_at, stale_after),
            "status": "stale" if age > stale_after else "fresh",
            "quotaWindows": quota_windows,
            "budgets": budgets,
        }

    def _usage_entry_locked(self, worker: _SourceWorker, now_iso: str) -> Optional[dict[str, Any]]:
        """Build a usage entry for one source. Must be called with lock held."""
        obs = worker.last_observation
        if obs is None or obs.status != SourceStatus.SUCCESS:
            return None

        return self._observation_entry(
            provider=obs.provider,
            source=obs.source,
            account_id=obs.account_id,
            observed_at=obs.observed_at,
            collected_at=obs.collected_at,
            quota_windows=[w.to_dict() for w in obs.quota_windows],
            budgets=[b.to_dict() for b in obs.budgets],
            stale_after=worker._stale_after,
            now_iso=now_iso,
        )

    def usage_entry(self, source: SourceDef) -> Optional[dict[str, Any]]:
        """Return a single source's usage entry, or None if no successful observation."""
        with self._lock:
            worker = self._workers.get(source.source)
            if worker is None:
                return None
            return self._usage_entry_locked(worker, _now_iso())

    def history_payload(
        self, hours: int, limit: int, max_bytes: Optional[int] = None
    ) -> dict[str, Any]:
        """Build the bounded history payload (spec: ``/api/v1/usage/history``).

        Rows are returned in ascending ``collectedAt`` order. ``truncated`` is
        True when matching records were omitted by the count bound *or* the
        byte bound. When ``max_bytes`` is given, the oldest observations are
        dropped until the serialized payload fits, so the endpoint degrades
        gracefully instead of returning ``response_too_large``.
        """
        now_iso = _now_iso()
        try:
            # Select the *most recent* rows, then present ascending (spec:
            # bounded history returns the latest records in ascending order).
            rows, truncated = self._db.history(
                hours=hours, limit=limit, newest_first=True
            )
        except Exception:  # noqa: BLE001
            log.exception("history query failed")
            raise
        with self._lock:
            observations = [
                self._observation_entry(
                    provider=row.get("provider") or self._provider_for(row.get("source") or ""),
                    source=row.get("source") or "",
                    account_id=row.get("accountId"),
                    observed_at=row.get("observedAt"),
                    collected_at=collected_at,
                    quota_windows=row.get("quotaWindows") or [],
                    budgets=row.get("budgets") or [],
                    stale_after=self._stale_after_for(row.get("source") or ""),
                    now_iso=now_iso,
                )
                for row in reversed(rows)
                # Skip rows whose collectedAt cannot form a valid timestamp
                # (corrupt data would otherwise violate the string contract).
                if (collected_at := row.get("collectedAt"))
                and _iso_plus_seconds(collected_at, 0) is not None
            ]

        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "generatedAt": now_iso,
            "hours": hours,
            "truncated": truncated,
            "observations": observations,
        }
        if max_bytes is not None:
            # Drop the oldest observations (list is ascending) until it fits.
            while observations and len(json.dumps(payload).encode("utf-8")) > max_bytes:
                observations.pop(0)
                payload["truncated"] = True
        return payload

    def _stale_after_for(self, source_id: str) -> float:
        worker = self._workers.get(source_id)
        if worker is not None:
            return worker._stale_after
        try:
            return self._config.stale_after(source_id)
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _provider_for(source_id: str) -> str:
        source_def = get_source_def(source_id)
        return source_def.provider if source_def is not None else "unknown"
