"""Guards on the example tailnet policy for the widget API.

The live policy lives in the Tailscale admin console, so nothing here can test
it. What this file can do is hold the example to the requirement it exists to
satisfy, because that requirement is easy to lose in an edit and would fail
open.

The tailnet's effective policy is allow-all, verified from this node with
``tailscale debug netmap``. Access rules union and the only action is ``accept``,
so a broad rule that covers the served port cannot be narrowed by adding
anything — the served port has to be excluded from the broad rule instead. An
edit that drops that exclusion, or that grants the served port to ``*``, reads
as perfectly reasonable and exposes the API to every device in the tailnet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

POLICY = (
    Path(__file__).resolve().parent.parent / "deploy" / "tailnet-policy.example.hujson"
)

# The port the widget API is served on, and the ports zoey already serves for
# other things (surveyed in task 5.3). The policy change must not narrow those.
WIDGET_PORT = 443
EXISTING_SERVED_PORTS = (3773, 8443, 8444, 8445)

OPERATOR = "papaya.ch0mp@gmail.com"
# The identity that must be denied: the tagged service nodes, which belong to the
# separate "tagged-devices" user rather than to the operator.
DENIED_IDENTITY = "tag:agent"


def _strip_comments(text: str) -> str:
    """Drop ``//`` comments without touching ``//`` inside a string."""
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif ch == "/" and text[i + 1 : i + 2] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _covers_port(spec: str, port: int) -> bool:
    """Whether one ``dst`` port spec includes ``port``.

    Handles the four documented forms: ``*``, ``443``, ``80,443``, ``1000-2000``.
    """
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            return True
        if "-" in part:
            low, high = part.split("-", 1)
            if int(low) <= port <= int(high):
                return True
        elif int(part) == port:
            return True
    return False


def _split(entry: str) -> tuple[str, str]:
    """Split ``host:ports`` into its two halves."""
    host, _, spec = entry.rpartition(":")
    return host, spec


def _entries(policy: dict) -> list[tuple[str, str, str]]:
    """Flatten the rules into ``(src, dst-host, dst-port-spec)`` triples."""
    return [
        (src, *_split(dst))
        for rule in policy["acls"]
        for dst in rule["dst"]
        for src in rule["src"]
    ]


def _reaches(host: str, spec: str, zoey_ip: str, port: int) -> bool:
    """Whether a destination reaches ``zoey`` on ``port``."""
    return host in ("*", "zoey", zoey_ip) and _covers_port(spec, port)


@pytest.fixture
def policy() -> dict:
    return json.loads(_strip_comments(POLICY.read_text("utf-8")))


@pytest.fixture
def zoey_ip(policy: dict) -> str:
    return policy["hosts"]["zoey"]


def test_comment_stripping_leaves_urls_alone():
    """A ``//`` inside a string is data, not a comment."""
    text = '{"a": "https://zoey.tail831c33.ts.net/", "b": 1} // trailing\n'
    assert json.loads(_strip_comments(text)) == {
        "a": "https://zoey.tail831c33.ts.net/",
        "b": 1,
    }


def test_no_broad_rule_reaches_the_widget_port(policy, zoey_ip):
    """The reason the example exists: ``*`` must not reach the served port."""
    for src, host, spec in _entries(policy):
        if src != "*":
            continue
        assert not _reaches(host, spec, zoey_ip, WIDGET_PORT), (
            f"a rule granting {src!r} to {host}:{spec} exposes port "
            f"{WIDGET_PORT}; the broad rule has to exclude it"
        )


def test_the_operator_reaches_the_widget_port(policy, zoey_ip):
    assert any(
        src == OPERATOR and _reaches(host, spec, zoey_ip, WIDGET_PORT)
        for src, host, spec in _entries(policy)
    ), "the intended identity must be able to reach the widget API"


def test_every_other_port_stays_open_to_every_device(policy, zoey_ip):
    """The four routes zoey already serves must keep their reach."""
    for port in EXISTING_SERVED_PORTS:
        assert any(
            src == "*" and _reaches(host, spec, zoey_ip, port)
            for src, host, spec in _entries(policy)
        ), f"port {port} would stop being reachable by every device"


def test_the_denied_identity_is_not_the_operator(policy):
    assert DENIED_IDENTITY != OPERATOR
    assert not DENIED_IDENTITY.startswith("papaya.ch0mp")


def test_the_policy_asserts_the_denial_itself(policy, zoey_ip):
    """The ``tests`` block is what the console runs when the file is saved."""
    cases = {case["src"]: case for case in policy["tests"]}

    denied = cases[DENIED_IDENTITY]
    assert any(
        host == zoey_ip and _covers_port(spec, WIDGET_PORT)
        for host, spec in (_split(entry) for entry in denied["deny"])
    ), "the disallowed device must be asserted as denied on the widget port"
    assert any(
        host == zoey_ip and _covers_port(spec, 8443)
        for host, spec in (_split(entry) for entry in denied["accept"])
    ), "the same device must be asserted as still reaching zoey's other routes"

    allowed = cases[OPERATOR]
    assert any(
        host == zoey_ip and _covers_port(spec, WIDGET_PORT)
        for host, spec in (_split(entry) for entry in allowed["accept"])
    ), "the intended device must be asserted as reaching the widget port"


def test_the_tests_and_the_host_alias_agree(policy, zoey_ip):
    """A stale alias would leave the assertions testing a different device."""
    for case in policy["tests"]:
        for key in ("accept", "deny"):
            for entry in case.get(key, []):
                host, _ = _split(entry)
                assert host == zoey_ip, f"{entry} does not name the aliased host"


def test_the_runbook_uses_a_flag_the_installed_cli_accepts():
    """``tailscale set`` has no ``--advertise-tags``, and says so.

    Verified on this host with Tailscale 1.102.4:

        $ sudo tailscale set --advertise-tags=tag:zoey
        flag provided but not defined: -advertise-tags

    ``tailscale up`` is the command that takes it. The runbook said ``set``, which
    cost a round trip; this keeps the wrong form from coming back.
    """
    runbook = (POLICY.parent.parent / "docs" / "gx10-runbook.md").read_text("utf-8")
    assert "tailscale up --advertise-tags=" in runbook
    assert "tailscale set --advertise-tags" not in runbook
    # The tag has to exist before it can be applied, or the command is rejected.
    assert "tagOwners" in runbook
