# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The admin setting and the push key (VAPID) of this MEHO.

The push key is a P-256 key pair. The private half proves to the push
services that a message comes from this MEHO; it stays on the server and is
never logged or shown. The public half is public by design: each browser
needs it to subscribe (``applicationServerKey``), so the account page hands
it to the browser.

The install makes the private key once, in the Secret
``<release>-core-web-push`` (docs/codebase/core-secrets.md), and the chart
passes it in ``WEB_PUSH_VAPID_PRIVATE_KEY``. The format is the base64url
form of the raw 32-byte private value, without padding.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meho_backplane.settings import get_settings

__all__ = [
    "b64url_decode",
    "b64url_encode",
    "new_private_key",
    "public_key_for",
    "push_public_key",
    "reset_key_cache_for_testing",
]

_log = structlog.get_logger(__name__)

_PRIVATE_KEY_BYTES = 32
_B64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def b64url_encode(data: bytes) -> str:
    """Encode *data* as base64url without padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    """Decode base64url with or without padding; raise ``ValueError`` on bad input.

    Strict: any character outside the base64url alphabet is an error (the
    standard decoder would silently drop it).
    """
    if any(ch not in _B64URL_CHARS for ch in text.rstrip("=")):
        raise ValueError("not base64url")
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("not base64url") from exc


def new_private_key() -> str:
    """Return a fresh push private key in the stored format."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64url_encode(key.private_numbers().private_value.to_bytes(_PRIVATE_KEY_BYTES, "big"))


def public_key_for(private_key: str) -> str:
    """Return the public key (uncompressed point, base64url) of *private_key*.

    Raises ``ValueError`` when the text is not a valid key. The message never
    holds the key.
    """
    try:
        raw = b64url_decode(private_key)
    except ValueError as exc:
        raise ValueError("the push key is not base64url") from exc
    if len(raw) != _PRIVATE_KEY_BYTES:
        raise ValueError("the push key is not 32 bytes")
    try:
        key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except ValueError as exc:
        raise ValueError("the push key is not a P-256 key") from exc
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url_encode(public)


@lru_cache(maxsize=1)
def _public_key_from_settings() -> str | None:
    private_key = get_settings().web_push_vapid_private_key
    if not private_key:
        _log.warning("web_push_key_missing", fix="set WEB_PUSH_VAPID_PRIVATE_KEY")
        return None
    try:
        return public_key_for(private_key)
    except ValueError as exc:
        _log.warning("web_push_key_unusable", reason=str(exc))
        return None


def push_public_key() -> str | None:
    """Return the public push key when the push feature can run, else ``None``.

    ``None`` when the admin setting is off (the key is then not read at all),
    or when the setting is on but the key is missing or not valid. In both
    cases the push routes answer 404 and the account page shows no switch.
    """
    if not get_settings().web_push_enabled:
        return None
    return _public_key_from_settings()


def reset_key_cache_for_testing() -> None:
    """Forget the cached public key (tests change the settings)."""
    _public_key_from_settings.cache_clear()
