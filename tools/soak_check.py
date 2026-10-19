"""Bounded local soak for the usage service (task 5.1 evidence).

Runs the real service with the real adapters for a short representative
interval and samples the loopback API throughout, recording what the specs
require: response latency independent of provider polls, freshness and status
transitions, bounded history, first-failure omission, and independent per-source
failures.

Run from the repository root with the package importable:

    python3 tools/soak_check.py [--seconds 90] [--interval 1.0]

It prints a summary and exits non-zero when an expectation fails. It writes only
to a temporary data directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.state import UsageWorker


def _get(url: str) -> tuple[int, dict, float]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.loads(response.read())
            return response.status, body, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except json.JSONDecodeError:
            body = {}
        return exc.code, body, time.perf_counter() - started
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}, time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="ai-monitor-soak-")
    config = Config(
        data_dir=workdir,
        api_port=0,
        codex_interval=10.0,
        codex_transport="stdio",
        antigravity_interval=15.0,
        openrouter_interval=15.0,
        retry_base_delay=5.0,
        retry_max_delay=20.0,
    )
    db = Database(os.path.join(workdir, "usage.db"))
    worker = UsageWorker(config=config, db=db, version="soak")
    server = serve_http(config, worker, version="soak")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker.start()

    base = f"http://127.0.0.1:{server.port}"
    latencies: list[float] = []
    statuses: dict[str, set[str]] = {}
    failures: list[str] = []
    codex_seen_healthy = False
    usage_counts: set[int] = set()
    history_truncated: set[bool] = set()
    history_lengths: list[int] = []
    observed_sources: set[str] = set()
    # A source that has reported its own success at least once; observations may
    # only ever come from these.
    succeeded: set[str] = set()
    samples = 0

    print(f"soaking for {args.seconds:.0f}s on {base} ...")
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            loop_start = time.monotonic()
            health_status, health, t1 = _get(f"{base}/health")
            usage_status, usage, t2 = _get(f"{base}/api/v1/usage")
            hist_status, history, t3 = _get(f"{base}/api/v1/usage/history?hours=1&limit=5")
            latencies.extend([t1, t2, t3])
            samples += 1

            for label, status in (
                ("health", health_status),
                ("usage", usage_status),
                ("history", hist_status),
            ):
                if status != 200:
                    failures.append(f"{label} returned {status}")
            if health_status == 200:
                for source in health.get("sources", []):
                    statuses.setdefault(source["id"], set()).add(source["status"])
                    if source["id"] == "codex" and source["status"] == "healthy":
                        codex_seen_healthy = True
                    if source["status"] == "healthy":
                        succeeded.add(source["id"])
                if health.get("serviceStatus") not in ("healthy", "degraded"):
                    failures.append(f"bad serviceStatus {health.get('serviceStatus')!r}")
            if usage_status == 200:
                observations = usage.get("observations", [])
                usage_counts.add(len(observations))
                for observation in observations:
                    observed_sources.add(observation["source"])
            if hist_status == 200:
                history_truncated.add(history.get("truncated"))
                history_lengths.append(len(history.get("observations", [])))
                if "hours" not in history:
                    failures.append("history response has no hours field")

            remaining = args.interval - (time.monotonic() - loop_start)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        worker.stop()
        server.shutdown()
        db.close()
        shutil.rmtree(workdir, ignore_errors=True)

    if not latencies:
        print("FAIL: no samples collected")
        return 1

    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print()
    print(f"samples: {samples}  requests: {len(latencies)}")
    print(
        "latency ms: "
        f"median {statistics.median(ordered) * 1000:.1f}  "
        f"p95 {p95 * 1000:.1f}  "
        f"max {ordered[-1] * 1000:.1f}"
    )
    for source_id in sorted(statuses):
        print(f"  {source_id}: {', '.join(sorted(statuses[source_id]))}")
    printed_source = ", ".join(sorted(observed_sources)) or "none"
    print(f"usage observation counts seen: {sorted(usage_counts)}")
    print(f"history truncated values seen: {sorted(history_truncated)}")
    print(f"history lengths seen: {sorted(set(history_lengths))}")
    print(f"sources ever observed: {printed_source}")

    # Expectations from the specs.
    if not codex_seen_healthy:
        failures.append("codex never reached healthy")
    if "codex" not in observed_sources:
        failures.append("codex never appeared in the usage payload")
    if ordered[-1] > 0.5:
        failures.append(
            f"a response took {ordered[-1] * 1000:.0f} ms"
            " (polls must not block the API)"
        )
    # First-failure omission: a source that only ever failed must never appear in
    # the usage payload. Accumulated across every sample, so a source that fails
    # early and succeeds later is not counted as failed-only.
    failed_only = {
        source_id
        for source_id, seen in statuses.items()
        if seen - {"healthy"} and "healthy" not in seen
    }
    leaked = observed_sources & failed_only
    if leaked:
        failures.append(
            f"never-successful sources leaked into usage: {sorted(leaked)}"
        )
    # Independence: a source failing must not stop another from being collected.
    if failed_only and not codex_seen_healthy:
        failures.append("codex was not collected while other sources failed")
    # Bounded history: the server must never return more rows than requested.
    over = [length for length in history_lengths if length > 5]
    if over:
        failures.append(f"history returned more than the requested limit: {over}")
    if any(not isinstance(value, bool) for value in history_truncated):
        failures.append("history `truncated` is not a boolean")

    if failures:
        print()
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print()
    print("soak complete: all expectations met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
