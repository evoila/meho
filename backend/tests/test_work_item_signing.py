# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ed25519 signing + edge verification of remote-write work items (#3189).

Pure-function tests for :mod:`meho_backplane.runner.work_item_signing` — the
edge-safe signing seam (mechanism 1 of the satellite write-path composed gate).
No DB, no session.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.runner.work_item_signing import (
    TARGETLESS_SCOPE,
    SigningKeyUnavailableError,
    load_signing_key,
    load_verify_key,
    params_digest,
    sign_remote_write_item,
    verify_remote_write_item,
)

_EXPIRES = datetime(2099, 1, 1, tzinfo=UTC)
_SCOPE = "11111111-1111-1111-1111-111111111111"


def _keypair() -> tuple[str, str]:
    """A fresh (signing_b64, verify_b64) Ed25519 pair as base64 raw 32-byte keys."""
    key = Ed25519PrivateKey.generate()
    signing_b64 = base64.b64encode(key.private_bytes_raw()).decode("ascii")
    verify_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    return signing_b64, verify_b64


def _sign(
    signing_b64: str,
    *,
    op_id: str = "vmware.vm.tag_set",
    params_hash: str = "abc123",
    target_scope: str = _SCOPE,
    expires_at: datetime = _EXPIRES,
) -> str:
    return sign_remote_write_item(
        load_signing_key(signing_b64),
        op_id=op_id,
        params_hash=params_hash,
        target_scope=target_scope,
        expires_at=expires_at,
    )


def _verify(
    verify_b64: str,
    signature: str,
    *,
    op_id: str = "vmware.vm.tag_set",
    params_hash: str = "abc123",
    target_scope: str = _SCOPE,
    expires_at: datetime = _EXPIRES,
) -> bool:
    return verify_remote_write_item(
        load_verify_key(verify_b64),
        signature,
        op_id=op_id,
        params_hash=params_hash,
        target_scope=target_scope,
        expires_at=expires_at,
    )


def test_params_digest_matches_compute_params_hash() -> None:
    # The edge-safe mirror must equal the DB-coupled canonical hash byte-for-byte,
    # so the digest the centre signs and the runner recomputes always agree.
    params = {"z": 1, "a": [3, 2, 1], "id": uuid.uuid4(), "when": datetime(2026, 1, 1, tzinfo=UTC)}
    assert params_digest(params) == compute_params_hash(params)
    assert params_digest({}) == compute_params_hash({})


def test_sign_then_verify_roundtrip() -> None:
    signing_b64, verify_b64 = _keypair()
    assert _verify(verify_b64, _sign(signing_b64)) is True


@pytest.mark.parametrize(
    "tamper",
    [
        {"op_id": "vmware.vm.destroy"},
        {"params_hash": "deadbeef"},
        {"target_scope": "22222222-2222-2222-2222-222222222222"},
        {"expires_at": _EXPIRES + timedelta(days=1)},
    ],
)
def test_verify_fails_on_any_tampered_field(tamper: dict[str, object]) -> None:
    # Every signed field is bound: changing op / params / target-scope / freshness
    # after signing breaks verification (integrity + scope + freshness anchor).
    signing_b64, verify_b64 = _keypair()
    signature = _sign(signing_b64)
    assert _verify(verify_b64, signature, **tamper) is False  # type: ignore[arg-type]


def test_verify_fails_with_wrong_key() -> None:
    signing_b64, _ = _keypair()
    _, other_verify_b64 = _keypair()
    # A signature from key A does not verify under key B — the non-repudiation
    # property (only the central signing key can mint a valid capability).
    assert _verify(other_verify_b64, _sign(signing_b64)) is False


def test_verify_fails_on_malformed_signature() -> None:
    _, verify_b64 = _keypair()
    assert _verify(verify_b64, "not-base64!!") is False


def test_targetless_scope_roundtrips() -> None:
    signing_b64, verify_b64 = _keypair()
    signature = _sign(signing_b64, target_scope=TARGETLESS_SCOPE)
    assert _verify(verify_b64, signature, target_scope=TARGETLESS_SCOPE) is True


def test_expires_at_naive_is_treated_as_utc() -> None:
    # A naive datetime must sign/verify identically to its UTC-aware twin, so a
    # DB round-trip that drops tzinfo cannot break verification.
    signing_b64, verify_b64 = _keypair()
    aware = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
    naive = datetime(2099, 6, 1, 12, 0)
    signature = _sign(signing_b64, expires_at=aware)
    assert _verify(verify_b64, signature, expires_at=naive) is True


@pytest.mark.parametrize("bad", ["", "not-base64!!", base64.b64encode(b"tooshort").decode("ascii")])
def test_load_signing_key_fails_closed(bad: str) -> None:
    with pytest.raises(SigningKeyUnavailableError):
        load_signing_key(bad)


@pytest.mark.parametrize("bad", ["", "not-base64!!", base64.b64encode(b"tooshort").decode("ascii")])
def test_load_verify_key_fails_closed(bad: str) -> None:
    with pytest.raises(SigningKeyUnavailableError):
        load_verify_key(bad)
