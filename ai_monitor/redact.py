"""Redaction helpers for diagnostics.

Provider text (CLI stderr, HTTP error bodies, response snippets) can contain
credentials, CSRF tokens, or account identifiers. Everything that reaches a log
line or a persisted ``notes`` field goes through these helpers first:

- ``redact_text`` masks secret-like tokens, email addresses, and long
  base64-ish strings, collapses whitespace, and truncates the result.
- ``redact_value`` applies the same rules recursively to dicts and lists.
- ``RedactingFilter`` is a logging filter that redacts every log record, so a
  stray ``log.info("... %s", payload)`` cannot leak a secret.

The helpers never log or return the original value, and they are deliberately
conservative: when in doubt, replace with ``[redacted]``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

__all__ = [
    "REDACTED",
    "MAX_DIAGNOSTIC_CHARS",
    "redact_text",
    "redact_value",
    "RedactingFilter",
    "install_redaction",
]

#: Replacement marker for anything that looks like a secret.
REDACTED = "[redacted]"

#: Upper bound for a diagnostic string. A "full payload" is never logged.
MAX_DIAGNOSTIC_CHARS = 200

#: ``key=value`` / ``key: value`` forms for credential-ish field names. The
#: field name may be quoted (JSON) and may carry prefixes/suffixes
#: (``csrf_token``, ``api-key``), and a separator is required so ordinary prose
#: ("authorization failed") survives.
_SECRET_FIELD_RE = re.compile(
    r"(?i)(?<![\w-])([a-z0-9_-]*"
    r"(?:authorization|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
    r"token|secret|password|passwd|bearer|cookie|csrf|xsrf|nonce)"
    r"[a-z0-9_-]*)[\"']?\s*[:=]\s*[\"']?([^\s\"',;]+)"
)

#: Field names that always hide their whole value, used for object keys where
#: no textual ``key=value`` form is present.
_SECRET_KEY_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
    r"token|secret|password|passwd|bearer|cookie|csrf|xsrf|nonce)"
)

#: Bare bearer tokens and vendor-prefixed keys anywhere in the text.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_VENDOR_KEY_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{8,}"
)

#: Email addresses (also used as account identifiers).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: Long opaque strings (JWTs, signatures, session identifiers). Requires a mix
#: of letters and digits, and does not start mid-path, so ordinary file paths
#: and identifiers survive redaction.
_OPAQUE_RE = re.compile(
    r"(?<![\w/])(?=[A-Za-z0-9+/_-]{32,}={0,2})(?=[^ ]*\d)[A-Za-z0-9+/_-]{32,}={0,2}"
)


def redact_text(
    text: Any,
    limit: int = MAX_DIAGNOSTIC_CHARS,
    collapse_whitespace: bool = True,
) -> str:
    """Return a redacted, length-bounded version of ``text``.

    The result never contains something that looks like a credential, a CSRF
    token, an email address, or an opaque session string. ``None`` becomes the
    empty string. Set ``collapse_whitespace=False`` to keep line structure
    (used for tracebacks).

    Note: an unkeyed, purely alphabetic run of 32+ characters is not treated
    as a secret, because that shape is indistinguishable from an ordinary file
    path. Keyed forms (``token=...``, ``Authorization: ...``) are always
    redacted.
    """
    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    if collapse_whitespace:
        value = " ".join(value.split())
    # Order matters: mask bearer tokens first, so an
    # ``Authorization: Bearer <token>`` line cannot leave the token behind
    # after the field-name rule consumes only the word "Bearer".
    value = _BEARER_RE.sub(lambda m: f"Bearer {REDACTED}", value)
    value = _SECRET_FIELD_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", value)
    value = _VENDOR_KEY_RE.sub(REDACTED, value)
    value = _EMAIL_RE.sub(REDACTED, value)
    value = _OPAQUE_RE.sub(REDACTED, value)
    if collapse_whitespace:
        value = " ".join(value.split())
    if limit is not None and limit >= 0 and len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def redact_value(value: Any, limit: int = MAX_DIAGNOSTIC_CHARS) -> Any:
    """Recursively redact a JSON-like value.

    A key whose name looks credential-ish masks its whole value, whatever its
    type, and only a bounded summary is kept: dicts and lists are summarized
    rather than dumped, so a "full payload" can never reach a log or the
    database.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, limit=limit)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            name = str(key)
            out[name] = (
                REDACTED if _SECRET_KEY_RE.search(name) else redact_value(item, limit=limit)
            )
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item, limit=limit) for item in list(value)[:32]]
    return redact_text(value, limit=limit)


class RedactingFilter(logging.Filter):
    """Redact the message, arguments, and traceback of every log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the service
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = ()
        if record.exc_info:
            import traceback

            text = "".join(traceback.format_exception(*record.exc_info))
            # Keep line structure: a traceback collapsed to one line is not
            # diagnosable, and each line is still redacted.
            record.exc_text = redact_text(
                text, limit=None, collapse_whitespace=False
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(
                record.exc_text, limit=None, collapse_whitespace=False
            )
        return True


def install_redaction() -> RedactingFilter:
    """Attach the redacting filter to the root logger and its handlers.

    A filter on a logger only sees records logged *directly* on that logger;
    records from child loggers (``ai_monitor.state``) propagate straight to
    the handlers. Attaching to the handlers is therefore what actually covers
    every log line. Call this after the handlers are configured.
    """
    filter_ = RedactingFilter()
    root = logging.getLogger()
    root.addFilter(filter_)
    for handler in root.handlers:
        handler.addFilter(filter_)
    return filter_
