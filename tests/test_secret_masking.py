"""Configured credentials must not survive into a payload, a database, or a log.

The shape patterns in ``ai_monitor.redact`` are a heuristic and are documented as
one: an unkeyed 32+ character alphabetic run is indistinguishable from a file
path, so it is deliberately not masked. That is fine for diagnostics, but the
spec is unconditional — credentials "SHALL NOT appear in HTTP responses or logs"
and successful observations "SHALL NOT persist credentials". A heuristic cannot
carry an unconditional guarantee, so configured credentials are also registered
and masked by exact match.

The demonstration is `TestProviderCannotSmuggleACredential`: a provider echoing
the configured key back in a field we map into a budget reaches neither the
served payload nor SQLite. Before this, it reached both.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.pace import PaceSettings, enrich_observation
from ai_monitor.redact import (
    REDACTED,
    mask_secrets,
    redact_text,
    register_config_secrets,
    register_secret,
)

#: A shape the heuristic patterns catch.
VENDOR_KEY = "sk-or-v1-" + "a1b2c3d4e5f6" * 4

#: A shape they deliberately do not: 40 alphabetic characters, no key= context.
PLAIN_SECRET = "ZqWm" * 10


class TestRegisterSecret:
    def test_a_shape_the_heuristic_misses_is_registered(self):
        assert redact_text(PLAIN_SECRET) == PLAIN_SECRET  # heuristic lets it pass
        assert register_secret(PLAIN_SECRET) is True
        assert redact_text(PLAIN_SECRET) == REDACTED

    def test_a_registered_secret_is_masked_inside_a_sentence(self):
        register_secret(PLAIN_SECRET)
        assert redact_text(f"request failed for {PLAIN_SECRET}") == (
            f"request failed for {REDACTED}"
        )

    def test_a_short_value_is_refused(self):
        # Masking a three-character "credential" would mangle ordinary text.
        assert register_secret("abc") is False
        assert register_secret("") is False

    def test_a_non_string_is_refused(self):
        assert register_secret(None) is False
        assert register_secret(12345678) is False

    def test_surrounding_whitespace_is_trimmed_before_matching(self):
        assert register_secret(f"  {PLAIN_SECRET}  ") is True
        assert redact_text(PLAIN_SECRET) == REDACTED

    def test_the_longest_registered_secret_wins(self):
        # Otherwise masking the shorter one leaves a readable tail behind.
        register_secret("abcdefgh")
        register_secret("abcdefghijklmnop")
        assert redact_text("abcdefghijklmnop") == REDACTED


class TestMaskSecrets:
    def test_it_masks_only_exact_match(self):
        """The digest must survive: masking it would merge every account."""
        digest = "a1b2c3d4" * 8
        register_secret(PLAIN_SECRET)
        assert mask_secrets(digest) == digest
        assert redact_text(digest) == REDACTED  # the heuristic does mask it

    def test_it_masks_a_registered_credential(self):
        register_secret(VENDOR_KEY)
        assert mask_secrets(f"key={VENDOR_KEY}") == f"key={REDACTED}"

    def test_none_becomes_empty(self):
        assert mask_secrets(None) == ""


class TestRegisterConfigSecrets:
    def test_every_credential_field_is_registered(self, tmp_path):
        config = Config(
            data_dir=str(tmp_path),
            openrouter_api_key=VENDOR_KEY,
            openrouter_management_key="mgmt-" + "b" * 20,
            account_digest_key="c" * 64,
        )
        assert register_config_secrets(config) == 3
        for value in (VENDOR_KEY, "mgmt-" + "b" * 20, "c" * 64):
            assert redact_text(value) == REDACTED

    def test_empty_fields_register_nothing(self, tmp_path):
        assert register_config_secrets(Config(data_dir=str(tmp_path))) == 0

    def test_it_accepts_any_object_with_the_attributes(self):
        class Bare:
            openrouter_api_key = "x" * 24

        assert register_config_secrets(Bare()) == 1


def _observation(resets_at: str | None, window_id: str = "current-key", **window):
    """Run a raw OpenRouter-shaped payload through the real normalization path."""
    raw = {
        "status": "success",
        "budgets": [
            {
                "id": window_id,
                "scope": "key",
                "remaining_usd": 95.0,
                "limit_usd": 100.0,
                "used_usd": 5.0,
                "resets_at": resets_at,
            }
        ],
        "quota_windows": [
            {
                "id": window_id,
                "label": window.get("label"),
                "group": window.get("group"),
                "model": window.get("model"),
                "used_percent": 25.0,
                "resets_at": resets_at,
            }
        ],
    }
    source = get_source_def("openrouter-key")
    enriched = enrich_observation(source, raw, PaceSettings(), {}, None, {})
    return normalize_observation(source, enriched)


class TestNormalizedObservationIsRedacted:
    def test_a_credential_in_a_reset_timestamp_is_masked(self):
        register_secret(VENDOR_KEY)
        observation = _observation(VENDOR_KEY)
        assert observation.budgets[0].resets_at == REDACTED
        assert observation.quota_windows[0].resets_at == REDACTED
        assert VENDOR_KEY not in json.dumps(observation.to_dict())

    def test_provider_display_text_is_masked(self):
        register_secret(PLAIN_SECRET)
        observation = _observation(
            None, label=PLAIN_SECRET, group=PLAIN_SECRET, model=PLAIN_SECRET
        )
        window = observation.quota_windows[0]
        assert window.label == REDACTED
        assert window.group == REDACTED
        assert window.model == REDACTED

    def test_legitimate_values_are_untouched(self):
        register_secret(VENDOR_KEY)
        observation = _observation("2026-10-20T01:00:00Z", window_id="codex:codex:primary",
                                   label="5h · primary", group="primary", model="gpt-5-codex")
        window = observation.quota_windows[0]
        assert window.resets_at == "2026-10-20T01:00:00Z"
        assert window.id == "codex:codex:primary"
        assert window.label == "5h · primary"
        assert window.group == "primary"
        assert window.model == "gpt-5-codex"
        assert observation.budgets[0].resets_at == "2026-10-20T01:00:00Z"

    def test_distinct_opaque_window_ids_are_not_merged(self):
        """A digest-shaped id must survive, or unrelated buckets collapse."""
        register_secret(PLAIN_SECRET)
        first = _observation(None, window_id="a1b2c3d4" * 8)
        second = _observation(None, window_id="deadbeef" * 8)
        assert first.quota_windows[0].id == "a1b2c3d4" * 8
        assert second.quota_windows[0].id == "deadbeef" * 8
        assert first.quota_windows[0].id != second.quota_windows[0].id

    def test_a_credential_in_a_window_id_is_masked_exactly(self):
        register_secret(VENDOR_KEY)
        observation = _observation(None, window_id=f"antigravity:{VENDOR_KEY}")
        assert observation.quota_windows[0].id == f"antigravity:{REDACTED}"

    def test_account_id_is_never_masked(self):
        """It is our own digest; the heuristic would flatten every account."""
        register_secret(PLAIN_SECRET)
        digest = "a1b2c3d4" * 8
        source = get_source_def("codex")
        raw = {"status": "success", "account_id": digest, "quota_windows": [], "budgets": []}
        observation = normalize_observation(source, raw)
        assert observation.account_id == digest


class TestProviderCannotSmuggleACredential:
    """End to end: normalization, the payload, and the database."""

    def _persist(self, tmp_path, resets_at: str):
        observation = _observation(resets_at)
        db = Database(str(tmp_path / "usage.db"))
        try:
            db.insert_observation(get_source_def("openrouter-key"), observation)
        finally:
            db.close()
        return observation, tmp_path / "usage.db"

    def test_the_payload_and_the_database_both_exclude_it(self, tmp_path):
        register_secret(VENDOR_KEY)
        observation, db_path = self._persist(tmp_path, VENDOR_KEY)

        assert VENDOR_KEY not in json.dumps(observation.to_dict())

        connection = sqlite3.connect(db_path)
        try:
            stored = connection.execute(
                "select budgets_json, quota_windows_json from observations"
            ).fetchall()
        finally:
            connection.close()
        blob = json.dumps(stored)
        assert VENDOR_KEY not in blob
        assert REDACTED in blob, "the value should be masked, not dropped"

    def test_an_unregistered_credential_shape_is_out_of_scope(self, tmp_path):
        """Documents the boundary: registration is what makes it airtight."""
        _, db_path = self._persist(tmp_path, PLAIN_SECRET)
        connection = sqlite3.connect(db_path)
        try:
            blob = json.dumps(
                connection.execute(
                    "select budgets_json, quota_windows_json from observations"
                ).fetchall()
            )
        finally:
            connection.close()
        # Not registered in this test, and its shape misses the heuristic.
        assert PLAIN_SECRET in blob


class TestEntryPointRegisters:
    """Without this, exact masking would be dead code in the deployed service."""

    def test_main_registers_the_configured_credentials(self, tmp_path, monkeypatch):
        import os
        import signal
        import threading
        import time

        import ai_monitor.main as main_module

        canary = VENDOR_KEY
        digest = "d" * 64
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "data_dir": str(tmp_path / "data"),
                    "api_port": 0,
                    # Nothing should be spawned by this test: the sources are
                    # pointed at binaries that cannot run.
                    "codex_bin": "/nonexistent/codex",
                    "codex_interval": 3600.0,
                    "antigravity_bin": "/nonexistent/agy",
                    "antigravity_interval": 3600.0,
                    "openrouter_interval": 3600.0,
                    # A closed local port: the poll fails immediately and sends
                    # no traffic anywhere, so the test stays offline.
                    "openrouter_base_url": "http://127.0.0.1:1",
                    "openrouter_api_key": canary,
                    "account_digest_key": digest,
                }
            ),
            "utf-8",
        )

        calls: list[int] = []
        real = main_module.register_config_secrets
        monkeypatch.setattr(
            main_module,
            "register_config_secrets",
            lambda config: calls.append(real(config)) or calls[-1],
        )

        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        def stop_soon() -> None:
            time.sleep(2.0)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=stop_soon, daemon=True).start()
        try:
            exit_code = main_module.main(["--config", str(config_path), "--verbose"])
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

        assert exit_code == 0, "the service should shut down cleanly on SIGTERM"
        assert calls and calls[-1] >= 1, "main() must register the credentials"
        # The registration has to be effective, not just called.
        assert redact_text(canary) == REDACTED
        assert redact_text(digest) == REDACTED
