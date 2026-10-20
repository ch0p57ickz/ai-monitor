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

The example is huJSON, so the loader below normalises it the way the admin
console does: comments and trailing commas are allowed, and nothing else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

POLICY = (
    Path(__file__).resolve().parent.parent / "deploy" / "tailnet-policy.example.hujson"
)
RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "gx10-runbook.md"

# The port the widget API is served on, and the ports zoey already serves for
# other things (surveyed in task 5.3). The policy change must not narrow those.
WIDGET_PORT = 443
#: The device routes zoey serves today. `8444` and `8445` were removed
#: intentionally on 2026-10-20 and their backends answer as Services instead, so
#: they are no longer samples of "a port that must keep the reach it has".
EXISTING_SERVED_PORTS = (3773, 8443)

OPERATOR = "papaya.ch0mp@gmail.com"
# The identity that must be denied: the tagged service nodes, which belong to the
# separate "tagged-devices" user rather than to the operator.
DENIED_IDENTITY = "tag:agent"


def _normalise_hujson(text: str) -> str:
    """Turn the huJSON example into strict JSON.

    Handles the two things huJSON allows that JSON does not: ``//`` and ``/* */``
    comments, and a trailing comma before ``}`` or ``]``. Both are recognised only
    outside strings, so a ``//`` in a URL or a ``",}"`` in a value survives.
    """
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
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and text[i + 1 : i + 2] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch == "/" and text[i + 1 : i + 2] == "*":
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text: str) -> str:
    """Remove a comma that is followed only by whitespace and a closing bracket."""
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "," and text[i + 1 :].lstrip()[:1] in ("}", "]"):
            continue
        out.append(ch)
    return "".join(out)


def _covers_port(spec: str, port: int) -> bool:
    """Whether one ``dst`` port spec includes ``port``.

    Handles the four documented forms: ``*``, ``443``, ``80,443``, ``1000-2000``.
    Anything malformed raises rather than returning a quiet ``False``, because a
    silent miss here reads as "the rule does not cover the port".
    """
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            return True
        if "-" in part:
            low, high = (int(bound) for bound in part.split("-", 1))
            assert low <= high, f"{part!r} is a reversed port range"
            if low <= port <= high:
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
    """Whether a destination reaches ``zoey`` on ``port``.

    A bare ``zoey`` is accepted alongside the alias and the address, because all
    three name the same node in a policy file. IPv6 destinations are not handled;
    the example does not use them.
    """
    return host in ("*", "zoey", zoey_ip) and _covers_port(spec, port)


def _shell_blocks(text: str) -> list[str]:
    """Every fenced ``sh`` block in a Markdown document."""
    return re.findall(r"```sh\n(.*?)```", text, re.S)


@pytest.fixture
def policy() -> dict:
    return json.loads(_normalise_hujson(POLICY.read_text("utf-8")))


@pytest.fixture
def zoey_ip(policy: dict) -> str:
    return policy["hosts"]["zoey"]


def test_hujson_comments_and_trailing_commas_are_normalised():
    """The two things huJSON allows and JSON does not, without eating data."""
    text = (
        "{\n"
        '  "a": "https://zoey.tail831c33.ts.net/", // a URL, not a comment\n'
        '  /* block */ "b": ",}",\n'
        '  "c": [1, 2,],\n'
        "}\n"
    )
    assert json.loads(_normalise_hujson(text)) == {
        "a": "https://zoey.tail831c33.ts.net/",
        "b": ",}",
        "c": [1, 2],
    }


def test_the_example_defines_hosts_and_rules(policy):
    """Non-empty, or every other test in this file would loop over nothing."""
    assert policy["hosts"]["zoey"]
    assert policy["acls"]


def test_only_the_operator_reaches_the_widget_port(policy, zoey_ip):
    """The requirement the example exists for: exclusivity on the served port.

    A positive assertion, not a scan for ``*``. Filtering by source would let a
    rule naming any other identity — a tag, a group, a second login — reach the
    widget port and still pass, which is the failure that would expose the API.
    """
    reaching = sorted(
        src
        for src, host, spec in _entries(policy)
        if _reaches(host, spec, zoey_ip, WIDGET_PORT)
    )
    assert reaching == [OPERATOR], (
        f"port {WIDGET_PORT} on zoey must be reachable by {OPERATOR} alone, but "
        f"these sources reach it: {reaching}"
    )


def test_the_operator_reaches_the_widget_port(policy, zoey_ip):
    assert any(
        src == OPERATOR and _reaches(host, spec, zoey_ip, WIDGET_PORT)
        for src, host, spec in _entries(policy)
    ), "the intended identity must be able to reach the widget API"


def test_every_other_port_stays_open_to_every_device(policy, zoey_ip):
    """The device routes zoey serves must keep the reach they have."""
    for port in EXISTING_SERVED_PORTS:
        assert any(
            src == "*" and _reaches(host, spec, zoey_ip, port)
            for src, host, spec in _entries(policy)
        ), f"port {port} would stop being reachable by every device"


def test_the_policy_asserts_the_denial_itself(policy, zoey_ip):
    """The ``tests`` block is what the console runs when the file is saved."""
    assert DENIED_IDENTITY != OPERATOR
    assert not DENIED_IDENTITY.startswith("papaya.ch0mp")
    assert policy["tests"], "an empty tests block would pass without asserting"

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
    entries = [
        entry
        for case in policy["tests"]
        for key in ("accept", "deny")
        for entry in case.get(key, [])
    ]
    assert entries, "the tests block asserts nothing, so it would pass vacuously"
    for entry in entries:
        host, _ = _split(entry)
        assert host in (zoey_ip, "zoey"), f"{entry} does not name the aliased host"


def test_the_runbook_uses_a_flag_the_installed_cli_accepts():
    """``tailscale set`` has no ``--advertise-tags``, and says so.

    Verified on this host with Tailscale 1.102.4, and again on 1.104.1:

        $ sudo tailscale set --advertise-tags=tag:zoey
        flag provided but not defined: -advertise-tags

    ``tailscale up`` is the command that takes it. The runbook said ``set``, which
    cost a round trip; this keeps the wrong form from coming back.
    """
    runbook = RUNBOOK.read_text("utf-8")
    assert "tailscale set --advertise-tags" not in runbook
    block = next(
        (b for b in _shell_blocks(runbook) if "tailscale up --advertise-tags=" in b),
        None,
    )
    assert block, "the runbook must tag the host with `tailscale up`"
    # `up` is not a merge. Its help says the flags must be the complete set of
    # desired settings, and this host's OperatorUser is non-default, so `up` exits
    # with "requires mentioning all non-default flags" unless it is named.
    assert "--operator=" in block, (
        "`up` needs every non-default setting named, or it refuses to run"
    )
    # The tag has to exist before it can be applied, or the command is rejected.
    assert block.index("tagOwners") < block.index("tailscale up --advertise-tags="), (
        "the tag definition and the tagging command belong in one step, tag first"
    )
