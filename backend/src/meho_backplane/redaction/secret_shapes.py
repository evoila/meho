# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Say whether a free text holds something that looks like a secret.

MEHO keeps one list of secret shapes: the named patterns in
:mod:`.patterns`, in the order of the default redaction policy
(``policies/default.yaml``). The value hiding on approval cards and in
stored results runs that policy. :func:`secret_shape` runs the same rules
over a free text, so a shape added to the list is caught in every place.

Two more rules sit here, because a regex cannot do them. They decode the
text first:

* a signed token (JWT) whose first part decodes to JSON with an ``alg``
  field, also when its parts are shorter than the ``jwt`` pattern needs;
* a base64 text that decodes to a key: a raw key of a usual size, a DER
  private key, or a text that holds a secret of the list.

Each check reads the text a fixed number of times, so its time grows in
step with the length of the text.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from itertools import pairwise
from typing import Final

from meho_backplane.redaction.patterns import get_pattern, secret_spans
from meho_backplane.redaction.resolver import get_default_policy

__all__ = ["SHAPE_WORDS", "secret_shape"]

#: How a sentence names each pattern of the default policy.
SHAPE_WORDS: Final[dict[str, str]] = {
    "bind_key_secret": "a BIND key secret",
    "pem_private_key": "a private key",
    "authorization_header": "an Authorization header",
    "bearer_token": "a bearer token",
    "jwt": "a JWT",
    "url_secret": "a link with a password or a secret part",
    "api_key": "a password or key after its name",
    "vendor_token": "a vendor access token",
    "kubeconfig": "a kubeconfig",
    "password_hash": "a password hash",
    "ceph_key": "a Ceph key",
}

_B64URL_RUN_RE: Final = re.compile(r"[A-Za-z0-9_-]+")
#: The shortest JWT header or payload part: ``eyJ`` and eight more characters.
_JWT_PART_MIN: Final = 11
_BASE64_RE: Final = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
#: Raw symmetric-key sizes (AES-128/192/256, HMAC-384/512, Fernet = 32).
_BINARY_KEY_LENGTHS: Final[frozenset[int]] = frozenset({16, 24, 32, 48, 64})


def secret_shape(text: str) -> str | None:
    """Name what *text* holds that looks like a secret ("a JWT", ...), or ``None``."""
    if _jwt_shaped(text):
        return "a JWT"
    kind = _listed_shape(text)
    if kind is not None:
        return kind
    return _decoded_shape(text)


def _listed_shape(text: str) -> str | None:
    for rule in get_default_policy().rules:
        if secret_spans(get_pattern(rule.pattern), text):
            return SHAPE_WORDS.get(rule.pattern, "a secret")
    return None


def _decoded_shape(text: str) -> str | None:
    decoded = _b64_decode(text)
    if decoded is None:
        return None
    if not _is_binary(decoded):
        inner = decoded.decode("utf-8")
        kind = "a JWT" if _jwt_shaped(inner) else _listed_shape(inner)
        if kind is None:
            return None
        return "a base64-encoded " + kind.split(" ", 1)[1]
    if _looks_like_der_private_key(decoded):
        return "a base64-encoded private key"
    compact = _compact(text)
    mixed = (
        any(c.isupper() for c in compact)
        and any(c.islower() for c in compact)
        and any(c.isdigit() for c in compact)
    )
    if len(decoded) in _BINARY_KEY_LENGTHS and mixed:
        return "a base64-encoded key"
    return None


def _compact(text: str) -> str:
    # Line breaks only: base64 is often wrapped, but a value with spaces is
    # prose, and joining prose would turn a sentence into "valid base64".
    return re.sub(r"[\r\n]+", "", text.strip())


def _b64_decode(text: str) -> bytes | None:
    compact = _compact(text)
    if len(compact) < 22 or not _BASE64_RE.match(compact):
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        if "-" in compact or "_" in compact:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _is_binary(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return any(ord(c) < 0x20 and c not in "\t\r\n" for c in text)


def _looks_like_der_private_key(data: bytes) -> bool:
    # PKCS#1 / PKCS#8 / SEC1 private keys open with SEQUENCE then INTEGER 0
    # (``02 01 00``) within the first few bytes; X.509 certificates do not.
    return len(data) >= 64 and data[0] == 0x30 and b"\x02\x01\x00" in data[2:8]


def _jwt_header_start(text: str, start: int, end: int) -> int | None:
    """Where a JWT header can start in the run ``text[start:end]``, or ``None``.

    The header starts at the first ``eyJ`` that follows no letter, digit or
    ``_`` and leaves at least :data:`_JWT_PART_MIN` characters to the end of
    the run. A later ``eyJ`` in the same run is always shorter.
    """
    pos = text.find("eyJ", start, end)
    while pos != -1 and end - pos >= _JWT_PART_MIN:
        before = text[pos - 1] if pos > 0 else ""
        if not (before.isalnum() or before == "_"):
            return pos
        pos = text.find("eyJ", pos + 1, end)
    return None


def _jwt_headers(text: str) -> list[str]:
    """Each header of a ``<header>.<payload>.<signature>`` shape in *text*.

    The text is split once into runs of base64url characters. A header run
    is followed by a dot, then a payload run that starts with ``eyJ``, then
    a dot.
    """
    runs = [m.span() for m in _B64URL_RUN_RE.finditer(text)]
    headers: list[str] = []
    for (start, end), (p_start, p_end) in pairwise(runs):
        if p_start != end + 1 or text[end] != "." or text[p_end : p_end + 1] != ".":
            continue
        if p_end - p_start < _JWT_PART_MIN or not text.startswith("eyJ", p_start):
            continue
        header_start = _jwt_header_start(text, start, end)
        if header_start is not None:
            headers.append(text[header_start:end])
    return headers


def _jwt_shaped(text: str) -> bool:
    for header in _jwt_headers(text):
        try:
            decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        except (binascii.Error, ValueError):
            continue
        if isinstance(decoded, dict) and "alg" in decoded:
            return True
    return False
