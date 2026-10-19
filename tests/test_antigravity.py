"""Tests for the Antigravity adapter and supported-bucket parsing.

Covers task 3.2: capability-aware discovery, retention of reported scope,
group, and model values, no fabricated weekly window for a session-only
variant, and `unavailable` for an incompatible install.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.sources import registry
from ai_monitor.sources.registry import (
    AntigravityAdapter,
    CollectionError,
    parse_antigravity_usage,
)

ANTIGRAVITY = get_source_def("antigravity")


# ---------------------------------------------------------------------------
# Parser: session-only variant
# ---------------------------------------------------------------------------


class TestSessionOnly:
    def test_retains_session_data_without_a_weekly_window(self):
        payload = {
            "buckets": [
                {
                    "scope": "session",
                    "group": "workspace-alpha",
                    "model": "claude-sonnet-4",
                }
            ]
        }

        windows = parse_antigravity_usage(payload)

        assert len(windows) == 1
        window = windows[0]
        assert window["scope"] == "session"
        assert window["group"] == "workspace-alpha"
        assert window["model"] == "claude-sonnet-4"
        # No percentage was reported, so it stays unknown rather than zero.
        assert window["used_percent"] is None
        assert window["remaining_percent"] is None
        # A weekly window is never added when the variant does not report one.
        assert all(w["scope"] != "weekly" for w in windows)

    def test_reports_session_percentage_when_present(self):
        payload = {
            "buckets": [
                {"scope": "session", "group": "g", "usedPercent": 12.5}
            ]
        }
        window = parse_antigravity_usage(payload)[0]
        assert window["used_percent"] == 12.5

    def test_reported_zero_is_preserved(self):
        payload = {"buckets": [{"scope": "session", "used_percent": 0}]}
        window = parse_antigravity_usage(payload)[0]
        assert window["used_percent"] == 0.0


class TestSupportedScopes:
    def test_reported_weekly_window_is_retained(self):
        payload = {
            "buckets": [
                {"scope": "session", "group": "g"},
                {"scope": "weekly", "group": "g", "used_percent": 80.0},
            ]
        }
        windows = parse_antigravity_usage(payload)
        assert [w["scope"] for w in windows] == ["session", "weekly"]

    @pytest.mark.parametrize(
        "entry, expected",
        [
            ({"kind": "WEEKLY"}, "weekly"),
            ({"type": "Session"}, "session"),
            ({"bucket": "team"}, "group"),
            ({"window": "model"}, "model"),
            ({"scope": "org"}, "group"),
        ],
    )
    def test_scope_aliases(self, entry, expected):
        payload = {"buckets": [dict(entry, used_percent=1.0)]}
        assert parse_antigravity_usage(payload)[0]["scope"] == expected

    def test_unknown_scope_is_kept_verbatim_not_invented(self):
        payload = {"buckets": [{"scope": "experimental-pool", "used_percent": 5.0}]}
        assert parse_antigravity_usage(payload)[0]["scope"] == "experimental-pool"

    def test_keyed_buckets_inherit_the_bucket_name(self):
        payload = {
            "buckets": {
                "session": {"group": "g", "used_percent": 3.0},
                "weekly": {"group": "g", "used_percent": 40.0},
            }
        }
        windows = parse_antigravity_usage(payload)
        assert [w["scope"] for w in windows] == ["session", "weekly"]

    def test_bare_list_payload(self):
        windows = parse_antigravity_usage([{"scope": "session", "used_percent": 1.0}])
        assert windows[0]["scope"] == "session"

    def test_missing_numeric_fields_stay_unknown(self):
        payload = {
            "buckets": [
                {"scope": "session", "used_percent": "n/a", "durationMinutes": float("nan")}
            ]
        }
        window = parse_antigravity_usage(payload)[0]
        assert window["used_percent"] is None
        assert window["duration_minutes"] is None

    def test_window_ids_are_unique_and_prefixed(self):
        payload = {
            "buckets": [
                {"id": "a", "scope": "session"},
                {"id": "b", "scope": "weekly"},
            ]
        }
        ids = [w["id"] for w in parse_antigravity_usage(payload)]
        assert ids == ["antigravity:a", "antigravity:b"]

    def test_id_less_buckets_with_the_same_identity_do_not_collide(self):
        payload = {"buckets": [{"scope": "session"}, {"scope": "session"}]}
        ids = [w["id"] for w in parse_antigravity_usage(payload)]
        assert len(ids) == 2
        assert len(set(ids)) == 2
        assert ids[0] == "antigravity:session"
        assert ids[1] == "antigravity:session#1"

    def test_id_less_buckets_with_distinct_identity_stay_stable(self):
        payload = {
            "buckets": [
                {"scope": "session", "group": "a"},
                {"scope": "session", "group": "b"},
            ]
        }
        ids = [w["id"] for w in parse_antigravity_usage(payload)]
        assert ids == ["antigravity:session:a", "antigravity:session:b"]

    def test_bare_usage_banner_is_not_treated_as_incompatible(self):
        # A transient crash may print the usage banner; it must stay retryable.
        assert "usage: agy" not in registry._ANTIGRAVITY_INCOMPATIBLE_MARKERS

    def test_null_scope_does_not_mask_a_later_alias(self):
        payload = {"buckets": [{"scope": None, "kind": "weekly", "used_percent": 1.0}]}
        assert parse_antigravity_usage(payload)[0]["scope"] == "weekly"

    def test_empty_primary_container_falls_through_to_a_later_one(self):
        payload = {"buckets": [], "quotaWindows": [{"scope": "session"}]}
        assert parse_antigravity_usage(payload)[0]["scope"] == "session"


class TestIncompatible:
    @pytest.mark.parametrize(
        "payload",
        [None, {}, {"buckets": []}, {"buckets": {}}, {"buckets": "nope"}, "nope", 7],
    )
    def test_no_buckets_is_unavailable(self, payload):
        with pytest.raises(CollectionError) as exc_info:
            parse_antigravity_usage(payload)
        assert exc_info.value.category == "unavailable"

    def test_non_dict_entries_are_skipped(self):
        payload = {"buckets": ["nope", {"scope": "session"}]}
        windows = parse_antigravity_usage(payload)
        assert len(windows) == 1


class TestNormalization:
    def test_session_windows_normalize(self):
        raw = {
            "status": "success",
            "quota_windows": parse_antigravity_usage(
                {"buckets": [{"scope": "session", "group": "g", "model": "m"}]}
            ),
        }
        obs = normalize_observation(ANTIGRAVITY, raw)
        assert len(obs.quota_windows) == 1
        assert obs.quota_windows[0].scope == "session"
        assert obs.quota_windows[0].group == "g"
        assert obs.quota_windows[0].used_percent is None


# ---------------------------------------------------------------------------
# Adapter: discovery and failure classification
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, bin_path: str = "/usr/bin/agy") -> Config:
    return Config(
        data_dir=str(tmp_path / "data"),
        antigravity_bin=bin_path,
        antigravity_timeout_seconds=5,
    )


def _fake_run(monkeypatch, *, returncode=0, stdout="", stderr=""):
    def fake(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(registry.subprocess, "run", fake)


class TestAdapter:
    def test_success_reports_windows_and_scopes(self, tmp_path, monkeypatch):
        payload = {
            "buckets": [
                {"scope": "session", "group": "g", "used_percent": 12.0},
                {"scope": "weekly", "group": "g"},
            ]
        }
        _fake_run(monkeypatch, stdout=json.dumps(payload))

        out = AntigravityAdapter().fetch(_config(tmp_path))

        assert out["status"] == "success"
        assert out["notes"]["scopes"] == ["session", "weekly"]
        assert [w["scope"] for w in out["quota_windows"]] == ["session", "weekly"]

    def test_no_bin_is_unavailable(self, tmp_path):
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path, bin_path=""))
        assert exc_info.value.category == "unavailable"

    def test_missing_binary_is_unavailable(self, tmp_path, monkeypatch):
        def fake(*args, **kwargs):
            raise FileNotFoundError("agy")

        monkeypatch.setattr(registry.subprocess, "run", fake)
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "unavailable"

    def test_timeout(self, tmp_path, monkeypatch):
        def fake(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="agy", timeout=5)

        monkeypatch.setattr(registry.subprocess, "run", fake)
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "timeout"

    @pytest.mark.parametrize(
        "stderr",
        [
            "unknown command 'usage'",
            "error: unrecognized arguments: --json",
            'Error: unexpected argument "usage".',
        ],
    )
    def test_unsupported_subcommand_is_unavailable(self, tmp_path, monkeypatch, stderr):
        _fake_run(monkeypatch, returncode=2, stderr=stderr)
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "unavailable"

    def test_unexpected_crash_is_internal(self, tmp_path, monkeypatch):
        _fake_run(monkeypatch, returncode=1, stderr="segmentation fault")
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "internal"

    def test_empty_stdout_is_unavailable(self, tmp_path, monkeypatch):
        _fake_run(monkeypatch, returncode=0, stdout="   ")
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "unavailable"

    def test_non_json_stdout_is_invalid_response(self, tmp_path, monkeypatch):
        _fake_run(monkeypatch, returncode=0, stdout="<html>")
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "invalid_response"

    def test_payload_without_buckets_is_unavailable(self, tmp_path, monkeypatch):
        _fake_run(monkeypatch, returncode=0, stdout=json.dumps({"status": "ok"}))
        with pytest.raises(CollectionError) as exc_info:
            AntigravityAdapter().fetch(_config(tmp_path))
        assert exc_info.value.category == "unavailable"
