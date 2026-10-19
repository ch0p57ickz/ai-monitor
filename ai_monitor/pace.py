"""Pace classification for quota windows and budgets.

Implements the pace requirement from the usage-snapshots spec: for a quota
with measured used percentage and reliable start and end, expected used
percentage is the *elapsed fraction of the window* (clamped 0-100) and the
actual used percentage is classified ``under`` / ``at`` / ``over`` with a
configurable tolerance (default five percentage points).

Two bases are distinguished by how strongly the evidence supports that
classification:

- ``estimated`` — only the window's reset time and duration are known, so
  the start is inferred from them. The elapsed-window expectation is used
  directly.
- ``reported``  — in addition to the window bounds, there is a measured
  recent spend series over a sufficient span of consecutive, same-account
  snapshots. The elapsed-window expectation is still what the actual used
  percentage is compared against, but the classification is only labeled
  ``reported`` when the measured series shows real, recent movement —
  otherwise it degrades to ``estimated`` (so a stale flat series does not
  masquerade as strong evidence).
- ``none``      — insufficient evidence. The pace is always an object with
  ``classification == "unknown"``.

``expectedUsedPercent`` is the elapsed-window expectation in both bases, so
it means the same thing whether the basis is ``reported`` or ``estimated``.
"""

from __future__ import annotations

import math
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
    """Thresholds for deciding whether pace evidence earns the ``reported`` label.

    The comparison target is always the elapsed-window expectation; these
    thresholds only gate the *basis* label.

    min_span_seconds:
        Minimum span (plus the current point) of a consecutive measured
        series before it is strong enough to label the reading ``reported``.
    max_gap_seconds:
        Maximum gap between consecutive snapshots for the series to count as
        "recent and consecutive". A larger gap means the series is stale, so
        the reading falls back to ``estimated``.
    tolerance_percent:
        The configurable tolerance (default 5 percentage points) used to
        decide ``under`` / ``at`` / ``over``.
    """

    min_span_seconds: float = 600.0
    max_gap_seconds: float = 1800.0
    tolerance_percent: float = 5.0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _get(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _as_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to float, or return None when it is not a finite number.

    Guards against malformed provider payloads (a used percent sent as a
    non-numeric string, or a JSON ``NaN``/``Infinity``) so a single bad
    window cannot raise, nor be silently mis-classified, and drop or corrupt
    the whole source.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


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

    The required rule (usage-snapshots spec): expected used percentage is
    the *elapsed fraction of the window*, clamped 0-100, and the actual
    used percentage is classified ``under`` / ``at`` / ``over`` against it
    with ``tolerance_percent``. ``expectedUsedPercent`` is that expectation
    in both bases.

    The basis labels how strongly the evidence supports the reading:
    ``reported`` when a sufficient, consecutive, same-account measured
    series with real recent movement exists alongside the window bounds;
    otherwise ``estimated`` (start inferred from reset and duration);
    otherwise ``none`` (unknown).

    Returns the pace object
    ``{"classification", "basis", "expectedUsedPercent"}``.
    """
    settings = settings or PaceSettings()
    now = _parse_iso(now_iso) or datetime.now(timezone.utc)

    used_value = _as_float(_get(window, "usedPercent", "used_percent"))

    if used_value is None:
        return unknown_pace()

    # Elapsed-window expectation: the required comparison target. The end is
    # the reset time — a provider's ``resets_in_seconds`` (relative to now)
    # is preferred because it is live; otherwise the absolute ``resets_at``;
    # the start is end - duration_minutes.
    # A non-numeric or non-finite ``resetsInSeconds`` coerces to None, so a
    # malformed relative reset does not suppress a valid absolute
    # ``resetsAt``. ``resetsInSeconds`` (live, relative to the poll) is
    # otherwise preferred over the absolute ``resetsAt``.
    resets_in_seconds = _as_float(_get(window, "resetsInSeconds", "resets_in_seconds"))
    duration_minutes = _get(window, "durationMinutes", "duration_minutes")
    start, end = _window_bounds(
        reset_at=None if resets_in_seconds is not None else _parse_iso(_get(window, "resetsAt", "resets_at")),
        reset_in_seconds=resets_in_seconds,
        duration_minutes=duration_minutes,
        now=now,
    )
    if start is None or end is None:
        return unknown_pace()

    # ``_window_bounds`` guarantees a strictly positive duration, so
    # ``total`` is always > 0 here.
    total = (end - start).total_seconds()
    elapsed_fraction = _clamp01((now - start).total_seconds() / total)
    expected = _clamp_percent(elapsed_fraction * 100.0)
    classification = _classify(expected, used_value, settings.tolerance_percent)

    # ``reported`` only when a sufficient measured same-account series with
    # real, recent movement exists; otherwise the reading is an ``estimated``
    # elapsed-window fraction.
    basis = "estimated"
    if history and _reported_evidence(window, now, settings, list(history)):
        basis = "reported"

    return {
        "classification": classification,
        "basis": basis,
        "expectedUsedPercent": round(expected, 2),
    }


# ---------------------------------------------------------------------------
# reported basis evidence (internal)
# ---------------------------------------------------------------------------


def _reported_evidence(
    window: dict[str, Any],
    now: datetime,
    settings: PaceSettings,
    history: list[dict[str, Any]],
) -> bool:
    """Whether a measured series justifies the ``reported`` basis label.

    The elapsed-window expectation is the comparison target in every case;
    this gate only decides how strongly the evidence supports the reading.
    A ``reported`` label requires a measured recent spend series over a
    sufficient span of consecutive snapshots that shows real recent movement
    (positive net used percent over the span). Otherwise the reading degrades
    to ``estimated`` — a stale or flat series is not strong evidence.
    """
    points = _gather_points(window, now, history)
    if len(points) < 2:
        return False

    # Consecutive: no gap larger than the maximum. ``points`` is sorted
    # ascending by timestamp, so gaps are never negative.
    for (t0, _), (t1, _) in zip(points, points[1:]):
        gap = (t1 - t0).total_seconds()
        if gap > settings.max_gap_seconds:
            return False

    span = (points[-1][0] - points[0][0]).total_seconds()
    if span < settings.min_span_seconds:
        return False

    # Real recent movement: net positive used percent over the span.
    return (points[-1][1] - points[0][1]) > 0


def _gather_points(
    window: dict[str, Any],
    now: datetime,
    history: list[dict[str, Any]],
) -> list[tuple[datetime, float]]:
    """Build the ascending (time, used_percent) series for this window."""
    points: list[tuple[datetime, float]] = []
    used = _as_float(_get(window, "usedPercent", "used_percent"))
    if used is not None:
        points.append((now, used))
    for row in history:
        collected = row.get("collected_at") or row.get("collectedAt")
        prior = row.get("used_percent")
        if prior is None:
            prior = row.get("usedPercent")
        ts = _parse_iso(collected)
        prior_value = _as_float(prior)
        if ts is not None and prior_value is not None:
            points.append((ts, prior_value))
    points.sort(key=lambda p: p[0])
    return points


# ---------------------------------------------------------------------------
# window bounds (for the elapsed-window estimate, internal)
# ---------------------------------------------------------------------------


def _window_bounds(
    *,
    reset_at: Optional[datetime],
    reset_in_seconds: Any,
    duration_minutes: Any,
    now: datetime,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (start, end) datetimes, or (None, None) when implausible.

    The end is the window's reset time: an explicit ``reset_at`` if given,
    otherwise ``now + reset_in_seconds``. The start is ``end -
    duration_minutes``. The pair is accepted only when the start is not in
    the future and the end is not in the past (a stale or not-yet-started
    reset is not a live window).
    """
    end: Optional[datetime] = reset_at
    if end is None and reset_in_seconds is not None:
        try:
            end = now + timedelta(seconds=float(reset_in_seconds))
        except (TypeError, ValueError):
            end = None
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
