"""Integration tests: worker polling, payloads, HTTP server, and registry."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.http import UsageServer, serve_http
from ai_monitor.models import SourceStatus, get_source_def, normalize_observation
from ai_monitor.sources.codex import CodexAdapter
from ai_monitor.sources.registry import (
    AntigravityAdapter,
    CollectionError,
    OpenRouterAccountAdapter,
    OpenRouterKeyAdapter,
    SourceAdapter,
    build_registry,
    default_registry,
)
from ai_monitor.state import ResponseTooLarge, UsageWorker, _SourceWorker
from conftest import FakeSourceAdapter

CODEX = get_source_def("codex")
ANTIGRAVITY = get_source_def("antigravity")
OPENROUTER_ACC = get_source_def("openrouter-account")
OPENROUTER_KEY = get_source_def("openrouter-key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_obs(source_key: str, collected_at: str | None = None):
    source = get_source_def(source_key)
    raw = {
        "status": "success",
        "quota_windows": [{"id": f"{source_key}-w", "label": "Test", "used_percent": 10.0}],
    }
    obs = normalize_observation(source, raw)
    if collected_at:
        obs.collected_at = collected_at
    return source, obs


class FakeAdapter(SourceAdapter):
    def __init__(
        self,
        raw: dict[str, Any] | None = None,
        error: CollectionError | None = None,
    ) -> None:
        self._raw = raw if raw is not None else {}
        self._error = error
        self.fetch_count = 0

    def fetch(self, config: "Config") -> dict[str, Any]:
        self.fetch_count += 1
        if self._error is not None:
            raise self._error
        return self._raw


# ---------------------------------------------------------------------------
# Worker tests
# ---------------------------------------------------------------------------


class TestUsageWorker:
    def test_health_payload_structure(self, sample_config: Config, sample_db: Database):
        worker = UsageWorker(config=sample_config, db=sample_db, version="0.1.0-test")
        payload = worker.health_payload()
        assert payload["schemaVersion"] == 1
        assert "generatedAt" in payload
        # No data collected yet -> every source is retrying -> degraded.
        assert payload["serviceStatus"] == "degraded"
        assert isinstance(payload["sources"], list)
        assert len(payload["sources"]) == 4

        assert [s["id"] for s in payload["sources"]] == [
            "codex",
            "antigravity",
            "openrouter-account",
            "openrouter-key",
        ]
        for s in payload["sources"]:
            assert s["provider"] is not None
            # Initial state before any poll is UNAVAILABLE (not yet attempted).
            assert s["status"] == "unavailable"
            assert s["lastAttemptAt"] is None
            assert s["lastSuccessAt"] is None
            assert s["errorCategory"] is None

    def test_health_payload_status_mapping(self, sample_config: Config, sample_db: Database):
        worker = UsageWorker(config=sample_config, db=sample_db, version="0.1.0-test")

        def mk(status: SourceStatus, err: str | None) -> _SourceWorker:
            return _SourceWorker(
                source=CODEX, adapter=FakeSourceAdapter(), status=status,
                last_error_category=err,
            )

        assert worker._health_status(mk(SourceStatus.SUCCESS, None)) == "healthy"
        assert worker._health_status(mk(SourceStatus.UNAVAILABLE, "unsupported")) == "unavailable"
        assert worker._health_status(mk(SourceStatus.ERROR, "authentication")) == "operator_action"
        assert worker._health_status(mk(SourceStatus.ERROR, "timeout")) == "retrying"

        # Mixed fleet: two healthy, one retrying, one operator_action.
        # _workers is a dict keyed by source ID in SOURCE_DEFS order:
        #   codex, antigravity, openrouter-account, openrouter-key
        _workers = list(worker._workers.values())
        for w in _workers:
            w.status = SourceStatus.SUCCESS
            w.last_error_category = None
            w.last_success = datetime.now(UTC).isoformat()
        _workers[0].status = SourceStatus.ERROR   # codex
        _workers[0].last_error_category = "timeout"
        _workers[2].status = SourceStatus.ERROR   # openrouter-account
        _workers[2].last_error_category = "not_configured"

        payload = worker.health_payload()
        assert payload["serviceStatus"] == "degraded"
        by_id = {s["id"]: s for s in payload["sources"]}
        assert by_id["codex"]["status"] == "retrying"
        assert by_id["codex"]["errorCategory"] == "timeout"
        assert by_id["openrouter-account"]["status"] == "operator_action"
        assert by_id["antigravity"]["status"] == "healthy"
        assert by_id["openrouter-key"]["status"] == "healthy"
        assert by_id["openrouter-key"]["errorCategory"] is None

    def test_usage_payload_structure(self, sample_config: Config, sample_db: Database):
        worker = UsageWorker(config=sample_config, db=sample_db, version="0.1.0-test")
        payload = worker.usage_payload()
        assert payload["schemaVersion"] == 1
        assert "generatedAt" in payload
        assert "observations" in payload
        assert isinstance(payload["observations"], list)

    def test_usage_entry_returns_none_without_data(self, sample_config: Config, sample_db: Database):
        worker = UsageWorker(config=sample_config, db=sample_db)
        assert worker.usage_entry(CODEX) is None

    def test_usage_entry_after_success(self, sample_config: Config, sample_db: Database):
        # Seed the DB with a successful observation
        source, obs = make_obs("codex")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs)

        worker = UsageWorker(config=sample_config, db=sample_db)
        entry = worker.usage_entry(CODEX)
        assert entry is not None
        assert entry["source"] == "codex"
        assert entry["status"] == "fresh"
        assert entry["collectedAt"]
        # Spec shape: full observation fields, not the legacy "quota" key.
        assert entry["provider"] == "codex"
        assert entry["quotaWindows"]
        assert entry["budgets"] == []
        assert set(entry) >= {
            "provider", "source", "accountId", "observedAt",
            "collectedAt", "staleAfter", "status", "quotaWindows", "budgets",
        }

    def test_staleness_after_two_missed_intervals(
        self, sample_config: Config, sample_db: Database
    ):
        """Freshness is derived from the interval: stale after two missed ones."""
        worker = UsageWorker(config=sample_config, db=sample_db)
        codex = worker._workers["codex"]
        assert codex._stale_after == 2.0 * sample_config.codex_interval

        # A current observation is fresh.
        _, fresh = make_obs("codex")
        codex.last_observation = fresh
        assert worker.usage_entry(CODEX)["status"] == "fresh"

        # An observation older than two intervals is stale.
        _, old = make_obs("codex")
        old.collected_at = "2020-01-01T00:00:00Z"
        codex.last_observation = old
        assert worker.usage_entry(CODEX)["status"] == "stale"

    def test_poll_failure_retains_last_good(self, sample_config: Config, sample_db: Database):
        """On failure, the worker retains the last successful observation."""
        # Seed with a successful observation
        source, obs = make_obs("codex")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs)

        class FailAdapter(SourceAdapter):
            def fetch(self, config):
                raise CollectionError("unavailable", "test failure")

        worker = UsageWorker(
            config=sample_config,
            db=sample_db,
            registry={"codex": FailAdapter},
        )

        # Simulate a poll
        codex_worker = worker._workers.get("codex")
        assert codex_worker is not None
        worker._poll_source(codex_worker)

        # Status should be error
        assert codex_worker.status == SourceStatus.ERROR
        assert codex_worker.last_error_category == "unavailable"

        # But last_observation should still be the seeded one
        assert codex_worker.last_observation is not None
        assert codex_worker.last_observation.status == SourceStatus.SUCCESS

    def test_poll_success_updates_state_and_db(self, sample_config: Config, sample_db: Database):
        """A successful poll updates worker state and persists to the DB."""
        raw = {
            "status": "success",
            "quota_windows": [{"id": "test-w", "label": "Test", "used_percent": 25.0}],
        }

        class SuccessAdapter(SourceAdapter):
            def fetch(self, config):
                return dict(raw)

        worker = UsageWorker(
            config=sample_config,
            db=sample_db,
            registry={"codex": SuccessAdapter},
        )

        codex_worker = worker._workers.get("codex")
        assert codex_worker is not None
        worker._poll_source(codex_worker)

        # Worker state should reflect success
        assert codex_worker.status == SourceStatus.SUCCESS
        assert codex_worker.last_observation is not None
        assert codex_worker.last_observation.status == SourceStatus.SUCCESS
        assert codex_worker.last_success is not None
        assert codex_worker._consecutive_failures == 0

        # DB should have the observation
        rows = sample_db.latest_per_source()
        codex_rows = [r for r in rows if r["source"] == "codex"]
        assert len(codex_rows) == 1
        assert codex_rows[0]["status"] == "success"

        # Usage payload should include the source
        payload = worker.usage_payload()
        sources = [o["source"] for o in payload["observations"]]
        assert "codex" in sources

    def test_restart_preserves_successful_observations(self, sample_config: Config, sample_db: Database):
        """A restart (new worker on same DB) restores last successful observations."""
        # First worker: poll codex successfully
        raw = {
            "status": "success",
            "quota_windows": [{"id": "w1", "label": "W", "used_percent": 42.0}],
        }

        class SuccessAdapter(SourceAdapter):
            def fetch(self, config):
                return dict(raw)

        worker1 = UsageWorker(
            config=sample_config,
            db=sample_db,
            registry={"codex": SuccessAdapter},
        )
        codex_worker = worker1._workers.get("codex")
        assert codex_worker is not None
        worker1._poll_source(codex_worker)
        assert codex_worker.status == SourceStatus.SUCCESS

        # Second worker (simulates restart) on the same DB
        worker2 = UsageWorker(config=sample_config, db=sample_db)
        codex_worker2 = worker2._workers.get("codex")
        assert codex_worker2 is not None
        assert codex_worker2.last_observation is not None
        assert codex_worker2.last_observation.status == SourceStatus.SUCCESS
        assert codex_worker2.last_success is not None

        # Usage payload from the restarted worker should include codex
        entry = worker2.usage_entry(CODEX)
        assert entry is not None
        assert entry["source"] == "codex"

    def test_window_history_reads_stored_rows_and_filters_account(
        self, sample_config: Config, sample_db: Database
    ):
        """``_window_history`` must read the camelCase keys that
        ``db.history`` returns (``quotaWindows`` / ``usedPercent`` /
        ``collectedAt``), and restrict to the same account when one is known.

        Regression: the method previously read snake_case keys, so it always
        returned an empty history and the ``reported`` pace basis could never
        fire in production.
        """
        def make_acct_adapter(account_id: str):
            class _AcctAdapter(SourceAdapter):
                def fetch(self, config):
                    return {
                        "status": "success",
                        "account_id": account_id,
                        "quota_windows": [
                            {"id": "w", "used_percent": 50.0, "duration_minutes": 60}
                        ],
                    }
            return _AcctAdapter

        worker = UsageWorker(
            config=sample_config,
            db=sample_db,
            registry={"codex": make_acct_adapter("acct1")},
        )
        codex_worker = worker._workers["codex"]
        worker._poll_source(codex_worker)
        worker._poll_source(codex_worker)

        hist = worker._window_history("codex", account_id="acct1")
        assert "w" in hist, "history must be non-empty (camelCase keys read correctly)"
        assert len(hist["w"]) == 2
        assert hist["w"][0]["used_percent"] == 50.0
        assert hist["w"][0]["collected_at"]

        # A different account must not see the first account's rows.
        assert worker._window_history("codex", account_id="acct2") == {}

    def test_window_history_returns_newest_rows(
        self, sample_config: Config, sample_db: Database
    ):
        """``_window_history`` must return the *most recent* rows, not the
        oldest.

        Regression: ``db.history`` ordered ascending, so an ascending LIMIT
        returned the earliest rows in the lookback window and the recent tail
        required for the ``reported`` pace basis became unreachable after the
        first hour of uptime.
        """
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        base = datetime.now(UTC)
        # 60 rows, 10 minutes apart (10 h total, inside the 48 h lookback);
        # used_percent increases with recency so the assertion can tell which
        # end of the series was returned.
        total = 60
        for i in range(total):
            raw = {
                "status": "success",
                "quota_windows": [{"id": "w", "used_percent": float(i)}],
            }
            obs = normalize_observation(source, raw)
            ts = base - timedelta(minutes=10 * (total - 1 - i))
            obs.collected_at = ts.replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            sample_db.insert_observation(source, obs)

        worker = UsageWorker(config=sample_config, db=sample_db)
        hist = worker._window_history("codex", limit=24)
        assert len(hist["w"]) == 24
        used = sorted(r["used_percent"] for r in hist["w"])
        # Only the newest 24 rows (i = 36..59) must be present.
        assert used == [float(i) for i in range(total - 24, total)]

    def test_reported_basis_fires_end_to_end(
        self, sample_config: Config, sample_db: Database
    ):
        """The ``reported`` pace basis must fire through the real poll path
        when a sufficient recent same-window series exists.

        This covers the composition of ``_window_history`` and
        ``enrich_observation`` inside ``_poll_source`` — the seam where the
        history-read bug lived (camelCase keys + oldest-row ordering), which
        isolated unit tests could not catch.
        """
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        base = datetime.now(UTC)
        # 60 rows, 60 s apart, used_percent rising. More rows than the history
        # ``limit`` (24) is deliberate: with the old oldest-row ordering the
        # returned tail would end ~35 min before now, exceeding
        # max_gap_seconds (1800 s), so ``reported`` could not fire.
        n = 60
        for i in range(n):
            raw = {
                "status": "success",
                "quota_windows": [
                    {"id": "w", "used_percent": 10.0 + 0.5 * i, "duration_minutes": 120}
                ],
            }
            obs = normalize_observation(source, raw)
            ts = base - timedelta(seconds=60 * (n - i))
            obs.collected_at = ts.replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            sample_db.insert_observation(source, obs)

        class LiveAdapter(SourceAdapter):
            def fetch(self, config):
                # Live window: 120 min duration resetting in 3600 s → the
                # window is half elapsed, so the elapsed-window expectation
                # is 50%. Actual used (55%) is within tolerance → "at".
                return {
                    "status": "success",
                    "quota_windows": [
                        {
                            "id": "w",
                            "used_percent": 55.0,
                            "duration_minutes": 120,
                            "resets_in_seconds": 3600,
                        }
                    ],
                }

        worker = UsageWorker(
            config=sample_config, db=sample_db, registry={"codex": LiveAdapter}
        )
        worker._poll_source(worker._workers["codex"])
        obs = worker._workers["codex"].last_observation
        assert obs is not None
        pace = obs.quota_windows[0].pace
        assert pace["basis"] == "reported"
        assert pace["expectedUsedPercent"] == pytest.approx(50.0)
        assert pace["classification"] == "at"

    def test_unexpected_exception_does_not_kill_scheduler(
        self, sample_config: Config, sample_db: Database, monkeypatch
    ):
        """A source whose polling raises an unexpected (non-CollectionError)
        exception must not take down the scheduler.

        Regression: the loop guard in ``_run()`` only matters for exceptions
        that *escape* ``_poll_source``. A crash inside the adapter's
        ``fetch`` is already caught by ``_poll_source``'s own try/except (into
        an ``unknown`` error), so the test makes the crash originate *after*
        that catch — by monkeypatching ``_poll_source`` to raise for
        antigravity, so the exception reaches the per-source guard in the
        while-loop.

        Ordering matters: ``_workers`` is built in ``SOURCE_DEFS`` order
        (codex, antigravity, ...) and each source is dispatched on its own
        attempt thread, so a crash in one source cannot abort the others.
        The healthy source (codex) keeps polling while the crashing source
        (antigravity) is isolated.
        """
        poll_counts: dict[str, int] = {"codex": 0, "antigravity": 0}

        def make_ok(key: str):
            class _OkAdapter(SourceAdapter):
                def fetch(self, config):
                    poll_counts[key] += 1
                    return {"status": "success", "quota_windows": [{"id": f"{key}-w", "used_percent": 30.0}]}
            return _OkAdapter

        worker = UsageWorker(
            config=sample_config,
            db=sample_db,
            registry={"codex": make_ok("codex"), "antigravity": make_ok("antigravity")},
        )

        # Make antigravity's poll raise an unexpected exception (post-catch),
        # so it escapes _poll_source and hits the _run() per-source guard.
        real_poll = worker._poll_source

        def boom_poll(src):
            # ``src`` is a _SourceWorker; its ``.source`` is a SourceDef, whose
            # ``.source`` is the string key.
            if src.source.source == "antigravity":
                poll_counts["antigravity"] += 1
                raise RuntimeError("boom: unexpected failure outside _poll_source")
            real_poll(src)

        monkeypatch.setattr(worker, "_poll_source", boom_poll)

        # Speed up the loop so several cycles complete quickly. SourceDef is
        # frozen, so use dataclasses.replace to rebuild it.
        for w in worker._workers.values():
            w.source = dataclasses.replace(w.source, cadence_seconds=0)
        worker.start()
        # Wait until codex has been dispatched on at least three cycles, with
        # a bounded deadline, instead of a fixed sleep that depends on the
        # machine keeping up. `stop()` already joins the thread.
        deadline = time.monotonic() + 5.0
        while (
            poll_counts["codex"] < 3
            and worker._thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        worker.stop()

        # antigravity's poll crashed; the scheduler survived it.
        assert poll_counts["antigravity"] >= 1, (
            "antigravity's first attempt should have run and crashed"
        )
        # codex keeps polling on later cycles, proving the crash was isolated
        # to antigravity's own attempt thread.
        assert poll_counts["codex"] >= 3, (
            "codex should have been re-polled on subsequent cycles, "
            "proving a crashing source does not freeze the scheduler"
        )
        # The crash schedules a backoff instead of retrying on every tick.
        assert worker._workers["antigravity"]._next_due_at is not None
        assert worker._workers["codex"].status == SourceStatus.SUCCESS


class TestHttpEndpoints:
    @pytest.fixture
    def server(self, sample_config: Config, sample_db: Database):
        worker = UsageWorker(config=sample_config, db=sample_db, version="0.1.0-test")
        srv = serve_http(sample_config, worker, version="0.1.0-test")
        import threading
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield srv, worker
        srv.shutdown()

    def test_health(self, server):
        srv, _ = server
        url = f"http://127.0.0.1:{srv.port}/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
        assert data["schemaVersion"] == 1
        assert data["serviceStatus"] in ("healthy", "degraded")

    def test_usage(self, server):
        srv, _ = server
        url = f"http://127.0.0.1:{srv.port}/api/v1/usage"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
        assert data["schemaVersion"] == 1
        assert "observations" in data

    def test_version(self, server):
        srv, _ = server
        url = f"http://127.0.0.1:{srv.port}/api/v1/version"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
        assert data["api_version"] == "1"
        assert data["service_version"] == "0.1.0-test"

    def test_404(self, server):
        srv, _ = server
        url = f"http://127.0.0.1:{srv.port}/nonexistent"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=5)
        assert exc_info.value.code == 404


class TestRegistry:
    def test_default_registry_has_all_sources(self):
        reg = default_registry()
        assert "codex" in reg
        assert "antigravity" in reg
        assert "openrouter-account" in reg
        assert "openrouter-key" in reg

    def test_build_registry_overrides(self):
        class CustomAdapter(SourceAdapter):
            def fetch(self, config):
                return {}

        reg = build_registry(overrides={"codex": CustomAdapter})
        assert reg["codex"] is CustomAdapter
        assert reg["antigravity"] is AntigravityAdapter

    def test_codex_adapter_no_socket_configured(self, sample_config: Config):
        adapter = CodexAdapter()
        cfg = dataclasses.replace(sample_config, codex_transport="socket")
        with pytest.raises(CollectionError) as exc_info:
            adapter.fetch(cfg)
        assert exc_info.value.category == "not_configured"

    def test_codex_adapter_socket_absent(self, sample_config: Config, tmp_path):
        cfg = dataclasses.replace(
            sample_config,
            codex_transport="socket",
            codex_app_server_socket=str(tmp_path / "missing.sock"),
        )
        adapter = CodexAdapter()
        with pytest.raises(CollectionError) as exc_info:
            adapter.fetch(cfg)
        assert exc_info.value.category == "unsupported"

    def test_codex_adapter_default_transport_is_stdio(self, sample_config: Config):
        """The default must not depend on a daemon being installed."""
        assert sample_config.codex_transport == "stdio"

    def test_openrouter_adapter_no_key(self, sample_config: Config):
        adapter = OpenRouterAccountAdapter()
        with pytest.raises(CollectionError) as exc_info:
            adapter.fetch(sample_config)
        assert exc_info.value.category == "not_configured"


# ---------------------------------------------------------------------------
# HTTP API contract (spec: widget-usage-api)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _serve(sample_config: Config, sample_db: Database, registry=None, version="0.1.0-test"):
    worker = UsageWorker(config=sample_config, db=sample_db, registry=registry, version=version)
    srv = serve_http(sample_config, worker, version=version)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, worker
    finally:
        srv.shutdown()


def _get(url: str):
    """Return (status, json_body, headers) for a GET, following no redirects."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


def _ok(raw: dict) -> partial:
    """A registry entry: a fresh adapter that always returns ``raw``."""
    return partial(FakeAdapter, raw=raw)


def _success_window(window_id: str, used: float = 10.0) -> dict:
    return {"status": "success", "quota_windows": [{"id": window_id, "used_percent": used}]}


OBS_FIELDS = {
    "provider", "source", "accountId", "observedAt",
    "collectedAt", "staleAfter", "status", "quotaWindows", "budgets",
}


class TestHttpApiContract:
    def test_usage_empty_before_first_success(self, sample_config: Config, sample_db: Database):
        """GET /api/v1/usage returns observations: [] before any success."""
        with _serve(sample_config, sample_db) as (srv, _):
            status, data, headers = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
        assert status == 200
        assert data["schemaVersion"] == 1
        assert "generatedAt" in data
        assert data["observations"] == []
        assert headers.get("Cache-Control") == "private, no-store"

    def test_usage_shape_and_ordering(self, sample_config: Config, sample_db: Database):
        """Observations use the full spec shape, ordered by source ID."""
        registry = {
            "codex": _ok(_success_window("codex-w")),
            "antigravity": _ok(_success_window("agy-w")),
        }
        with _serve(sample_config, sample_db, registry) as (srv, worker):
            worker._poll_source(worker._workers["codex"])
            worker._poll_source(worker._workers["antigravity"])
            status, data, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
        assert status == 200
        assert [o["source"] for o in data["observations"]] == ["antigravity", "codex"]
        entry = next(o for o in data["observations"] if o["source"] == "codex")
        assert set(entry) >= OBS_FIELDS
        assert entry["provider"] == "codex"
        assert entry["status"] == "fresh"
        # Spec: staleAfter is a timestamp string (collectedAt + 2 intervals).
        assert isinstance(entry["staleAfter"], str)
        assert entry["staleAfter"].endswith("Z")
        assert entry["quotaWindows"][0]["id"] == "codex-w"
        assert entry["budgets"] == []

    def test_usage_omits_never_successful_source(self, sample_config: Config, sample_db: Database):
        """A source that never succeeded has no usage entry but stays in /health."""
        registry = {"codex": _ok(_success_window("codex-w"))}
        with _serve(sample_config, sample_db, registry) as (srv, worker):
            worker._poll_source(worker._workers["codex"])
            _, usage, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
            _, health, _ = _get(f"http://127.0.0.1:{srv.port}/health")
        assert [o["source"] for o in usage["observations"]] == ["codex"]
        assert {s["id"] for s in health["sources"]} == {
            "codex", "antigravity", "openrouter-account", "openrouter-key",
        }
        assert health["serviceStatus"] == "degraded"

    def test_usage_retains_prior_success_during_failure(
        self, sample_config: Config, sample_db: Database
    ):
        """A failure retains the last good observation with its freshness state."""
        registry = {"codex": _ok(_success_window("codex-w"))}
        with _serve(sample_config, sample_db, registry) as (srv, worker):
            worker._poll_source(worker._workers["codex"])
            # Swap in a failing adapter and poll again.
            worker._workers["codex"].adapter = FakeAdapter(
                error=CollectionError("network", "network unreachable")
            )
            worker._poll_source(worker._workers["codex"])
            _, usage, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
            _, health, _ = _get(f"http://127.0.0.1:{srv.port}/health")
        assert [o["source"] for o in usage["observations"]] == ["codex"]
        assert usage["observations"][0]["quotaWindows"][0]["id"] == "codex-w"
        codex_health = next(s for s in health["sources"] if s["id"] == "codex")
        assert codex_health["status"] in ("retrying", "operator_action", "unavailable")
        assert codex_health["errorCategory"] == "network"

    def test_usage_serving_does_not_poll(self, sample_config: Config, sample_db: Database):
        """Serving /api/v1/usage must not trigger a provider poll."""
        registry = {"codex": _ok(_success_window("codex-w"))}
        with _serve(sample_config, sample_db, registry) as (srv, worker):
            before = worker._workers["codex"].adapter.fetch_count
            _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
            after = worker._workers["codex"].adapter.fetch_count
        assert after == before

    def test_history_defaults_and_ascending_order(
        self, sample_config: Config, sample_db: Database
    ):
        """History defaults to hours=24, returns ascending collectedAt."""
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        base = datetime.now(UTC)
        for i in range(3):
            _, obs = make_obs("codex")
            ts = base - timedelta(minutes=10 * (3 - i))
            obs.collected_at = ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            obs.quota_windows[0].used_percent = float(i)
            sample_db.insert_observation(source, obs)

        with _serve(sample_config, sample_db) as (srv, _):
            status, data, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage/history")
        assert status == 200
        assert data["hours"] == 24
        assert data["truncated"] is False
        assert len(data["observations"]) == 3
        ts = [o["collectedAt"] for o in data["observations"]]
        assert ts == sorted(ts)
        assert set(data["observations"][0]) >= OBS_FIELDS

    def test_history_limit_and_truncated(self, sample_config: Config, sample_db: Database):
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        for _ in range(5):
            _, obs = make_obs("codex")
            sample_db.insert_observation(source, obs)
        with _serve(sample_config, sample_db) as (srv, _):
            status, data, _ = _get(
                f"http://127.0.0.1:{srv.port}/api/v1/usage/history?limit=2"
            )
        assert status == 200
        assert len(data["observations"]) == 2
        assert data["truncated"] is True

    @pytest.mark.parametrize("qs", ["hours=999", "hours=0", "hours=abc", "limit=0", "limit=9999"])
    def test_history_invalid_query(self, sample_config: Config, sample_db: Database, qs: str):
        with _serve(sample_config, sample_db) as (srv, _):
            status, data, _ = _get(
                f"http://127.0.0.1:{srv.port}/api/v1/usage/history?{qs}"
            )
        assert status == 400
        assert data["schemaVersion"] == 1
        assert data["error"]["category"] == "invalid_query"
        assert "message" in data["error"]

    def test_404_has_safe_error_body(self, sample_config: Config, sample_db: Database):
        with _serve(sample_config, sample_db) as (srv, _):
            status, data, _ = _get(f"http://127.0.0.1:{srv.port}/nope")
        assert status == 404
        assert data["schemaVersion"] == 1
        assert data["error"]["category"] == "not_found"

    def test_method_not_allowed(self, sample_config: Config, sample_db: Database):
        with _serve(sample_config, sample_db) as (srv, _):
            url = f"http://127.0.0.1:{srv.port}/api/v1/usage"
            req = urllib.request.Request(url, data=b"", method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, data = resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                status, data = exc.code, json.loads(exc.read())
        assert status == 405
        assert data["error"]["category"] == "method_not_allowed"

    def test_response_too_large(self, sample_config: Config, sample_db: Database):
        """A payload beyond the configured cap returns response_too_large."""
        tiny = dataclasses.replace(sample_config, response_cap_bytes=1024)
        big = {
            "status": "success",
            "quota_windows": [
                {"id": f"w{i}", "label": "x" * 60, "used_percent": 1.0} for i in range(100)
            ],
        }
        with _serve(tiny, sample_db, {"codex": _ok(big)}) as (srv, worker):
            worker._poll_source(worker._workers["codex"])
            status, data, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
        assert status == 413
        assert data["error"]["category"] == "response_too_large"

    def test_history_returns_413_when_one_observation_exceeds_the_cap(
        self, sample_config: Config, sample_db: Database
    ):
        """Spec: if one observation alone exceeds the cap, return 413.

        Regression: the endpoint used to drop every row and return 200 with an
        empty list, hiding the fact that the request could not be satisfied.
        """
        tiny = dataclasses.replace(sample_config, response_cap_bytes=1024)
        big = {
            "status": "success",
            "quota_windows": [
                {"id": f"w{i}", "label": "x" * 60, "used_percent": 1.0} for i in range(100)
            ],
        }
        with _serve(tiny, sample_db, {"codex": _ok(big)}) as (srv, worker):
            worker._poll_source(worker._workers["codex"])
            status, data, _ = _get(
                f"http://127.0.0.1:{srv.port}/api/v1/usage/history?hours=1&limit=5"
            )
        assert status == 413
        assert data["error"]["category"] == "response_too_large"

    def test_history_trims_oldest_rows_but_keeps_the_newest(
        self, sample_config: Config, sample_db: Database
    ):
        worker = UsageWorker(config=sample_config, db=sample_db, version="t")
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        for index in range(3):
            obs = normalize_observation(
                source,
                {
                    "status": "success",
                    "quota_windows": [
                        {"id": f"w{index}-{n}", "label": "y" * 40, "used_percent": 1.0}
                        for n in range(20)
                    ],
                },
            )
            stamp = datetime.now(UTC) - timedelta(minutes=index)
            obs.collected_at = stamp.replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            sample_db.insert_observation(source, obs)

        full = worker.history_payload(hours=24, limit=100)
        assert len(full["observations"]) == 3
        newest = full["observations"][-1]["collectedAt"]

        one = {**full, "observations": full["observations"][-1:]}
        cap = len(json.dumps(one).encode("utf-8")) + 2

        trimmed = worker.history_payload(hours=24, limit=100, max_bytes=cap)
        assert trimmed["truncated"] is True
        assert len(trimmed["observations"]) == 1
        assert trimmed["observations"][0]["collectedAt"] == newest
        assert cap < len(json.dumps(full).encode("utf-8"))

    def test_history_raises_when_even_metadata_exceeds_the_cap(
        self, sample_config: Config, sample_db: Database
    ):
        worker = UsageWorker(config=sample_config, db=sample_db, version="t")
        with pytest.raises(ResponseTooLarge):
            worker.history_payload(hours=24, limit=100, max_bytes=10)

    def test_configured_pace_tolerance_is_used(
        self, sample_config: Config, sample_db: Database
    ):
        """Regression: the validated ``pace_tolerance`` was never applied.

        A window that is 30 of 60 minutes elapsed expects 50% usage; 62% is
        ``over`` at the 5pp default and ``at`` with a 20pp tolerance.
        """

        class Adapter(SourceAdapter):
            def fetch(self, config):
                return {
                    "status": "success",
                    "quota_windows": [
                        {
                            "id": "w",
                            "used_percent": 62.0,
                            "resetsInSeconds": 1800,
                            "duration_minutes": 60,
                        }
                    ],
                }

        def classification(tolerance: float) -> str:
            worker = UsageWorker(
                config=dataclasses.replace(sample_config, pace_tolerance=tolerance),
                db=sample_db,
                registry={"codex": Adapter},
            )
            assert worker._pace_settings.tolerance_percent == tolerance
            worker._poll_source(worker._workers["codex"])
            return worker._workers["codex"].last_observation.quota_windows[0].pace[
                "classification"
            ]

        assert classification(5.0) == "over"
        assert classification(20.0) == "at"

    def test_server_rejects_non_loopback_binding(self, sample_config: Config, sample_db: Database):
        worker = UsageWorker(config=sample_config, db=sample_db, version="t")
        with pytest.raises(ValueError):
            UsageServer("0.0.0.0", 0, worker, "t", config=sample_config)

    def test_budget_usd_round_trip_after_restart(
        self, sample_config: Config, sample_db: Database
    ):
        """Seeded budgets survive a restart and serialize without raising.

        Regression: ``_initial_seed`` rebuilds Budget objects from stored
        decimal strings; the serializer used to format those strings as
        floats and raise, dropping the whole /usage response.
        """
        source = get_source_def("openrouter-account")
        sample_db.upsert_source(source)
        raw = {
            "status": "success",
            "budgets": [
                {
                    "id": "b1",
                    "scope": "account",
                    "total_credits_usd": 10.0,
                    "total_usage_usd": 2.5,
                    "remaining_usd": 7.5,
                    "limit_usd": 10.0,
                    "used_usd": 2.5,
                }
            ],
        }
        sample_db.insert_observation(source, normalize_observation(source, raw))

        with _serve(sample_config, sample_db) as (srv, _):
            status, data, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
        assert status == 200
        entry = next(o for o in data["observations"] if o["source"] == "openrouter-account")
        budget = entry["budgets"][0]
        assert budget["remainingUsd"] == "7.5"
        assert budget["totalCreditsUsd"] == "10"

    def test_history_returns_newest_rows(self, sample_config: Config, sample_db: Database):
        """Bounded history retains the most recent rows, returned ascending."""
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        base = datetime.now(UTC)
        total = 10
        for i in range(total):
            _, obs = make_obs("codex")
            ts = base - timedelta(minutes=total - i)
            obs.collected_at = ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            obs.quota_windows[0].used_percent = float(i)
            sample_db.insert_observation(source, obs)

        with _serve(sample_config, sample_db) as (srv, _):
            status, data, _ = _get(
                f"http://127.0.0.1:{srv.port}/api/v1/usage/history?limit=3"
            )
        assert status == 200
        assert data["truncated"] is True
        used = [o["quotaWindows"][0]["usedPercent"] for o in data["observations"]]
        assert used == [7.0, 8.0, 9.0]

    def test_partial_openrouter_access(self, sample_config: Config, sample_db: Database):
        """Account credits succeed while the key endpoint cannot run: usage and
        health report the two OpenRouter sources separately."""
        registry = {
            "openrouter-account": _ok(
                {
                    "status": "success",
                    "budgets": [
                        {"id": "acct", "scope": "account", "remaining_usd": 5.0}
                    ],
                }
            ),
            "openrouter-key": partial(
                FakeAdapter, error=CollectionError("unavailable", "no key")
            ),
        }
        with _serve(sample_config, sample_db, registry) as (srv, worker):
            worker._poll_source(worker._workers["openrouter-account"])
            worker._poll_source(worker._workers["openrouter-key"])
            _, usage, _ = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
            _, health, _ = _get(f"http://127.0.0.1:{srv.port}/health")
        assert [o["source"] for o in usage["observations"]] == ["openrouter-account"]
        by_id = {s["id"]: s for s in health["sources"]}
        assert by_id["openrouter-account"]["status"] == "healthy"
        assert by_id["openrouter-key"]["status"] == "unavailable"
        assert by_id["openrouter-key"]["errorCategory"] is None
        assert health["serviceStatus"] == "degraded"

    def test_history_trims_to_byte_cap(self, sample_config: Config, sample_db: Database):
        """History trims to the byte cap and sets truncated, never 413."""
        tiny = dataclasses.replace(sample_config, response_cap_bytes=2048)
        source = get_source_def("codex")
        sample_db.upsert_source(source)
        for i in range(20):
            raw = {
                "status": "success",
                "quota_windows": [
                    {"id": f"w{i}", "label": "x" * 100, "used_percent": 1.0}
                ],
            }
            sample_db.insert_observation(source, normalize_observation(source, raw))

        with _serve(tiny, sample_db) as (srv, _):
            status, data, _ = _get(
                f"http://127.0.0.1:{srv.port}/api/v1/usage/history?limit=500"
            )
        assert status == 200
        assert data["truncated"] is True
        assert 0 < len(data["observations"]) < 20
        # The serialized payload must fit the configured cap.
        assert len(json.dumps(data).encode("utf-8")) <= tiny.response_cap_bytes

    def test_two_servers_are_isolated(self, sample_config: Config, sample_db: Database):
        """Two live servers must not share worker/version/config state."""
        with _serve(sample_config, sample_db, version="alpha") as (srv_a, _):
            with _serve(sample_config, sample_db, version="beta") as (srv_b, _):
                _, a, _ = _get(f"http://127.0.0.1:{srv_a.port}/api/v1/version")
                _, b, _ = _get(f"http://127.0.0.1:{srv_b.port}/api/v1/version")
        assert a["service_version"] == "alpha"
        assert b["service_version"] == "beta"

    def test_internal_error_returns_500(
        self, sample_config: Config, sample_db: Database, monkeypatch
    ):
        """An unexpected failure returns a safe 500 JSON body, not a drop."""
        with _serve(sample_config, sample_db) as (srv, worker):
            monkeypatch.setattr(
                worker, "usage_payload", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            )
            status, data, headers = _get(f"http://127.0.0.1:{srv.port}/api/v1/usage")
        assert status == 500
        assert data["schemaVersion"] == 1
        assert data["error"]["category"] == "internal"
        assert "boom" not in data["error"]["message"]

    def test_post_body_does_not_desync_keepalive(self, sample_config: Config, sample_db: Database):
        """A 405 must drain the request body so the next request parses."""
        import http.client

        with _serve(sample_config, sample_db) as (srv, _):
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            try:
                conn.request(
                    "POST",
                    "/api/v1/usage",
                    body=b'{"a": 1}',
                    headers={"Content-Type": "application/json"},
                )
                r1 = conn.getresponse()
                r1.read()
                assert r1.status == 405
                assert r1.getheader("Allow") == "GET"
                conn.request("GET", "/health")
                r2 = conn.getresponse()
                body2 = r2.read()
                assert r2.status == 200
                assert r2.getheader("Content-Type") == "application/json"
                json.loads(body2)
            finally:
                conn.close()

    @pytest.mark.parametrize(
        "category, expected",
        [("unavailable", None), ("unknown", "internal"), ("network", "network")],
    )
    def test_health_error_category_uses_spec_enum(
        self, sample_config: Config, sample_db: Database, category: str, expected
    ):
        registry = {
            "codex": partial(
                FakeAdapter, error=CollectionError(category, "boom")
            )
        }
        with _serve(sample_config, sample_db, registry) as (srv, worker):
            worker._poll_source(worker._workers["codex"])
            _, health, _ = _get(f"http://127.0.0.1:{srv.port}/health")
        codex = next(s for s in health["sources"] if s["id"] == "codex")
        assert codex["errorCategory"] == expected
