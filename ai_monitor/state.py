"""In-memory worker: per-source polling, cadence, bounded retries, and JSON payloads."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _age_seconds(collected_at: str, now_iso: str) -> float:
    try:
        c = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        n = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return max(0.0, (n - c).total_seconds())
    except (ValueError, AttributeError):
        return 0.0


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
            self._poll_source(worker)

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
                    self._poll_source(worker)

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
            history = self._window_history(worker.source.source)
            raw = enrich_observation(worker.source, raw, self._pace_settings, history)
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
        limit: int = 24,
        hours: float = 48.0,
    ) -> dict[str, list[dict[str, Any]]]:
        """Build the per-window history for pace calculation.

        Maps a quota-window ``id`` to its list of prior normalized rows
        (newest first), each carrying ``collected_at`` and ``used_percent``.
        Rows are restricted to this source's last few successful snapshots
        so the reported spend-rate stays on recent, consecutive data.
        """
        try:
            rows, _ = self._db.history(source=source_key, hours=hours, limit=limit)
        except Exception:  # noqa: BLE001
            return {}
        history: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for w in row.get("quota_windows") or []:
                wid = w.get("id")
                used = w.get("used_percent")
                if not wid or used is None:
                    continue
                history.setdefault(wid, []).append(
                    {"collected_at": row.get("collected_at"), "used_percent": used}
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
                    error_category = worker.last_error_category
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

        return {
            "schemaVersion": 1,
            "generatedAt": now_iso,
            "observations": observations,
        }

    def _usage_entry_locked(self, worker: _SourceWorker, now_iso: str) -> Optional[dict[str, Any]]:
        """Build a usage entry for one source. Must be called with lock held."""
        obs = worker.last_observation
        if obs is None or obs.status != SourceStatus.SUCCESS:
            return None

        age = _age_seconds(obs.collected_at, now_iso)
        stale = age > worker._stale_after

        entry: dict[str, Any] = {
            "source": worker.source.source,
            "label": worker.source.label,
            "status": "stale" if stale else "fresh",
            "collectedAt": obs.collected_at,
            "staleAfter": worker._stale_after,
        }

        if obs.quota_windows:
            entry["quota"] = [w.to_dict() for w in obs.quota_windows]
        if obs.budgets:
            entry["budgets"] = [b.to_dict() for b in obs.budgets]

        return entry

    def usage_entry(self, source: SourceDef) -> Optional[dict[str, Any]]:
        """Return a single source's usage entry, or None if no successful observation."""
        with self._lock:
            worker = self._workers.get(source.source)
            if worker is None:
                return None
            return self._usage_entry_locked(worker, _now_iso())
