# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Inbound ingest orchestration: resolve-secret -> auth -> guard -> publish (#2881).

The endpoint (:mod:`meho_backplane.api.v1.events_ingest`) resolves the source
and reads the capped body; this module runs everything that follows, in the
order the issue fixes:

1. Read the source's shared secret from Vault under the synthetic
   ``__ingest__`` identity (a background-dispatch service principal, since
   the webhook is JWT-less).
2. Verify the sender against the source's auth strategy (constant-time).
3. Enforce the per-source rate limit.
4. Compute the dedupe key (sender delivery id, else body hash).
5. Normalise-lite (parse the JSON body into an envelope) and
   :func:`~meho_backplane.events.outbox.publish` it to ``event_outbox`` **in
   the same transaction** as a synchronous ``__ingest__`` audit row -- the
   spec's "no success without a committed audit row" invariant (CLAUDE.md
   postulate 7). A ``dedupe_key`` collision is mapped to an idempotent
   acknowledgement, never a second outbox row.
6. Publish a fail-open broadcast event after the transaction commits.

The ``__ingest__`` audit identity
=================================

Ingest is authenticated per-source, not per-operator, so there is no JWT to
derive an ``operator_sub`` from. The audit row is written under the synthetic
sentinel ``__ingest__`` (the ``__sensor__`` / ``__scheduler__`` precedent;
``audit_log.operator_sub`` is plain ``Text`` and accepts it), with the source
id recorded as ``origin`` on both the outbox row and the audit payload.

The Vault-read identity
=======================

Reading the secret needs a Vault client, which needs an operator. The webhook
carries none, so the ``__ingest__`` operator reuses the backplane's existing
background-dispatch service-principal token
(:func:`~meho_backplane.auth.runner_identity.check_runner_jwt`) -- the same
"MEHO's own in-process dispatch, outward" identity the sensor check-runner
uses. It carries ``check_runner_dispatch=False`` so the Vault login uses the
standard ``vault_oidc_role`` (the check-runner's bounded role is scoped to
target secrets, not event-source secrets). When that principal is
unconfigured the token is empty, the Vault read fails, and ingest fails
closed -- the correct posture.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.runner_identity import check_runner_jwt
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.broadcast.publisher import publish_event
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EventOutbox
from meho_backplane.event_source.schemas import EventSourceAuthStrategy
from meho_backplane.event_source.secrets import read_event_source_secret
from meho_backplane.events.ingest.auth import IngestAuthError, verify_ingest_auth
from meho_backplane.events.ingest.config import IngestConfig
from meho_backplane.events.ingest.rate_limit import enforce_ingest_rate_limit
from meho_backplane.events.outbox import publish
from meho_backplane.settings import get_settings

__all__ = [
    "INGEST_AUDIT_METHOD",
    "INGEST_AUDIT_PATH",
    "INGEST_OPERATOR_SUB",
    "IngestOutcome",
    "IngestPayloadError",
    "IngestVaultError",
    "ResolvedSource",
    "accept_ingest_event",
]

_log = structlog.get_logger(__name__)

#: Synthetic ``operator_sub`` every ingest audit row is written under. The
#: ``__sensor__`` / ``__scheduler__`` sentinel precedent -- a stable literal
#: audit filters can partition JWT-less ingest traffic by.
INGEST_OPERATOR_SUB: str = "__ingest__"

#: Audit channel + op-id for ingest rows (the ``method``-is-channel,
#: ``path``-is-op-id convention the internal-audit writer documents).
INGEST_AUDIT_METHOD: str = "INGEST"
INGEST_AUDIT_PATH: str = "events.ingest"

#: Broadcast op metadata. ``other`` is a valid ``op_class`` (avoids touching
#: the classifier); ``events.ingest`` mirrors the audit op-id.
_INGEST_OP_ID: str = "events.ingest"
_INGEST_OP_CLASS: str = "other"

#: Defensive cap on a sender-supplied delivery id used as the dedupe basis,
#: so an attacker cannot bloat the ``dedupe_key`` column with a huge header.
_MAX_DELIVERY_ID_LEN: int = 200

#: Max length of the derived ``event_type`` token in ``external.{kind}.{type}``.
_MAX_EVENT_TYPE_LEN: int = 64


class IngestPayloadError(Exception):
    """The request body is not a JSON object/array (400 at the boundary)."""


class IngestVaultError(Exception):
    """The source secret could not be read from Vault (503 -- transient)."""


@dataclass(frozen=True)
class ResolvedSource:
    """Immutable snapshot of the resolved source's routing/auth fields.

    Snapshotted off the ORM row by the endpoint so this service never
    touches a possibly-detached ORM instance across its own transaction.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    slug: str
    kind: str
    auth_strategy: EventSourceAuthStrategy
    secret_ref: str | None
    extras: dict[str, Any]


@dataclass(frozen=True)
class IngestOutcome:
    """Result of an accepted (or deduplicated) delivery."""

    event_id: int | None
    deduplicated: bool


async def _build_ingest_operator(tenant_id: uuid.UUID) -> Operator:
    """Build the synthetic ``__ingest__`` operator for the outward Vault read."""
    return Operator(
        sub=INGEST_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt=await check_runner_jwt(),
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _read_secret_or_fail(source: ResolvedSource) -> SecretStr:
    """Read the source's Vault secret, mapping failures to fail-closed errors.

    A source with no ``secret_ref`` cannot authenticate any sender, so it
    fails closed as an auth error (uniform 401). A Vault I/O / auth failure
    is transient and maps to :class:`IngestVaultError` (503); the sender
    retries.
    """
    if source.secret_ref is None:
        raise IngestAuthError("no_secret_configured")
    operator = await _build_ingest_operator(source.tenant_id)
    try:
        return await read_event_source_secret(operator, source.secret_ref)
    except VaultClientError as exc:
        _log.warning(
            "ingest_secret_read_failed",
            slug=source.slug,
            error_class=type(exc).__name__,
        )
        raise IngestVaultError(str(type(exc).__name__)) from exc


def _dedupe_key(source: ResolvedSource, headers: Any, raw_body: bytes, config: IngestConfig) -> str:
    """Derive the ``(tenant, dedupe_key)`` idempotency token for this delivery.

    Prefers a sender-supplied delivery id (capped) and falls back to a
    SHA-256 of the raw body. Namespaced by source id so two sources in one
    tenant that happen to relay an identical body never false-collide.
    """
    delivery_id = headers.get(config.delivery_id_header)
    if delivery_id and delivery_id.strip():
        basis = delivery_id.strip()[:_MAX_DELIVERY_ID_LEN]
    else:
        basis = hashlib.sha256(raw_body).hexdigest()
    return f"{source.id}:{basis}"


def _parse_json_or_fail(raw_body: bytes) -> object:
    """Parse the raw body as JSON (normalise-lite). Raise on invalid JSON.

    Runs only after auth + rate-limit have passed, so an unauthenticated
    sender never learns whether its body parsed -- the pre-parse failure it
    could otherwise distinguish is behind the uniform auth wall.
    """
    try:
        return json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IngestPayloadError(f"body is not valid JSON: {type(exc).__name__}") from exc


def _sanitise_event_type(raw: object) -> str:
    """Reduce a payload ``type`` hint to a safe ``[a-z0-9._-]`` token.

    Untrusted external input, so a non-string / empty / all-stripped value
    degrades to ``"event"`` and anything outside the allowed set is dropped.
    """
    if not isinstance(raw, str):
        return "event"
    kept = "".join(c for c in raw.lower() if c.isalnum() or c in "._-")
    return kept[:_MAX_EVENT_TYPE_LEN] or "event"


def _normalise_lite(
    source: ResolvedSource, parsed_body: object, now: datetime
) -> tuple[str, dict[str, object]]:
    """Build ``(event_kind, envelope)`` from the parsed body (normalise-lite).

    The external (untrusted) body is nested under ``data`` so it can never
    collide with the envelope's own ``source`` / ``event_type`` keys, and the
    outbox ``payload`` is always a JSON object regardless of the body's shape
    (a top-level array/scalar body still yields a dict envelope). Richer
    per-vendor normalisation is #2882.
    """
    event_type = _sanitise_event_type(
        parsed_body.get("type") if isinstance(parsed_body, dict) else None
    )
    event_kind = f"external.{source.kind}.{event_type}"
    envelope = {
        "source": {"slug": source.slug, "kind": source.kind, "id": str(source.id)},
        "event_type": event_type,
        "received_at": now.isoformat(),
        "data": parsed_body,
    }
    return event_kind, envelope


def _build_audit_row(
    *,
    audit_id: uuid.UUID,
    source: ResolvedSource,
    event_id: int,
    event_kind: str,
    dedupe_key: str,
    duration_ms: float,
    now: datetime,
) -> AuditLog:
    """Construct the synchronous ``__ingest__`` audit row (same-txn insert).

    The payload is metadata only -- source attribution, the event kind, the
    outbox id, the dedupe key -- never the raw external body (that lives on
    the outbox row; the audit row must not duplicate untrusted, possibly-PII
    content).
    """
    return AuditLog(
        id=audit_id,
        occurred_at=now,
        operator_sub=INGEST_OPERATOR_SUB,
        tenant_id=source.tenant_id,
        method=INGEST_AUDIT_METHOD,
        path=INGEST_AUDIT_PATH,
        status_code=202,
        request_id=None,
        duration_ms=Decimal(str(round(duration_ms, 2))),
        payload={
            "op_id": INGEST_AUDIT_PATH,
            "origin": str(source.id),
            "source_slug": source.slug,
            "source_kind": source.kind,
            "event_kind": event_kind,
            "event_id": event_id,
            "dedupe_key": dedupe_key,
        },
    )


async def _lookup_existing_event_id(tenant_id: uuid.UUID, dedupe_key: str) -> int | None:
    """Fetch the event_id of the row a duplicate delivery collided with."""
    async with get_sessionmaker()() as session:
        stmt = select(EventOutbox.event_id).where(
            EventOutbox.tenant_id == tenant_id,
            EventOutbox.dedupe_key == dedupe_key,
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def _publish_ingest_broadcast(
    *, audit_id: uuid.UUID, source: ResolvedSource, event_id: int, event_kind: str, now: datetime
) -> None:
    """Emit the per-ingest broadcast event. Fail-open by contract.

    Aggregate/attribution metadata only -- no external body content -- so the
    read-class default holds without a redactor pass.
    """
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=now,
        tenant_id=source.tenant_id,
        principal_sub=INGEST_OPERATOR_SUB,
        principal_name=None,
        target_name=source.slug,
        op_id=_INGEST_OP_ID,
        op_class=_INGEST_OP_CLASS,
        result_status="ok",
        audit_id=audit_id,
        payload={
            "op_class": _INGEST_OP_CLASS,
            "result_status": "ok",
            "origin": str(source.id),
            "source_slug": source.slug,
            "source_kind": source.kind,
            "event_kind": event_kind,
            "event_id": event_id,
        },
    )
    await publish_event(event)


def _resolve_rate_limit(source: ResolvedSource, config: IngestConfig) -> int:
    """Per-source override wins over the global default cap."""
    if config.rate_per_minute_override is not None:
        return config.rate_per_minute_override
    return get_settings().events_ingest_rate_per_minute


async def accept_ingest_event(
    *,
    source: ResolvedSource,
    config: IngestConfig,
    headers: Any,
    raw_body: bytes,
) -> IngestOutcome:
    """Run auth -> rate-limit -> dedupe -> normalise -> publish+audit -> broadcast.

    Raises the typed ingest errors on a fail-closed guard; returns an
    :class:`IngestOutcome` on accept or on a duplicate delivery (no second
    outbox/audit row). The JSON body is parsed only after auth + rate-limit
    pass, so a parse failure (400) is never reachable unauthenticated.
    """
    started = time.perf_counter()
    now = datetime.now(UTC)

    secret = await _read_secret_or_fail(source)
    verify_ingest_auth(
        strategy=source.auth_strategy,
        secret=secret,
        headers=headers,
        raw_body=raw_body,
        config=config,
        now=now.timestamp(),
    )
    await enforce_ingest_rate_limit(
        source.tenant_id, source.slug, _resolve_rate_limit(source, config)
    )

    dedupe_key = _dedupe_key(source, headers, raw_body, config)
    parsed_body = _parse_json_or_fail(raw_body)
    event_kind, envelope = _normalise_lite(source, parsed_body, now)

    audit_id = uuid.uuid4()
    outcome = await _publish_with_audit(
        source=source,
        event_kind=event_kind,
        envelope=envelope,
        dedupe_key=dedupe_key,
        audit_id=audit_id,
        started=started,
        now=now,
    )
    if outcome.deduplicated:
        return outcome

    assert outcome.event_id is not None  # non-dedup path always has an id
    # Fail-open broadcast: the audit row has already committed (the canonical
    # record of the accepted delivery), so a broadcast failure must never turn
    # a successful ingest into an error. publish_event is itself fail-open;
    # this guard makes that contract local and explicit at the ingest boundary.
    try:
        await _publish_ingest_broadcast(
            audit_id=audit_id,
            source=source,
            event_id=outcome.event_id,
            event_kind=event_kind,
            now=now,
        )
    except Exception as exc:
        _log.warning("ingest_broadcast_failed", slug=source.slug, error_class=type(exc).__name__)
    return outcome


async def _publish_with_audit(
    *,
    source: ResolvedSource,
    event_kind: str,
    envelope: dict[str, object],
    dedupe_key: str,
    audit_id: uuid.UUID,
    started: float,
    now: datetime,
) -> IngestOutcome:
    """Publish the outbox row + audit row in one transaction; dedupe -> 200.

    The load-bearing invariant (postulate 7): the audit row is inserted in the
    **same** ``session.begin()`` block as the outbox row, so a delivery that
    cannot commit its audit row publishes nothing. A ``(tenant_id, dedupe_key)``
    collision is caught and mapped to an idempotent ack carrying the original
    event id -- never a second outbox/audit row.
    """
    event_id: int | None = None
    try:
        async with get_sessionmaker()() as session, session.begin():
            outbox = await publish(
                session,
                tenant_id=source.tenant_id,
                event_kind=event_kind,
                payload=envelope,
                origin=str(source.id),
                dedupe_key=dedupe_key,
            )
            # Capture the flushed id before the commit expires the instance.
            event_id = outbox.event_id
            session.add(
                _build_audit_row(
                    audit_id=audit_id,
                    source=source,
                    event_id=event_id,
                    event_kind=event_kind,
                    dedupe_key=dedupe_key,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    now=now,
                )
            )
    except IntegrityError:
        _log.info("ingest_duplicate", slug=source.slug, dedupe_key=dedupe_key)
        existing = await _lookup_existing_event_id(source.tenant_id, dedupe_key)
        return IngestOutcome(event_id=existing, deduplicated=True)

    assert event_id is not None  # populated inside the committed block
    _log.info(
        "ingest_accepted",
        slug=source.slug,
        event_id=event_id,
        event_kind=event_kind,
        origin=str(source.id),
    )
    return IngestOutcome(event_id=event_id, deduplicated=False)
