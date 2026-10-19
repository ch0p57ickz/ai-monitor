"""Tests for the backup/restore verification tool and the documents it checks.

The tool itself is end-to-end (it starts a service and runs the `sqlite3` CLI),
so these tests cover the parts that decide its verdict, plus two invariants that
would otherwise only fail on a deployed host:

- the example config the install step copies must load through the real `Config`;
- the runbook's backup procedure must create the directory it writes into.

That second one was a real defect: `install -m 0600 <file> /var/lib/ai-monitor/
backups/` fails with "Not a directory" when the directory does not exist, and the
runbook never created it.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from ai_monitor.config import Config

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "gx10-runbook.md"
EXAMPLE_CONFIG = REPO / "deploy" / "config.example.json"


@pytest.fixture(scope="module")
def check():
    """Load the tool as a module without running its entry point."""
    spec = importlib.util.spec_from_file_location(
        "backup_restore_check", REPO / "tools" / "backup_restore_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path, rows: int = 2) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            source TEXT, provider TEXT, collected_at TEXT, observed_at TEXT,
            account_id TEXT, status TEXT, quota_windows_json TEXT,
            budgets_json TEXT, notes_json TEXT
        )
        """
    )
    for index in range(rows):
        connection.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (index + 1, "codex", "codex", "2026-10-19T12:00:00Z",
             "2026-10-19T12:00:00Z", None, "success", "[]", "[]", "{}"),
        )
    connection.commit()
    connection.close()


class TestCommands:
    """The argv must match the commands the runbook documents."""

    def test_backup_uses_the_documented_dot_command(self, check):
        argv = check.backup_command("/var/lib/ai-monitor/usage.db", "/b/usage.db")
        assert argv == ["sqlite3", "/var/lib/ai-monitor/usage.db", ".backup '/b/usage.db'"]

    def test_restore_uses_the_documented_dot_command(self, check):
        argv = check.restore_command("/var/lib/ai-monitor/usage.db", "/b/usage.db")
        assert argv == ["sqlite3", "/var/lib/ai-monitor/usage.db", ".restore '/b/usage.db'"]

    def test_a_path_with_a_space_stays_one_argument(self, check):
        argv = check.backup_command("/var/lib/my state/usage.db", "/b/a b.db")
        assert len(argv) == 3
        assert argv[2] == ".backup '/b/a b.db'"

    def test_integrity_check_is_the_documented_pragma(self, check):
        assert check.integrity_command("/db") [2] == "pragma integrity_check;"


class TestFingerprint:
    def test_reads_every_row(self, check, tmp_path):
        db = tmp_path / "usage.db"
        _make_db(db, rows=3)
        assert len(check.fingerprint(str(db))) == 3

    def test_missing_file_is_none(self, check, tmp_path):
        assert check.fingerprint(str(tmp_path / "absent.db")) is None

    def test_a_non_database_file_is_none(self, check, tmp_path):
        junk = tmp_path / "usage.db"
        junk.write_text("not a database")
        assert check.fingerprint(str(junk)) is None

    def test_it_never_writes_to_the_database(self, check, tmp_path):
        db = tmp_path / "usage.db"
        _make_db(db, rows=1)
        before = db.stat().st_mtime_ns
        check.fingerprint(str(db))
        assert db.stat().st_mtime_ns == before


class TestCompare:
    def _rows(self, count: int = 1):
        return [(index, "codex") for index in range(count)]

    def test_identical_is_clean(self, check):
        assert check.compare(self._rows(), self._rows()) == []

    def test_nothing_captured_is_a_failure(self, check):
        # A backup taken before any collection captures nothing to lose, so the
        # restore proves nothing.
        assert any("no observation was collected" in f for f in check.compare([], self._rows()))

    def test_unreadable_after_is_a_failure(self, check):
        assert any("could not be opened" in f for f in check.compare(self._rows(), None))

    def test_nothing_before_is_a_failure(self, check):
        assert any("cannot be verified" in f for f in check.compare(None, self._rows()))

    def test_a_different_row_set_is_a_failure(self, check):
        failures = check.compare(self._rows(2), self._rows(3))
        assert any("differs from the one that was backed up" in f for f in failures)

    def test_same_count_different_contents_is_a_failure(self, check):
        """Counts alone would pass this."""
        assert check.compare([(1, "codex")], [(1, "antigravity")]) != []


class TestExampleConfigLoads:
    """The install step copies this file; a bad key would fail at `systemctl start`."""

    def test_it_loads_through_the_real_config(self):
        config = Config(**json.loads(EXAMPLE_CONFIG.read_text("utf-8")))
        assert config.api_host == "127.0.0.1"
        assert config.codex_transport == "stdio"

    def test_no_credential_is_prefilled(self):
        data = json.loads(EXAMPLE_CONFIG.read_text("utf-8"))
        assert data["openrouter_api_key"] == ""
        assert data["openrouter_management_key"] == ""
        assert data["account_digest_key"] == ""

    def test_the_paths_it_names_are_the_documented_layout(self):
        data = json.loads(EXAMPLE_CONFIG.read_text("utf-8"))
        runbook = RUNBOOK.read_text("utf-8")
        assert data["data_dir"] == "/var/lib/ai-monitor"
        assert data["data_dir"] in runbook
        assert "/home/ai-monitor/.codex" in data["codex_app_server_socket"]

    def test_it_mentions_every_serialisable_field(self):
        """A key the loader knows but the example omits would surprise an operator."""
        data = json.loads(EXAMPLE_CONFIG.read_text("utf-8"))
        defaults = Config(data_dir="/tmp/x")
        missing = {
            field
            for field in vars(defaults)
            if not field.startswith("_") and field not in data and field != "data_dir"
        }
        assert missing == set(), f"the example config omits: {sorted(missing)}"


class TestRunbookBackupProcedure:
    def test_the_backup_directory_is_created_before_it_is_written_to(self):
        runbook = RUNBOOK.read_text("utf-8")
        section = runbook.split("## Backup", 1)[1].split("## Rollback", 1)[0]
        create = section.find("install -d")
        write = section.find(".backup")
        assert create != -1, "the procedure never creates the backup directory"
        assert create < write, "the directory must be created before the backup"

    def test_the_procedure_uses_the_commands_the_tool_runs(self, check):
        runbook = RUNBOOK.read_text("utf-8")
        section = runbook.split("## Backup", 1)[1].split("## Rollback", 1)[0]
        assert ".backup '" in section
        assert ".restore '" in section
        assert check.backup_command("db", "out")[2].startswith(".backup '")
        assert check.restore_command("db", "out")[2].startswith(".restore '")

    def test_the_restore_clears_stale_wal_sidecars(self):
        runbook = RUNBOOK.read_text("utf-8")
        section = runbook.split("## Backup", 1)[1].split("## Rollback", 1)[0]
        assert "usage.db-wal" in section, (
            "a leftover -wal can replay over the restored file"
        )

    def test_the_backup_target_is_not_left_loose_in_the_state_directory(self):
        runbook = RUNBOOK.read_text("utf-8")
        section = runbook.split("## Backup", 1)[1].split("## Rollback", 1)[0]
        # The old procedure backed up into the state dir and then *copied* it,
        # leaving a stray database beside the live one.
        assert "/var/lib/ai-monitor/backup-$(date" not in section
