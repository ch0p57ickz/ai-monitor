"""Guards on the restart-preservation check (task 5.2).

The criterion passes wrongly in two independent ways, so the tests assert both
failures, the pass, and the CLI contract that separates a skip from a success:

- a fresh poll satisfies "does the API serve data?" even when nothing was retained,
  because the worker polls as soon as it starts — the main PID must have changed;
- a changed PID satisfies "did it restart?" even when the database lost everything —
  the captured timestamps must still be served.

The tool is end-to-end against a running unit, so the injection here is at the two
boundaries it has: the API fetch and `systemctl`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "gx10-runbook.md"


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


BEFORE = ("2026-10-19T18:00:00Z", "2026-10-19T18:00:01Z")


class TestServedTimestamps:
    def test_reads_every_observation(self, check):
        assert check.served_timestamps(_usage("b", "a")) == ["a", "b"]

    def test_ignores_rows_without_a_timestamp(self, check):
        assert check.served_timestamps({"observations": [{"id": "codex"}, "junk"]}) == []

    def test_a_payload_without_observations_is_empty(self, check):
        assert check.served_timestamps({}) == []


class TestCapture:
    def test_records_the_pid_and_the_timestamps(self, check):
        state = check.capture(
            "ai-monitor",
            "http://127.0.0.1:8732",
            fetch=_fetch(usage=_usage(*BEFORE)),
            run=_runner("1000"),
        )
        assert state == {
            "unit": "ai-monitor",
            "pid": "1000",
            "timestamps": sorted(BEFORE),
        }

    def test_it_refuses_to_capture_nothing(self, check):
        """An empty capture would make `--verify` pass on anything."""
        with pytest.raises(RuntimeError, match="nothing to verify"):
            check.capture(
                "ai-monitor",
                "http://127.0.0.1:8732",
                fetch=_fetch(usage=_usage()),
                run=_runner("1000"),
            )

    def test_a_failed_systemctl_call_is_not_a_state_file(self, check):
        with pytest.raises(RuntimeError, match="systemctl show"):
            check.capture(
                "ai-monitor",
                "http://127.0.0.1:8732",
                fetch=_fetch(usage=_usage(*BEFORE)),
                run=_runner("", returncode=1),
            )


class TestVerify:
    def _state(self, pid: str = "1000") -> dict:
        return {"unit": "ai-monitor", "pid": pid, "timestamps": list(BEFORE)}

    def test_passes_with_a_new_pid_and_the_same_rows(self, check):
        ok, report = check.verify(
            self._state(),
            "ai-monitor",
            "http://127.0.0.1:8732",
            fetch=_fetch(history=_history(*BEFORE, "2026-10-19T18:01:00Z")),
            run=_runner("2000"),
        )
        assert ok, report
        assert "1000 -> 2000" in report
        assert "2 captured, 2 still served" in report

    def test_it_fails_when_the_unit_did_not_restart(self, check):
        """The fresh-poll trap: the rows are served, and it proves nothing."""
        ok, report = check.verify(
            self._state(),
            "ai-monitor",
            "http://127.0.0.1:8732",
            fetch=_fetch(history=_history(*BEFORE)),
            run=_runner("1000"),
        )
        assert not ok
        assert "main PID did not change" in report

    def test_it_fails_when_a_captured_row_is_gone(self, check):
        ok, report = check.verify(
            self._state(),
            "ai-monitor",
            "http://127.0.0.1:8732",
            fetch=_fetch(history=_history(BEFORE[0], "2026-10-19T18:01:00Z")),
            run=_runner("2000"),
        )
        assert not ok
        assert "no longer served" in report
        assert BEFORE[1] in report

    def test_both_failures_are_reported_together(self, check):
        ok, report = check.verify(
            self._state(),
            "ai-monitor",
            "http://127.0.0.1:8732",
            fetch=_fetch(history=_history()),
            run=_runner("1000"),
        )
        assert not ok
        assert "main PID did not change" in report
        assert "no longer served" in report

    def test_a_truncated_window_is_called_out(self, check):
        """"Missing" from a truncated response may be outside the window, not lost."""
        ok, report = check.verify(
            self._state(),
            "ai-monitor",
            "http://127.0.0.1:8732",
            fetch=_fetch(history=_history("2026-10-19T18:01:00Z", truncated=True)),
            run=_runner("2000"),
        )
        assert not ok
        assert "truncated" in report


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

        holder["pid"] = "3000"  # the restart
        assert check.main(["--verify", "--state", str(state)]) == 0
        assert "ok: a new process" in capsys.readouterr().out

    def test_verify_fails_on_the_same_process(self, wired, capsys):
        check, state, _ = wired
        assert check.main(["--capture", "--state", str(state)]) == 0
        assert check.main(["--verify", "--state", str(state)]) == 1
        assert "FAIL" in capsys.readouterr().out

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
