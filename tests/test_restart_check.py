"""Guards on the restart-preservation check (task 5.2).

The criterion passes wrongly in two independent ways, so the tests assert both
failures, the pass, and the CLI contract that separates a skip from a success:

- a fresh poll satisfies "does the API serve data?" even when nothing was retained,
  because the worker polls as soon as it starts — the main PID must have changed;
- a changed PID satisfies "did it restart?" even when the database lost everything —
  the captured timestamps must still be served.

Every remaining way to make the PID half true without a restart is a false pass, and
each one was live before this file covered it: `systemctl show <no-such-unit>` exits 0
and prints `MainPID=0`; a state file can be empty, malformed, or carry a PID that can
never equal a live one; it can be captured from another host, unit, or URL; it can be
too old, or dated in the future, for the age guard to mean anything; and its
permissions decide whether anyone else could have edited the verdict. The tests assert
exit 2, not exit 0, for all of them.

The tool is end-to-end against a running unit, so the injection here is at the two
boundaries it has: the API fetch and `systemctl`.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "gx10-runbook.md"
URL = "http://127.0.0.1:8732"


@pytest.fixture(scope="module")
def check():
    """Load the tool as a module without running its entry point."""
    spec = importlib.util.spec_from_file_location(
        "restart_check", REPO / "tools" / "restart_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner(pid: str, returncode: int = 0):
    """A stand-in for `subprocess.run` that answers `systemctl show`."""

    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=returncode,
            stdout=f"MainPID={pid}\n" if returncode == 0 else "",
            stderr="" if returncode == 0 else "unit not found",
        )

    return run


def _fetch(usage: dict | None = None, history: dict | None = None):
    """A stand-in for the API fetch, dispatching on the path it is called with."""

    def fetch(url: str):
        if "/history" in url:
            if history is None:
                raise AssertionError(f"unexpected history call: {url}")
            return history
        if usage is None:
            raise AssertionError(f"unexpected usage call: {url}")
        return usage

    return fetch


def _usage(*timestamps: str) -> dict:
    return {"observations": [{"collectedAt": value} for value in timestamps]}


def _history(*timestamps: str, truncated: bool = False) -> dict:
    return {
        "observations": [{"collectedAt": value} for value in timestamps],
        "truncated": truncated,
    }


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


BEFORE = ("2026-10-19T18:00:00Z", "2026-10-19T18:00:01Z")


def _state(pid: str = "1000", **overrides) -> dict:
    """A state file captured a moment ago, on this host."""
    state = {
        "host": socket.gethostname(),
        "unit": "ai-monitor",
        "url": URL,
        "pid": pid,
        "capturedAt": _ago(0),
        "timestamps": list(BEFORE),
    }
    state.update(overrides)
    return state


class TestServedTimestamps:
    def test_reads_every_observation(self, check):
        assert check.served_timestamps(_usage("b", "a")) == ["a", "b"]

    def test_ignores_rows_without_a_timestamp(self, check):
        assert check.served_timestamps({"observations": [{"id": "codex"}, "junk"]}) == []

    def test_a_payload_without_observations_is_empty(self, check):
        assert check.served_timestamps({}) == []


class TestParseTimestamp:
    def test_a_z_suffix_is_utc(self, check):
        assert check._parse_timestamp("2026-10-19T18:00:00Z").isoformat() == (
            "2026-10-19T18:00:00+00:00"
        )

    def test_a_missing_zone_is_read_as_utc(self, check):
        assert check._parse_timestamp("2026-10-19T18:00:00").tzinfo == timezone.utc

    def test_a_bogus_value_raises(self, check):
        with pytest.raises(ValueError):
            check._parse_timestamp("yesterday")


class TestMainPid:
    def test_it_reads_the_pid(self, check):
        assert check.main_pid("ai-monitor", run=_runner("1000")) == "1000"

    def test_a_unit_that_matches_nothing_is_not_pid_zero(self, check):
        """`systemctl show <typo> -p MainPID` exits 0 with MainPID=0.

        Treating that zero as a PID made a mistyped `--unit` look like a restart,
        which turned the check into a pass for the case it exists to catch.
        """
        with pytest.raises(RuntimeError, match="not running"):
            check.main_pid("ai_monitor_typo", run=_runner("0"))

    @pytest.mark.parametrize("value", ["", "not-a-pid", "-1", "1.5"])
    def test_a_pid_that_is_not_a_pid_is_not_a_restart(self, check, value):
        with pytest.raises(RuntimeError, match="not running"):
            check.main_pid("ai-monitor", run=_runner(value))

    def test_a_failed_systemctl_call_raises(self, check):
        with pytest.raises(RuntimeError, match="systemctl show"):
            check.main_pid("ai-monitor", run=_runner("", returncode=1))


class TestStateValidation:
    """A state file that cannot prove anything must not pass."""

    @pytest.mark.parametrize(
        "state",
        [
            {},  # empty
            [],  # not an object
            [1, 2],
            "text",
            None,
            _state(pid=""),
            _state(pid="0"),
            _state(pid="0000"),
            _state(pid="not-a-pid"),
            _state(pid="1000x"),
            _state(pid=1000),
            _state(timestamps=[]),
            _state(timestamps="not a list"),
            _state(timestamps=[None]),
            _state(timestamps=["", "x"]),
            _state(host=""),
            _state(unit=""),
            _state(url=""),
            _state(capturedAt=""),
        ],
    )
    def test_it_rejects_a_state_file_that_cannot_prove_anything(self, check, state):
        with pytest.raises(ValueError):
            check._validate_state(state)

    def test_a_usable_state_passes_validation(self, check):
        assert check._validate_state(_state())["pid"] == "1000"


class TestCapture:
    def test_records_the_host_the_pid_and_the_timestamps(self, check):
        state = check.capture(
            "ai-monitor",
            URL,
            fetch=_fetch(usage=_usage(*BEFORE)),
            run=_runner("1000"),
        )
        assert state["host"] == socket.gethostname()
        assert state["unit"] == "ai-monitor"
        assert state["url"] == URL
        assert state["pid"] == "1000"
        assert state["timestamps"] == sorted(BEFORE)
        # Parses, and is recent, so a later `--verify` is not refused as stale.
        age = (datetime.now(timezone.utc) - check._parse_timestamp(state["capturedAt"])).total_seconds()
        assert 0 <= age < 60

    def test_it_normalises_a_trailing_slash(self, check):
        """`--url http://host:8732/` would otherwise build `//api/v1/usage`."""
        state = check.capture(
            "ai-monitor",
            f"{URL}/",
            fetch=_fetch(usage=_usage(*BEFORE)),
            run=_runner("1000"),
        )
        assert state["url"] == URL

    def test_it_refuses_to_capture_nothing(self, check):
        """An empty capture would make `--verify` pass on anything."""
        with pytest.raises(RuntimeError, match="nothing to verify"):
            check.capture(
                "ai-monitor",
                URL,
                fetch=_fetch(usage=_usage()),
                run=_runner("1000"),
            )

    def test_a_failed_systemctl_call_is_not_a_state_file(self, check):
        with pytest.raises(RuntimeError, match="systemctl show"):
            check.capture(
                "ai-monitor",
                URL,
                fetch=_fetch(usage=_usage(*BEFORE)),
                run=_runner("", returncode=1),
            )


class TestStateFilePermissions:
    def test_it_writes_the_state_file_mode_600(self, check, tmp_path):
        path = tmp_path / "state.json"
        check._write_state_file(path, _state())
        assert path.stat().st_mode & 0o777 == 0o600

    def test_it_writes_json_that_validates(self, check, tmp_path):
        path = tmp_path / "state.json"
        check._write_state_file(path, _state())
        assert check._validate_state(json.loads(path.read_text()))["pid"] == "1000"

    def test_a_group_writable_state_file_is_refused(self, check, tmp_path):
        """A verdict read from a file anyone else can edit is not a verdict."""
        path = tmp_path / "state.json"
        check._write_state_file(path, _state())
        path.chmod(0o666)
        with pytest.raises(RuntimeError, match="writable by group or others"):
            check._read_state_file(path)

    def test_a_symlinked_state_file_is_refused(self, check, tmp_path):
        real = tmp_path / "real.json"
        check._write_state_file(real, _state())
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(OSError):
            check._read_state_file(link)

    def test_a_state_file_owned_by_someone_else_is_refused(self, check, tmp_path, monkeypatch):
        path = tmp_path / "foreign.json"
        check._write_state_file(path, _state())
        monkeypatch.setattr(check.os, "geteuid", lambda: 99999)
        with pytest.raises(RuntimeError, match="not by you"):
            check._read_state_file(path)

    def test_a_failed_write_does_not_leave_a_usable_state_file(
        self, check, tmp_path, monkeypatch
    ):
        """`O_TRUNC` runs first, so a failed capture must not leave a valid file."""

        def boom(fd, mode):
            raise PermissionError("EPERM")

        path = tmp_path / "state.json"
        check._write_state_file(path, _state())  # a valid capture already exists
        monkeypatch.setattr(check.os, "fchmod", boom)
        with pytest.raises(OSError):
            check._write_state_file(path, _state())
        monkeypatch.undo()
        # Whatever it left behind is refused, not read as the new capture.
        with pytest.raises(json.JSONDecodeError):
            check._validate_state(json.loads(check._read_state_file(path)))


class TestVerify:
    def test_passes_with_a_new_pid_and_the_same_rows(self, check):
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            URL,
            fetch=_fetch(history=_history(*BEFORE, "2026-10-19T18:01:00Z")),
            run=_runner("2000"),
        )
        assert ok, report
        assert "1000 -> 2000" in report
        assert "2 captured, 2 still served" in report

    def test_it_fails_when_the_unit_did_not_restart(self, check):
        """The fresh-poll trap: the rows are served, and it proves nothing."""
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            URL,
            fetch=_fetch(history=_history(*BEFORE)),
            run=_runner("1000"),
        )
        assert not ok
        assert "main PID did not change" in report

    def test_it_fails_when_a_captured_row_is_gone(self, check):
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            URL,
            fetch=_fetch(history=_history(BEFORE[0], "2026-10-19T18:01:00Z")),
            run=_runner("2000"),
        )
        assert not ok
        assert "no longer served" in report
        assert BEFORE[1] in report

    def test_both_failures_are_reported_together(self, check):
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            URL,
            fetch=_fetch(history=_history()),
            run=_runner("1000"),
        )
        assert not ok
        assert "main PID did not change" in report
        assert "no longer served" in report

    def test_an_untruncated_response_makes_an_absence_a_loss(self, check):
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            URL,
            fetch=_fetch(history=_history()),
            run=_runner("2000"),
        )
        assert not ok
        assert "not truncated" in report

    def test_a_truncated_response_cannot_tell_a_loss_from_the_window(self, check):
        """Past the byte cap, absence proves nothing either way, so it is exit 2."""
        with pytest.raises(RuntimeError, match="truncated"):
            check.verify(
                _state(),
                "ai-monitor",
                URL,
                fetch=_fetch(history=_history("2026-10-19T18:01:00Z", truncated=True)),
                run=_runner("2000"),
            )

    def test_a_truncated_response_with_nothing_missing_still_passes(self, check):
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            URL,
            fetch=_fetch(history=_history(*BEFORE, truncated=True)),
            run=_runner("2000"),
        )
        assert ok, report

    def test_a_state_file_from_another_host_cannot_prove_this_one(self, check):
        with pytest.raises(RuntimeError, match="captured on"):
            check.verify(
                _state(host="somewhere-else"),
                "ai-monitor",
                URL,
                fetch=_fetch(history=_history(*BEFORE)),
                run=_runner("2000"),
            )

    def test_a_state_file_from_another_unit_cannot_prove_this_one(self, check):
        with pytest.raises(RuntimeError, match="captured on"):
            check.verify(
                _state(unit="other-unit"),
                "ai-monitor",
                URL,
                fetch=_fetch(history=_history(*BEFORE)),
                run=_runner("2000"),
            )

    def test_a_state_file_from_another_url_cannot_prove_this_one(self, check):
        with pytest.raises(RuntimeError, match="captured on"):
            check.verify(
                _state(url="http://127.0.0.1:9999"),
                "ai-monitor",
                URL,
                fetch=_fetch(history=_history(*BEFORE)),
                run=_runner("2000"),
            )

    def test_a_trailing_slash_is_not_a_mismatch(self, check):
        ok, report = check.verify(
            _state(),
            "ai-monitor",
            f"{URL}/",
            fetch=_fetch(history=_history(*BEFORE)),
            run=_runner("2000"),
        )
        assert ok, report

    def test_a_stale_capture_is_refused_rather_than_guessed(self, check):
        with pytest.raises(RuntimeError, match="minutes old"):
            check.verify(
                _state(capturedAt=_ago(45)),
                "ai-monitor",
                URL,
                fetch=_fetch(history=_history(*BEFORE)),
                run=_runner("2000"),
            )

    def test_a_future_capture_time_cannot_disable_the_age_guard(self, check):
        """A negative age is never "too old", so it must be refused outright."""
        with pytest.raises(RuntimeError, match="in the future"):
            check.verify(
                _state(capturedAt=_ago(-120)),
                "ai-monitor",
                URL,
                fetch=_fetch(history=_history(*BEFORE)),
                run=_runner("2000"),
            )


class TestCli:
    @pytest.fixture
    def wired(self, check, monkeypatch, tmp_path):
        """Point the CLI at fakes and a temporary state file."""
        state = tmp_path / "state.json"
        monkeypatch.setattr(
            check, "_get_json", _fetch(usage=_usage(*BEFORE), history=_history(*BEFORE))
        )
        # The PID moves when the test says the restart happened.
        holder = {"pid": "2000"}

        def run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=f"MainPID={holder['pid']}\n",
                stderr="",
            )

        monkeypatch.setattr(check.subprocess, "run", run)
        return check, state, holder

    def test_capture_then_verify_round_trip(self, wired, capsys):
        check, state, holder = wired
        assert check.main(["--capture", "--state", str(state)]) == 0
        captured = json.loads(state.read_text(encoding="utf-8"))
        assert captured["timestamps"] == sorted(BEFORE)
        assert captured["pid"] == "2000"
        assert captured["url"] == check.DEFAULT_URL
        assert state.stat().st_mode & 0o777 == 0o600

        holder["pid"] = "3000"  # the restart
        assert check.main(["--verify", "--state", str(state)]) == 0
        assert "ok: a new process" in capsys.readouterr().out

    def test_verify_fails_on_the_same_process(self, wired, capsys):
        check, state, _ = wired
        assert check.main(["--capture", "--state", str(state)]) == 0
        assert check.main(["--verify", "--state", str(state)]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_a_mistyped_unit_is_not_a_pass(self, wired, capsys):
        """`MainPID=0` for a name that matches nothing reached exit 0 before."""
        check, state, holder = wired
        assert check.main(["--capture", "--state", str(state)]) == 0
        holder["pid"] = "0"
        assert check.main(["--verify", "--state", str(state)]) == 2
        assert "not running" in capsys.readouterr().err

    def test_a_unit_change_is_not_a_pass(self, wired, capsys):
        check, state, _ = wired
        assert check.main(["--capture", "--state", str(state)]) == 0
        assert check.main(["--verify", "--state", str(state), "--unit", "other"]) == 2
        assert "captured on" in capsys.readouterr().err

    def test_an_empty_state_file_is_not_a_pass(self, wired, tmp_path, capsys):
        check, _, _ = wired
        empty = tmp_path / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        assert check.main(["--verify", "--state", str(empty)]) == 2
        assert "cannot run" in capsys.readouterr().err

    def test_a_non_object_state_file_is_not_a_pass(self, wired, tmp_path, capsys):
        check, _, _ = wired
        bad = tmp_path / "list.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        assert check.main(["--verify", "--state", str(bad)]) == 2
        assert "cannot run" in capsys.readouterr().err

    def test_capturing_into_a_directory_is_not_a_pass(self, wired, tmp_path, capsys):
        check, _, _ = wired
        assert check.main(["--capture", "--state", str(tmp_path)]) == 2
        assert "cannot run" in capsys.readouterr().err

    def test_one_mode_is_required(self, wired):
        check, state, _ = wired
        with pytest.raises(SystemExit):
            check.main(["--state", str(state)])
        with pytest.raises(SystemExit):
            check.main(["--capture", "--verify", "--state", str(state)])

    def test_a_skipped_check_is_not_a_pass(self, wired, capsys):
        """Exit 2, so a missing state file cannot read as success."""
        check, state, _ = wired
        assert check.main(["--verify", "--state", str(state)]) == 2
        assert "cannot run" in capsys.readouterr().err


class TestRunbookUsesIt:
    """The prose wraps, so compare against normalised whitespace."""

    @pytest.fixture(scope="class")
    def prose(self) -> str:
        return " ".join(RUNBOOK.read_text("utf-8").split())

    def test_the_runbook_documents_both_phases(self, prose):
        assert "restart_check.py --capture" in prose
        assert "restart_check.py --verify" in prose

    def test_the_runbook_names_the_trap_this_check_exists_for(self, prose):
        assert "polls immediately" in prose, (
            "the runbook should say why a glance at the API proves nothing"
        )
        assert "main PID must change" in prose, (
            "the runbook should state the half that catches a skipped restart"
        )

    def test_the_runbook_says_what_a_cannot_run_means(self, prose):
        """Exit 2 covers age and permission refusals, so the operator must recapture."""
        assert "Exit 2 is not a pass" in prose
        assert "capture again" in prose

    def test_the_runbook_rechecks_every_source_after_a_restart(self, prose):
        """A restart re-reads the config, which is where a missing key shows."""
        assert "per-source states" in prose
        assert "not_configured" in prose
