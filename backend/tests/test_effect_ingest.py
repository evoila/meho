# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Central ingest + chain verification for satellite effect audit (#2901, #3193).

Fail-closed conformance for mechanism 4's centre half
(:mod:`meho_backplane.gateway.effect_ingest`):

* a normal round-trip links the effect rows to the mint audit row
  (``parent_audit_id = gateway_command.mint_audit_id``), store-and-forward
  provenance stamped;
* a **sequence gap** on ingest is flagged tamper-evident (raises, no partial
  ingest);
* a **tampered body** (record_hash no longer re-derives) is flagged;
* a **broken link** (prev_hash not matching the accepted head) is flagged;
* a runner **cannot forge a sibling runner's** chain (the record's ``runner_id``
  must equal the authenticated runner).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GatewayCommand, RunnerEffectChain, Tenant
from meho_backplane.gateway.effect_ingest import (
    EFFECT_AUDIT_PATH,
    STORE_AND_FORWARD_PROVENANCE,
    EffectChainTamperError,
    ingest_effect_records,
)
from meho_backplane.runner.effect_audit import EffectAuditChain, EffectAuditRecord
from meho_backplane.settings import get_settings

_RUNNER = "runner-w"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub=f"runner:{_RUNNER}",
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.RUNNER,
    )


async def _seed_tenant(tenant_id: uuid.UUID) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            slug = f"t-{tenant_id.hex[:8]}"
            session.add(Tenant(id=tenant_id, slug=slug, name=slug))
            await session.commit()


async def _seed_minted_command(
    *,
    tenant_id: uuid.UUID,
    command_id: uuid.UUID,
    runner_id: str = _RUNNER,
) -> uuid.UUID:
    """Seed a minted remote-write command + its synchronous mint audit row.

    Returns the ``mint_audit_id`` the effect rows must link back to.
    """
    mint_audit_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=mint_audit_id,
                operator_sub="op-admin",
                tenant_id=tenant_id,
                method="GATEWAY",
                path="gateway.command.mint",
                status_code=200,
                payload={},
            )
        )
        session.add(
            GatewayCommand(
                id=command_id,
                tenant_id=tenant_id,
                runner_id=runner_id,
                op_id="vmware.vm.tag.set",
                params={"tag": "prod"},
                enqueued_by_sub="op-admin",
                params_hash="ph-1",
                safety_level="caution",
                mint_audit_id=mint_audit_id,
                signature="sig-1",
            )
        )
        await session.commit()
    return mint_audit_id


def _build_records(
    tmp_path: Path,
    *,
    runner_id: str,
    command_id: str,
) -> list[EffectAuditRecord]:
    """A valid two-record (intent + outcome) chain for one command.

    A fresh unique sub-directory per call so a test that builds two independent
    chains (the broken-link case) never shares a head.
    """
    chain = EffectAuditChain(tmp_path / f"chain-{uuid.uuid4().hex}", runner_id=runner_id)
    chain.record_intent(
        command_id=command_id,
        op_id="vmware.vm.tag.set",
        params_hash="ph-1",
        signature="sig-1",
        target_scope="scope-1",
    )
    chain.record_outcome(
        command_id=command_id,
        op_id="vmware.vm.tag.set",
        params_hash="ph-1",
        signature="sig-1",
        target_scope="scope-1",
        outcome="ok",
    )
    return chain.unforwarded()


async def _effect_rows(tenant_id: uuid.UUID) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == EFFECT_AUDIT_PATH)))
            .scalars()
            .all()
        )
    return [r for r in rows if r.tenant_id == tenant_id]


@pytest.mark.asyncio
async def test_roundtrip_links_mint_audit_row(tmp_path: Path) -> None:
    """AC: a clean chain ingests, provenance-marked, linked to the mint audit row."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    mint_audit_id = await _seed_minted_command(tenant_id=tenant, command_id=command_id)
    records = _build_records(tmp_path, runner_id=_RUNNER, command_id=command_id.hex)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        written = await ingest_effect_records(
            session, operator=_operator(tenant), runner_id=_RUNNER, records=records
        )
        await session.commit()
    assert len(written) == 2

    rows = sorted(await _effect_rows(tenant), key=lambda r: r.payload["seq"])
    assert len(rows) == 2
    for row in rows:
        assert row.payload["provenance"] == STORE_AND_FORWARD_PROVENANCE
        assert row.payload["chain_verified"] is True
        assert row.payload["linked"] is True
        assert row.parent_audit_id == mint_audit_id, "effect links to the mint audit row"
    assert [r.payload["phase"] for r in rows] == ["intent", "outcome"]

    # The per-runner head advanced to the last accepted seq.
    async with sessionmaker() as session:
        head = (
            await session.execute(
                select(RunnerEffectChain).where(RunnerEffectChain.tenant_id == tenant)
            )
        ).scalar_one()
    assert head.last_seq == 1
    assert head.runner_id == _RUNNER


@pytest.mark.asyncio
async def test_sequence_gap_is_flagged_tamper_evident(tmp_path: Path) -> None:
    """AC: a dropped/suppressed record (seq gap) is detected on ingest."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_minted_command(tenant_id=tenant, command_id=command_id)
    records = _build_records(tmp_path, runner_id=_RUNNER, command_id=command_id.hex)

    # Forward only the SECOND record (seq 1) — seq 0 is dropped, so the very
    # first ingested record is a gap against the genesis head (expected 0).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with pytest.raises(EffectChainTamperError) as exc:
            await ingest_effect_records(
                session, operator=_operator(tenant), runner_id=_RUNNER, records=[records[1]]
            )
    assert exc.value.seq == 1
    assert "gap" in exc.value.reason

    # No effect rows and no head were written (no partial ingest).
    assert await _effect_rows(tenant) == []


@pytest.mark.asyncio
async def test_tampered_body_is_flagged(tmp_path: Path) -> None:
    """Conformance: an altered record body (record_hash no longer derives) is caught."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_minted_command(tenant_id=tenant, command_id=command_id)
    records = _build_records(tmp_path, runner_id=_RUNNER, command_id=command_id.hex)

    # Mutate the op_id in place without recomputing record_hash — the classic
    # transit tamper. seq (0) and prev_hash (genesis) still line up, so only the
    # record_hash re-derivation catches it.
    tampered = records[0].model_copy(update={"op_id": "vmware.vm.destroy"})

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with pytest.raises(EffectChainTamperError) as exc:
            await ingest_effect_records(
                session, operator=_operator(tenant), runner_id=_RUNNER, records=[tampered]
            )
    assert "record_hash" in exc.value.reason or "tampered" in exc.value.reason


@pytest.mark.asyncio
async def test_broken_link_is_flagged(tmp_path: Path) -> None:
    """Conformance: a valid record whose prev_hash misses the accepted head is caught."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_minted_command(tenant_id=tenant, command_id=command_id)

    # Ingest a genuine seq-0 record so the head is at seq 0 / hash0.
    chain_a = _build_records(tmp_path, runner_id=_RUNNER, command_id=command_id.hex)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await ingest_effect_records(
            session, operator=_operator(tenant), runner_id=_RUNNER, records=[chain_a[0]]
        )
        await session.commit()

    # A DIFFERENT chain's seq-1 record: self-consistent (record_hash derives from
    # its own prev_hash) and seq==1 matches the head, but its prev_hash is that
    # other chain's seq-0 hash, not the accepted head — the broken-link branch.
    chain_b = _build_records(tmp_path, runner_id=_RUNNER, command_id=command_id.hex)
    async with sessionmaker() as session:
        with pytest.raises(EffectChainTamperError) as exc:
            await ingest_effect_records(
                session, operator=_operator(tenant), runner_id=_RUNNER, records=[chain_b[1]]
            )
    assert exc.value.seq == 1
    assert "link" in exc.value.reason or "prev_hash" in exc.value.reason


@pytest.mark.asyncio
async def test_runner_cannot_forge_a_sibling_runners_chain(tmp_path: Path) -> None:
    """Conformance: a record claiming another runner is rejected (keying/identity)."""
    tenant = uuid.uuid4()
    command_id = uuid.uuid4()
    await _seed_tenant(tenant)
    await _seed_minted_command(tenant_id=tenant, command_id=command_id, runner_id="runner-victim")

    # Records authored under "runner-victim" but forwarded on runner-w's channel.
    forged = _build_records(tmp_path, runner_id="runner-victim", command_id=command_id.hex)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with pytest.raises(EffectChainTamperError) as exc:
            await ingest_effect_records(
                session, operator=_operator(tenant), runner_id=_RUNNER, records=forged
            )
    assert "another runner" in exc.value.reason
    assert await _effect_rows(tenant) == []
