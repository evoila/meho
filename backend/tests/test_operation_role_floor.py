# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The per-operation role floor, checked on every dispatch path.

``targets.register`` and the five ``topology.*`` writes need the
``tenant_admin`` role from a person. Their REST routes and MCP tools check
it at the door; ``call_operation``, the REST dispatch route and the CLI
reach the same operations through ``dispatch``, where the policy gate
checks it (:mod:`meho_backplane.operations.role_floors`).

* **The rule** -- no floor, at the floor and above the floor pass; below
  the floor is refused with a text that names both roles.
* **A person below the floor** -- ``denied``, nothing written, and the
  audit row says ``denied``. The preview says the same.
* **A person at the floor** -- runs at once, as before.
* **An agent** -- waits for approval, as before. A service login waits too.
* **At the decision** -- an ``operator`` cannot approve an agent's parked
  call; the request stays pending and nothing is written. A
  ``tenant_admin`` approves it, and the agent's own resume then runs.
* **After approval** -- the approver's role counts again when a person
  runs the approved call: an ``operator`` approver is refused, a
  ``tenant_admin`` approver runs the call. The requester's own run is
  not checked again, the person's approval is the authorization: an
  agent's own resume runs, and a service login with the ``operator``
  role runs its approved call again after a failed run.
* **The topology writes** -- the same rule on all five.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import OperationResult, ProbeResult
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
    register_targets_registry_operations,
)
from meho_backplane.connectors.topology.ops import (
    TOPOLOGY_ANNOTATE_OP_ID,
    TOPOLOGY_BULK_IMPORT_OP_ID,
    TOPOLOGY_CREATE_NODE_OP_ID,
    TOPOLOGY_DELETE_NODE_OP_ID,
    TOPOLOGY_GRAPH_CONNECTOR_ID,
    TOPOLOGY_UNANNOTATE_OP_ID,
    register_topology_graph_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, GraphNode, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError, approve_request
from meho_backplane.operations.approval_rerun import rerun_failed_request
from meho_backplane.operations.meta_tools import preview_operation
from meho_backplane.operations.role_floors import role_floor_denial, role_floor_for
from meho_backplane.operations.secondary_target_policy import bind_approved_requester
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_PRODUCT = "exampleproduct"
_REGISTER_PARAMS: dict[str, Any] = {
    "name": "new-system",
    "product": _PRODUCT,
    "host": "new-system.example",
}
_TOPOLOGY_PARAMS: dict[str, dict[str, Any]] = {
    TOPOLOGY_ANNOTATE_OP_ID: {"from_name": "svc-a", "kind": "depends-on", "to_name": "db-a"},
    TOPOLOGY_CREATE_NODE_OP_ID: {"kind": "service", "name": "svc-new"},
    TOPOLOGY_DELETE_NODE_OP_ID: {"node_id": str(uuid.uuid4())},
    TOPOLOGY_UNANNOTATE_OP_ID: {"edge_id": str(uuid.uuid4())},
    TOPOLOGY_BULK_IMPORT_OP_ID: {
        "rows": [{"from_name": "svc-a", "kind": "depends-on", "to_name": "db-a"}]
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture
async def _registered_ops() -> AsyncIterator[None]:
    """The targets registry and the topology ops, and a product for the target."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_registry_operations(embedding_service=service)
    await register_topology_graph_operations(embedding_service=service)

    class _ExampleConnector(Connector):
        product = _PRODUCT

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product=_PRODUCT, version="1.0", impl_id=f"{_PRODUCT}-rest", cls=_ExampleConnector
    )
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT_ID, slug="floor-tenant", name="Floor tenant"))
    yield


def _person(
    role: TenantRole,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    sub: str = "p-1",
    approver: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=kind,
        approver=approver,
    )


async def _register(operator: Operator, *, approved: bool = False) -> OperationResult:
    return await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_REGISTER_OP_ID,
        target=None,
        params=dict(_REGISTER_PARAMS),
        _approved=approved,
    )


async def _target_rows() -> list[TargetORM]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(TargetORM).where(TargetORM.tenant_id == _TENANT_ID))
        return list(rows.scalars().all())


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(ApprovalRequest))
        return list(rows.scalars().all())


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row for row in rows if (row.payload or {}).get("op_id") == op_id]


async def _decision_audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision"))
        return list(rows.scalars().all())


async def _park_for_approval(requester: Operator) -> uuid.UUID:
    parked = await _register(requester)
    assert parked.status == "awaiting_approval", parked
    return uuid.UUID(parked.extras["approval_request_id"])


async def _approve(request_id: uuid.UUID, approver: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=dict(_REGISTER_PARAMS))
        await session.commit()


async def _approval_row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    return row


async def _store_failed_run(request_id: uuid.UUID) -> None:
    """Record that the approved call ran and failed, so the requester may run it again."""
    async with get_sessionmaker()() as session, session.begin():
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        row.resumed_at = datetime.now(UTC)
        row.resume_result = {
            "status": "error",
            "op_id": TARGETS_REGISTER_OP_ID,
            "error": "connector_error: not yet",
        }


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_an_op_without_a_floor_has_no_denial() -> None:
    assert role_floor_for("example.read") is None
    assert role_floor_denial("example.read", TenantRole.READ_ONLY) is None


@pytest.mark.parametrize(
    "op_id",
    [
        TARGETS_REGISTER_OP_ID,
        TOPOLOGY_ANNOTATE_OP_ID,
        TOPOLOGY_CREATE_NODE_OP_ID,
        TOPOLOGY_DELETE_NODE_OP_ID,
        TOPOLOGY_UNANNOTATE_OP_ID,
        TOPOLOGY_BULK_IMPORT_OP_ID,
    ],
)
def test_the_registry_writes_need_tenant_admin(op_id: str) -> None:
    assert role_floor_for(op_id) is TenantRole.TENANT_ADMIN
    assert role_floor_denial(op_id, TenantRole.TENANT_ADMIN) is None
    for role in (TenantRole.OPERATOR, TenantRole.READ_ONLY):
        denial = role_floor_denial(op_id, role)
        assert denial == f"{op_id} needs the tenant_admin role; your role is {role.value}"


# ---------------------------------------------------------------------------
# targets.register through dispatch (call_operation, REST dispatch, CLI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
async def test_a_person_below_the_floor_is_denied(_registered_ops: None, role: TenantRole) -> None:
    result = await _register(_person(role))

    assert result.status == "denied", result
    assert result.error is not None
    assert "tenant_admin" in result.error
    assert await _target_rows() == []
    assert await _approval_rows() == []
    (audit,) = await _audit_rows(TARGETS_REGISTER_OP_ID)
    assert audit.payload["result_status"] == "denied"
    assert audit.operator_sub == "p-1"


async def test_the_preview_says_denied_for_a_person_below_the_floor(
    _registered_ops: None,
) -> None:
    preview = await preview_operation(
        _person(TenantRole.OPERATOR),
        {
            "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
            "op_id": TARGETS_REGISTER_OP_ID,
            "params": dict(_REGISTER_PARAMS),
        },
    )

    assert preview["policy_verdict"] == "deny", preview
    assert "tenant_admin" in str(preview["policy_reason"])
    assert await _target_rows() == []


async def test_a_tenant_admin_runs_at_once(_registered_ops: None) -> None:
    result = await _register(_person(TenantRole.TENANT_ADMIN))

    assert result.status == "ok", result
    (row,) = await _target_rows()
    assert row.name == "new-system"
    assert await _approval_rows() == []


async def test_an_agent_still_waits_for_approval(_registered_ops: None) -> None:
    result = await _register(_person(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, sub="agent:a"))

    assert result.status == "awaiting_approval", result
    assert len(await _approval_rows()) == 1
    assert await _target_rows() == []


async def test_a_service_login_still_waits_for_approval(_registered_ops: None) -> None:
    result = await _register(_person(TenantRole.OPERATOR, kind=PrincipalKind.SERVICE, sub="svc:a"))

    assert result.status == "awaiting_approval", result
    assert len(await _approval_rows()) == 1
    assert await _target_rows() == []


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
async def test_a_person_below_the_floor_cannot_approve_the_parked_call(
    _registered_ops: None, role: TenantRole
) -> None:
    """The approval is the authorization for every resume, so the approver needs the role."""
    agent = _person(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, sub="agent:a")
    request_id = await _park_for_approval(agent)
    # ``approver=True`` lets a ``read_only`` person decide requests at all.
    approver = _person(role, sub="approver", approver=True)

    with pytest.raises(ApprovalRoleFloorError, match="tenant_admin"):
        await _approve(request_id, approver)

    (row,) = await _approval_rows()
    assert row.status == "pending"
    assert row.reviewed_by is None
    assert await _decision_audit_rows() == []
    assert await _target_rows() == []


async def test_a_tenant_admin_approves_and_the_agent_resume_runs(_registered_ops: None) -> None:
    """A real approval by a ``tenant_admin``, then the agent's own resume of the call."""
    agent = _person(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, sub="agent:a")
    request_id = await _park_for_approval(agent)

    await _approve(request_id, _person(TenantRole.TENANT_ADMIN, sub="approver"))
    result = await _register(agent, approved=True)

    assert result.status == "ok", result
    (row,) = await _approval_rows()
    assert row.status == "approved"
    assert row.reviewed_by == "approver"
    assert len(await _target_rows()) == 1


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
async def test_an_approver_below_the_floor_is_refused(
    _registered_ops: None, role: TenantRole
) -> None:
    """The approved call runs under the approver, so the approver's role counts."""
    result = await _register(_person(role, sub="approver"), approved=True)

    assert result.status == "denied", result
    assert "tenant_admin" in str(result.error)
    assert await _target_rows() == []
    (audit,) = await _audit_rows(TARGETS_REGISTER_OP_ID)
    assert audit.payload["result_status"] == "denied"


async def test_a_tenant_admin_approver_runs_the_call(_registered_ops: None) -> None:
    result = await _register(_person(TenantRole.TENANT_ADMIN, sub="approver"), approved=True)

    assert result.status == "ok", result
    assert len(await _target_rows()) == 1


async def test_an_agent_runs_its_own_approved_call(_registered_ops: None) -> None:
    """The agent runtime resumes an approved call under the agent; the approval counts."""
    agent = _person(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, sub="agent:a")

    result = await _register(agent, approved=True)

    assert result.status == "ok", result
    assert len(await _target_rows()) == 1


async def test_a_hosted_agent_run_resumes_an_approved_call(_registered_ops: None) -> None:
    """A direct run is bound to the person who started it; the approval counts."""
    with actor_delegation("agent:helper"):
        result = await _register(_person(TenantRole.OPERATOR, sub="starter"), approved=True)

    assert result.status == "ok", result
    assert len(await _target_rows()) == 1


async def test_a_service_login_runs_its_approved_call_again_after_a_failed_run(
    _registered_ops: None,
) -> None:
    """The re-run runs under the requester; the ``tenant_admin`` approval counts."""
    service = _person(TenantRole.OPERATOR, kind=PrincipalKind.SERVICE, sub="svc:a")
    request_id = await _park_for_approval(service)
    await _approve(request_id, _person(TenantRole.TENANT_ADMIN, sub="approver"))
    await _store_failed_run(request_id)

    outcome = await rerun_failed_request(operator=service, request_id=request_id)

    assert outcome.result.status == "ok", outcome.result
    assert outcome.attempt == 2
    assert len(await _target_rows()) == 1
    row = await _approval_row(request_id)
    assert row.resume_result is not None
    assert row.resume_result["status"] == "ok"


async def test_another_person_below_the_floor_is_refused_on_a_service_login_call(
    _registered_ops: None,
) -> None:
    """Only the requester's own run skips the check; a reviewer's run is checked."""
    service = _person(TenantRole.OPERATOR, kind=PrincipalKind.SERVICE, sub="svc:a")
    request_id = await _park_for_approval(service)

    with bind_approved_requester(await _approval_row(request_id)):
        result = await _register(_person(TenantRole.OPERATOR, sub="reviewer"), approved=True)

    assert result.status == "denied", result
    assert "tenant_admin" in str(result.error)
    assert await _target_rows() == []


# ---------------------------------------------------------------------------
# The five topology writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", sorted(_TOPOLOGY_PARAMS))
async def test_a_person_below_the_floor_is_denied_on_every_topology_write(
    _registered_ops: None, op_id: str
) -> None:
    result = await dispatch(
        operator=_person(TenantRole.OPERATOR),
        connector_id=TOPOLOGY_GRAPH_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=dict(_TOPOLOGY_PARAMS[op_id]),
    )

    assert result.status == "denied", result
    assert "tenant_admin" in str(result.error)
    (audit,) = await _audit_rows(op_id)
    assert audit.payload["result_status"] == "denied"


async def test_a_tenant_admin_creates_a_node_at_once(_registered_ops: None) -> None:
    result = await dispatch(
        operator=_person(TenantRole.TENANT_ADMIN),
        connector_id=TOPOLOGY_GRAPH_CONNECTOR_ID,
        op_id=TOPOLOGY_CREATE_NODE_OP_ID,
        target=None,
        params=dict(_TOPOLOGY_PARAMS[TOPOLOGY_CREATE_NODE_OP_ID]),
    )

    assert result.status == "ok", result
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(GraphNode).where(GraphNode.name == "svc-new"))
        assert len(list(rows.scalars().all())) == 1
