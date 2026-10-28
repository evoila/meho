# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operation rows record the connector package that wrote them (#5037).

* Registration writes ``owner_package`` for an op whose handler lives in
  a folder of an installed package, and ``NULL`` for a built-in op.
* When the package is not installed, its rows are hidden from
  ``search_operations`` and ``list_operation_groups``, and a call or a
  preview answers ``connector_package_not_installed``. Nothing is deleted.
* When the package is back, the rows are visible again with the
  operator's settings (an op the operator turned off stays off).
* Built-in rows (owner ``NULL``) behave as before.

The package and connector names are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane import connector_packages
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connector_packages import (
    connector_package_of,
    installed_connector_packages,
    record_installed_connector_packages,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.meta_tools import list_operation_groups, search_operations
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings
from tests.fixtures.composites.handlers import typed_sub_op_handler

_TENANT = UUID("00000000-0000-0000-0000-000000005037")
_PACKAGE = "example-connectors"

_PRODUCT = "widget"
_VERSION = "2"
_IMPL_ID = "widget-api"
_CONNECTOR_ID = "widget-api-2"

_PACKAGE_OP = "widget.item.list"
_PACKAGE_OP_OFF = "widget.item.delete"
_BUILTIN_OP = "widget.health.read"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _installed_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no package installed; restore the real state after."""
    monkeypatch.setattr(connector_packages, "_installed_folders", {})


@pytest.fixture(autouse=True)
def _stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``search_operations`` off the real embedding model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)


def _install() -> None:
    record_installed_connector_packages({"widget_pkg": _PACKAGE})


def _remove() -> None:
    record_installed_connector_packages({})


def _operator() -> Operator:
    return Operator(
        sub="op-5037",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_group(group_key: str) -> UUID:
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key=group_key,
                name=group_key,
                when_to_use=f"{group_key} ops.",
                review_status="enabled",
            )
        )
    return group_id


async def _seed_op(
    *, op_id: str, owner: str | None, group_id: UUID, is_enabled: bool = True
) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=op_id,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref=f"meho_backplane.connectors.widget_pkg.ops.{op_id.replace('.', '_')}",
                summary=f"widget op {op_id}",
                description="widget op",
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=is_enabled,
                custom_notes="operator note" if owner else None,
                owner_package=owner,
                created_at=now,
                updated_at=now,
            )
        )


async def _seed_estate() -> None:
    """One group of package ops (one turned off by the operator), one built-in group."""
    package_group = await _seed_group("items")
    builtin_group = await _seed_group("health")
    await _seed_op(op_id=_PACKAGE_OP, owner=_PACKAGE, group_id=package_group)
    await _seed_op(op_id=_PACKAGE_OP_OFF, owner=_PACKAGE, group_id=package_group, is_enabled=False)
    await _seed_op(op_id=_BUILTIN_OP, owner=None, group_id=builtin_group)


async def _search() -> set[str]:
    result = await search_operations(
        _operator(), {"connector_id": _CONNECTOR_ID, "query": "widget op"}
    )
    return {hit["op_id"] for hit in result["hits"]}


async def _groups() -> list[str]:
    result = await list_operation_groups(_operator(), {"connector_id": _CONNECTOR_ID})
    return [g["group_key"] for g in result["groups"]]


async def _call(op_id: str) -> Any:
    return await dispatch(
        operator=_operator(), connector_id=_CONNECTOR_ID, op_id=op_id, target=None, params={}
    )


async def _rows() -> dict[str, EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EndpointDescriptor))).scalars().all()
    return {row.op_id: row for row in rows}


def test_connector_package_of_maps_installed_folders_only() -> None:
    _install()
    assert installed_connector_packages() == frozenset({_PACKAGE})
    assert connector_package_of("meho_backplane.connectors.widget_pkg.ops.Widget.list") == _PACKAGE
    assert connector_package_of("meho_backplane.connectors.vault.connector.VaultConnector") is None
    assert connector_package_of("tests.fixtures.composites.handlers.typed_sub_op_handler") is None
    _remove()
    assert installed_connector_packages() == frozenset()
    assert connector_package_of("meho_backplane.connectors.widget_pkg.ops.Widget.list") is None


@pytest.mark.asyncio
async def test_registration_records_the_owner_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler in a package folder is owned by the package; a built-in one is not."""

    async def _register(op_id: str) -> None:
        await register_typed_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
            handler=typed_sub_op_handler,
            summary=f"widget op {op_id}",
            description="widget op",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=AsyncMock(encode_one=AsyncMock(return_value=[0.1] * 384)),
        )

    await _register(_BUILTIN_OP)
    # Treat the test fixtures folder as a connector folder of the package.
    monkeypatch.setattr(connector_packages, "_CONNECTORS_MODULE_PREFIX", "tests.")
    record_installed_connector_packages({"fixtures": _PACKAGE})
    await _register(_PACKAGE_OP)

    rows = await _rows()
    assert rows[_BUILTIN_OP].owner_package is None
    assert rows[_PACKAGE_OP].owner_package == _PACKAGE


@pytest.mark.asyncio
async def test_removed_package_rows_are_hidden_not_deleted() -> None:
    await _seed_estate()
    _remove()

    assert await _search() == {_BUILTIN_OP}
    assert await _groups() == ["health"]

    rows = await _rows()
    assert set(rows) == {_PACKAGE_OP, _PACKAGE_OP_OFF, _BUILTIN_OP}


@pytest.mark.asyncio
async def test_call_and_preview_of_a_removed_package_op_are_refused() -> None:
    await _seed_estate()
    _remove()

    result = await _call(_PACKAGE_OP)
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_package_not_installed"
    assert result.extras["package"] == _PACKAGE
    assert result.error.startswith("connector_package_not_installed:")
    assert _PACKAGE in result.error

    preview = await preview_dispatch(
        operator=_operator(), connector_id=_CONNECTOR_ID, op_id=_PACKAGE_OP, target=None, params={}
    )
    assert preview["status"] == "error"
    assert preview["extras"]["error_code"] == "connector_package_not_installed"


@pytest.mark.asyncio
async def test_returned_package_brings_back_rows_with_operator_settings() -> None:
    await _seed_estate()
    _remove()
    assert await _search() == {_BUILTIN_OP}

    _install()
    assert await _search() == {_BUILTIN_OP, _PACKAGE_OP}
    assert await _groups() == ["health", "items"]

    rows = await _rows()
    assert rows[_PACKAGE_OP_OFF].is_enabled is False
    assert rows[_PACKAGE_OP].custom_notes == "operator note"

    result = await _call(_PACKAGE_OP_OFF)
    assert result.extras["error_code"] == "unknown_op"


@pytest.mark.asyncio
async def test_builtin_rows_behave_as_before() -> None:
    await _seed_estate()

    for record in (_remove, _install):
        record()
        assert _BUILTIN_OP in await _search()
        assert "health" in await _groups()
        row = await lookup_descriptor(
            tenant_id=_TENANT,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=_BUILTIN_OP,
        )
        assert row is not None
        assert row.owner_package is None
        result = await _call("widget.nothing.here")
        assert result.extras["error_code"] == "unknown_op"
