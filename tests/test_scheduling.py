"""Tests for per-source scheduling: deadlines, backoff, jitter, Retry-After.

Covers task 3.4: independent schedules, no same-source overlap, capped local
backoff with jitter, a provider `Retry-After` as an uncapped minimum delay,
operator-action handling for 401/403, and a failing or hung source that does
not delay another.
"""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from ai_monitor.config import Config
from ai_monitor.sources.registry import (
    CollectionError,
    SourceAdapter,
)
from ai_monitor.state import UsageWorker


def _worker(sample_config: Config, sample_db, registry) -> UsageWorker:
    return UsageWorker(config=sample_config, db=sample_db, registry=registry)


class _FixedAdapter(SourceAdapter):
    """Raises a fixed CollectionError, or succeeds when ``error`` is None."""

    error: CollectionError | None = None

    def fetch(self, config: Config) -> dict:
        if self.error is not None:
            raise self.error
        return {"status": "success", "quota_windows": []}


class TestNextDelay:
    def test_success_waits_the_cadence(self, sample_config, sample_db):
        worker = _worker(sample_config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        assert worker._next_delay(src, None) == src.source.cadence_seconds

    def test_transient_failure_backs_off_exponentially(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=5.0, retry_max_delay=300.0,
            retry_backoff_factor=2.0, retry_jitter=0.0,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]

        src._consecutive_failures = 1
        assert worker._next_delay(src, "network") == 5.0
        src._consecutive_failures = 2
        assert worker._next_delay(src, "network") == 10.0
        src._consecutive_failures = 3
        assert worker._next_delay(src, "network") == 20.0

    def test_backoff_is_capped(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=5.0, retry_max_delay=30.0,
            retry_backoff_factor=2.0, retry_jitter=0.0,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        src._consecutive_failures = 10
        assert worker._next_delay(src, "network") == 30.0

    def test_jitter_only_shrinks_the_delay(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=100.0, retry_max_delay=300.0,
            retry_backoff_factor=2.0, retry_jitter=0.2,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        src._consecutive_failures = 1
        delays = {worker._next_delay(src, "network") for _ in range(50)}
        assert all(80.0 <= d <= 100.0 for d in delays)
        assert len(delays) > 1, "jitter should vary the delay"

    def test_operator_action_does_not_retry_rapidly(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_max_delay=300.0, retry_jitter=0.0
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        src._consecutive_failures = 1
        assert worker._next_delay(src, "authentication") == 300.0
        assert worker._next_delay(src, "authorization") == 300.0
        assert worker._next_delay(src, "not_configured") == 300.0

    def test_retry_after_exceeds_the_local_cap(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=5.0, retry_max_delay=300.0,
            retry_backoff_factor=2.0, retry_jitter=0.0,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        src._consecutive_failures = 1
        src._retry_after = 3600.0
        # 3600s is far above the 300s cap and must be honored in full.
        assert worker._next_delay(src, "rate_limited") == 3600.0

    def test_retry_after_shorter_than_backoff_is_ignored(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=60.0, retry_max_delay=300.0,
            retry_backoff_factor=2.0, retry_jitter=0.0,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        src._consecutive_failures = 1
        src._retry_after = 1.0
        assert worker._next_delay(src, "rate_limited") == 60.0


class TestPollSourceScheduling:
    def test_failure_sets_the_next_due_time(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=5.0, retry_max_delay=300.0,
            retry_backoff_factor=2.0, retry_jitter=0.0,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        _FixedAdapter.error = CollectionError("network", "boom")
        try:
            before = time.monotonic()
            worker._poll_source(worker._workers["codex"])
        finally:
            _FixedAdapter.error = None

        src = worker._workers["codex"]
        assert src._next_due_at is not None
        assert src._next_due_at - before == pytest.approx(5.0, abs=1.0)
        assert not src.poll_due()

    def test_retry_after_from_the_adapter_is_honored(self, sample_config, sample_db):
        config = dataclasses.replace(
            sample_config, retry_base_delay=5.0, retry_max_delay=300.0,
            retry_backoff_factor=2.0, retry_jitter=0.0,
        )
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        _FixedAdapter.error = CollectionError(
            "rate_limited", "slow down", retry_after=3600.0
        )
        try:
            before = time.monotonic()
            worker._poll_source(worker._workers["codex"])
        finally:
            _FixedAdapter.error = None

        src = worker._workers["codex"]
        assert src._next_due_at - before == pytest.approx(3600.0, abs=1.0)

    def test_success_sets_the_cadence(self, sample_config, sample_db):
        worker = _worker(sample_config, sample_db, {"codex": _FixedAdapter})
        before = time.monotonic()
        worker._poll_source(worker._workers["codex"])
        src = worker._workers["codex"]
        assert src._next_due_at - before == pytest.approx(
            src.source.cadence_seconds, abs=1.0
        )


class TestDeadline:
    def test_hung_source_is_abandoned_at_the_deadline(self, sample_config, sample_db):
        config = dataclasses.replace(sample_config, deadline=1.0)

        class Hung(SourceAdapter):
            def fetch(self, config: Config) -> dict:
                time.sleep(5)
                return {"status": "success"}

        worker = _worker(config, sample_db, {"codex": Hung})
        started = time.monotonic()
        worker._poll_source(worker._workers["codex"])
        elapsed = time.monotonic() - started

        assert elapsed < 3.0, "the attempt must not wait for the hung provider"
        src = worker._workers["codex"]
        assert src.last_error_category == "timeout"
        assert src.status.name == "ERROR"


class TestDispatcher:
    def test_hung_source_does_not_delay_another(self, sample_config, sample_db):
        config = dataclasses.replace(sample_config, deadline=10.0)
        slow_started = threading.Event()

        class Slow(SourceAdapter):
            def fetch(self, config: Config) -> dict:
                slow_started.set()
                time.sleep(2.0)
                return {"status": "success", "quota_windows": []}

        fast_polls = {"n": 0}

        class Fast(SourceAdapter):
            def fetch(self, config: Config) -> dict:
                fast_polls["n"] += 1
                return {"status": "success", "quota_windows": []}

        worker = _worker(
            config, sample_db, {"codex": Slow, "antigravity": Fast}
        )
        # Poll both sources often; only the hung codex attempt should stall.
        for w in worker._workers.values():
            w.source = dataclasses.replace(w.source, cadence_seconds=0.1)
        worker.start()
        try:
            assert slow_started.wait(timeout=5.0)
            # While codex is still blocked, antigravity must keep polling.
            deadline = time.monotonic() + 2.0
            while fast_polls["n"] < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert fast_polls["n"] >= 2
        finally:
            worker.stop()

    def test_no_overlapping_attempt_for_the_same_source(self, sample_config, sample_db):
        config = dataclasses.replace(sample_config, deadline=10.0)
        state = {"active": 0, "max": 0, "polls": 0}
        lock = threading.Lock()

        class Counting(SourceAdapter):
            def fetch(self, config: Config) -> dict:
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                    state["polls"] += 1
                time.sleep(0.1)
                with lock:
                    state["active"] -= 1
                return {"status": "success", "quota_windows": []}

        worker = _worker(config, sample_db, {"codex": Counting})
        # Make the source always due so only the in-flight guard can serialize.
        for w in worker._workers.values():
            w.source = dataclasses.replace(w.source, cadence_seconds=0)
        worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while state["polls"] < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            worker.stop()

        assert state["polls"] >= 3
        assert state["max"] == 1, "a source must never have overlapping attempts"

    def test_stop_joins_in_flight_attempts(self, sample_config, sample_db):
        config = dataclasses.replace(sample_config, deadline=10.0)

        class Slow(SourceAdapter):
            def fetch(self, config: Config) -> dict:
                time.sleep(0.5)
                return {"status": "success", "quota_windows": []}

        worker = _worker(config, sample_db, {"codex": Slow})
        worker.start()
        time.sleep(0.1)  # let the attempt start
        worker.stop()
        assert not worker._thread.is_alive()

    def test_finished_attempts_are_released(self, sample_config, sample_db):
        worker = _worker(sample_config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]
        worker._dispatch(src)
        thread = worker._attempts["codex"]
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert worker._attempts == {}
        assert src._in_flight is False

    def test_crashed_attempt_still_backs_off(self, sample_config, sample_db, monkeypatch):
        config = dataclasses.replace(sample_config, retry_base_delay=5.0, retry_jitter=0.0)
        worker = _worker(config, sample_db, {"codex": _FixedAdapter})
        src = worker._workers["codex"]

        def boom(worker_arg):
            raise RuntimeError("crash outside the adapter catch")

        monkeypatch.setattr(worker, "_poll_source", boom)
        worker._dispatch(src)
        worker._attempts["codex"].join(timeout=5)

        # A crash must not leave the source retrying on every scheduler tick.
        assert src._next_due_at is not None
        assert not src.poll_due()
        assert src._in_flight is False
