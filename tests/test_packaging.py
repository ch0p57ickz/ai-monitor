"""The reported version must have one source, and it must be the installed one.

A second hardcoded copy drifts: bumping only the packaged version leaves
``--version`` and ``/api/v1/version`` announcing the old release, so an upgrade
looks like it did not take effect.
"""

from __future__ import annotations

import importlib
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

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

    def test_corrupt_metadata_falls_back_instead_of_breaking_the_import(
        self, monkeypatch
    ):
        """A non-UTF-8 METADATA raises UnicodeDecodeError, not PackageNotFoundError."""

        def corrupt(name: str) -> str:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr(metadata, "version", corrupt)
        # Failing softly is not the same as failing silently: a deployed install
        # that cannot read its metadata must be distinguishable from a checkout.
        with pytest.warns(RuntimeWarning):
            assert ai_monitor._resolve_version() == ai_monitor.SOURCE_VERSION

    def test_metadata_without_a_version_falls_back(self, monkeypatch):
        """An empty dist-info makes the call return None rather than raise."""
        monkeypatch.setattr(metadata, "version", lambda name: None)
        assert ai_monitor._resolve_version() == ai_monitor.SOURCE_VERSION

class TestTheModuleWiring:
    def test_the_version_is_resolved_when_the_module_loads(self, monkeypatch):
        """Pin the wiring, not just the helper.

        A hardcoded ``__version__`` would ignore the metadata and stay at the
        source version, which is what let the packaged and reported versions drift
        apart. The equality assertion alone cannot catch that in a checkout, where
        a hardcoded value would happen to match ``SOURCE_VERSION``; reloading with
        the metadata patched can.

        The patch is scoped with ``monkeypatch.context`` because monkeypatch's
        own teardown runs *after* the test body: a plain ``finally`` reload would
        still see the patched metadata and leave the module reporting the fake
        version for the rest of the session.
        """
        with monkeypatch.context() as patch:
            patch.setattr(metadata, "version", lambda name: "7.7.7")
            assert importlib.reload(ai_monitor).__version__ == "7.7.7"

        # Real metadata is back now, so the reload must not keep the fake value.
        importlib.reload(ai_monitor)
        assert ai_monitor.__version__ != "7.7.7", (
            "the reload leaked the patched version into the session"
        )
        assert ai_monitor.__version__ == ai_monitor._resolve_version()

    def test_the_reported_version_is_a_non_empty_string(self):
        assert isinstance(ai_monitor.__version__, str)
        assert ai_monitor.__version__
