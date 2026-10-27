# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector delete is refused while something still uses the connector (#5027).

Service-level tests for :meth:`ReviewService.delete_connector` (the one
path REST, MCP and the console share):

* one refusal test per kind of user: sensor, runbook template (draft and
  published), hosted agent, schedule, waiting approval, approved approval
  that has not run, queued and running operation run;
* finished things (a rejected approval, an approval that already ran, an
  approval decided longer ago than the approval time limit that never
  ran, a finished operation run, a deprecated runbook, a cancelled schedule) do
  not block;
* the refusal lists every user, deletes nothing and writes no audit row;
* with no users the delete works as before, audit row included;
* a built-in delete counts other tenants' users without naming them, and
  skips tenants that have their own copy of the connector;
* the two orders of a sensor create racing a delete: one of the two fails
  cleanly and no sensor is left for a deleted connector;
* the connector-usage lock issues the right PostgreSQL statements.

The real two-transaction race on PostgreSQL is in
``tests/integration/test_connector_delete_in_use_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import SensorAdminService, SensorOperationNotFoundError
from meho_backplane.db import connector_usage_lock
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    OperationRun,
    RunbookTemplate,
    Sensor,
    Tenant,
)
from meho_backplane.operations.ingest import ConnectorInUseError, ReviewService
from meho_backplane.scheduler.repository import create_cron_trigger
from meho_backplane.settings import get_settings

_PRODUCT = "usecheck"
_VERSION = "1.0"
_IMPL_ID = "usecheck-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_OP = "GET:/api/v1/disks"
_OTHER_OP = "GET:/api/v1/other"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant_id: uuid.UUID, *, platform_admin: bool = False) -> Operator:
    return Operator(
        sub=f"test-admin-{uuid.uuid4()}",
        name="Test Admin",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=platform_admin,
    )


async def _seed_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        slug = f"t-{tenant_id.hex[:8]}"
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()
    return tenant_id


async def _seed_connector(tenant_id: uuid.UUID | None) -> None:
    async with get_sessionmaker()() as session:
        for op_id in (_OP, _OTHER_OP):
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=op_id.split(":", 1)[1],
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                )
            )
        await session.commit()


async def _add(row: Any) -> Any:
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
    return row


async def _create_sensor(tenant_id: uuid.UUID, name: str = "disk-free") -> uuid.UUID:
    entry = await SensorAdminService().create(
        tenant_id=tenant_id,
        created_by_sub="admin",
        payload=SensorCreate.model_validate(
            {
                "name": name,
                "connector_id": _CONNECTOR_ID,
                "op_id": _OP,
                "assertion": _ASSERTION,
                "cadence_kind": "interval",
                "interval_seconds": 60,
            }
        ),
    )
    return entry.id


def _runbook(tenant_id: uuid.UUID, *, status: str, verify_only: bool = False) -> RunbookTemplate:
    step: dict[str, object] = {
        "id": "check",
        "title": "Check disks",
        "body": "Look at the disks.",
        "type": "manual" if verify_only else "operation_call",
        "verify": (
            {"type": "operation_call", "op_id": _OP, "params": {}, "expect": {}}
            if verify_only
            else {"type": "confirm", "prompt": "ok?"}
        ),
    }
    if not verify_only:
        step["op_id"] = _OP
        step["params"] = {}
    return RunbookTemplate(
        tenant_id=tenant_id,
        slug=f"disk-check-{status}",
        version=1,
        title="Disk check",
        description="Checks disks.",
        steps=[step],
        status=status,
        created_by="admin",
        edited_by="admin",
    )


def _agent(tenant_id: uuid.UUID, connectors: list[str]) -> AgentDefinition:
    return AgentDefinition(
        tenant_id=tenant_id,
        name=f"nightly-{uuid.uuid4().hex[:6]}",
        identity_ref="agent-principal",
        model_tier="standard",
        system_prompt="Check things.",
        toolset={"connectors": connectors},
        turn_budget=5,
        created_by_sub="admin",
    )


def _approval(
    tenant_id: uuid.UUID,
    *,
    status: str,
    resumed: bool = False,
    decided_at: datetime | None = None,
    resume_result: dict[str, object] | None = None,
) -> ApprovalRequest:
    if decided_at is None and status != "pending":
        decided_at = datetime.now(UTC)
    row = ApprovalRequest(
        tenant_id=tenant_id,
        principal_sub="someone",
        op_id=_OP,
        connector_id=_CONNECTOR_ID,
        params_hash="hash",
        status=status,
        decided_at=decided_at,
        resumed_at=datetime.now(UTC) if resumed else None,
    )
    # Set the result only when there is one: an explicit None is saved as
    # a JSON null, while real rows keep a SQL NULL until a result is stored.
    if resume_result is not None:
        row.resume_result = resume_result
    return row


def _operation_run(tenant_id: uuid.UUID, *, status: str) -> OperationRun:
    return OperationRun(
        tenant_id=tenant_id,
        identity_sub="someone",
        origin="direct",
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        status=status,
    )


async def _descriptor_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            await session.scalar(
                select(func.count(EndpointDescriptor.id)).where(
                    EndpointDescriptor.impl_id == _IMPL_ID
                )
            )
            or 0
        )


async def _delete_audit_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            await session.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.path == "meho_connector_delete")
            )
            or 0
        )


async def _refusal(tenant_id: uuid.UUID) -> ConnectorInUseError:
    with pytest.raises(ConnectorInUseError) as caught:
        await ReviewService(_operator(tenant_id)).delete_connector(
            _CONNECTOR_ID, tenant_id=tenant_id
        )
    return caught.value


# ---------------------------------------------------------------------------
# One refusal per kind of user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_blocks_delete_and_is_named() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    sensor_id = await _create_sensor(tenant)

    exc = await _refusal(tenant)

    assert [(u.kind, u.id, u.name, u.op_id) for u in exc.users] == [
        ("sensor", str(sensor_id), "disk-free", _OP)
    ]
    assert "sensor 'disk-free'" in str(exc)
    assert exc.detail["detail"] == "connector_in_use"
    assert await _descriptor_count() == 2
    assert await _delete_audit_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "published"])
async def test_runbook_template_step_blocks_delete(status: str) -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    row = await _add(_runbook(tenant, status=status))

    exc = await _refusal(tenant)

    assert [(u.kind, u.id, u.name, u.op_id) for u in exc.users] == [
        ("runbook_template", str(row.id), f"disk-check-{status} v1", _OP)
    ]


@pytest.mark.asyncio
async def test_runbook_template_verify_call_blocks_delete() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    await _add(_runbook(tenant, status="draft", verify_only=True))

    exc = await _refusal(tenant)

    assert [(u.kind, u.op_id) for u in exc.users] == [("runbook_template", _OP)]


@pytest.mark.asyncio
async def test_hosted_agent_blocks_delete() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    agent = await _add(_agent(tenant, ["other-1.0", _CONNECTOR_ID]))
    await _add(_agent(tenant, ["other-1.0"]))

    exc = await _refusal(tenant)

    assert [(u.kind, u.id, u.name, u.op_id) for u in exc.users] == [
        ("hosted_agent", str(agent.id), agent.name, None)
    ]


@pytest.mark.asyncio
async def test_schedule_of_hosted_agent_blocks_delete() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    agent = await _add(_agent(tenant, [_CONNECTOR_ID]))
    async with get_sessionmaker()() as session:
        trigger = await create_cron_trigger(
            session,
            tenant_id=tenant,
            agent_definition_id=agent.id,
            cron_expr="0 * * * *",
            inputs=None,
            identity_sub="__scheduler__",
            created_by_sub="admin",
        )
        await session.commit()

    exc = await _refusal(tenant)

    kinds = [(u.kind, u.id) for u in exc.users]
    assert ("schedule", str(trigger.id)) in kinds
    schedule = next(u for u in exc.users if u.kind == "schedule")
    assert schedule.name is not None and agent.name in schedule.name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "resumed"),
    [("pending", False), ("approved", False)],
    ids=["waiting", "approved-not-run"],
)
async def test_approval_request_blocks_delete(status: str, resumed: bool) -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    row = await _add(_approval(tenant, status=status, resumed=resumed))

    exc = await _refusal(tenant)

    assert [(u.kind, u.id, u.op_id) for u in exc.users] == [("approval_request", str(row.id), _OP)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running"])
async def test_operation_run_blocks_delete(status: str) -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    row = await _add(_operation_run(tenant, status=status))

    exc = await _refusal(tenant)

    assert [(u.kind, u.id, u.op_id) for u in exc.users] == [("operation_run", str(row.id), _OP)]


# ---------------------------------------------------------------------------
# What does not block, and the plain delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["approved-long-ago-never-run", "approved-with-stored-result"])
async def test_approved_request_that_cannot_run_does_not_block_delete(case: str) -> None:
    """An approved request with an empty ``resumed_at`` that can no longer run.

    Rows approved before ``resumed_at`` existed, and rows whose agent
    stopped waiting, keep ``resumed_at`` empty. ``approved`` is final, so
    they must not block the delete for ever.
    """
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    if case == "approved-long-ago-never-run":
        time_limit = timedelta(seconds=get_settings().approval_default_ttl_seconds)
        row = _approval(
            tenant,
            status="approved",
            decided_at=datetime.now(UTC) - time_limit - timedelta(minutes=1),
        )
    else:
        row = _approval(tenant, status="approved", resume_result={"status": "ok"})
    await _add(row)

    result = await ReviewService(_operator(tenant)).delete_connector(
        _CONNECTOR_ID, tenant_id=tenant
    )

    assert result.operations_deleted == 2
    assert await _descriptor_count() == 0


@pytest.mark.asyncio
async def test_finished_things_do_not_block_delete() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    await _add(_approval(tenant, status="rejected"))
    await _add(_approval(tenant, status="approved", resumed=True))
    await _add(_operation_run(tenant, status="succeeded"))
    await _add(_runbook(tenant, status="deprecated"))
    agent = await _add(_agent(tenant, ["other-1.0"]))
    async with get_sessionmaker()() as session:
        await create_cron_trigger(
            session,
            tenant_id=tenant,
            agent_definition_id=agent.id,
            cron_expr="0 * * * *",
            inputs=None,
            identity_sub="__scheduler__",
            created_by_sub="admin",
        )
        await session.commit()

    result = await ReviewService(_operator(tenant)).delete_connector(
        _CONNECTOR_ID, tenant_id=tenant
    )

    assert result.operations_deleted == 2
    assert await _descriptor_count() == 0
    assert await _delete_audit_count() == 1


@pytest.mark.asyncio
async def test_refusal_lists_every_user() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    await _create_sensor(tenant, "disk-free")
    await _create_sensor(tenant, "disk-warn")
    await _add(_runbook(tenant, status="published"))
    await _add(_agent(tenant, [_CONNECTOR_ID]))
    await _add(_approval(tenant, status="pending"))
    await _add(_operation_run(tenant, status="running"))

    exc = await _refusal(tenant)

    assert sorted(u.kind for u in exc.users) == [
        "approval_request",
        "hosted_agent",
        "operation_run",
        "runbook_template",
        "sensor",
        "sensor",
    ]
    assert exc.other_tenant_user_count == 0
    assert len(exc.detail["users"]) == 6  # type: ignore[arg-type]
    assert await _descriptor_count() == 2


@pytest.mark.asyncio
async def test_other_tenant_users_do_not_block_a_tenant_delete() -> None:
    tenant = await _seed_tenant()
    other = await _seed_tenant()
    await _seed_connector(tenant)
    await _seed_connector(other)
    await _create_sensor(other)

    await ReviewService(_operator(tenant)).delete_connector(_CONNECTOR_ID, tenant_id=tenant)

    async with get_sessionmaker()() as session:
        left = await session.scalar(
            select(func.count(EndpointDescriptor.id)).where(
                EndpointDescriptor.impl_id == _IMPL_ID, EndpointDescriptor.tenant_id == other
            )
        )
    assert left == 2


# ---------------------------------------------------------------------------
# Built-in connector: every tenant that uses the built-in rows counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builtin_delete_counts_other_tenants_without_names() -> None:
    mine = await _seed_tenant()
    other = await _seed_tenant()
    await _seed_connector(None)
    await _create_sensor(mine, "mine")
    await _create_sensor(other, "theirs")

    exc = await _refusal_builtin(_operator(mine))

    assert [u.name for u in exc.users] == ["mine"]
    assert exc.other_tenant_user_count == 1
    assert "theirs" not in str(exc)
    assert "1 more in other tenants" in str(exc)


@pytest.mark.asyncio
async def test_builtin_delete_names_all_tenants_for_platform_admin() -> None:
    mine = await _seed_tenant()
    other = await _seed_tenant()
    await _seed_connector(None)
    await _create_sensor(other, "theirs")

    exc = await _refusal_builtin(_operator(mine, platform_admin=True))

    assert [(u.name, u.tenant_id) for u in exc.users] == [("theirs", other)]
    assert exc.other_tenant_user_count == 0


@pytest.mark.asyncio
async def test_builtin_delete_skips_tenants_with_their_own_copy() -> None:
    """A tenant with its own rows never calls the built-in rows, so it does not block."""
    mine = await _seed_tenant()
    owner = await _seed_tenant()
    await _seed_connector(None)
    await _seed_connector(owner)
    await _create_sensor(owner)

    result = await ReviewService(_operator(mine)).delete_connector(_CONNECTOR_ID, tenant_id=None)

    assert result.operations_deleted == 2


async def _refusal_builtin(operator: Operator) -> ConnectorInUseError:
    with pytest.raises(ConnectorInUseError) as caught:
        await ReviewService(operator).delete_connector(_CONNECTOR_ID, tenant_id=None)
    return caught.value


# ---------------------------------------------------------------------------
# A sensor create racing the delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_saved_first_makes_the_delete_fail() -> None:
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    await _create_sensor(tenant)

    exc = await _refusal(tenant)

    assert [u.kind for u in exc.users] == ["sensor"]
    assert await _descriptor_count() == 2


@pytest.mark.asyncio
async def test_delete_landing_during_sensor_create_makes_the_create_fail() -> None:
    """The delete runs after the create's first check and before its insert.

    The create reads the operation again inside its write transaction (after
    the shared lock), finds it gone and fails cleanly: no sensor is saved
    for the deleted connector.
    """
    tenant = await _seed_tenant()
    await _seed_connector(tenant)
    from meho_backplane.checks import service as sensor_service

    real_lookup = sensor_service.lookup_descriptor

    async def lookup_then_delete(**kwargs: Any) -> Any:
        found = await real_lookup(**kwargs)
        await ReviewService(_operator(tenant)).delete_connector(_CONNECTOR_ID, tenant_id=tenant)
        return found

    with (
        patch.object(sensor_service, "lookup_descriptor", lookup_then_delete),
        pytest.raises(SensorOperationNotFoundError),
    ):
        await _create_sensor(tenant)

    async with get_sessionmaker()() as session:
        sensors = await session.scalar(
            select(func.count(Sensor.id)).where(Sensor.tenant_id == tenant)
        )
    assert sensors == 0
    assert await _descriptor_count() == 0


# ---------------------------------------------------------------------------
# The lock statements
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.statements: list[tuple[str, dict[str, object]]] = []

    async def connection(self) -> Any:
        class _Conn:
            pass

        conn = _Conn()
        conn.dialect = type("D", (), {"name": self.dialect})()  # type: ignore[attr-defined]
        return conn

    async def execute(self, statement: Any, params: dict[str, object]) -> None:
        self.statements.append((str(statement), params))


@pytest.mark.asyncio
async def test_lock_uses_shared_and_exclusive_xact_locks_on_postgres() -> None:
    session = _FakeSession("postgresql")

    await connector_usage_lock.lock_connector_usage_shared(session)  # type: ignore[arg-type]
    await connector_usage_lock.lock_connector_usage_exclusive(session)  # type: ignore[arg-type]

    key = connector_usage_lock.CONNECTOR_USAGE_LOCK_KEY
    assert session.statements == [
        ("SELECT pg_advisory_xact_lock_shared(:k)", {"k": key}),
        ("SELECT pg_advisory_xact_lock(:k)", {"k": key}),
    ]
    assert 0 < key < 2**63


@pytest.mark.asyncio
async def test_lock_does_nothing_on_sqlite() -> None:
    session = _FakeSession("sqlite")

    await connector_usage_lock.lock_connector_usage_shared(session)  # type: ignore[arg-type]
    await connector_usage_lock.lock_connector_usage_exclusive(session)  # type: ignore[arg-type]

    assert session.statements == []
