"""Integration tests: worker polling, payloads, HTTP server, and registry."""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.models import SourceStatus, get_source_def, normalize_observation
from ai_monitor.sources.registry import (
    AntigravityAdapter,
    CodexAdapter,
    CollectionError,
    OpenRouterAccountAdapter,
    OpenRouterKeyAdapter,
    SourceAdapter,
    build_registry,
    default_registry,
)
from ai_monitor.state import UsageWorker

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
        assert payload["serviceStatus"] in ("healthy", "degraded")
        assert isinstance(payload["sources"], list)
        assert len(payload["sources"]) == 4

        source_names = [s["source"] for s in payload["sources"]]
        assert "codex" in source_names
        assert "antigravity" in source_names
        assert "openrouter-account" in source_names
        assert "openrouter-key" in source_names

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
        assert "quota" in entry

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

    def test_codex_adapter_missing_socket(self, sample_config: Config):
        adapter = CodexAdapter()
        with pytest.raises(CollectionError) as exc_info:
            adapter.fetch(sample_config)
        assert exc_info.value.category == "unavailable"

    def test_openrouter_adapter_no_key(self, sample_config: Config):
        adapter = OpenRouterAccountAdapter()
        with pytest.raises(CollectionError) as exc_info:
            adapter.fetch(sample_config)
        assert exc_info.value.category == "unavailable"
