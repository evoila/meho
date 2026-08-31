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

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select, update
from structlog.testing import capture_logs

import meho_backplane.operations.gateway_commands as gc
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
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
from meho_backplane.runner.work_item_signing import (
    TARGETLESS_SCOPE,
    load_verify_key,
    verify_remote_write_item,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RUNNER = "runner-a"
_CONNECTOR_ID = "net-1.x"
_OP_ID = "net.ping"
_PARAMS: dict[str, object] = {"host": "10.0.0.1"}

# A fixed Ed25519 keypair for the remote-write signing tests (base64 raw keys).
_SIGNING_KEYPAIR = Ed25519PrivateKey.generate()
_SIGNING_KEY_B64 = base64.b64encode(_SIGNING_KEYPAIR.private_bytes_raw()).decode("ascii")
_VERIFY_KEY_B64 = base64.b64encode(_SIGNING_KEYPAIR.public_key().public_bytes_raw()).decode("ascii")


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
    # Provision the central signing key so an approval-bound remote-write mint
    # can sign (the no-approval / no-key refusals are asserted explicitly).
    monkeypatch.setenv("SATELLITE_WRITE_SIGNING_KEY", _SIGNING_KEY_B64)
    get_settings.cache_clear()


async def _seed_committed_approval(
    *,
    params: dict[str, object] | None = None,
    target_id: uuid.UUID | None = None,
    op_id: str = _OP_ID,
) -> uuid.UUID:
    """Insert an ``approved``, un-consumed ApprovalRequest and return its id."""
    resolved = params if params is not None else dict(_PARAMS)
    now = datetime.now(UTC)
    approval = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        principal_sub="requester-sub",
        op_id=op_id,
        connector_id=_CONNECTOR_ID,
        target_id=target_id,
        params_hash=compute_params_hash(resolved),
        params=resolved,
        proposed_effect={"op_id": op_id},
        status=ApprovalRequestStatus.APPROVED.value,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        decided_at=now,
        reviewed_by="approver-sub",
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(approval)
        await session.commit()
    return approval.id


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
    *,
    safety_level: str = "safe",
    requires_approval: bool = False,
    parameter_schema: dict[str, object] | None = None,
) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="net",
        version="1.x",
        impl_id="net",
        op_id=_OP_ID,
        source_kind="typed",
        safety_level=safety_level,
        requires_approval=requires_approval,
        parameter_schema=parameter_schema if parameter_schema is not None else {},
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


async def _mint_refused_before_policy_gate(
    monkeypatch: pytest.MonkeyPatch, *, level: str
) -> gc.MintResult:
    """Mint an op at *level* with the policy gate wired to blow up if consulted.

    The satellite-mint tier ladder (#3188) refuses an ``EXCLUDED`` or
    unauthorised ``remote-write`` op **before** the policy gate, so a
    consulted gate is a bug. Returns the refusal result for the caller to
    assert the exact code + zero rows written.
    """
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
    return result


@pytest.mark.parametrize("level", ["dangerous", "destructive"])
async def test_mint_refuses_excluded_op(monkeypatch: pytest.MonkeyPatch, level: str) -> None:
    """A ``dangerous`` / ``destructive`` op is never minted — no rows written.

    The ``EXCLUDED`` tier keeps the ``OP_NOT_SAFE`` refusal: the destructive
    tier (#3183/#3196) is excluded from every satellite by default, so a
    delete is never minted to a runner (#3225 conformance, satellite write-path
    decision #2901 / #3187).
    """
    result = await _mint_refused_before_policy_gate(monkeypatch, level=level)

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
    assert await _count(GatewayCommand) == 0
    assert await _count(ApprovalRequest) == 0


async def test_mint_refuses_remote_write_gate_unsatisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``caution`` (remote-write) op is refused fail-closed — no rows written.

    The additive ``remote-write`` tier mints only through the composed gate
    (per-runner allowlist + approval/policy), wired by the sibling tasks
    (#3189-#3193). Until then the gate is fail-closed, so a remote-write op is
    refused with the tier's own refusal code — distinct from ``OP_NOT_SAFE`` —
    before the policy gate and without parking.
    """
    result = await _mint_refused_before_policy_gate(monkeypatch, level="caution")

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.REMOTE_WRITE_GATE_UNSATISFIED
    assert "remote-write" in (result.refusal_reason or "")
    assert await _count(GatewayCommand) == 0
    assert await _count(ApprovalRequest) == 0


# ---------------------------------------------------------------------------
# Remote-write tier — approval-bound minting + signing (#3189, mechanism 1)
# ---------------------------------------------------------------------------


async def _mint_caution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    params: dict[str, object] | None = None,
    target: object = None,
) -> gc.MintResult:
    """Mint a ``caution`` (remote-write) op through the real mint orchestration."""
    await _seed_tenant()
    _patch_lookup(monkeypatch, _descriptor(safety_level="caution"))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await mint_gateway_command(
            session,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params=params if params is not None else dict(_PARAMS),
            runner_id=_RUNNER,
        )
        await session.commit()
    return result


async def _approval_resumed_at(approval_id: uuid.UUID) -> datetime | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(ApprovalRequest, approval_id)
        assert row is not None
        return row.resumed_at


async def test_mint_remote_write_binds_approval_and_signs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caution op mints — signed — against a committed approval (mechanism 1).

    The human approval decision is the authorization (policy gate bypassed for
    this tier); the minted capability carries a verifiable Ed25519 signature,
    the audit row records the approval lineage + signed marker, and the
    approval is consumed single-use.
    """
    approval_id = await _seed_committed_approval()

    result = await _mint_caution(monkeypatch)

    assert result.minted
    command = result.command
    assert command is not None and command.signature is not None
    # The signature verifies under the provisioned verify key over the canonical
    # payload (op_id + params_hash + targetless scope + the bounded expires_at).
    assert (
        verify_remote_write_item(
            load_verify_key(_VERIFY_KEY_B64),
            command.signature,
            op_id=_OP_ID,
            params_hash=compute_params_hash(_PARAMS),
            target_scope=TARGETLESS_SCOPE,
            expires_at=command.expires_at,
        )
        is True
    )
    assert await _count(GatewayCommand) == 1
    # Single-use: the binding approval is now consumed.
    assert await _approval_resumed_at(approval_id) is not None
    # The mint audit row carries the approval lineage + signed marker.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        audit = (
            await session.execute(select(AuditLog).where(AuditLog.path == "gateway.command.mint"))
        ).scalar_one()
    assert audit.payload["approval_request_id"] == str(approval_id)
    assert audit.payload["signed"] is True


async def test_mint_remote_write_refused_on_params_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mint whose params differ from the approved ones binds no approval.

    The ``params_hash`` predicate is the swap defence (#1503 / #3197): the
    seeded approval is never matched, never consumed, and no capability mints.
    """
    approval_id = await _seed_committed_approval(params={"host": "10.0.0.1"})

    result = await _mint_caution(monkeypatch, params={"host": "10.9.9.9"})

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.REMOTE_WRITE_GATE_UNSATISFIED
    assert await _count(GatewayCommand) == 0
    assert await _approval_resumed_at(approval_id) is None


async def test_mint_remote_write_approval_is_single_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One committed approval mints at most one capability."""
    await _seed_committed_approval()

    first = await _mint_caution(monkeypatch)
    second = await _mint_caution(monkeypatch)

    assert first.minted
    assert not second.minted
    assert second.refusal_code is MintRefusalCode.REMOTE_WRITE_GATE_UNSATISFIED
    assert await _count(GatewayCommand) == 1


async def test_mint_remote_write_refused_without_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No central signing key → fail-closed refusal, and the approval is untouched.

    The signing key is checked before the single-use latch is claimed, so a
    fail-closed mint never wastes an approval.
    """
    approval_id = await _seed_committed_approval()
    monkeypatch.setenv("SATELLITE_WRITE_SIGNING_KEY", "")
    get_settings.cache_clear()

    result = await _mint_caution(monkeypatch)

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.REMOTE_WRITE_SIGNING_UNAVAILABLE
    assert await _count(GatewayCommand) == 0
    assert await _approval_resumed_at(approval_id) is None


async def test_mint_remote_write_ignores_non_approved_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-pending (un-decided) request does not authorise a mint."""
    now = datetime.now(UTC)
    await _seed_tenant()
    pending = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        principal_sub="requester-sub",
        op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        params_hash=compute_params_hash(_PARAMS),
        params=dict(_PARAMS),
        proposed_effect={"op_id": _OP_ID},
        status=ApprovalRequestStatus.PENDING.value,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(pending)
        await session.commit()

    result = await _mint_caution(monkeypatch)

    assert not result.minted
    assert result.refusal_code is MintRefusalCode.REMOTE_WRITE_GATE_UNSATISFIED
    assert await _count(GatewayCommand) == 0


async def test_mint_refuses_poisoned_parameter_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored schema with a dangling $ref refuses the mint fail-closed (#3095).

    Same defect class as the dispatcher's ``invalid_op_schema`` branch:
    the descriptor — not the caller's params — is broken, so the refusal
    carries a distinct code and never reaches the policy gate or a runner.
    """
    await _seed_tenant()
    _patch_lookup(
        monkeypatch,
        _descriptor(
            parameter_schema={
                "type": "object",
                "properties": {"host": {"$ref": "#/components/schemas/Ghost"}},
            }
        ),
    )

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
    assert result.refusal_code is MintRefusalCode.INVALID_OP_SCHEMA
    assert result.refusal_reason is not None
    assert "#/components/schemas/Ghost" in result.refusal_reason
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
