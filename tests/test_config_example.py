"""Guards on the shipped example configuration.

``deploy/config.example.json`` is not documentation: the install step copies it to
``/etc/ai-monitor/config.json`` verbatim, so every value in it becomes the running
service's value.

``tests/test_backup_restore_check.py`` already loads it through ``Config(**json)``
and checks loopback, the transport, the empty credentials, and that every field is
present. That constructor does not validate values, and it cannot compare the file
against the unit. The tests here cover the four things that span two files which are
edited independently:

- the file survives the **validating** loader the service actually runs;
- it differs from the loader's defaults only in the three deployment paths;
- its state directory is the one ``ProtectSystem=strict`` grants the unit;
- its port is the one Serve proxies to, at the path the unit reads.

A drift in any of these starts a service that looks healthy and answers nothing
useful, which is why none of them is left to review.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from ai_monitor.config import Config, load_config

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "deploy" / "config.example.json"
UNIT = REPO / "deploy" / "ai-monitor.service"
RUNBOOK = REPO / "docs" / "gx10-runbook.md"

#: Where the install step puts the example, and what the unit reads.
CONFIG_PATH = "/etc/ai-monitor/config.json"

#: The fields the example is allowed to differ from the loader's defaults on.
#: Each is a path that belongs to the deployed host, not a policy choice.
DEPLOYMENT_PATHS = {
    "data_dir": "/var/lib/ai-monitor",
    "codex_app_server_socket": (
        "/home/ai-monitor/.codex/app-server-control/app-server-control.sock"
    ),
    "antigravity_bin": "/home/ai-monitor/.local/bin/agy",
}


@pytest.fixture(scope="module")
def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


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
        value = value.split("#", 1)[0]
        values.extend(value.split())
    return values


def _directive_texts(text: str, directive: str) -> list[str]:
    """Whole values, for directives whose argument list matters."""
    texts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != directive:
            continue
        texts.append(value.split("#", 1)[0].strip())
    return texts


def _shell_blocks(text: str) -> list[str]:
    """The contents of every fenced block, without the fences."""
    blocks: list[str] = []
    current: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("```"):
            if inside:
                blocks.append("\n".join(current))
                current = []
            inside = not inside
            continue
        if inside:
            current.append(line)
    return blocks


def test_the_example_loads_through_the_validating_loader():
    """``Config(**json)`` skips validation; this is the loader the service runs.

    It rejects unknown keys and range-checks every value, so loading here fails for
    the reasons ``systemctl start`` would fail. It also carries the two values the
    deployment depends on: loopback binding, which the loader refuses to relax, and
    the port Serve proxies to.
    """
    config = load_config(EXAMPLE)
    assert config.api_host == "127.0.0.1"
    assert config.api_port == 8732


def test_the_example_differs_from_the_defaults_only_in_deployment_paths():
    """Anything else that differs is a decision that should be recorded here.

    The spec fixes the intervals, bounds, and the response cap, and
    ``tests/test_config.py`` holds the loader's defaults to it. This asserts the file
    the operator installs is that same configuration, so an edit to one and not the
    other fails rather than shipping a posture the tests never checked.

    Field *names* are compared, not values: a credential accidentally committed to
    the example must not be echoed into test output.
    """
    example = load_config(EXAMPLE)
    defaults = Config()
    differing = {
        field.name
        for field in dataclasses.fields(Config)
        if getattr(example, field.name) != getattr(defaults, field.name)
    }
    unexpected = differing ^ set(DEPLOYMENT_PATHS)
    assert not unexpected, (
        "the example must be the loader's defaults plus the deployment paths; "
        f"these fields differ unexpectedly: {sorted(unexpected)}"
    )
    for name, expected in DEPLOYMENT_PATHS.items():
        assert getattr(example, name) == expected, f"{name} should be {expected!r}"


def test_the_state_directory_is_the_one_the_unit_grants(unit_text):
    """``ProtectSystem=strict`` makes everything writable-by-exception.

    A ``data_dir`` outside ``ReadWritePaths`` fails at startup with a permission
    error that names the database, not the unit line that caused it. The unit also
    pins the same value through ``AI_MONITOR_DATA_DIR``, and
    ``tests/test_deploy_unit.py`` holds that to the literal this dict names.
    """
    data_dir = load_config(EXAMPLE).data_dir
    writable = _directive_values(unit_text, "ReadWritePaths")
    assert data_dir in writable, (
        f"{data_dir} must be one of the unit's ReadWritePaths, or "
        f"ProtectSystem=strict makes the database unwritable; the unit grants "
        f"{writable}"
    )


def test_the_unit_reads_the_config_the_install_writes(unit_text, runbook_text):
    """One path, named in three places that are edited independently.

    Only ``ExecStart`` is parsed; an ``ExecStartPre`` that also named a config would
    not be covered here.
    """
    exec_start = _directive_texts(unit_text, "ExecStart")
    assert len(exec_start) == 1, "the unit should have exactly one ExecStart"
    args = exec_start[0].split()
    installed = None
    for index, arg in enumerate(args):
        if arg == "--config" and index + 1 < len(args):
            installed = args[index + 1]
        elif arg.startswith("--config="):
            installed = arg.split("=", 1)[1]
    assert installed == CONFIG_PATH, (
        f"the unit must pass its config path explicitly; it passes {installed!r}"
    )

    # Join continuations first: the install command may be wrapped, and a wrapped
    # command would otherwise look like a block with no install line in it.
    blocks = [
        block
        for block in _shell_blocks(runbook_text.replace("\\\n", " "))
        if any(
            line.strip().startswith("sudo install") and "config.example.json" in line
            for line in block.splitlines()
        )
    ]
    assert len(blocks) == 1, (
        f"the runbook should install the example in exactly one step: {len(blocks)}"
    )
    install = next(
        line.strip()
        for line in blocks[0].splitlines()
        if "config.example.json" in line and line.strip().startswith("sudo install")
    )
    assert install.split()[-1] == CONFIG_PATH, (
        f"the install step must write the path the unit reads ({CONFIG_PATH}): "
        f"{install}"
    )


def test_the_example_port_is_the_one_serve_proxies(runbook_text):
    """A drift here is invisible until the widget has no data.

    Serve keeps answering, the journal shows a healthy service, and the only symptom
    is a request that reaches nothing. The scan is limited to this service's own
    commands — the runbook also documents routes for other services — and tolerates
    a wrapped command, since the runbook wraps long lines.
    """
    lines = runbook_text.replace("\\\n", " ").splitlines()
    commands = [
        f"{line} {following}"
        for line, following in zip(lines, lines[1:])
        if "tailscale serve" in line
        and ("svc:ai-monitor" in line or "--https=443" in line)
    ]
    assert len(commands) >= 2, (
        "the runbook should document the deployed and fallback Serve commands; "
        f"found {len(commands)}"
    )
    proxied: set[str] = set()
    for command in commands:
        proxied.update(re.findall(r"127\.0\.0\.1:(\d+)", command))
    assert proxied == {str(load_config(EXAMPLE).api_port)}, (
        f"Serve proxies to {sorted(proxied)}; the example listens on "
        f"{load_config(EXAMPLE).api_port}"
    )
