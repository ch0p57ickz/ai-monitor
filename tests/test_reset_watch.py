"""Tests for the reset-watch tool's decision logic.

``tools/reset_watch.py`` is not part of the installed package (it is an operator
tool), so it is loaded from its path. The logic under test decides whether a
quota window turned over between two samples and whether the pace computed right
after a reset compares against the *new* window — the exact mistake a stale
window start would cause.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).parents[1] / "tools" / "reset_watch.py"


@pytest.fixture(scope="module")
def watch():
    spec = importlib.util.spec_from_file_location("reset_watch", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _window(**overrides):
    window = {
        "id": "codex:codex:primary",
        "usedPercent": 100.0,
        "resetsAt": "2026-10-19T19:37:34Z",
        "durationMinutes": 300.0,
        "pace": {"classification": "over", "basis": "estimated", "expectedUsedPercent": 99.0},
    }
    window.update(overrides)
    return window


class TestDetectReset:
    def test_a_resets_at_advance_of_one_window_is_a_reset(self, watch):
        previous = _window()
        current = _window(
            usedPercent=0.0, resetsAt="2026-10-20T00:37:34Z"
        )
        assert watch.detect_reset(previous, current) is not None

    def test_a_tiny_resets_at_shift_is_not_a_reset(self, watch):
        previous = _window()
        current = _window(resetsAt="2026-10-19T19:39:34Z")
        assert watch.detect_reset(previous, current) is None

    def test_a_used_percent_collapse_is_a_reset_even_without_moved_bounds(self, watch):
        previous = _window(usedPercent=80.0)
        current = _window(usedPercent=1.0)
        assert watch.detect_reset(previous, current) is not None

    def test_a_small_used_percent_drop_is_not_a_reset(self, watch):
        previous = _window(usedPercent=30.0)
        current = _window(usedPercent=25.0)
        assert watch.detect_reset(previous, current) is None

    def test_a_missing_sample_is_not_a_reset(self, watch):
        assert watch.detect_reset(None, _window()) is None
        assert watch.detect_reset(_window(), None) is None
        assert watch.detect_reset(None, None) is None

    def test_a_backwards_resets_at_is_not_a_reset(self, watch):
        previous = _window(resetsAt="2026-10-20T00:37:34Z")
        current = _window(resetsAt="2026-10-19T19:37:34Z")
        assert watch.detect_reset(previous, current) is None


class TestFindWindow:
    def test_finds_the_primary_window(self, watch):
        usage = {
            "observations": [
                {
                    "source": "codex",
                    "quotaWindows": [
                        {"id": "codex:codex:primary", "usedPercent": 10.0},
                        {"id": "codex:codex:secondary", "usedPercent": 20.0},
                    ],
                }
            ]
        }
        assert watch.find_window(usage, "primary")["usedPercent"] == 10.0
        assert watch.find_window(usage, "secondary")["usedPercent"] == 20.0

    def test_ignores_other_sources(self, watch):
        usage = {
            "observations": [
                {
                    "source": "antigravity",
                    "quotaWindows": [{"id": "antigravity:primary"}],
                }
            ]
        }
        assert watch.find_window(usage, "primary") is None

    def test_missing_suffix_is_none(self, watch):
        assert watch.find_window({"observations": []}, "primary") is None


class TestIsDrifting:
    """A boundary that tracks the clock is not a fixed one."""

    def test_an_advance_of_one_poll_interval_is_drift(self, watch):
        previous = _window(resetsAt="2026-10-19T19:37:35Z")
        current = _window(resetsAt="2026-10-19T19:38:36Z")
        assert watch.is_drifting(previous, current) is True

    def test_a_full_window_advance_is_a_reset_not_drift(self, watch):
        previous = _window(resetsAt="2026-10-19T19:37:35Z")
        current = _window(
            resetsAt="2026-10-20T00:37:35Z", usedPercent=0.0
        )
        assert watch.is_drifting(previous, current) is False

    def test_an_unchanged_boundary_is_not_drift(self, watch):
        assert watch.is_drifting(_window(), _window()) is False

    def test_jitter_below_the_slop_is_not_drift(self, watch):
        previous = _window(resetsAt="2026-10-19T19:37:35Z")
        current = _window(resetsAt="2026-10-19T19:37:40Z")
        assert watch.is_drifting(previous, current) is False

    def test_a_backwards_correction_is_not_drift(self, watch):
        previous = _window(resetsAt="2026-10-19T19:37:35Z")
        current = _window(resetsAt="2026-10-19T19:37:30Z")
        assert watch.is_drifting(previous, current) is False

    def test_missing_samples_are_not_drift(self, watch):
        assert watch.is_drifting(None, _window()) is False
        assert watch.is_drifting(_window(), None) is False

    def test_a_missing_duration_falls_back_to_the_previous_sample(self, watch):
        """One sample missing a duration must not lose the classification."""
        previous = _window(resetsAt="2026-10-19T19:37:35Z", durationMinutes=None)
        current = _window(resetsAt="2026-10-19T19:38:36Z")
        assert watch.is_drifting(previous, current) is True

    def test_no_duration_on_either_side_is_not_drift(self, watch):
        previous = _window(resetsAt="2026-10-19T19:37:35Z", durationMinutes=None)
        current = _window(resetsAt="2026-10-19T19:38:36Z", durationMinutes=None)
        assert watch.is_drifting(previous, current) is False


class TestParseIso:
    def test_trailing_z_is_utc(self, watch):
        parsed = watch.parse_iso("2026-10-19T19:37:34Z")
        assert parsed is not None
        assert parsed.utcoffset().total_seconds() == 0

    def test_non_string_and_garbage_are_none(self, watch):
        assert watch.parse_iso(None) is None
        assert watch.parse_iso("") is None
        assert watch.parse_iso("not-a-date") is None
        assert watch.parse_iso(1760000000) is None
