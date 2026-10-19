"""SQLite persistence for usage observations.

Concurrency model
-----------------
A single scheduler thread is the only writer. The HTTP handler thread reads
the latest observation per source. SQLite in WAL mode with
``busy_timeout=5000`` keeps the two from dead-locking; the writer uses short
explicit transactions.

All timestamps are stored as ISO-8601 UTC strings with a ``Z`` suffix
(second precision).

Privacy: raw provider payloads are NEVER persisted (spec requirement).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ai_monitor.models import Observation, SourceDef

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    key             TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    label           TEXT NOT NULL,
    window          TEXT NOT NULL,
    cadence_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    collected_at     TEXT    NOT NULL,
    observed_at      TEXT,
    account_id       TEXT,
    status           TEXT    NOT NULL,
    quota_windows_json TEXT,
    budgets_json     TEXT,
    notes_json       TEXT,
    FOREIGN KEY (source) REFERENCES sources (key)
);

CREATE INDEX IF NOT EXISTS idx_obs_source_time
    ON observations (source, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_obs_collected
    ON observations (collected_at);
"""


def _dumps(obj: Any) -> str | None:
    """Serialize to compact JSON, or None when the value is absent/unknown."""
    if obj is None:
        return None
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def _loads(s: str | None) -> Any:
    return json.loads(s) if s else None


class Database:
    """WAL-mode SQLite wrapper for the usage service.

    A single connection is created at construction time and guarded by a
    lock. The scheduler is the only writer and writes are short, so one lock
    is sufficient and avoids cross-thread connection sharing.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) "
                "VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # writes (scheduler thread only)
    # ------------------------------------------------------------------

    def upsert_source(self, source: "SourceDef") -> None:
        """Register (or update) a source's static metadata."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sources
                    (key, provider, label, window, cadence_seconds)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    provider=excluded.provider,
                    label=excluded.label,
                    window=excluded.window,
                    cadence_seconds=excluded.cadence_seconds
                """,
                (
                    source.source,
                    source.provider,
                    source.label,
                    source.window,
                    source.cadence_seconds,
                ),
            )

    def insert_observation(self, source: "SourceDef", obs: "Observation") -> int:
        """Insert a successful observation and return its row id.

        Only successful observations are stored. Failed polls are tracked
        in-memory for health only. Unknown values are stored as JSON nulls.
        """
        if obs.status.value != "success":
            return 0

        with self._lock:
            quota_windows = [
                w.to_dict() if hasattr(w, "to_dict") else w
                for w in obs.quota_windows
            ]
            budgets = [
                b.to_dict() if hasattr(b, "to_dict") else b
                for b in obs.budgets
            ]
            cur = self._conn.execute(
                """
                INSERT INTO observations
                    (source, provider, collected_at, observed_at, account_id,
                     status, quota_windows_json, budgets_json, notes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs.source,
                    obs.provider,
                    obs.collected_at,
                    obs.observed_at,
                    obs.account_id,
                    obs.status.value,
                    _dumps(quota_windows),
                    _dumps(budgets),
                    _dumps(obs.notes),
                ),
            )
            return int(cur.lastrowid or 0)

    def apply_retention(self, retention_days: int) -> int:
        """Delete observations strictly older than ``retention_days``.

        Returns the number of rows removed. VACUUM is run in autocommit
        mode (no explicit transaction) to avoid SQLite's "cannot VACUUM
        from within a transaction" error.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM observations WHERE collected_at < ?", (cutoff,)
            )
            deleted = cur.rowcount
            if deleted and deleted > 10000:
                self._conn.execute("VACUUM")
            return deleted

    # ------------------------------------------------------------------
    # reads (HTTP handler thread)
    # ------------------------------------------------------------------

    def latest_per_source(self) -> list[dict[str, Any]]:
        """Latest *successful* observation row per source.

        Only rows with ``status = 'success'`` are joined. Sources with no
        successful observations are returned with ``status = 'no_data'``.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    s.key             AS source,
                    s.provider        AS provider,
                    s.label           AS label,
                    s.window          AS window,
                    s.cadence_seconds AS cadence_seconds,
                    o.id              AS id,
                    o.collected_at    AS collected_at,
                    o.observed_at     AS observed_at,
                    o.account_id      AS account_id,
                    o.status          AS status,
                    o.quota_windows_json AS quota_windows_json,
                    o.budgets_json      AS budgets_json,
                    o.notes_json        AS notes_json
                FROM sources s
                LEFT JOIN observations o ON
                    o.id = (
                        SELECT o2.id FROM observations o2
                        WHERE o2.source = s.key
                          AND o2.status = 'success'
                        ORDER BY o2.collected_at DESC, o2.id DESC
                        LIMIT 1
                    )
                ORDER BY s.key
                """
            ).fetchall()
        return [_row_to_latest(row) for row in rows]

    def history(
        self,
        hours: int,
        limit: int,
        source: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return successful observations in ascending collected_at order.

        Returns (observations, truncated). ``truncated`` is True when more
        matching records exist than the limit allows.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            if source:
                rows = self._conn.execute(
                    """
                    SELECT source, provider, collected_at, observed_at,
                           account_id, status, quota_windows_json,
                           budgets_json, notes_json
                    FROM observations
                    WHERE collected_at >= ? AND status = 'success'
                      AND source = ?
                    ORDER BY collected_at ASC
                    LIMIT ?
                    """,
                    (cutoff, source, limit + 1),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT source, provider, collected_at, observed_at,
                           account_id, status, quota_windows_json,
                           budgets_json, notes_json
                    FROM observations
                    WHERE collected_at >= ? AND status = 'success'
                    ORDER BY collected_at ASC
                    LIMIT ?
                    """,
                    (cutoff, limit + 1),
                ).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        return [_row_to_obs(row) for row in rows], truncated


def _row_to_latest(row: sqlite3.Row) -> dict[str, Any]:
    has_obs = row["id"] is not None
    base = {
        "source": row["source"],
        "provider": row["provider"],
        "label": row["label"],
        "window": row["window"],
        "cadence_seconds": row["cadence_seconds"],
    }
    if not has_obs:
        return {
            **base,
            "status": "no_data",
            "collected_at": None,
            "observed_at": None,
            "account_id": None,
            "quota_windows": [],
            "budgets": [],
            "notes": None,
        }
    return {
        **base,
        "status": row["status"],
        "collected_at": row["collected_at"],
        "observed_at": row["observed_at"],
        "account_id": row["account_id"],
        "quota_windows": _loads(row["quota_windows_json"]) or [],
        "budgets": _loads(row["budgets_json"]) or [],
        "notes": _loads(row["notes_json"]),
    }


def _row_to_obs(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "provider": row["provider"],
        "source": row["source"],
        "accountId": row["account_id"],
        "observedAt": row["observed_at"],
        "collectedAt": row["collected_at"],
        "staleAfter": None,
        "status": "fresh",
        "quotaWindows": _loads(row["quota_windows_json"]) or [],
        "budgets": _loads(row["budgets_json"]) or [],
    }
