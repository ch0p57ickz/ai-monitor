"""Tests for the Codex app-server adapter and rate-limit parser.

Covers task 3.1: the JSON-RPC handshake over the app-server unix socket, the
keyed view (primary + secondary preserved per limit), the single-view
fallback, and missing-field handling.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.sources.codex import (
    CodexAdapter,
    _duration_seconds,
    parse_codex_rate_limits,
)
from ai_monitor.sources.registry import CollectionError

CODEX = get_source_def("codex")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestDurationSeconds:
    @pytest.mark.parametrize(
        "limit_id, expected",
        [
            ("5h", 18000),
            ("30m", 1800),
            ("7d", 604800),
            ("codex", None),
            ("5x", None),
            ("", None),
        ],
    )
    def test_duration(self, limit_id: str, expected):
        assert _duration_seconds(limit_id) == expected


class TestKeyedView:
    def test_preserves_primary_and_secondary(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {
                    "primary": {
                        "usedPercent": 40,
                        "windowDurationMins": 300,
                        "resetsAt": 1760000000,
                    },
                    "secondary": {
                        "usedPercent": 10,
                        "windowDurationMins": 10080,
                        "resetsAt": 1760600000,
                    },
                },
                "7d": {
                    "primary": {"usedPercent": 60, "windowDurationMins": 10080}
                },
            }
        }

        windows = parse_codex_rate_limits(result)

        ids = [w["id"] for w in windows]
        assert ids == [
            "codex:5h:primary",
            "codex:5h:secondary",
            "codex:7d:primary",
            "codex:7d:secondary",
        ]

        five_primary = windows[0]
        assert five_primary["used_percent"] == 40.0
        assert five_primary["duration_minutes"] == 300
        assert five_primary["resets_at"] == "2025-10-09T08:53:20Z"
        assert five_primary["label"] == "5h · primary"
        assert five_primary["group"] == "5h"
        assert five_primary["scope"] == "rate_limit"

        five_secondary = windows[1]
        assert five_secondary["duration_minutes"] == 10080
        assert five_secondary["label"] == "7d · secondary"

        # A missing window is retained as unknown, not fabricated as zero.
        seven_secondary = next(w for w in windows if w["id"] == "codex:7d:secondary")
        assert seven_secondary["used_percent"] is None
        assert seven_secondary["resets_at"] is None
        assert seven_secondary["duration_minutes"] is None

    def test_duration_falls_back_to_the_limit_id_suffix(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {"primary": {"usedPercent": 1}},
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["duration_minutes"] == 300
        assert windows[0]["label"] == "5h · primary"

    def test_unknown_duration_suffix_keeps_no_duration(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 1}}
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["duration_minutes"] is None
        assert windows[0]["label"] == "codex · primary"

    def test_resets_at_unix_seconds_become_iso(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {"primary": {"usedPercent": 1, "resetsAt": 1760000000}}
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["resets_at"] == "2025-10-09T08:53:20Z"

    def test_non_finite_resets_at_is_unknown(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {"primary": {"usedPercent": 1, "resetsAt": float("inf")}}
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["resets_at"] is None


class TestSingleViewFallback:
    def test_uses_rate_limits_when_no_keyed_view(self):
        result = {
            "rateLimits": {
                "primary": {"usedPercent": 25, "windowDurationMins": 300},
                "secondary": {"usedPercent": 5, "windowDurationMins": 10080},
            }
        }
        windows = parse_codex_rate_limits(result)
        assert [w["id"] for w in windows] == [
            "rate-limits:primary",
            "rate-limits:secondary",
        ]
        assert windows[0]["used_percent"] == 25.0
        assert windows[0]["label"] == "5h · primary"
        assert windows[1]["used_percent"] == 5.0

    def test_keyed_view_takes_precedence(self):
        result = {
            "rateLimitsByLimitId": {"5h": {"primary": {"usedPercent": 40.0}}},
            "rateLimits": {"primary": {"usedPercent": 99.0}},
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["id"] == "codex:5h:primary"
        assert all(not w["id"].startswith("rate-limits:") for w in windows)


class TestMissingData:
    @pytest.mark.parametrize("result", [None, {}, [], "not-a-dict", 42])
    def test_non_dict_or_empty_returns_empty(self, result):
        assert parse_codex_rate_limits(result) == []

    def test_non_numeric_values_become_unknown(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {
                    "primary": {"usedPercent": "n/a", "resetsAt": "soon"},
                    "secondary": {
                        "usedPercent": float("nan"),
                        "resetsAt": float("inf"),
                    },
                }
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["used_percent"] is None
        assert windows[0]["resets_at"] is None
        assert windows[1]["used_percent"] is None
        assert windows[1]["resets_at"] is None

    def test_normalizes_into_quota_windows(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {
                    "primary": {
                        "usedPercent": 40,
                        "windowDurationMins": 300,
                        "resetsAt": 1760000000,
                    }
                }
            }
        }
        obs = normalize_observation(
            CODEX, {"status": "success", "quota_windows": parse_codex_rate_limits(result)}
        )
        assert obs.quota_windows[0].id == "codex:5h:primary"
        assert obs.quota_windows[0].used_percent == 40.0
        assert obs.quota_windows[0].duration_minutes == 300
        assert obs.quota_windows[0].resets_at == "2025-10-09T08:53:20Z"


# ---------------------------------------------------------------------------
# Handshake (fake unix-socket app-server)
# ---------------------------------------------------------------------------


def _serve_once(
    sock_path: Path,
    result: dict | None = None,
    *,
    error: dict | None = None,
    raw: bytes | None = None,
    notifications: tuple[dict, ...] = (),
    capture: list | None = None,
    hang: bool = False,
):
    """Start a one-session app-server on a unix socket; return its thread.

    The session mirrors the real app-server: it replies to ``initialize``
    first, optionally emits notifications, then replies to the rate-limit
    request. Received requests are appended to ``capture`` when given.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def read_request(conn) -> dict | None:
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        line = data.split(b"\n", 1)[0]
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"_unparseable": line.decode("utf-8", "replace")}

    def send(conn, message: dict | bytes) -> None:
        payload = message if isinstance(message, bytes) else (
            json.dumps(message) + "\n"
        ).encode()
        try:
            conn.sendall(payload)
        except OSError:
            pass  # client gave up (e.g. size-bound test)

    def run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                if hang:
                    # Hold the connection open without replying so the client
                    # hits its socket timeout rather than seeing EOF.
                    time.sleep(2)
                    return
                first = read_request(conn)
                if capture is not None:
                    capture.append(first)
                if first is None:
                    return
                send(conn, {"jsonrpc": "2.0", "id": "init-1", "result": {}})
                for note in notifications:
                    send(conn, note)
                second = read_request(conn)
                if capture is not None:
                    capture.append(second)
                if second is None:
                    return
                if raw is not None:
                    send(conn, raw)
                    return
                message: dict = {"jsonrpc": "2.0", "id": "usage-1"}
                if error is not None:
                    message["error"] = error
                else:
                    message["result"] = result if result is not None else {}
                send(conn, message)
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _config(tmp_path: Path, sock: Path) -> Config:
    return Config(
        data_dir=str(tmp_path / "data"),
        codex_app_server_socket=str(sock),
        codex_timeout_seconds=5,
    )


class TestHandshake:
    def test_initialize_then_rate_limits_read(self, tmp_path: Path):
        """The session must open with initialize, then read rate limits."""
        sock = tmp_path / "codex.sock"
        capture: list = []
        thread = _serve_once(
            sock,
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 4,
                            "windowDurationMins": 300,
                            "resetsAt": 1760000000,
                        }
                    }
                }
            },
            capture=capture,
        )

        out = CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)

        assert [r.get("method") for r in capture] == [
            "initialize",
            "account/rateLimits/read",
        ]
        assert capture[0]["id"] == "init-1"
        assert capture[0]["params"]["clientInfo"]["name"] == "ai-monitor"
        assert capture[1]["id"] == "usage-1"
        assert out["status"] == "success"
        assert out["notes"]["view"] == "keyed"
        assert out["quota_windows"][0]["used_percent"] == 4.0
        assert out["quota_windows"][0]["duration_minutes"] == 300
        assert out["quota_windows"][0]["resets_at"] == "2025-10-09T08:53:20Z"

    def test_server_notifications_are_skipped(self, tmp_path: Path):
        """Notifications between replies must not be mistaken for the answer."""
        sock = tmp_path / "codex.sock"
        thread = _serve_once(
            sock,
            {"rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300}}},
            notifications=(
                {"method": "configWarning", "params": {"summary": "sandbox"}},
                {"method": "account/updated", "params": {"authMode": "chatgpt"}},
            ),
        )

        out = CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)

        assert out["notes"]["view"] == "single"
        assert out["quota_windows"][0]["id"] == "rate-limits:primary"
        assert out["quota_windows"][0]["used_percent"] == 25.0

    def test_keyed_success(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        result = {
            "rateLimitsByLimitId": {
                "5h": {
                    "primary": {"usedPercent": 40, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 10, "windowDurationMins": 10080},
                }
            }
        }
        thread = _serve_once(sock, result)

        out = CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)

        assert out["status"] == "success"
        assert out["notes"]["view"] == "keyed"
        assert [w["id"] for w in out["quota_windows"]] == [
            "codex:5h:primary",
            "codex:5h:secondary",
        ]

    def test_single_view_success(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        result = {"rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300}}}
        thread = _serve_once(sock, result)

        out = CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)

        assert out["notes"]["view"] == "single"
        assert out["quota_windows"][0]["id"] == "rate-limits:primary"

    def test_rpc_error_on_rate_limits_is_internal(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(
            sock, error={"code": -32601, "message": "method not found"}
        )
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "internal"

    def test_non_json_is_invalid_response(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, raw=b"not json\n")
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "invalid_response"

    def test_empty_response_is_network_error(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, raw=b"")
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "network"

    def test_oversized_response_is_invalid_response(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        # > 1 MiB with no newline: the adapter must stop buffering and fail.
        thread = _serve_once(sock, raw=b"x" * (1024 * 1024 + 4096))
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "invalid_response"

    def test_timeout(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, hang=True)
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            codex_app_server_socket=str(sock),
            codex_timeout_seconds=1,
        )
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(cfg)
        thread.join(timeout=5)
        assert exc_info.value.category == "timeout"
