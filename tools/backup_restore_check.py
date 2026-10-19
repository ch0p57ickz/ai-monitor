"""Verify the runbook's backup and restore procedure against a live service.

Task 5.2 asks for a restart that preserves cached data, and task 5.4 asks for the
documented commands to be verified. Both are otherwise only checkable on a
deployed host, under `sudo`, with real data — the worst place to discover that a
command does not work. This tool runs the same procedure in a temporary
directory, against the *real* service and the *real* `sqlite3` CLI:

1. start the service, wait for a successful collection;
2. back up with the documented `.backup` command **while it is running**, which
   is the case the runbook claims is safe;
3. stop it and delete the database and its WAL sidecars, standing in for the
   data loss a restore exists for;
4. restore with the documented `.restore` command;
5. compare the restored database with what was captured, then start a worker on
   it and confirm the API serves the restored observation.

The comparison in step 5 is the point: a restore that silently produced an empty
database would still leave the API answering 200.

It writes only to a temporary directory, needs no privileges, and prints no
credentials. Run it from the repository root with the package importable:

    python3 tools/backup_restore_check.py

Exit status is 0 only when every step above holds. One step cannot be reproduced
locally: `journalctl` only knows about units installed by systemd, so the
"no secret in the journal" check stays a deployed-host check.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.state import UsageWorker

#: How long to wait for a first successful collection.
COLLECT_TIMEOUT_SECONDS = 90.0

#: How many stored observations are enough to prove a restore carried data.
MIN_FINGERPRINT_ROWS = 1


def sqlite_argv(db_path: str, dot_command: str) -> list[str]:
    """The runbook's `sqlite3 <db> "<dot command>"` invocation, as argv.

    Built as argv rather than a shell string so a path with a space cannot turn
    into a second argument.
    """
    return ["sqlite3", db_path, dot_command]


def backup_command(db_path: str, backup_path: str) -> list[str]:
    """The documented online backup."""
    return sqlite_argv(db_path, f".backup '{backup_path}'")


def restore_command(db_path: str, backup_path: str) -> list[str]:
    """The documented restore."""
    return sqlite_argv(db_path, f".restore '{backup_path}'")


def integrity_command(db_path: str) -> list[str]:
    """The documented post-restore sanity check."""
    return sqlite_argv(db_path, "pragma integrity_check;")


def fingerprint(db_path: str) -> Optional[list[tuple[Any, ...]]]:
    """Comparable summary of every stored observation, or ``None`` when unreadable.

    Compared before and after the restore. Counts alone would miss a restore that
    recovered the right number of rows with the wrong contents.

    Reads through the standard library's ``sqlite3`` rather than the service's
    own ``Database`` class, so the check does not lean on the code it is checking.
    """
    if not os.path.exists(db_path):
        return None
    try:
        # Read-only, so a check can never alter what it is checking.
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            """
            SELECT source, provider, collected_at, observed_at, account_id,
                   status, quota_windows_json, budgets_json, notes_json
            FROM observations
            ORDER BY id
            """
        ).fetchall()
        return [tuple(row) for row in rows]
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def compare(before: Optional[list[tuple[Any, ...]]], after: Optional[list[tuple[Any, ...]]]) -> list[str]:
    """Failures describing how the restore differed from the capture."""
    failures: list[str] = []
    if before is None:
        failures.append("nothing was captured before the restore, so it cannot be verified")
        return failures
    if len(before) < MIN_FINGERPRINT_ROWS:
        failures.append("no observation was collected, so the backup captured nothing to lose")
    if after is None:
        failures.append("the restored database could not be opened")
        return failures
    if after != before:
        failures.append(
            f"the restored database differs from the one that was backed up "
            f"({len(before)} rows before, {len(after)} after)"
        )
    return failures


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except urllib.error.URLError:
        return 0, {}


def _start(config: Config, db: Database) -> tuple[UsageWorker, Any]:
    worker = UsageWorker(config=config, db=db, version="backup-restore-check")
    server = serve_http(config, worker, version="backup-restore-check")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    worker.start()
    return worker, server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=COLLECT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    if shutil.which("sqlite3") is None:
        print("SKIP: the sqlite3 CLI is not installed, so the documented commands "
              "cannot be run")
        return 2

    workdir = tempfile.mkdtemp(prefix="ai-monitor-backup-check-")
    state = os.path.join(workdir, "var", "lib", "ai-monitor")
    backups = os.path.join(state, "backups")
    os.makedirs(backups, mode=0o700)
    db_path = os.path.join(state, "usage.db")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_path = os.path.join(backups, f"usage-{stamp}.db")

    # The deployed layout uses a 1-hour cadence for the restart phase so a new
    # poll cannot race the check; the first phase needs a short one.
    config = Config(
        data_dir=state,
        api_port=0,
        codex_transport="stdio",
        codex_interval=15.0,
        antigravity_interval=600.0,
        openrouter_interval=600.0,
    )
    db = Database(db_path)
    worker, server = _start(config, db)
    base = f"http://127.0.0.1:{server.port}"

    failures: list[str] = []
    try:
        print(f"state directory: {state}")
        deadline = time.monotonic() + args.timeout
        observations = 0
        while time.monotonic() < deadline:
            status, usage = _get(f"{base}/api/v1/usage")
            if status == 200:
                observations = len(usage.get("observations", []))
                if observations:
                    break
            time.sleep(2.0)
        print(f"observations collected: {observations}")
        if not observations:
            failures.append(
                f"no observation within {args.timeout:.0f}s; nothing to back up"
            )

        before = fingerprint(db_path)
        print(f"rows captured: {len(before) if before else 0}")

        # --- the runbook's backup, while the service is running ---
        result = subprocess.run(backup_command(db_path, backup_path), capture_output=True, text=True)
        if result.returncode != 0:
            failures.append(f"the documented backup failed: {result.stderr.strip()}")
        elif not os.path.exists(backup_path):
            failures.append("the documented backup reported success but wrote no file")
        else:
            os.chmod(backup_path, 0o600)
            print(f"online backup written while running: {os.path.basename(backup_path)}")
    finally:
        worker.stop()
        server.shutdown()
        db.close()

    # --- data loss, exactly what a restore is for ---
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
    print(f"database removed (backup kept): {not os.path.exists(db_path)}")

    # --- the runbook's restore ---
    result = subprocess.run(restore_command(db_path, backup_path), capture_output=True, text=True)
    if result.returncode != 0:
        failures.append(f"the documented restore failed: {result.stderr.strip()}")

    integrity = subprocess.run(integrity_command(db_path), capture_output=True, text=True)
    if integrity.returncode != 0 or "ok" not in integrity.stdout:
        failures.append(f"integrity_check did not report ok: {integrity.stdout.strip()!r}")

    failures.extend(compare(before, fingerprint(db_path)))

    # --- a restart on the restored database serves the data (task 5.2) ---
    served = 0
    restart_config = Config(
        data_dir=state,
        api_port=0,
        codex_transport="stdio",
        codex_interval=3600.0,
        antigravity_interval=3600.0,
        openrouter_interval=3600.0,
    )
    db2 = Database(db_path)
    worker2, server2 = _start(restart_config, db2)
    try:
        time.sleep(1.0)
        status, usage = _get(f"http://127.0.0.1:{server2.port}/api/v1/usage")
        if status != 200:
            failures.append(f"/api/v1/usage returned {status} after the restart")
        else:
            served = len(usage.get("observations", []))
    finally:
        worker2.stop()
        server2.shutdown()
        db2.close()

    print(f"observations served after restore and restart: {served}")
    if served == 0:
        failures.append(
            "the restarted service served no cached observation: the restore did "
            "not survive a restart"
        )

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        shutil.rmtree(workdir, ignore_errors=True)
        return 1
    print("backup and restore check complete: all expectations met")
    print(
        "not covered here: the journal redaction check (`journalctl`), which needs "
        "the unit installed under systemd"
    )
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
