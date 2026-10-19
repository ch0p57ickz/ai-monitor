"""Local smoke check for the ai-monitor loopback API.

Starts the service on a loopback port against a temporary data directory, using
the real adapters, then exercises the HTTP contract. It exits non-zero if a
contract check fails.

Run from the repository root with the package importable:

    PYTHONPATH=. python3 tools/smoke_check.py

With no arguments it uses the built-in defaults and an empty database. Pass
``--config`` to check a deployment's own limits and provider settings: that
deployment's adapters, provider binaries, and credentials are used, against a
temporary database, so the running service is neither stopped nor reconfigured.
With ``codex_transport = "socket"`` it does attach to the same app-server socket
the service uses. Reading a deployed config such as
``/etc/ai-monitor/config.json`` (mode ``0640``) needs root.

Only ``data_dir`` and ``api_port`` are overridden. Nothing else on the host is
changed, and the temporary directory is removed on exit unless
``AI_MONITOR_SMOKE_DIR`` names it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ai_monitor.config import Config, load_config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.sources.registry import default_registry
from ai_monitor.state import UsageWorker

#: Endpoints a widget needs. Each must answer 200 within the configured cap.
CONTRACT_PATHS = (
    "/api/v1/version",
    "/health",
    "/api/v1/usage",
    "/api/v1/usage/history?hours=1&limit=5",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=os.environ.get("AI_MONITOR_CONFIG"),
        help=(
            "Config file of the deployment under test. Its provider settings, "
            "binaries, and credentials are used, but data_dir and api_port are "
            "overridden so the running service and its database are untouched."
        ),
    )
    args = parser.parse_args(argv)

    # A fresh directory by default: a reused one accumulates observations, and the
    # history response then grows run to run, so the sizes printed below would not
    # be comparable between runs. Naming one in the environment keeps it, so a run
    # can be inspected afterwards.
    reuse = os.environ.get("AI_MONITOR_SMOKE_DIR")
    data_dir = reuse or tempfile.mkdtemp(prefix="ai-monitor-smoke-")

    if args.config:
        config = dataclasses.replace(load_config(args.config), data_dir=data_dir, api_port=0)
        cap_source = args.config
    else:
        config = Config(
            data_dir=data_dir,
            api_port=0,
            codex_app_server_socket=os.environ.get("CODEX_APP_SERVER_SOCKET", ""),
        )
        cap_source = "built-in default"

    # No proxies: a proxy that answers for a loopback URL would make every check
    # below a measurement of the proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    cap = config.response_cap_bytes
    sizes: dict[str, int] = {}

    def get(path: str):
        started = time.monotonic()
        port = urlparse(server.url).port
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with opener.open(url, timeout=5) as resp:
                status, raw, headers = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            # 4xx/5xx are results to inspect, not tracebacks to crash on.
            status, raw, headers = exc.code, exc.read(), exc.headers
        elapsed = (time.monotonic() - started) * 1000
        sizes[path] = len(raw)

        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            # The service always sends JSON. Anything else answered instead, so
            # report the status and content type rather than echoing the body.
            raise SystemExit(
                f"FAIL: {path} returned HTTP {status} with a non-JSON body "
                f"({headers.get('Content-Type')!r})"
            ) from None

        # The service enforces the cap itself, replacing an oversized payload
        # with a 413 error body, so an oversized body here means that enforcement
        # broke.
        if len(raw) > cap:
            raise SystemExit(
                f"FAIL: {path} returned {len(raw)} bytes, over the {cap}-byte cap"
            )
        # A widget has to be able to *fetch* these endpoints. A 413 means the
        # configured cap cannot hold the payload, which makes the deployment
        # useless for its purpose however correctly it behaves, so it fails loudly.
        if status != 200:
            category = (body.get("error") or {}).get("category", "unknown")
            raise SystemExit(
                f"FAIL: {path} returned HTTP {status} ({category}) with a {cap}-byte "
                "cap; a widget must be able to fetch this endpoint"
            )
        return status, headers, body, elapsed

    db = Database(config.data_dir + "/usage.db")
    worker = UsageWorker(config=config, db=db, registry=default_registry(), version="smoke")
    worker.start()
    server = serve_http(config, worker, version="smoke")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("listening on", server.url)
    print("data directory", config.data_dir)
    print(f"response cap {cap} bytes, from {cap_source}")

    try:
        time.sleep(4.0)  # let one polling cycle finish

        for path in CONTRACT_PATHS:
            status, headers, body, elapsed = get(path)
            print(f"\n=== GET {path} -> {status} ({elapsed:.1f} ms, {sizes[path]} bytes)")
            print("cache-control:", headers.get("Cache-Control"))
            if path == "/health":
                print("serviceStatus:", body["serviceStatus"])
                for s in body["sources"]:
                    print("  ", s["id"], s["status"], s["errorCategory"], s["lastSuccessAt"])
            elif path == "/api/v1/usage":
                print("observations:", [(o["source"], o["status"]) for o in body["observations"]])
            elif "/history" in path:
                print(
                    "hours:", body.get("hours"),
                    "truncated:", body.get("truncated"),
                    "count:", len(body.get("observations", [])),
                )
            else:
                print(json.dumps(body))

        # Every response body must be free of credential-looking text.
        blob = json.dumps(get("/health")[2]) + json.dumps(get("/api/v1/usage")[2])
        for needle in ("Bearer ", "sk-", "@", "authorization"):
            if needle in blob:
                raise SystemExit(f"FAIL: response leaked {needle!r}")
        print("\nno credential-looking text in responses: OK")

        # The service must bind loopback only, so the host's own address must not
        # reach the port.
        port = urlparse(server.url).port
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip.startswith("127."):
            print("host resolves to loopback; skipping non-loopback probe")
        else:
            try:
                opener.open(f"http://{host_ip}:{port}/health", timeout=2).read()
            except Exception as exc:  # noqa: BLE001
                print(f"non-loopback {host_ip}:{port} refused: {type(exc).__name__}")
            else:
                raise SystemExit(
                    f"FAIL: non-loopback {host_ip}:{port} is reachable; the service "
                    "must bind 127.0.0.1 only"
                )

        print(f"\nresponse sizes (cap {cap} bytes, from {cap_source}):")
        for path, size in sizes.items():
            print(f"  {size:7d}  {path}")
        largest = max(sizes.values(), default=0)
        print(f"largest response {largest} bytes, {cap - largest} bytes under the cap: OK")
    finally:
        server.shutdown()
        worker.stop()
        db.close()
        if not reuse:
            shutil.rmtree(config.data_dir, ignore_errors=True)

    print("\nsmoke check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
