"""Guards on the shipped systemd unit.

The unit's hardening and the bundled Codex CLI have a non-obvious interaction
that is easy to reintroduce: ``ProtectSystem=strict`` mounts the entire file
system hierarchy read-only, ``$HOME`` included, but the CLI keeps its
credentials and state in ``~/.codex``.

Verified on 2026-10-19 with the real CLI: pointing ``$HOME`` at a read-only
directory makes ``codex app-server`` die without answering, and pointing it at a
fresh writable directory fails with "codex account authentication required". So
the service account's home must be writable, or the stdio transport — the
default — cannot collect at all. Losing that line would only show up as a
failing service on a deployed host, which is why it is asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "ai-monitor.service"

#: The home the unit's own comment promises to the service account.
SERVICE_HOME = "/home/ai-monitor"


@pytest.fixture(scope="module")
def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def _directive_values(text: str, directive: str) -> list[str]:
    """Every value of ``directive``, since systemd accumulates repeated keys."""
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == directive:
            values.extend(value.split())
    return values


def test_the_unit_exists(unit_text):
    assert "ExecStart=" in unit_text


def test_state_directory_is_the_service_accounts_state(unit_text):
    assert _directive_values(unit_text, "StateDirectory") == ["ai-monitor"]


def test_the_codex_home_stays_writable_under_protect_system_strict(unit_text):
    """The regression this file exists for."""
    protect_system = _directive_values(unit_text, "ProtectSystem")
    assert protect_system == ["strict"], (
        "if ProtectSystem changes, re-check whether $HOME needs to stay writable"
    )
    writable = _directive_values(unit_text, "ReadWritePaths")
    assert SERVICE_HOME in writable, (
        "ProtectSystem=strict makes $HOME read-only, and the bundled Codex CLI "
        f"cannot run without a writable ~/.codex; add {SERVICE_HOME} to "
        "ReadWritePaths="
    )
    assert "/var/lib/ai-monitor" in writable, "the database needs its state dir"


def test_credentials_are_not_passed_in_the_unit(unit_text):
    """Credentials belong in the config file, not the unit or the environment."""
    for line in unit_text.splitlines():
        if line.strip().startswith("ExecStart="):
            assert "--config" in line
        assert "API_KEY" not in line
        assert "TOKEN" not in line
        assert "SECRET" not in line


def test_the_unit_restarts_and_keeps_state(unit_text):
    assert _directive_values(unit_text, "Restart") == ["always"]
    assert _directive_values(unit_text, "Environment") == [
        "AI_MONITOR_DATA_DIR=/var/lib/ai-monitor"
    ]
