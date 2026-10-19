"""Guards on the runbook's install and upgrade copy step.

These commands run as root against a real host, so a mistake in them is
expensive. ``rsync -a`` of the checkout copies the repository's ``.env`` — mode
0664 here — into a 0755 deployment directory, where every local user can read it.
Rehearsed on 2026-10-19: the denylist form the runbook used did exactly that.

An allowlist fails safe. A secret file is copied only if it is named, so a new one
stays put; a denylist misses it silently, and the failure is invisible because the
service still works.
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
