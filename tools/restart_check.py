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

The state file defaults to ``/tmp/ai-monitor-restart-state.json``. It holds the unit,
the URL, a PID, the capture time, and timestamps — no credentials.

Exit status is 0 when the check holds, 1 when it fails, and 2 when it cannot run, so a
skip never looks like a pass. "Cannot run" covers an unreachable API or unit, a unit
that is not running (systemd reports ``MainPID=0`` for a name that does not match, so
the name is verified rather than assumed), a state file that is missing, malformed, or
captured from another unit or URL, and a capture too old for the API's row limit to
still cover it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["capture", "main", "main_pid", "served_timestamps", "verify"]

DEFAULT_URL = "http://127.0.0.1:8732"
DEFAULT_UNIT = "ai-monitor"
DEFAULT_STATE = Path("/tmp/ai-monitor-restart-state.json")

#: The API accepts 24 or 168 hours for `hours`. 24 is enough: a capture is taken
#: seconds before the restart, so its rows are the newest ones.
HISTORY_QUERY = "hours=24&limit=500"

#: Each response carries at most 500 rows. Three sources polling once a minute reach
#: that in about 2.8 hours, and past it a captured timestamp can fall outside the
#: query while still being in the database — a false failure. Refuse instead, and
#: say why, so a loss is never confused with a window that moved.
MAX_CAPTURE_AGE_SECONDS = 2 * 60 * 60


def _get_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp from the API."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def served_timestamps(payload: dict[str, Any]) -> list[str]:
    """The ``collectedAt`` of every observation in a usage or history payload."""
    rows = payload.get("observations") or []
    return sorted(
        row["collectedAt"] for row in rows if isinstance(row, dict) and row.get("collectedAt")
    )


def main_pid(
    unit: str, run: Optional[Callable[..., subprocess.CompletedProcess]] = None
) -> str:
    """The unit's main PID, as systemd reports it.

    ``systemctl show`` exits 0 and prints ``MainPID=0`` when no unit matches, so a
    zero is not a PID: it means the name is wrong, and treating it as a PID would let
    an unrelated process count as the restart.
    """
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
    pid = completed.stdout.strip().partition("=")[2].strip()
    if pid in ("", "0"):
        raise RuntimeError(
            f"unit {unit!r} is not running (systemd reports MainPID={pid or 'nothing'}); "
            f"check the unit name"
        )
    return pid


def _validate_state(state: Any, path: Path | None = None) -> dict[str, Any]:
    """Reject a state file that cannot prove anything, rather than passing on it."""
    where = f"{path}: " if path else ""
    if not isinstance(state, dict):
        raise ValueError(
            f"{where}expected a state file object, found {type(state).__name__}"
        )
    pid = state.get("pid")
    if not isinstance(pid, str) or pid in ("", "0"):
        raise ValueError(f"{where}no usable pid; capture again with --capture")
    timestamps = state.get("timestamps")
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError(f"{where}no timestamps; capture again with --capture")
    for key in ("unit", "url"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise ValueError(f"{where}missing {key}; capture again with --capture")
    if not isinstance(state.get("capturedAt"), str) or not state["capturedAt"]:
        raise ValueError(f"{where}missing capturedAt; capture again with --capture")
    return state


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
    timestamps = served_timestamps(payload)
    if not timestamps:
        raise RuntimeError(
            "the API served no observations, so there is nothing to verify: wait for "
            "a successful collection and capture again"
        )
    generated = payload.get("generatedAt")
    try:
        captured_at = _parse_timestamp(generated).isoformat() if generated else None
    except ValueError:
        captured_at = None
    return {
        "unit": unit,
        "url": url,
        "pid": main_pid(unit, run),
        "capturedAt": captured_at or _utc_now().isoformat(),
        "timestamps": timestamps,
    }


def verify(
    state: dict[str, Any],
    unit: str,
    url: str,
    *,
    fetch: Optional[Callable[[str], dict[str, Any]]] = None,
    run: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> tuple[bool, str]:
    """Check the captured state against the running service.

    Raises when the check cannot be made, which the CLI reports as exit 2: a state
    file that cannot prove anything, one captured from another unit or URL, or one
    captured too long ago for the API's row limit to cover it.
    """
    fetch = fetch or _get_json
    state = _validate_state(state)
    if state["unit"] != unit or state["url"] != url:
        raise RuntimeError(
            f"the state file was captured from {state['unit']!r} at {state['url']}; "
            f"verify with the same --unit and --url, or capture again"
        )

    age = (_utc_now() - _parse_timestamp(state["capturedAt"])).total_seconds()
    if age > MAX_CAPTURE_AGE_SECONDS:
        raise RuntimeError(
            f"the capture is {age / 60:.0f} minutes old; each history response carries "
            f"at most 500 rows, so this can no longer tell a loss from a window that "
            f"moved. Restart and capture again."
        )

    before_pid = state["pid"]
    now_pid = main_pid(unit, run)
    payload = fetch(f"{url}/api/v1/usage/history?{HISTORY_QUERY}")
    served = set(served_timestamps(payload))
    wanted = {str(value) for value in state["timestamps"]}
    missing = sorted(wanted - served)

    lines = [
        f"pid: {before_pid} -> {now_pid}",
        f"timestamps: {len(wanted)} captured, {len(wanted) - len(missing)} still served "
        f"(captured {age:.0f} s ago)",
    ]
    if missing:
        lines.append(
            "note: a history response carries at most 500 rows, so a missing timestamp "
            "can mean it fell outside the query as well as out of the database"
        )
    if payload.get("truncated"):
        lines.append(f"note: the response was truncated to {len(served)} rows")
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
            args.state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            print(
                f"captured pid {state['pid']} and {len(state['timestamps'])} "
                f"timestamp(s) to {args.state}"
            )
            print(f"now restart the unit, then run: {sys.argv[0]} --verify")
            return 0

        raw = json.loads(args.state.read_text(encoding="utf-8"))
        state = _validate_state(raw, args.state)
        ok, report = verify(state, args.unit, args.url)
        print(report)
        return 0 if ok else 1
    except (
        urllib.error.URLError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RuntimeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        print(f"cannot run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
