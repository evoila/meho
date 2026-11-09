# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks on a push subscription that a browser sends.

MEHO will later send messages to the subscription's endpoint, so the
endpoint must be a push service of a browser maker, and nothing else: no
host inside the network, no other port, no plain http. The keys must have
the sizes the web push encryption needs (RFC 8291): ``p256dh`` is a P-256
public key (65 bytes), ``auth`` is 16 bytes.

The error texts never quote the endpoint or the keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

from meho_backplane.web_push.keys import b64url_decode

__all__ = [
    "MAX_ENDPOINT_LENGTH",
    "CheckedSubscription",
    "SubscriptionRefusedError",
    "check_subscription",
    "push_service_name",
]

MAX_ENDPOINT_LENGTH = 2048
_MAX_KEY_TEXT = 128
_P256DH_BYTES = 65
_AUTH_BYTES = 16

# The browser makers' push services. An exact host, or a suffix that starts
# with a dot (so "fcm.googleapis.com.example" never matches).
_EXACT_HOSTS: dict[str, str] = {
    "fcm.googleapis.com": "Google",
    "updates.push.services.mozilla.com": "Mozilla",
}
_HOST_SUFFIXES: dict[str, str] = {
    ".push.apple.com": "Apple",
    ".notify.windows.com": "Microsoft",
}


class SubscriptionRefusedError(ValueError):
    """The subscription is not accepted; the message is one plain sentence."""


@dataclass(frozen=True)
class CheckedSubscription:
    """A subscription that passed every check."""

    endpoint: str
    p256dh: str
    auth: str


def push_service_name(endpoint: str) -> str | None:
    """Return the browser maker of *endpoint*'s push service, or ``None``."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    if port not in (None, 443):
        return None
    host = (parts.hostname or "").lower()
    if host in _EXACT_HOSTS:
        return _EXACT_HOSTS[host]
    for suffix, name in _HOST_SUFFIXES.items():
        if host.endswith(suffix):
            return name
    return None


def _decoded_key(text: str, *, name: str, size: int) -> bytes:
    if not text or len(text) > _MAX_KEY_TEXT:
        raise SubscriptionRefusedError(f"The subscription key {name} is missing or too long.")
    try:
        raw = b64url_decode(text)
    except ValueError as exc:
        raise SubscriptionRefusedError(f"The subscription key {name} is not base64url.") from exc
    if len(raw) != size:
        raise SubscriptionRefusedError(f"The subscription key {name} has the wrong size.")
    return raw


def check_subscription(endpoint: str, p256dh: str, auth: str) -> CheckedSubscription:
    """Return the checked subscription, or raise :class:`SubscriptionRefusedError`."""
    endpoint = endpoint.strip()
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise SubscriptionRefusedError("The push endpoint is missing or too long.")
    if push_service_name(endpoint) is None:
        raise SubscriptionRefusedError(
            "The push endpoint is not a push service of Apple, Google, Microsoft or Mozilla."
        )
    p256dh = p256dh.strip()
    auth = auth.strip()
    raw_p256dh = _decoded_key(p256dh, name="p256dh", size=_P256DH_BYTES)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw_p256dh)
    except ValueError as exc:
        raise SubscriptionRefusedError("The subscription key p256dh is not a P-256 key.") from exc
    _decoded_key(auth, name="auth", size=_AUTH_BYTES)
    return CheckedSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
