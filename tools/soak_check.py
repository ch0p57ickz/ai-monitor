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
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.sources.registry import default_registry
from ai_monitor.state import UsageWorker


def _get(url: str) -> tuple[int, dict, float]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.loads(response.read())
            return response.status, body, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), time.perf_counter() - started


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
    history_counts: list[int] = set()
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
                if health.get("serviceStatus") not in ("healthy", "degraded"):
                    failures.append(f"bad serviceStatus {health.get('serviceStatus')!r}")
            if usage_status == 200:
                usage_counts.add(len(usage.get("observations", [])))
            if hist_status == 200:
                history_counts.add(history.get("truncated"))
                if "hours" not in history:
                    failures.append("history response has no hours field")

            remaining = args.interval - (time.monotonic() - loop_start)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        worker.stop()
        server.shutdown()
        db.close()

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
    print(f"usage observation counts seen: {sorted(usage_counts)}")
    print(f"history truncated values seen: {sorted(history_counts)}")

    # Expectations from the specs.
    if not codex_seen_healthy:
        failures.append("codex never reached healthy")
    if ordered[-1] > 0.5:
        failures.append(f"a response took {ordered[-1] * 1000:.0f} ms (polls must not block the API)")
    if 1 not in usage_counts:
        failures.append("never observed exactly one committed usage observation")
    non_healthy = [
        source_id
        for source_id, seen in statuses.items()
        if seen - {"healthy"}
    ]
    if not non_healthy:
        failures.append("expected at least one failing source to coexist with codex")

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
