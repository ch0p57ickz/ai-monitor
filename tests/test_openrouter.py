"""Tests for the OpenRouter account-credit and current-key adapters.

Covers task 3.3: the two endpoints are independent result streams, remaining
account credits are derived only from both inputs, key fields are mapped
separately, and absent fields stay unknown.
"""

from __future__ import annotations

import dataclasses

import pytest

from ai_monitor.config import Config
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.state import UsageWorker
from ai_monitor.sources import registry
from ai_monitor.sources.registry import (
    CollectionError,
    OpenRouterAccountAdapter,
    OpenRouterKeyAdapter,
    parse_openrouter_account_credits,
    parse_openrouter_key_usage,
)


class TestAccountCreditsParser:
    def test_derives_remaining_when_both_present(self):
        budgets = parse_openrouter_account_credits(
            {"data": {"total_credits": 10.0, "total_usage": 2.5}}
        )
        assert len(budgets) == 1
        budget = budgets[0]
        assert budget["scope"] == "account"
        assert budget["total_credits_usd"] == 10.0
        assert budget["total_usage_usd"] == 2.5
        assert budget["remaining_usd"] == 7.5
        assert budget["limit_usd"] is None
        assert budget["used_usd"] is None

    @pytest.mark.parametrize(
        "data",
        [
            {"total_credits": 10.0},  # usage missing
            {"total_usage": 2.5},  # credits missing
            {},  # both missing
        ],
    )
    def test_never_fabricates_remaining(self, data):
        budget = parse_openrouter_account_credits({"data": data})[0]
        assert budget["remaining_usd"] is None

    def test_non_numeric_values_become_none(self):
        budget = parse_openrouter_account_credits(
            {"data": {"total_credits": "n/a", "total_usage": float("nan")}}
        )[0]
        assert budget["total_credits_usd"] is None
        assert budget["total_usage_usd"] is None
        assert budget["remaining_usd"] is None


class TestKeyUsageParser:
    def test_maps_reported_fields(self):
        budget = parse_openrouter_key_usage(
            {"data": {"limit": 50.0, "usage": 10.0, "limit_remaining": 40.0}}
        )[0]
        assert budget["scope"] == "key"
        assert budget["limit_usd"] == 50.0
        assert budget["used_usd"] == 10.0
        assert budget["remaining_usd"] == 40.0
        assert budget["total_credits_usd"] is None

    def test_absent_fields_stay_unknown(self):
        budget = parse_openrouter_key_usage({"data": {}})[0]
        assert budget["limit_usd"] is None
        assert budget["used_usd"] is None
        assert budget["remaining_usd"] is None

    def test_does_not_infer_remaining_from_limit_minus_usage(self):
        # The provider reports limit/usage but not limit_remaining.
        budget = parse_openrouter_key_usage(
            {"data": {"limit": 50.0, "usage": 10.0}}
        )[0]
        assert budget["remaining_usd"] is None


class TestEnvelope:
    @pytest.mark.parametrize(
        "parser",
        [parse_openrouter_account_credits, parse_openrouter_key_usage],
    )
    @pytest.mark.parametrize(
        "payload",
        [None, {}, {"data": None}, {"data": []}, "nope"],
    )
    def test_missing_data_object_is_invalid_response(self, parser, payload):
        with pytest.raises(CollectionError) as exc_info:
            parser(payload)
        assert exc_info.value.category == "invalid_response"


class TestSeparation:
    def test_account_and_key_budgets_are_independent(self):
        account = parse_openrouter_account_credits(
            {"data": {"total_credits": 10.0, "total_usage": 2.5}}
        )[0]
        key = parse_openrouter_key_usage(
            {"data": {"limit": 50.0, "usage": 10.0, "limit_remaining": 40.0}}
        )[0]
        assert account["scope"] == "account"
        assert key["scope"] == "key"
        # Account remaining is credits-usage (7.5); key remaining is reported
        # (40). Neither is the sum of the two streams.
        assert account["remaining_usd"] == 7.5
        assert key["remaining_usd"] == 40.0


class TestReportedZero:
    def test_zero_credits_is_reported_not_unknown(self):
        budget = parse_openrouter_account_credits(
            {"data": {"total_credits": 0, "total_usage": 0}}
        )[0]
        assert budget["total_credits_usd"] == 0.0
        assert budget["total_usage_usd"] == 0.0
        assert budget["remaining_usd"] == 0.0

    def test_zero_key_values_are_reported(self):
        budget = parse_openrouter_key_usage(
            {"data": {"limit": 0, "usage": 0, "limit_remaining": 0}}
        )[0]
        assert budget["limit_usd"] == 0.0
        assert budget["used_usd"] == 0.0
        assert budget["remaining_usd"] == 0.0


class TestBudgetIds:
    def test_budget_ids_are_stable(self):
        account = parse_openrouter_account_credits({"data": {}})[0]
        key = parse_openrouter_key_usage({"data": {}})[0]
        assert account["id"] == "account-credits"
        assert key["id"] == "current-key"


class TestHttpErrorMapping:
    def _cfg(self, sample_config: Config) -> Config:
        return dataclasses.replace(sample_config, openrouter_api_key="sk-test")

    @pytest.mark.parametrize(
        "code, category",
        [
            (401, "authentication"),
            (403, "authorization"),
            (429, "rate_limited"),
            (500, "internal"),
        ],
    )
    def test_http_error_categories(self, sample_config, monkeypatch, code, category):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)

        monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == category

    def test_non_json_body_is_invalid_response(self, sample_config, monkeypatch):
        import io

        monkeypatch.setattr(
            registry.urllib.request,
            "urlopen",
            lambda req, timeout=None: io.BytesIO(b"<html>gateway error</html>"),
        )
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == "invalid_response"

    def test_connect_phase_timeout_is_timeout(self, sample_config, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == "timeout"

    def test_network_error(self, sample_config, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == "network"

    def test_read_phase_timeout_is_timeout(self, sample_config, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("read timed out")

        monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == "timeout"

    def test_remote_disconnect_is_network(self, sample_config, monkeypatch):
        import http.client

        def fake_urlopen(req, timeout=None):
            raise http.client.RemoteDisconnected("remote closed connection")

        monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == "network"

    def test_incomplete_read_is_invalid_response(self, sample_config, monkeypatch):
        import http.client

        def fake_urlopen(req, timeout=None):
            raise http.client.IncompleteRead(b"partial")

        monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(CollectionError) as exc_info:
            OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert exc_info.value.category == "invalid_response"


class TestAdapters:
    def _cfg(self, sample_config: Config) -> Config:
        return dataclasses.replace(sample_config, openrouter_api_key="sk-test")

    def test_account_adapter_uses_credits_endpoint(self, sample_config, monkeypatch):
        seen: dict[str, str] = {}

        def fake_request(url, headers, timeout):
            seen["url"] = url
            return {"data": {"total_credits": 10.0, "total_usage": 2.5}}

        monkeypatch.setattr(registry, "_openrouter_request", fake_request)
        out = OpenRouterAccountAdapter().fetch(self._cfg(sample_config))
        assert seen["url"].endswith("/api/v1/credits")
        assert out["status"] == "success"
        assert out["budgets"][0]["remaining_usd"] == 7.5

    def test_key_adapter_uses_key_endpoint(self, sample_config, monkeypatch):
        seen: dict[str, str] = {}

        def fake_request(url, headers, timeout):
            seen["url"] = url
            return {"data": {"limit": 50.0, "usage": 10.0}}

        monkeypatch.setattr(registry, "_openrouter_request", fake_request)
        out = OpenRouterKeyAdapter().fetch(self._cfg(sample_config))
        assert seen["url"].endswith("/api/v1/key")
        assert out["budgets"][0]["scope"] == "key"

    def test_missing_key_is_not_configured(self, sample_config):
        for adapter in (OpenRouterAccountAdapter(), OpenRouterKeyAdapter()):
            with pytest.raises(CollectionError) as exc_info:
                adapter.fetch(sample_config)
            assert exc_info.value.category == "not_configured"

    def test_budgets_normalize_into_observation(self):
        source = get_source_def("openrouter-account")
        raw = {
            "status": "success",
            "budgets": parse_openrouter_account_credits(
                {"data": {"total_credits": 10.0, "total_usage": 2.5}}
            ),
        }
        obs = normalize_observation(source, raw)
        assert obs.budgets[0].scope == "account"
        assert obs.budgets[0].total_credits_usd == 10.0
        assert obs.budgets[0].remaining_usd == 7.5

    def test_account_adapter_prefers_management_key(self, sample_config, monkeypatch):
        seen: dict[str, str] = {}

        def fake_request(url, headers, timeout):
            seen["auth"] = headers["Authorization"]
            return {"data": {"total_credits": 1.0, "total_usage": 0.0}}

        monkeypatch.setattr(registry, "_openrouter_request", fake_request)
        cfg = dataclasses.replace(
            sample_config,
            openrouter_api_key="sk-inference",
            openrouter_management_key="sk-management",
        )
        OpenRouterAccountAdapter().fetch(cfg)
        assert seen["auth"] == "Bearer sk-management"

    def test_account_adapter_falls_back_to_inference_key(
        self, sample_config, monkeypatch
    ):
        seen: dict[str, str] = {}

        def fake_request(url, headers, timeout):
            seen["auth"] = headers["Authorization"]
            return {"data": {"total_credits": 1.0, "total_usage": 0.0}}

        monkeypatch.setattr(registry, "_openrouter_request", fake_request)
        cfg = dataclasses.replace(sample_config, openrouter_api_key="sk-inference")
        OpenRouterAccountAdapter().fetch(cfg)
        assert seen["auth"] == "Bearer sk-inference"

    def test_key_adapter_sends_inference_key_not_management_key(
        self, sample_config, monkeypatch
    ):
        seen: dict[str, str] = {}

        def fake_request(url, headers, timeout):
            seen["auth"] = headers["Authorization"]
            return {"data": {"limit": 50.0, "usage": 10.0}}

        monkeypatch.setattr(registry, "_openrouter_request", fake_request)
        cfg = dataclasses.replace(
            sample_config,
            openrouter_api_key="sk-inference",
            openrouter_management_key="sk-management",
        )
        OpenRouterKeyAdapter().fetch(cfg)
        assert seen["auth"] == "Bearer sk-inference"


class TestIndependence:
    def test_streams_commit_and_report_health_separately(
        self, sample_config: Config, sample_db
    ):
        cfg = dataclasses.replace(
            sample_config,
            openrouter_api_key="sk-inference",
            openrouter_management_key="sk-management",
        )

        def fake_request(url, headers, timeout):
            if url.endswith("/api/v1/credits"):
                return {"data": {"total_credits": 10.0, "total_usage": 2.5}}
            raise CollectionError("authorization", "management key required")

        import unittest.mock as mock

        with mock.patch.object(registry, "_openrouter_request", fake_request):
            worker = UsageWorker(config=cfg, db=sample_db)
            worker._poll_source(worker._workers["openrouter-account"])
            worker._poll_source(worker._workers["openrouter-key"])
            usage = worker.usage_payload()
            health = {s["id"]: s for s in worker.health_payload()["sources"]}

        # Only the succeeding stream is committed; the failing one adds nothing.
        assert [o["source"] for o in usage["observations"]] == ["openrouter-account"]
        assert usage["observations"][0]["budgets"][0]["remainingUsd"] == "7.5"

        # Each stream has its own health, and the failure is operator-actionable.
        assert health["openrouter-account"]["status"] == "healthy"
        assert health["openrouter-account"]["errorCategory"] is None
        assert health["openrouter-key"]["status"] == "operator_action"
        assert health["openrouter-key"]["errorCategory"] == "authorization"

    def test_last_good_result_is_retained_when_a_stream_starts_failing(
        self, sample_config: Config, sample_db
    ):
        cfg = dataclasses.replace(
            sample_config,
            openrouter_api_key="sk-inference",
            openrouter_management_key="sk-management",
        )
        credits_ok = {"value": True}

        def fake_request(url, headers, timeout):
            if url.endswith("/api/v1/credits"):
                if not credits_ok["value"]:
                    raise CollectionError("rate_limited", "slow down")
                return {"data": {"total_credits": 10.0, "total_usage": 2.5}}
            return {"data": {"limit": 50.0, "usage": 10.0, "limit_remaining": 40.0}}

        import unittest.mock as mock

        with mock.patch.object(registry, "_openrouter_request", fake_request):
            worker = UsageWorker(config=cfg, db=sample_db)
            worker._poll_source(worker._workers["openrouter-account"])
            worker._poll_source(worker._workers["openrouter-key"])
            credits_ok["value"] = False
            worker._poll_source(worker._workers["openrouter-account"])
            usage = worker.usage_payload()
            health = {s["id"]: s for s in worker.health_payload()["sources"]}

        # Both streams keep a committed observation; the failing account stream
        # retains its last good value rather than dropping out.
        assert sorted(o["source"] for o in usage["observations"]) == [
            "openrouter-account",
            "openrouter-key",
        ]
        account = next(
            o for o in usage["observations"] if o["source"] == "openrouter-account"
        )
        assert account["budgets"][0]["remainingUsd"] == "7.5"
        key = next(o for o in usage["observations"] if o["source"] == "openrouter-key")
        assert key["budgets"][0]["remainingUsd"] == "40"

        # The failing stream retries with its own state; the other stays healthy.
        assert health["openrouter-account"]["status"] == "retrying"
        assert health["openrouter-account"]["errorCategory"] == "rate_limited"
        assert health["openrouter-key"]["status"] == "healthy"
        assert health["openrouter-key"]["errorCategory"] is None
