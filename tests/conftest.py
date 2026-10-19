"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_monitor.config import Config
from ai_monitor.db import Database
from ai_monitor.models import (
    Budget,
    QuotaWindow,
    SOURCE_DEFS,
    SourceDef,
    SourceStatus,
)
from ai_monitor.state import UsageWorker
from ai_monitor.sources.registry import (
    AntigravityAdapter,
    CodexAdapter,
    CollectionError,
    OpenRouterAccountAdapter,
    OpenRouterKeyAdapter,
    SourceAdapter,
)


@pytest.fixture
def sample_config(tmp_path: Path) -> Config:
    return Config(data_dir=str(tmp_path / "data"), api_port=0)


@pytest.fixture
def sample_db(sample_config: Config) -> Database:
    db = Database(Path(sample_config.data_dir) / "usage.db")
    yield db
    db.close()


@pytest.fixture
def sample_worker(sample_config: Config, sample_db: Database) -> UsageWorker:
    return UsageWorker(config=sample_config, db=sample_db, version="0.1.0-test")


@pytest.fixture
def all_sources() -> list[SourceDef]:
    return list(SOURCE_DEFS)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSourceAdapter(SourceAdapter):
    """Configurable fake adapter for unit tests."""

    def __init__(
        self,
        raw: dict[str, Any] | None = None,
        error: CollectionError | None = None,
    ) -> None:
        self._raw = raw or {}
        self._error = error
        self.fetch_count = 0

    def fetch(self, config: "Config") -> dict[str, Any]:
        self.fetch_count += 1
        if self._error is not None:
            raise self._error
        return self._raw


# ---------------------------------------------------------------------------
# Raw payload shapes (mirrors of real provider outputs)
# ---------------------------------------------------------------------------

CODEX_RAW: dict[str, Any] = {
    "quota": [
        {
            "id": "codex-weekly",
            "label": "Weekly quota",
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

ANTIGRAVITY_RAW: dict[str, Any] = {
    "quota": [
        {
            "id": "agy-session-1",
            "label": "Session",
            "scope": "session",
            "group": "default",
            "used_percent": 12.5,
            "duration_minutes": 43200,
        }
    ],
}

OPENROUTER_ACCOUNT_RAW: dict[str, Any] = {
    "limits": {"limit": 100.0, "usage": 30.0, "currency": "usd"},
}

OPENROUTER_KEY_RAW: dict[str, Any] = {
    "data": {"limit": 50.0, "usage": 10.0, "currency": "usd"},
}


def codex_source() -> SourceDef:
    return get_source_def("codex")


def antigravity_source() -> SourceDef:
    return get_source_def("antigravity")


def openrouter_account_source() -> SourceDef:
    return get_source_def("openrouter-account")


def openrouter_key_source() -> SourceDef:
    return get_source_def("openrouter-key")


def get_source_def(key: str) -> SourceDef:
    from ai_monitor.models import get_source_def as _g

    return _g(key)
