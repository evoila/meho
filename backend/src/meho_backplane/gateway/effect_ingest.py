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

Residual (design §3, mechanism 4): a **fully compromised** runner holds its own
genesis and can forge a self-consistent alternate chain. Tamper evidence catches
transit tampering + dropped records, **not** a lying edge; that residual is
bounded by the composition (allowlist x credential TTL) and the un-reported-mint
alarm, never by this verification alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import AuditLog, GatewayCommand, RunnerEffectChain
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
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
    """
    mint_audit_id, linked = await _resolve_mint_link(
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
        "params_hash": record.params_hash,
        "signature": record.signature,
        "target_scope": record.target_scope,
        "outcome": record.outcome,
        "recorded_at": record.recorded_at,
        "record_hash": record.record_hash,
        "chain_verified": True,
        "linked": linked,
    }
    session.add(
        AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            parent_audit_id=mint_audit_id,
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


async def _resolve_mint_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    record: EffectAuditRecord,
) -> tuple[uuid.UUID | None, bool]:
    """Resolve the mint audit id the effect record links to, if the command exists."""
    try:
        command_uuid = uuid.UUID(record.command_id)
    except (ValueError, AttributeError):
        return (None, False)
    row = (
        await session.execute(
            select(GatewayCommand.mint_audit_id).where(
                GatewayCommand.id == command_uuid,
                GatewayCommand.tenant_id == tenant_id,
                GatewayCommand.runner_id == runner_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return (None, False)
    return (row, True)


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
