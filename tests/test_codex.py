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
                    "primary": {"usedPercent": 40.0, "resetsIn": 100},
                    "secondary": {"usedPercent": 10.0, "resetsIn": 200},
                },
                "7d": {
                    "primary": {"usedPercent": 60.0, "resetsIn": 300}
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
        assert five_primary["resets_in_seconds"] == 100
        assert five_primary["duration_minutes"] == 300
        assert five_primary["label"] == "5h · primary"
        assert five_primary["group"] == "5h"
        assert five_primary["scope"] == "rate_limit"

        # A missing secondary is retained as unknown, not fabricated as zero.
        seven_secondary = next(w for w in windows if w["id"] == "codex:7d:secondary")
        assert seven_secondary["used_percent"] is None
        assert seven_secondary["resets_in_seconds"] is None

    def test_unknown_duration_suffix_keeps_no_duration(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 1.0, "resetsIn": 5}}
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["duration_minutes"] is None
        assert windows[0]["label"] == "codex · primary"

    def test_resets_in_seconds_alias(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {"primary": {"usedPercent": 1.0, "resetsInSeconds": 42}}
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["resets_in_seconds"] == 42


class TestSingleViewFallback:
    def test_uses_rate_limits_when_no_keyed_view(self):
        result = {
            "rateLimits": {
                "primary": {"usedPercent": 25.0, "resetsIn": 50},
                "secondary": {"usedPercent": 5.0, "resetsIn": 60},
            }
        }
        windows = parse_codex_rate_limits(result)
        assert [w["id"] for w in windows] == [
            "rate-limits:primary",
            "rate-limits:secondary",
        ]
        assert windows[0]["used_percent"] == 25.0
        assert windows[0]["label"] == "default · primary"
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
                    "primary": {"usedPercent": "n/a", "resetsIn": "soon"},
                    "secondary": {"usedPercent": float("nan"), "resetsIn": float("inf")},
                }
            }
        }
        windows = parse_codex_rate_limits(result)
        assert windows[0]["used_percent"] is None
        assert windows[0]["resets_in_seconds"] is None
        assert windows[1]["used_percent"] is None
        assert windows[1]["resets_in_seconds"] is None

    def test_normalizes_into_quota_windows(self):
        result = {
            "rateLimitsByLimitId": {
                "5h": {"primary": {"usedPercent": 40.0, "resetsIn": 100}}
            }
        }
        obs = normalize_observation(
            CODEX, {"status": "success", "quota_windows": parse_codex_rate_limits(result)}
        )
        assert obs.quota_windows[0].id == "codex:5h:primary"
        assert obs.quota_windows[0].used_percent == 40.0
        assert obs.quota_windows[0].duration_minutes == 300


# ---------------------------------------------------------------------------
# Handshake (fake unix-socket app-server)
# ---------------------------------------------------------------------------


def _serve_once(
    sock_path: Path,
    response: dict,
    *,
    raw: bytes | None = None,
    hang: bool = False,
):
    """Start a one-shot JSON-RPC server on a unix socket; return its thread."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)  # request line; ignored
                if hang:
                    # Hold the connection open without replying so the client
                    # hits its socket timeout rather than seeing EOF.
                    time.sleep(2)
                    return
                payload = raw if raw is not None else (json.dumps(response) + "\n").encode()
                try:
                    conn.sendall(payload)
                except OSError:
                    pass  # client gave up (e.g. size-bound test)
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
    def test_keyed_success(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        result = {
            "rateLimitsByLimitId": {
                "5h": {
                    "primary": {"usedPercent": 40.0, "resetsIn": 100},
                    "secondary": {"usedPercent": 10.0, "resetsIn": 200},
                }
            }
        }
        thread = _serve_once(sock, {"jsonrpc": "2.0", "id": "usage-1", "result": result})

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
        result = {"rateLimits": {"primary": {"usedPercent": 25.0, "resetsIn": 50}}}
        thread = _serve_once(sock, {"result": result})

        out = CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)

        assert out["notes"]["view"] == "single"
        assert out["quota_windows"][0]["id"] == "rate-limits:primary"

    def test_rpc_error_is_internal(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(
            sock, {"error": {"code": -32601, "message": "method not found"}}
        )
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "internal"

    def test_non_json_is_invalid_response(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, {}, raw=b"not json\n")
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "invalid_response"

    def test_empty_response_is_network_error(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, {}, raw=b"")
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "network"

    def test_oversized_response_is_invalid_response(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        # > 1 MiB with no newline: the adapter must stop buffering and fail.
        thread = _serve_once(sock, {}, raw=b"x" * (1024 * 1024 + 4096))
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(_config(tmp_path, sock))
        thread.join(timeout=5)
        assert exc_info.value.category == "invalid_response"

    def test_timeout(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, {}, hang=True)
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            codex_app_server_socket=str(sock),
            codex_timeout_seconds=1,
        )
        with pytest.raises(CollectionError) as exc_info:
            CodexAdapter().fetch(cfg)
        thread.join(timeout=5)
        assert exc_info.value.category == "timeout"
