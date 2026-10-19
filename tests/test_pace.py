"""Tests for the pace classification module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_monitor.pace import (
    PaceSettings,
    compute_pace,
    enrich_observation,
    unknown_pace,
)
from ai_monitor.models import SourceDef, get_source_def

CODEX = get_source_def("codex")

# Fixed "now" for deterministic tests.
NOW = "2026-10-19T12:00:00Z"
NOW_DT = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _history(span_seconds: float = 1800.0, points: int = 4, start_used: float = 10.0, end_used: float = 50.0) -> list[dict]:
    """Build a list of history rows spread over ``span_seconds`` ending at NOW."""
    rows = []
    for i in range(points):
        frac = i / max(points - 1, 1)
        ts = NOW_DT - timedelta(seconds=span_seconds * (1.0 - frac))
        used = start_used + (end_used - start_used) * frac
        rows.append({"collected_at": ts.isoformat().replace("+00:00", "Z"), "used_percent": round(used, 4)})
    return rows


def _window(**overrides):
    """Build a snake_case window dict with sensible defaults."""
    w = {
        "id": "w1",
        "label": "Test window",
        "scope": "account",
        "group": "default",
        "model": None,
        "used_percent": None,
        "remaining_percent": None,
        "duration_minutes": 10080,  # 1 week
        "resets_at": "2026-10-26T12:00:00Z",  # 7 days from NOW
        "resets_in_seconds": 604800,
    }
    w.update(overrides)
    return w


class TestUnknownPace:
    def test_shape(self):
        p = unknown_pace()
        assert p == {"classification": "unknown", "basis": "none", "expectedUsedPercent": None}

    def test_null_used_percent(self):
        """A window with no measured used_percent yields unknown pace."""
        p = compute_pace(_window(used_percent=None), now_iso=NOW)
        assert p["classification"] == "unknown"
        assert p["basis"] == "none"

    def test_no_reset_info(self):
        """A window with used_percent but no reset/duration yields unknown."""
        p = compute_pace(
            _window(used_percent=42.0, resets_at=None, resets_in_seconds=None, duration_minutes=None),
            now_iso=NOW,
        )
        assert p["classification"] == "unknown"

    def test_ended_window(self):
        """A window whose reset is in the past yields unknown (no live window)."""
        p = compute_pace(
            _window(used_percent=42.0, resets_at="2026-10-18T00:00:00Z", resets_in_seconds=None,
                    duration_minutes=10080),
            now_iso=NOW,
        )
        assert p["classification"] == "unknown"


class TestMalformedInputs:
    """A malformed payload must never raise nor be silently mis-classified
    (spec: pace is ``unknown`` when measured usage is missing)."""

    @pytest.mark.parametrize(
        "bad",
        ["abc", "", "NaN", float("nan"), float("inf"), float("-inf")],
    )
    def test_non_finite_used_percent_is_unknown(self, bad):
        p = compute_pace(_window(used_percent=bad), now_iso=NOW)
        assert p["classification"] == "unknown"
        assert p["basis"] == "none"

    def test_malformed_resets_in_falls_back_to_resets_at(self):
        """A non-numeric relative reset must not suppress a valid absolute
        ``resets_at``."""
        p = compute_pace(
            _window(
                used_percent=50.0,
                resets_in_seconds="soon",
                resets_at=(NOW_DT + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
                duration_minutes=1440,
            ),
            now_iso=NOW,
        )
        assert p["classification"] != "unknown"
        assert p["expectedUsedPercent"] == pytest.approx(50.0)

    def test_non_finite_history_point_is_ignored(self):
        """A NaN history point must not enter the measured series."""
        hist = _history(span_seconds=1800, points=4, start_used=10.0, end_used=50.0)
        hist.append(
            {
                "collected_at": NOW_DT.isoformat().replace("+00:00", "Z"),
                "used_percent": float("nan"),
            }
        )
        p = compute_pace(
            _window(
                used_percent=50.0,
                resets_in_seconds=43200,
                resets_at=(NOW_DT + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
                duration_minutes=1440,
            ),
            history=hist,
            now_iso=NOW,
        )
        # With the NaN point dropped, the real 10→50 series has a sufficient
        # span and positive movement, so the basis is ``reported``. If the
        # NaN point leaked in, it would become the newest point and
        # ``nan > 0`` is False, degrading the basis to ``estimated``.
        assert p["classification"] == "at"
        assert p["basis"] == "reported"


class TestEstimatedPace:
    def test_halfway(self):
        """Window halfway elapsed, used matches expected → 'at'."""
        # Window: 24 h duration, resets 12 h from now → start was 12 h ago.
        # Elapsed fraction = 12/24 = 0.5 → expected = 50%.
        p = compute_pace(
            _window(used_percent=50.0, resets_in_seconds=43200, duration_minutes=1440,
                    resets_at=(NOW_DT + timedelta(hours=12)).isoformat().replace("+00:00", "Z")),
            now_iso=NOW,
        )
        assert p["basis"] == "estimated"
        assert p["classification"] == "at"
        assert p["expectedUsedPercent"] == pytest.approx(50.0, abs=0.5)

    def test_under(self):
        """Used well below expected → 'under'."""
        p = compute_pace(
            _window(used_percent=10.0, resets_in_seconds=43200, duration_minutes=1440),
            now_iso=NOW,
        )
        assert p["basis"] == "estimated"
        assert p["classification"] == "under"
        assert p["expectedUsedPercent"] == pytest.approx(50.0, abs=0.5)

    def test_over(self):
        """Used well above expected → 'over'."""
        p = compute_pace(
            _window(used_percent=90.0, resets_in_seconds=43200, duration_minutes=1440),
            now_iso=NOW,
        )
        assert p["basis"] == "estimated"
        assert p["classification"] == "over"

    def test_near_end_clamped_at_100(self):
        """Window nearly elapsed: expected fraction clamped toward 100."""
        # 24 h duration, resets in 30 min → 1410/1440 elapsed = ~97.9%.
        p = compute_pace(
            _window(used_percent=99.0, resets_in_seconds=1800, duration_minutes=1440),
            now_iso=NOW,
        )
        assert p["basis"] == "estimated"
        assert p["expectedUsedPercent"] == pytest.approx(97.92, abs=0.5)

    def test_not_yet_started_is_unknown(self):
        """A window whose reset is further out than its duration (start in the
        future) has no elapsed fraction to pace against → unknown."""
        # duration 1440 min (24 h) but resets in 100000 s (~27.8 h) →
        # start = now + ~3.8 h, in the future.
        p = compute_pace(
            _window(used_percent=42.0, resets_in_seconds=100000, duration_minutes=1440),
            now_iso=NOW,
        )
        assert p["classification"] == "unknown"
        assert p["basis"] == "none"


class TestReportedPace:
    """The reported basis is an *evidence gate*, not a different comparison.

    The comparison is always actual used % vs the elapsed-window expectation;
    the basis only records whether a sufficient, consecutive, same-account
    measured series with real recent movement backs the reading.
    """

    def test_reported_basis_on_sufficient_series(self):
        """Sufficient consecutive series with movement → basis 'reported',
        classification still from actual vs elapsed-window expectation."""
        # Window: 24 h duration, resets in 12 h → 50% elapsed → expected 50.
        # Used 50 → 'at'; series 40→50 over 1800 s (consecutive, positive)
        # upgrades the basis to 'reported'.
        hist = _history(span_seconds=1800.0, points=4, start_used=40.0, end_used=50.0)
        p = compute_pace(
            _window(used_percent=50.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["basis"] == "reported"
        assert p["classification"] == "at"
        assert p["expectedUsedPercent"] == pytest.approx(50.0, abs=0.5)

    def test_reported_over(self):
        """Same series, but actual used is well above the elapsed expectation
        → 'over', with the basis still 'reported'."""
        # Expected 50 (24 h, 12 h elapsed), actual 90 → over.
        hist = _history(span_seconds=1800.0, points=4, start_used=80.0, end_used=90.0)
        p = compute_pace(
            _window(used_percent=90.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["basis"] == "reported"
        assert p["classification"] == "over"
        assert p["expectedUsedPercent"] == pytest.approx(50.0, abs=0.5)

    def test_reported_under(self):
        """Same series, but actual used is well below the elapsed expectation
        → 'under', with the basis still 'reported'."""
        # Expected 50 (24 h, 12 h elapsed), actual 10 → under.
        hist = _history(span_seconds=1800.0, points=4, start_used=0.0, end_used=10.0)
        p = compute_pace(
            _window(used_percent=10.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["basis"] == "reported"
        assert p["classification"] == "under"
        assert p["expectedUsedPercent"] == pytest.approx(50.0, abs=0.5)

    def test_expected_is_elapsed_window_fraction(self):
        """The spec's authoritative repro: a window half elapsed and half used
        reads 'at' at the elapsed-window expectation (~50%), never 'over'."""
        hist = _history(span_seconds=1800.0, points=4, start_used=40.0, end_used=50.0)
        p = compute_pace(
            _window(used_percent=50.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["classification"] == "at"
        assert p["expectedUsedPercent"] == pytest.approx(50.0, abs=0.5)

    def test_insufficient_span(self):
        """History span below min_span_seconds → not reported (estimated)."""
        # Only 60 s span → too short for reported.
        hist = [
            {"collected_at": (NOW_DT - timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
             "used_percent": 10.0},
            {"collected_at": NOW_DT.isoformat().replace("+00:00", "Z"),
             "used_percent": 11.0},
        ]
        p = compute_pace(_window(used_percent=11.0), now_iso=NOW, history=hist)
        # Falls through to estimated (window has reset+duration).
        assert p["basis"] in ("estimated", "none")

    def test_too_gappy(self):
        """Gap between history rows exceeds max_gap_seconds → not reported."""
        hist = [
            {"collected_at": (NOW_DT - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
             "used_percent": 10.0},
            {"collected_at": NOW_DT.isoformat().replace("+00:00", "Z"),
             "used_percent": 50.0},
        ]
        p = compute_pace(_window(used_percent=50.0), now_iso=NOW, history=hist)
        # Gap = 5 h > 1800 s max_gap → not reported; falls to estimated.
        assert p["basis"] == "estimated"

    def test_no_spend(self):
        """Flat series (no positive delta) → not reported (estimated)."""
        hist = [
            {"collected_at": (NOW_DT - timedelta(seconds=900)).isoformat().replace("+00:00", "Z"),
             "used_percent": 50.0},
            {"collected_at": NOW_DT.isoformat().replace("+00:00", "Z"),
             "used_percent": 50.0},
        ]
        p = compute_pace(_window(used_percent=50.0), now_iso=NOW, history=hist)
        assert p["basis"] == "estimated"

    def test_single_point_insufficient(self):
        """A single history row at NOW + current point → span 0, not reported."""
        hist = [{"collected_at": NOW_DT.isoformat().replace("+00:00", "Z"), "used_percent": 42.0}]
        p = compute_pace(_window(used_percent=42.0), now_iso=NOW, history=hist)
        assert p["basis"] == "estimated"


class TestTolerance:
    # All tolerance tests use a 24 h window that is 50% elapsed (resets in
    # 12 h) so the elapsed-window expectation is 50.
    _WIN = dict(resets_in_seconds=43200, duration_minutes=1440)

    def test_at_boundary(self):
        """Exactly at tolerance boundary → 'at'."""
        # Expected 50, actual 55, tolerance 5 → |55-50|=5 ≤ 5 → 'at'
        p = compute_pace(
            _window(used_percent=55.0, **self._WIN),
            settings=PaceSettings(tolerance_percent=5.0),
            now_iso=NOW,
        )
        assert p["classification"] == "at"

    def test_over_boundary(self):
        """Just above tolerance → 'over'."""
        p = compute_pace(
            _window(used_percent=56.0, **self._WIN),
            settings=PaceSettings(tolerance_percent=5.0),
            now_iso=NOW,
        )
        assert p["classification"] == "over"

    def test_under_boundary(self):
        p = compute_pace(
            _window(used_percent=44.0, **self._WIN),
            settings=PaceSettings(tolerance_percent=5.0),
            now_iso=NOW,
        )
        assert p["classification"] == "under"


class TestEnrichObservation:
    def test_fills_pace_for_all_windows(self):
        raw = {
            "quota_windows": [
                {"id": "w1", "used_percent": 50.0, "resets_in_seconds": 43200, "duration_minutes": 1440},
                {"id": "w2", "used_percent": None},
            ],
        }
        out = enrich_observation(CODEX, raw)
        assert len(out["quota_windows"]) == 2
        assert out["quota_windows"][0]["pace"]["basis"] == "estimated"
        assert out["quota_windows"][1]["pace"]["classification"] == "unknown"

    def test_preserves_other_keys(self):
        raw = {"quota_windows": [], "account_id": "abc", "notes": "x"}
        out = enrich_observation(CODEX, raw)
        assert out["account_id"] == "abc"
        assert out["notes"] == "x"

    def test_uses_history_per_window(self):
        raw = {"quota_windows": [{"id": "w1", "used_percent": 50.0, "resets_in_seconds": 600, "duration_minutes": 1440}]}
        # History ends below current used so the series has a positive delta
        # (40 → 48 over 1800 s, current = 50). This lets the reported path
        # fire and the per-window id lookup is exercised.
        hist = {"w1": _history(span_seconds=1800.0, points=4, start_used=40.0, end_used=48.0)}
        # Anchor to the fixed NOW so the history (which ends at NOW) is
        # recent and the reported spend-rate path fires deterministically.
        out = enrich_observation(CODEX, raw, history=hist, now_iso=NOW)
        assert out["quota_windows"][0]["pace"]["basis"] == "reported"
