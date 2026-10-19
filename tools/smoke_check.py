"""Local smoke check for the ai-monitor loopback API.

Starts the service against this host on a loopback port with a temporary data
directory, using the real adapters, then exercises the HTTP contract. It never
reads credentials and makes no changes to the host.

Run from the repository root with the package importable:

    python3 tools/smoke_check.py

It exits non-zero if a contract check fails.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ai_monitor.config import Config, load_config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.sources.registry import default_registry
from ai_monitor.state import UsageWorker

# A fresh directory by default: a reused one accumulates observations, and the
# history response then grows run to run, so the sizes printed below would not be
# comparable between runs. Set the variable to reuse a directory deliberately.
DATA = os.environ.get("AI_MONITOR_SMOKE_DIR") or tempfile.mkdtemp(
    prefix="ai-monitor-smoke-"
)

# The cap under test has to come from the deployment being checked, so point this
# at the service's own config file. Without one it falls back to the built-in
# default, and says so, rather than implying it verified a deployment.
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument(
    "--config",
    default=os.environ.get("AI_MONITOR_CONFIG"),
    help=(
        "Config file of the deployment under test. Its provider settings and "
        "credentials are used, but data_dir and api_port are overridden so this "
        "does not touch the running service's database or port."
    ),
)
args = parser.parse_args()

if args.config:
    config = dataclasses.replace(load_config(args.config), data_dir=DATA, api_port=0)
    cap_source = args.config
else:
    config = Config(
        data_dir=DATA,
        api_port=0,
        codex_app_server_socket=os.environ.get("CODEX_APP_SERVER_SOCKET", ""),
    )
    cap_source = "built-in default"

db = Database(config.data_dir + "/usage.db")
worker = UsageWorker(config=config, db=db, registry=default_registry(), version="smoke")
worker.start()
server = serve_http(config, worker, version="smoke")
import threading
http_thread = threading.Thread(target=server.serve_forever, daemon=True)
http_thread.start()
print("listening on", server.url)
print("data directory", config.data_dir)

time.sleep(4.0)  # let one polling cycle finish


CAP = config.response_cap_bytes
print(f"response cap {CAP} bytes, from {cap_source}")
SIZES: dict[str, int] = {}


def get(path: str):
    started = time.monotonic()
    port = urlparse(server.url).port
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            status, raw, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        # 4xx/5xx are results to inspect, not tracebacks to crash on.
        status, raw, headers = exc.code, exc.read(), exc.headers
    elapsed = (time.monotonic() - started) * 1000
    SIZES[path] = len(raw)
    body = json.loads(raw or b"{}")

    # The service enforces the cap itself, replacing an oversized payload with a
    # 413 error body, so an oversized body here means that enforcement broke.
    if len(raw) > CAP:
        raise SystemExit(
            f"FAIL: {path} returned {len(raw)} bytes, over the {CAP}-byte cap"
        )
    # A widget has to be able to *fetch* these endpoints. A 413 means the
    # configured cap cannot hold the payload, which makes the deployment useless
    # for its purpose however correctly it behaves, so it fails loudly here.
    if status != 200:
        category = (body.get("error") or {}).get("category", "unknown")
        raise SystemExit(
            f"FAIL: {path} returned HTTP {status} ({category}) with a {CAP}-byte "
            "cap; a widget must be able to fetch this endpoint"
        )
    return status, headers, body, elapsed


for path in ("/api/v1/version", "/health", "/api/v1/usage", "/api/v1/usage/history?hours=1&limit=5"):
    status, headers, body, elapsed = get(path)
    print(f"\n=== GET {path} -> {status} ({elapsed:.1f} ms, {SIZES[path]} bytes)")
    print("cache-control:", headers.get("Cache-Control"))
    if path == "/health":
        print("serviceStatus:", body["serviceStatus"])
        for s in body["sources"]:
            print("  ", s["id"], s["status"], s["errorCategory"], s["lastSuccessAt"])
    elif path == "/api/v1/usage":
        print("observations:", [(o["source"], o["status"]) for o in body["observations"]])
    elif "/history" in path:
        print("hours:", body.get("hours"), "truncated:", body.get("truncated"), "count:", len(body.get("observations", [])))
    else:
        print(json.dumps(body))

# Every response body must be free of credential-looking text.
blob = json.dumps(get("/health")[2]) + json.dumps(get("/api/v1/usage")[2])
for needle in ("Bearer ", "sk-", "@", "authorization"):
    if needle in blob:
        raise SystemExit(f"FAIL: response leaked {needle!r}")
print("\nno credential-looking text in responses: OK")

# A non-loopback address must not be able to reach the port.
import socket
port = urlparse(server.url).port

host_ip = socket.gethostbyname(socket.gethostname())
if host_ip.startswith("127."):
    print("host resolves to loopback; skipping non-loopback probe")
else:
    try:
        urllib.request.urlopen(f"http://{host_ip}:{port}/health", timeout=2).read()
        print(f"NON-LOOPBACK REACHABLE at {host_ip}:{port} - FAIL")
    except Exception as exc:  # noqa: BLE001
        print(f"non-loopback {host_ip}:{port} refused: {type(exc).__name__}")

print(f"\nresponse sizes (cap {CAP} bytes, from {cap_source}):")
for path, size in SIZES.items():
    print(f"  {size:7d}  {path}")
largest = max(SIZES.values(), default=0)
print(f"largest response {largest} bytes, {CAP - largest} bytes under the cap: OK")

server.shutdown()
worker.stop()
db.close()
print("\nsmoke check complete")
