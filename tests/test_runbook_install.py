"""Guards on the runbook's install and upgrade commands.

These commands run as root against a real host, so a mistake in them is expensive.
Two classes of mistake are guarded here, both observed rather than imagined.

``rsync -a`` of the checkout copies the repository's ``.env`` — mode 0664 here —
into a 0755 deployment directory, where every local user can read it. Rehearsed on
2026-10-19, and then seen on the host: the deployed tree carries a world-readable
``.env``. An allowlist fails safe; a denylist misses the file that matters, and the
failure stays invisible because the service still works.

A precondition check that runs as the wrong user passes vacuously. ``sudo test -r``
runs as root and says yes regardless of what the service account can actually read.
"""

from __future__ import annotations

import re
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "gx10-runbook.md"

# What the deployed tree must contain: the package and its build metadata for
# `pip install`, plus the paths the unit and the runbook reference by name.
# `venv/` is not here because the venv step creates it, and the copy protects it.
REQUIRED_INCLUDES = (
    "/ai_monitor/***",
    "/deploy/***",
    "/docs/***",
    "/tools/***",
    "/pyproject.toml",
)


def _rsync_commands(text: str) -> list[str]:
    """Every ``sudo rsync`` command in the runbook, with continuations joined."""
    joined = text.replace("\\\n", " ")
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip().startswith("sudo rsync")
    ]


def test_both_copy_steps_use_an_allowlist():
    commands = _rsync_commands(RUNBOOK.read_text("utf-8"))
    assert len(commands) == 2, f"install and upgrade should each copy: {commands}"
    for command in commands:
        for include in REQUIRED_INCLUDES:
            assert f"--include '{include}'" in command, (
                f"{include} is missing, so the deployed tree lacks it: {command}"
            )


def test_the_copy_excludes_nothing_by_name():
    """Excluding a named file is a denylist, and a denylist misses secrets."""
    for command in _rsync_commands(RUNBOOK.read_text("utf-8")):
        excludes = re.findall(r"--exclude '([^']*)'", command)
        assert excludes == ["*"], (
            f"only `--exclude '*'` keeps this fail-safe: {command}"
        )


def test_both_copy_steps_check_for_a_leaked_env():
    text = RUNBOOK.read_text("utf-8")
    assert text.count("/opt/ai-monitor/.env") >= 2, (
        "the install and the upgrade step should each confirm no .env landed"
    )


def test_the_config_check_runs_as_the_service_account():
    """A root check passes regardless of what the service will see.

    The service runs as ``ai-monitor``. ``sudo test -r`` runs as root and would have
    said the config was readable on this host while the unit was failing with
    "config error: config file not found".
    """
    text = RUNBOOK.read_text("utf-8")
    assert "sudo -u ai-monitor test -r /etc/ai-monitor/config.json" in text
    assert "sudo test -r /etc/ai-monitor/config.json" not in text, (
        "reading the config as root proves nothing about the service account"
    )


def test_the_precondition_check_covers_every_observed_failure():
    """Each check corresponds to a failure that happened here, silently."""
    text = RUNBOOK.read_text("utf-8")
    for check, failure in (
        ("sudo test -x /opt/ai-monitor/venv/bin/ai-monitor", "status=203/EXEC"),
        ("sudo -u ai-monitor test -r /etc/ai-monitor/config.json", "config not found"),
        ('sudo test ! -e /opt/ai-monitor/.env', "credentials in the tree"),
    ):
        assert check in text, f"no precondition check for {failure}: {check}"
    # The home check is written across a line continuation, so match its parts.
    assert "sudo -u ai-monitor test -w" in text
    assert "getent passwd ai-monitor" in text
