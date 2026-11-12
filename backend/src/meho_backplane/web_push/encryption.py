# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Encrypt a push message and sign the request to the push service.

* :func:`encrypt` -- the message body, encrypted for one browser as web
  push requires (RFC 8291, content coding ``aes128gcm`` of RFC 8188). Only
  that browser can read it; the push service in between cannot.
* :func:`vapid_authorization` -- the ``Authorization`` header (RFC 8292):
  a short-lived token signed with this MEHO's push key, so the push service
  knows the message comes from the MEHO the browser subscribed to.

No function here logs or returns the private key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from meho_backplane.web_push.keys import b64url_decode, b64url_encode

__all__ = ["MAX_PLAINTEXT_SIZE", "RECORD_SIZE", "encrypt", "vapid_authorization"]

#: The record size in the header. One record holds the whole message.
RECORD_SIZE = 4096
#: The largest message that fits into one push message (RFC 8291 section 4).
MAX_PLAINTEXT_SIZE = 3993
_SALT_BYTES = 16
_PRIVATE_KEY_BYTES = 32
#: RFC 8292 allows at most 24 hours; 12 hours leaves room for clock skew.
_TOKEN_LIFETIME_SECONDS = 12 * 60 * 60


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def encrypt(
    plaintext: bytes,
    *,
    p256dh: str,
    auth: str,
    server_key: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Return the encrypted body for the browser with keys *p256dh* and *auth*.

    *server_key* and *salt* are made fresh for each message; tests pass them
    to check the result against the example of RFC 8291, Appendix A.
    Raises :class:`ValueError` for a message larger than
    :data:`MAX_PLAINTEXT_SIZE`: it would not fit into one record.
    """
    if len(plaintext) > MAX_PLAINTEXT_SIZE:
        raise ValueError("the push message is larger than one record")
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(_SALT_BYTES)
    as_public = _public_bytes(server_key)

    browser_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = server_key.exchange(ec.ECDH(), browser_key)
    # RFC 8291 section 3.4: combine the shared secret with the auth secret.
    prk_key = _hmac(auth_secret, ecdh_secret)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac(prk_key, key_info + b"\x01")
    # RFC 8188 section 2.2 and 2.3: the content key and the nonce.
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    # 0x02 marks the last (and only) record.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


def vapid_authorization(endpoint: str, *, private_key: str, subject: str | None) -> str:
    """Return the ``Authorization`` header value for a message to *endpoint*.

    *private_key* is the stored push key (base64url of the raw 32-byte
    value). *subject* is the ``https:`` address of this MEHO, or ``None``
    when it is not known; some push services refuse a token without one.
    """
    raw = b64url_decode(private_key)
    if len(raw) != _PRIVATE_KEY_BYTES:
        raise ValueError("the push key is not 32 bytes")
    key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    # The origin of the push service, from the host only: never a user
    # name or a port written into the endpoint.
    parts = urlsplit(endpoint)
    claims: dict[str, str | int] = {
        "aud": f"{parts.scheme}://{parts.hostname}",
        "exp": int(time.time()) + _TOKEN_LIFETIME_SECONDS,
    }
    if subject:
        claims["sub"] = subject
    token = jwt.encode(claims, key, algorithm="ES256", headers={"typ": "JWT"})
    return f"vapid t={token}, k={b64url_encode(_public_bytes(key))}"
