"""Service configuration for the GX10 usage service.

Configuration is loaded from a JSON file and validated strictly. Invalid values
raise :class:`ConfigError` with a stable error category so an operator can see
exactly what was wrong without a stack trace.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any

# ---------------------------------------------------------------------------
# Defaults and bounds
# ---------------------------------------------------------------------------

#: Default polling intervals in seconds.
DEFAULT_CODEX_INTERVAL = 60.0
DEFAULT_ANTIGRAVITY_INTERVAL = 60.0
DEFAULT_OPENROUTER_INTERVAL = 300.0

#: Hard bounds for operator-configurable intervals.
INTERVAL_BOUNDS = (1.0, 86400.0)

#: Port bound for the local HTTP API. 0 means "auto-assign a free port"
#: (used by tests).
PORT_BOUNDS = (0, 65535)

#: Response size cap (bytes) shared by all endpoints. The widget contract
#: specifies a 256 KiB cap.
DEFAULT_RESPONSE_CAP_BYTES = 262_144

#: History query bounds.
DEFAULT_HISTORY_HOURS = 24
MAX_HISTORY_HOURS = 168
DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 500

#: Default retention for stored observations (days).
DEFAULT_RETENTION_DAYS = 30

#: Default pace tolerance in percentage points.
DEFAULT_PACE_TOLERANCE = 5.0

#: Retry/backoff bounds (seconds).
DEFAULT_RETRY_BASE_DELAY = 5.0
DEFAULT_RETRY_MAX_DELAY = 300.0
DEFAULT_RETRY_BACKOFF_FACTOR = 2.0
DEFAULT_RETRY_JITTER = 0.2

#: Per-attempt deadline bounds (seconds).
DEFAULT_DEADLINE = 30.0
DEADLINE_BOUNDS = (1.0, 3600.0)

#: How the Codex adapter reaches the app-server.
CODEX_TRANSPORT_SOCKET = "socket"
CODEX_TRANSPORT_STDIO = "stdio"
_CODEX_TRANSPORTS = frozenset({CODEX_TRANSPORT_SOCKET, CODEX_TRANSPORT_STDIO})

#: Default scheduler tick (seconds).
DEFAULT_SCHEDULER_TICK = 1.0

#: Default per-source timeouts (seconds).
DEFAULT_CODEX_TIMEOUT = 10.0
DEFAULT_ANTIGRAVITY_TIMEOUT = 15.0
DEFAULT_OPENROUTER_TIMEOUT = 15.0


class ConfigError(ValueError):
    """Raised when configuration is invalid."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class Config:
    """Validated service configuration.

    All durations are in seconds. ``api_host`` must be the loopback address
    (the spec requires a ``127.0.0.1`` binding).
    """

    api_host: str = "127.0.0.1"
    api_port: int = 8732
    data_dir: str = "data"

    scheduler_tick_seconds: float = 1.0

    codex_interval: float = DEFAULT_CODEX_INTERVAL
    antigravity_interval: float = DEFAULT_ANTIGRAVITY_INTERVAL
    openrouter_interval: float = DEFAULT_OPENROUTER_INTERVAL

    codex_app_server_socket: str = ""
    # How the Codex adapter reaches the app-server: ``socket`` connects to an
    # already-running app-server control socket (the daemon), ``stdio`` spawns
    # `codex app-server --listen stdio://` as a child process and speaks the
    # same protocol over its pipes. ``stdio`` needs no daemon, which matters on
    # hosts where the daemon cannot start (for example when AppArmor blocks the
    # bubblewrap sandbox's user namespace).
    codex_transport: str = "socket"
    codex_bin: str = "codex"
    codex_timeout_seconds: int = 10

    antigravity_bin: str = "agy"
    antigravity_timeout_seconds: int = int(DEFAULT_ANTIGRAVITY_TIMEOUT)

    openrouter_api_key: str = field(default="", repr=False, compare=False)
    # Management/provisioning key for account credits (/api/v1/credits).
    # Falls back to openrouter_api_key when empty.
    openrouter_management_key: str = field(default="", repr=False, compare=False)
    openrouter_base_url: str = "https://openrouter.ai"
    openrouter_timeout_seconds: int = 15

    deadline: float = DEFAULT_DEADLINE
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY
    retry_backoff_factor: float = DEFAULT_RETRY_BACKOFF_FACTOR
    retry_jitter: float = DEFAULT_RETRY_JITTER

    pace_tolerance: float = DEFAULT_PACE_TOLERANCE
    retention_days: int = DEFAULT_RETENTION_DAYS

    history_default_hours: int = DEFAULT_HISTORY_HOURS
    history_max_hours: int = MAX_HISTORY_HOURS
    history_default_limit: int = DEFAULT_HISTORY_LIMIT
    history_max_limit: int = MAX_HISTORY_LIMIT

    response_cap_bytes: int = DEFAULT_RESPONSE_CAP_BYTES

    # The key used to derive opaque account IDs.
    account_digest_key: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate(self)

    def source_interval(self, source_id: str) -> float:
        """Return the configured interval for ``source_id``."""
        intervals = {
            "codex": self.codex_interval,
            "antigravity": self.antigravity_interval,
            "openrouter-account": self.openrouter_interval,
            "openrouter-key": self.openrouter_interval,
        }
        try:
            return intervals[source_id]
        except KeyError:
            raise ConfigError("unknown_source", f"no interval defined for {source_id!r}")

    def stale_after(self, source_id: str) -> float:
        """Return the staleness threshold (two missed intervals) for a source."""
        return 2.0 * self.source_interval(source_id)


_BOUNDS: dict[str, tuple[float, float]] = {
    "api_port": PORT_BOUNDS,
    "codex_interval": INTERVAL_BOUNDS,
    "antigravity_interval": INTERVAL_BOUNDS,
    "openrouter_interval": INTERVAL_BOUNDS,
    "deadline": DEADLINE_BOUNDS,
    "retry_base_delay": (0.0, 86400.0),
    "retry_max_delay": (0.0, 86400.0),
    "retry_backoff_factor": (1.0, 10.0),
    "retry_jitter": (0.0, 1.0),
    "pace_tolerance": (0.0, 50.0),
    "retention_days": (1, 3650),
    "history_default_hours": (1, 168),
    "history_max_hours": (1, 168),
    "history_default_limit": (1, 500),
    "history_max_limit": (1, 500),
    "response_cap_bytes": (1024, 262_144),
    "scheduler_tick_seconds": (0.1, 3600.0),
    "codex_timeout_seconds": (1, 3600),
    "antigravity_timeout_seconds": (1, 3600),
    "openrouter_timeout_seconds": (1, 3600),
}

_INT_KEYS = {
    "api_port",
    "retention_days",
    "history_default_hours",
    "history_max_hours",
    "history_default_limit",
    "history_max_limit",
    "response_cap_bytes",
    "codex_timeout_seconds",
    "antigravity_timeout_seconds",
    "openrouter_timeout_seconds",
}

_FLOAT_KEYS = {
    "codex_interval",
    "antigravity_interval",
    "openrouter_interval",
    "deadline",
    "retry_base_delay",
    "retry_max_delay",
    "retry_backoff_factor",
    "retry_jitter",
    "pace_tolerance",
    "scheduler_tick_seconds",
}


def _coerce(key: str, value: Any) -> Any:
    if key in _INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("invalid_type", f"{key} must be an integer")
        as_int = int(value)
        if as_int != value:
            raise ConfigError("invalid_type", f"{key} must be an integer")
        return as_int
    if key in _FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("invalid_type", f"{key} must be a number")
        return float(value)
    if key == "api_host":
        if not isinstance(value, str) or not value:
            raise ConfigError("invalid_type", "api_host must be a non-empty string")
        return value
    if key in (
        "data_dir",
        "account_digest_key",
        "codex_app_server_socket",
        "codex_transport",
        "codex_bin",
        "antigravity_bin",
        "openrouter_api_key",
        "openrouter_management_key",
        "openrouter_base_url",
    ):
        if not isinstance(value, str):
            raise ConfigError("invalid_type", f"{key} must be a string")
        return value
    raise ConfigError("unknown_key", f"unhandled configuration key {key!r}")


def _validate(cfg: Config) -> None:
    for name, value in vars(cfg).items():
        bounds = _BOUNDS.get(name)
        if bounds is None:
            continue
        lo, hi = bounds
        if not (lo <= value <= hi):
            raise ConfigError(
                "out_of_bounds",
                f"{name}={value!r} is outside the allowed range [{lo}, {hi}]",
            )
    if cfg.api_host != "127.0.0.1":
        raise ConfigError(
            "invalid_binding",
            f"api_host must be 127.0.0.1 (loopback-only binding); got {cfg.api_host!r}",
        )
    if cfg.history_max_hours < cfg.history_default_hours:
        raise ConfigError(
            "invalid_range",
            "history_max_hours must be >= history_default_hours",
        )
    if cfg.history_max_limit < cfg.history_default_limit:
        raise ConfigError(
            "invalid_range",
            "history_max_limit must be >= history_default_limit",
        )
    if cfg.account_digest_key and len(cfg.account_digest_key) < 16:
        raise ConfigError(
            "invalid_range",
            "account_digest_key must be at least 16 characters when set: the "
            "identities it digests are low entropy (account names, emails), so a "
            "short key could be brute-forced from a database dump",
        )
    if cfg.codex_transport not in _CODEX_TRANSPORTS:
        raise ConfigError(
            "invalid_value",
            f"codex_transport must be one of {sorted(_CODEX_TRANSPORTS)}; "
            f"got {cfg.codex_transport!r}",
        )
    if cfg.codex_transport == "stdio" and not cfg.codex_bin:
        raise ConfigError(
            "invalid_value",
            "codex_bin must be a non-empty command path when codex_transport is "
            "'stdio'",
        )


def _from_dict(raw: dict[str, Any]) -> Config:
    known = {f.name for f in fields(Config)}
    extra = set(raw) - known
    if extra:
        raise ConfigError("unknown_key", f"unknown configuration keys: {sorted(extra)}")
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        kwargs[key] = _coerce(key, value)
    cfg = Config(**kwargs)
    _validate(cfg)
    return cfg


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate configuration.

    ``path`` points to a JSON file. When omitted, defaults are returned with
    ``data_dir`` set to ``$AI_MONITOR_DATA_DIR`` when present, else ``"data"``.

    Raises:
        ConfigError: on any invalid value, unknown key, unreadable file, or
            malformed JSON.
    """
    if path is None:
        data_dir = os.environ.get("AI_MONITOR_DATA_DIR") or "data"
        return Config(data_dir=data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("not_found", f"config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigError("invalid_json", f"config file is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise ConfigError("invalid_shape", "config root must be a JSON object")
    return _from_dict(raw)
