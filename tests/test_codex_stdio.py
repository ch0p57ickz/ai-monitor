"""Tests for the Codex stdio transport (`codex_transport = "stdio"`).

The stdio transport spawns `codex app-server --listen stdio://` as a child
process instead of connecting to the app-server daemon's control socket. It is
what makes the Codex source work on hosts where the daemon cannot start, so the
child's lifecycle (start, reuse, teardown, restart) is part of the contract.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from ai_monitor.config import Config, ConfigError
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.sources.codex import CodexAdapter
from ai_monitor.sources.registry import CollectionError

FAKE = Path(__file__).parent / "fake_codex_app_server.py"


def _wrapper(tmp_path: Path) -> Path:
    """A `codex_bin` that runs the fake with the *test's* interpreter.

    Going through a wrapper keeps the tests independent of whichever `python3`
    happens to be first on PATH.
    """
    path = tmp_path / "fake-codex"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    path.chmod(0o755)
    return path

KEYED = {
    "rateLimitsByLimitId": {
        "codex": {
            "primary": {
                "usedPercent": 33,
                "windowDurationMins": 300,
                "resetsAt": 1760000000,
            },
            "secondary": {"usedPercent": 11, "windowDurationMins": 10080},
        }
    }
}


@pytest.fixture
def fake(monkeypatch, tmp_path: Path):
    """Point `codex_bin` at the fake app-server and give it a scratch env."""
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "starts.log"))
    monkeypatch.setenv("FAKE_PAYLOAD", str(tmp_path / "payload.json"))
    (tmp_path / "payload.json").write_text(json.dumps(KEYED))
    _wrapper(tmp_path)
    return tmp_path


def _config(tmp_path: Path, **overrides) -> Config:
    defaults = dict(
        data_dir=str(tmp_path / "data"),
        codex_transport="stdio",
        codex_bin=str(tmp_path / "fake-codex"),
        codex_timeout_seconds=10,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _starts(tmp_path: Path) -> int:
    marker = tmp_path / "starts.log"
    if not marker.exists():
        return 0
    return len([line for line in marker.read_text().splitlines() if line.strip()])


class TestStdioConfig:
    def test_unknown_transport_is_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError) as exc_info:
            Config(data_dir=str(tmp_path), codex_transport="carrier-pigeon")
        assert exc_info.value.category == "invalid_value"

    def test_stdio_requires_a_binary(self, tmp_path: Path):
        with pytest.raises(ConfigError) as exc_info:
            Config(
                data_dir=str(tmp_path),
                codex_transport="stdio",
                codex_bin="",
            )
        assert exc_info.value.category == "invalid_value"

    def test_socket_remains_the_default(self, tmp_path: Path):
        config = Config(data_dir=str(tmp_path))
        assert config.codex_transport == "socket"
        assert config.codex_bin == "codex"


class TestStdioTransport:
    def test_collects_windows(self, fake: Path):
        adapter = CodexAdapter()
        try:
            out = adapter.fetch(_config(fake))
        finally:
            adapter.close()

        assert out["status"] == "success"
        assert out["notes"]["view"] == "keyed"
        ids = [w["id"] for w in out["quota_windows"]]
        assert ids == ["codex:codex:primary", "codex:codex:secondary"]
        assert out["quota_windows"][0]["used_percent"] == 33.0
        assert out["quota_windows"][0]["resets_at"] == "2025-10-09T08:53:20Z"

    def test_notifications_on_stdio_are_not_mistaken_for_the_reply(self, fake: Path):
        """The fake always emits `account/updated` after initialize."""
        adapter = CodexAdapter()
        try:
            out = adapter.fetch(_config(fake))
        finally:
            adapter.close()
        assert out["quota_windows"][1]["used_percent"] == 11.0

    def test_normalizes_into_quota_windows(self, fake: Path):
        adapter = CodexAdapter()
        try:
            raw = adapter.fetch(_config(fake))
        finally:
            adapter.close()
        obs = normalize_observation(get_source_def("codex"), raw)
        assert obs.quota_windows[0].duration_minutes == 300
        assert obs.quota_windows[0].resets_at == "2025-10-09T08:53:20Z"

    def test_the_child_is_reused_across_polls(self, fake: Path):
        adapter = CodexAdapter()
        try:
            adapter.fetch(_config(fake))
            adapter.fetch(_config(fake))
        finally:
            adapter.close()
        assert _starts(fake) == 1, "the child must be reused, not respawned per poll"

    def test_a_failed_exchange_tears_the_child_down(self, fake: Path, monkeypatch):
        """After a failure the next poll must talk to a fresh child."""
        monkeypatch.setenv("FAKE_MODE", "die")
        adapter = CodexAdapter()
        try:
            with pytest.raises(CollectionError):
                adapter.fetch(_config(fake))
            # The child is gone; a working child is started on the next attempt.
            monkeypatch.setenv("FAKE_MODE", "ok")
            out = adapter.fetch(_config(fake))
        finally:
            adapter.close()

        assert out["status"] == "success"
        assert _starts(fake) == 2

    def test_a_hung_child_hits_the_timeout(self, fake: Path, monkeypatch):
        monkeypatch.setenv("FAKE_MODE", "hang")
        adapter = CodexAdapter()
        try:
            with pytest.raises(CollectionError) as exc_info:
                adapter.fetch(_config(fake, codex_timeout_seconds=1))
        finally:
            adapter.close()
        assert exc_info.value.category == "timeout"

    def test_a_missing_binary_is_unsupported(self, fake: Path):
        adapter = CodexAdapter()
        try:
            with pytest.raises(CollectionError) as exc_info:
                adapter.fetch(_config(fake, codex_bin=str(fake / "not-a-program")))
        finally:
            adapter.close()
        assert exc_info.value.category == "unsupported"

    def test_close_is_idempotent_and_ends_the_child(self, fake: Path):
        adapter = CodexAdapter()
        adapter.fetch(_config(fake))
        session = adapter._session
        assert session is not None
        adapter.close()
        adapter.close()
        assert session._proc is None

    def test_close_aborts_a_child_that_is_mid_request(self, fake: Path, monkeypatch):
        """Shutdown must not wait out the timeout of an in-flight read."""
        monkeypatch.setenv("FAKE_MODE", "hang")
        adapter = CodexAdapter()
        config = _config(fake, codex_timeout_seconds=30)
        errors: list = []

        def run() -> None:
            try:
                adapter.fetch(config)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(1.0)  # let the request reach the blocked read
        started = time.monotonic()
        adapter.close()
        elapsed = time.monotonic() - started
        worker.join(timeout=10)

        assert elapsed < 5, "close() must abort the child, not wait for the deadline"
        assert errors and errors[0].category == "network"

    def test_socket_transport_never_spawns_a_child(self, fake: Path):
        """A socket deployment must not start the stdio child at all."""
        adapter = CodexAdapter()
        with pytest.raises(CollectionError):
            adapter.fetch(_config(fake, codex_transport="socket"))
        assert _starts(fake) == 0
        assert adapter._session is None

    def test_worker_stop_releases_the_child(self, fake: Path, sample_db):
        from ai_monitor.state import UsageWorker

        adapter = CodexAdapter()
        adapter.fetch(_config(fake))
        session = adapter._session
        assert session is not None and session._proc is not None

        worker = UsageWorker(config=_config(fake), db=sample_db, version="t")
        worker._workers["codex"].adapter = adapter
        worker.stop()

        assert session._proc is None


class TestStdioFraming:
    def test_a_reply_split_across_writes_is_reassembled(self, fake: Path, monkeypatch):
        """A reply arriving in two pieces must still parse (line buffering)."""
        monkeypatch.setenv("FAKE_MODE", "split")
        adapter = CodexAdapter()
        try:
            out = adapter.fetch(_config(fake))
        finally:
            adapter.close()
        assert out["status"] == "success"
        assert out["quota_windows"][0]["used_percent"] == 33.0
