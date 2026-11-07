# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The check key and the fingerprint of a secret handoff.

``secret.handoff.export`` puts a random key into the handoff next to the
value. Both steps compute the fingerprint of the value under that key, so
the export and the import answer the same fingerprint and an auditor can
match the two rows. MEHO stores the key nowhere: it lives in the handoff
and is gone once the handoff is used or expired, so the fingerprint on
an audit row cannot be computed again from a value. The two instances
share no server key, which is why the handoff carries its own.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Final

__all__ = [
    "CHECK_KEY_BYTES",
    "CHECK_KEY_FIELD",
    "handoff_fingerprint",
    "new_check_key",
    "parse_check_key",
]

#: The key of the wrapped payload that carries the check key, as hex.
CHECK_KEY_FIELD: Final[str] = "check_key"

#: Size of the check key in bytes.
CHECK_KEY_BYTES: Final[int] = 32


def new_check_key() -> bytes:
    """A fresh random check key for one handoff."""
    return secrets.token_bytes(CHECK_KEY_BYTES)


def handoff_fingerprint(check_key: bytes, value: bytes) -> str:
    """HMAC-SHA256 of *value* under *check_key*, as 64 hex characters."""
    return hmac.new(check_key, value, hashlib.sha256).hexdigest()


def parse_check_key(raw: Any) -> bytes | None:
    """The check key from the wrapped payload's field, or ``None`` when the field is absent.

    A handoff made by an older MEHO carries no field. A field that is not a
    hex key of the right size is a payload that ``secret.handoff.export``
    did not make; raises :class:`ValueError` with the reason, in plain
    words and without the field's content.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("is not a hex key")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise ValueError("is not a hex key") from None
    if len(key) != CHECK_KEY_BYTES:
        raise ValueError("has the wrong size")
    return key
