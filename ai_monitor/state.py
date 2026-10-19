"""In-memory worker: per-source polling, cadence, bounded retries, and JSON payloads."""

from __future__ import annotations

import json
import logging
import random
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


def _encoded_size(payload: Any) -> int:
    """Serialized size of a payload in bytes, as sent over HTTP."""
    return len(json.dumps(payload).encode("utf-8"))


class ResponseTooLarge(Exception):
    """A payload cannot be returned within the configured byte cap.

    Raised by ``history_payload`` when even a single observation exceeds the
    cap; the HTTP layer maps it to 413 ``response_too_large``
    (widget-usage-api spec: "if one observation alone exceeds that cap, the
    service SHALL return ``response_too_large``").
    """


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
    # Monotonic time at which this source may be polled again. ``None`` means
    # "immediately" (never polled, or a fresh start).
    _next_due_at: Optional[float] = None
    # A poll is currently running for this source. The dispatcher never starts
    # a second overlapping attempt for the same source.
    _in_flight: bool = False
    # Provider-supplied minimum delay (seconds) for the next attempt.
    _retry_after: Optional[float] = None

    def poll_due(self, now: Optional[float] = None) -> bool:
        if self._last_polled_at is None or self._next_due_at is None:
            return True
        return (now if now is not None else time.monotonic()) >= self._next_due_at

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
        self._pace_settings = PaceSettings(tolerance_percent=config.pace_tolerance)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._workers: Dict[str, _SourceWorker] = {}
        # In-flight attempt threads, so ``stop()`` can join them.
        self._attempts: Dict[str, threading.Thread] = {}
        # Injectable jitter source, so tests are deterministic.
        self._random = random.Random()

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
        if not self._config.account_digest_key:
            # Account-scoped trends (same-account pace, spend deltas) need an
            # opaque account identifier, which cannot be derived without a
            # digest key. Warn once so an inert feature is visible rather than
            # silently reported as a null account id.
            log.warning(
                "account_digest_key is not set: provider account ids will be null "
                "and account-scoped trends are disabled"
            )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="usage-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        # Attempts run in their own threads so a hung source cannot block the
        # dispatcher; join them so no work outlives ``stop()``. An attempt can
        # legitimately take up to ``deadline``, so allow for that (and for the
        # helper thread it abandons) before giving up. Cap the wait: a service
        # manager will SIGKILL long before a multi-minute join completes, which
        # would skip the ``db.close()`` this is meant to protect.
        join_timeout = min(self._config.deadline + 5.0, 60.0)
        with self._lock:
            attempts = list(self._attempts.values())
        for attempt in attempts:
            attempt.join(timeout=join_timeout)

    def _run(self) -> None:
        """Dispatcher loop: start each source's attempt when it is due.

        Polling happens on per-source attempt threads. The dispatcher itself
        never blocks on a provider, so a failing or hung source cannot delay
        another source, and the ``_in_flight`` flag prevents an overlapping
        attempt for the same source.
        """
        last_retention = time.monotonic()
        retention_interval = 86400.0  # run retention at most once per day

        while not self._stop.is_set():
            now_mono = time.monotonic()
            with self._lock:
                workers = list(self._workers.values())
            for worker in workers:
                if self._stop.is_set():
                    break
                if worker._in_flight or not worker.poll_due(now_mono):
                    continue
                self._dispatch(worker)

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

            self._stop.wait(timeout=self._config.scheduler_tick_seconds)

    def _dispatch(self, worker: _SourceWorker) -> None:
        """Start one attempt for ``worker`` on its own thread."""
        key = worker.source.source
        with self._lock:
            worker.mark_polled()
            attempt = threading.Thread(
                target=self._run_attempt, args=(worker,), daemon=True, name=f"poll-{key}"
            )
            try:
                attempt.start()
            except Exception:  # noqa: BLE001 - a failed start must not latch state
                log.exception("could not start an attempt thread for %s", key)
                self._schedule_next(worker, "internal")
                return
            # Only mark in-flight once the thread really started; otherwise the
            # source would never be polled again.
            worker._in_flight = True
            self._attempts[key] = attempt

    def _run_attempt(self, worker: _SourceWorker) -> None:
        """Run one attempt and always clear the in-flight flag."""
        key = worker.source.source
        try:
            self._poll_source(worker)
        except Exception:  # noqa: BLE001
            # A single source must never take down the scheduler. The attempt
            # thread is isolated, so other sources keep their own schedules.
            # ``_poll_source`` normally schedules the next attempt on both the
            # success and failure paths; if it raised before doing so, apply a
            # fallback backoff so this source is not retried every tick.
            log.exception(
                "poll for %s raised an unexpected error; "
                "continuing with other sources",
                key,
            )
            with self._lock:
                if worker._next_due_at is None:
                    self._schedule_next(worker, "internal")
        finally:
            with self._lock:
                self._attempts.pop(key, None)
                worker._in_flight = False

    def _attempt(self, worker: _SourceWorker) -> dict[str, Any]:
        """Run one adapter fetch, bounded by the configured deadline.

        The fetch runs on a helper thread and is abandoned at the deadline, so
        a hung provider cannot block this source indefinitely. Returns a dict
        with either ``raw`` or ``category`` (plus ``retry_after`` when the
        provider requested a delay).
        """
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def fetch() -> None:
            try:
                outcome["raw"] = worker.adapter.fetch(self._config)
            except CollectionError as exc:
                outcome["category"] = exc.category
                if getattr(exc, "retry_after", None) is not None:
                    outcome["retry_after"] = exc.retry_after
            except Exception:  # noqa: BLE001
                log.exception("unexpected error polling %s", worker.source.source)
                outcome["category"] = "unknown"
            finally:
                done.set()

        helper = threading.Thread(
            target=fetch, daemon=True, name=f"fetch-{worker.source.source}"
        )
        helper.start()
        if not done.wait(timeout=self._config.deadline):
            log.warning(
                "source %s: attempt exceeded the %.0fs deadline",
                worker.source.source,
                self._config.deadline,
            )
            return {"category": "timeout"}
        return outcome

    def _next_delay(self, worker: _SourceWorker, category: Optional[str]) -> float:
        """Delay before the next attempt for this source.

        Success waits the source cadence. A transient failure waits a capped
        exponential backoff with jitter. An operator-action failure (401/403
        or missing configuration) waits the maximum delay, so the service does
        not enter a rapid retry loop. A provider ``Retry-After`` is applied as
        an uncapped minimum: it is never shortened by the local cap.
        """
        config = self._config
        if category is None:
            delay = worker.source.cadence_seconds
        elif category in _OPERATOR_ACTION_CATEGORIES:
            delay = config.retry_max_delay
        else:
            exponent = max(0, worker._consecutive_failures - 1)
            delay = min(
                config.retry_max_delay,
                config.retry_base_delay * (config.retry_backoff_factor**exponent),
            )
            # Jitter only shrinks the local backoff, within [1-jitter, 1].
            jitter = config.retry_jitter
            if jitter > 0:
                delay *= 1.0 - jitter * self._random.random()
        if worker._retry_after is not None:
            # Uncapped: the local backoff cap never shortens the provider delay.
            delay = max(delay, worker._retry_after)
        return max(0.0, delay)

    def _schedule_next(self, worker: _SourceWorker, category: Optional[str]) -> None:
        worker._next_due_at = time.monotonic() + self._next_delay(worker, category)

    def _poll_source(self, worker: _SourceWorker) -> None:
        """Poll one source. Updates worker state, DB, and the next due time."""
        now = _now_iso()
        worker._last_polled_at = time.monotonic()
        worker._retry_after = None

        with self._lock:
            worker.last_attempt = now

        outcome = self._attempt(worker)
        raw: dict[str, Any] | None = outcome.get("raw")
        error_category: str | None = outcome.get("category")
        if outcome.get("retry_after") is not None:
            worker._retry_after = outcome["retry_after"]

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
            self._schedule_next(worker, error_category)
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
            budget_history = self._budget_history(
                worker.source.source, account_id=raw.get("account_id")
            )
            raw = enrich_observation(
                worker.source,
                raw,
                self._pace_settings,
                history,
                now_iso=now,
                budget_history=budget_history,
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
        self._schedule_next(worker, None)

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

    def _budget_history(
        self,
        source_key: str,
        account_id: Optional[str] = None,
        limit: int = 24,
        hours: float = 48.0,
    ) -> dict[str, list[dict[str, Any]]]:
        """Build the per-budget history for budget pace calculation.

        Maps a budget ``id`` to its list of prior rows, each carrying
        ``collected_at``, ``used_usd``, and ``limit_usd``.

        Returns an empty mapping when the observation has no ``account_id``.
        The spec forbids combining inferred spending across an account change,
        and without an account identifier a spend series cannot be attributed
        to one account, so no cross-snapshot spend evidence is used — the
        budget still reports its known balance from the current poll.
        """
        if account_id is None:
            return {}
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
            for b in row.get("budgets") or []:
                bid = b.get("id")
                if not bid:
                    continue
                history.setdefault(bid, []).append(
                    {
                        "collected_at": row.get("collectedAt"),
                        "used_usd": b.get("usedUsd"),
                        "limit_usd": b.get("limitUsd"),
                    }
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
        dropped until the serialized payload fits (always keeping the newest
        one), so the endpoint degrades gracefully. If a single observation
        still exceeds the cap, :class:`ResponseTooLarge` is raised and the HTTP
        layer returns ``response_too_large``.
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
            if _encoded_size(payload) <= max_bytes:
                return payload
            if not observations:
                # The metadata alone exceeds the cap, so even an empty history
                # cannot be served in-bounds.
                raise ResponseTooLarge()
            # Keep the largest suffix (the newest observations) that fits the
            # cap, always at least the newest one: a widget should still get the
            # most recent reading when older rows are too large. The list is
            # ascending and the serialized size grows monotonically with the
            # count, so binary search avoids re-serializing the whole payload
            # once per dropped row.
            low, high, keep = 1, len(observations), None
            while low <= high:
                mid = (low + high) // 2
                candidate = {
                    **payload,
                    "truncated": True,
                    "observations": observations[-mid:],
                }
                if _encoded_size(candidate) <= max_bytes:
                    keep = mid
                    low = mid + 1
                else:
                    high = mid - 1
            if keep is None:
                # A single observation alone exceeds the cap, so the endpoint
                # cannot satisfy the request in-bounds.
                raise ResponseTooLarge()
            payload["truncated"] = True
            payload["observations"] = observations[-keep:]
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
