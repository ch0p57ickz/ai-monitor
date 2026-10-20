"""Guards on the shipped example configuration.

``deploy/config.example.json`` is not documentation: the install step copies it to
``/etc/ai-monitor/config.json`` verbatim, so every value in it becomes the running
service's value. Nothing in the suite loaded it until now, which meant it could
drift from the unit and from the port Serve proxies without a test noticing. The
service would still start — on the wrong directory, or behind the wrong port, where
the only symptom is an obscure startup failure or a 502 that looks like a provider
outage.

Each guard below pairs the example with the thing it has to agree with: the loader,
the unit, the Runbook's install step, and the Runbook's Serve command.
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


def test_the_example_loads_and_binds_loopback():
    """The real loader, not a bare ``json.load``.

    The loader rejects unknown keys and non-loopback hosts, so loading here also
    proves the file has neither. A ``0.0.0.0`` in the example would not expose the
    API — startup would fail — but the operator would meet it as a confusing config
    error at the worst moment.
    """
    config = load_config(EXAMPLE)
    assert config.api_host == "127.0.0.1"
    assert config.api_port == 8732


def test_the_example_differs_from_the_defaults_only_in_deployment_paths():
    """Anything else that differs is a decision that should be recorded here.

    The spec fixes the intervals, bounds, and the response cap, and
    ``tests/test_config.py`` holds the loader's defaults to it. This asserts the
    file the operator installs is that same configuration, so an edit to one and not
    the other fails rather than shipping a different posture than the tests checked.
    """
    example = load_config(EXAMPLE)
    defaults = Config()
    differing = {
        field.name: getattr(example, field.name)
        for field in dataclasses.fields(Config)
        if getattr(example, field.name) != getattr(defaults, field.name)
    }
    assert differing == DEPLOYMENT_PATHS


def test_the_example_carries_no_credentials():
    """It is committed, so anything in it is in every install and every clone.

    The install step copies the file as-is; leaving a real key here would put it on
    the deployed host at 0640 root:ai-monitor and in git history at once.
    """
    config = load_config(EXAMPLE)
    assert config.openrouter_api_key == ""
    assert config.openrouter_management_key == ""
    assert config.account_digest_key == ""


def test_the_state_directory_is_the_one_the_unit_grants(unit_text):
    """``ProtectSystem=strict`` makes everything writable-by-exception.

    A ``data_dir`` outside ``ReadWritePaths`` fails at startup with a permission
    error that names the database, not the unit line that caused it. The unit also
    pins the same value through ``AI_MONITOR_DATA_DIR``, so both have to agree with
    the file.
    """
    data_dir = load_config(EXAMPLE).data_dir
    assert data_dir in _directive_values(unit_text, "ReadWritePaths")
    assert f"AI_MONITOR_DATA_DIR={data_dir}" in _directive_values(
        unit_text, "Environment"
    )


def test_the_unit_reads_the_config_the_install_writes(unit_text, runbook_text):
    """One path, named in three places that are edited independently."""
    exec_start = _directive_texts(unit_text, "ExecStart")
    assert len(exec_start) == 1, "the unit should have exactly one ExecStart"
    args = exec_start[0].split()
    assert "--config" in args, "the unit must pass its config path explicitly"
    installed = args[args.index("--config") + 1]
    assert installed == CONFIG_PATH
    assert f"deploy/config.example.json {CONFIG_PATH}" in runbook_text, (
        "the runbook's install step must write the path the unit reads"
    )


def test_the_example_port_is_the_one_serve_proxies(runbook_text):
    """A drift here is invisible until a client gets a 502.

    Serve keeps answering, the journal shows a healthy service, and the only
    symptom is that the widget has no data.
    """
    proxied = set(
        re.findall(
            r"tailscale serve [^\n]*?127\.0\.0\.1:(\d+)",
            runbook_text,
        )
    )
    assert proxied, "no Serve command proxies to a literal loopback port"
    assert proxied == {str(load_config(EXAMPLE).api_port)}, (
        f"Serve proxies to {sorted(proxied)}; the example listens on "
        f"{load_config(EXAMPLE).api_port}"
    )


def test_the_example_uses_the_transport_the_deployment_can_run(unit_text):
    """stdio is deliberate; the socket route is closed on this host.

    AppArmor blocks ``bubblewrap`` here, so the daemon the socket belongs to cannot
    start. The unit sets no transport override, which makes the config file the only
    place this is decided — and the only place a future edit would change it.
    """
    assert load_config(EXAMPLE).codex_transport == "stdio"
    overrides = _directive_values(unit_text, "Environment")
    assert not any("TRANSPORT" in value for value in overrides), (
        "the unit must not shadow the configured transport"
    )
