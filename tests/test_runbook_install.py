"""Guards on the runbook's install and upgrade commands.

These commands run as root against a real host, so a mistake in them is expensive.
Two classes of mistake are guarded here, both observed rather than imagined.

``rsync -a`` of the checkout copies the repository's ``.env`` — mode 0664 here —
into a 0775 deployment directory, where every local user can read it. Rehearsed on
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


def _shell_blocks(text: str) -> list[str]:
    """Every fenced ``sh`` block in the runbook."""
    return re.findall(r"```sh\n(.*?)```", text, re.S)


def test_both_copy_steps_use_an_allowlist():
    commands = _rsync_commands(RUNBOOK.read_text("utf-8"))
    assert len(commands) == 2, f"install and upgrade should each copy: {commands}"
    for command in commands:
        for include in REQUIRED_INCLUDES:
            assert f"--include '{include}'" in command, (
                f"{include} is missing, so the deployed tree lacks it: {command}"
            )
        # Closed on both sides. The list is the allowlist, so an extra `--include`
        # is a new file entering the tree, not a harmless addition.
        assert sorted(re.findall(r"--include '([^']*)'", command)) == sorted(
            REQUIRED_INCLUDES
        ), f"the include list is not the documented allowlist: {command}"


def test_the_copy_excludes_nothing_by_name():
    """Excluding a named file is a denylist, and a denylist misses secrets."""
    for command in _rsync_commands(RUNBOOK.read_text("utf-8")):
        excludes = re.findall(r"--exclude '([^']*)'", command)
        assert excludes == ["*"], (
            f"only `--exclude '*'` keeps this fail-safe: {command}"
        )


def test_both_copy_steps_check_for_a_leaked_env():
    """Counting mentions is not enough: the check has to be in the same step.

    Two mentions could both sit in the install step, leaving the upgrade step —
    the one that runs repeatedly against a live tree — to copy `.env` back in.
    """
    blocks = [
        block
        for block in _shell_blocks(RUNBOOK.read_text("utf-8"))
        if "sudo rsync" in block
    ]
    assert len(blocks) == 2, f"install and upgrade should each copy: {len(blocks)}"
    for block in blocks:
        assert "/opt/ai-monitor/.env" in block, (
            "every copy step must confirm no .env landed:\n" + block
        )


def test_no_git_step_runs_against_the_deployed_tree_as_the_service_account():
    """`git -C /opt/ai-monitor` cannot work there, and not as that account.

    Verified on the host: the deployed tree is an allowlist copy, so it carries no
    `.git` and `git -C /opt/ai-monitor` fails with "not a git repository". It also
    cannot run as the service account, which cannot reach the operator's checkout.
    A root-run git inside an ai-monitor-owned tree trips git's dubious-ownership
    check, so every git invocation against the deployed tree names
    `safe.directory` and runs as root.
    """
    commands = [
        line.strip()
        for block in _shell_blocks(RUNBOOK.read_text("utf-8").replace("\\\n", " "))
        for line in block.splitlines()
        if line.strip()
        and not line.strip().startswith("#")
        and "git" in line
        and "/opt/ai-monitor" in line
    ]
    # The upgrade and the rollback each document the git form, two commands each.
    # A count, not a bare "at least one", so deleting one of the two forms fails.
    assert len(commands) >= 4, (
        f"the upgrade and the rollback should each document the git form: {commands}"
    )
    for command in commands:
        assert command.startswith("sudo git "), (
            f"git against the deployed tree runs as root, not as the service "
            f"account: {command}"
        )
        assert "safe.directory=/opt/ai-monitor" in command, (
            f"a root-run git inside an ai-monitor-owned tree needs "
            f"safe.directory: {command}"
        )


def test_every_in_tree_build_clears_the_stale_build_directory():
    """`pip install <dir>` and `pip wheel <dir>` reuse the `build/` they leave.

    Verified on this host: install a tree, add a module, install again, delete the
    module from the tree, then reinstall — the module was still installed, and a
    wheel built from that tree contained it, because setuptools had kept it in
    `build/lib/`. The rsync cannot clear it either, since excluded paths survive
    `--delete`.

    The clear also needs `sudo`: /opt/ai-monitor is 0775 ai-monitor:ai-monitor, so
    the operator cannot remove anything inside it. Comments are ignored, so a note
    mentioning the command cannot stand in for running it.
    """
    building = [
        block
        for block in _shell_blocks(RUNBOOK.read_text("utf-8"))
        if "pip install /opt/ai-monitor" in block or "pip wheel" in block
    ]
    assert len(building) == 5, (
        f"install, upgrade in both forms, rollback and the wheel build each build "
        f"in-tree: {len(building)}"
    )
    for block in building:
        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        builds = [
            index
            for index, line in enumerate(lines)
            if "pip install /opt/ai-monitor" in line or "pip wheel" in line
        ]
        assert builds, f"no build command found in this block:\n{block}"
        for index in builds:
            assert any(
                line == "sudo rm -rf /opt/ai-monitor/build" for line in lines[:index]
            ), (
                f"`{lines[index]}` needs a preceding "
                f"`sudo rm -rf /opt/ai-monitor/build`:\n{block}"
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
