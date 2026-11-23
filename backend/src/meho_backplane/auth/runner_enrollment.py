# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One-time enrollment of a runner's own public key.

``meho runner-principal register`` prints a one-time code. A runner at its
site makes a key pair, keeps the private key on its own disk, and sends the
public key with that code to ``POST /api/v1/runner-principals/enroll``. This
module checks the code and puts the public key on the runner's Keycloak
client. The client then logs in with a signed JWT (Keycloak "Signed JWT"
client authentication), so the runner gets short tokens without any person
handing it a new token.

The code has the form ``<runner id>.<secret>``. Only its SHA-256 is stored.
It works once, and only until it expires. A wrong, used, expired or revoked
code all get the same refusal, so a caller learns nothing from the answer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from sqlalchemy import select, update

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, RunnerPrincipal
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.settings import get_settings

__all__ = [
    "ENROLLMENT_CODE_TTL",
    "SIGNING_ALG",
    "EnrollmentCodeInvalidError",
    "EnrollmentKeyInvalidError",
    "RunnerEnrollment",
    "enroll_runner_key",
    "new_enrollment_code",
]

#: How long a code works after it is made.
ENROLLMENT_CODE_TTL: Final[timedelta] = timedelta(minutes=30)

#: The signature type of the runner's login. ES256 is an elliptic-curve key
#: that Keycloak accepts for signed JWT client login.
SIGNING_ALG: Final[str] = "ES256"

_ENROLL_AUDIT_PATH: Final[str] = "runner.principal.key_enrolled"
_PRIVATE_JWK_MEMBERS: Final[frozenset[str]] = frozenset({"d", "p", "q", "dp", "dq", "qi", "k"})


class EnrollmentCodeInvalidError(Exception):
    """The code is wrong, already used, expired, or its runner is revoked."""


class EnrollmentKeyInvalidError(ValueError):
    """The public key is not a usable P-256 public key."""


@dataclass(frozen=True)
class RunnerEnrollment:
    """What the runner needs to log in after its key is enrolled."""

    client_id: str
    issuer: str
    token_endpoint: str
    signing_alg: str
    key_id: str


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def new_enrollment_code(runner_id: uuid.UUID) -> tuple[str, str, datetime]:
    """Return ``(code, code_hash, expires_at)`` for *runner_id*."""
    code = f"{runner_id.hex}.{secrets.token_urlsafe(32)}"
    return code, _hash_code(code), datetime.now(UTC) + ENROLLMENT_CODE_TTL


def _parse_runner_id(code: str) -> uuid.UUID:
    head, sep, tail = code.partition(".")
    if not sep or not tail:
        raise EnrollmentCodeInvalidError
    try:
        return uuid.UUID(hex=head)
    except ValueError:
        raise EnrollmentCodeInvalidError from None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _public_jwks(public_jwk: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Check *public_jwk* and return ``(jwks, key_id)`` for Keycloak.

    Accepts only a P-256 public key. A key with a private member is refused,
    so a private key can never be stored by mistake.
    """
    if _PRIVATE_JWK_MEMBERS & public_jwk.keys():
        raise EnrollmentKeyInvalidError("send the public key only")
    try:
        key = ECAlgorithm.from_jwk(json.dumps(public_jwk))
    except Exception as exc:
        raise EnrollmentKeyInvalidError("the public key is not a valid EC key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise EnrollmentKeyInvalidError("the public key must be an EC P-256 public key")
    clean = {k: public_jwk[k] for k in ("kty", "crv", "x", "y")}
    key_id = _thumbprint(clean)
    jwk = {**clean, "use": "sig", "alg": SIGNING_ALG, "kid": key_id}
    return {"keys": [jwk]}, key_id


def _thumbprint(clean: dict[str, str]) -> str:
    """RFC 7638 thumbprint of an EC public key (base64url SHA-256)."""
    canonical = json.dumps(
        {k: clean[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _consume_code(code: str) -> RunnerPrincipal:
    """Use the code up in one step; refuse it when it is not good."""
    runner_id = _parse_runner_id(code)
    presented = _hash_code(code)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.id == runner_id))
        ).scalar_one_or_none()
        stored = row.enrollment_code_hash if row is not None else None
        # Compare in constant time; do the same work for a missing row.
        if row is None or stored is None or not hmac.compare_digest(stored, presented):
            raise EnrollmentCodeInvalidError
        expires = row.enrollment_code_expires_at
        if row.revoked or expires is None or _as_utc(expires) <= datetime.now(UTC):
            raise EnrollmentCodeInvalidError
        # The condition on the stored hash makes the code work once, also
        # when two requests arrive together: only one update matches.
        result = await session.execute(
            update(RunnerPrincipal)
            .where(
                RunnerPrincipal.id == runner_id,
                RunnerPrincipal.enrollment_code_hash == presented,
            )
            .values(enrollment_code_hash=None)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise EnrollmentCodeInvalidError
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
        row.enrollment_code_expires_at = expires
    return row


async def _give_code_back(row: RunnerPrincipal, code: str) -> None:
    """Put the code back when the key could not be stored, so the runner can retry."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(RunnerPrincipal)
            .where(RunnerPrincipal.id == row.id, RunnerPrincipal.revoked.is_(False))
            .values(
                enrollment_code_hash=_hash_code(code),
                enrollment_code_expires_at=row.enrollment_code_expires_at,
            )
        )
        await session.commit()


async def enroll_runner_key(code: str, public_jwk: dict[str, Any]) -> RunnerEnrollment:
    """Check *code* and put *public_jwk* on the runner's Keycloak client.

    Raises :class:`EnrollmentKeyInvalidError` for a bad key (the code is kept),
    :class:`EnrollmentCodeInvalidError` for a bad code, and a
    :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError` when
    Keycloak cannot store the key (the code is kept, so the runner can retry).
    """
    log = structlog.get_logger()
    jwks, key_id = _public_jwks(public_jwk)
    row = await _consume_code(code)
    try:
        async with KeycloakAdminClient.from_settings() as kc:
            await kc.set_client_signed_jwt_key(
                row.keycloak_internal_id, jwks=jwks, signing_alg=SIGNING_ALG
            )
    except BaseException:
        await _give_code_back(row, code)
        raise
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(RunnerPrincipal)
            .where(RunnerPrincipal.id == row.id)
            .values(key_enrolled_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        await session.commit()
    await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub=row.keycloak_client_id,
        actor_sub=row.keycloak_client_id,
        tenant_id=row.tenant_id,
        method=INTERNAL_METHOD,
        path=_ENROLL_AUDIT_PATH,
        status_code=200,
        duration_ms=0.0,
        payload={"runner": row.name, "key_id": key_id},
    )
    log.info("runner_key_enrolled", tenant_id=str(row.tenant_id), name=row.name, key_id=key_id)
    issuer = str(get_settings().keycloak_issuer_url).rstrip("/")
    return RunnerEnrollment(
        client_id=row.keycloak_client_id,
        issuer=issuer,
        token_endpoint=f"{issuer}/protocol/openid-connect/token",
        signing_alg=SIGNING_ALG,
        key_id=key_id,
    )
