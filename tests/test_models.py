"""Unit tests for ai_monitor.models — normalization, dataclasses, and source defs."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ai_monitor.models import (
    SOURCE_DEFS,
    Budget,
    QuotaWindow,
    SourceStatus,
    get_source_def,
    normalize_observation,
)

CODEX = get_source_def("codex")
ANTIGRAVITY = get_source_def("antigravity")
OPENROUTER_ACC = get_source_def("openrouter-account")
OPENROUTER_KEY = get_source_def("openrouter-key")


class TestSourceDefs:
    def test_all_sources_present(self):
        sources = [s.source for s in SOURCE_DEFS]
        assert "codex" in sources
        assert "antigravity" in sources
        assert "openrouter-account" in sources
        assert "openrouter-key" in sources

    def test_get_source_def_known(self):
        codex = get_source_def("codex")
        assert codex is not None
        assert codex.provider == "codex"
        assert codex.label == "Codex"
        assert codex.window == "rolling_1h"
        assert codex.cadence_seconds == 60

    def test_get_source_def_unknown(self):
        assert get_source_def("nonexistent") is None

    def test_get_source_def_openrouter_key(self):
        or_key = get_source_def("openrouter-key")
        assert or_key is not None
        assert or_key.cadence_seconds == 300


def _raw_codex() -> dict:
    return {
        "status": "success",
        "quota_windows": [
            {
                "id": "w1",
                "label": "Weekly",
                "scope": "account",
                "group": "default",
                "model": "gpt-5.4",
                "used_percent": 42.0,
                "remaining_percent": 58.0,
                "duration_minutes": 10080,
                "resets_at": "2026-10-25T00:00:00Z",
            }
        ],
    }


def _raw_antigravity() -> dict:
    return {
        "status": "success",
        "quota_windows": [{"id": "a1", "label": "Session", "used_percent": 10.0}],
    }


def _raw_openrouter_account() -> dict:
    return {
        "status": "success",
        "budgets": [{"id": "b1", "limit_usd": 100.0, "used_usd": 30.0, "remaining_usd": 70.0}],
    }


def _raw_openrouter_key() -> dict:
    return {
        "status": "success",
        "budgets": [{"id": "b1", "limit_usd": 50.0, "used_usd": 10.0, "remaining_usd": 40.0}],
    }


class TestNormalizeObservation:
    def test_codex_success(self):
        obs = normalize_observation(CODEX, _raw_codex())
        assert obs.source == "codex"
        assert obs.status == SourceStatus.SUCCESS
        assert obs.collected_at
        assert len(obs.quota_windows) == 1
        w = obs.quota_windows[0]
        assert w.id == "w1"
        assert w.used_percent == 42.0
        assert w.remaining_percent == 58.0

    def test_antigravity_success(self):
        obs = normalize_observation(ANTIGRAVITY, _raw_antigravity())
        assert obs.source == "antigravity"
        assert obs.status == SourceStatus.SUCCESS
        assert len(obs.quota_windows) == 1
        assert obs.quota_windows[0].id == "a1"

    def test_openrouter_account_success(self):
        obs = normalize_observation(OPENROUTER_ACC, _raw_openrouter_account())
        assert obs.source == "openrouter-account"
        assert obs.status == SourceStatus.SUCCESS
        assert len(obs.budgets) == 1
        b = obs.budgets[0]
        assert b.limit_usd == 100.0
        assert b.used_usd == 30.0

    def test_openrouter_key_success(self):
        obs = normalize_observation(OPENROUTER_KEY, _raw_openrouter_key())
        assert obs.source == "openrouter-key"
        assert obs.status == SourceStatus.SUCCESS
        assert len(obs.budgets) == 1
        assert obs.budgets[0].limit_usd == 50.0

    def test_missing_fields_are_null_not_zero(self):
        raw = {"status": "success", "quota_windows": [{"id": "w1", "label": "Weekly"}]}
        obs = normalize_observation(CODEX, raw)
        assert obs.quota_windows[0].used_percent is None
        assert obs.quota_windows[0].remaining_percent is None

    def test_empty_raw_gives_no_windows(self):
        obs = normalize_observation(CODEX, {})
        assert obs.quota_windows == []
        assert obs.budgets == []

    def test_status_is_success(self):
        obs = normalize_observation(CODEX, {"status": "success", "quota_windows": []})
        assert obs.status == SourceStatus.SUCCESS

    def test_error_status(self):
        obs = normalize_observation(CODEX, {"status": "error", "error_category": "auth"})
        assert obs.status == SourceStatus.ERROR

    def test_unavailable_status(self):
        obs = normalize_observation(CODEX, {"status": "unavailable"})
        assert obs.status == SourceStatus.UNAVAILABLE


class TestQuotaWindow:
    def test_to_dict_roundtrip(self):
        w = QuotaWindow(
            id="w1",
            label="Weekly",
            scope="account",
            group="default",
            model="gpt-5.4",
            used_percent=50.0,
            remaining_percent=50.0,
            duration_minutes=10080,
            resets_at="2026-10-25T00:00:00Z",
        )
        d = w.to_dict()
        assert d["id"] == "w1"
        assert d["usedPercent"] == 50.0
        assert d["model"] == "gpt-5.4"

    def test_pace_insufficient_data(self):
        w = QuotaWindow(id="w1", label="Test")
        assert w.pace["classification"] == "unknown"
        assert w.pace["basis"] == "none"

    def test_pace_zero_duration(self):
        w = QuotaWindow(id="w1", label="Test", duration_minutes=0)
        assert w.pace["classification"] == "unknown"


class TestBudget:
    def test_to_dict_integer_values(self):
        b = Budget(id="b1", scope="account", limit_usd=100.0, used_usd=25.0)
        d = b.to_dict()
        assert d["limitUsd"] == "100"
        assert d["usedUsd"] == "25"

    def test_to_dict_float_values(self):
        b = Budget(id="b1", scope="account", limit_usd=50.5, used_usd=10.25)
        d = b.to_dict()
        assert d["limitUsd"] == "50.5"
        assert d["usedUsd"] == "10.25"


class TestSourceStatus:
    def test_values(self):
        assert SourceStatus.SUCCESS.value == "success"
        assert SourceStatus.UNAVAILABLE.value == "unavailable"
        assert SourceStatus.ERROR.value == "error"
