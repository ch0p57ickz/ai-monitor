"""Tests for the OpenRouter live check's safety helpers.

The tool exists to prove two things without ever printing a credential: that a
stream can collect, and that the credential cannot reach an observation payload.
The first is verified live against OpenRouter; the second is pure logic, and is
tested here so a regression in the leak check cannot slip through silently.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).parents[1] / "tools" / "openrouter_live_check.py"


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("openrouter_live_check", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLeaked:
    def test_detects_a_secret_at_the_top_level(self, check):
        assert check.leaked("sk-secret", {"key": "sk-secret"}) is True

    def test_detects_a_secret_nested_in_a_payload(self, check):
        payload = {"budgets": [{"notes": {"hint": "uses sk-secret"}}]}
        assert check.leaked("sk-secret", payload) is True

    def test_clean_payloads_do_not_report_a_leak(self, check):
        payload = {"budgets": [{"remainingUsd": "1.50", "notes": None}]}
        assert check.leaked("sk-secret", payload) is False

    def test_an_empty_secret_never_reports_a_leak(self, check):
        assert check.leaked("", {"anything": "at all"}) is False

    def test_every_payload_is_checked(self, check):
        assert check.leaked("sk-secret", {"clean": True}, {"dirty": "sk-secret"}) is True

    def test_a_non_serializable_payload_is_compared_as_text(self, check):
        class Opaque:
            def __init__(self, secret: str) -> None:
                self._secret = secret

            def __str__(self) -> str:
                return self._secret

        assert check.leaked("sk-secret", Opaque("sk-secret")) is True


class TestRedact:
    def test_replaces_strings_recursively(self, check):
        payload = {
            "error": "failed with sk-secret",
            "list": ["sk-secret", 1, None],
            "nested": {"inner": "sk-secret"},
        }
        assert check._redact(payload, "sk-secret") == {
            "error": "failed with <redacted>",
            "list": ["<redacted>", 1, None],
            "nested": {"inner": "<redacted>"},
        }

    def test_leaves_non_strings_alone(self, check):
        assert check._redact(42, "sk-secret") == 42
        assert check._redact(None, "sk-secret") is None


class TestExplain:
    """The summary must describe what happened, not a canned expectation.

    The live run on 2026-10-19 had a management key and no inference key: the
    account stream collected and the current-key stream reported
    `not_configured`. The old message blamed the credit balance for a failure
    that was actually a missing key credential.
    """

    def _outcomes(self, account_status, key_status, **errors):
        account = {"source": "openrouter-account", "status": account_status}
        key = {"source": "openrouter-key", "status": key_status}
        for stream, category in errors.items():
            target = account if stream == "account" else key
            target["errorCategory"] = category
        return {"openrouter-account": account, "openrouter-key": key}

    def test_management_key_only_names_the_missing_key_credential(self, check):
        notes = check._explain(
            self._outcomes("success", "failed", key="not_configured"),
            management=True,
            inference=False,
        )
        joined = "\n".join(notes)
        assert "account-credit stream collected" in joined
        assert "OPENROUTER_API_KEY" in joined
        assert "not_configured" in joined

    def test_inference_key_only_explains_the_refused_fallback(self, check):
        notes = check._explain(
            self._outcomes("failed", "success", account="authorization"),
            management=False,
            inference=True,
        )
        joined = "\n".join(notes)
        assert "current-key stream collected" in joined
        assert "only the current-key stream succeeds" in joined

    def test_both_failing_says_so(self, check):
        notes = check._explain(
            self._outcomes("failed", "failed", account="unavailable", key="unavailable"),
            management=True,
            inference=True,
        )
        assert any("neither stream collected" in note for note in notes)

    def test_a_clean_run_needs_no_notes(self, check):
        notes = check._explain(
            self._outcomes("success", "success"), management=True, inference=True
        )
        assert notes == []

    def test_fallback_balance_is_flagged_when_it_works(self, check):
        notes = check._explain(
            self._outcomes("success", "success"), management=False, inference=True
        )
        assert any("used the inference key" in note for note in notes)
