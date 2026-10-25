# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central ingest + chain verification for satellite effect audit records (#3193).

The centre half of mechanism 4 (design ``docs/research/2901-satellite-write-path.md``
§3, decision ``docs/decisions/satellite-write-path.md``). A satellite runner
forwards its local hash-chained effect audit records
(:mod:`meho_backplane.runner.effect_audit`) on next contact; this module ingests
them into ``audit_log`` with a ``store-and-forward`` provenance marker **only
after verifying the chain** against the persisted per-runner head
(:class:`~meho_backplane.db.models.RunnerEffectChain`).

Three verified properties, each fail-closed:

* **Continuity / gap detection.** A record's ``seq`` must be exactly
  ``head.last_seq + 1``; a hole (a dropped or suppressed record) is refused.
* **Link integrity.** A record's ``prev_hash`` must equal the head's
  ``last_hash`` and its ``record_hash`` must re-derive from the canonical body —
  any transit tampering breaks one of these.
* **Runner binding.** A record's ``runner_id`` must equal the authenticated
  runner; one runner cannot extend another's chain (the sibling-forge defence —
  the runner name comes from the unforgeable token, not the request body).

A break raises :class:`EffectChainTamperError`; the runner-facing endpoint rolls
back the whole result submission (so a tampered report is *not* accepted — the
un-reported-mint alarm then fires on the un-consumed capability) and
:func:`quarantine_effect_chain` records the security event. A clean batch writes
one ``audit_log`` row per record, each linked to its mint audit row
(``gateway_command.mint_audit_id``) so the store-and-forward effect joins the
mint/result audit subtree the split lineage (#2500) already forms.

The checks above use the record's plain values. The ``audit_log`` row stores
keyed hashes instead, like every other audit row: ``params_hash`` is
:func:`~meho_backplane.operations._validate.compute_audit_params_hash` of the
command's stored params (the value the mint row carries), and ``record_hash``
and ``signature`` go through
:func:`~meho_backplane.operations._validate.compute_audit_digest_hash`. The
plain values stay where they are checked: with the runner, in the chain head
(``runner_effect_chain``) and on the ``gateway_command`` row.

Residual (design §3, mechanism 4): a **fully compromised** runner holds its own
genesis and can forge a self-consistent alternate chain. Tamper evidence catches
transit tampering + dropped records, **not** a lying edge; that residual is
bounded by the composition (allowlist x credential TTL) and the un-reported-mint
alarm, never by this verification alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import AuditEventClass, AuditLog, GatewayCommand, RunnerEffectChain
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
)
from meho_backplane.operations._validate import (
    compute_audit_digest_hash,
    compute_audit_params_hash,
)
from meho_backplane.runner.effect_audit import (
    GENESIS_PREV_HASH,
    EffectAuditRecord,
    canonical_record_body,
    compute_record_hash,
)

__all__ = [
    "EFFECT_AUDIT_METHOD",
    "EFFECT_AUDIT_PATH",
    "EFFECT_QUARANTINE_PATH",
    "STORE_AND_FORWARD_PROVENANCE",
    "EffectChainTamperError",
    "ingest_effect_records",
    "quarantine_effect_chain",
]

_log = structlog.get_logger(__name__)

#: ``audit_log.method`` channel for an ingested effect record — the same
#: ``GATEWAY`` channel the mint / result rows use, so an effect row sits in the
#: mint's audit subtree via ``parent_audit_id``.
EFFECT_AUDIT_METHOD: str = "GATEWAY"
#: ``audit_log.path`` for an ingested, chain-verified effect record.
EFFECT_AUDIT_PATH: str = "gateway.command.effect"
#: ``audit_log.path`` for a quarantined (tamper-detected) forward — an
#: ``INTERNAL``-channel **security** event, distinct from a normal effect row.
EFFECT_QUARANTINE_PATH: str = "gateway.command.effect.quarantine"
#: The provenance marker stamped on every store-and-forward effect row's payload
#: (the effect was audited at the edge and forwarded, not synchronously at the
#: centre — the recorded v0.1-spec §6 exception).
STORE_AND_FORWARD_PROVENANCE: str = "store-and-forward"

#: Synthetic identity for centre-detected effect-chain security events.
_SECURITY_OPERATOR_SUB: str = "system:satellite-effect-audit"


class _MintLink(NamedTuple):
    """What an effect record's command contributes to its audit row."""

    #: The command's mint audit row id, or ``None`` when no command matches.
    mint_audit_id: uuid.UUID | None
    #: ``True`` when a minted command matches the record.
    linked: bool
    #: The command's stored params when its digest equals the record's
    #: ``params_hash``; ``None`` otherwise.
    params: dict[str, Any] | None


class EffectChainTamperError(Exception):
    """A forwarded effect record broke the chain — refuse / quarantine.

    Carries the offending ``seq`` and a human reason; the endpoint surfaces the
    reason and :func:`quarantine_effect_chain` records it.
    """

    def __init__(self, reason: str, *, seq: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.seq = seq


async def ingest_effect_records(
    session: AsyncSession,
    *,
    operator: Operator,
    runner_id: str,
    records: list[EffectAuditRecord],
) -> list[uuid.UUID]:
    """Verify a forwarded effect chain and ingest it into ``audit_log``.

    Verifies each record against the persisted per-runner head (continuity,
    link integrity, runner binding), writes one ``audit_log`` row per record
    (store-and-forward provenance, linked to the mint audit row), and advances
    the head. Flushed, **not** committed — the caller owns the commit so the
    effect rows land atomically with the accepted result.

    Raises:
        EffectChainTamperError: a gap, a broken link, a body mismatch, or a
            record claiming a different runner. No partial ingest — the caller
            rolls the whole submission back.
    """
    if not records:
        return []

    head = await _load_head(session, tenant_id=operator.tenant_id, runner_id=runner_id)
    last_seq = head.last_seq if head is not None else -1
    last_hash = head.last_hash if head is not None else GENESIS_PREV_HASH

    written: list[uuid.UUID] = []
    for record in records:
        _verify_link(record, runner_id=runner_id, last_seq=last_seq, last_hash=last_hash)
        audit_id = await _write_effect_audit_row(session, operator=operator, record=record)
        written.append(audit_id)
        last_seq = record.seq
        last_hash = record.record_hash

    await _persist_head(
        session,
        head=head,
        tenant_id=operator.tenant_id,
        runner_id=runner_id,
        last_seq=last_seq,
        last_hash=last_hash,
    )
    await session.flush()
    _log.info(
        "satellite_effect_chain_ingested",
        runner_id=runner_id,
        tenant_id=str(operator.tenant_id),
        records=len(records),
        head_seq=last_seq,
    )
    return written


def _verify_link(
    record: EffectAuditRecord,
    *,
    runner_id: str,
    last_seq: int,
    last_hash: str,
) -> None:
    """Fail-closed verification of one record against the running head."""
    if record.runner_id != runner_id:
        raise EffectChainTamperError(
            f"effect record claims runner {record.runner_id!r} but was forwarded by "
            f"{runner_id!r}; a runner cannot extend another runner's chain",
            seq=record.seq,
        )
    expected_seq = last_seq + 1
    if record.seq != expected_seq:
        raise EffectChainTamperError(
            f"effect chain gap for runner {runner_id!r}: expected seq {expected_seq}, "
            f"got {record.seq} (a dropped or suppressed record breaks the chain)",
            seq=record.seq,
        )
    if record.prev_hash != last_hash:
        raise EffectChainTamperError(
            f"effect chain broken link for runner {runner_id!r} at seq {record.seq}: "
            "prev_hash does not match the accepted head (transit tampering)",
            seq=record.seq,
        )
    canonical = canonical_record_body(
        runner_id=record.runner_id,
        seq=record.seq,
        phase=record.phase,
        command_id=record.command_id,
        op_id=record.op_id,
        params_hash=record.params_hash,
        signature=record.signature,
        target_scope=record.target_scope,
        outcome=record.outcome,
        recorded_at=record.recorded_at,
    )
    if compute_record_hash(record.prev_hash, canonical) != record.record_hash:
        raise EffectChainTamperError(
            f"effect record body tampered for runner {runner_id!r} at seq {record.seq}: "
            "record_hash does not re-derive from the record body",
            seq=record.seq,
        )


async def _write_effect_audit_row(
    session: AsyncSession,
    *,
    operator: Operator,
    record: EffectAuditRecord,
) -> uuid.UUID:
    """Insert one store-and-forward ``audit_log`` row for a verified record.

    ``parent_audit_id`` links to the mint audit row via the command the record
    references, so the remote effect joins the mint/result subtree; ``linked``
    is ``False`` when no minted command matches (a residual: a lying edge may
    reference a capability that was never minted — recorded, not fatal here).

    ``params_hash``, ``signature`` and ``record_hash`` hold keyed hashes, not
    the record's plain values (see the module docstring and
    :func:`_effect_params_hash`).
    """
    link = await _resolve_mint_link(
        session, tenant_id=operator.tenant_id, runner_id=record.runner_id, record=record
    )
    audit_id = uuid.uuid4()
    payload: dict[str, Any] = {
        "provenance": STORE_AND_FORWARD_PROVENANCE,
        "runner_id": record.runner_id,
        "seq": record.seq,
        "phase": str(record.phase),
        "command_id": record.command_id,
        "op_id": record.op_id,
        "params_hash": _effect_params_hash(record, link.params),
        "signature": (
            compute_audit_digest_hash(record.signature) if record.signature is not None else None
        ),
        "target_scope": record.target_scope,
        "outcome": record.outcome,
        "recorded_at": record.recorded_at,
        "record_hash": compute_audit_digest_hash(record.record_hash),
        "chain_verified": True,
        "linked": link.linked,
    }
    session.add(
        AuditLog(
            id=audit_id,
            event_class=AuditEventClass.OPERATION_EXECUTION,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            parent_audit_id=link.mint_audit_id,
            method=EFFECT_AUDIT_METHOD,
            path=EFFECT_AUDIT_PATH,
            status_code=200,
            request_id=None,
            duration_ms=None,
            payload=payload,
        )
    )
    await session.flush()
    return audit_id


def _effect_params_hash(record: EffectAuditRecord, params: dict[str, Any] | None) -> str:
    """The ``params_hash`` an effect audit row stores.

    With the command's stored params (*params*, set when their digest equals
    the record's), the same masked, keyed hash every audit row carries, so the
    effect rows match their mint row. Without them, the keyed hash of the
    runner's digest.
    """
    if params is not None:
        return compute_audit_params_hash(params)
    return compute_audit_digest_hash(record.params_hash)


async def _resolve_mint_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    record: EffectAuditRecord,
) -> _MintLink:
    """Resolve the command an effect record belongs to, if it exists."""
    try:
        command_uuid = uuid.UUID(record.command_id)
    except (ValueError, AttributeError):
        return _MintLink(mint_audit_id=None, linked=False, params=None)
    row = (
        await session.execute(
            select(
                GatewayCommand.mint_audit_id,
                GatewayCommand.params,
                GatewayCommand.params_hash,
            ).where(
                GatewayCommand.id == command_uuid,
                GatewayCommand.tenant_id == tenant_id,
                GatewayCommand.runner_id == runner_id,
            )
        )
    ).one_or_none()
    if row is None:
        return _MintLink(mint_audit_id=None, linked=False, params=None)
    mint_audit_id, params, digest = row
    matched = params if digest == record.params_hash else None
    return _MintLink(mint_audit_id=mint_audit_id, linked=True, params=matched)


async def _load_head(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
) -> RunnerEffectChain | None:
    return (
        await session.execute(
            select(RunnerEffectChain).where(
                RunnerEffectChain.tenant_id == tenant_id,
                RunnerEffectChain.runner_id == runner_id,
            )
        )
    ).scalar_one_or_none()


async def _persist_head(
    session: AsyncSession,
    *,
    head: RunnerEffectChain | None,
    tenant_id: uuid.UUID,
    runner_id: str,
    last_seq: int,
    last_hash: str,
) -> None:
    """Insert or advance the per-runner chain head."""
    now = datetime.now(UTC)
    if head is None:
        session.add(
            RunnerEffectChain(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                runner_id=runner_id,
                last_seq=last_seq,
                last_hash=last_hash,
                updated_at=now,
            )
        )
        return
    head.last_seq = last_seq
    head.last_hash = last_hash
    head.updated_at = now


async def quarantine_effect_chain(
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    error: EffectChainTamperError,
) -> uuid.UUID:
    """Record a tamper-detected forward as a security audit row (own session).

    Written on its own committed transaction (the
    :func:`~meho_backplane.memory.audit.write_internal_audit_row` mould) so it
    survives the caller's rollback of the rejected result submission — the whole
    point is a durable, forensically-visible record that a runner's forwarded
    chain broke. ``event_class='security'`` marks it distinct from a benign
    effect row at audit-query time.
    """
    return await write_internal_audit_row(
        event_class=AuditEventClass.SECURITY_EVENT,
        operator_sub=_SECURITY_OPERATOR_SUB,
        tenant_id=tenant_id,
        method=INTERNAL_METHOD,
        path=EFFECT_QUARANTINE_PATH,
        status_code=409,
        duration_ms=0.0,
        payload={
            "event_class": "security",
            "runner": runner_id,
            "reason": error.reason,
            "seq": error.seq,
        },
    )
