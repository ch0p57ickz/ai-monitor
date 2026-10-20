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

The state file defaults to ``/tmp/ai-monitor-restart-state.json``. It holds the host,
unit, and URL, a PID, the capture time, and timestamps — no credentials. It is written
mode 0600, without following symlinks, and read back only when the caller owns it and
nobody else can write it: a verdict read from a file anyone can edit is not a verdict.

Exit status is 0 when the check holds, 1 when it fails, and 2 when it cannot run, so a
skip never looks like a pass. "Cannot run" covers an unreachable API or unit; a unit
that is not running (systemd exits 0 with ``MainPID=0`` for a name that matches
nothing, so the name is verified rather than assumed); a state file that is missing,
malformed, unreadable, not owned by the caller, or captured on another host, unit, or
URL; a capture older than the promptness window; and a truncated history response in
which a captured row is absent, because there the window and the database are
indistinguishable.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "capture",
    "main",
    "main_pid",
    "served_timestamps",
    "verify",
]

DEFAULT_URL = "http://127.0.0.1:8732"
DEFAULT_UNIT = "ai-monitor"
DEFAULT_STATE = Path("/tmp/ai-monitor-restart-state.json")

#: The API accepts 24 or 168 hours for `hours`. 24 is enough: a capture is taken
#: seconds before the restart, so its rows are the newest ones.
HISTORY_QUERY = "hours=24&limit=500"

#: How long a capture stays usable. This is a promptness guard, not the real
#: protection: the response byte cap can drop rows well before the 500-row limit
#: does, and the truncated-with-a-missing-row case is refused outright rather than
#: read as a loss. Keep this short enough that a forgotten capture is re-taken
#: instead of believed.
MAX_CAPTURE_AGE_SECONDS = 30 * 60

#: Clock steps happen. A little future skew is tolerated; a lot means the capture
#: time cannot be trusted, and a future timestamp would otherwise disable the age
#: guard entirely, since a negative age is never "too old".
MAX_CLOCK_SKEW_SECONDS = 60


def _get_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a missing zone as UTC."""
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
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError(
            f"unit {unit!r} is not running (systemd reports MainPID={pid or 'nothing'}); "
            f"check the unit name"
        )
    return pid


def _validate_state(state: Any, path: Path | None = None) -> dict[str, Any]:
    """Reject a state file that cannot prove anything, rather than passing on it.

    A non-numeric PID matters as much as a zero one: the check compares PID *strings*,
    so any value that cannot equal a live PID would pass the restart half by
    construction.
    """
    where = f"{path}: " if path else ""
    if not isinstance(state, dict):
        raise ValueError(
            f"{where}expected a state file object, found {type(state).__name__}"
        )
    pid = state.get("pid")
    if not isinstance(pid, str) or not pid.strip().isdigit() or int(pid) <= 0:
        raise ValueError(f"{where}no usable pid; capture again with --capture")
    timestamps = state.get("timestamps")
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError(f"{where}no timestamps; capture again with --capture")
    if not all(isinstance(value, str) and value.strip() for value in timestamps):
        raise ValueError(
            f"{where}timestamps must be non-empty strings; capture again with --capture"
        )
    for key in ("host", "unit", "url", "capturedAt"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise ValueError(f"{where}missing {key}; capture again with --capture")
    return state


def _write_state_file(path: Path, state: dict[str, Any]) -> None:
    """Write the state file mode 0600, refusing to follow a symlink."""
    data = (json.dumps(state, indent=2) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        # A file created by someone else stays theirs; changing its mode fails, which
        # is the point — a state file another user can rewrite proves nothing.
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _read_state_file(path: Path) -> str:
    """Read the state file, refusing a symlink or a file someone else can write."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise RuntimeError(
                f"{path} is owned by uid {info.st_uid}, not by you (uid {os.geteuid()}); "
                f"a state file another user can edit proves nothing. Capture again as "
                f"the user that will verify."
            )
        if info.st_mode & 0o022:
            raise RuntimeError(
                f"{path} is writable by group or others; capture again"
            )
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")


def capture(
    unit: str,
    url: str,
    *,
    fetch: Optional[Callable[[str], dict[str, Any]]] = None,
    run: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> dict[str, Any]:
    """Record what is served now, and which process serves it."""
    fetch = fetch or _get_json
    url = url.rstrip("/")
    payload = fetch(f"{url}/api/v1/usage")
    timestamps = served_timestamps(payload)
    if not timestamps:
        raise RuntimeError(
            "the API served no observations, so there is nothing to verify: wait for "
            "a successful collection and capture again"
        )
    return {
        "host": socket.gethostname(),
        "unit": unit,
        "url": url,
        "pid": main_pid(unit, run),
        "capturedAt": _utc_now().isoformat(),
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
    file that cannot prove anything, one captured elsewhere or too long ago, or a
    response too truncated to tell a moved window from lost rows.
    """
    fetch = fetch or _get_json
    url = url.rstrip("/")
    state = _validate_state(state)
    if (
        state["host"] != socket.gethostname()
        or state["unit"] != unit
        or state["url"] != url
    ):
        raise RuntimeError(
            f"the state file was captured on {state['host']!r} from {state['unit']!r} "
            f"at {state['url']}; verify with the same --unit and --url on the same "
            f"host, or capture again"
        )

    age = (_utc_now() - _parse_timestamp(state["capturedAt"])).total_seconds()
    if age < -MAX_CLOCK_SKEW_SECONDS:
        raise RuntimeError(
            f"the capture is dated {abs(age):.0f} s in the future, so its age cannot be "
            f"trusted; capture and verify again"
        )
    if age > MAX_CAPTURE_AGE_SECONDS:
        raise RuntimeError(
            f"the capture is {age / 60:.0f} minutes old; a history response carries at "
            f"most 500 rows and is further capped by response size, so this can no "
            f"longer tell a loss from a window that moved. Restart and capture again."
        )

    before_pid = state["pid"]
    now_pid = main_pid(unit, run)
    payload = fetch(f"{url}/api/v1/usage/history?{HISTORY_QUERY}")
    rows = payload.get("observations") or []
    served = set(served_timestamps(payload))
    wanted = {str(value) for value in state["timestamps"]}
    missing = sorted(wanted - served)
    truncated = bool(payload.get("truncated"))

    if missing and truncated:
        raise RuntimeError(
            f"the history response was truncated to {len(rows)} rows, so the "
            f"{len(missing)} captured timestamp(s) that are absent cannot be told "
            f"apart from rows that fell outside it. Restart again and verify within "
            f"the capture window."
        )

    lines = [
        f"pid: {before_pid} -> {now_pid}",
        f"timestamps: {len(wanted)} captured, {len(wanted) - len(missing)} still served "
        f"({len(rows)} rows returned, captured {age:.0f} s ago)",
    ]
    if missing:
        lines.append(
            "note: the response was not truncated, so the absent rows are not a window "
            "artefact"
        )
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
            _write_state_file(args.state, state)
            print(
                f"captured pid {state['pid']} and {len(state['timestamps'])} "
                f"timestamp(s) to {args.state}"
            )
            print(f"now restart the unit, then run: {sys.argv[0]} --verify")
            return 0

        raw = json.loads(_read_state_file(args.state))
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
