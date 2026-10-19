"""Codex app-server adapter: JSON-RPC over the app-server unix socket.

The adapter opens a session (connect, send ``initialize``, read its reply),
then sends ``account/rateLimits/read`` and converts the result into a
normalized raw payload. Both messages are newline-delimited JSON-RPC on one
connection; server notifications between replies are skipped.

Parser contract (spec: provider-usage-collection)
-------------------------------------------------
- ``result.rateLimitsByLimitId`` (keyed view) is used when present:
  every limit and its primary/secondary windows are preserved as
  separately identified quota windows (IDs ``codex:<limitId>:primary`` /
  ``codex:<limitId>:secondary``). Duration-based labels (e.g. ``5h``) come
  from the reported ``windowDurationMins``, falling back to a duration
  encoded in the limit ID.
- When the keyed view is absent, ``result.rateLimits`` (single view) is
  used with ID ``rate-limits:primary`` and no invented limit IDs.
- A missing or ``null`` window is retained with ``used_percent`` and
  ``resets_at`` set to ``None`` (unknown), so the distinction between
  "not reported" and "zero" is preserved.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .. import __version__
from ..identity import account_identity, derive_account_id
from .registry import CollectionError, SourceAdapter

#: JSON-RPC request that opens an app-server session. The app-server closes the
#: connection when any request arrives before ``initialize``
#: (codex-cli 0.157.1 sends ``configWarning``/``account/updated`` notifications
#: after this reply).
INITIALIZE_REQUEST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": "init-1",
    "method": "initialize",
    "params": {
        "clientInfo": {"name": "ai-monitor", "version": __version__},
    },
}

#: Reads the account rate-limit snapshot. Verified against codex-cli 0.157.1
#: (`codex app-server generate-json-schema`: method ``Account/rateLimits/read``,
#: response ``GetAccountRateLimitsResponse``). The previous ``account/get``
#: guess is not a valid method and is answered with a closed connection.
RATE_LIMITS_REQUEST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": "usage-1",
    "method": "account/rateLimits/read",
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


def _unix_to_iso(value: Any) -> Optional[str]:
    """Convert an integer Unix timestamp (seconds) to an ISO-8601 UTC string."""
    seconds = _to_float(value)
    if seconds is None:
        return None
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _window_entry(
    *,
    window_id: str,
    limit_id: Optional[str],
    kind: str,
    window: Any,
    fallback_duration_minutes: Optional[float],
) -> dict[str, Any]:
    """Normalize one app-server rate-limit window.

    Confirmed shape (codex-cli 0.157.1 ``RateLimitWindow``): ``usedPercent``
    (integer percent), ``windowDurationMins`` (minutes, nullable), and
    ``resetsAt`` (Unix seconds, nullable). Every field stays ``None`` when the
    app-server does not report it: unknown is never inferred as zero or full.

    The window's own duration is used when reported. A *reported* window that
    omits it (``{"primary": {}}``) falls back to a duration encoded in the
    limit ID, because the limit still identifies the bucket; an *absent*
    window (``primary`` missing or ``null``) gets no duration and no label
    derived from one, since there is no window to describe. The used percent is
    never inferred either way.
    """
    raw = window if isinstance(window, dict) else {}
    used = _to_float(raw.get("usedPercent"))
    duration_minutes = _to_float(raw.get("windowDurationMins"))
    if duration_minutes is None and isinstance(window, dict):
        # A reported window that omits its duration still belongs to a limit
        # whose ID may encode one; an absent window does not.
        duration_minutes = fallback_duration_minutes
    resets_at = _unix_to_iso(raw.get("resetsAt"))
    return {
        "id": window_id,
        "label": _label(limit_id, duration_minutes, kind),
        "scope": "rate_limit",
        "group": limit_id,
        "model": None,
        "used_percent": used,
        "remaining_percent": None,
        "duration_minutes": duration_minutes,
        "resets_at": resets_at,
    }


def parse_codex_rate_limits(result: Any) -> list[dict[str, Any]]:
    """Convert an ``account/rateLimits/read`` result into raw window dicts.

    Returns a list of raw-window dicts suitable for
    ``models.normalize_observation`` (snake_case keys). The keyed
    ``rateLimitsByLimitId`` view is preferred; the single ``rateLimits`` view is
    used only when the keyed view is absent.
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
            # The reported ``windowDurationMins`` is authoritative; the limit ID
            # suffix is only a fallback for older payloads that omit it.
            seconds = _duration_seconds(limit_id)
            fallback_minutes = (seconds / 60) if seconds else None
            prefix = f"codex:{limit_id}"
            for kind in ("primary", "secondary"):
                windows.append(
                    _window_entry(
                        window_id=f"{prefix}:{kind}",
                        limit_id=limit_id,
                        kind=kind,
                        window=entry.get(kind),
                        fallback_duration_minutes=fallback_minutes,
                    )
                )
        return windows

    single = result.get("rateLimits")
    if isinstance(single, dict):
        for kind in ("primary", "secondary"):
            windows.append(
                _window_entry(
                    window_id=f"rate-limits:{kind}",
                    limit_id=None,
                    kind=kind,
                    window=single.get(kind),
                    fallback_duration_minutes=None,
                )
            )
    return windows


def _label(limit_id: Optional[str], duration_minutes: Optional[float], kind: str) -> str:
    """A duration-based label when the reported duration supports one."""
    if duration_minutes:
        if duration_minutes % 1440 == 0:
            return f"{duration_minutes / 1440:g}d · {kind}"
        if duration_minutes % 60 == 0:
            return f"{duration_minutes / 60:g}h · {kind}"
        return f"{duration_minutes:g}m · {kind}"
    return f"{limit_id or 'rate-limits'} · {kind}"


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
            result = _rpc_call(socket_path, config.codex_timeout_seconds)
        except _HandshakeError as exc:
            raise exc.error

        windows = parse_codex_rate_limits(result)
        # Mirror the parser's branch predicate exactly (non-empty keyed dict),
        # so the diagnostic label never disagrees with the data.
        keyed = result.get("rateLimitsByLimitId") if isinstance(result, dict) else None
        view = "keyed" if isinstance(keyed, dict) and keyed else "single"
        notes: dict[str, Any] = {"view": view}

        payload: dict[str, Any] = {
            "status": "success",
            "quota_windows": windows,
            "notes": notes,
        }

        # Only an opaque, keyed digest is kept: the app-server identity (which
        # may be an account name) is never stored. Without a digest key, or
        # without an identity, the account stays unknown so account-dependent
        # trends are avoided.
        account_id = derive_account_id(
            "codex", account_identity(result), config.account_digest_key
        )
        if account_id:
            payload["account_id"] = account_id
        return payload


class _HandshakeError(Exception):
    def __init__(self, error: CollectionError) -> None:
        super().__init__(str(error))
        self.error = error


class _ResponseReader:
    """Reads newline-delimited JSON-RPC messages under one shared deadline."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline
        self._buffer = b""

    def read_response(self, want_id: Any) -> Any:
        """Return the result for ``want_id``, skipping server notifications.

        The app-server emits notifications (``configWarning``,
        ``account/updated``, ...) between replies, so a single line read is not
        enough: read until the message carrying our request id arrives.
        """
        while True:
            if time.monotonic() >= self._deadline:
                raise CollectionError(
                    "timeout", "codex app-server did not respond in time"
                )
            message = self._read_message()
            message_id = message.get("id")
            if (
                "method" not in message
                and message.get("error") is not None
                and message_id in (None, want_id)
            ):
                # An id-less error is how JSON-RPC reports a rejected request
                # (parse error / invalid request). Surface it: waiting for the
                # matching reply would block until the deadline. A message with
                # a ``method`` is a notification and is never fatal.
                raise _rpc_error(message["error"])
            if message_id != want_id:
                continue
            return message.get("result")

    def _read_message(self) -> dict[str, Any]:
        while True:
            while b"\n" not in self._buffer:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionError(
                        "timeout", "codex app-server did not respond in time"
                    )
                self._sock.settimeout(remaining)
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    raise CollectionError(
                        "timeout", "codex app-server did not respond in time"
                    )
                if not chunk:
                    raise CollectionError(
                        "network", "codex app-server closed the connection"
                    )
                self._buffer += chunk
                if len(self._buffer) > _MAX_RESPONSE_BYTES:
                    raise CollectionError(
                        "invalid_response", "codex app-server response too large"
                    )
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                # Keep-alive/blank lines are tolerated, not recursed on.
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                raise CollectionError(
                    "invalid_response", "codex app-server returned non-JSON data"
                )
            if not isinstance(message, dict):
                raise CollectionError(
                    "invalid_response", "codex app-server response is not an object"
                )
            return message


def _rpc_error(detail: Any) -> CollectionError:
    text = (
        detail.get("message", "rpc error")
        if isinstance(detail, dict)
        else str(detail)
    )
    return CollectionError("internal", f"codex app-server rpc error: {text}")


def _rpc_call(socket_path: str, timeout: float) -> Any:
    """Open an app-server session and read the account rate limits.

    A new connection must send ``initialize`` first; the app-server closes the
    socket for any other request. The rate-limit read then follows on the same
    connection.
    """
    sock: Optional[socket.socket] = None
    deadline = time.monotonic() + timeout
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
        reader = _ResponseReader(sock, deadline)

        sock.sendall((json.dumps(INITIALIZE_REQUEST) + "\n").encode("utf-8"))
        reader.read_response(INITIALIZE_REQUEST["id"])

        sock.sendall((json.dumps(RATE_LIMITS_REQUEST) + "\n").encode("utf-8"))
        return reader.read_response(RATE_LIMITS_REQUEST["id"])
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
