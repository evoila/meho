# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An edit of one operation's settings is a ``config_change`` audit row (#5791).

``meho_connector_edit_op`` writes a row whose ``payload["op_id"]`` names
the edited operation. That row used to be ``operation_execution``, so it
looked like a run of the edited operation. Now:

* turning an operation on or off, and changing or removing its risk level
  override, writes a ``config_change`` row;
* a filter on ``event_class = operation_execution`` does not return it,
  also when it filters on the edited op id;
* the other connector review rows (for example ``enable_connector``) keep
  ``operation_execution``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_EDITED_OP = "GET:/api/v1/licenses"
_OTHER_OP = "GET:/api/v1/hosts"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-config-change",
        name="Config Change Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed(tenant_id: uuid.UUID) -> None:
    """One staged group with two ingested reads, both off."""
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                group_key="inventory",
                name="Inventory",
                when_to_use="Use it to read the inventory.",
                review_status="staged",
            )
        )
        for op_id in (_EDITED_OP, _OTHER_OP):
            method, _, path = op_id.partition(":")
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    group_id=group_id,
                    summary=op_id,
                    is_enabled=False,
                )
            )
        await session.commit()


async def _rows(path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        stmt = select(AuditLog).where(AuditLog.path == path).order_by(AuditLog.occurred_at)
        return list((await session.execute(stmt)).scalars().all())


async def _query(tenant_id: uuid.UUID, **filters: object) -> list[str]:
    """The ``path`` of each row the audit query returns for *filters*."""
    async with get_sessionmaker()() as session:
        result = await query_audit(
            AuditQueryFilters.model_validate(filters), tenant_id=tenant_id, session=session
        )
    return [row.path for row in result.rows]


@pytest.mark.asyncio
async def test_turning_an_op_on_and_off_writes_config_change_rows() -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    service = ReviewService(_operator(tenant_id))

    await service.edit_op(_CONNECTOR_ID, _EDITED_OP, tenant_id=tenant_id, is_enabled=True)
    await service.edit_op(_CONNECTOR_ID, _EDITED_OP, tenant_id=tenant_id, is_enabled=False)

    rows = await _rows("meho_connector_edit_op")
    assert [row.event_class for row in rows] == [
        AuditEventClass.CONFIG_CHANGE,
        AuditEventClass.CONFIG_CHANGE,
    ]
    assert [row.payload["is_enabled_set_to"] for row in rows] == [True, False]
    assert all(row.payload["op_id"] == _EDITED_OP for row in rows)
    assert all(row.status_code == 200 for row in rows)


@pytest.mark.asyncio
async def test_an_execution_filter_does_not_return_the_edit() -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    await ReviewService(_operator(tenant_id)).edit_op(
        _CONNECTOR_ID, _EDITED_OP, tenant_id=tenant_id, is_enabled=True
    )

    execution = AuditEventClass.OPERATION_EXECUTION
    assert await _query(tenant_id, event_class=execution) == []
    assert await _query(tenant_id, event_class=execution, op_id=_EDITED_OP) == []
    # The edit is still found by its op id and by its own class.
    assert await _query(tenant_id, op_id=_EDITED_OP) == ["meho_connector_edit_op"]
    assert await _query(tenant_id, event_class=AuditEventClass.CONFIG_CHANGE) == [
        "meho_connector_edit_op"
    ]


@pytest.mark.asyncio
async def test_removing_a_risk_level_override_writes_a_config_change_row() -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    service = ReviewService(_operator(tenant_id))
    await service.edit_op(_CONNECTOR_ID, _EDITED_OP, tenant_id=tenant_id, safety_level="dangerous")

    await service.remove_op_override(
        _CONNECTOR_ID,
        _EDITED_OP,
        override_tenant_id=tenant_id,
        reason=None,
        person_present=False,
    )

    rows = await _rows("meho_connector_edit_op")
    assert len(rows) == 2
    assert rows[-1].payload["override_removed"] is True
    assert {row.event_class for row in rows} == {AuditEventClass.CONFIG_CHANGE}


@pytest.mark.asyncio
async def test_other_review_rows_keep_operation_execution() -> None:
    """Only the edit of one operation changes class; enabling the connector does not."""
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)

    await ReviewService(_operator(tenant_id)).enable_connector(_CONNECTOR_ID, tenant_id=tenant_id)

    (row,) = await _rows("meho_connector_enable")
    assert row.event_class == AuditEventClass.OPERATION_EXECUTION
