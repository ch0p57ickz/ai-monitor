"""Fixture-driven tests and redaction checks.

Covers task 3.5: parser fixtures for missing fields, changed shapes, and source
errors, plus verification that logs and persisted records never contain
credentials, CSRF tokens, email addresses, or full payloads.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ai_monitor.models import (
    get_source_def,
    normalize_observation,
)
from ai_monitor.redact import (
    REDACTED,
    RedactingFilter,
    install_redaction,
    redact_text,
    redact_value,
)
from ai_monitor.sources.registry import (
    CollectionError,
    parse_antigravity_usage,
    parse_openrouter_account_credits,
    parse_openrouter_key_usage,
)
from ai_monitor.sources.codex import parse_codex_rate_limits

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Codex fixtures
# ---------------------------------------------------------------------------


class TestCodexFixtures:
    def test_keyed_view(self):
        windows = parse_codex_rate_limits(_load("codex_keyed.json"))
        assert [w["id"] for w in windows] == [
            "codex:5h:primary",
            "codex:5h:secondary",
            "codex:7d:primary",
            "codex:7d:secondary",
        ]
        primary = windows[0]
        assert primary["used_percent"] == 42.0
        assert primary["duration_minutes"] == 300
        assert primary["resets_at"] == "2025-10-09T08:53:20Z"
        assert windows[1]["duration_minutes"] == 10080
        assert windows[2]["resets_at"] == "2025-11-07T07:20:00Z"
        # A missing secondary stays unknown rather than being fabricated.
        assert windows[3]["used_percent"] is None
        assert windows[3]["duration_minutes"] is None
        assert windows[3]["resets_at"] is None

    def test_single_view(self):
        windows = parse_codex_rate_limits(_load("codex_single.json"))
        assert [w["id"] for w in windows] == [
            "rate-limits:primary",
            "rate-limits:secondary",
        ]
        assert windows[0]["used_percent"] == 25.0
        assert windows[0]["duration_minutes"] == 300
        assert windows[0]["resets_at"] == "2025-10-09T08:53:20Z"

    def test_missing_secondary_reported_zero_is_kept(self):
        windows = parse_codex_rate_limits(_load("codex_missing_secondary.json"))
        primary = next(w for w in windows if w["id"] == "codex:5h:primary")
        secondary = next(w for w in windows if w["id"] == "codex:5h:secondary")
        assert primary["used_percent"] == 0.0  # a real reported zero
        assert primary["duration_minutes"] == 300
        assert primary["resets_at"] == "2025-10-09T08:53:20Z"
        assert secondary["used_percent"] is None
        assert secondary["resets_at"] is None

    def test_changed_shape_falls_back_to_the_single_view(self):
        windows = parse_codex_rate_limits(_load("codex_changed_shape.json"))
        assert [w["id"] for w in windows] == [
            "rate-limits:primary",
            "rate-limits:secondary",
        ]
        assert windows[0]["used_percent"] == 10.0
        assert windows[0]["duration_minutes"] == 300


# ---------------------------------------------------------------------------
# Antigravity fixtures
# ---------------------------------------------------------------------------


class TestAntigravityFixtures:
    def test_session_only_has_no_weekly_window(self):
        windows = parse_antigravity_usage(_load("antigravity_session_only.json"))
        assert len(windows) == 2
        assert {w["scope"] for w in windows} == {"session"}
        assert windows[0]["group"] == "workspace-alpha"
        assert windows[0]["used_percent"] is None
        assert windows[1]["used_percent"] == 15.5

    def test_changed_shape_is_unavailable(self):
        with pytest.raises(CollectionError) as exc_info:
            parse_antigravity_usage(_load("antigravity_changed_shape.json"))
        assert exc_info.value.category == "unavailable"


# ---------------------------------------------------------------------------
# OpenRouter fixtures
# ---------------------------------------------------------------------------


class TestOpenRouterFixtures:
    def test_key_full(self):
        budget = parse_openrouter_key_usage(_load("openrouter_key_full.json"))[0]
        assert budget["limit_usd"] == 50.0
        assert budget["used_usd"] == 10.0
        assert budget["remaining_usd"] == 40.0
        assert budget["reset_period"] == "monthly"
        assert budget["resets_at"] is None  # no exact timestamp in the fixture

    def test_key_missing_fields_stays_unknown(self):
        budget = parse_openrouter_key_usage(
            _load("openrouter_key_missing_fields.json")
        )[0]
        assert budget["limit_usd"] is None
        assert budget["used_usd"] is None
        assert budget["remaining_usd"] is None

    def test_credits_partial_does_not_fabricate_remaining(self):
        budget = parse_openrouter_account_credits(
            _load("openrouter_credits_partial.json")
        )[0]
        assert budget["total_credits_usd"] == 100.0
        assert budget["total_usage_usd"] is None
        assert budget["remaining_usd"] is None

    def test_error_body_is_not_a_success_payload(self):
        with pytest.raises(CollectionError) as exc_info:
            parse_openrouter_account_credits(_load("openrouter_error_401.json"))
        assert exc_info.value.category == "invalid_response"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedactText:
    @pytest.mark.parametrize(
        "secret",
        [
            "admin@example.com",
            "sk-live-abcdef0123456789",
            "Bearer abcdefghijklmnop",
            "csrf_token=abcdef0123456789abcdef",
            "authorization: token-abcdefghijkl",
            "opaque 0123456789abcdef0123456789abcdef",
        ],
    )
    def test_secret_forms_are_masked(self, secret):
        result = redact_text(f"provider said {secret} while polling")
        assert secret not in result
        assert REDACTED in result

    def test_multiline_text_is_collapsed(self):
        assert redact_text("line one\nline two\n") == "line one line two"

    def test_multiline_preserved_when_requested(self):
        result = redact_text("line one\nline two", collapse_whitespace=False)
        assert result == "line one\nline two"

    def test_quoted_json_key_is_masked(self):
        result = redact_text('{"token": "secret-value", "keep": "ok"}')
        assert "secret-value" not in result
        assert "ok" in result

    def test_long_text_is_truncated(self):
        result = redact_text("x" * 500)
        assert len(result) <= 201
        assert result.endswith("…")

    def test_none_becomes_empty(self):
        assert redact_text(None) == ""

    def test_ordinary_text_survives(self):
        assert redact_text("rate limit exceeded") == "rate limit exceeded"

    def test_authorization_bearer_line_leaves_no_token(self):
        result = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwx")
        assert "abcdefghijklmnopqrstuvwx" not in result

    def test_paths_and_error_messages_survive(self):
        text = (
            'Error: unexpected argument "usage". '
            "openrouter HTTP 429 rate limit exceeded"
        )
        assert redact_text(text) == text


class TestRedactValue:
    def test_nested_secrets_are_masked(self):
        value = redact_value(
            {
                "email": "user@example.com",
                "nested": {"token": "sk-abcdef0123456789"},
                "items": ["a@b.com", 3],
            }
        )
        assert value["email"] == REDACTED
        assert value["nested"]["token"] == REDACTED
        assert value["items"][0] == REDACTED
        assert value["items"][1] == 3

    def test_secret_key_masks_its_whole_value(self):
        value = redact_value(
            {
                "password": "hunter2",
                "csrf_token": 12345,
                "nested": {"api_key": {"deep": "value"}},
                "safe": "kept",
            }
        )
        assert value["password"] == REDACTED
        assert value["csrf_token"] == REDACTED
        assert value["nested"]["api_key"] == REDACTED
        assert value["safe"] == "kept"


class TestInstallRedaction:
    def test_child_logger_records_are_redacted_via_handlers(self):
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        filter_ = None
        try:
            filter_ = install_redaction()
            logging.getLogger("ai_monitor.child").warning(
                "leaked %s and %s", "sk-abcdef0123456789", "user@example.com"
            )
        finally:
            root.removeHandler(handler)
            # Undo the global install so other tests see raw log text.
            if filter_ is not None:
                root.removeFilter(filter_)
                for existing in root.handlers:
                    existing.removeFilter(filter_)

        output = stream.getvalue()
        assert "sk-abcdef0123456789" not in output
        assert "user@example.com" not in output
        assert REDACTED in output


class TestRedactingFilter:
    def _emit(self, record: logging.LogRecord) -> str:
        filter_ = RedactingFilter()
        filter_.filter(record)
        return logging.Formatter("%(message)s").format(record)

    def test_message_and_args_are_redacted(self):
        record = logging.LogRecord(
            "ai_monitor.test",
            logging.WARNING,
            __file__,
            1,
            "polling %s for %s",
            ("sk-abcdef0123456789", "user@example.com"),
            None,
        )
        out = self._emit(record)
        assert "sk-abcdef0123456789" not in out
        assert "user@example.com" not in out

    def test_traceback_text_is_redacted(self):
        try:
            raise RuntimeError("token sk-abcdef0123456789 leaked")
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "ai_monitor.test",
                logging.ERROR,
                __file__,
                1,
                "unexpected error",
                (),
                sys.exc_info(),
            )
        out = self._emit(record)
        assert "sk-abcdef0123456789" not in out
        assert "RuntimeError" in out


# ---------------------------------------------------------------------------
# Persisted records
# ---------------------------------------------------------------------------


class TestPersistedRecords:
    def test_notes_are_redacted_when_persisted(self):
        source = get_source_def("openrouter-key")
        payload = _load("openrouter_error_401_redaction.json")
        raw = {
            "status": "success",
            "budgets": [],
            "notes": {
                "provider": payload,
                "detail": payload["error"]["message"],
            },
        }
        obs = normalize_observation(source, raw)
        serialized = json.dumps(obs.notes)
        assert "admin@example.com" not in serialized
        assert "sk-live-abcdef0123456789" not in serialized
        assert "abcdef0123456789abcdef" not in serialized

    def test_notes_are_redacted_before_reaching_the_database(
        self, sample_config, sample_db
    ):
        source = get_source_def("openrouter-key")
        secret_notes = {
            "detail": "user@example.com used token sk-live-abcdef0123456789"
        }
        obs = normalize_observation(
            source, {"status": "success", "notes": secret_notes}
        )
        sample_db.insert_observation(source, obs)

        rows, _ = sample_db.history(hours=1, limit=10, source="openrouter-key")
        blob = json.dumps(rows)
        assert "user@example.com" not in blob
        assert "sk-live-abcdef0123456789" not in blob

    def test_full_payload_is_not_persisted(self):
        """A raw provider payload must not survive normalization wholesale."""
        source = get_source_def("openrouter-key")
        payload = _load("openrouter_key_full.json")
        obs = normalize_observation(
            source,
            {"status": "success", "budgets": parse_openrouter_key_usage(payload)},
        )
        serialized = json.dumps(obs.to_dict())
        assert "is_free_tier" not in serialized
        assert payload["data"]["label"] not in serialized
