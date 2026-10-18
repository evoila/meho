# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conformance tests: a tenant's own connector row is authoritative (#3870).

When a tenant has its own ``endpoint_descriptor`` rows for a connector
``(product, version, impl_id)``, only those rows count for the tenant. An op
that is disabled on the tenant's row, or not on it at all, must not resolve
through the built-in row (``tenant_id IS NULL``), even when the built-in row
has it enabled.

Dispatch, preview, ``count_known_ops``, ``list_operation_groups`` and
``search_operations`` must all give the same answer. When a tenant has no row
for the connector, the built-in rows still serve it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import _search, dispatch
from meho_backplane.operations._lookup import (
    count_known_ops,
    descriptor_exists_any_state,
    lookup_descriptor,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.meta_tools import list_operation_groups, search_operations
from meho_backplane.settings import get_settings

_TENANT_A = UUID("00000000-0000-0000-0000-00000000a870")
_TENANT_B = UUID("00000000-0000-0000-0000-00000000b870")

_PRODUCT = "acme"
_VERSION = "3"
_IMPL_ID = "acme-rest"
_CONNECTOR_ID = "acme-rest-3"

_READ_CONTENTS = "GET:/repos/{owner}/{repo}/contents/{path}"
_LIST_ISSUES = "GET:/repos/{owner}/{repo}/issues"
_CREATE_ISSUE = "POST:/repos/{owner}/{repo}/issues"


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
def _stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``search_operations`` off the real embedding model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="op-3870",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "acme-target"
    product = _PRODUCT
    id = uuid.uuid4()
    tenant_id = _TENANT_A


async def _seed_group(*, tenant_id: UUID | None, group_key: str) -> UUID:
    group_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
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
    *,
    tenant_id: UUID | None,
    op_id: str,
    is_enabled: bool,
    group_id: UUID | None = None,
) -> None:
    method, path = op_id.split(":", 1)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                summary=f"repo op {op_id}",
                description="repo op",
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe" if method == "GET" else "caution",
                requires_approval=False,
                is_enabled=is_enabled,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=now,
                updated_at=now,
            )
        )


async def _seed_estate() -> None:
    """Built-in row with every op enabled; tenant A's curated row.

    Tenant A disabled ``_READ_CONTENTS`` and never had ``_LIST_ISSUES``.
    Only ``_CREATE_ISSUE`` is enabled on tenant A's row.
    """
    builtin_group = await _seed_group(tenant_id=None, group_key="repos")
    tenant_group = await _seed_group(tenant_id=_TENANT_A, group_key="issues")
    for op_id in (_READ_CONTENTS, _LIST_ISSUES, _CREATE_ISSUE):
        await _seed_op(tenant_id=None, op_id=op_id, is_enabled=True, group_id=builtin_group)
    await _seed_op(
        tenant_id=_TENANT_A, op_id=_READ_CONTENTS, is_enabled=False, group_id=tenant_group
    )
    await _seed_op(tenant_id=_TENANT_A, op_id=_CREATE_ISSUE, is_enabled=True, group_id=tenant_group)


async def _lookup(tenant_id: UUID, op_id: str) -> EndpointDescriptor | None:
    return await lookup_descriptor(
        tenant_id=tenant_id,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
    )


@pytest.mark.asyncio
async def test_disabled_on_tenant_row_does_not_fall_back_to_builtin() -> None:
    await _seed_estate()

    assert await _lookup(_TENANT_A, _READ_CONTENTS) is None


@pytest.mark.asyncio
async def test_absent_on_tenant_row_does_not_fall_back_to_builtin() -> None:
    await _seed_estate()

    assert await _lookup(_TENANT_A, _LIST_ISSUES) is None
    assert not await descriptor_exists_any_state(
        tenant_id=_TENANT_A,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_LIST_ISSUES,
    )


@pytest.mark.asyncio
async def test_enabled_on_tenant_row_resolves_to_tenant_row() -> None:
    await _seed_estate()

    row = await _lookup(_TENANT_A, _CREATE_ISSUE)
    assert row is not None
    assert row.tenant_id == _TENANT_A


@pytest.mark.asyncio
async def test_tenant_without_its_own_row_still_uses_builtin() -> None:
    await _seed_estate()

    for op_id in (_READ_CONTENTS, _LIST_ISSUES, _CREATE_ISSUE):
        row = await _lookup(_TENANT_B, op_id)
        assert row is not None, op_id
        assert row.tenant_id is None


@pytest.mark.asyncio
async def test_known_op_count_counts_only_the_tenant_row() -> None:
    await _seed_estate()

    count_a = await count_known_ops(
        tenant_id=_TENANT_A, product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID
    )
    count_b = await count_known_ops(
        tenant_id=_TENANT_B, product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID
    )
    assert count_a == 1
    assert count_b == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_READ_CONTENTS, _LIST_ISSUES])
async def test_dispatch_refuses_op_the_tenant_row_does_not_enable(op_id: str) -> None:
    await _seed_estate()

    result = await dispatch(
        operator=_operator(_TENANT_A),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "unknown_op"
    assert result.extras["known_op_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_READ_CONTENTS, _LIST_ISSUES])
async def test_preview_refuses_op_the_tenant_row_does_not_enable(op_id: str) -> None:
    await _seed_estate()

    result = await preview_dispatch(
        operator=_operator(_TENANT_A),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params={},
    )

    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "unknown_op"


@pytest.mark.asyncio
async def test_search_operations_returns_only_tenant_row_ops() -> None:
    await _seed_estate()

    hits_a = await search_operations(
        _operator(_TENANT_A), {"connector_id": _CONNECTOR_ID, "query": "repo op"}
    )
    hits_b = await search_operations(
        _operator(_TENANT_B), {"connector_id": _CONNECTOR_ID, "query": "repo op"}
    )

    assert _op_ids(hits_a) == {_CREATE_ISSUE}
    assert _op_ids(hits_b) == {_READ_CONTENTS, _LIST_ISSUES, _CREATE_ISSUE}


@pytest.mark.asyncio
async def test_search_operations_group_filter_ignores_builtin_group() -> None:
    await _seed_estate()

    hits = await search_operations(
        _operator(_TENANT_A),
        {"connector_id": _CONNECTOR_ID, "query": "repo op", "group": "repos"},
    )

    assert _op_ids(hits) == set()


@pytest.mark.asyncio
async def test_list_operation_groups_shows_only_tenant_row_groups() -> None:
    await _seed_estate()

    groups_a = await list_operation_groups(_operator(_TENANT_A), {"connector_id": _CONNECTOR_ID})
    groups_b = await list_operation_groups(_operator(_TENANT_B), {"connector_id": _CONNECTOR_ID})

    assert [g["group_key"] for g in groups_a["groups"]] == ["issues"]
    assert [g["group_key"] for g in groups_b["groups"]] == ["repos"]


def _op_ids(result: dict[str, Any]) -> set[str]:
    return {hit["op_id"] for hit in result["hits"]}


def test_postgres_search_queries_hold_the_visible_scope_predicate() -> None:
    """Both PostgreSQL candidate queries carry ``_VISIBLE_SCOPE_SQL`` word for word."""
    scope = " ".join(_search._VISIBLE_SCOPE_SQL.split())
    for statement in (_search._BM25_CANDIDATES_SQL, _search._COSINE_CANDIDATES_SQL):
        assert scope in " ".join(str(statement.text).split())


def test_scope_guard_fails_when_a_query_drifts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The import-time guard raises when a query no longer matches the predicate."""
    monkeypatch.setattr(_search, "_VISIBLE_SCOPE_SQL", "(tenant_id = :other)")
    with pytest.raises(AssertionError, match="_BM25_CANDIDATES_SQL"):
        _search._assert_scope_inlined()
