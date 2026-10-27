# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret removal for FortiOS answers, error texts and stored traces.

FortiOS returns some secrets in its answers: an ``ENC ...`` value (a secret
the device stores encrypted, for example an IPsec pre-shared key or an admin
password) and plain values under secret field names (``psksecret``,
``password``, ``passwd``, ...). The connector removes both **inside the
operation**, before the answer leaves the handler. So the audit row, a
stored result, the flight-recorder trace and the broadcast feed only see the
cleaned value.

A value is replaced with :data:`REDACTED` when one of these holds:

1. it is a text that starts with ``ENC`` and a space (FortiOS's mark for an
   encrypted secret), whatever the field name;
2. it is a text that holds a PEM private key;
3. its field name is a known secret name (:data:`SECRET_FIELD_NAMES`), holds
   a secret word (:data:`_SECRET_NAME_PARTS`), or ends in ``-key`` / ``_key``
   (with a short list of names that are not secrets, :data:`_NOT_SECRET_NAMES`).

The limit: this is a list of known names and shapes. A secret typed into a
free-text field (a comment, for example) is not found.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "REDACTED",
    "SECRET_FIELD_NAMES",
    "is_secret_name",
    "scrub",
    "scrub_text",
]

#: What a removed value is replaced with.
REDACTED: Final[str] = "[REDACTED]"

#: Field names FortiOS uses for secrets (CLI and REST use the same names).
SECRET_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "psksecret",
        "psksecret-remote",
        "ppk-secret",
        "secret",
        "key",
        "private-key",
        "passphrase",
        "authpasswd",
        "auth-password",
        "auth-pwd",
        "api-key",
        "apikey",
        "access-token",
        "token",
        "sso-password",
        "md5-key",
        "auth-key",
        "server-key",
        "group-password",
        "ha-password",
        "community",
    }
)

#: Parts of a field name that mark a secret, wherever they are in the name.
_SECRET_NAME_PARTS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "psk",
    "passphrase",
    "private-key",
    "private_key",
    "privatekey",
    "api-key",
    "api_key",
    "token",
)

#: Names that match a rule above but hold no secret. ``q_origin_key`` is the
#: object's own name in a FortiOS answer; ``mkey`` is the key of an object.
_NOT_SECRET_NAMES: Final[frozenset[str]] = frozenset(
    {"q_origin_key", "mkey", "public-key", "psk-type", "token-count"}
)

_ENC_VALUE: Final[re.Pattern[str]] = re.compile(r"(?i)\bENC\s+\S+")
_PEM_PRIVATE_KEY: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)


def is_secret_name(name: str) -> bool:
    """Return ``True`` when the field *name* holds a secret in a FortiOS answer."""
    lowered = name.lower()
    if lowered in _NOT_SECRET_NAMES:
        return False
    if lowered in SECRET_FIELD_NAMES:
        return True
    if any(part in lowered for part in _SECRET_NAME_PARTS):
        return True
    return lowered.endswith(("-key", "_key"))


def _is_secret_value(value: str) -> bool:
    stripped = value.lstrip()
    return stripped[:4].upper() == "ENC " or bool(_PEM_PRIVATE_KEY.search(value))


def scrub(value: Any) -> tuple[Any, int]:
    """Return *value* with every secret replaced, and how many were replaced.

    Walks dicts and lists. A secret-named field is replaced whole, even when
    it holds a dict or a list. ``None`` and empty texts stay as they are, so
    an empty secret field still shows that it is empty.
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            name = str(key)
            if is_secret_name(name) and item not in (None, "", [], {}):
                out[name] = REDACTED
                count += 1
                continue
            cleaned, found = scrub(item)
            out[name] = cleaned
            count += found
        return out, count
    if isinstance(value, list | tuple):
        items: list[Any] = []
        count = 0
        for item in value:
            cleaned, found = scrub(item)
            items.append(cleaned)
            count += found
        return items, count
    if isinstance(value, str) and _is_secret_value(value):
        return REDACTED, 1
    return value, 0


def scrub_text(text: str) -> str:
    """Return *text* with every ``ENC ...`` value and PEM private key replaced.

    For error texts and other free text, where there is no field name to go by.
    """
    cleaned = _PEM_PRIVATE_KEY.sub(REDACTED, text)
    return _ENC_VALUE.sub(REDACTED, cleaned)
