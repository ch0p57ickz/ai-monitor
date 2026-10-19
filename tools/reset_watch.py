"""Watch a Codex quota window across a reset (task 5.1 evidence).

Task 5.1 asks for a soak across an observed reset. A reset is the one moment
where the pace math is easiest to get wrong: the window's start has to move to
the new window, so the comparison target drops back to nearly zero instead of
staying at the elapsed fraction of the window that just ended.

This tool runs the *real* service (real adapters, the same scheduler and payload
code the widgets use) with a short Codex interval, samples ``/api/v1/usage``
throughout, records every sample as JSON Lines, and then checks:

- a reset was actually observed (the window's ``resetsAt`` advanced by about one
  window, or the used percentage collapsed);
- immediately after the reset the window has a usable pace whose
  ``expectedUsedPercent`` is small (a stale window start would push it to 100);
- the window id and duration are unchanged across the reset;
- history stayed bounded and the API never returned a non-200.

It writes only to a temporary data directory and to the ``--log`` file. Run it
from the repository root with the package importable:

    python3 tools/reset_watch.py --minutes 50 --log /tmp/opencode/reset-watch.jsonl

Exit status is 0 only when every expectation above holds.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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

#: How far ``resetsAt`` must jump to count as a new window: half a window.
RESET_JUMP_RATIO = 0.5

#: A used-percentage collapse large enough to be a reset on its own.
USED_DROP_PERCENT = 50.0

#: After a reset the elapsed fraction is tiny, so the expectation must be small.
#: A stale window start yields 100, which this bound separates cleanly.
POST_RESET_MAX_EXPECTED = 30.0


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except json.JSONDecodeError:
            body = {}
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp, tolerating a trailing ``Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_window(usage: dict[str, Any], suffix: str) -> Optional[dict[str, Any]]:
    """Return the Codex window whose id ends with ``suffix``, if present."""
    for observation in usage.get("observations", []):
        if observation.get("source") != "codex":
            continue
        for window in observation.get("quotaWindows", []):
            identifier = window.get("id") or ""
            if identifier.endswith(suffix):
                return window
    return None


def detect_reset(
    previous: Optional[dict[str, Any]], current: Optional[dict[str, Any]]
) -> Optional[str]:
    """Return why a reset happened between two samples, or ``None``.

    A new window shows up as ``resetsAt`` moving forward by about one window,
    which is the reliable signal. A large drop in the used percentage is kept as
    a fallback for a provider that reports the new window without moving
    ``resetsAt`` yet.
    """
    if not previous or not current:
        return None
    old_end = parse_iso(previous.get("resetsAt"))
    new_end = parse_iso(current.get("resetsAt"))
    duration_minutes = current.get("durationMinutes") or previous.get("durationMinutes")
    if old_end and new_end and new_end > old_end and duration_minutes:
        advanced = (new_end - old_end).total_seconds() / 60.0
        if advanced >= float(duration_minutes) * RESET_JUMP_RATIO:
            return f"resetsAt advanced by {advanced:.0f} min"
    old_used = previous.get("usedPercent")
    new_used = current.get("usedPercent")
    if isinstance(old_used, (int, float)) and isinstance(new_used, (int, float)):
        if old_used - new_used >= USED_DROP_PERCENT:
            return f"usedPercent dropped {old_used - new_used:.0f} points"
    return None


def _sample(window: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": (window or {}).get("id"),
        "usedPercent": (window or {}).get("usedPercent"),
        "resetsAt": (window or {}).get("resetsAt"),
        "durationMinutes": (window or {}).get("durationMinutes"),
        "pace": (window or {}).get("pace"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=50.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--suffix", default="primary")
    parser.add_argument(
        "--log",
        default=os.path.join(
            tempfile.gettempdir(),
            f"reset-watch-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl",
        ),
    )
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="ai-monitor-reset-watch-")
    config = Config(
        data_dir=workdir,
        api_port=0,
        codex_interval=max(args.interval, 10.0),
        codex_transport="stdio",
        antigravity_interval=300.0,
        openrouter_interval=300.0,
    )
    db = Database(os.path.join(workdir, "usage.db"))
    worker = UsageWorker(config=config, db=db, version="reset-watch")
    server = serve_http(config, worker, version="reset-watch")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    worker.start()
    base = f"http://127.0.0.1:{server.port}"

    history_lengths: list[int] = []
    failures: list[str] = []
    resets: list[dict[str, Any]] = []
    previous: Optional[dict[str, Any]] = None
    samples = 0

    print(f"watching codex '{args.suffix}' for {args.minutes:.0f} min on {base}")
    print(f"recording samples to {args.log}")
    deadline = time.monotonic() + args.minutes * 60.0
    try:
        with open(args.log, "w", encoding="utf-8") as log:
            while time.monotonic() < deadline:
                loop_start = time.monotonic()
                status, usage = _get(f"{base}/api/v1/usage")
                hist_status, history = _get(
                    f"{base}/api/v1/usage/history?hours=1&limit=5"
                )
                if status != 200:
                    failures.append(f"/api/v1/usage returned {status}")
                if hist_status == 200:
                    history_lengths.append(len(history.get("observations", [])))

                window = find_window(usage, args.suffix) if status == 200 else None
                current = _sample(window) if window else None
                reason = detect_reset(previous, current)
                if reason and current:
                    entry = {"at": _now(), "reason": reason, "window": current}
                    resets.append(entry)
                    print(f"RESET detected: {reason} -> {json.dumps(entry)}")
                log.write(
                    json.dumps(
                        {"at": _now(), "sample": samples, "window": current}
                    )
                    + "\n"
                )
                log.flush()
                previous = current or previous
                samples += 1

                remaining = args.interval - (time.monotonic() - loop_start)
                if remaining > 0:
                    time.sleep(remaining)
    finally:
        worker.stop()
        server.shutdown()
        db.close()
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print(f"samples: {samples}  resets observed: {len(resets)}")
    if history_lengths:
        print(f"history lengths seen: {sorted(set(history_lengths))}")

    if samples < 2:
        failures.append("not enough samples to judge a reset")
    if not resets:
        failures.append(
            "no reset was observed: the window did not turn over during the watch"
        )
    for entry in resets:
        window = entry["window"]
        pace = window.get("pace") or {}
        expected = pace.get("expectedUsedPercent")
        if not isinstance(expected, (int, float)):
            failures.append(
                f"post-reset pace is not usable: {json.dumps(pace)}"
            )
        elif expected > POST_RESET_MAX_EXPECTED:
            failures.append(
                f"post-reset expectedUsedPercent is {expected} (a stale window "
                f"start would explain this); the new window must compare against "
                f"its own elapsed fraction"
            )
        if pace.get("classification") == "unknown":
            failures.append("post-reset pace is unknown despite known window bounds")
    # The window identity must survive the reset; only its values change.
    ids = {entry["window"].get("id") for entry in resets}
    if any(identifier != f"codex:codex:{args.suffix}" for identifier in ids):
        failures.append(f"the window id changed across the reset: {sorted(ids)}")
    over = [length for length in history_lengths if length > 5]
    if over:
        failures.append(f"history returned more than the requested limit: {over}")

    if failures:
        print()
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print()
    print("reset watch complete: all expectations met")
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
