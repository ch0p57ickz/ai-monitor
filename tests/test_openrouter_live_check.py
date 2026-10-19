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
