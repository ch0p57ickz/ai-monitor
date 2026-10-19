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

    def test_clamped_at_window_end(self):
        """Elapsed fraction clamped to 100% when window nearly ended."""
        p = compute_pace(
            _window(used_percent=100.0, resets_in_seconds=60, duration_minutes=1440),
            now_iso=NOW,
        )
        assert p["basis"] == "estimated"
        assert p["expectedUsedPercent"] == pytest.approx(100.0, abs=1.0)


class TestReportedPace:
    def test_reported_precedence(self):
        """When sufficient history exists, reported takes precedence over estimated."""
        hist = _history(span_seconds=1800.0, points=4, start_used=40.0, end_used=50.0)
        p = compute_pace(_window(used_percent=50.0), settings=PaceSettings(), now_iso=NOW, history=hist)
        assert p["basis"] == "reported"

    def test_reported_at(self):
        """Project the measured burn rate to the window end; if it lands within
        tolerance of the even-consumption projection → 'at'.

        Window: 24 h duration, resets in 12 h → horizon 43200 s.
        History 40 → 50 over 1800 s → rate = 10/1800 ≈ 0.00556 %/s.
        projected_end = 50 + 0.00556 * 43200 = 290 → clamps to 100.
        expected_end = 100 * span/(span+horizon) = 100 * 1800/45000 = 4.0.
        100 > 4 + 5 → 'over'.
        """
        hist = _history(span_seconds=1800.0, points=4, start_used=40.0, end_used=50.0)
        p = compute_pace(
            _window(used_percent=50.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["basis"] == "reported"
        assert p["classification"] == "over"
        assert p["expectedUsedPercent"] == 100.0

    def test_reported_over(self):
        """Projection well above the even-consumption end-use → 'over'.

        Window: 24 h duration, resets in 6 h → horizon 21600 s.
        History 0 → 30 over 1800 s → rate = 30/1800 ≈ 0.01667 %/s.
        projected_end = 30 + 0.01667 * 21600 = 390 → clamped to 100.
        expected_end = 100 * 1800/(1800+21600) ≈ 7.69.
        100 > 7.69 + 5 → 'over'.
        """
        hist = _history(span_seconds=1800.0, points=4, start_used=0.0, end_used=30.0)
        p = compute_pace(
            _window(used_percent=30.0, resets_in_seconds=21600, duration_minutes=1440),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["basis"] == "reported"
        assert p["classification"] == "over"
        assert p["expectedUsedPercent"] == 100.0

    def test_reported_under(self):
        """Projection below the even-consumption end-use → 'under'.

        This is the case the old rate-vs-pace-rate math made unreachable; with
        the consistent projected-end comparison it is reachable.

        Window: 1 h duration (3600 s), resets in 1200 s (start was 2400 s
        ago → 66.7% elapsed) → horizon 1200 s. History 10 → 12 over 1800 s
        → rate = 2/1800 ≈ 0.001111 %/s.
        projected_end = 12 + 0.001111 * 1200 = 13.33.
        expected_end = 100 * span/(span+horizon) = 100 * 1800/3000 = 60.0.
        The even-consumption baseline says ~60% used by reset (66.7% of the
        window elapsed), but this window is only at ~12% and burning slowly,
        so the projection (13.33) is well below 60 → 'under'.
        """
        hist = _history(span_seconds=1800.0, points=4, start_used=10.0, end_used=12.0)
        p = compute_pace(
            _window(used_percent=12.0, resets_in_seconds=1200, duration_minutes=60),
            settings=PaceSettings(),
            now_iso=NOW,
            history=hist,
        )
        assert p["basis"] == "reported"
        assert p["classification"] == "under"
        assert p["expectedUsedPercent"] == pytest.approx(13.33, abs=0.2)

    def test_insufficient_span(self):
        """History span below min_span_seconds → falls through to estimated or unknown."""
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
        """No positive delta in history → not reported."""
        hist = [
            {"collected_at": (NOW_DT - timedelta(seconds=900)).isoformat().replace("+00:00", "Z"),
             "used_percent": 50.0},
            {"collected_at": NOW_DT.isoformat().replace("+00:00", "Z"),
             "used_percent": 50.0},
        ]
        p = compute_pace(_window(used_percent=50.0), now_iso=NOW, history=hist)
        assert p["basis"] == "estimated"

    def test_single_point_insufficient(self):
        """A single history row + current point = 2 points but span may be 0."""
        hist = [{"collected_at": NOW_DT.isoformat().replace("+00:00", "Z"), "used_percent": 42.0}]
        p = compute_pace(_window(used_percent=42.0), now_iso=NOW, history=hist)
        # 2 points, same time → span 0 < min_span → not reported.
        assert p["basis"] == "estimated"


class TestTolerance:
    def test_at_boundary(self):
        """Exactly at tolerance boundary → 'at'."""
        # Expected 50, actual 55, tolerance 5 → |55-50|=5 ≤ 5 → 'at'
        p = compute_pace(
            _window(used_percent=55.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(tolerance_percent=5.0),
            now_iso=NOW,
        )
        assert p["classification"] == "at"

    def test_over_boundary(self):
        """Just above tolerance → 'over'."""
        p = compute_pace(
            _window(used_percent=56.0, resets_in_seconds=43200, duration_minutes=1440),
            settings=PaceSettings(tolerance_percent=5.0),
            now_iso=NOW,
        )
        assert p["classification"] == "over"

    def test_under_boundary(self):
        p = compute_pace(
            _window(used_percent=44.0, resets_in_seconds=43200, duration_minutes=1440),
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
