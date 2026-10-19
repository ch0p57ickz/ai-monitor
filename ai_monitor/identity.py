"""Opaque account identifiers derived from provider identity.

The usage-snapshots spec requires the service to distinguish snapshots from
different provider accounts by an *opaque* identifier, and never to store a
provider account name or email address. This module turns a provider account
identity into a keyed digest so two snapshots from the same account can be
correlated without persisting anything identifying.

When no identity is available, or no digest key is configured, the identifier
is ``None`` and the caller must avoid account-dependent trends.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Optional

__all__ = ["derive_account_id", "account_identity", "IDENTITY_FIELDS"]

#: Unambiguous top-level payload fields that may carry an account identity.
IDENTITY_FIELDS = ("account_id", "accountId", "email")

#: Fields to look for inside a nested ``account`` object. A top-level ``id`` is
#: deliberately excluded: on these payloads it may be a limit or window id.
NESTED_IDENTITY_FIELDS = ("id", "account_id", "accountId", "email", "name")


def derive_account_id(source: str, identity: Any, digest_key: str) -> Optional[str]:
    """Return an opaque, source-scoped account id, or ``None``.

    The raw identity (which may be a name or email address) is never returned
    or stored: only a keyed HMAC digest is. The digest is scoped by source, so
    the same identity seen through two providers never collides.

    Returns ``None`` when the identity is empty or no digest key is configured
    (design: use ``null`` and avoid account-dependent trends).
    """
    if not digest_key or identity is None:
        return None
    text = identity if isinstance(identity, str) else str(identity)
    text = text.strip()
    if not text:
        return None
    digest = hmac.new(
        digest_key.encode("utf-8"),
        f"{source}:{text}".encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()[:32]


def account_identity(payload: Any) -> Optional[str]:
    """Extract a candidate account identity from a provider result.

    Checks an unambiguous top-level field first, then a nested ``account``
    object (dict or bare string). Returns ``None`` when nothing suitable is
    present, so an unknown payload shape never invents an account.
    """
    if not isinstance(payload, dict):
        return None

    for field in IDENTITY_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    account = payload.get("account")
    if isinstance(account, str) and account.strip():
        return account.strip()
    if isinstance(account, dict):
        for field in NESTED_IDENTITY_FIELDS:
            value = account.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
