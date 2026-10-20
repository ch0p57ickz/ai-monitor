"""Unit tests for ai_monitor.db — schema, inserts, reads, retention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_monitor.db import Database, SCHEMA_VERSION
from ai_monitor.models import get_source_def, normalize_observation


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_obs(source_key: str, collected_at: str | None = None):
    raw = {
        "status": "success",
        "quota_windows": [{"id": "w1", "label": "Weekly", "used_percent": 50.0}],
    }
    source = get_source_def(source_key)
    obs = normalize_observation(source, raw)
    if collected_at:
        obs.collected_at = collected_at
    return source, obs


class TestDatabaseSchema:
    def test_tables_exist(self, sample_db: Database):
        tables = {
            row[0]
            for row in sample_db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "observations" in tables
        assert "schema_meta" in tables

    def test_schema_version(self, sample_db: Database):
        assert SCHEMA_VERSION == 2


class TestInsertObservation:
    def test_insert_and_read_back(self, sample_db: Database):
        source, obs = make_obs("codex")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs)

        latest = sample_db.latest_per_source()
        codex = next(e for e in latest if e["source"] == "codex")
        assert codex["status"] == "success"
        assert len(codex["quota_windows"]) == 1
        assert codex["quota_windows"][0]["id"] == "w1"

    def test_insert_multiple_sources(self, sample_db: Database):
        for key in ("codex", "antigravity", "openrouter-account"):
            source, obs = make_obs(key)
            sample_db.upsert_source(source)
            sample_db.insert_observation(source, obs)

        latest = sample_db.latest_per_source()
        sources = {e["source"] for e in latest if e["status"] != "no_data"}
        assert sources == {"codex", "antigravity", "openrouter-account"}


class TestLatestPerSource:
    def test_returns_only_latest(self, sample_db: Database):
        older = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        now = now_iso()

        s, obs_old = make_obs("codex", older)
        sample_db.upsert_source(s)
        sample_db.insert_observation(s, obs_old)

        s2, obs_new = make_obs("codex", now)
        sample_db.insert_observation(s2, obs_new)

        s_agy, obs_agy = make_obs("antigravity", now)
        sample_db.upsert_source(s_agy)
        sample_db.insert_observation(s_agy, obs_agy)

        latest = sample_db.latest_per_source()
        codex = next(e for e in latest if e["source"] == "codex")
        assert codex["collected_at"] == now
        agy = next(e for e in latest if e["source"] == "antigravity")
        assert agy["collected_at"] == now

    def test_empty_db(self, sample_db: Database):
        assert sample_db.latest_per_source() == []


class TestHistory:
    def test_history_within_window(self, sample_db: Database):
        source, obs = make_obs("codex", now_iso())
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs)

        rows, truncated = sample_db.history(hours=24, limit=100, source="codex")
        assert len(rows) == 1
        assert truncated is False

    def test_history_respects_limit(self, sample_db: Database):
        now = now_iso()
        for _ in range(5):
            source, obs = make_obs("codex", now)
            sample_db.upsert_source(source)
            sample_db.insert_observation(source, obs)

        rows, truncated = sample_db.history(hours=24, limit=3, source="codex")
        assert len(rows) == 3
        assert truncated is True


class TestRetention:
    def test_apply_retention_deletes_old(self, sample_db: Database):
        source, obs_old = make_obs("codex", "2020-01-01T00:00:00Z")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs_old)

        s2, obs_new = make_obs("codex", now_iso())
        sample_db.insert_observation(s2, obs_new)

        deleted = sample_db.apply_retention(retention_days=30)
        assert deleted >= 1

        latest = sample_db.latest_per_source()
        codex = next(e for e in latest if e["source"] == "codex")
        assert codex["status"] == "success"

    def test_apply_retention_keeps_recent(self, sample_db: Database):
        source, obs = make_obs("codex", now_iso())
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs)

        deleted = sample_db.apply_retention(retention_days=30)
        assert deleted == 0

        latest = sample_db.latest_per_source()
        codex = next(e for e in latest if e["source"] == "codex")
        assert codex["status"] == "success"

    def test_apply_retention_keeps_the_latest_row_of_a_dormant_source(
        self, sample_db: Database
    ):
        """The spec: retention must not affect the latest observation for each source.

        A source that has been failing longer than the window has only its one row, and
        losing it would leave `/api/v1/usage` with nothing to serve and the next restart
        with nothing to seed from.
        """
        source, obs_old = make_obs("codex", "2020-01-01T00:00:00Z")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs_old)

        deleted = sample_db.apply_retention(retention_days=30)

        assert deleted == 0
        codex = next(
            e for e in sample_db.latest_per_source() if e["source"] == "codex"
        )
        assert codex["status"] == "success"
        assert codex["collected_at"] == "2020-01-01T00:00:00Z"

    def test_apply_retention_still_deletes_older_rows_of_that_source(
        self, sample_db: Database
    ):
        """One row per source is protected, not the whole source."""
        source, obs_oldest = make_obs("codex", "2020-01-01T00:00:00Z")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs_oldest)
        _, obs_older = make_obs("codex", "2020-06-01T00:00:00Z")
        sample_db.insert_observation(source, obs_older)
        _, obs_new = make_obs("codex", now_iso())
        sample_db.insert_observation(source, obs_new)

        # Both expired rows go: the protected one is the newest, which is current.
        deleted = sample_db.apply_retention(retention_days=30)

        assert deleted == 2
        codex = next(
            e for e in sample_db.latest_per_source() if e["source"] == "codex"
        )
        assert codex["collected_at"] == obs_new.collected_at

    def test_retention_keeps_the_row_the_reads_would_have_served(
        self, sample_db: Database
    ):
        """With several expired rows, the survivor is the one ``latest_per_source`` picks.

        Retention and the read path must agree on which row is "the latest", or a
        source could keep a row that nothing serves.
        """
        source, obs_oldest = make_obs("codex", "2020-01-01T00:00:00Z")
        sample_db.upsert_source(source)
        sample_db.insert_observation(source, obs_oldest)
        _, obs_newer = make_obs("codex", "2020-06-01T00:00:00Z")
        sample_db.insert_observation(source, obs_newer)

        before = next(
            e for e in sample_db.latest_per_source() if e["source"] == "codex"
        )

        deleted = sample_db.apply_retention(retention_days=30)

        assert deleted == 1
        after = next(
            e for e in sample_db.latest_per_source() if e["source"] == "codex"
        )
        assert after["collected_at"] == before["collected_at"] == "2020-06-01T00:00:00Z"

    def test_apply_retention_with_no_rows_deletes_nothing(self, sample_db: Database):
        """An empty protected set must not turn into a no-op ``NOT IN (NULL)``."""
        assert sample_db.apply_retention(retention_days=30) == 0
