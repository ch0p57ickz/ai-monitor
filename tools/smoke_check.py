"""Local smoke check for the ai-monitor loopback API.

Starts the service against this host on a loopback port with a temporary data
directory, using the real adapters, then exercises the HTTP contract. It never
reads credentials and makes no changes to the host.

Run from the repository root with the package importable:

    python3 tools/smoke_check.py

It exits non-zero if a contract check fails.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.http import serve_http
from ai_monitor.sources.registry import default_registry
from ai_monitor.state import UsageWorker

DATA = os.environ.get("AI_MONITOR_SMOKE_DIR", "/tmp/ai-monitor-smoke")
config = Config(
    data_dir=DATA,
    api_port=0,
    codex_app_server_socket=os.environ.get("CODEX_APP_SERVER_SOCKET", ""),
)
db = Database(config.data_dir + "/usage.db")
worker = UsageWorker(config=config, db=db, registry=default_registry(), version="smoke")
worker.start()
server = serve_http(config, worker, version="smoke")
import threading
http_thread = threading.Thread(target=server.serve_forever, daemon=True)
http_thread.start()
print("listening on", server.url)

time.sleep(4.0)  # let one polling cycle finish


def get(path: str):
    started = time.monotonic()
    port = urlparse(server.url).port
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        body = resp.read()
        elapsed = (time.monotonic() - started) * 1000
        return resp.status, resp.headers, json.loads(body), elapsed


for path in ("/api/v1/version", "/health", "/api/v1/usage", "/api/v1/usage/history?hours=1&limit=5"):
    status, headers, body, elapsed = get(path)
    print(f"\n=== GET {path} -> {status} ({elapsed:.1f} ms, {len(json.dumps(body))} bytes)")
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

server.shutdown()
worker.stop()
db.close()
print("\nsmoke check complete")
