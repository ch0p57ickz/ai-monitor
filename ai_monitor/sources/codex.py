"""Codex app-server adapter: JSON-RPC over the app-server unix socket.

The adapter performs the app-server handshake (connect, send one
newline-delimited JSON-RPC request, read one newline-delimited response,
close) and converts the ``account/get`` result into a normalized raw
payload.

Parser contract (spec: provider-usage-collection)
-------------------------------------------------
- ``result.rateLimitsByLimitId`` (keyed view) is used when present:
  every limit and its primary/secondary windows are preserved as
  separately identified quota windows (IDs ``codex:<limitId>:primary`` /
  ``codex:<limitId>:secondary``). Duration-based labels (e.g. ``5h``)
  are assigned only when the limit ID encodes a duration.
- When the keyed view is absent, ``result.rateLimits`` (single view) is
  used with ID ``rate-limits:primary`` and no invented limit IDs.
- A missing or ``null`` secondary window is retained as a window with
  ``used_percent``/``resets_in_seconds`` set to ``None`` (unknown), so
  the distinction between "not reported" and "zero" is preserved.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Optional

from .registry import CollectionError, SourceAdapter

HANDSHAKE_REQUEST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": "usage-1",
    "method": "account/get",
    "params": {},
}

#: Duration suffixes recognized in Codex limit IDs.
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}

#: Upper bound on the app-server response we are willing to buffer.
_MAX_RESPONSE_BYTES = 1024 * 1024


def _duration_seconds(limit_id: str) -> Optional[int]:
    """Return the window duration in seconds when the limit ID encodes one.

    ``"5h" -> 18000``, ``"30m" -> 1800``, ``"7d" -> 604800``. Returns
    ``None`` for IDs that do not end in a recognizable duration suffix.
    """
    digits = ""
    for ch in limit_id:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    suffix = limit_id[len(digits):]
    if suffix not in _DURATION_UNITS:
        return None
    return int(digits) * _DURATION_UNITS[suffix]


def _window_fields(
    window: Optional[dict[str, Any]],
) -> tuple[Optional[float], Optional[float]]:
    """Extract (used_percent, resets_in_seconds) from a raw window object."""
    if not isinstance(window, dict):
        return None, None
    used = _to_float(window.get("usedPercent"))
    resets_in = _to_float(window.get("resetsIn"))
    if resets_in is None:
        resets_in = _to_float(window.get("resetsInSeconds"))
    return used, resets_in


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def parse_codex_rate_limits(result: Any) -> list[dict[str, Any]]:
    """Convert a ``account/get`` result into normalized quota-window dicts.

    Returns a list of raw-window dicts suitable for
    ``models.normalize_observation`` (snake_case keys).
    """
    if not isinstance(result, dict):
        return []
    windows: list[dict[str, Any]] = []

    keyed = result.get("rateLimitsByLimitId")
    if isinstance(keyed, dict) and keyed:
        for limit_id in sorted(keyed):
            entry = keyed[limit_id]
            if not isinstance(entry, dict):
                continue
            duration = _duration_seconds(limit_id)
            prefix = f"codex:{limit_id}"
            primary = entry.get("primary")
            p_used, p_resets = _window_fields(primary)
            windows.append(
                {
                    "id": f"{prefix}:primary",
                    "label": _label(limit_id, duration, "primary"),
                    "scope": "rate_limit",
                    "group": limit_id,
                    "model": None,
                    "used_percent": p_used,
                    "remaining_percent": None,
                    "duration_minutes": (duration / 60) if duration else None,
                    "resets_at": None,
                    "resets_in_seconds": p_resets,
                    "reported_used_percent": p_used,
                    "reported_resets_in_seconds": p_resets,
                }
            )
            secondary = entry.get("secondary")
            s_used, s_resets = _window_fields(secondary)
            windows.append(
                {
                    "id": f"{prefix}:secondary",
                    "label": _label(limit_id, duration, "secondary"),
                    "scope": "rate_limit",
                    "group": limit_id,
                    "model": None,
                    "used_percent": s_used,
                    "remaining_percent": None,
                    "duration_minutes": (duration / 60) if duration else None,
                    "resets_at": None,
                    "resets_in_seconds": s_resets,
                    "reported_used_percent": s_used,
                    "reported_resets_in_seconds": s_resets,
                }
            )
        return windows

    single = result.get("rateLimits")
    if isinstance(single, dict):
        p_used, p_resets = _window_fields(single.get("primary"))
        windows.append(
            {
                "id": "rate-limits:primary",
                "label": "default · primary",
                "scope": "rate_limit",
                "group": None,
                "model": None,
                "used_percent": p_used,
                "remaining_percent": None,
                "duration_minutes": None,
                "resets_at": None,
                "resets_in_seconds": p_resets,
                "reported_used_percent": p_used,
                "reported_resets_in_seconds": p_resets,
            }
        )
        s_used, s_resets = _window_fields(single.get("secondary"))
        windows.append(
            {
                "id": "rate-limits:secondary",
                "label": "default · secondary",
                "scope": "rate_limit",
                "group": None,
                "model": None,
                "used_percent": s_used,
                "remaining_percent": None,
                "duration_minutes": None,
                "resets_at": None,
                "resets_in_seconds": s_resets,
                "reported_used_percent": s_used,
                "reported_resets_in_seconds": s_resets,
            }
        )
    return windows


def _label(limit_id: str, duration: Optional[int], kind: str) -> str:
    if duration:
        hours = duration / 3600
        unit = f"{hours:g}h" if duration % 3600 == 0 else f"{duration / 60:g}m"
        return f"{unit} · {kind}"
    return f"{limit_id} · {kind}"


class CodexAdapter(SourceAdapter):
    """Polls the local Codex app-server rate-limit interface."""

    def fetch(self, config) -> dict[str, Any]:
        socket_path = config.codex_app_server_socket
        if not socket_path:
            raise CollectionError(
                "not_configured", "codex app-server socket not configured"
            )
        if not os.path.exists(socket_path):
            raise CollectionError("unsupported", "codex app-server socket not present")

        try:
            result = _rpc_call(
                socket_path, HANDSHAKE_REQUEST, config.codex_timeout_seconds
            )
        except _HandshakeError as exc:
            raise exc.error

        windows = parse_codex_rate_limits(result)
        notes: dict[str, Any] = {"view": "single"}
        if isinstance(result, dict) and isinstance(result.get("rateLimitsByLimitId"), dict):
            notes["view"] = "keyed"
        elif isinstance(result, dict) and isinstance(result.get("rateLimits"), dict):
            notes["view"] = "single"

        return {
            "status": "success",
            "quota_windows": windows,
            "notes": notes,
        }


class _HandshakeError(Exception):
    def __init__(self, error: CollectionError) -> None:
        super().__init__(str(error))
        self.error = error


def _rpc_call(socket_path: str, request: dict[str, Any], timeout: float) -> Any:
    """Perform one newline-delimited JSON-RPC call over the unix socket."""
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))

        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > _MAX_RESPONSE_BYTES:
                raise CollectionError(
                    "invalid_response", "codex app-server response too large"
                )
        if not data:
            raise CollectionError("network", "codex app-server closed the connection")

        line = data.split(b"\n", 1)[0]
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            raise CollectionError(
                "invalid_response", "codex app-server returned non-JSON data"
            )

        if not isinstance(response, dict):
            raise CollectionError(
                "invalid_response", "codex app-server response is not an object"
            )
        if response.get("error"):
            message = response["error"]
            detail = (
                message.get("message", "rpc error")
                if isinstance(message, dict)
                else str(message)
            )
            raise CollectionError("internal", f"codex app-server rpc error: {detail}")
        return response.get("result")
    except FileNotFoundError:
        raise _HandshakeError(
            CollectionError("unsupported", "codex app-server socket not present")
        )
    except TimeoutError:
        raise _HandshakeError(
            CollectionError("timeout", "codex app-server did not respond in time")
        )
    except ConnectionRefusedError:
        raise _HandshakeError(
            CollectionError("network", "codex app-server connection refused")
        )
    except OSError as exc:
        raise _HandshakeError(
            CollectionError("network", f"codex app-server socket error: {exc}")
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
