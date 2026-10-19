"""ai_monitor: cached provider usage service for GX10.

See openspec/changes/gx10-usage-service for the governing specs.
"""

from importlib import metadata as _metadata
import warnings

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

    The lookup is deliberately defensive. Metadata can be corrupt or unreadable
    (a non-UTF-8 ``METADATA`` raises ``UnicodeDecodeError``, which is not a
    ``PackageNotFoundError``), and a dist-info with missing metadata makes the
    call return ``None`` rather than raise. Either one escaping here would break
    the import of the whole package, so every failure falls back to the source
    version — a wrong-but-harmless version string beats an unimportable service.
    """
    try:
        installed = _metadata.version("ai-monitor")
    except _metadata.PackageNotFoundError:
        # The normal case in a source checkout: no distribution metadata at all.
        return SOURCE_VERSION
    except Exception as exc:  # noqa: BLE001 - never break the package import
        # Corrupt or unreadable metadata must not make the service unimportable,
        # but it must not pass silently either: a broken deployed install would
        # otherwise look exactly like a checkout and report the source version.
        warnings.warn(
            f"cannot read the installed ai-monitor version ({exc!r}); "
            f"reporting {SOURCE_VERSION}",
            RuntimeWarning,
            stacklevel=2,
        )
        return SOURCE_VERSION
    return installed or SOURCE_VERSION


__version__ = _resolve_version()

__all__ = ["SOURCE_VERSION", "__version__"]
