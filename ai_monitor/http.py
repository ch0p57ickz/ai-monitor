"""Loopback-only JSON HTTP API for the cached provider-usage service.

All endpoints are read-only JSON with ``schemaVersion: 1``. Responses carry
private cache headers and are bounded by the configured byte cap; a body that
exceeds the cap is replaced with a ``response_too_large`` error.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .models import utc_now_iso
from .state import ResponseTooLarge

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config
    from .state import UsageWorker

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Widget-appropriate response cap (256 KiB), per the widget-usage-api spec.
MAX_RESPONSE_BYTES = 262_144


class _UsageRequestHandler(BaseHTTPRequestHandler):
    """Serves read-only, loopback usage endpoints.

    A per-server subclass binds ``_worker_ref`` / ``_version_ref`` /
    ``_config_ref`` so multiple servers never share state.
    """

    protocol_version = "HTTP/1.1"

    _worker_ref: Optional["UsageWorker"] = None
    _version_ref: str = "unknown"
    _config_ref: Optional["Config"] = None

    @property
    def _cap(self) -> int:
        cfg = self._config_ref
        if cfg is not None:
            return int(cfg.response_cap_bytes)
        return MAX_RESPONSE_BYTES

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D401
        log.exception("unhandled error from %s", client_address)

    # -- response helpers ---------------------------------------------

    def _json_bytes(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload).encode("utf-8")

    def _send_payload(self, status: int, payload: dict[str, Any]) -> None:
        body = self._json_bytes(payload)
        if len(body) > self._cap:
            self._send_error(413, "response_too_large", "response exceeds the configured size cap")
            return
        self._write(status, body)

    def _write(
        self,
        status: int,
        body: bytes,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "private, no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(
        self,
        status: int,
        category: str,
        message: str,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": utc_now_iso(),
            "error": {"category": category, "message": message},
        }
        body = self._json_bytes(payload)
        # Never let an error body exceed the cap either.
        if len(body) > self._cap:
            body = self._json_bytes(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "generatedAt": utc_now_iso(),
                    "error": {"category": category, "message": "error"},
                }
            )
        self._write(status, body, extra_headers)

    def _drain_body(self) -> None:
        """Consume any request body so keep-alive connections stay in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            if length > self._cap:
                # Too large to drain fully; close instead of desyncing.
                self.close_connection = True
            else:
                self.rfile.read(length)
        if self.headers.get("Transfer-Encoding"):
            # We do not parse chunked bodies; close rather than desync.
            self.close_connection = True

    # -- query parsing -------------------------------------------------

    def _parse_history_query(self) -> tuple[Optional[tuple[int, int]], Optional[str]]:
        """Return ((hours, limit), None) or (None, error_message)."""
        cfg = self._config_ref
        default_hours = cfg.history_default_hours if cfg else 24
        max_hours = cfg.history_max_hours if cfg else 168
        default_limit = cfg.history_default_limit if cfg else 100
        max_limit = cfg.history_max_limit if cfg else 500

        params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        hours = default_hours
        limit = default_limit

        if "hours" in params:
            raw = params["hours"][0]
            try:
                hours = int(raw)
            except (TypeError, ValueError):
                return None, "hours must be an integer"
            if hours < 1 or hours > max_hours:
                return None, f"hours must be between 1 and {max_hours}"

        if "limit" in params:
            raw = params["limit"][0]
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                return None, "limit must be an integer"
            if limit < 1 or limit > max_limit:
                return None, f"limit must be between 1 and {max_limit}"

        return (hours, limit), None

    # -- routing -------------------------------------------------------

    def _dispatch_get(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        worker = self._worker_ref
        version = self._version_ref
        if path == "/health":
            self._send_payload(200, worker.health_payload())
        elif path == "/api/v1/usage":
            self._send_payload(200, worker.usage_payload())
        elif path == "/api/v1/usage/history":
            parsed, error = self._parse_history_query()
            if parsed is None:
                self._send_error(400, "invalid_query", error or "invalid query")
                return
            hours, limit = parsed
            try:
                payload = worker.history_payload(
                    hours=hours, limit=limit, max_bytes=self._cap
                )
            except ResponseTooLarge:
                self._send_error(
                    413, "response_too_large", "response exceeds the configured size cap"
                )
                return
            except Exception:  # noqa: BLE001
                log.exception("history query failed")
                self._send_error(503, "storage_unavailable", "history is temporarily unavailable")
                return
            self._send_payload(200, payload)
        elif path in ("/", "/api"):
            # Root returns a minimal versioned index (not a data endpoint).
            self._send_payload(
                200,
                {
                    "api_version": "1",
                    "endpoints": [
                        "/health",
                        "/api/v1/usage",
                        "/api/v1/usage/history",
                        "/api/v1/version",
                    ],
                },
            )
        elif path == "/api/v1/version":
            self._send_payload(
                200,
                {
                    "api_version": "1",
                    "service_version": version,
                },
            )
        else:
            self._send_error(404, "not_found", "no such endpoint")

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._dispatch_get()
        except Exception:  # noqa: BLE001
            log.exception("unhandled error serving %s", self.path)
            try:
                self._send_error(500, "internal", "internal error")
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def _method_not_allowed(self) -> None:
        self._drain_body()
        self._send_error(
            405,
            "method_not_allowed",
            "this endpoint is read-only",
            extra_headers={"Allow": "GET"},
        )

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_HEAD(self) -> None:  # noqa: N802
        # HEAD must not carry a body, but still reports the read-only status.
        self._drain_body()
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.send_header("Allow", "GET")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()


class UsageServer:
    """Bounded, loopback-bound HTTP server over the worker."""

    def __init__(self, host: str, port: int, worker: "UsageWorker", version: str,
                 config: Optional["Config"] = None) -> None:
        if host != "127.0.0.1":
            raise ValueError("Usage service must bind to 127.0.0.1 only")
        # Bind the handler to this server's worker/version/config via a
        # per-server subclass so two servers never share state.
        handler = type(
            "_BoundUsageHandler",
            (_UsageRequestHandler,),
            {
                "_worker_ref": worker,
                "_version_ref": version,
                "_config_ref": config,
            },
        )
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self.port: int = self._httpd.server_address[1]

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
    return UsageServer(config.api_host, config.api_port, worker, version, config=config)
