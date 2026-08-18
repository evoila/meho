# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-source sender authentication for the inbound ingest endpoint (#2881).

The webhook is JWT-less: MEHO does not know the sender is who it claims until
it verifies the request against the source's Vault-custodied shared secret.
Three strategies (mirroring the ``ck_event_source_auth_strategy`` CHECK set):

* ``hmac-sha256`` -- the sender signs the raw body (optionally prefixed with
  a timestamp, which is then bound into the signature and gated by a replay
  window) with the shared key. The generic sender uses ``X-Meho-Signature``
  + ``X-Meho-Timestamp``; a vendor with different header names (Grafana's
  ``X-Grafana-Alerting-Signature``) sets them per-source via ``extras``.
* ``static-header`` -- a fixed header carries a shared token compared against
  the secret (Harbor).
* ``basic`` -- HTTP Basic; the non-secret username lives in ``extras``, the
  password is the Vault secret (Alertmanager ``http_config``).

Constant-time discipline (acceptance criterion 4)
================================================

**Every** comparison that touches secret material runs through
:func:`hmac.compare_digest`, so a network attacker cannot recover the secret
(or the expected signature) one byte at a time from response-timing. The
Basic path computes both the username and password verdicts before combining
them, so it leaks neither which half failed nor the username length through
an early return. All comparisons are on ``bytes`` -- ``compare_digest`` on a
``str`` carrying non-ASCII raises, and a secret is arbitrary bytes.

Fail-closed: any missing header, malformed value, unparseable timestamp, or
mismatch raises :class:`IngestAuthError`. The endpoint maps it to a uniform
``401`` -- the reason is logged server-side, never returned, so the response
is the same shape whatever failed (no oracle for *why* auth failed).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Mapping

from pydantic import SecretStr

from meho_backplane.event_source.schemas import EventSourceAuthStrategy
from meho_backplane.events.ingest.config import IngestConfig

__all__ = ["IngestAuthError", "verify_ingest_auth"]


class IngestAuthError(Exception):
    """Sender authentication failed. Carries a server-side-only *reason*.

    The reason is a stable machine token (``bad_signature``,
    ``stale_timestamp``, ``missing_signature``, ...) logged for operator
    debugging; it is **never** returned to the sender, so every failure
    presents the same uniform ``401`` and reveals nothing about which check
    tripped.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _require_header(headers: Mapping[str, str], name: str, reason: str) -> str:
    """Return the non-empty header *name* or raise :class:`IngestAuthError`."""
    value = headers.get(name)
    if not value or not value.strip():
        raise IngestAuthError(reason)
    return value.strip()


def _verify_hmac_sha256(
    secret: SecretStr,
    headers: Mapping[str, str],
    raw_body: bytes,
    config: IngestConfig,
    now: float,
) -> None:
    """Verify an HMAC-SHA256 signature over the (optionally timestamped) body.

    When ``config.require_timestamp`` is set, the timestamp header is
    mandatory, must parse as unix seconds, must fall inside the replay
    window, and is bound into the signed message (``"<ts>." + body``) so an
    attacker cannot swap a fresh timestamp onto a captured body. When it is
    unset (a body-only vendor scheme), the signature covers the raw body
    alone and no replay check applies.
    """
    provided = _require_header(headers, config.signature_header, "missing_signature")

    signed = raw_body
    if config.require_timestamp:
        ts_raw = _require_header(headers, config.timestamp_header, "missing_timestamp")
        try:
            ts = float(ts_raw)
        except ValueError as exc:
            raise IngestAuthError("bad_timestamp") from exc
        if abs(now - ts) > config.replay_window_seconds:
            raise IngestAuthError("stale_timestamp")
        signed = ts_raw.encode("utf-8") + b"." + raw_body

    expected = hmac.new(
        secret.get_secret_value().encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    # Hex is case-insensitive; normalise both sides so a sender emitting
    # upper-case hex is not spuriously rejected. compare_digest on unequal
    # lengths returns False without leaking the length difference by timing.
    if not hmac.compare_digest(expected.encode("ascii"), provided.lower().encode("ascii")):
        raise IngestAuthError("bad_signature")


def _verify_static_header(
    secret: SecretStr,
    headers: Mapping[str, str],
    config: IngestConfig,
) -> None:
    """Constant-time compare a fixed header value against the shared token."""
    provided = _require_header(headers, config.static_header, "missing_static_header")
    if not hmac.compare_digest(provided.encode("utf-8"), secret.get_secret_value().encode("utf-8")):
        raise IngestAuthError("bad_static_header")


def _verify_basic(
    secret: SecretStr,
    headers: Mapping[str, str],
    config: IngestConfig,
) -> None:
    """Verify HTTP Basic: username from ``extras``, password is the secret."""
    header = _require_header(headers, "Authorization", "missing_authorization")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise IngestAuthError("bad_authorization_scheme")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise IngestAuthError("malformed_basic_credentials") from exc
    username, sep, password = decoded.partition(":")
    if not sep:
        raise IngestAuthError("malformed_basic_credentials")
    # Compute both verdicts before combining -- no early return that would
    # leak which half failed or the username length through timing.
    user_ok = hmac.compare_digest(username.encode("utf-8"), config.basic_username.encode("utf-8"))
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"), secret.get_secret_value().encode("utf-8")
    )
    if not (user_ok and pass_ok):
        raise IngestAuthError("bad_basic_credentials")


def verify_ingest_auth(
    *,
    strategy: EventSourceAuthStrategy,
    secret: SecretStr,
    headers: Mapping[str, str],
    raw_body: bytes,
    config: IngestConfig,
    now: float,
) -> None:
    """Verify the sender against the source's *strategy*. Raise on any failure.

    ``headers`` is the request's case-insensitive header map, ``raw_body`` the
    exact bytes the signature is computed over (never a re-serialised view),
    ``now`` the current unix time for the replay window. Returns ``None`` on
    success; raises :class:`IngestAuthError` (uniform ``401`` at the boundary)
    otherwise.
    """
    if strategy is EventSourceAuthStrategy.HMAC_SHA256:
        _verify_hmac_sha256(secret, headers, raw_body, config, now)
    elif strategy is EventSourceAuthStrategy.STATIC_HEADER:
        _verify_static_header(secret, headers, config)
    elif strategy is EventSourceAuthStrategy.BASIC:
        _verify_basic(secret, headers, config)
    else:  # pragma: no cover -- exhaustive over the closed StrEnum
        raise IngestAuthError("unsupported_strategy")
