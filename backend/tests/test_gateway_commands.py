# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Single-use capability commands — mint gate, latches, audit lineage (#2500).

Initiative #2415 (Remote execution gateway), Task #2500 — the authorization
keystone. Covers the central mint gate (the safe-only wall + policy-gate
refusals that write no rows), the delivery predicate + params-hash
substitution defence, the one-way consumption latch with central replay
refusal, expiry bounding, and the result → mint audit lineage.

Service-level (no HTTP): ``lookup_descriptor`` / ``policy_gate`` are patched
where a controlled verdict is needed, so the tests exercise the mint
*orchestration* (the order of the ladder and its fail-closed refusals)
against the real ``gateway_command`` / ``audit_log`` tables.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from structlog.testing import capture_logs

import meho_backplane.operations.gateway_commands as gc
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    GatewayCommand,
    GatewayCommandStatus,
    PermissionVerdict,
    Tenant,
)
from meho_backplane.gateway.queue import (
    GATEWAY_COMMAND_DEFAULT_TTL,
    claim_next_command,
    enqueue_command,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.gateway_commands import (
    GatewayCommandAlreadyConsumedError,
    MintRefusalCode,
    accept_command_result,
    consume_command,
    mint_gateway_command,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RUNNER = "runner-a"
_CONNECTOR_ID = "net-1.x"
_OP_ID = "net.ping"
_PARAMS: dict[str, object] = {"host": "10.0.0.1"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test file pins the required settings fields (the conftest owns
    # only DATABASE_URL); get_sessionmaker + the mint audit path load Settings.
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="tenant-a"))
            await session.commit()


def _operator() -> Operator:
    # A non-agent (service) principal: the policy gate default-allows an
    # ordinary op, so a safe op with requires_approval=False auto-executes.
    return Operator(
        sub="minter-sub",
        raw_jwt="",
        tenant_id=_TENANT,
        tenant_role=TenantRole.READ_ONLY,
        principal_kind=PrincipalKind.SERVICE,
    )


def _descriptor(
    *, safety_level: str = "safe", requires_approval: bool = False
) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="net",
        version="1.x",
        impl_id="net",
        op_id=_OP_ID,
        source_kind="typed",
        safety_level=safety_level,
        requires_approval=requires_approval,
        parameter_schema={},
        is_enabled=True,
    )


def _patch_lookup(monkeypatch: pytest.MonkeyPatch, descriptor: EndpointDescriptor | None) -> None:
    async def _fake_lookup(**_kwargs: object) -> EndpointDescriptor | None:
        return descriptor

    monkeypatch.setattr(gc, "lookup_descriptor", _fake_lookup)


async def _count(model: type) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _enqueue(*, params: dict[str, object] | None = None) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        command = await enqueue_command(
            session,
            tenant_id=_TENANT,
            runner_id=_RUNNER,
            op_id=_OP_ID,
            params=params if params is not None else dict(_PARAMS),
            enqueued_by_sub="enq-sub",
        )
        command_id = command.id
        await session.commit()
        return command_id


async def _claim() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER)
        await session.commit()


# ---------------------------------------------------------------------------
# Mint gate — the safe-only wall + policy-gate refusals (no rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["caution", "dangerous"])
async def test_mint_refuses_non_safe_op(monkeypatch: pytest.MonkeyPatch, level: str) -> None:
    """A non-'safe' op is refused before the policy gate — no rows written."""
    await _seed_tenant()
    _patch_lookup(monkeypatch, _descriptor(safety_level=level))

    async def _gate_must_not_run(**_kwargs: object) -> tuple[PermissionVerdict, str | None]:
        raise AssertionError("policy_gate must not be consulted for a non-safe op")

    monkeypatch.setattr(gc, "policy_gate", _gate_must_not_run)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id=_RUNNER,
        )
        await session.commit()

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
    assert await _count(GatewayCommand) == 0
    assert await _count(ApprovalRequest) == 0


async def test_mint_refuses_denied_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DENY verdict refuses the mint — no command row, no approval park."""
    await _seed_tenant()
    _patch_lookup(monkeypatch, _descriptor(safety_level="safe"))

    async def _deny(**_kwargs: object) -> tuple[PermissionVerdict, str | None]:
        return PermissionVerdict.DENY, "denied by test"

    monkeypatch.setattr(gc, "policy_gate", _deny)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id=_RUNNER,
        )
        await session.commit()

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.POLICY_DENIED
    assert await _count(GatewayCommand) == 0
    assert await _count(ApprovalRequest) == 0


async def test_mint_refuses_needs_approval_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requires_approval op yields NEEDS_APPROVAL — refused, never parked."""
    await _seed_tenant()
    # Real policy_gate: a non-agent principal on a requires_approval op routes
    # to NEEDS_APPROVAL. The gateway refuses it (change-ops-over-gateway is v2)
    # rather than writing an approval_request row.
    _patch_lookup(monkeypatch, _descriptor(safety_level="safe", requires_approval=True))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id=_RUNNER,
        )
        await session.commit()

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.NEEDS_APPROVAL
    assert await _count(GatewayCommand) == 0
    assert await _count(ApprovalRequest) == 0


# ---------------------------------------------------------------------------
# Mint gate — the happy path: command row + synchronous audit row
# ---------------------------------------------------------------------------


async def test_mint_writes_synchronous_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful mint writes the command row + its GATEWAY mint audit row."""
    await _seed_tenant()
    _patch_lookup(monkeypatch, _descriptor(safety_level="safe"))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id=_RUNNER,
        )
        command_id = result.command.id  # captured before commit expiry
        mint_audit_id = result.mint_audit_id
        await session.commit()

    assert result.minted
    assert mint_audit_id is not None
    expected_hash = compute_params_hash(_PARAMS)

    async with sessionmaker() as session:
        command = await session.get(GatewayCommand, command_id)
        assert command is not None
        assert command.params_hash == expected_hash
        assert command.mint_audit_id == mint_audit_id
        assert command.expires_at is not None
        assert command.status == GatewayCommandStatus.PENDING.value

        audit = await session.get(AuditLog, mint_audit_id)
        assert audit is not None
        assert audit.method == "GATEWAY"
        assert audit.path == "gateway.command.mint"
        assert audit.status_code == 202
        assert audit.payload["params_hash"] == expected_hash
        assert audit.payload["command_id"] == str(command_id)

    assert await _count(GatewayCommand) == 1


async def test_mint_bounds_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """expires_at is NOT NULL at mint and a too-long caller TTL is bounded down."""
    await _seed_tenant()
    _patch_lookup(monkeypatch, _descriptor(safety_level="safe"))
    far_future = datetime.now(UTC) + timedelta(days=1)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id=_RUNNER,
            expires_at=far_future,
        )
        expires_at = result.command.expires_at
        await session.commit()

    ceiling = datetime.now(UTC) + GATEWAY_COMMAND_DEFAULT_TTL
    assert expires_at is not None
    assert expires_at < far_future, "a caller TTL longer than the default is bounded down"
    assert expires_at <= ceiling + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Consumption latch — central replay refusal (at-most-once)
# ---------------------------------------------------------------------------


async def test_consume_command_refuses_replay() -> None:
    """Of two consume attempts, exactly one wins; the replay is refused + logged."""
    await _seed_tenant()
    command_id = await _enqueue()
    await _claim()  # pending -> delivered

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        won = await consume_command(
            session, tenant_id=_TENANT, runner_id=_RUNNER, command_id=command_id
        )
        assert won.consumed_at is not None
        await session.commit()

    with capture_logs() as logs:
        async with sessionmaker() as session:
            with pytest.raises(GatewayCommandAlreadyConsumedError):
                await consume_command(
                    session, tenant_id=_TENANT, runner_id=_RUNNER, command_id=command_id
                )

    assert any(entry["event"] == "gateway_command_replay_refused" for entry in logs)


async def test_claim_delivery_predicate() -> None:
    """Claim never hands out an expired, already-delivered, or consumed command."""
    await _seed_tenant()
    sessionmaker = get_sessionmaker()

    # (a) A fresh unexpired pending command is claimable.
    fresh = await _enqueue()
    async with sessionmaker() as session:
        row = await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER)
        await session.commit()
        assert row is not None and row.id == fresh

    # (b) Re-claiming finds nothing (the only command is now delivered).
    async with sessionmaker() as session:
        assert await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER) is None

    # (c) An expired pending command is not claimable.
    expired = await _enqueue()
    async with sessionmaker() as session:
        await session.execute(
            update(GatewayCommand)
            .where(GatewayCommand.id == expired)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    async with sessionmaker() as session:
        assert await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER) is None

    # (d) A pending-but-consumed command is not claimable (consumed_at latch).
    consumed = await _enqueue()
    async with sessionmaker() as session:
        await session.execute(
            update(GatewayCommand)
            .where(GatewayCommand.id == consumed)
            .values(consumed_at=datetime.now(UTC))
        )
        await session.commit()
    async with sessionmaker() as session:
        assert await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER) is None

    # (e) A second fresh command is still claimable — the predicate excludes
    #     only the bad rows, not everything.
    fresh2 = await _enqueue()
    async with sessionmaker() as session:
        row = await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER)
        await session.commit()
        assert row is not None and row.id == fresh2


async def test_delivery_refuses_params_hash_mismatch() -> None:
    """Delivery re-hashes stored params against params_hash and refuses on mismatch."""
    await _seed_tenant()
    command_id = await _enqueue(params={"host": "orig"})

    # Tamper the params column post-mint without updating params_hash.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(GatewayCommand)
            .where(GatewayCommand.id == command_id)
            .values(params={"host": "tampered"})
        )
        await session.commit()

    with capture_logs() as logs:
        async with sessionmaker() as session:
            row = await claim_next_command(session, tenant_id=_TENANT, runner_id=_RUNNER)

    assert row is None, "a params_hash mismatch must refuse delivery"
    assert any(entry["event"] == "gateway_command_params_hash_mismatch" for entry in logs)

    # The tampered row stays pending (undelivered) — fail-closed.
    async with sessionmaker() as session:
        tampered = await session.get(GatewayCommand, command_id)
        assert tampered is not None
        assert tampered.status == GatewayCommandStatus.PENDING.value


# ---------------------------------------------------------------------------
# Audit lineage — accepted result links back to the mint row
# ---------------------------------------------------------------------------


async def test_result_audit_links_to_mint_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The accepted result's audit row carries parent_audit_id == mint_audit_id."""
    await _seed_tenant()
    _patch_lookup(monkeypatch, _descriptor(safety_level="safe"))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id=_RUNNER,
        )
        command_id = result.command.id
        mint_audit_id = result.mint_audit_id
        await session.commit()

    await _claim()  # pending -> delivered

    async with sessionmaker() as session:
        await accept_command_result(
            session,
            operator=_operator(),
            runner_id=_RUNNER,
            command_id=command_id,
            outcome=GatewayCommandStatus.SUCCEEDED,
            result={"reachable": True},
        )
        await session.commit()

    async with sessionmaker() as session:
        result_rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.path == "gateway.command.result")
                )
            )
            .scalars()
            .all()
        )
        assert len(result_rows) == 1
        assert result_rows[0].parent_audit_id == mint_audit_id

        # The terminal command carries its consumption latch + outcome.
        command = await session.get(GatewayCommand, command_id)
        assert command is not None
        assert command.status == GatewayCommandStatus.SUCCEEDED.value
        assert command.consumed_at is not None
        assert command.result == {"reachable": True}
