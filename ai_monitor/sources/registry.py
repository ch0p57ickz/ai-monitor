"""Provider source adapters: abstract base and concrete implementations.

Each adapter fetches raw provider data for one source and returns a dict.
The state worker normalizes that dict via ``models.normalize_observation``.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Config


class CollectionError(Exception):
    """A collector-level error with a spec error category."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class SourceAdapter(ABC):
    """Base class for per-source data collection."""

    @abstractmethod
    def fetch(self, config: "Config") -> dict[str, Any]:
        """Fetch raw provider data.

        Returns a raw dict on success. Raises ``CollectionError`` on failure.
        """
        raise NotImplementedError


class UnavailableAdapter(SourceAdapter):
    """Placeholder adapter for sources not yet wired."""

    def fetch(self, config: "Config") -> dict[str, Any]:
        raise CollectionError("unavailable", "source adapter not implemented")


# ---------------------------------------------------------------------------
# Codex (JSON-RPC over the app-server unix socket)
# ---------------------------------------------------------------------------


class CodexAdapter(SourceAdapter):
    def fetch(self, config: "Config") -> dict[str, Any]:
        if not config.codex_app_server_socket:
            raise CollectionError("unavailable", "codex app-server socket not configured or found")

        payload = {
            "jsonrpc": "2.0",
            "id": "usage-1",
            "method": "account/get",
            "params": {},
        }

        import socket

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(config.codex_timeout_seconds)
            sock.connect(config.codex_app_server_socket)
            sock.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            sock.close()
            response = json.loads(data.split(b"\n", 1)[0])
        except FileNotFoundError:
            raise CollectionError("unavailable", "codex app-server socket missing")
        except TimeoutError:
            raise CollectionError("timeout", "codex app-server did not respond in time")
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectionError("unknown", f"codex app-server error: {exc}")

        if "error" in response:
            raise CollectionError("unknown", response["error"].get("message", "rpc error"))
        return response.get("result", {})


# ---------------------------------------------------------------------------
# Antigravity (agy CLI, session-scoped)
# ---------------------------------------------------------------------------


class AntigravityAdapter(SourceAdapter):
    def fetch(self, config: "Config") -> dict[str, Any]:
        if not config.antigravity_bin:
            raise CollectionError("unavailable", "agy binary not found")
        try:
            proc = subprocess.run(
                [config.antigravity_bin, "usage", "--json"],
                capture_output=True,
                text=True,
                timeout=config.antigravity_timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            raise CollectionError("unavailable", "agy binary not found")
        except subprocess.TimeoutExpired:
            raise CollectionError("timeout", "agy usage timed out")

        if proc.returncode != 0:
            raise CollectionError("unavailable", proc.stderr.strip()[:200])
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CollectionError("unknown", f"agy output not JSON: {exc}")


# ---------------------------------------------------------------------------
# OpenRouter (account summary + per-key)
# ---------------------------------------------------------------------------


def _openrouter_request(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise CollectionError("authentication", "openrouter authentication failed")
        if exc.code == 429:
            raise CollectionError("rate_limited", "openrouter rate limit")
        raise CollectionError("unknown", f"openrouter HTTP {exc.code}")
    except urllib.error.URLError as exc:
        raise CollectionError("network", str(exc.reason))
    except TimeoutError:
        raise CollectionError("timeout", "openrouter request timed out")


class OpenRouterAccountAdapter(SourceAdapter):
    def fetch(self, config: "Config") -> dict[str, Any]:
        if not config.openrouter_api_key:
            raise CollectionError("unavailable", "openrouter api key not configured")
        return _openrouter_request(
            config.openrouter_base_url.rstrip("/") + "/api/v1/auth/key",
            {"Authorization": f"Bearer {config.openrouter_api_key}"},
            config.openrouter_timeout_seconds,
        )


class OpenRouterKeyAdapter(SourceAdapter):
    def fetch(self, config: "Config") -> dict[str, Any]:
        if not config.openrouter_api_key:
            raise CollectionError("unavailable", "openrouter api key not configured")
        return _openrouter_request(
            config.openrouter_base_url.rstrip("/") + "/api/v1/credits",
            {"Authorization": f"Bearer {config.openrouter_api_key}"},
            config.openrouter_timeout_seconds,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def default_registry() -> Dict[str, type[SourceAdapter]]:
    return {
        "codex": CodexAdapter,
        "antigravity": AntigravityAdapter,
        "openrouter-account": OpenRouterAccountAdapter,
        "openrouter-key": OpenRouterKeyAdapter,
    }


def build_registry(
    overrides: Dict[str, type[SourceAdapter]] | None = None,
) -> Dict[str, type[SourceAdapter]]:
    reg = default_registry()
    if overrides:
        reg.update(overrides)
    return reg
