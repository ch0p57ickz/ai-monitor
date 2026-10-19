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
        if key.strip() != directive:
            continue
        # Drop a trailing inline comment, so `ProtectSystem=strict  # keep`
        # does not read as three values.
        value = value.split("#", 1)[0]
        values.extend(value.split())
    return values


def test_the_unit_exists(unit_text):
    exec_lines = _directive_values(unit_text, "ExecStart")
    assert exec_lines, "the unit needs ExecStart="
    assert any("--config" in value for value in exec_lines)


def test_state_directory_is_the_service_accounts_state(unit_text):
    assert _directive_values(unit_text, "StateDirectory") == ["ai-monitor"]


def test_the_codex_home_stays_writable_under_protect_system_strict(unit_text):
    """The regression this file exists for."""
    protect_system = _directive_values(unit_text, "ProtectSystem")
    assert protect_system == ["strict"], (
        "if ProtectSystem changes, re-check whether $HOME needs to stay writable"
    )
    # ProtectHome=true makes /home inaccessible, and ReadWritePaths= cannot be
    # nested inside InaccessiblePaths=, so the home would break again by a
    # different route.
    assert _directive_values(unit_text, "ProtectHome") == ["false"], (
        "ProtectHome=true hides /home, which ReadWritePaths= cannot override"
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
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"):
            assert marker not in stripped, f"{marker} must not appear in the unit"


def test_the_unit_restarts_and_keeps_state(unit_text):
    assert _directive_values(unit_text, "Restart") == ["always"]
    assert _directive_values(unit_text, "Environment") == [
        "AI_MONITOR_DATA_DIR=/var/lib/ai-monitor"
    ]


def test_a_start_that_cannot_succeed_is_given_up_on(unit_text):
    """``Restart=always`` must not mean "restart forever without saying so".

    Observed on this host: an empty /opt/ai-monitor left ExecStart unexecutable,
    and the unit reached 2141 restarts of status=203/EXEC while ``is-enabled``
    still read "enabled" and ``enable --now`` had returned success. A start limit
    turns that silent loop into a reported failure. The directives belong in
    [Unit], not [Service], on systemd 255.
    """
    unit_section, _, service_section = unit_text.partition("[Service]")
    assert _directive_values(unit_section, "StartLimitBurst") == ["10"]
    assert _directive_values(unit_section, "StartLimitIntervalSec") == ["300"]
    assert _directive_values(service_section, "StartLimitBurst") == [], (
        "StartLimitBurst in [Service] is not the directive systemd reads here"
    )


def test_the_install_step_verifies_the_unit_actually_started():
    """`enable --now` reports success even when the unit fails immediately."""
    runbook = (UNIT.parent.parent / "docs" / "gx10-runbook.md").read_text("utf-8")
    assert "systemctl is-active ai-monitor" in runbook
    assert "systemd-analyze verify" in runbook
    assert "reset-failed" in runbook, (
        "a tripped start limit needs the counter cleared before a fresh start"
    )


def test_credentials_file_mode_matches_the_runbook(unit_text):
    """The unit's comment and the install step must agree on the mode."""
    runbook = (UNIT.parent.parent / "docs" / "gx10-runbook.md").read_text("utf-8")
    assert "install -m 0640 -o root -g ai-monitor" in runbook, (
        "the unit comment promises a 0640 root:ai-monitor config file"
    )
    assert "0640 config file owned by root" in unit_text
