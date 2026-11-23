# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Seed rows for the "a service login reads only its own audit rows" tests.

One tenant with rows of two logins: the service login (``SERVICE_SUB``) and a
person (``PERSON_SUB``). Each login has one call with a stored trace, one call
without a trace, one session of its own, and one call with no session. One
more session holds a row of each login. All rows sit in May 2026, so a window
on that month leaves out every row a test request writes itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, Tenant
from meho_backplane.flight_recorder import SpanInput, record_trace

SERVICE_SUB = "svc-login-sub"
PERSON_SUB = "person-sub"
WORK_REF = "chg-100"

BASE = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
#: ``since`` / ``until`` that hold every seeded row and no row a request writes.
WINDOW = {"since": "2026-05-01T00:00:00Z", "until": "2026-06-01T00:00:00Z"}


@dataclass(frozen=True)
class LoginRows:
    """The ids of the rows of one login."""

    traced: uuid.UUID
    untraced: uuid.UUID
    session_root: uuid.UUID
    session_child: uuid.UUID
    no_session: uuid.UUID

    @property
    def all_ids(self) -> set[str]:
        return {
            str(i)
            for i in (
                self.traced,
                self.untraced,
                self.session_root,
                self.session_child,
                self.no_session,
            )
        }


@dataclass(frozen=True)
class Seeded:
    """Everything the tests compare against."""

    service: LoginRows
    person: LoginRows
    service_session: uuid.UUID
    person_session: uuid.UUID
    mixed_session: uuid.UUID
    #: The person's row that has a row of the service login as its parent.
    person_child_of_service: uuid.UUID


def _row(
    *,
    tenant_id: uuid.UUID,
    sub: str,
    second: int,
    method: str = "MCP",
    agent_session_id: uuid.UUID | None = None,
    parent_audit_id: uuid.UUID | None = None,
) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=BASE + timedelta(seconds=second),
        operator_sub=sub,
        tenant_id=tenant_id,
        method=method,
        path="/mcp/tools/call/call_operation",
        status_code=200,
        duration_ms=Decimal("1.0"),
        payload={"op_id": "vsphere.vm.list", "op_class": "read"},
        event_class=AuditEventClass.OPERATION_EXECUTION,
        agent_session_id=agent_session_id,
        parent_audit_id=parent_audit_id,
        work_ref=WORK_REF,
    )


def _span() -> SpanInput:
    return SpanInput(
        span_kind="vendor_call",
        name="GET /rest/vcenter/vm",
        started_at=BASE,
        status="200",
        attributes={"op_id": "vsphere.vm.list"},
    )


def _seed_login(
    tenant_id: uuid.UUID, sub: str, session_id: uuid.UUID, offset: int
) -> tuple[LoginRows, list[AuditLog]]:
    traced = _row(tenant_id=tenant_id, sub=sub, second=offset)
    untraced = _row(tenant_id=tenant_id, sub=sub, second=offset + 1)
    root = _row(tenant_id=tenant_id, sub=sub, second=offset + 2, agent_session_id=session_id)
    child = _row(
        tenant_id=tenant_id,
        sub=sub,
        second=offset + 3,
        agent_session_id=session_id,
        parent_audit_id=root.id,
    )
    no_session = _row(tenant_id=tenant_id, sub=sub, second=offset + 4)
    rows = [traced, untraced, root, child, no_session]
    ids = LoginRows(
        traced=traced.id,
        untraced=untraced.id,
        session_root=root.id,
        session_child=child.id,
        no_session=no_session.id,
    )
    return ids, rows


async def seed(tenant_id: uuid.UUID, *, with_tenant: bool = True) -> Seeded:
    """Write the rows of both logins (and the tenant row) and return their ids."""
    service_session, person_session, mixed_session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service, service_rows = _seed_login(tenant_id, SERVICE_SUB, service_session, 0)
    person, person_rows = _seed_login(tenant_id, PERSON_SUB, person_session, 10)
    mixed = [
        _row(tenant_id=tenant_id, sub=SERVICE_SUB, second=20, agent_session_id=mixed_session),
        _row(tenant_id=tenant_id, sub=PERSON_SUB, second=21, agent_session_id=mixed_session),
    ]
    person_child = _row(
        tenant_id=tenant_id,
        sub=PERSON_SUB,
        second=22,
        method="POST",
        parent_audit_id=service.session_root,
    )
    async with get_sessionmaker()() as session:
        if with_tenant:
            session.add(Tenant(id=tenant_id, slug="own-rows", name="Own rows"))
        session.add_all([*service_rows, *person_rows, *mixed, person_child])
        await session.commit()
    for traced in (service.traced, person.traced):
        await record_trace(audit_id=traced, tenant_id=tenant_id, spans=[_span()], now=BASE)
    return Seeded(
        service=service,
        person=person,
        service_session=service_session,
        person_session=person_session,
        mixed_session=mixed_session,
        person_child_of_service=person_child.id,
    )
