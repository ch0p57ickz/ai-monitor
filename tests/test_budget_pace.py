"""Tests for OpenRouter budget pace and same-account spend deltas.

Covers task 2.5: exact budget pace requires a key limit and exact reset-period
boundaries; a `limit_reset: "monthly"` label without a timestamp must not
produce an exact pace; and locally observed, same-account spend is signalled
without mixing accounts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.pace import (
    PaceSettings,
    compute_budget_pace,
    enrich_observation,
)
from ai_monitor.state import UsageWorker
from ai_monitor.sources.registry import (
    SourceAdapter,
    parse_openrouter_key_usage,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _unknown() -> dict:
    return {
        "classification": "unknown",
        "basis": "none",
        "expectedUsedPercent": None,
    }


class TestExactBoundaries:
    def test_monthly_without_timestamp_is_not_exact(self):
        budget = {
            "id": "current-key",
            "limit_usd": 50.0,
            "used_usd": 10.0,
            "reset_period": "monthly",
        }
        pace = compute_budget_pace(budget, now_iso="2026-03-16T00:00:00+00:00")
        assert pace == _unknown()

    def test_timestamp_without_a_period_is_not_exact(self):
        budget = {
            "id": "current-key",
            "limit_usd": 50.0,
            "used_usd": 10.0,
            "resets_at": "2026-04-01T00:00:00+00:00",
        }
        pace = compute_budget_pace(budget, now_iso="2026-03-16T00:00:00+00:00")
        assert pace == _unknown()

    def test_unrecognized_period_is_not_exact(self):
        budget = {
            "id": "current-key",
            "limit_usd": 50.0,
            "used_usd": 10.0,
            "resets_at": "2026-04-01T00:00:00+00:00",
            "reset_period": "fortnightly",
        }
        assert compute_budget_pace(
            budget, now_iso="2026-03-16T00:00:00+00:00"
        ) == _unknown()

    def test_monthly_with_timestamp_computes_an_elapsed_fraction(self):
        budget = {
            "id": "current-key",
            "limit_usd": 50.0,
            "used_usd": 10.0,
            "resets_at": "2026-04-01T00:00:00+00:00",
            "reset_period": "monthly",
        }
        pace = compute_budget_pace(budget, now_iso="2026-03-16T00:00:00+00:00")
        # 2026-03-01 -> 2026-04-01 is 31 days; 15 elapsed days is 48.39%.
        assert pace["expectedUsedPercent"] == 48.39
        assert pace["classification"] == "under"  # 10/50 = 20%
        assert pace["basis"] == "estimated"

    def test_weekly_over_spend(self):
        now = datetime(2026, 3, 16, tzinfo=timezone.utc)
        budget = {
            "id": "current-key",
            "limit_usd": 50.0,
            "used_usd": 45.0,
            "resets_at": _iso(now + timedelta(days=3)),
            "reset_period": "weekly",
        }
        pace = compute_budget_pace(budget, now_iso=_iso(now))
        # 4 of 7 days elapsed = 57.14%; 45/50 = 90% is clearly over.
        assert pace["expectedUsedPercent"] == 57.14
        assert pace["classification"] == "over"

    def test_daily_at_the_boundary_is_at(self):
        now = datetime(2026, 3, 16, 12, tzinfo=timezone.utc)
        budget = {
            "id": "current-key",
            "limit_usd": 20.0,
            "used_usd": 10.0,
            "resets_at": _iso(now + timedelta(hours=12)),
            "reset_period": "daily",
        }
        pace = compute_budget_pace(budget, now_iso=_iso(now))
        assert pace["classification"] == "at"
        assert pace["expectedUsedPercent"] == 50.0

    def test_expired_reset_is_not_a_live_window(self):
        now = datetime(2026, 3, 16, tzinfo=timezone.utc)
        budget = {
            "id": "current-key",
            "limit_usd": 50.0,
            "used_usd": 10.0,
            "resets_at": _iso(now - timedelta(days=1)),
            "reset_period": "monthly",
        }
        assert compute_budget_pace(budget, now_iso=_iso(now)) == _unknown()

    @pytest.mark.parametrize(
        "budget",
        [
            {"limit_usd": None, "used_usd": 5.0},
            {"limit_usd": 0.0, "used_usd": 5.0},
            {"limit_usd": 50.0, "used_usd": None},
        ],
    )
    def test_insufficient_budget_data_is_unknown(self, budget):
        assert compute_budget_pace(
            dict(budget, id="k", resets_at="2026-04-01T00:00:00+00:00",
                 reset_period="monthly"),
            now_iso="2026-03-16T00:00:00+00:00",
        ) == _unknown()


class TestSpendEvidence:
    NOW = "2026-03-16T12:00:00+00:00"

    def _budget(self, used: float = 40.0) -> dict:
        return {
            "id": "current-key",
            "limit_usd": 100.0,
            "used_usd": used,
            "resets_at": "2026-04-01T00:00:00+00:00",
            "reset_period": "monthly",
        }

    def test_consecutive_same_limit_series_is_reported(self):
        history = [
            {"collected_at": "2026-03-16T11:40:00+00:00", "used_usd": 25.0, "limit_usd": 100.0},
            {"collected_at": "2026-03-16T11:50:00+00:00", "used_usd": 30.0, "limit_usd": 100.0},
        ]
        pace = compute_budget_pace(self._budget(), now_iso=self.NOW, history=history)
        assert pace["basis"] == "reported"
        assert pace["classification"] == "under"

    def test_flat_series_is_not_reported(self):
        history = [
            {"collected_at": "2026-03-16T11:40:00+00:00", "used_usd": 40.0, "limit_usd": 100.0},
            {"collected_at": "2026-03-16T11:50:00+00:00", "used_usd": 40.0, "limit_usd": 100.0},
        ]
        pace = compute_budget_pace(self._budget(), now_iso=self.NOW, history=history)
        assert pace["basis"] == "estimated"

    def test_sparse_series_is_not_reported(self):
        history = [
            {"collected_at": "2026-03-16T08:00:00+00:00", "used_usd": 10.0, "limit_usd": 100.0},
            {"collected_at": "2026-03-16T11:50:00+00:00", "used_usd": 30.0, "limit_usd": 100.0},
        ]
        pace = compute_budget_pace(self._budget(), now_iso=self.NOW, history=history)
        assert pace["basis"] == "estimated"

    def test_changed_limit_invalidates_the_series(self):
        history = [
            {"collected_at": "2026-03-16T11:40:00+00:00", "used_usd": 5.0, "limit_usd": 200.0},
            {"collected_at": "2026-03-16T11:50:00+00:00", "used_usd": 8.0, "limit_usd": 200.0},
        ]
        pace = compute_budget_pace(self._budget(), now_iso=self.NOW, history=history)
        assert pace["basis"] == "estimated"

    def test_reported_evidence_without_boundaries_still_reports_basis(self):
        budget = {
            "id": "current-key",
            "limit_usd": 100.0,
            "used_usd": 40.0,
            "reset_period": "monthly",  # no timestamp
        }
        history = [
            {"collected_at": "2026-03-16T11:40:00+00:00", "used_usd": 25.0, "limit_usd": 100.0},
            {"collected_at": "2026-03-16T11:50:00+00:00", "used_usd": 30.0, "limit_usd": 100.0},
        ]
        pace = compute_budget_pace(budget, now_iso=self.NOW, history=history)
        assert pace["classification"] == "unknown"
        assert pace["expectedUsedPercent"] is None
        assert pace["basis"] == "reported"

    def test_short_series_is_not_reported(self):
        history = [
            {"collected_at": "2026-03-16T11:59:30+00:00", "used_usd": 39.0, "limit_usd": 100.0}
        ]
        pace = compute_budget_pace(self._budget(), now_iso=self.NOW, history=history)
        assert pace["basis"] == "estimated"

    def test_stale_series_is_not_reported(self):
        # The newest point is far older than now, so it is not live evidence.
        history = [
            {"collected_at": "2026-03-16T09:00:00+00:00", "used_usd": 25.0, "limit_usd": 100.0},
            {"collected_at": "2026-03-16T09:10:00+00:00", "used_usd": 30.0, "limit_usd": 100.0},
        ]
        pace = compute_budget_pace(self._budget(used=40.0), now_iso=self.NOW, history=history)
        assert pace["basis"] == "estimated"

    def test_limit_round_trip_tolerance(self):
        # A limit with more than 4 decimals is persisted rounded ("12.3457"),
        # which must not invalidate the whole series.
        budget = self._budget(used=6.0)
        budget["limit_usd"] = 12.345678
        history = [
            {"collected_at": "2026-03-16T11:40:00+00:00", "used_usd": 3.0, "limit_usd": 12.3457},
            {"collected_at": "2026-03-16T11:50:00+00:00", "used_usd": 4.0, "limit_usd": 12.3457},
        ]
        pace = compute_budget_pace(budget, now_iso=self.NOW, history=history)
        assert pace["basis"] == "reported"


class TestMonthShift:
    """The monthly period start is derived by shifting a calendar month."""

    def test_month_end_is_clamped(self):
        from ai_monitor.pace import _shift_months

        assert _shift_months(
            datetime(2026, 3, 31, tzinfo=timezone.utc), -1
        ) == datetime(2026, 2, 28, tzinfo=timezone.utc)

    def test_year_rolls_over(self):
        from ai_monitor.pace import _shift_months

        assert _shift_months(
            datetime(2026, 1, 31, tzinfo=timezone.utc), -1
        ) == datetime(2025, 12, 31, tzinfo=timezone.utc)

    def test_leap_year_is_handled(self):
        from ai_monitor.pace import _shift_months

        assert _shift_months(
            datetime(2028, 3, 31, tzinfo=timezone.utc), -1
        ) == datetime(2028, 2, 29, tzinfo=timezone.utc)


class TestEnrichBudgets:
    def test_enrich_observation_fills_budget_pace(self):
        raw = {
            "status": "success",
            "quota_windows": [],
            "budgets": [
                {
                    "id": "current-key",
                    "scope": "key",
                    "limit_usd": 50.0,
                    "used_usd": 10.0,
                    "reset_period": "monthly",
                }
            ],
        }
        out = enrich_observation(
            None, raw, now_iso="2026-03-16T00:00:00+00:00"  # type: ignore[arg-type]
        )
        assert out["budgets"][0]["pace"] == _unknown()
        # The input payload is not mutated in place.
        assert "pace" not in raw["budgets"][0]

    def test_budget_reset_period_is_not_serialized(self):
        source = get_source_def("openrouter-key")
        raw = {
            "status": "success",
            "budgets": [
                {
                    "id": "current-key",
                    "scope": "key",
                    "limit_usd": 50.0,
                    "used_usd": 10.0,
                    "resets_at": "2026-04-01T00:00:00+00:00",
                    "reset_period": "monthly",
                    "pace": _unknown(),
                }
            ],
        }
        obs = normalize_observation(source, raw)
        serialized = obs.budgets[0].to_dict()
        assert set(serialized) == {
            "id",
            "scope",
            "totalCreditsUsd",
            "totalUsageUsd",
            "remainingUsd",
            "limitUsd",
            "usedUsd",
            "resetsAt",
            "pace",
        }
        assert serialized["pace"] == _unknown()


class TestKeyParserFields:
    def test_reset_period_and_timestamp_are_mapped(self):
        budget = parse_openrouter_key_usage(
            {
                "data": {
                    "limit": 50.0,
                    "usage": 10.0,
                    "limit_remaining": 40.0,
                    "limit_reset": "monthly",
                }
            }
        )[0]
        assert budget["reset_period"] == "monthly"
        assert budget["resets_at"] is None

    def test_exact_timestamp_is_mapped_when_present(self):
        budget = parse_openrouter_key_usage(
            {
                "data": {
                    "limit": 50.0,
                    "usage": 10.0,
                    "limit_reset": "monthly",
                    "limit_reset_at": "2026-04-01T00:00:00+00:00",
                }
            }
        )[0]
        assert budget["resets_at"] == "2026-04-01T00:00:00+00:00"

    def test_monthly_label_alone_yields_unknown_pace_end_to_end(self):
        source = get_source_def("openrouter-key")
        raw = {
            "status": "success",
            "budgets": parse_openrouter_key_usage(
                {
                    "data": {
                        "limit": 50.0,
                        "usage": 10.0,
                        "limit_remaining": 40.0,
                        "limit_reset": "monthly",
                    }
                }
            ),
        }
        raw = enrich_observation(source, raw, now_iso="2026-03-16T00:00:00+00:00")
        obs = normalize_observation(source, raw)
        assert obs.budgets[0].pace == _unknown()
        # Known balance is still presented alongside the unknown pace.
        assert obs.budgets[0].remaining_usd == 40.0


class _BudgetAdapter(SourceAdapter):
    """Returns a key-scope budget with a caller-controlled used amount."""

    used: float = 10.0
    account_id: str | None = "acct-1"

    def fetch(self, config: Config) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "status": "success",
            "account_id": self.account_id,
            "budgets": [
                {
                    "id": "current-key",
                    "scope": "key",
                    "limit_usd": 100.0,
                    "used_usd": self.used,
                    "resets_at": _iso(now + timedelta(days=10)),
                    "reset_period": "monthly",
                }
            ],
        }


class TestWorkerBudgetHistory:
    def _worker(self, sample_config: Config, sample_db, adapter_cls):
        worker = UsageWorker(
            config=sample_config,
            db=sample_db,
            registry={"openrouter-key": adapter_cls},
        )
        # Relax the evidence gate so two back-to-back polls can qualify.
        worker._pace_settings = PaceSettings(min_span_seconds=0.0, max_gap_seconds=10**9)
        return worker

    def test_same_account_spend_delta_yields_reported_basis(
        self, sample_config: Config, sample_db
    ):
        worker = self._worker(sample_config, sample_db, _BudgetAdapter)

        _BudgetAdapter.used = 10.0
        worker._poll_source(worker._workers["openrouter-key"])
        _BudgetAdapter.used = 25.0
        worker._poll_source(worker._workers["openrouter-key"])

        obs = worker._workers["openrouter-key"].last_observation
        assert obs.budgets[0].pace["basis"] == "reported"

    def test_no_account_id_means_no_spend_series(self, sample_config: Config, sample_db):
        class NoAccount(_BudgetAdapter):
            account_id = None

        worker = self._worker(sample_config, sample_db, NoAccount)
        NoAccount.used = 10.0
        worker._poll_source(worker._workers["openrouter-key"])
        NoAccount.used = 25.0
        worker._poll_source(worker._workers["openrouter-key"])

        obs = worker._workers["openrouter-key"].last_observation
        # Without an account id, cross-snapshot spend must not be combined.
        assert obs.budgets[0].pace["basis"] == "estimated"

    def test_account_change_does_not_inherit_prior_spend(
        self, sample_config: Config, sample_db
    ):
        worker = self._worker(sample_config, sample_db, _BudgetAdapter)

        _BudgetAdapter.account_id = "acct-1"
        _BudgetAdapter.used = 10.0
        worker._poll_source(worker._workers["openrouter-key"])
        _BudgetAdapter.used = 25.0
        worker._poll_source(worker._workers["openrouter-key"])
        assert (
            worker._workers["openrouter-key"].last_observation.budgets[0].pace["basis"]
            == "reported"
        )

        # A different account must not inherit the previous account's series.
        _BudgetAdapter.account_id = "acct-2"
        _BudgetAdapter.used = 60.0
        worker._poll_source(worker._workers["openrouter-key"])
        obs = worker._workers["openrouter-key"].last_observation
        assert obs.account_id == "acct-2"
        assert obs.budgets[0].pace["basis"] == "estimated"
