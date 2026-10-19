"""Loopback-only JSON HTTP API for the cached provider-usage service."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config
    from .state import UsageWorker

SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB cap per spec


class _UsageRequestHandler(BaseHTTPRequestHandler):
    """Serves read-only, loopback usage endpoints."""

    protocol_version = "HTTP/1.1"

    _worker_ref: Optional["UsageWorker"] = None
    _version_ref: str = "unknown"

    def _get_worker(self) -> "UsageWorker":
        return self._worker_ref  # type: ignore[return-value]

    def _get_version(self) -> str:
        return self._version_ref

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        # Keep quiet in tests; production can override via logging.
        pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            self.send_response(413)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        worker = self._worker_ref
        version = self._version_ref
        if path == "/health":
            self._send_json(200, worker.health_payload())
        elif path == "/api/v1/usage":
            self._send_json(200, worker.usage_payload())
        elif path in ("/", "/api"):
            # Root returns a minimal versioned index (not a data endpoint).
            self._send_json(200, {"api_version": "1", "endpoints": ["/health", "/api/v1/usage", "/api/v1/version"]})
        elif path == "/api/v1/version":
            self._send_json(
                200,
                {
                    "api_version": "1",
                    "service_version": version,
                },
            )
        else:
            self._send_json(404, {"error": "not_found"})


class UsageServer:
    """Bounded, loopback-bound HTTP server over the worker."""

    def __init__(self, host: str, port: int, worker: "UsageWorker", version: str) -> None:
        if host != "127.0.0.1":
            raise ValueError("Usage service must bind to 127.0.0.1 only")
        _UsageRequestHandler._worker_ref = worker
        _UsageRequestHandler._version_ref = version
        self._httpd = ThreadingHTTPServer((host, port), _UsageRequestHandler)
        self._httpd.daemon_threads = True
        self.port: int = self._httpd.server_address[1]
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def serve_http(
    config: "Config",
    worker: "UsageWorker",
    *,
    version: str = "unknown",
) -> UsageServer:
    return UsageServer(config.api_host, config.api_port, worker, version)
