# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL proof that a tenant's own connector row is authoritative (#3870).

``search_operations`` runs raw BM25 and pgvector SQL on PostgreSQL, so the
SQLite unit suite (``tests/test_operations_tenant_row_authoritative.py``)
does not reach it. This suite checks on a real cluster that dispatch lookup
and search agree: when a tenant has its own row for a connector, ops that
are disabled or absent there are not served from the built-in row.
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
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.meta_tools import search_operations

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

_TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000a870")
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b870")
_PRODUCT = "acme"
_IMPL_ID = "acme-rest"
_READ_CONTENTS = "GET:/repos/{owner}/{repo}/contents/{path}"
_LIST_ISSUES = "GET:/repos/{owner}/{repo}/issues"
_CREATE_ISSUE = "POST:/repos/{owner}/{repo}/issues"
_VECTOR = [0.1] * 384


@pytest.fixture
async def version(pg_engine: None) -> AsyncIterator[str]:
    """A connector version unique to this test; its rows are removed after."""
    value = f"3.{uuid.uuid4().hex[:8]}"
    yield value
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            delete(EndpointDescriptor).where(
                EndpointDescriptor.product == _PRODUCT,
                EndpointDescriptor.version == value,
            )
        )


async def _seed_op(*, version: str, tenant_id: uuid.UUID | None, op_id: str, enabled: bool) -> None:
    method, path = op_id.split(":", 1)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=version,
                impl_id=_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                summary="repository operation",
                description="repository operation",
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


async def _seed_estate(version: str) -> None:
    for op_id in (_READ_CONTENTS, _LIST_ISSUES, _CREATE_ISSUE):
        await _seed_op(version=version, tenant_id=None, op_id=op_id, enabled=True)
    await _seed_op(version=version, tenant_id=_TENANT_A, op_id=_READ_CONTENTS, enabled=False)
    await _seed_op(version=version, tenant_id=_TENANT_A, op_id=_CREATE_ISSUE, enabled=True)


def _operator(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="op-3870",
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
            {"connector_id": f"{_IMPL_ID}-{version}", "query": "repository operation"},
        )
    return {hit["op_id"] for hit in result["hits"]}


@pytest.mark.asyncio
async def test_search_and_lookup_use_only_the_tenant_row(version: str) -> None:
    await _seed_estate(version)

    assert await _search(_TENANT_A, version) == {_CREATE_ISSUE}
    assert await _search(_TENANT_B, version) == {_READ_CONTENTS, _LIST_ISSUES, _CREATE_ISSUE}

    async def _lookup(op_id: str) -> EndpointDescriptor | None:
        return await lookup_descriptor(
            tenant_id=_TENANT_A,
            product=_PRODUCT,
            version=version,
            impl_id=_IMPL_ID,
            op_id=op_id,
        )

    assert await _lookup(_READ_CONTENTS) is None
    assert await _lookup(_LIST_ISSUES) is None
    row = await _lookup(_CREATE_ISSUE)
    assert row is not None
    assert row.tenant_id == _TENANT_A
