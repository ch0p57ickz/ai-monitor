"""Tests for opaque account identifiers (usage-snapshots: account identity).

The service must distinguish snapshots from different provider accounts by an
opaque identifier, and must never store an account name or email address.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from ai_monitor.config import Config
from ai_monitor.identity import account_identity, derive_account_id
from ai_monitor.models import get_source_def, normalize_observation
from ai_monitor.sources.codex import CodexAdapter


class TestDeriveAccountId:
    def test_is_stable_for_the_same_identity(self):
        first = derive_account_id("codex", "acct-1", "digest-key-0123456789")
        second = derive_account_id("codex", "acct-1", "digest-key-0123456789")
        assert first == second
        assert first is not None

    def test_is_scoped_by_source(self):
        assert derive_account_id("codex", "acct-1", "k") != derive_account_id(
            "antigravity", "acct-1", "k"
        )

    def test_is_scoped_by_digest_key(self):
        assert derive_account_id("codex", "acct-1", "k1") != derive_account_id(
            "codex", "acct-1", "k2"
        )

    def test_digest_is_a_source_scoped_hmac(self):
        expected = hmac.new(
            b"digest-key-0123456789", b"codex:acct-1", hashlib.sha256
        ).hexdigest()[:32]
        assert derive_account_id("codex", "acct-1", "digest-key-0123456789") == expected
        assert len(expected) == 32
        assert set(expected) <= set("0123456789abcdef")

    @pytest.mark.parametrize(
        "identity, digest_key",
        [
            (None, "k"),
            ("", "k"),
            ("   ", "k"),
            ("acct-1", ""),
            (None, ""),
        ],
    )
    def test_missing_input_yields_none(self, identity, digest_key):
        assert derive_account_id("codex", identity, digest_key) is None


class TestAccountIdentity:
    def test_top_level_field(self):
        assert account_identity({"account_id": "acct-1"}) == "acct-1"
        assert account_identity({"accountId": " acct-2 "}) == "acct-2"
        assert account_identity({"email": "user@example.com"}) == "user@example.com"

    def test_nested_account_object(self):
        assert account_identity({"account": {"id": "acct-3"}}) == "acct-3"
        assert account_identity({"account": {"name": "team-x"}}) == "team-x"

    def test_bare_account_string(self):
        assert account_identity({"account": "acct-4"}) == "acct-4"

    @pytest.mark.parametrize(
        "payload",
        [None, {}, {"id": "limit-1"}, {"account": {}}, {"account": 5}, [], "x"],
    )
    def test_unusable_payloads_yield_none(self, payload):
        # A top-level ``id`` is deliberately not used: on these payloads it can
        # be a limit or window id rather than an account.
        assert account_identity(payload) is None


def _serve_once(sock_path: Path, result: dict):
    """Serve one app-server session (initialize, then rate limits)."""
    import socket
    import threading

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def read_request(conn) -> bool:
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return False
            data += chunk
        return True

    def run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                if not read_request(conn):
                    return
                conn.sendall(
                    (json.dumps({"jsonrpc": "2.0", "id": "init-1", "result": {}}) + "\n").encode()
                )
                if not read_request(conn):
                    return
                conn.sendall(
                    (json.dumps({"jsonrpc": "2.0", "id": "usage-1", "result": result}) + "\n").encode()
                )
        finally:
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


KEYED = {"rateLimitsByLimitId": {"5h": {"primary": {"usedPercent": 20.0}}}}


class TestCodexAccountId:
    def test_account_id_is_derived_from_a_reported_identity(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, {**KEYED, "account": {"id": "acct-1"}})
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            codex_app_server_socket=str(sock),
            codex_timeout_seconds=5,
            account_digest_key="digest-key-0123456789",
        )
        out = CodexAdapter().fetch(cfg)
        thread.join(timeout=5)

        expected = derive_account_id("codex", "acct-1", "digest-key-0123456789")
        assert out["account_id"] == expected
        assert "acct-1" not in json.dumps(out)

    def test_no_account_id_without_a_digest_key(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, {**KEYED, "account": {"id": "acct-1"}})
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            codex_app_server_socket=str(sock),
            codex_timeout_seconds=5,
        )
        out = CodexAdapter().fetch(cfg)
        thread.join(timeout=5)

        assert "account_id" not in out

    def test_no_account_id_when_no_identity_is_reported(self, tmp_path: Path):
        sock = tmp_path / "codex.sock"
        thread = _serve_once(sock, KEYED)
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            codex_app_server_socket=str(sock),
            codex_timeout_seconds=5,
            account_digest_key="digest-key-0123456789",
        )
        out = CodexAdapter().fetch(cfg)
        thread.join(timeout=5)

        assert "account_id" not in out

    def test_raw_identity_is_not_persisted(self, tmp_path: Path, sample_db):
        """The whole path must drop the raw identity, not just the adapter.

        The fake app-server reports an email address; only the opaque digest
        may reach the database.
        """
        sock = tmp_path / "codex.sock"
        thread = _serve_once(
            sock, {**KEYED, "account": {"email": "user@example.com"}}
        )
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            codex_app_server_socket=str(sock),
            codex_timeout_seconds=5,
            account_digest_key="digest-key-0123456789",
        )
        source = get_source_def("codex")
        obs = normalize_observation(source, CodexAdapter().fetch(cfg))
        thread.join(timeout=5)

        assert obs.account_id == derive_account_id(
            "codex", "user@example.com", "digest-key-0123456789"
        )
        sample_db.insert_observation(source, obs)

        rows, _ = sample_db.history(hours=1, limit=10, source="codex")
        blob = json.dumps(rows)
        assert "user@example.com" not in blob
        assert "example.com" not in blob
        assert obs.account_id in blob

    def test_same_account_history_is_same_account_only(
        self, sample_config: Config, sample_db
    ):
        source = get_source_def("codex")

        def poll(identity: str, used: float):
            raw = {
                "status": "success",
                "quota_windows": [{"id": "w", "used_percent": used}],
                "account_id": derive_account_id("codex", identity, "digest-key-0123456789"),
            }
            obs = normalize_observation(source, raw)
            sample_db.insert_observation(source, obs)
            return obs

        poll("acct-1", 10.0)
        poll("acct-2", 90.0)

        rows, _ = sample_db.history(
            hours=1,
            limit=10,
            source="codex",
            account_id=derive_account_id("codex", "acct-1", "digest-key-0123456789"),
        )
        assert len(rows) == 1
        assert rows[0]["quotaWindows"][0]["usedPercent"] == 10.0
