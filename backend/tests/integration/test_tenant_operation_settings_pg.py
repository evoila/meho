# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL proof that search reads a tenant's own on/off setting (#5029).

``search_operations`` runs raw BM25 and pgvector SQL on PostgreSQL, so the
SQLite unit suite (``tests/test_tenant_operation_settings.py``) does not
reach it. This suite checks on a real cluster that the inlined
``tenant_operation_setting`` predicate in both candidate queries hides a
built-in operation the tenant turned off, shows one the tenant turned on,
and leaves another tenant on the install default.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, TenantOperationSetting
from meho_backplane.operations.meta_tools import search_operations

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000a029")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b029")
_PRODUCT = "acme"
_IMPL_ID = "acme-rest"
_LIST_WIDGETS = "GET:/widgets"
_COUNT_WIDGETS = "GET:/widgets/count"
_CREATE_WIDGET = "POST:/widgets"
_VECTOR = [0.1] * 384


@pytest.fixture
async def version(pg_engine: None) -> AsyncIterator[str]:
    """A connector version unique to this test; its rows are removed after."""
    value = f"5.{uuid.uuid4().hex[:8]}"
    yield value
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            delete(TenantOperationSetting).where(
                TenantOperationSetting.product == _PRODUCT,
                TenantOperationSetting.version == value,
            )
        )
        await session.execute(
            delete(EndpointDescriptor).where(
                EndpointDescriptor.product == _PRODUCT,
                EndpointDescriptor.version == value,
            )
        )


async def _seed_builtin_op(*, version: str, op_id: str, enabled: bool) -> None:
    method, path = op_id.split(":", 1)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=_PRODUCT,
                version=version,
                impl_id=_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                summary="widget operation",
                description="widget operation",
                group_id=None,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=enabled,
                embedding=_VECTOR,
                custom_description=None,
                custom_notes=None,
                created_at=now,
                updated_at=now,
            )
        )


async def _seed_setting(*, version: str, tenant_id: uuid.UUID, op_id: str, enabled: bool) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TenantOperationSetting(
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=version,
                impl_id=_IMPL_ID,
                op_id=op_id,
                enabled=enabled,
                set_by_sub="op-5029",
                set_at=datetime.now(UTC),
            )
        )


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-5029",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _search(tenant_id: uuid.UUID, version: str) -> set[str]:
    fake = AsyncMock()
    fake.encode_one.return_value = _VECTOR
    with patch("meho_backplane.operations._search.get_embedding_service", return_value=fake):
        result = await search_operations(
            _operator(tenant_id),
            {"connector_id": f"{_IMPL_ID}-{version}", "query": "widget operation"},
        )
    return {hit["op_id"] for hit in result["hits"]}


@pytest.mark.asyncio
async def test_search_reads_the_tenant_setting_before_the_install_default(version: str) -> None:
    await _seed_builtin_op(version=version, op_id=_LIST_WIDGETS, enabled=True)
    await _seed_builtin_op(version=version, op_id=_COUNT_WIDGETS, enabled=False)
    await _seed_builtin_op(version=version, op_id=_CREATE_WIDGET, enabled=True)
    await _seed_setting(version=version, tenant_id=_TENANT_A, op_id=_LIST_WIDGETS, enabled=False)
    await _seed_setting(version=version, tenant_id=_TENANT_A, op_id=_COUNT_WIDGETS, enabled=True)

    assert await _search(_TENANT_A, version) == {_COUNT_WIDGETS, _CREATE_WIDGET}
    assert await _search(_TENANT_B, version) == {_LIST_WIDGETS, _CREATE_WIDGET}
