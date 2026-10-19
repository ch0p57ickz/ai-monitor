"""Pace classification for quota windows and budgets.

Implements the pace requirement from the usage-snapshots spec and the
"explicit about evidence" decision from the design:

- ``reported``  — measured recent spend over a sufficient span of
  consecutive, same-account snapshots, projected to the end of the window.
- ``estimated`` — the elapsed fraction of a window whose start is inferred
  from a plausible reset time and duration.
- ``none``      — insufficient evidence. The pace is always an object with
  ``classification == "unknown"``.

Classification is ``under`` / ``at`` / ``over`` relative to the expected
used percentage at the end of the window, using a configurable tolerance
(default five percentage points). Expected values are clamped to 0-100.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .models import SourceDef, unknown_pace

__all__ = [
    "PaceSettings",
    "unknown_pace",
    "compute_pace",
    "enrich_observation",
]


@dataclass(frozen=True)
class PaceSettings:
    """Thresholds for deciding when pace has enough evidence.

    min_span_seconds:
        Minimum span (plus the current point) required before a reported
        spend-rate projection is meaningful.
    max_gap_seconds:
        Maximum gap between consecutive snapshots for the series to count as
        "recent and consecutive". A larger gap means the trend is stale.
    tolerance_percent:
        The configurable tolerance (default 5 percentage points) used to
        decide ``under`` / ``at`` / ``over``.
    """

    min_span_seconds: float = 600.0
    max_gap_seconds: float = 1800.0
    tolerance_percent: float = 5.0


def unknown_pace() -> dict[str, Any]:
    """The canonical 'unknown' pace object (always an object, never null)."""
    return {"classification": "unknown", "basis": "none", "expectedUsedPercent": None}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _get(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def _classify(expected: float, actual: float, tolerance: float) -> str:
    """Classify ``actual`` usage against ``expected`` within ``tolerance``.

    ``at`` when within tolerance, ``over`` when clearly above, ``under``
    when clearly below.
    """
    if actual > expected + tolerance:
        return "over"
    if actual < expected - tolerance:
        return "under"
    return "at"


# ---------------------------------------------------------------------------
# core computation
# ---------------------------------------------------------------------------


def compute_pace(
    window: dict[str, Any],
    settings: Optional[PaceSettings] = None,
    now_iso: str | None = None,
    history: Optional[Iterable[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Compute the pace object for a single quota window.

    ``window`` is a window dict with either camelCase keys (from
    ``QuotaWindow.to_dict``) or snake_case keys (a raw adapter window). Both
    are accepted. ``history`` is an iterable of prior normalized rows for
    the *same window id and account*, each carrying ``collected_at`` (ISO)
    and ``used_percent``.

    Returns the pace object
    ``{"classification", "basis", "expectedUsedPercent"}``.
    """
    settings = settings or PaceSettings()
    now = _parse_iso(now_iso) or datetime.now(timezone.utc)

    used = _get(window, "usedPercent", "used_percent")

    # -- reported spend-rate projection (preferred) ------------------------
    # The strongest available evidence is a measured recent spend series
    # over the same window: project the measured rate to the end of the
    # window, then compare the measured use with that projection. It
    # requires a sufficient, consecutive, positive-spend span and a
    # determinable horizon. When this succeeds it supersedes the
    # elapsed-window estimate because it is based on observed spend rather
    # than a time-based assumption.
    if used is not None and history:
        pace = _reported_pace(window, now, settings, list(history))
        if pace is not None:
            return pace

    # -- estimated elapsed-window fraction (fallback) ----------------------
    # "For a quota with measured used percentage and reliable start and
    # end, the service SHALL calculate expected used percentage from
    # elapsed fraction of the window" — basis ``estimated`` when the start
    # is inferred from reset minus duration, clamped to 0-100.
    if used is not None:
        resets_in_seconds = _get(window, "resetsInSeconds", "resets_in_seconds")
        resets_at = _get(window, "resetsAt", "resets_at")
        duration_minutes = _get(window, "durationMinutes", "duration_minutes")
        start, end = _window_bounds(
            resets_in_seconds=resets_in_seconds,
            resets_at=resets_at,
            duration_minutes=duration_minutes,
            now=now,
        )
        if start is not None and end is not None:
            total = (end - start).total_seconds()
            if total > 0:
                elapsed_fraction = _clamp01((now - start).total_seconds() / total)
                expected = _clamp_percent(elapsed_fraction * 100.0)
                return {
                    "classification": _classify(expected, float(used), settings.tolerance_percent),
                    "basis": "estimated",
                    "expectedUsedPercent": round(expected, 2),
                }

    return unknown_pace()


# ---------------------------------------------------------------------------
# reported spend-rate (internal)
# ---------------------------------------------------------------------------


def _reported_pace(
    window: dict[str, Any],
    now: datetime,
    settings: PaceSettings,
    history: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Project from a consecutive same-account series of measurements.

    Returns None when the series is insufficient (too short, too gappy, no
    measurable net spend, or no horizon to project over).
    """
    points = _gather_points(window, now, history)
    if len(points) < 2:
        return None

    # Consecutive-gap checks.
    for (t0, _), (t1, _) in zip(points, points[1:]):
        gap = (t1 - t0).total_seconds()
        if gap < 0:
            return None
        if gap > settings.max_gap_seconds:
            return None

    span = (points[-1][0] - points[0][0]).total_seconds()
    if span <= 0 or span < settings.min_span_seconds:
        return None

    delta = points[-1][1] - points[0][1]
    if delta <= 0:
        return None

    rate = delta / span  # measured percent per second

    # The window's total duration, in seconds.
    duration_minutes = _get(window, "durationMinutes", "duration_minutes")
    if duration_minutes is None:
        return None
    try:
        total_seconds = float(duration_minutes) * 60.0
    except (TypeError, ValueError):
        return None
    if total_seconds <= 0:
        return None

    # Horizon from now to the window end. Fall back to the remaining
    # duration when the reset is not determinable, so a projection is still
    # possible.
    horizon = _horizon_seconds(window, now)
    if horizon is None:
        horizon = max(0.0, total_seconds)

    # Project the measured spend rate forward to the window end.
    projected_end = _clamp_percent(points[-1][1] + rate * horizon)

    # The expected end-use, for comparison, is the even-consumption
    # projection: the whole window is consumed by reset, so with ``span``
    # seconds of measured activity and ``horizon`` seconds remaining, the
    # expected use at reset under even consumption is 100% * span / (span +
    # horizon). The classification compares the projected end-use against
    # that baseline with the same tolerance the estimated path uses, so the
    # classification and expectedUsedPercent always agree.
    expected_end = _clamp_percent(100.0 * span / (span + horizon))
    tolerance = settings.tolerance_percent
    if projected_end > expected_end + tolerance:
        classification = "over"
    elif projected_end < expected_end - tolerance:
        classification = "under"
    else:
        classification = "at"

    return {
        "classification": classification,
        "basis": "reported",
        "expectedUsedPercent": round(projected_end, 2),
    }


def _gather_points(
    window: dict[str, Any],
    now: datetime,
    history: list[dict[str, Any]],
) -> list[tuple[datetime, float]]:
    """Build the ascending (time, used_percent) series for this window."""
    points: list[tuple[datetime, float]] = []
    used = _get(window, "usedPercent", "used_percent")
    if used is not None:
        points.append((now, float(used)))
    for row in history:
        collected = row.get("collected_at") or row.get("collectedAt")
        prior = row.get("used_percent")
        if prior is None:
            prior = row.get("usedPercent")
        ts = _parse_iso(collected)
        if ts is not None and prior is not None:
            points.append((ts, float(prior)))
    points.sort(key=lambda p: p[0])
    return points


def _horizon_seconds(window: dict[str, Any], now: datetime) -> Optional[float]:
    """Seconds from now to the window end, if determinable."""
    resets_in_seconds = _get(window, "resetsInSeconds", "resets_in_seconds")
    if resets_in_seconds is not None:
        try:
            return max(0.0, float(resets_in_seconds))
        except (TypeError, ValueError):
            pass
    resets_at = _get(window, "resetsAt", "resets_at")
    if resets_at:
        reset = _parse_iso(resets_at)
        if reset is not None:
            return max(0.0, (reset - now).total_seconds())
    return None


# ---------------------------------------------------------------------------
# window bounds (for the elapsed-window estimate, internal)
# ---------------------------------------------------------------------------


def _window_bounds(
    *,
    resets_in_seconds: Any,
    resets_at: Any,
    duration_minutes: Any,
    now: datetime,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (start, end) datetimes, or (None, None) when implausible.

    The end is ``reset`` (from ``resets_in_seconds`` or ``resets_at``). The
    start is ``end - duration_minutes``. The pair is accepted only when the
    start is not in the future and the end is not more than a day in the
    past (a stale reset is not a live window).
    """
    end: Optional[datetime] = None
    if resets_in_seconds is not None:
        try:
            end = now + timedelta(seconds=float(resets_in_seconds))
        except (TypeError, ValueError):
            end = None
    elif resets_at:
        end = _parse_iso(resets_at)
    if end is None:
        return None, None

    if duration_minutes is None:
        return None, None
    try:
        duration_seconds = float(duration_minutes) * 60.0
    except (TypeError, ValueError):
        return None, None
    if duration_seconds <= 0:
        return None, None

    start = end - timedelta(seconds=duration_seconds)
    if end <= now:
        # The window has already ended: there is no live window left to
        # pace against. Treat the reset as implausible rather than
        # reporting a clamped 100% expectation.
        return None, None
    if start > now:
        # The window has not started yet (the reset is further out than the
        # duration). There is no elapsed fraction to pace against, so the
        # timing is not reliable.
        return None, None
    return start, end


# ---------------------------------------------------------------------------
# Convenience for the worker: enrich a full observation's raw payload
# ---------------------------------------------------------------------------


def enrich_observation(
    source_def: SourceDef,
    raw: dict[str, Any],
    settings: Optional[PaceSettings] = None,
    history: Optional[dict[str, list[dict[str, Any]]]] = None,
    now_iso: Optional[str] = None,
) -> dict[str, Any]:
    """Return a new raw payload with ``pace`` filled in for every window.

    ``history`` maps a window ``id`` to its list of prior normalized rows.
    Windows with no prior rows still get an estimated pace when the window
    has a plausible reset and duration. ``now_iso`` anchors the pace math to
    a fixed clock (used by tests); it defaults to the current time.
    """
    out = dict(raw)
    windows = list(raw.get("quota_windows") or [])
    history = history or {}
    for i, w in enumerate(windows):
        w = dict(w)
        wid = w.get("id", "")
        w["pace"] = compute_pace(
            w, settings=settings, history=history.get(wid, []), now_iso=now_iso
        )
        windows[i] = w
    out["quota_windows"] = windows
    return out
