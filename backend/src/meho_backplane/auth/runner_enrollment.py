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
It enrolls one key: MEHO saves which key the code accepted before it calls
Keycloak. A key is accepted only until the code expires. A wrong, used, expired
or revoked code all get the same refusal, so a caller learns nothing from the
answer. The runner that holds the accepted key can send the same code and key
again: it gets the same answer, and when Keycloak has not stored the key yet,
MEHO stores it now.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import structlog
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.auth.runner_enrollment_limits import (
    give_back_refusal_slot,
    read_left_out_total,
    take_refusal_slot,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, RunnerPrincipal
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.settings import get_settings

__all__ = [
    "ENROLLMENT_CODE_TTL",
    "SIGNING_ALG",
    "EnrollmentCodeInvalidError",
    "EnrollmentKeyInvalidError",
    "EnrollmentRunnerMismatchError",
    "RunnerEnrollment",
    "audit_refused_body",
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
_STARTED_AUDIT_PATH: Final[str] = "runner.principal.key_enrollment_started"
_REFUSED_AUDIT_PATH: Final[str] = "runner.principal.key_enrollment_refused"
_PRIVATE_JWK_MEMBERS: Final[frozenset[str]] = frozenset({"d", "p", "q", "dp", "dq", "qi", "k"})


class EnrollmentCodeInvalidError(Exception):
    """The code is wrong, already used, expired, or its runner is revoked.

    ``reason`` is set when the code matched a runner and was still refused
    (``code_expired``, ``code_used_by_another_key`` or ``runner_revoked``). It
    is ``None`` when the code matches no runner.
    """

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(reason or "enrollment code invalid")


class EnrollmentRunnerMismatchError(Exception):
    """The code is good, but it belongs to a runner with another name."""

    reason: Final[str] = "runner_name_mismatch"


class EnrollmentKeyInvalidError(ValueError):
    """The public key is not a usable P-256 public key."""

    reason: Final[str] = "public_key_invalid"


@dataclass(frozen=True)
class RunnerEnrollment:
    """What the runner needs to log in after its key is enrolled."""

    client_id: str
    issuer: str
    token_endpoint: str
    signing_alg: str
    key_id: str


def _hash_code(code: str) -> str:
    # "surrogatepass": a lone surrogate (valid in JSON, not in UTF-8) is hashed
    # as its own bytes, so such a code matches no stored code and is refused.
    return hashlib.sha256(code.encode("utf-8", "surrogatepass")).hexdigest()


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


def _public_jwks(public_jwk: Any) -> tuple[dict[str, Any], str]:
    """Check *public_jwk* and return ``(jwks, key_id)`` for Keycloak.

    Accepts only a P-256 public key. A key with a private member is refused,
    so a private key can never be stored by mistake. Any other value (a text,
    a list, a number, ``None``) is refused as a bad key. No error text holds
    the value.
    """
    if not isinstance(public_jwk, dict):
        raise EnrollmentKeyInvalidError("the public key must be a JWK object")
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


async def _row_after_wait(
    session: AsyncSession, matched: RunnerPrincipal, code: str
) -> RunnerPrincipal:
    """Read the row of *matched* again, after a wait for the runner's lock.

    *code* matched *matched* at its first read. A revoke that ran during the
    wait is refused with the reason ``runner_revoked``.
    """
    try:
        return await _row_of_code(session, code)
    except EnrollmentCodeInvalidError:
        # The read put the current values into *matched*: the session holds
        # one object per row.
        raise EnrollmentCodeInvalidError("runner_revoked" if matched.revoked else None) from None


def _check_not_expired(row: RunnerPrincipal) -> None:
    expires = row.enrollment_code_expires_at
    if expires is None or _as_utc(expires) <= datetime.now(UTC):
        raise EnrollmentCodeInvalidError("code_expired")


def _claimed_key(row: RunnerPrincipal, public_jwk: Any) -> tuple[dict[str, Any], str]:
    """Return ``(jwks, key_id)`` when *public_jwk* is the key the code accepted."""
    try:
        jwks, key_id = _public_jwks(public_jwk)
    except EnrollmentKeyInvalidError:
        raise EnrollmentCodeInvalidError("code_used_by_another_key") from None
    if key_id != row.enrolled_key_id:
        raise EnrollmentCodeInvalidError("code_used_by_another_key")
    return jwks, key_id


def _check_runner_name(row: RunnerPrincipal, runner_name: str) -> None:
    if not hmac.compare_digest(row.name.encode("utf-8"), runner_name.encode("utf-8")):
        raise EnrollmentRunnerMismatchError


def _started_audit_row(row: RunnerPrincipal, key_id: str) -> AuditLog:
    """The audit row that says which key the code accepted. It holds no secret."""
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        operator_sub=row.keycloak_client_id,
        actor_sub=row.keycloak_client_id,
        tenant_id=row.tenant_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        method=INTERNAL_METHOD,
        path=_STARTED_AUDIT_PATH,
        status_code=202,
        duration_ms=Decimal("0.0"),
        payload={"runner": row.name, "key_id": key_id},
    )


async def _claim_key(
    session: AsyncSession, row: RunnerPrincipal, code: str, key_id: str
) -> RunnerPrincipal:
    """Save that the code accepted *key_id*, then take the runner's lock again.

    The claim is committed before Keycloak is called. So when the call to
    Keycloak ends without an answer, the code still belongs to this one key:
    another key is refused, and this key can finish, also after the code's end.
    The audit row "enrollment started" is part of the same commit. When the row
    cannot be written, the claim is not saved, Keycloak is not called, and the
    runner can send the same code and key again.
    """
    row.enrolled_key_id = key_id
    row.updated_at = datetime.now(UTC)
    return await _commit_started(session, row, code, key_id)


async def _commit_started(
    session: AsyncSession, row: RunnerPrincipal, code: str, key_id: str
) -> RunnerPrincipal:
    """Commit the row "enrollment started", then take the runner's lock again.

    Keycloak is called only after this commit. When the row cannot be written,
    nothing is saved and the runner can send the same code and key again.
    """
    session.add(_started_audit_row(row, key_id))
    await session.commit()
    await take_runner_lock(session, row.id)
    return await _row_after_wait(session, row, code)


async def _store_key(session: AsyncSession, row: RunnerPrincipal, jwks: dict[str, Any]) -> None:
    """Put the key on the runner's Keycloak client and mark it as stored.

    The lock is held, so a revoke waits until this is done. When Keycloak
    fails, the claim stays and the runner can try again with the same key.
    """
    async with KeycloakAdminClient.from_settings() as kc:
        await kc.set_client_signed_jwt_key(
            row.keycloak_internal_id, jwks=jwks, signing_alg=SIGNING_ALG
        )
    now = datetime.now(UTC)
    row.key_enrolled_at = now
    row.updated_at = now
    await session.commit()


def _offered_key_id(public_jwk: Any) -> str | None:
    """The id of the offered key, or ``None`` when the key cannot be read."""
    try:
        return _public_jwks(public_jwk)[1]
    except EnrollmentKeyInvalidError:
        return None


async def _audit_refusal(
    row: RunnerPrincipal, status_code: int, reason: str, public_jwk: Any
) -> None:
    """One audit row for a refused enrollment whose code matched *row*.

    The row holds the runner's name, the answer's status, the reason and the
    id of the offered key. It never holds the code or the key. A runner gets
    one row per reason and minute; the next row that is written says how many
    refusals were left out since the row before. When the row cannot be written,
    the slot is given back.
    """
    slot = await take_refusal_slot(row.id, reason)
    if slot is None:
        return
    try:
        await _write_refusal(row, status_code, reason, public_jwk)
    except BaseException:
        await give_back_refusal_slot(slot)
        raise


def _refusal_lock_key(runner_id: uuid.UUID, reason: str) -> int:
    """Map a runner and a reason to a stable, non-negative 63-bit lock key."""
    digest = hashlib.blake2b(
        b"meho:runner-refusal\0" + runner_id.bytes + reason.encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


async def _recorded_left_out_total(session: AsyncSession, row: RunnerPrincipal, reason: str) -> int:
    """The left-out count stored on the newest refusal row of this runner and reason."""
    payload = (
        await session.execute(
            select(AuditLog.payload)
            .where(
                AuditLog.path == _REFUSED_AUDIT_PATH,
                AuditLog.operator_sub == row.keycloak_client_id,
                AuditLog.tenant_id == row.tenant_id,
                AuditLog.payload["reason"].as_string() == reason,
            )
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    value = (payload or {}).get("left_out_total", 0)
    return value if isinstance(value, int) else 0


async def _write_refusal(
    row: RunnerPrincipal, status_code: int, reason: str, public_jwk: Any
) -> None:
    """Commit the refusal row with the refusals left out since the row before.

    The left-out counter only goes up. The newest row stores the value it
    read, and this row counts the difference. The read of both values and the
    insert are one transaction, under a lock per runner and reason on
    PostgreSQL: a second writer waits for this commit and then reads this
    row. A row that is not committed changes nothing, so its count goes into
    the next row, once.
    """
    async with get_sessionmaker()() as session:
        conn = await session.connection()
        if conn.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _refusal_lock_key(row.id, reason)},
            )
        recorded = await _recorded_left_out_total(session, row, reason)
        total = await read_left_out_total(row.id, reason)
        if total is None:
            total = recorded
        # A total under the stored one means Valkey started again from zero.
        left_out = total - recorded if total >= recorded else total
        payload: dict[str, Any] = {
            "runner": row.name,
            "reason": reason,
            "key_id": _offered_key_id(public_jwk),
            "left_out_total": total,
        }
        if left_out:
            payload["left_out_before"] = left_out
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                operator_sub=row.keycloak_client_id,
                actor_sub=row.keycloak_client_id,
                tenant_id=row.tenant_id,
                event_class=AuditEventClass.SECURITY_EVENT,
                method=INTERNAL_METHOD,
                path=_REFUSED_AUDIT_PATH,
                status_code=status_code,
                duration_ms=Decimal("0.0"),
                payload=payload,
            )
        )
        await session.commit()


async def audit_refused_body(code: Any) -> None:
    """One audit row for a request whose body fails the body check.

    The row is written only when *code* is a text that matches a runner that
    is not revoked. It says status 422 and the reason ``request_body_invalid``
    (``code_expired`` for an ended code that enrolled no key). It holds no code
    and no key. Any other *code* (not a text, too long, matching no runner)
    writes nothing.
    """
    if not isinstance(code, str) or len(code) > _CODE_MAX_LENGTH:
        return
    async with get_sessionmaker()() as session:
        try:
            row = await _row_of_code(session, code)
        except EnrollmentCodeInvalidError:
            return
    reason = "request_body_invalid"
    if row.enrolled_key_id is None:
        try:
            _check_not_expired(row)
        except EnrollmentCodeInvalidError as exc:
            reason = exc.reason or reason
    await _audit_refusal(row, 422, reason, None)


_Refusal = EnrollmentCodeInvalidError | EnrollmentRunnerMismatchError | EnrollmentKeyInvalidError

_REFUSAL_STATUS: Final[dict[type[Exception], int]] = {
    EnrollmentCodeInvalidError: 403,
    EnrollmentRunnerMismatchError: 409,
    EnrollmentKeyInvalidError: 422,
}


async def _enroll_matched(
    session: AsyncSession,
    row: RunnerPrincipal,
    code: str,
    public_jwk: Any,
    runner_name: str,
) -> tuple[RunnerPrincipal, str, bool]:
    """Enroll *public_jwk* for *row*, whose code matched. The lock is held.

    Returns ``(row, key_id, repeated)``.
    """
    if row.enrolled_key_id is None:
        # A new key: the code must not have ended. Only then do the name
        # and the key count, so an ended code always gets the same refusal.
        _check_not_expired(row)
        _check_runner_name(row, runner_name)
        jwks, key_id = _public_jwks(public_jwk)
        row = await _claim_key(session, row, code, key_id)
    else:
        # A used code answers only its own key; any other key gets the
        # same refusal as a wrong code, before the name is looked at.
        jwks, key_id = _claimed_key(row, public_jwk)
        _check_runner_name(row, runner_name)
        if row.key_enrolled_at is None:
            # A new attempt to store the key (the claim is older, or Keycloak
            # failed before): it gets its own "started" row first.
            row = await _commit_started(session, row, code, key_id)
    if row.enrolled_key_id != key_id:
        raise EnrollmentCodeInvalidError("code_used_by_another_key")
    repeated = row.key_enrolled_at is not None
    if not repeated:
        await _store_key(session, row, jwks)
    return row, key_id, repeated


async def enroll_runner_key(code: str, public_jwk: Any, runner_name: str) -> RunnerEnrollment:
    """Check *code* and put *public_jwk* on the runner's Keycloak client.

    The code enrolls one key: the first good key sent with it. A repeat of the
    same code with that same key gives the same answer again, also after the
    code's end, so a runner that did not get the answer can ask for it again.
    *runner_name* is the name the runner expects. A code of another runner is
    refused before anything changes, so the code stays usable for its runner.
    An ended code that has no key yet, and a used code with another key, get
    the code refusal before the name or the key is checked. *public_jwk* is the
    value as it was sent: anything that is not a usable key is a bad key.

    When the code matches a runner and the enrollment is still refused, one
    audit row is written in that runner's tenant. A code that matches no
    runner writes no audit row.

    Raises :class:`EnrollmentCodeInvalidError` for a bad code or for a used
    code with another key, :class:`EnrollmentRunnerMismatchError` when the code
    is for another runner, :class:`EnrollmentKeyInvalidError` for a bad key
    (the code is kept), and a
    :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError` when
    Keycloak cannot store the key (the runner can retry with the same key).
    """
    log = structlog.get_logger()
    if len(code) > _CODE_MAX_LENGTH:
        raise EnrollmentCodeInvalidError
    refusal: _Refusal | None = None
    async with get_sessionmaker()() as session:
        # Check the code first, so only a caller with a good code takes the
        # lock. Then read the row again under the lock: a revoke or another
        # enrollment may have changed it.
        matched = await _row_of_code(session, code)
        await take_runner_lock(session, matched.id)
        try:
            row = await _row_after_wait(session, matched, code)
            row, key_id, repeated = await _enroll_matched(
                session, row, code, public_jwk, runner_name
            )
        except (
            EnrollmentCodeInvalidError,
            EnrollmentRunnerMismatchError,
            EnrollmentKeyInvalidError,
        ) as exc:
            refusal = exc
    if refusal is not None:
        if refusal.reason is not None:
            await _audit_refusal(
                matched, _REFUSAL_STATUS[type(refusal)], refusal.reason, public_jwk
            )
        raise refusal
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
