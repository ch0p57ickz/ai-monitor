"""Verify that a restart serves the rows collected before it.

Task 5.2 asks for a restart that preserves cached data. No other check covers it on
the host: ``tools/backup_restore_check.py`` rehearses the backup and restore in a
temporary directory, but the restart of the *installed* unit happens under `sudo`,
with real data, and can only be verified where it happens.

The check compares two things, because either one alone passes wrongly:

- **the unit's main PID, which must change.** Without this the check reports success
  before the restart has happened at all.
- **every observation timestamp served before the restart**, which must still be
  served after it. Without this, a fresh poll satisfies the check:
  ``UsageWorker.start()`` polls immediately, so "is there data?" is true even for a
  service whose database retained nothing.

Run it before the restart, then again after:

    python3 tools/restart_check.py --capture
    sudo systemctl restart ai-monitor
    python3 tools/restart_check.py --verify

The state file defaults to ``/tmp/ai-monitor-restart-state.json``. It holds a PID
and timestamps, no credentials.

Exit status is 0 when the check holds, 1 when it fails, and 2 when it cannot run at
all (the API or the unit is unreachable), so a skip never looks like a pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["capture", "main", "main_pid", "served_timestamps", "verify"]

DEFAULT_URL = "http://127.0.0.1:8732"
DEFAULT_UNIT = "ai-monitor"
DEFAULT_STATE = Path("/tmp/ai-monitor-restart-state.json")

#: The API accepts 24 or 168 hours. 24 is enough: the capture is taken seconds
#: before the restart, so its rows are the newest ones.
HISTORY_QUERY = "hours=24&limit=500"


def _get_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def served_timestamps(payload: dict[str, Any]) -> list[str]:
    """The ``collectedAt`` of every observation in a usage or history payload."""
    rows = payload.get("observations") or []
    return sorted(
        row["collectedAt"] for row in rows if isinstance(row, dict) and row.get("collectedAt")
    )


def main_pid(
    unit: str, run: Optional[Callable[..., subprocess.CompletedProcess]] = None
) -> str:
    """The unit's main PID, as systemd reports it."""
    run = run or subprocess.run
    completed = run(
        ["systemctl", "show", unit, "-p", "MainPID"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"systemctl show {unit} failed: {completed.stderr.strip() or 'no output'}"
        )
    return completed.stdout.strip().split("=")[1]


def capture(
    unit: str,
    url: str,
    *,
    fetch: Optional[Callable[[str], dict[str, Any]]] = None,
    run: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> dict[str, Any]:
    """Record what is served now, and which process serves it."""
    fetch = fetch or _get_json
    payload = fetch(f"{url}/api/v1/usage")
    state = {
        "unit": unit,
        "pid": main_pid(unit, run),
        "timestamps": served_timestamps(payload),
    }
    if not state["timestamps"]:
        raise RuntimeError(
            "the API served no observations, so there is nothing to verify: wait for "
            "a successful collection and capture again"
        )
    return state


def verify(
    state: dict[str, Any],
    unit: str,
    url: str,
    *,
    fetch: Optional[Callable[[str], dict[str, Any]]] = None,
    run: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> tuple[bool, str]:
    """Check the captured state against the running service."""
    fetch = fetch or _get_json
    before_pid = str(state.get("pid", ""))
    now_pid = main_pid(unit, run)
    payload = fetch(f"{url}/api/v1/usage/history?{HISTORY_QUERY}")
    served = set(served_timestamps(payload))
    wanted = {str(value) for value in state.get("timestamps") or []}
    missing = sorted(wanted - served)

    lines = [
        f"pid: {before_pid} -> {now_pid}",
        f"timestamps: {len(wanted)} captured, {len(wanted) - len(missing)} still served",
    ]
    if payload.get("truncated"):
        lines.append(
            f"note: the history response was truncated to {len(served)} rows, so a "
            f"missing timestamp may be outside the 24h window rather than lost"
        )
    if missing:
        lines.append("missing: " + ", ".join(missing))

    problems = []
    if before_pid == now_pid:
        problems.append("the unit's main PID did not change")
    if missing:
        problems.append(f"{len(missing)} captured timestamp(s) are no longer served")
    lines.append(
        "ok: a new process serves the observations collected before it started"
        if not problems
        else "FAIL: " + "; ".join(problems)
    )
    return not problems, "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="record the current state, before the restart",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check the state file, after the restart",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    args = parser.parse_args(argv)

    if args.capture == args.verify:
        parser.error("pass exactly one of --capture or --verify")

    try:
        if args.capture:
            state = capture(args.unit, args.url)
            args.state.write_text(
                json.dumps(state, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"captured pid {state['pid']} and {len(state['timestamps'])} "
                f"timestamp(s) to {args.state}"
            )
            print(f"now restart the unit, then run: {sys.argv[0]} --verify")
            return 0

        state = json.loads(args.state.read_text(encoding="utf-8"))
        ok, report = verify(state, args.unit, args.url)
        print(report)
        return 0 if ok else 1
    except (
        urllib.error.URLError,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        RuntimeError,
        json.JSONDecodeError,
    ) as error:
        print(f"cannot run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
