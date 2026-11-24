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
It enrolls one key, and only until it expires. A wrong, used, expired or
revoked code all get the same refusal, so a caller learns nothing from the
answer. The runner that holds the enrolled key can send the same code and key
again: it gets the same answer and nothing changes.
"""

from __future__ import annotations

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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, RunnerPrincipal
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.settings import get_settings

__all__ = [
    "ENROLLMENT_CODE_TTL",
    "SIGNING_ALG",
    "EnrollmentCodeInvalidError",
    "EnrollmentKeyInvalidError",
    "RunnerEnrollment",
    "enroll_runner_key",
    "new_enrollment_code",
    "take_runner_lock",
]

#: How long a code works after it is made.
ENROLLMENT_CODE_TTL: Final[timedelta] = timedelta(minutes=30)

#: The signature type of the runner's login. ES256 is an elliptic-curve key
#: that Keycloak accepts for signed JWT client login.
SIGNING_ALG: Final[str] = "ES256"

#: A longer text is no code. A real one has about 80 characters.
_CODE_MAX_LENGTH: Final[int] = 256

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
    key_id = ec_key_id(clean)
    jwk = {**clean, "use": "sig", "alg": SIGNING_ALG, "kid": key_id}
    return {"keys": [jwk]}, key_id


def _is_enrolled_key(public_jwk: dict[str, Any], enrolled_key_id: str) -> bool:
    try:
        return _public_jwks(public_jwk)[1] == enrolled_key_id
    except EnrollmentKeyInvalidError:
        return False


def _runner_lock_key(runner_id: uuid.UUID) -> int:
    """Map a runner to a stable, non-negative 63-bit advisory-lock key."""
    digest = hashlib.blake2b(b"meho:runner-key\0" + runner_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


async def take_runner_lock(session: AsyncSession, runner_id: uuid.UUID) -> None:
    """Make enrollment and revocation of one runner take turns.

    Both read the runner's Keycloak client and write it back, so each holds
    this lock from its read of the row until its commit. PostgreSQL releases
    the lock when the transaction ends. An advisory lock is used, not a row
    lock, so the calls of other runners and the heartbeat of this one do not
    wait. On other databases (the single-writer SQLite test path) there is no
    lock.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _runner_lock_key(runner_id)}
        )


async def _row_of_code(session: AsyncSession, code: str) -> RunnerPrincipal:
    """The current row of the not revoked runner that *code* belongs to."""
    if len(code) > _CODE_MAX_LENGTH:
        raise EnrollmentCodeInvalidError
    runner_id = _parse_runner_id(code)
    presented = _hash_code(code)
    row = (
        await session.execute(
            select(RunnerPrincipal)
            .where(RunnerPrincipal.id == runner_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    stored = row.enrollment_code_hash if row is not None else None
    if row is None or stored is None or not hmac.compare_digest(stored, presented):
        raise EnrollmentCodeInvalidError
    if row.revoked:
        raise EnrollmentCodeInvalidError
    return row


async def _enroll_under_lock(
    session: AsyncSession, row: RunnerPrincipal, public_jwk: dict[str, Any]
) -> str:
    """Put the key on the runner's Keycloak client, use the code up, return the key id.

    The code is used up by the commit, after Keycloak has stored the key. When
    Keycloak fails, the transaction rolls back and the runner can try again.
    """
    expires = row.enrollment_code_expires_at
    if expires is None or _as_utc(expires) <= datetime.now(UTC):
        raise EnrollmentCodeInvalidError
    jwks, key_id = _public_jwks(public_jwk)
    async with KeycloakAdminClient.from_settings() as kc:
        await kc.set_client_signed_jwt_key(
            row.keycloak_internal_id, jwks=jwks, signing_alg=SIGNING_ALG
        )
    now = datetime.now(UTC)
    row.enrolled_key_id = key_id
    row.key_enrolled_at = now
    row.updated_at = now
    await session.commit()
    return key_id


async def enroll_runner_key(code: str, public_jwk: dict[str, Any]) -> RunnerEnrollment:
    """Check *code* and put *public_jwk* on the runner's Keycloak client.

    The code enrolls one key. A repeat of the same code with that same key
    changes nothing and gives the same answer again, also after the code's
    end, so a runner that did not get the answer can ask for it again.

    Raises :class:`EnrollmentCodeInvalidError` for a bad code or for a used
    code with another key, :class:`EnrollmentKeyInvalidError` for a bad key
    (the code is kept), and a
    :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError` when
    Keycloak cannot store the key (the code is kept, so the runner can retry).
    """
    log = structlog.get_logger()
    async with get_sessionmaker()() as session:
        # Check the code first, so only a caller with a good code takes the
        # lock. Then read the row again under the lock: a revoke or another
        # enrollment may have changed it.
        row = await _row_of_code(session, code)
        await take_runner_lock(session, row.id)
        row = await _row_of_code(session, code)
        repeated = row.enrolled_key_id is not None
        if row.enrolled_key_id is None:
            key_id = await _enroll_under_lock(session, row, public_jwk)
        elif _is_enrolled_key(public_jwk, row.enrolled_key_id):
            key_id = row.enrolled_key_id
        else:
            raise EnrollmentCodeInvalidError
    await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub=row.keycloak_client_id,
        actor_sub=row.keycloak_client_id,
        tenant_id=row.tenant_id,
        method=INTERNAL_METHOD,
        path=_ENROLL_AUDIT_PATH,
        status_code=200,
        duration_ms=0.0,
        payload={"runner": row.name, "key_id": key_id, "repeated": repeated},
    )
    log.info(
        "runner_key_enrolled",
        tenant_id=str(row.tenant_id),
        name=row.name,
        key_id=key_id,
        repeated=repeated,
    )
    issuer = str(get_settings().keycloak_issuer_url).rstrip("/")
    return RunnerEnrollment(
        client_id=row.keycloak_client_id,
        issuer=issuer,
        token_endpoint=f"{issuer}/protocol/openid-connect/token",
        signing_alg=SIGNING_ALG,
        key_id=key_id,
    )
