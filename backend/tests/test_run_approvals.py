# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Call-time tests for run approvals (#4126).

A person approves the operation list of one automation run once. Then:

* a listed call runs without a new approval, and its audit rows name the
  run approval;
* an unlisted call waits for approval, as before;
* after a revoke, an end or the time limit, the next listed call waits;
* a destructive call waits for a person, even when it is on the list;
* an approval in tenant A does nothing in tenant B;
* a target kind entry works only with the tenant setting on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    PermissionVerdict,
    RunApproval,
    Target,
    Tenant,
)
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalConflictError,
    RunApprovalGroupError,
    RunApprovalNotFoundError,
    RunApprovalRoleFloorError,
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000412a")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000412b")
_PRINCIPAL = "svc-sub-4126"
_RUN = "run-0001"
_CONNECTOR = "vmware-rest-9.0"
_WRITE_OP = "POST:/vcenter/vm"
_OTHER_OP = "PATCH:/vcenter/vm/{vm}"
_DELETE_OP = "DELETE:/vcenter/vm/{vm}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    get_settings.cache_clear()


_DANGEROUS_OP = "POST:/vcenter/vm/{vm}/power?action=reset"


async def _seed_descriptor(
    op_id: str,
    *,
    safety_level: str = "caution",
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> None:
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                method="POST",
                source_kind="ingested",
                safety_level=safety_level,
                requires_approval=False,
                parameter_schema={},
                tenant_id=None,
            )
        )
        await s.commit()


@pytest.fixture(autouse=True)
async def _descriptors(_settings_env: None) -> None:
    """The operations the tests list; approve reads each one's risk level."""
    await _seed_descriptor(_WRITE_OP)
    await _seed_descriptor(_OTHER_OP)
    await _seed_descriptor(_DANGEROUS_OP, safety_level="dangerous")
    await _seed_descriptor("targets.register", product="meho", version="", impl_id="")


@pytest.fixture
def run_ref() -> Iterator[str]:
    token = work_ref_var.set(_RUN)
    yield _RUN
    work_ref_var.reset(token)


def _operator(
    *,
    tenant_id: uuid.UUID = _TENANT_A,
    sub: str = _PRINCIPAL,
    kind: PrincipalKind = PrincipalKind.SERVICE,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
    )


def _person(tenant_id: uuid.UUID = _TENANT_A) -> Operator:
    return _operator(tenant_id=tenant_id, sub="person-approver", kind=PrincipalKind.USER)


def _descriptor(
    op_id: str = _WRITE_OP, *, method: str = "POST", safety_level: str = "caution"
) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        method=method,
        source_kind="ingested",
        safety_level=safety_level,
        requires_approval=False,
        parameter_schema={},
    )


async def _target(name: str, *, tenant_id: uuid.UUID = _TENANT_A) -> Target:
    async with get_sessionmaker()() as s:
        target = Target(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            aliases=[],
            product="vcenter",
            host=f"{name}.example.test",
            port=443,
        )
        s.add(target)
        await s.commit()
        await s.refresh(target)
        s.expunge(target)
    return target


async def _tenant(tenant_id: uuid.UUID, *, target_kinds: bool) -> None:
    async with get_sessionmaker()() as s:
        s.add(
            Tenant(
                id=tenant_id,
                slug=f"t-{tenant_id.hex[-4:]}",
                name="tenant",
                run_approval_target_kinds_enabled=target_kinds,
            )
        )
        await s.commit()


async def _approved(*entries: RunApprovalEntry, tenant_id: uuid.UUID = _TENANT_A) -> uuid.UUID:
    service = RunApprovalService()
    read = await service.request(
        _operator(tenant_id=tenant_id),
        RunApprovalCreate(work_ref=_RUN, entries=list(entries), reason="build one VM"),
    )
    assert read.status == "pending"
    approved = await service.approve(_person(tenant_id), read.id)
    assert approved.status == "approved"
    return read.id


async def _verdict(
    target: Target | None,
    *,
    descriptor: EndpointDescriptor | None = None,
    operator: Operator | None = None,
    decide_only: bool = False,
) -> tuple[PermissionVerdict, str | None]:
    return await policy_gate(
        operator=operator or _operator(),
        descriptor=descriptor or _descriptor(),
        target=target,
        connector_id=_CONNECTOR,
        decide_only=decide_only,
    )


async def _use_rows(run_approval_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        rows = (
            await s.scalars(
                select(AuditLog).where(
                    AuditLog.method == "APPROVAL", AuditLog.path == "approval.decision"
                )
            )
        ).all()
    return [r for r in rows if r.payload.get("run_approval_id") == str(run_approval_id)]


# ---------------------------------------------------------------------------
# A listed call runs; its audit row names the approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listed_call_runs_and_audit_row_names_the_approval(run_ref: str) -> None:
    target = await _target("vc-a")
    approval_id = await _approved(
        RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP, target_id=target.id)
    )

    verdict, reason = await _verdict(target)

    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason == f"auto-approved by run approval {approval_id}"
    (row,) = await _use_rows(approval_id)
    assert row.payload["decision"] == "auto-approved"
    assert row.payload["reviewed_by"] == f"run_approval:{approval_id}"
    assert row.payload["approved_by_sub"] == "person-approver"
    assert row.payload["matched_by"] == "target"
    assert row.operator_sub == _PRINCIPAL
    assert row.tenant_id == _TENANT_A
    assert row.target_id == target.id
    assert row.work_ref == run_ref
    bound = structlog.contextvars.get_contextvars()
    assert bound["audit_run_approval_id"] == str(approval_id)


@pytest.mark.asyncio
async def test_targetless_entry_covers_only_a_call_without_target(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    assert (await _verdict(None))[0] is PermissionVerdict.AUTO_EXECUTE
    # No target is not "any target".
    target = await _target("vc-a")
    assert (await _verdict(target))[0] is PermissionVerdict.NEEDS_APPROVAL
    assert len(await _use_rows(approval_id)) == 1


@pytest.mark.asyncio
async def test_preview_reports_cover_and_writes_nothing(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    verdict, _ = await _verdict(None, decide_only=True)

    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert await _use_rows(approval_id) == []


# ---------------------------------------------------------------------------
# An unlisted call waits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlisted_call_waits_for_approval(run_ref: str) -> None:
    target = await _target("vc-a")
    other = await _target("vc-b")
    approval_id = await _approved(
        RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP, target_id=target.id)
    )

    # Another operation, another target, another connector.
    assert (await _verdict(target, descriptor=_descriptor(_OTHER_OP, method="PATCH")))[
        0
    ] is PermissionVerdict.NEEDS_APPROVAL
    assert (await _verdict(other))[0] is PermissionVerdict.NEEDS_APPROVAL
    other_connector = await policy_gate(
        operator=_operator(),
        descriptor=_descriptor(),
        target=target,
        connector_id="vmware-rest-8.0",
    )
    assert other_connector[0] is PermissionVerdict.NEEDS_APPROVAL
    assert await _use_rows(approval_id) == []


@pytest.mark.asyncio
async def test_call_of_another_run_or_without_run_waits() -> None:
    await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    assert (await _verdict(None))[0] is PermissionVerdict.NEEDS_APPROVAL
    token = work_ref_var.set("run-0002")
    try:
        assert (await _verdict(None))[0] is PermissionVerdict.NEEDS_APPROVAL
    finally:
        work_ref_var.reset(token)


@pytest.mark.asyncio
async def test_other_principal_with_same_run_ref_waits(run_ref: str) -> None:
    await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    verdict, _ = await _verdict(None, operator=_operator(sub="svc-other"))

    assert verdict is PermissionVerdict.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_pending_request_covers_nothing(run_ref: str) -> None:
    await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)],
            reason="build one VM",
        ),
    )

    assert (await _verdict(None))[0] is PermissionVerdict.NEEDS_APPROVAL


# ---------------------------------------------------------------------------
# Revoke, end and the time limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_revoke_the_next_listed_call_waits(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))
    assert (await _verdict(None))[0] is PermissionVerdict.AUTO_EXECUTE

    revoked = await RunApprovalService().revoke(_person(), approval_id, "stop the run")

    assert revoked.status == "revoked"
    assert revoked.revoked_reason == "stop the run"
    assert (await _verdict(None))[0] is PermissionVerdict.NEEDS_APPROVAL
    with pytest.raises(RunApprovalConflictError):
        await RunApprovalService().revoke(_person(), approval_id, None)


@pytest.mark.asyncio
async def test_after_run_end_the_next_listed_call_waits(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    # Only the principal that asked ends its run.
    with pytest.raises(RunApprovalNotFoundError):
        await RunApprovalService().end(_operator(sub="svc-other"), approval_id)
    ended = await RunApprovalService().end(_operator(), approval_id)

    assert ended.status == "ended"
    assert (await _verdict(None))[0] is PermissionVerdict.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_after_the_time_limit_the_next_listed_call_waits(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))
    async with get_sessionmaker()() as s:
        row = await s.get(RunApproval, approval_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    assert (await _verdict(None))[0] is PermissionVerdict.NEEDS_APPROVAL
    assert (await RunApprovalService().get(_TENANT_A, approval_id)).status == "expired"


# ---------------------------------------------------------------------------
# Destructive calls always wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destructive_call_waits_even_when_it_is_on_the_list(run_ref: str) -> None:
    # Written straight to the table, past the request check, as an old row
    # or a later change of the risk level would be.
    approval_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as s:
        s.add(
            RunApproval(
                id=approval_id,
                tenant_id=_TENANT_A,
                principal_sub=_PRINCIPAL,
                work_ref=_RUN,
                entries=[
                    {"connector_id": _CONNECTOR, "op_id": _DELETE_OP},
                    {"connector_id": _CONNECTOR, "op_id": _WRITE_OP},
                ],
                reason="build and clean up",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
        await s.commit()

    delete = _descriptor(_DELETE_OP, method="DELETE", safety_level="destructive")
    assert (await _verdict(None, descriptor=delete))[0] is PermissionVerdict.NEEDS_APPROVAL
    renamed = _descriptor(_WRITE_OP, safety_level="destructive")
    assert (await _verdict(None, descriptor=renamed))[0] is PermissionVerdict.NEEDS_APPROVAL
    assert await _use_rows(approval_id) == []


@pytest.mark.asyncio
async def test_request_with_a_delete_operation_is_refused() -> None:
    with pytest.raises(RunApprovalValidationError, match="entries\\[1\\]"):
        await RunApprovalService().request(
            _operator(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[
                    RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP),
                    RunApprovalEntry(connector_id=_CONNECTOR, op_id=_DELETE_OP),
                ],
                reason="build and clean up",
            ),
        )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_in_tenant_a_does_nothing_in_tenant_b(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    verdict, _ = await _verdict(None, operator=_operator(tenant_id=_TENANT_B))

    assert verdict is PermissionVerdict.NEEDS_APPROVAL
    with pytest.raises(RunApprovalNotFoundError):
        await RunApprovalService().get(_TENANT_B, approval_id)
    with pytest.raises(RunApprovalNotFoundError):
        await RunApprovalService().revoke(_person(_TENANT_B), approval_id, None)


@pytest.mark.asyncio
async def test_approve_in_tenant_b_is_not_found() -> None:
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)],
            reason="build one VM",
        ),
    )

    with pytest.raises(RunApprovalNotFoundError):
        await RunApprovalService().approve(_person(_TENANT_B), read.id)


# ---------------------------------------------------------------------------
# Target kinds (tenant setting)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_kind_is_refused_without_the_tenant_setting() -> None:
    await _tenant(_TENANT_A, target_kinds=False)

    with pytest.raises(RunApprovalValidationError, match="target kind"):
        await RunApprovalService().request(
            _operator(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[
                    RunApprovalEntry(
                        connector_id=_CONNECTOR, op_id=_WRITE_OP, target_product="vcenter"
                    )
                ],
                reason="build",
            ),
        )


@pytest.mark.asyncio
async def test_target_kind_covers_a_target_found_during_the_run(run_ref: str) -> None:
    await _tenant(_TENANT_A, target_kinds=True)
    approval_id = await _approved(
        RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP, target_product="vcenter")
    )
    found_later = await _target("vc-new")

    assert (await _verdict(found_later))[0] is PermissionVerdict.AUTO_EXECUTE
    (row,) = await _use_rows(approval_id)
    assert row.payload["matched_by"] == "kind"

    # The tenant turns the setting off: the kind entry covers nothing more.
    async with get_sessionmaker()() as s:
        tenant = await s.get(Tenant, _TENANT_A)
        assert tenant is not None
        tenant.run_approval_target_kinds_enabled = False
        await s.commit()
    assert (await _verdict(found_later))[0] is PermissionVerdict.NEEDS_APPROVAL


# ---------------------------------------------------------------------------
# Request and approve rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_live_request_for_the_same_run_is_refused() -> None:
    entry = RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)
    await _approved(entry)

    with pytest.raises(RunApprovalConflictError):
        await RunApprovalService().request(
            _operator(), RunApprovalCreate(work_ref=_RUN, entries=[entry], reason="again")
        )


@pytest.mark.asyncio
async def test_approve_only_once() -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP))

    with pytest.raises(RunApprovalConflictError):
        await RunApprovalService().approve(_person(), approval_id)


@pytest.mark.asyncio
async def test_approver_needs_the_role_floor_of_every_entry() -> None:
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id="meho", op_id="targets.register")],
            reason="register the new appliance",
        ),
    )

    with pytest.raises(RunApprovalRoleFloorError):
        await RunApprovalService().approve(_person(), read.id)
    admin = _operator(sub="admin", kind=PrincipalKind.USER, role=TenantRole.TENANT_ADMIN)
    assert (await RunApprovalService().approve(admin, read.id)).status == "approved"


async def _request_dangerous_list() -> uuid.UUID:
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[
                RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP),
                RunApprovalEntry(connector_id=_CONNECTOR, op_id=_DANGEROUS_OP),
            ],
            reason="build and reset one VM",
        ),
    )
    return read.id


def _approver(*, role: TenantRole, approver: bool = False, sa: bool = False) -> Operator:
    return Operator(
        sub="person-approver",
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        approver=approver,
        self_approver=sa,
    )


@pytest.mark.parametrize(
    "person",
    [
        _approver(role=TenantRole.READ_ONLY, approver=True),
        _approver(role=TenantRole.OPERATOR),
    ],
    ids=["approver-flag", "operator"],
)
@pytest.mark.asyncio
async def test_list_with_a_dangerous_op_needs_the_high_risk_group(person: Operator) -> None:
    approval_id = await _request_dangerous_list()

    with pytest.raises(RunApprovalGroupError, match="approval_group_required: entries\\[1\\]"):
        await RunApprovalService().approve(person, approval_id)
    assert (await RunApprovalService().get(_TENANT_A, approval_id)).status == "pending"


@pytest.mark.parametrize(
    "person",
    [
        _approver(role=TenantRole.TENANT_ADMIN),
        _approver(role=TenantRole.OPERATOR, sa=True),
    ],
    ids=["tenant-admin", "sa-operator"],
)
@pytest.mark.asyncio
async def test_high_risk_group_approves_a_list_with_a_dangerous_op(person: Operator) -> None:
    approval_id = await _request_dangerous_list()

    assert (await RunApprovalService().approve(person, approval_id)).status == "approved"


@pytest.mark.asyncio
async def test_approver_flag_approves_a_caution_only_list() -> None:
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)],
            reason="build one VM",
        ),
    )
    person = _approver(role=TenantRole.READ_ONLY, approver=True)

    assert (await RunApprovalService().approve(person, read.id)).status == "approved"


@pytest.mark.asyncio
async def test_list_with_an_unknown_op_is_not_approved() -> None:
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[
                RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP),
                RunApprovalEntry(connector_id=_CONNECTOR, op_id="POST:/vcenter/not-there"),
            ],
            reason="build one VM",
        ),
    )
    admin = _approver(role=TenantRole.TENANT_ADMIN)

    with pytest.raises(RunApprovalValidationError, match=r"entries\[1\].*not found"):
        await RunApprovalService().approve(admin, read.id)
    assert (await RunApprovalService().get(_TENANT_A, read.id)).status == "pending"


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime.now(UTC) - timedelta(minutes=1),
        datetime.now(UTC) + timedelta(days=8),
        datetime(2030, 1, 1),  # no time zone
    ],
)
@pytest.mark.asyncio
async def test_bad_expiry_is_refused(expires_at: datetime) -> None:
    with pytest.raises(RunApprovalValidationError):
        await RunApprovalService().request(
            _operator(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_WRITE_OP)],
                reason="build",
                expires_at=expires_at,
            ),
        )


@pytest.mark.asyncio
async def test_wildcard_entry_is_refused() -> None:
    with pytest.raises(RunApprovalValidationError, match="exact value"):
        await RunApprovalService().request(
            _operator(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id="POST:/vcenter/*")],
                reason="build",
            ),
        )


# ---------------------------------------------------------------------------
# Through the dispatcher: the call's own audit row names the approval
# ---------------------------------------------------------------------------

_STUB_CONNECTOR = "stub-1.x"
_STUB_OP = "stub.deploy"


class _StubConnector(Connector):
    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _stub_deploy(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"deployed": True}


@pytest.fixture
async def stub_op() -> Any:
    clear_registry()
    reset_dispatcher_caches()
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=_STUB_OP,
        handler=_stub_deploy,
        summary="Stub deploy for run approval tests.",
        description="A caution-level write a service principal needs an approval for.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        embedding_service=service,
    )
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_op")
async def test_dispatch_runs_listed_call_and_dispatch_row_names_the_approval(
    run_ref: str,
) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_STUB_CONNECTOR, op_id=_STUB_OP))

    result = await dispatch(
        operator=_operator(), connector_id=_STUB_CONNECTOR, op_id=_STUB_OP, target=None, params={}
    )

    assert result.status == "ok", result
    async with get_sessionmaker()() as s:
        dispatch_row = (
            await s.scalars(
                select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == _STUB_OP)
            )
        ).one()
    assert dispatch_row.payload["run_approval_id"] == str(approval_id)
    assert len(await _use_rows(approval_id)) == 1
    # The binding belongs to that one call.
    assert "audit_run_approval_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_op")
async def test_dispatch_after_revoke_waits_for_approval(run_ref: str) -> None:
    approval_id = await _approved(RunApprovalEntry(connector_id=_STUB_CONNECTOR, op_id=_STUB_OP))
    await RunApprovalService().revoke(_person(), approval_id, None)

    result = await dispatch(
        operator=_operator(), connector_id=_STUB_CONNECTOR, op_id=_STUB_OP, target=None, params={}
    )

    assert result.status == "awaiting_approval", result
