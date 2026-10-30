# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each tenant turns a built-in operation on or off for itself (#5029).

A built-in operation's ``is_enabled`` flag is the install default. A
:class:`~meho_backplane.db.models.TenantOperationSetting` row changes the
answer for one tenant only. These tests check that dispatch,
``search_operations`` and ``list_operation_groups`` all read the tenant's
setting first, that another tenant is not affected, that a start or an
update keeps the settings, and that every change writes one audit row.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, TenantOperationSetting
from meho_backplane.operations import dispatch
from meho_backplane.operations.ingest.service import ReviewService
from meho_backplane.operations.meta_tools import list_operation_groups, search_operations
from meho_backplane.operations.tenant_operation_settings import (
    OP_TENANT_OPERATION_SETTING,
    TenantOwnsConnectorError,
    list_operation_settings,
    set_group_setting,
    set_operation_setting,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_TENANT_A = UUID("00000000-0000-0000-0000-00000000a029")
_TENANT_B = UUID("00000000-0000-0000-0000-00000000b029")

_PRODUCT = "acme"
_VERSION = "2"
_IMPL_ID = "acme-ops"
_CONNECTOR_ID = "acme-ops-2"
_GROUP = "widgets"
_LIST_OP = "acme.widget.list"
_COUNT_OP = "acme.widget.count"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


async def _widget_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler; needs no target."""
    return {"ok": True}


def _operator(tenant_id: UUID, *, role: TenantRole = TenantRole.TENANT_ADMIN) -> Operator:
    return Operator(
        sub=f"admin-{tenant_id.hex[-4:]}",
        name="Test Admin",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


async def _register(embedding_service: AsyncMock) -> None:
    """Register the two built-in typed ops (what every start does)."""
    for op_id in (_LIST_OP, _COUNT_OP):
        await register_typed_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
            handler=_widget_handler,
            summary=f"widget op {op_id}",
            description="Widget operation.",
            parameter_schema={"type": "object"},
            when_to_use="Widget operations.",
            group_key=_GROUP,
            embedding_service=embedding_service,
        )


async def _set_install_default(op_id: str, enabled: bool) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == op_id, EndpointDescriptor.tenant_id.is_(None))
            .values(is_enabled=enabled)
        )


async def _call(tenant_id: UUID, op_id: str) -> Any:
    return await dispatch(
        operator=_operator(tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params={},
    )


async def _found(tenant_id: UUID) -> set[str]:
    result = await search_operations(
        _operator(tenant_id), {"connector_id": _CONNECTOR_ID, "query": "widget op"}
    )
    return {hit["op_id"] for hit in result["hits"]}


async def _groups(tenant_id: UUID) -> dict[str, int]:
    result = await list_operation_groups(_operator(tenant_id), {"connector_id": _CONNECTOR_ID})
    return {group["group_key"]: group["enabled_op_count"] for group in result["groups"]}


async def _set(tenant_id: UUID, op_id: str, enabled: bool | None) -> Any:
    return await set_operation_setting(
        _operator(tenant_id),
        tenant_id=tenant_id,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        enabled=enabled,
    )


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.path == OP_TENANT_OPERATION_SETTING)
        )
        return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_tenant_turns_an_op_off_other_tenant_still_uses_it(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)

    item = await _set(_TENANT_A, _LIST_OP, False)
    assert (item.default_enabled, item.tenant_enabled, item.enabled) == (True, False, False)

    refused = await _call(_TENANT_A, _LIST_OP)
    assert refused.status == "error"
    assert refused.extras["error_code"] == "unknown_op"
    allowed = await _call(_TENANT_B, _LIST_OP)
    assert allowed.status == "ok", allowed.error

    assert await _found(_TENANT_A) == {_COUNT_OP}
    assert await _found(_TENANT_B) == {_LIST_OP, _COUNT_OP}
    assert await _groups(_TENANT_A) == {_GROUP: 1}
    assert await _groups(_TENANT_B) == {_GROUP: 2}


@pytest.mark.asyncio
async def test_tenant_turns_on_an_op_that_is_off_by_default(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)
    await _set_install_default(_COUNT_OP, False)

    await _set(_TENANT_A, _COUNT_OP, True)

    allowed = await _call(_TENANT_A, _COUNT_OP)
    assert allowed.status == "ok", allowed.error
    refused = await _call(_TENANT_B, _COUNT_OP)
    assert refused.status == "error"
    assert refused.extras["error_code"] == "unknown_op"
    assert await _found(_TENANT_A) == {_LIST_OP, _COUNT_OP}
    assert await _found(_TENANT_B) == {_LIST_OP}


@pytest.mark.asyncio
async def test_group_off_hides_the_group_for_that_tenant_only(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)

    items = await set_group_setting(
        _operator(_TENANT_A),
        tenant_id=_TENANT_A,
        connector_id=_CONNECTOR_ID,
        group_key=_GROUP,
        enabled=False,
    )

    assert {item.op_id: item.enabled for item in items} == {_LIST_OP: False, _COUNT_OP: False}
    assert await _groups(_TENANT_A) == {}
    assert await _found(_TENANT_A) == set()
    found_in_group = await search_operations(
        _operator(_TENANT_A),
        {"connector_id": _CONNECTOR_ID, "query": "widget op", "group": _GROUP},
    )
    assert found_in_group["hits"] == []
    assert await _groups(_TENANT_B) == {_GROUP: 2}
    assert len(await _audit_rows()) == 1


@pytest.mark.asyncio
async def test_back_to_install_default_removes_the_setting(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)
    await _set(_TENANT_A, _LIST_OP, False)

    item = await _set(_TENANT_A, _LIST_OP, None)

    assert (item.tenant_enabled, item.enabled) == (None, True)
    assert (await _call(_TENANT_A, _LIST_OP)).status == "ok"
    listed = await list_operation_settings(tenant_id=_TENANT_A, connector_id=_CONNECTOR_ID)
    assert listed.items == []


@pytest.mark.asyncio
async def test_start_and_update_keep_the_tenant_settings(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)
    await _set(_TENANT_A, _LIST_OP, False)

    # A start registers every built-in op again.
    await _register(embedding_service)
    assert (await _call(_TENANT_A, _LIST_OP)).status == "error"

    # An update may delete and write the built-in rows again.
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            delete(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
        )
    await _register(embedding_service)

    refused = await _call(_TENANT_A, _LIST_OP)
    assert refused.extras["error_code"] == "unknown_op"
    assert (await _call(_TENANT_B, _LIST_OP)).status == "ok"
    listed = await list_operation_settings(tenant_id=_TENANT_A, connector_id=_CONNECTOR_ID)
    assert [(item.op_id, item.tenant_enabled) for item in listed.items] == [(_LIST_OP, False)]


@pytest.mark.asyncio
async def test_each_change_writes_one_audit_row(embedding_service: AsyncMock) -> None:
    await _register(embedding_service)

    await _set(_TENANT_A, _LIST_OP, False)
    await _set(_TENANT_A, _LIST_OP, False)  # no change, no row
    await _set(_TENANT_A, _LIST_OP, None)

    rows = await _audit_rows()
    assert len(rows) == 2
    payloads = sorted((row.payload["tenant_enabled"] is None, row.payload["op_id"]) for row in rows)
    assert payloads == [(False, _LIST_OP), (True, _LIST_OP)]
    assert all(row.payload["target_tenant_id"] == str(_TENANT_A) for row in rows)


@pytest.mark.asyncio
async def test_tenant_with_its_own_copy_is_refused(embedding_service: AsyncMock) -> None:
    await _register(embedding_service)
    async with get_sessionmaker()() as session, session.begin():
        builtin = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _LIST_OP)
            )
        ).scalar_one()
        copy = EndpointDescriptor(
            tenant_id=_TENANT_A,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=_LIST_OP,
            source_kind="typed",
            handler_ref=builtin.handler_ref,
            parameter_schema={"type": "object"},
            is_enabled=True,
        )
        session.add(copy)

    with pytest.raises(TenantOwnsConnectorError):
        await _set(_TENANT_A, _LIST_OP, False)
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(TenantOperationSetting))).first() is None


@pytest.mark.asyncio
async def test_review_payload_shows_default_and_tenant_setting(
    embedding_service: AsyncMock,
) -> None:
    await _register(embedding_service)
    await _set(_TENANT_A, _LIST_OP, False)

    payload_a = await ReviewService(_operator(_TENANT_A)).get_review_payload(
        _CONNECTOR_ID, _TENANT_A
    )
    payload_b = await ReviewService(_operator(_TENANT_B)).get_review_payload(
        _CONNECTOR_ID, _TENANT_B
    )

    ops_a = {op.op_id: (op.is_enabled, op.tenant_enabled) for op in payload_a.groups[0].ops}
    ops_b = {op.op_id: (op.is_enabled, op.tenant_enabled) for op in payload_b.groups[0].ops}
    assert ops_a == {_LIST_OP: (True, False), _COUNT_OP: (True, None)}
    assert ops_b == {_LIST_OP: (True, None), _COUNT_OP: (True, None)}
