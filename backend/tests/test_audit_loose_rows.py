# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Loose audit rows: the query, the typed op, the sensor template, the replay count (#4670).

A loose row is an audit record whose ``parent_audit_id`` names a row that
does not exist. MEHO leaves such rows as they are
(``docs/decisions/composite-audit-when-a-step-waits.md``), so an operator
needs a standing check. These tests cover:

* ``find_loose_rows`` lists a step row and an ``approval.request`` row
  whose parent is missing, skips rows whose parent exists, other tenants'
  rows and self-parented rows, and returns nothing on a clean database;
* the query writes nothing: the audit log row count is unchanged;
* the ``audit.loose_rows`` typed op dispatches as a ``safe`` targetless
  op and returns the counts next to the rows;
* the default sensor template is a valid ``SensorCreate`` and its
  assertion fires ``degraded`` / ``critical`` at the documented bounds;
* ``count_loose_rows_in_forest`` counts the loose roots of a replay tree.

Runs on the per-test SQLite database from ``tests.conftest``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import (
    LooseRowsResult,
    count_loose_rows_in_forest,
    find_loose_rows,
    replay_session,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks import evaluate_assertion
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.connectors.audit.ops import (
    AUDIT_INTEGRITY_CONNECTOR_ID,
    AUDIT_LOOSE_ROWS_OP_ID,
    register_audit_integrity_operations,
)
from meho_backplane.connectors.audit.sensor_template import (
    LOOSE_ROWS_SENSOR_TEMPLATE,
    loose_rows_sensor_template,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, Tenant
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
_NOW = _BASE + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _row(
    *,
    tenant_id: uuid.UUID = _TENANT_A,
    offset: timedelta = timedelta(0),
    parent_audit_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    row_id: uuid.UUID | None = None,
    method: str = "MCP",
    path: str = "/mcp",
    status_code: int = 200,
    event_class: str = "operation_execution",
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    return AuditLog(
        id=row_id or uuid.uuid4(),
        occurred_at=_BASE + offset,
        operator_sub="operator-1",
        tenant_id=tenant_id,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=Decimal("1.0"),
        payload=payload or {"op_id": "vsphere.vm.rename", "op_class": "write"},
        parent_audit_id=parent_audit_id,
        agent_session_id=agent_session_id,
        event_class=event_class,
    )


async def _seed_mixed_rows(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one loose step row and one loose approval request among sound rows.

    Returns ``(step_row_id, approval_request_row_id)``.
    """
    parent = _row()
    linked_step = _row(offset=timedelta(seconds=1), parent_audit_id=parent.id)
    self_parent_id = uuid.uuid4()
    self_parent = _row(
        offset=timedelta(seconds=2), row_id=self_parent_id, parent_audit_id=self_parent_id
    )
    # The operation never saved its own row: both records point at its id.
    lost_operation_id = uuid.uuid4()
    step = _row(offset=timedelta(seconds=3), parent_audit_id=lost_operation_id)
    approval_request = _row(
        offset=-timedelta(days=2),
        parent_audit_id=lost_operation_id,
        method="APPROVAL",
        path="approval.request",
        status_code=202,
        event_class="policy_decision",
        payload={"op_id": "vsphere.vm.rename", "approval_request_id": str(uuid.uuid4())},
    )
    foreign = _row(tenant_id=_TENANT_B, offset=timedelta(seconds=5), parent_audit_id=uuid.uuid4())
    session.add_all([parent, linked_step, self_parent, step, approval_request, foreign])
    await session.commit()
    return step.id, approval_request.id


async def _audit_row_count(session: AsyncSession) -> int:
    return int(await session.scalar(sa.select(sa.func.count()).select_from(AuditLog)) or 0)


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_loose_rows_lists_step_row_and_approval_request(session: AsyncSession) -> None:
    step_id, request_id = await _seed_mixed_rows(session)

    result = await find_loose_rows(tenant_id=_TENANT_A, session=session, now=_NOW)

    assert isinstance(result, LooseRowsResult)
    assert [r.id for r in result.rows] == [step_id, request_id]  # newest first
    assert result.count == 2
    # Only the step row is younger than a day; the request is two days old.
    assert result.recent_count == 1
    assert result.recent_since == _NOW - timedelta(days=1)
    assert result.truncated is False

    step, request = result.rows
    assert step.op_id == "vsphere.vm.rename"
    assert step.event_class == "operation_execution"
    assert step.principal_sub == "operator-1"
    assert step.parent_audit_id == request.parent_audit_id
    assert request.path == "approval.request"
    assert request.event_class == "policy_decision"


@pytest.mark.asyncio
async def test_find_loose_rows_clean_database_returns_nothing(session: AsyncSession) -> None:
    parent = _row()
    child = _row(offset=timedelta(seconds=1), parent_audit_id=parent.id)
    grandchild = _row(offset=timedelta(seconds=2), parent_audit_id=child.id)
    session.add_all([parent, child, grandchild])
    await session.commit()

    result = await find_loose_rows(tenant_id=_TENANT_A, session=session, now=_NOW)

    assert result.rows == []
    assert result.count == 0
    assert result.recent_count == 0
    assert result.truncated is False


@pytest.mark.asyncio
async def test_find_loose_rows_caps_rows_but_keeps_exact_counts(session: AsyncSession) -> None:
    session.add_all(
        [_row(offset=timedelta(seconds=i), parent_audit_id=uuid.uuid4()) for i in range(3)]
    )
    await session.commit()

    result = await find_loose_rows(tenant_id=_TENANT_A, session=session, limit=2, now=_NOW)

    assert len(result.rows) == 2
    assert result.count == 3
    assert result.recent_count == 3
    assert result.truncated is True


@pytest.mark.asyncio
async def test_find_loose_rows_honours_recent_window(session: AsyncSession) -> None:
    session.add_all(
        [
            _row(offset=-timedelta(hours=2), parent_audit_id=uuid.uuid4()),
            _row(offset=-timedelta(hours=30), parent_audit_id=uuid.uuid4()),
        ]
    )
    await session.commit()

    one_hour = await find_loose_rows(
        tenant_id=_TENANT_A, session=session, recent_window=timedelta(hours=1), now=_BASE
    )
    one_day = await find_loose_rows(tenant_id=_TENANT_A, session=session, now=_BASE)
    one_week = await find_loose_rows(
        tenant_id=_TENANT_A, session=session, recent_window=timedelta(days=7), now=_BASE
    )

    assert (one_hour.recent_count, one_day.recent_count, one_week.recent_count) == (0, 1, 2)
    assert one_hour.count == 2


@pytest.mark.asyncio
async def test_find_loose_rows_writes_nothing(session: AsyncSession) -> None:
    step_id, request_id = await _seed_mixed_rows(session)
    before = await _audit_row_count(session)

    first = await find_loose_rows(tenant_id=_TENANT_A, session=session, now=_NOW)
    second = await find_loose_rows(tenant_id=_TENANT_A, session=session, now=_NOW)

    assert await _audit_row_count(session) == before
    assert [r.id for r in first.rows] == [r.id for r in second.rows] == [step_id, request_id]


# ---------------------------------------------------------------------------
# The typed op
# ---------------------------------------------------------------------------


@pytest.fixture
async def _registered_op() -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_audit_integrity_operations(embedding_service=service)
    async with get_sessionmaker()() as s, s.begin():
        s.add(Tenant(id=_TENANT_A, slug="tenant-a", name="Tenant A"))
    yield


def _operator() -> Operator:
    return Operator(
        sub="operator:human",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered_op")
async def test_audit_loose_rows_op_is_safe_and_dispatches(session: AsyncSession) -> None:
    step_id, request_id = await _seed_mixed_rows(session)
    descriptor = await session.scalar(
        sa.select(EndpointDescriptor).where(EndpointDescriptor.op_id == AUDIT_LOOSE_ROWS_OP_ID)
    )
    assert descriptor is not None
    assert descriptor.safety_level == "safe"  # the Sensor create guard accepts only safe ops

    result = await dispatch(
        operator=_operator(),
        connector_id=AUDIT_INTEGRITY_CONNECTOR_ID,
        op_id=AUDIT_LOOSE_ROWS_OP_ID,
        target=None,
        params={"limit": 10},
    )

    assert result.status == "ok", result.error
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["count"] == 2
    assert payload["recent_count"] >= 0
    assert payload["truncated"] is False
    assert [uuid.UUID(r["id"]) for r in payload["rows"]] == [step_id, request_id]
    assert set(payload["rows"][0]) == {
        "id",
        "ts",
        "principal_sub",
        "op_id",
        "event_class",
        "path",
        "parent_audit_id",
    }

    # The check repaired nothing: both rows are still loose. The only new
    # audit row is the record of the check's own dispatch.
    again = await find_loose_rows(tenant_id=_TENANT_A, session=session, now=_NOW)
    assert [r.id for r in again.rows] == [step_id, request_id]
    own_rows = (
        (
            await session.execute(
                sa.select(AuditLog).where(
                    AuditLog.payload["op_id"].as_string() == AUDIT_LOOSE_ROWS_OP_ID
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(own_rows) == 1
    assert own_rows[0].parent_audit_id is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registered_op")
async def test_audit_loose_rows_op_rejects_unknown_params() -> None:
    result = await dispatch(
        operator=_operator(),
        connector_id=AUDIT_INTEGRITY_CONNECTOR_ID,
        op_id=AUDIT_LOOSE_ROWS_OP_ID,
        target=None,
        params={"tenant_id": str(_TENANT_B)},
    )
    assert result.status != "ok"


# ---------------------------------------------------------------------------
# The sensor template
# ---------------------------------------------------------------------------


def test_sensor_template_is_a_valid_sensor_create() -> None:
    body = SensorCreate.model_validate(LOOSE_ROWS_SENSOR_TEMPLATE)
    assert body.connector_id == AUDIT_INTEGRITY_CONNECTOR_ID
    assert body.op_id == AUDIT_LOOSE_ROWS_OP_ID
    assert body.cadence_kind == "interval"
    assert body.interval_seconds == 3600
    assert body.severity == "critical"
    assert body.target is None

    renamed = SensorCreate.model_validate(loose_rows_sensor_template(name="prod-loose-rows"))
    assert renamed.name == "prod-loose-rows"
    assert LOOSE_ROWS_SENSOR_TEMPLATE["name"] == "audit-loose-rows"  # the helper copies


@pytest.mark.parametrize(
    ("recent_count", "expected"),
    [
        (0, "ok"),
        (1, "degraded"),
        (10, "degraded"),
        (11, "critical"),
        (250, "critical"),
    ],
)
def test_sensor_template_assertion_fires_at_the_documented_bounds(
    recent_count: int, expected: str
) -> None:
    """Recorded answers of ``audit.loose_rows`` → the state the template yields."""
    spec = AssertionSpec.model_validate(LOOSE_ROWS_SENSOR_TEMPLATE["assertion"])
    payload = {
        "rows": [],
        "count": recent_count + 3,
        "recent_count": recent_count,
        "recent_since": "2026-10-19T12:00:00Z",
        "truncated": False,
    }
    outcome = evaluate_assertion(spec, payload, now=_NOW)
    assert outcome.state == expected
    assert outcome.value == recent_count


def test_sensor_template_assertion_reads_a_reduced_answer() -> None:
    """Over the JSONFlux threshold ``rows`` sits behind a handle; the counts stay inline."""
    spec = AssertionSpec.model_validate(LOOSE_ROWS_SENSOR_TEMPLATE["assertion"])
    reduced = {
        "row_count": 120,
        "sample_rows_returned": 5,
        "sample": [],
        "count": 120,
        "recent_count": 12,
        "recent_since": "2026-10-19T12:00:00Z",
        "truncated": False,
    }
    assert evaluate_assertion(spec, reduced, now=_NOW).state == "critical"
    assert evaluate_assertion(spec, {"rows": []}, now=_NOW).state == "unknown"


# ---------------------------------------------------------------------------
# The replay count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_loose_rows_in_forest(session: AsyncSession) -> None:
    this_session = uuid.uuid4()
    other_session = uuid.uuid4()
    root = _row(agent_session_id=this_session)
    child = _row(
        offset=timedelta(seconds=1), agent_session_id=this_session, parent_audit_id=root.id
    )
    loose = _row(
        offset=timedelta(seconds=2), agent_session_id=this_session, parent_audit_id=uuid.uuid4()
    )
    other_parent = _row(offset=timedelta(seconds=3), agent_session_id=other_session)
    linked_elsewhere = _row(
        offset=timedelta(seconds=4), agent_session_id=this_session, parent_audit_id=other_parent.id
    )
    foreign_parent = _row(tenant_id=_TENANT_B, offset=timedelta(seconds=5))
    cross_tenant = _row(
        offset=timedelta(seconds=6),
        agent_session_id=this_session,
        parent_audit_id=foreign_parent.id,
    )
    session.add_all(
        [root, child, loose, other_parent, linked_elsewhere, foreign_parent, cross_tenant]
    )
    await session.commit()

    forest = await replay_session(this_session, tenant_id=_TENANT_A, session=session)
    assert [n.id for n in forest] == [root.id, loose.id, linked_elsewhere.id, cross_tenant.id]

    # ``loose`` has no parent anywhere; ``cross_tenant``'s parent is outside
    # the tenant, which the replay closure also treats as absent.
    assert await count_loose_rows_in_forest(forest, tenant_id=_TENANT_A, session=session) == 2
    assert await count_loose_rows_in_forest([], tenant_id=_TENANT_A, session=session) == 0
