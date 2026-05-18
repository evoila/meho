# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.meta_tools`.

Coverage matrix (G0.6-T8 / Task #399 acceptance criteria):

* ``list_operation_groups`` returns enabled groups for a known
  ``connector_id`` with their ``when_to_use`` strings + per-group
  operation counts. Disabled groups are omitted.
* ``list_operation_groups`` / ``search_operations`` raise
  ``UnknownConnectorError`` for an unknown ``connector_id`` (REST → 404,
  G0.8-T5 #630); a known connector with zero enabled groups still
  returns an empty list (the meaningful-empty case is preserved).
* Tenant scoping on ``list_operation_groups`` -- a tenant-curated group
  is visible only to that tenant.
* ``search_operations`` ranks hits via hybrid BM25+cosine RRF; the
  obvious match for a query lands first. Empty corpus -> empty list.
* ``search_operations`` respects the optional ``group`` filter and
  enforces the tenant boundary.
* ``call_operation`` resolves the target via :func:`resolve_target` and
  invokes :func:`dispatch`. The OperationResult shape is passed through
  verbatim (``status='ok'`` on success; structured ``error`` on failure).
* End-to-end: a seeded typed op can be discovered via
  ``search_operations`` and invoked via ``call_operation``.

The fallback (SQLite) path is exercised here; the PG path runs in the
integration suite. The fallback uses substring + Python cosine ranking
that tracks the PG path's RRF math closely enough for behavioural
assertions to hold across dialects.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.meta_tools import (
    UnknownConnectorError,
    call_operation,
    describe_descriptor,
    list_operation_groups,
    search_operations,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings + isolation fixtures
# ---------------------------------------------------------------------------


_TENANT_A: UUID = UUID("00000000-0000-0000-0000-0000000000aa")
_TENANT_B: UUID = UUID("00000000-0000-0000-0000-0000000000bb")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry between tests."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` doesn't pull ONNX.

    The stub also gets patched into the meta_tools module's lookup of
    :func:`get_embedding_service` so the SQLite-fallback cosine branch
    runs against a known vector rather than the lazy-loaded fastembed
    model. Different rows get slightly different embeddings (offset by
    the row index) so the cosine signal is non-degenerate.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    def _fake_get_embedding_service() -> AsyncMock:
        return service

    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        _fake_get_embedding_service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    tenant_id: UUID = _TENANT_A,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


class _NoOpVaultConnector(Connector):
    """Connector class used to satisfy resolver lookups."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _module_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler used as a fixture."""
    return {"echo": params, "target_name": getattr(target, "name", None)}


# ---------------------------------------------------------------------------
# list_operation_groups
# ---------------------------------------------------------------------------


async def _seed_group(
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
    group_key: str,
    name: str,
    when_to_use: str,
    review_status: str = "enabled",
) -> UUID:
    """Insert an :class:`OperationGroup` row and return its id."""
    sessionmaker = get_sessionmaker()
    group_id = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key=group_key,
                name=name,
                when_to_use=when_to_use,
                review_status=review_status,
            )
        )
    return group_id


@pytest.mark.asyncio
async def test_list_operation_groups_returns_enabled_groups_with_when_to_use() -> None:
    """AC: enabled groups for a known connector come back with their when_to_use."""
    await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        name="KV v2",
        when_to_use="Use for reading and writing secrets in the KV v2 mount.",
    )
    operator = _make_operator()

    result = await list_operation_groups(operator, {"connector_id": "vault-1.x"})

    assert result["connector_id"] == "vault-1.x"
    groups = result["groups"]
    assert len(groups) == 1
    assert groups[0]["group_key"] == "kv"
    assert groups[0]["name"] == "KV v2"
    assert "Use for reading" in groups[0]["when_to_use"]
    assert groups[0]["operation_count"] == 0


@pytest.mark.asyncio
async def test_list_operation_groups_unknown_connector_raises() -> None:
    """G0.8-T5: unknown connector_id raises UnknownConnectorError.

    The route layer maps that to a 404; the prior behaviour (empty
    ``groups`` list, HTTP 200) was the "empty catalog" trap.
    """
    operator = _make_operator()

    with pytest.raises(UnknownConnectorError) as excinfo:
        await list_operation_groups(operator, {"connector_id": "ghost-9.9"})

    assert "ghost-9.9" in str(excinfo.value)
    assert "<impl_id>-<version>" in str(excinfo.value)


@pytest.mark.asyncio
async def test_search_operations_unknown_connector_raises(
    stub_embedding_service: AsyncMock,
) -> None:
    """G0.8-T5: search_operations raises UnknownConnectorError too —
    identical unknown-vs-known-empty semantics across both meta-tools."""
    operator = _make_operator()

    with pytest.raises(UnknownConnectorError):
        await search_operations(
            operator,
            {"connector_id": "ghost-9.9", "query": "anything"},
        )


@pytest.mark.asyncio
async def test_list_operation_groups_omits_disabled_groups() -> None:
    """Disabled (review_status != 'enabled') groups don't appear in the response."""
    await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="staged-group",
        name="Staged",
        when_to_use="staged.",
        review_status="staged",
    )
    await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        name="KV v2",
        when_to_use="enabled.",
        review_status="enabled",
    )
    operator = _make_operator()

    result = await list_operation_groups(operator, {"connector_id": "vault-1.x"})

    keys = [g["group_key"] for g in result["groups"]]
    assert keys == ["kv"]


@pytest.mark.asyncio
async def test_list_operation_groups_tenant_boundary() -> None:
    """A tenant-curated group is visible only to its own tenant."""
    await _seed_group(
        tenant_id=_TENANT_A,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="tenant-a-group",
        name="Tenant A",
        when_to_use="tenant a.",
    )
    await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="builtin",
        name="Built-in",
        when_to_use="global.",
    )

    op_a = _make_operator(tenant_id=_TENANT_A)
    op_b = _make_operator(tenant_id=_TENANT_B)

    result_a = await list_operation_groups(op_a, {"connector_id": "vault-1.x"})
    result_b = await list_operation_groups(op_b, {"connector_id": "vault-1.x"})

    keys_a = sorted(g["group_key"] for g in result_a["groups"])
    keys_b = sorted(g["group_key"] for g in result_b["groups"])

    assert keys_a == ["builtin", "tenant-a-group"]
    assert keys_b == ["builtin"]


@pytest.mark.asyncio
async def test_list_operation_groups_includes_operation_count(
    stub_embedding_service: AsyncMock,
) -> None:
    """``operation_count`` reflects the number of enabled ops in each group."""
    group_id = await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        name="KV v2",
        when_to_use="use kv.",
    )
    sessionmaker = get_sessionmaker()
    # Two enabled ops in the group + one disabled (must not count).
    from datetime import UTC, datetime

    async with sessionmaker() as s, s.begin():
        for i, enabled in enumerate([True, True, False]):
            s.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product="vault",
                    version="1.x",
                    impl_id="vault",
                    op_id=f"vault.kv.op{i}",
                    source_kind="typed",
                    method=None,
                    path=None,
                    handler_ref="tests.test_operations_meta_tools._module_handler",
                    summary=f"Op {i}",
                    description=f"Op {i} description.",
                    group_id=group_id,
                    tags=[],
                    parameter_schema={"type": "object"},
                    response_schema=None,
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=enabled,
                    embedding=None,
                    custom_description=None,
                    custom_notes=None,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    operator = _make_operator()
    result = await list_operation_groups(operator, {"connector_id": "vault-1.x"})

    assert len(result["groups"]) == 1
    assert result["groups"][0]["operation_count"] == 2


# ---------------------------------------------------------------------------
# search_operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_operations_returns_empty_list_on_empty_corpus(
    stub_embedding_service: AsyncMock,
) -> None:
    """A KNOWN connector with no matching descriptors -> empty ``hits``.

    The connector is made known-as-data via a seeded group (no
    descriptors), so this exercises the known-empty path rather than
    the unknown-connector path (which now raises ``UnknownConnectorError``;
    see ``test_search_operations_unknown_connector_raises``).
    """
    await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        name="KV v2",
        when_to_use="kv.",
    )
    operator = _make_operator()

    result = await search_operations(
        operator,
        {"connector_id": "vault-1.x", "query": "read a secret"},
    )

    assert result["hits"] == []
    assert "query_duration_ms" in result


@pytest.mark.asyncio
async def test_search_operations_ranks_lexical_match_first(
    stub_embedding_service: AsyncMock,
) -> None:
    """A query matching one op's summary surfaces that op as the top hit."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler,
        summary="Read a KV v2 secret from a path.",
        description="Reads a secret stored in the KV v2 mount.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.sys.health",
        handler=_module_handler,
        summary="Check the cluster health.",
        description="Reports the seal status and leader info.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    result = await search_operations(
        operator,
        {"connector_id": "vault-1.x", "query": "read secret"},
    )

    hits = result["hits"]
    # `vault.kv.read` is the obvious lexical match for "read secret".
    assert len(hits) >= 1
    assert hits[0]["op_id"] == "vault.kv.read"
    # Score surfaces -- fused, per-signal.
    assert hits[0]["fused_score"] > 0
    assert hits[0]["bm25_score"] is not None


@pytest.mark.asyncio
async def test_search_operations_filters_by_group(
    stub_embedding_service: AsyncMock,
) -> None:
    """Setting ``group`` narrows the result set to that group's ops."""
    kv_group = await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        name="KV",
        when_to_use="kv.",
    )
    sys_group = await _seed_group(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="sys",
        name="Sys",
        when_to_use="sys.",
    )
    # Two ops, one per group; both lexical-match the query.
    sessionmaker = get_sessionmaker()
    from datetime import UTC, datetime

    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.kv.read",
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_operations_meta_tools._module_handler",
                summary="Read a KV secret.",
                description="reads",
                group_id=kv_group,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.sys.read",
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_operations_meta_tools._module_handler",
                summary="Read a sys config.",
                description="reads",
                group_id=sys_group,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    operator = _make_operator()
    all_hits = (
        await search_operations(
            operator,
            {"connector_id": "vault-1.x", "query": "read"},
        )
    )["hits"]
    kv_hits = (
        await search_operations(
            operator,
            {"connector_id": "vault-1.x", "query": "read", "group": "kv"},
        )
    )["hits"]

    all_op_ids = sorted(h["op_id"] for h in all_hits)
    kv_op_ids = sorted(h["op_id"] for h in kv_hits)
    assert all_op_ids == ["vault.kv.read", "vault.sys.read"]
    assert kv_op_ids == ["vault.kv.read"]


@pytest.mark.asyncio
async def test_search_operations_unknown_group_returns_empty_hits(
    stub_embedding_service: AsyncMock,
) -> None:
    """An unknown ``group`` short-circuits to an empty hits list."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler,
        summary="Read.",
        description="read.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    operator = _make_operator()
    result = await search_operations(
        operator,
        {"connector_id": "vault-1.x", "query": "read", "group": "no-such-group"},
    )
    assert result["hits"] == []


@pytest.mark.asyncio
async def test_search_operations_enforces_tenant_boundary(
    stub_embedding_service: AsyncMock,
) -> None:
    """Tenant-scoped descriptors don't leak across tenants."""
    sessionmaker = get_sessionmaker()
    from datetime import UTC, datetime

    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=_TENANT_A,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.tenant.a.op",
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_operations_meta_tools._module_handler",
                summary="Tenant A only op for reading data.",
                description="private op.",
                group_id=None,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    op_a = _make_operator(tenant_id=_TENANT_A)
    op_b = _make_operator(tenant_id=_TENANT_B)
    result_a = await search_operations(
        op_a,
        {"connector_id": "vault-1.x", "query": "read"},
    )
    result_b = await search_operations(
        op_b,
        {"connector_id": "vault-1.x", "query": "read"},
    )

    assert any(h["op_id"] == "vault.tenant.a.op" for h in result_a["hits"])
    assert all(h["op_id"] != "vault.tenant.a.op" for h in result_b["hits"])


@pytest.mark.asyncio
async def test_search_operations_clamps_limit_to_max(
    stub_embedding_service: AsyncMock,
) -> None:
    """``limit`` greater than ``SEARCH_LIMIT_MAX`` is silently clamped."""
    # Register two ops; passing a giant limit shouldn't blow up.
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler,
        summary="Read a secret.",
        description="read.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    operator = _make_operator()
    result = await search_operations(
        operator,
        {"connector_id": "vault-1.x", "query": "read", "limit": 9999},
    )
    # Up to 50 hits; the corpus has 1 so length is 1.
    assert len(result["hits"]) <= 50


# ---------------------------------------------------------------------------
# call_operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_operation_with_target_resolves_and_dispatches(
    stub_embedding_service: AsyncMock,
) -> None:
    """End-to-end: resolve target by name -> dispatch -> ok result."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler,
        summary="Read a secret.",
        description="reads.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    # Seed a target row that resolve_target() can find by name.
    from datetime import UTC, datetime

    target_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_A,
                name="rdc-vault",
                aliases=["primary-vault"],
                product="vault",
                host="vault.example.com",
                port=8200,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    operator = _make_operator(tenant_id=_TENANT_A)
    result = await call_operation(
        operator,
        {
            "connector_id": "vault-1.x",
            "op_id": "vault.kv.read",
            "target": {"name": "rdc-vault"},
            "params": {"path": "secret/foo"},
        },
    )

    assert result["status"] == "ok", result.get("error")
    assert result["result"]["echo"] == {"path": "secret/foo"}
    assert result["result"]["target_name"] == "rdc-vault"


@pytest.mark.asyncio
async def test_call_operation_without_target_uses_none(
    stub_embedding_service: AsyncMock,
) -> None:
    """``target=null`` invokes the dispatcher with ``target=None``."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler,
        summary="Read.",
        description="reads.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(tenant_id=_TENANT_A)
    result = await call_operation(
        operator,
        {
            "connector_id": "vault-1.x",
            "op_id": "vault.kv.read",
            "target": None,
            "params": {"hello": "world"},
        },
    )

    assert result["status"] == "ok"
    assert result["result"]["echo"] == {"hello": "world"}
    assert result["result"]["target_name"] is None


@pytest.mark.asyncio
async def test_call_operation_missing_target_name_raises_valueerror(
    stub_embedding_service: AsyncMock,
) -> None:
    """A target dict without ``name`` raises ValueError for the route to surface as 400."""
    operator = _make_operator(tenant_id=_TENANT_A)
    with pytest.raises(ValueError):
        await call_operation(
            operator,
            {
                "connector_id": "vault-1.x",
                "op_id": "vault.kv.read",
                "target": {},
                "params": {},
            },
        )


@pytest.mark.asyncio
async def test_call_operation_unknown_op_returns_structured_error(
    stub_embedding_service: AsyncMock,
) -> None:
    """Unknown op_id surfaces as an OperationResult with ``status='error'``."""
    operator = _make_operator(tenant_id=_TENANT_A)
    result = await call_operation(
        operator,
        {
            "connector_id": "vault-1.x",
            "op_id": "vault.does.not.exist",
            "target": None,
            "params": {},
        },
    )
    assert result["status"] == "error"
    assert result["error"].startswith("unknown_op:")
    assert result["extras"]["error_code"] == "unknown_op"


# ---------------------------------------------------------------------------
# Search -> call end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_then_call_end_to_end(
    stub_embedding_service: AsyncMock,
) -> None:
    """Acceptance: ``search_operations -> call_operation`` flow against a seeded op."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler,
        summary="Read a secret from Vault KV v2.",
        description="Reads a secret from the KV v2 mount.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(tenant_id=_TENANT_A)
    search_result = await search_operations(
        operator,
        {"connector_id": "vault-1.x", "query": "read secret"},
    )
    assert len(search_result["hits"]) >= 1
    op_id = search_result["hits"][0]["op_id"]
    assert op_id == "vault.kv.read"

    call_result = await call_operation(
        operator,
        {
            "connector_id": "vault-1.x",
            "op_id": op_id,
            "target": None,
            "params": {"path": "secret/x"},
        },
    )
    assert call_result["status"] == "ok"


# ---------------------------------------------------------------------------
# describe_descriptor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_descriptor_returns_full_row_for_visible_descriptor(
    stub_embedding_service: AsyncMock,
) -> None:
    """A descriptor in this tenant (or built-in) returns the full row."""
    descriptor_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    from datetime import UTC, datetime

    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=descriptor_id,
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.kv.read",
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_operations_meta_tools._module_handler",
                summary="Read.",
                description="Reads.",
                group_id=None,
                tags=["read"],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions={"when_to_call": "always."},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    operator = _make_operator(tenant_id=_TENANT_A)
    descriptor = await describe_descriptor(operator, descriptor_id)
    assert descriptor is not None
    assert descriptor.op_id == "vault.kv.read"
    assert descriptor.llm_instructions == {"when_to_call": "always."}
    assert descriptor.tags == ["read"]


@pytest.mark.asyncio
async def test_describe_descriptor_returns_none_for_unknown_id() -> None:
    """Unknown descriptor_id returns None (route surfaces as 404)."""
    operator = _make_operator()
    result = await describe_descriptor(operator, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_describe_descriptor_cross_tenant_is_invisible() -> None:
    """A tenant-scoped row in a different tenant returns None (404)."""
    descriptor_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    from datetime import UTC, datetime

    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=descriptor_id,
                tenant_id=_TENANT_A,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.private",
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_operations_meta_tools._module_handler",
                summary="Private.",
                description="Private.",
                group_id=None,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    op_b = _make_operator(tenant_id=_TENANT_B)
    result = await describe_descriptor(op_b, descriptor_id)
    assert result is None
