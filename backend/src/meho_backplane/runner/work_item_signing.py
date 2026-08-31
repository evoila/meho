# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ed25519 signing + edge verification of ``remote-write`` work items (#3189).

Mechanism 1 (part 2) of the satellite write-path composed gate (Initiative
#2901, design ``docs/research/2901-satellite-write-path.md`` §3, decision
``docs/decisions/satellite-write-path.md``): a **real signature** over the
canonical serialisation of a ``remote-write`` work item, created **once at
authorization time** by the central mint and verified **offline at the edge**
by the DB-free runner.

This is the deliberate, write-tier-only reversal of #2500 ("the token is a
bare DB-row PK, not a JWT"). For a ``safe`` read an edge-verifiable signature
bought nothing over the central consume latch; for a **write** the signature
is the offline integrity + freshness + target-scope check against transit
tampering (threat T2) and the non-repudiation anchor the store-and-forward
effect audit references. The DB consume latch is **retained unchanged** for
at-most-once acceptance — the signature does not replace central state.

Key custody (asymmetric on purpose): the **signing (private) key is custodied
centrally** and used only by the central mint
(:func:`~meho_backplane.operations.gateway_commands.mint_gateway_command`); the
**verification (public) key is provisioned to the runner at enrollment**.
Ed25519 rather than an HMAC precisely so a compromised runner — which holds
only the public key — cannot forge a work item (a symmetric secret on the
fenced host would let it mint its own capabilities, defeating T2 and the
non-repudiation property).

This module lives beside :mod:`meho_backplane.runner.wire` and
:mod:`meho_backplane.runner.satellite_tier` — the other central+edge shared
contracts — and imports only the standard library and ``cryptography`` (which
does **not** pull the DB stack), so verifying a signature on the DB-free
runner never imports the central Postgres layer.

The signed payload is exactly the three checks the edge performs — integrity
over ``op_id`` + ``params_hash``, ``target_scope``, and the ``expires_at``
freshness bound — so a tampered op/params/target breaks the signature and a
validly-signed-but-stale item is caught by the separate freshness check at the
edge (see :func:`~meho_backplane.runner.executor._screen_item`).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "TARGETLESS_SCOPE",
    "SigningKeyUnavailableError",
    "load_signing_key",
    "load_verify_key",
    "params_digest",
    "remote_write_signing_bytes",
    "sign_remote_write_item",
    "verify_remote_write_item",
]

#: The canonical ``target_scope`` token for a targetless synthetic op (an item
#: with no resolved target descriptor). A concrete op signs ``str(target.id)``.
TARGETLESS_SCOPE = ""

#: The signed-payload schema marker — a future incompatible change to the
#: signed fields is a visible bump, never a silent reinterpretation.
_PAYLOAD_VERSION = 1


class SigningKeyUnavailableError(Exception):
    """The Ed25519 signing / verification key is not provisioned or malformed.

    Raised by :func:`load_signing_key` / :func:`load_verify_key` when the
    configured key is empty (not provisioned) or is not a valid base64
    32-byte Ed25519 key. Both the central mint and the edge treat this as
    **fail-closed**: a ``remote-write`` capability is neither minted nor
    executed when the key seam is unavailable.
    """


def params_digest(params: dict[str, Any]) -> str:
    """Return the stable SHA-256 hex digest over the canonicalised *params*.

    An **edge-safe mirror** of
    :func:`meho_backplane.operations._validate.compute_params_hash`: identical
    canonicalisation (``json.dumps(..., sort_keys=True, default=str,
    separators=(",", ":"))`` then SHA-256) so the digest the centre signs and
    the digest the runner recomputes over the params it is about to execute
    agree byte-for-byte. Duplicated here — rather than imported — because
    ``operations._validate`` pulls the DB stack, which must never load on the
    DB-free runner. The two are pinned equal by a unit test.
    """
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_expires_at(expires_at: datetime) -> str:
    """Normalise *expires_at* to a canonical UTC ISO-8601 string.

    Both ends must serialise the freshness bound identically for the signature
    to verify, so a naive datetime is treated as UTC and every value is
    converted to UTC before formatting.
    """
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at.astimezone(UTC).isoformat()


def remote_write_signing_bytes(
    *,
    op_id: str,
    params_hash: str,
    target_scope: str,
    expires_at: datetime,
) -> bytes:
    """Return the canonical byte serialisation the signature is computed over.

    Deterministic (sorted keys, tight separators) so the centre's signing
    input and the runner's verification input are byte-identical. The four
    fields map one-to-one onto the edge checks: integrity over
    ``op_id`` + ``params_hash``, the ``target_scope`` binding, and the
    ``expires_at`` freshness bound.
    """
    payload = {
        "v": _PAYLOAD_VERSION,
        "op_id": op_id,
        "params_hash": params_hash,
        "target_scope": target_scope,
        "expires_at": _canonical_expires_at(expires_at),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_signing_key(raw_b64: str) -> Ed25519PrivateKey:
    """Load the central Ed25519 **signing** key from a base64 32-byte seed.

    Raises :class:`SigningKeyUnavailableError` when unset or malformed — the
    mint fails closed rather than minting an unsigned remote-write capability.
    """
    seed = _decode_key(raw_b64, role="signing")
    try:
        return Ed25519PrivateKey.from_private_bytes(seed)
    except ValueError as exc:  # not 32 bytes
        raise SigningKeyUnavailableError(
            f"satellite-write signing key is not a valid Ed25519 private key: {exc}"
        ) from exc


def load_verify_key(raw_b64: str) -> Ed25519PublicKey:
    """Load the runner's Ed25519 **verification** key from a base64 32-byte key.

    Raises :class:`SigningKeyUnavailableError` when unset or malformed — the
    edge fails closed rather than executing an unverifiable remote-write item.
    """
    key = _decode_key(raw_b64, role="verification")
    try:
        return Ed25519PublicKey.from_public_bytes(key)
    except ValueError as exc:  # not 32 bytes
        raise SigningKeyUnavailableError(
            f"satellite-write verification key is not a valid Ed25519 public key: {exc}"
        ) from exc


def _decode_key(raw_b64: str, *, role: str) -> bytes:
    """Base64-decode a configured key, raising a fail-closed error on any fault."""
    if not raw_b64:
        raise SigningKeyUnavailableError(
            f"satellite-write {role} key is not provisioned "
            "(remote-write capabilities are refused fail-closed)"
        )
    try:
        return base64.b64decode(raw_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SigningKeyUnavailableError(
            f"satellite-write {role} key is not valid base64: {exc}"
        ) from exc


def sign_remote_write_item(
    signing_key: Ed25519PrivateKey,
    *,
    op_id: str,
    params_hash: str,
    target_scope: str,
    expires_at: datetime,
) -> str:
    """Sign the canonical work-item payload, returning a base64 signature."""
    message = remote_write_signing_bytes(
        op_id=op_id,
        params_hash=params_hash,
        target_scope=target_scope,
        expires_at=expires_at,
    )
    return base64.b64encode(signing_key.sign(message)).decode("ascii")


def verify_remote_write_item(
    verify_key: Ed25519PublicKey,
    signature_b64: str,
    *,
    op_id: str,
    params_hash: str,
    target_scope: str,
    expires_at: datetime,
) -> bool:
    """Return ``True`` iff *signature_b64* is a valid signature over the payload.

    Fail-closed: a malformed base64 signature or any integrity/scope mismatch
    returns ``False`` (never raises). The **freshness** bound is verified
    separately by the caller against ``expires_at`` — a signature over a stale
    item is cryptographically valid but must still be refused.
    """
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    message = remote_write_signing_bytes(
        op_id=op_id,
        params_hash=params_hash,
        target_scope=target_scope,
        expires_at=expires_at,
    )
    try:
        verify_key.verify(signature, message)
    except InvalidSignature:
        return False
    return True
