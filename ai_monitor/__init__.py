"""ai_monitor: cached provider usage service for GX10.

See openspec/changes/gx10-usage-service for the governing specs.
"""

from importlib import metadata as _metadata

#: Version to report when running from a source checkout, where no distribution
#: metadata exists. ``tests/test_packaging.py`` pins it against ``pyproject.toml``
#: so the two cannot drift apart unnoticed.
SOURCE_VERSION = "0.1.0"


def _resolve_version() -> str:
    """The installed distribution's version, or the source version in a checkout.

    Reading the installed metadata keeps ``--version``, ``/api/v1/version``, and
    ``pip show`` agreeing after an upgrade. A second hardcoded copy would let them
    disagree: an upgrade would report success while the service still announced
    the old version, so the upgrade would look like it had not taken effect.
    """
    try:
        return _metadata.version("ai-monitor")
    except _metadata.PackageNotFoundError:
        return SOURCE_VERSION


__version__ = _resolve_version()

__all__ = ["SOURCE_VERSION", "__version__"]
