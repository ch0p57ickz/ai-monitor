"""Codex app-server adapter: JSON-RPC over the app-server interface.

Two transports reach the same interface:

- ``socket`` (default) connects to an already-running app-server control socket
  (the ``codex app-server daemon`` socket).
- ``stdio`` spawns ``codex app-server --listen stdio://`` as a child process and
  speaks the same protocol over its pipes. No daemon is involved, so this works
  on hosts where the daemon cannot start — for example when AppArmor blocks the
  user namespace its bubblewrap sandbox needs.

Either way the adapter opens a session (send ``initialize``, read its reply),
then sends ``account/rateLimits/read`` and converts the result into a
normalized raw payload. Both messages are newline-delimited JSON-RPC on one
session; server notifications between replies are skipped.

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

import atexit
import json
import os
import select
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

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

    def __init__(self) -> None:
        # The stdio child, reused across polls. Created lazily so a socket
        # deployment never spawns anything.
        self._session: Optional["_StdioSession"] = None

    def fetch(self, config) -> dict[str, Any]:
        if getattr(config, "codex_transport", "socket") == "stdio":
            result = self._fetch_stdio(config)
        else:
            result = self._fetch_socket(config)

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

    def _fetch_socket(self, config) -> Any:
        socket_path = config.codex_app_server_socket
        if not socket_path:
            raise CollectionError(
                "not_configured", "codex app-server socket not configured"
            )
        if not os.path.exists(socket_path):
            raise CollectionError("unsupported", "codex app-server socket not present")
        try:
            return _rpc_call(socket_path, config.codex_timeout_seconds)
        except _HandshakeError as exc:
            raise exc.error

    def _fetch_stdio(self, config) -> Any:
        """Collect through a `codex app-server --listen stdio://` child.

        The child is started once and reused across polls; a failed exchange
        tears it down so the next poll starts a fresh one. No daemon is
        involved, which is what makes this transport work on hosts where the
        app-server daemon cannot start.
        """
        if not getattr(config, "codex_bin", ""):
            raise CollectionError(
                "not_configured", "codex_bin is not configured for the stdio transport"
            )
        session = self._stdio_session(config)
        return session.request(config.codex_timeout_seconds)

    def _stdio_session(self, config) -> "_StdioSession":
        command = [config.codex_bin, "app-server", "--listen", "stdio://"]
        session = self._session
        if session is None or session.command != command:
            if session is not None:
                session.close()
            session = _StdioSession(command)
            self._session = session
        return session

    def close(self) -> None:
        """Release the child process, if any. Safe to call more than once."""
        session, self._session = self._session, None
        if session is not None:
            session.close()


class _HandshakeError(Exception):
    def __init__(self, error: CollectionError) -> None:
        super().__init__(str(error))
        self.error = error


class _ResponseReader:
    """Reads newline-delimited JSON-RPC messages under one shared deadline.

    ``read_chunk(timeout)`` returns the next bytes, ``b""`` at end of stream, or
    raises ``TimeoutError``. Both transports (a socket and a child's pipe)
    provide one, so the framing, deadline, and notification logic is shared.
    """

    def __init__(self, read_chunk: Callable[[float], bytes], deadline: float) -> None:
        self._read_chunk = read_chunk
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
                try:
                    chunk = self._read_chunk(remaining)
                except TimeoutError:
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


def _read_rate_limits(
    reader: _ResponseReader, send: Callable[[dict[str, Any]], None]
) -> Any:
    """Send the rate-limit read and return its result."""
    send(RATE_LIMITS_REQUEST)
    return reader.read_response(RATE_LIMITS_REQUEST["id"])


def _initialize(
    reader: _ResponseReader, send: Callable[[dict[str, Any]], None]
) -> None:
    """Open a session. Required once per connection: the app-server answers a
    second ``initialize`` on the same connection with "Already initialized"."""
    send(INITIALIZE_REQUEST)
    reader.read_response(INITIALIZE_REQUEST["id"])


def _socket_chunk_reader(sock: socket.socket) -> Callable[[float], bytes]:
    """A ``read_chunk`` over a connected unix socket."""

    def read_chunk(timeout: float) -> bytes:
        sock.settimeout(timeout)
        return sock.recv(65536)

    return read_chunk


def _pipe_chunk_reader(stream: Any) -> Callable[[float], bytes]:
    """A ``read_chunk`` over a child process's stdout pipe."""

    fd = stream.fileno()

    def read_chunk(timeout: float) -> bytes:
        if timeout <= 0:
            # select() with a zero timeout polls readiness instead of failing,
            # which would let a read slip past the deadline.
            raise TimeoutError("codex app-server did not respond in time")
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            raise TimeoutError("codex app-server did not respond in time")
        return os.read(fd, 65536)

    return read_chunk


def _rpc_call(socket_path: str, timeout: float) -> Any:
    """Open a control-socket session and read the account rate limits."""
    sock: Optional[socket.socket] = None
    deadline = time.monotonic() + timeout
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
        reader = _ResponseReader(_socket_chunk_reader(sock), deadline)

        def send(message: dict[str, Any]) -> None:
            sock.sendall((json.dumps(message) + "\n").encode("utf-8"))

        _initialize(reader, send)
        return _read_rate_limits(reader, send)
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


class _StdioSession:
    """A long-lived ``codex app-server --listen stdio://`` child process.

    The child is started on first use and reused across polls. Any failed
    exchange tears it down, so the next poll starts a fresh child rather than
    talking to a wedged one. The service owns the child's lifetime; ``close()``
    (and an ``atexit`` hook) reaps it.
    """

    def __init__(self, command: list[str]) -> None:
        self.command = list(command)
        self._proc: Optional[subprocess.Popen] = None
        self._initialized = False
        self._lock = threading.Lock()
        atexit.register(self.close)

    def _ensure(self) -> "subprocess.Popen":
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise CollectionError(
                "unsupported", f"codex command not found: {self.command[0]!r}"
            )
        except OSError as exc:
            raise CollectionError(
                "network", f"could not start the codex app-server: {exc}"
            )
        # A new child is a new session and must be initialized again.
        self._initialized = False
        return self._proc

    def request(self, timeout: float) -> Any:
        """Run one initialize + rate-limits session against the child."""
        with self._lock:
            proc = self._ensure()
            stdin, stdout = proc.stdin, proc.stdout
            if stdin is None or stdout is None:  # pragma: no cover - defensive
                self._terminate()
                raise CollectionError(
                    "internal", "codex app-server stdio pipes are unavailable"
                )
            reader = _ResponseReader(
                _pipe_chunk_reader(stdout), time.monotonic() + timeout
            )

            def send(message: dict[str, Any]) -> None:
                try:
                    stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                    stdin.flush()
                except (BrokenPipeError, ValueError) as exc:
                    raise CollectionError(
                        "network", f"codex app-server closed the connection: {exc}"
                    )

            try:
                if not self._initialized:
                    _initialize(reader, send)
                    self._initialized = True
                return _read_rate_limits(reader, send)
            except Exception:
                # A failed exchange leaves the child in an unknown state.
                self._initialized = False
                self._terminate()
                raise

    def close(self) -> None:
        """Abort and reap the child. Never blocks behind an in-flight read.

        Deliberately does not take the session lock. ``UsageWorker.stop()``
        calls this after joining attempts, but an attempt that already hit its
        deadline can leave a helper thread blocked in a read; killing the child
        closes the pipe, so that read fails promptly instead of holding shutdown
        up for the whole timeout.
        """
        atexit.unregister(self.close)
        proc, self._proc = self._proc, None
        _reap(proc)

    def _terminate(self) -> None:
        """Reap the child; called from a failed exchange with the lock held."""
        proc, self._proc = self._proc, None
        _reap(proc)


def _reap(proc: Optional[subprocess.Popen]) -> None:
    """Terminate a child and wait for it, so it cannot become a zombie."""
    if proc is None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
    except OSError:  # pragma: no cover - defensive
        pass
