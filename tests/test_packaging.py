"""The reported version must have one source, and it must be the installed one.

A second hardcoded copy drifts: bumping only the packaged version leaves
``--version`` and ``/api/v1/version`` announcing the old release, so an upgrade
looks like it did not take effect.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import ai_monitor


PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


class TestSourceVersionMatchesPackaging:
    def test_the_fallback_matches_pyproject(self):
        """The checkout fallback and the packaged version are the same release."""
        with PYPROJECT.open("rb") as handle:
            packaged = tomllib.load(handle)["project"]["version"]
        assert ai_monitor.SOURCE_VERSION == packaged, (
            "ai_monitor.SOURCE_VERSION and pyproject.toml have drifted; bump both "
            "or the version reported from a checkout is wrong"
        )


class TestResolveVersion:
    def test_installed_metadata_wins(self, monkeypatch):
        monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
        assert ai_monitor._resolve_version() == "9.9.9"

    def test_a_checkout_falls_back_to_the_source_version(self, monkeypatch):
        def missing(name: str) -> str:
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(metadata, "version", missing)
        assert ai_monitor._resolve_version() == ai_monitor.SOURCE_VERSION

    def test_the_reported_version_is_a_non_empty_string(self):
        assert isinstance(ai_monitor.__version__, str)
        assert ai_monitor.__version__
