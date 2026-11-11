# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The passkey touch challenge of an approval (#6214).

A ``dangerous`` or ``destructive`` approval can need a passkey touch. When
the person clicks Approve, :func:`start_touch` makes one
:class:`~meho_backplane.db.models.ApprovalPresenceChallenge` row. The row
names exactly what the approval will run:

* the **card hash**: SHA-256 over the text of the approval card, built from
  the stored request (:func:`approval_card_text`);
* the **binding hash**: SHA-256 over a fixed, versioned encoding of the
  tenant, each request's ``params_hash``, ``preview_hash``, ``target_hash``
  and ``release_id``, the card hash and 32 random bytes of the server
  (:func:`compute_binding_hash`). MEHO sends it to Keycloak as the
  ``nonce``, and Keycloak signs it into the ID token of the touch;
* the **match code**: 6 characters from the binding hash, in an alphabet
  without look-alike characters (:func:`match_code`). The person sees it in
  MEHO before the touch.

When the person comes back, :func:`use_touch` checks the row and marks it
used. A row can be used once, only by the person who started it, only in
its own tenant, and only until it ends
(``APPROVAL_PRESENCE_TOUCH_VALID_SECONDS``, default 120 seconds). When any
request changed after the row was made, the binding hash built again
differs and the touch is refused.

Nothing calls these functions while the ``APPROVAL_PRESENCE_TOUCH`` switch
is off. A daily clean-up (:func:`start_presence_challenge_cleanup`) deletes
rows that were never used and are older than 14 days.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, NoReturn

import structlog
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.approval_sign_in import TouchBinding
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    APPROVAL_PRESENCE_CHANNELS,
    ApprovalPresenceChallenge,
    ApprovalRequest,
    ApprovalRequestStatus,
)
from meho_backplane.loop_schedule import FIRST_RUN_DELAY_SECONDS
from meho_backplane.metrics import note_loop_tick
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    take_approval_lock,
)
from meho_backplane.settings import get_settings

__all__ = [
    "BINDING_ALGORITHM",
    "CLEANUP_INTERVAL_SECONDS",
    "MATCH_CODE_ALPHABET",
    "MATCH_CODE_LENGTH",
    "UNUSED_CHALLENGE_RETENTION_DAYS",
    "TouchChallengeError",
    "TouchRefusal",
    "approval_card_text",
    "compute_binding_hash",
    "compute_card_hash",
    "delete_unused_challenges",
    "match_code",
    "start_presence_challenge_cleanup",
    "start_touch",
    "stop_presence_challenge_cleanup",
    "touch_binding",
    "use_touch",
]

#: The name and version of the binding hash encoding. A change of the
#: encoding gets a new version, so an old hash never matches a new one.
BINDING_ALGORITHM: Final[str] = "sha256/meho-touch-binding-v1"

#: Digits 2 to 9 and the letters A to Z without I and O: 32 characters, so
#: each one carries 5 bits. There is no 0, 1, I or O, so no two look alike.
MATCH_CODE_ALPHABET: Final[str] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MATCH_CODE_LENGTH: Final[int] = 6

#: Rows that were never used are deleted when they are older than this.
UNUSED_CHALLENGE_RETENTION_DAYS: Final[int] = 14

#: How often the clean-up runs: once a day.
CLEANUP_INTERVAL_SECONDS: Final[int] = 86400

#: How many random bytes of the server go into the binding hash.
_SERVER_RANDOM_BYTES: Final[int] = 32

#: The advisory-lock scope of one challenge row
#: (:func:`~meho_backplane.operations.approval_queue.take_approval_lock`).
_LOCK_SCOPE: Final[str] = "approval-presence-touch"

TouchRefusal = Literal["not_found", "used", "expired", "nonce_mismatch", "request_changed"]

_REFUSAL_MESSAGES: Final[dict[str, str]] = {
    "not_found": "This passkey touch is not known. Click Approve again to start a new one.",
    "used": "This passkey touch was already used. Click Approve again to start a new one.",
    "expired": "This passkey touch took too long and has ended. Click Approve again.",
    "nonce_mismatch": (
        "This passkey touch belongs to another approval. Click Approve again to start a new one."
    ),
    "request_changed": (
        "The request changed after you started the passkey touch. "
        "Click Approve again to see it as it is now."
    ),
}


class TouchChallengeError(Exception):
    """A passkey touch challenge cannot be used.

    ``reason`` is the machine-readable cause; the message is one plain
    sentence for the person. A row of another person or another tenant is
    ``not_found``, the same as a row that does not exist.
    """

    def __init__(self, reason: TouchRefusal) -> None:
        self.reason: TouchRefusal = reason
        super().__init__(_REFUSAL_MESSAGES[reason])


def _log() -> Any:
    # Resolved per call so test capture (structlog.testing.capture_logs) works.
    return structlog.get_logger(__name__)


def _canonical(value: object) -> str:
    """One fixed JSON text for *value*: sorted keys, no spaces, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """SQLite gives back naive datetimes; they are UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def approval_card_text(requests: Sequence[ApprovalRequest]) -> str:
    """The text of the approval card for *requests*, in the order given.

    Built from the stored request rows, at decision time: for each request
    the operation (``connector_id`` and ``op_id``), the target, the risk
    level, and the effect (``proposed_effect``, which carries the
    parameters as the card shows them). The card never uses what the
    caller sent at request time.
    """
    cards = []
    for request in requests:
        effect = dict(request.proposed_effect or {})
        cards.append(
            {
                "operation": {"connector_id": request.connector_id, "op_id": request.op_id},
                "target_id": str(request.target_id) if request.target_id else None,
                "risk_level": effect.get("safety_level"),
                "effect": effect,
            }
        )
    return _canonical(cards)


def compute_card_hash(requests: Sequence[ApprovalRequest]) -> str:
    """SHA-256 (hex) over :func:`approval_card_text`."""
    return _sha256(approval_card_text(requests))


def compute_binding_hash(
    tenant_id: uuid.UUID,
    requests: Sequence[ApprovalRequest],
    *,
    card_hash: str,
    server_random: str,
) -> str:
    """SHA-256 (hex) over the fixed encoding :data:`BINDING_ALGORITHM`.

    The encoding is one canonical JSON text of: the algorithm name, the
    tenant id, for each request (in order) its id, ``params_hash``,
    ``preview_hash``, ``target_hash`` and ``release_id``, the *card_hash*,
    and *server_random* (hex). A change of any one of them gives another
    hash.
    """
    encoding = {
        "algorithm": BINDING_ALGORITHM,
        "tenant_id": str(tenant_id),
        "requests": [
            {
                "id": str(request.id),
                "params_hash": request.params_hash,
                "preview_hash": request.preview_hash,
                "target_hash": request.target_hash,
                "release_id": request.release_id,
            }
            for request in requests
        ],
        "card_hash": card_hash,
        "server_random": server_random,
    }
    return _sha256(_canonical(encoding))


def match_code(binding_hash: str) -> str:
    """6 characters of :data:`MATCH_CODE_ALPHABET`, from the first 30 bits of the hash."""
    bits = int(binding_hash[:8], 16) >> 2
    chars = []
    for _ in range(MATCH_CODE_LENGTH):
        chars.append(MATCH_CODE_ALPHABET[bits & 0x1F])
        bits >>= 5
    return "".join(reversed(chars))


def touch_binding(challenge: ApprovalPresenceChallenge) -> TouchBinding:
    """The ``binding`` block that an approve with this challenge records."""
    return TouchBinding(
        algorithm=BINDING_ALGORITHM,
        hash=challenge.binding_hash,
        request_ids=tuple(challenge.request_ids),
        card_hash=challenge.card_hash,
        challenge_id=str(challenge.id),
    )


async def _load_requests(
    session: AsyncSession, tenant_id: uuid.UUID, request_ids: Sequence[uuid.UUID]
) -> list[ApprovalRequest | None]:
    """The tenant's request rows for *request_ids*, in order (``None`` when missing)."""
    rows = await session.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.id.in_(request_ids), ApprovalRequest.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    by_id = {row.id: row for row in rows.scalars()}
    return [by_id.get(request_id) for request_id in request_ids]


async def start_touch(
    session: AsyncSession,
    *,
    operator: Operator,
    request_ids: Sequence[uuid.UUID],
    channel: str,
    session_kind: str,
    now: datetime | None = None,
) -> ApprovalPresenceChallenge:
    """Make the challenge row for one passkey touch and return it (flushed, not committed).

    *request_ids* are the requests the touch approves, in the order the
    person sees them. Each must be a ``pending`` request of the operator's
    tenant (:class:`ApprovalNotFoundError` or
    :class:`ApprovalRequestAlreadyDecidedError` otherwise). *channel* is
    ``web``, ``cli_link`` or ``rest_link``. The row ends
    ``APPROVAL_PRESENCE_TOUCH_VALID_SECONDS`` after *now*.
    """
    if not request_ids or len(set(request_ids)) != len(request_ids):
        raise ValueError("request_ids must be a non-empty list without repeats")
    if channel not in APPROVAL_PRESENCE_CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    loaded = await _load_requests(session, operator.tenant_id, request_ids)
    requests: list[ApprovalRequest] = []
    for request_id, request in zip(request_ids, loaded, strict=True):
        if request is None:
            raise ApprovalNotFoundError(request_id)
        if request.status != ApprovalRequestStatus.PENDING.value:
            raise ApprovalRequestAlreadyDecidedError(request_id, request.status)
        requests.append(request)

    created_at = now or datetime.now(UTC)
    server_random = secrets.token_bytes(_SERVER_RANDOM_BYTES).hex()
    card_hash = compute_card_hash(requests)
    binding_hash = compute_binding_hash(
        operator.tenant_id, requests, card_hash=card_hash, server_random=server_random
    )
    valid_seconds = get_settings().approval_presence_touch_valid_seconds
    challenge = ApprovalPresenceChallenge(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        approver_sub=operator.sub,
        request_ids=[str(request_id) for request_id in request_ids],
        binding_hash=binding_hash,
        server_random=server_random,
        card_hash=card_hash,
        match_code=match_code(binding_hash),
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=valid_seconds),
        channel=channel,
        session_kind=session_kind,
    )
    session.add(challenge)
    await session.flush()
    _log().info(
        "approval_touch_started",
        challenge_id=str(challenge.id),
        operator_sub=operator.sub,
        request_ids=challenge.request_ids,
        channel=channel,
    )
    return challenge


async def _binding_still_holds(session: AsyncSession, challenge: ApprovalPresenceChallenge) -> bool:
    """Whether the requests still give the binding hash of *challenge*."""
    request_ids = [uuid.UUID(value) for value in challenge.request_ids]
    loaded = await _load_requests(session, challenge.tenant_id, request_ids)
    requests = [request for request in loaded if request is not None]
    if len(requests) != len(request_ids):
        return False
    card_hash = compute_card_hash(requests)
    binding_hash = compute_binding_hash(
        challenge.tenant_id,
        requests,
        card_hash=card_hash,
        server_random=challenge.server_random,
    )
    return hmac.compare_digest(card_hash, challenge.card_hash) and hmac.compare_digest(
        binding_hash, challenge.binding_hash
    )


def _refuse(challenge_id: uuid.UUID, operator: Operator, reason: TouchRefusal) -> NoReturn:
    _log().warning(
        "approval_touch_refused",
        challenge_id=str(challenge_id),
        operator_sub=operator.sub,
        reason=reason,
    )
    raise TouchChallengeError(reason)


async def use_touch(
    session: AsyncSession,
    challenge_id: uuid.UUID,
    *,
    operator: Operator,
    nonce: str,
    now: datetime | None = None,
) -> ApprovalPresenceChallenge:
    """Check a challenge row and mark it used; return it (not committed).

    *nonce* is the ``nonce`` of the touch's ID token. The row must be the
    operator's own, in their tenant, unused, not ended, with a binding hash
    equal to *nonce*, and every request must still give the same binding
    hash. Else :class:`TouchChallengeError`.

    Two approves can use one row at the same moment, on two replicas. The
    check runs under the same advisory lock helper that guards the reuse of
    a sign-in (:func:`~meho_backplane.operations.approval_queue.take_approval_lock`),
    keyed on the row. The mark is a conditional ``UPDATE ... WHERE used_at
    IS NULL`` as well, so exactly one approve wins also without the lock.
    The caller commits; a rollback leaves the row unused.
    """
    await take_approval_lock(session, _LOCK_SCOPE, str(challenge_id))
    found = await session.execute(
        select(ApprovalPresenceChallenge)
        .where(ApprovalPresenceChallenge.id == challenge_id)
        .execution_options(populate_existing=True)
    )
    challenge = found.scalar_one_or_none()
    if (
        challenge is None
        or challenge.tenant_id != operator.tenant_id
        or challenge.approver_sub != operator.sub
    ):
        _refuse(challenge_id, operator, "not_found")
    used_at = now or datetime.now(UTC)
    if challenge.used_at is not None:
        _refuse(challenge_id, operator, "used")
    if used_at >= _as_utc(challenge.expires_at):
        _refuse(challenge_id, operator, "expired")
    if not hmac.compare_digest(nonce.encode(), challenge.binding_hash.encode()):
        _refuse(challenge_id, operator, "nonce_mismatch")
    if not await _binding_still_holds(session, challenge):
        _refuse(challenge_id, operator, "request_changed")

    marked = await session.execute(
        update(ApprovalPresenceChallenge)
        .where(
            ApprovalPresenceChallenge.id == challenge_id,
            ApprovalPresenceChallenge.used_at.is_(None),
        )
        .values(used_at=used_at)
        .execution_options(synchronize_session=False)
    )
    if not isinstance(marked, CursorResult) or marked.rowcount != 1:
        _refuse(challenge_id, operator, "used")
    challenge.used_at = used_at
    _log().info(
        "approval_touch_used",
        challenge_id=str(challenge_id),
        operator_sub=operator.sub,
        request_ids=challenge.request_ids,
    )
    return challenge


async def delete_unused_challenges(*, now: datetime | None = None) -> int:
    """Delete rows never used and older than 14 days; return how many went."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=UNUSED_CHALLENGE_RETENTION_DAYS)
    async with get_sessionmaker()() as session:
        result = await session.execute(
            delete(ApprovalPresenceChallenge).where(
                ApprovalPresenceChallenge.used_at.is_(None),
                ApprovalPresenceChallenge.created_at < cutoff,
            )
        )
        await session.commit()
    dropped = result.rowcount if isinstance(result, CursorResult) else 0
    _log().info("approval_touch_cleanup_done", dropped_rows=dropped)
    return dropped


async def _cleanup_loop() -> None:
    """Clean up shortly after start, then once a day. Cancellation stops the loop.

    The first run comes after ``FIRST_RUN_DELAY_SECONDS`` (one minute), not
    after a full day. So a deployment that restarts more often than once a
    day still cleans the table. The delete is cheap and safe to run twice.
    """
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
    while True:
        try:
            await delete_unused_challenges()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log().warning("approval_touch_cleanup_failed", exc_info=True)
        note_loop_tick("approval_presence_cleanup", CLEANUP_INTERVAL_SECONDS)
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


def start_presence_challenge_cleanup() -> asyncio.Task[None]:
    """Start the daily clean-up loop and return its task (the caller keeps a reference)."""
    return asyncio.create_task(_cleanup_loop(), name="approval-presence-challenge-cleanup")


async def stop_presence_challenge_cleanup(task: asyncio.Task[None]) -> None:
    """Cancel the clean-up loop and wait for it to stop."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
