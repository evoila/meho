# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.ingest.register_ingested`.

Coverage matrix (G0.7-T2 / Task #403 acceptance criteria):

* First-call insert path: every parsed proto lands as a new
  :class:`EndpointDescriptor` row with ``source_kind='ingested'``,
  ``is_enabled=False`` (staged), ``group_id IS NULL``, ``tags``
  carrying the ``spec:<source>`` marker, embedding computed once.
* Body-hash skip: re-running with identical parser output produces
  zero embedding service calls + the result counts ``skipped`` per
  op.
* Re-run with changed summary/description triggers re-embed and
  counts ``updated`` per op.
* Re-run with changed parameter_schema (non-embedding-text field)
  does NOT trigger re-embed; counts ``skipped``; the parameter
  schema is updated on the row.
* Multi-spec merge: ingesting a second spec under the same
  ``(product, version, impl_id)`` with disjoint op-ids produces
  rows tagged distinctly via ``spec:<source>``.
* Op-id collision detected: ingesting two specs with overlapping
  op-ids raises :class:`OpIdCollision`; no rows are written from
  the failing call.
* Connector class auto-registration on first ingest: the v2
  registry contains a :class:`HttpConnector` subclass keyed on the
  triple; :attr:`IngestionResult.connector_registered` is ``True``.
* Second ingest under the same triple does NOT re-register;
  ``connector_registered=False``.
* Tenant scoping: ``tenant_id`` argument routes rows to the
  matching partial-unique index; built-in (``None``) and
  tenant-scoped rows can coexist for the same op-id without
  collision.
* :class:`IngestionResult` shape: counts + flags as documented.
* Caller-owned session: helper does not commit; caller controls
  transaction boundaries.

The embedding service is mocked via the explicit
``embedding_service=`` parameter so tests don't pull fastembed or
ONNX runtime, and the call count assertion is what proves the
skip-re-embed branch.

A fresh in-memory registry is installed for each test via the
``_reset_connector_registry`` autouse fixture so connector
auto-registration assertions don't leak across tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    IngestionResult,
    OpIdCollision,
    ensure_connector_class_registered,
    register_ingested_operations,
)
from meho_backplane.settings import get_settings


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
def _reset_connector_registry() -> Iterator[None]:
    """Empty the connector registry around every test.

    The v2 registry is a module-level dict; auto-registrations from
    one test leaking into another would make the
    ``connector_registered=True`` assertion flaky. Pairs with the
    documented test-only ``clear_registry`` escape hatch.
    """
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic 384-dim embedding mock; call count proves skip-re-embed."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """An :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _proto(
    *,
    op_id: str = "GET:/api/vcenter/cluster",
    method: str = "GET",
    path: str = "/api/vcenter/cluster",
    summary: str | None = "List clusters",
    description: str | None = "Return every vCenter cluster.",
    tags: list[str] | None = None,
    parameter_schema: dict[str, Any] | None = None,
    response_schema: dict[str, Any] | None = None,
    safety_level: str = "safe",
    requires_approval: bool = False,
) -> EndpointDescriptorProto:
    """Build an :class:`EndpointDescriptorProto` with defaults filled in."""
    return EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path,
        summary=summary,
        description=description,
        tags=tags if tags is not None else [],
        parameter_schema=(
            parameter_schema
            if parameter_schema is not None
            else {"type": "object", "properties": {}}
        ),
        response_schema=response_schema,
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=requires_approval,
    )


# ---------------------------------------------------------------------------
# First-call insert path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_first_call_inserts_descriptor_in_staged_state(
    stub_embedding_service: AsyncMock,
) -> None:
    """First call inserts every proto as a staged row with the spec tag."""
    protos = [
        _proto(
            op_id="GET:/api/vcenter/cluster",
            method="GET",
            path="/api/vcenter/cluster",
            tags=["cluster"],
        ),
        _proto(
            op_id="DELETE:/api/vcenter/vm/{vm_id}",
            method="DELETE",
            path="/api/vcenter/vm/{vm_id}",
            summary="Delete a VM",
            description="Permanently delete the specified virtual machine.",
            tags=["vm-lifecycle"],
            safety_level="dangerous",
        ),
    ]

    result = await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=protos,
        embedding_service=stub_embedding_service,
    )

    assert isinstance(result, IngestionResult)
    assert result.inserted_count == 2
    assert result.updated_count == 0
    assert result.skipped_count == 0
    assert result.connector_registered is True
    assert result.operations_grouped is False
    assert stub_embedding_service.encode_one.call_count == 2

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor)
                    .where(EndpointDescriptor.product == "vmware")
                    .order_by(EndpointDescriptor.op_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 2
    cluster = next(row for row in rows if row.op_id == "GET:/api/vcenter/cluster")
    delete_vm = next(row for row in rows if row.op_id == "DELETE:/api/vcenter/vm/{vm_id}")
    assert cluster.tenant_id is None
    assert cluster.source_kind == "ingested"
    assert cluster.method == "GET"
    assert cluster.path == "/api/vcenter/cluster"
    assert cluster.handler_ref is None
    assert cluster.is_enabled is False
    assert cluster.group_id is None
    assert cluster.safety_level == "safe"
    assert "cluster" in cluster.tags
    assert "spec:vcenter.yaml" in cluster.tags
    assert cluster.embedding == [0.1] * 384
    assert cluster.custom_description is None

    assert delete_vm.safety_level == "dangerous"
    assert "spec:vcenter.yaml" in delete_vm.tags


@pytest.mark.asyncio
async def test_register_first_call_registers_http_connector_shim(
    stub_embedding_service: AsyncMock,
) -> None:
    """First ingestion against a triple registers a HttpConnector subclass."""
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto()],
        embedding_service=stub_embedding_service,
    )

    registry = all_connectors_v2()
    key = ("vmware", "9.0", "vmware-rest")
    assert key in registry
    cls = registry[key]
    assert issubclass(cls, HttpConnector)
    assert cls.product == "vmware"
    assert cls.version == "9.0"
    assert cls.impl_id == "vmware-rest"
    # Derived range: same major.minor compatibility window.
    assert cls.supported_version_range == ">=9.0,<10.0"


@pytest.mark.asyncio
async def test_register_second_call_does_not_re_register_connector(
    stub_embedding_service: AsyncMock,
) -> None:
    """Subsequent ingestions against the same triple report connector_registered=False."""
    first = await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto(op_id="GET:/a")],
        embedding_service=stub_embedding_service,
    )
    second = await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto(op_id="GET:/b")],
        embedding_service=stub_embedding_service,
    )
    assert first.connector_registered is True
    assert second.connector_registered is False


# ---------------------------------------------------------------------------
# Idempotency — body-hash skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_same_args_twice_skips_reembed(
    stub_embedding_service: AsyncMock,
) -> None:
    """Re-call with identical parser output produces one row + one embed call."""
    protos = [_proto(op_id="GET:/api/vcenter/cluster", tags=["cluster"])]
    args: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "spec_source": "vcenter.yaml",
        "operations": protos,
        "embedding_service": stub_embedding_service,
    }

    first = await register_ingested_operations(**args)
    assert first.inserted_count == 1 and first.skipped_count == 0
    assert stub_embedding_service.encode_one.call_count == 1

    second = await register_ingested_operations(**args)
    assert second.inserted_count == 0
    assert second.updated_count == 0
    assert second.skipped_count == 1
    assert stub_embedding_service.encode_one.call_count == 1, (
        "Embedding service must not be invoked when parser output is unchanged"
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == "GET:/api/vcenter/cluster"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("summary", "Updated summary"),
        ("description", "Updated description body."),
    ],
)
@pytest.mark.asyncio
async def test_register_changed_embedding_text_triggers_reembed(
    stub_embedding_service: AsyncMock,
    field: str,
    new_value: Any,
) -> None:
    """Changing summary/description fields triggers a re-embed."""
    baseline = _proto(op_id="GET:/api/vcenter/cluster", tags=["cluster"])
    base_args: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "spec_source": "vcenter.yaml",
        "embedding_service": stub_embedding_service,
    }
    await register_ingested_operations(operations=[baseline], **base_args)
    assert stub_embedding_service.encode_one.call_count == 1

    updated = baseline.model_copy(update={field: new_value})
    result = await register_ingested_operations(
        operations=[updated],
        **base_args,
    )
    assert result.updated_count == 1
    assert result.skipped_count == 0
    assert stub_embedding_service.encode_one.call_count == 2, (
        f"Changing {field!r} must trigger a re-embed"
    )


@pytest.mark.asyncio
async def test_register_changed_parameter_schema_does_not_reembed(
    stub_embedding_service: AsyncMock,
) -> None:
    """Changing non-embedding-text fields updates the row without re-embed."""
    baseline = _proto(
        op_id="GET:/api/vcenter/cluster",
        parameter_schema={"type": "object", "properties": {}},
        tags=["cluster"],
    )
    base_args: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "spec_source": "vcenter.yaml",
        "embedding_service": stub_embedding_service,
    }
    await register_ingested_operations(operations=[baseline], **base_args)
    assert stub_embedding_service.encode_one.call_count == 1

    updated = baseline.model_copy(
        update={
            "parameter_schema": {
                "type": "object",
                "properties": {"filter": {"type": "string"}},
            }
        }
    )
    result = await register_ingested_operations(
        operations=[updated],
        **base_args,
    )
    assert result.skipped_count == 1
    assert result.updated_count == 0
    assert stub_embedding_service.encode_one.call_count == 1, (
        "Changing parameter_schema must not trigger a re-embed"
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "GET:/api/vcenter/cluster"
                )
            )
        ).scalar_one()
    assert row.parameter_schema["properties"] == {"filter": {"type": "string"}}


# ---------------------------------------------------------------------------
# Multi-spec merge + collision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_multi_spec_merge_tags_per_spec(
    stub_embedding_service: AsyncMock,
) -> None:
    """Two spec ingestions under one connector tag rows distinctly via spec:*."""
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[
            _proto(
                op_id="GET:/api/vcenter/cluster",
                method="GET",
                path="/api/vcenter/cluster",
                tags=["cluster"],
            ),
        ],
        embedding_service=stub_embedding_service,
    )
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vi-json.yaml",
        operations=[
            _proto(
                op_id="POST:/ClusterComputeResource/{moId}/Method",
                method="POST",
                path="/ClusterComputeResource/{moId}/Method",
                summary="Invoke a method",
                description="Invoke a managed-object method.",
                tags=["cluster"],
                safety_level="caution",
            ),
        ],
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor)
                    .where(EndpointDescriptor.product == "vmware")
                    .order_by(EndpointDescriptor.op_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 2
    cluster = next(row for row in rows if row.op_id == "GET:/api/vcenter/cluster")
    method = next(row for row in rows if row.op_id == "POST:/ClusterComputeResource/{moId}/Method")
    assert "spec:vcenter.yaml" in cluster.tags
    assert "spec:vi-json.yaml" not in cluster.tags
    assert "spec:vi-json.yaml" in method.tags
    assert "spec:vcenter.yaml" not in method.tags


@pytest.mark.asyncio
async def test_register_op_id_collision_across_specs_raises(
    stub_embedding_service: AsyncMock,
) -> None:
    """Ingesting the same op-id under a different spec_source raises OpIdCollision."""
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[
            _proto(op_id="GET:/api/vcenter/cluster", tags=["cluster"]),
            _proto(
                op_id="GET:/api/vcenter/host",
                method="GET",
                path="/api/vcenter/host",
                summary="List hosts",
                description="Return every vCenter host.",
                tags=["host"],
            ),
        ],
        embedding_service=stub_embedding_service,
    )

    with pytest.raises(OpIdCollision) as excinfo:
        await register_ingested_operations(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            spec_source="vi-json.yaml",
            operations=[
                _proto(op_id="GET:/api/vcenter/cluster", tags=[]),
                _proto(
                    op_id="GET:/api/vcenter/host",
                    method="GET",
                    path="/api/vcenter/host",
                    summary="List hosts",
                    description="Return every vCenter host.",
                    tags=[],
                ),
            ],
            embedding_service=stub_embedding_service,
        )

    error = excinfo.value
    assert error.incoming_spec_source == "vi-json.yaml"
    assert "GET:/api/vcenter/cluster" in error.colliding_op_ids
    assert "GET:/api/vcenter/host" in error.colliding_op_ids
    assert error.existing_spec_sources["GET:/api/vcenter/cluster"] == "vcenter.yaml"

    # No new rows from the failed call: still exactly the two from
    # the first successful ingestion.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "vmware")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    for row in rows:
        assert "spec:vcenter.yaml" in row.tags
        assert "spec:vi-json.yaml" not in row.tags


# ---------------------------------------------------------------------------
# Connector class auto-registration unit test
# ---------------------------------------------------------------------------


def test_ensure_connector_class_registered_first_call_returns_true() -> None:
    """Direct call to the helper registers a shim and reports True."""
    registered = ensure_connector_class_registered(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        base_url="https://acme.test",
    )
    assert registered is True
    cls = all_connectors_v2()[("acme", "1.0", "acme-rest")]
    assert issubclass(cls, HttpConnector)
    assert cls.product == "acme"
    assert cls.supported_version_range == ">=1.0,<2.0"


def test_ensure_connector_class_registered_idempotent() -> None:
    """Second call against the same triple does NOT re-register."""
    ensure_connector_class_registered(product="acme", version="1.0", impl_id="acme-rest")
    second = ensure_connector_class_registered(product="acme", version="1.0", impl_id="acme-rest")
    assert second is False


def test_ensure_connector_class_non_numeric_version_no_range() -> None:
    """Non-numeric version strings fall back to ``supported_version_range=None``."""
    ensure_connector_class_registered(product="acme", version="latest", impl_id="acme-rest")
    cls = all_connectors_v2()[("acme", "latest", "acme-rest")]
    assert cls.supported_version_range is None


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_spec_source", ["", "   ", "\t"])
@pytest.mark.asyncio
async def test_register_rejects_invalid_spec_source(
    stub_embedding_service: AsyncMock,
    bad_spec_source: str,
) -> None:
    """Empty / whitespace ``spec_source`` raises :class:`ValueError`."""
    with pytest.raises(ValueError, match="spec_source"):
        await register_ingested_operations(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            spec_source=bad_spec_source,
            operations=[_proto()],
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_rejects_empty_operations_list(
    stub_embedding_service: AsyncMock,
) -> None:
    """Empty operations list raises :class:`ValueError`."""
    with pytest.raises(ValueError, match="empty"):
        await register_ingested_operations(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            spec_source="vcenter.yaml",
            operations=[],
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_rejects_duplicate_op_ids_in_batch(
    stub_embedding_service: AsyncMock,
) -> None:
    """Duplicate ``op_id`` values within a single batch raise :class:`ValueError`."""
    with pytest.raises(ValueError, match="duplicate"):
        await register_ingested_operations(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            spec_source="vcenter.yaml",
            operations=[
                _proto(op_id="GET:/api/vcenter/cluster"),
                _proto(op_id="GET:/api/vcenter/cluster"),
            ],
            embedding_service=stub_embedding_service,
        )


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_tenant_scoped_rows_isolated_from_builtin(
    stub_embedding_service: AsyncMock,
) -> None:
    """Tenant-scoped and built-in rows can share an op-id without colliding."""
    tenant_id = uuid.uuid4()
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto(op_id="GET:/api/vcenter/cluster", tags=["cluster"])],
        embedding_service=stub_embedding_service,
    )
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto(op_id="GET:/api/vcenter/cluster", tags=["cluster"])],
        tenant_id=tenant_id,
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == "GET:/api/vcenter/cluster"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    by_scope = {row.tenant_id: row for row in rows}
    assert None in by_scope and tenant_id in by_scope


# ---------------------------------------------------------------------------
# Caller-owned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_caller_session_does_not_commit(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """When the caller passes a session, the helper does not commit."""
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto()],
        session=session,
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.product == "vmware")
        )
        assert result.scalar_one_or_none() is None

    await session.commit()
    async with sessionmaker() as fresh_after:
        result = await fresh_after.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.product == "vmware")
        )
        assert result.scalar_one() is not None


@pytest.mark.asyncio
async def test_register_caller_session_rollback_discards_inserts(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Caller rollback after the helper returns discards every inserted row."""
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto()],
        session=session,
        embedding_service=stub_embedding_service,
    )

    await session.rollback()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.product == "vmware")
        )
        assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# 50-op integration smoke (acceptance: ingest a fixture spec; assert row count
# + staged state + connector registered)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_50_op_batch_lands_staged_and_registered(
    stub_embedding_service: AsyncMock,
) -> None:
    """A 50-op batch lands as 50 staged rows; the connector class is registered."""
    protos = [
        _proto(
            op_id=f"GET:/api/v1/resource/{i}",
            method="GET",
            path=f"/api/v1/resource/{i}",
            summary=f"Get resource {i}",
            description=f"Fetch resource number {i}.",
            tags=["resource"],
        )
        for i in range(50)
    ]
    result = await register_ingested_operations(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        spec_source="acme.yaml",
        operations=protos,
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == 50
    assert result.connector_registered is True
    assert stub_embedding_service.encode_one.call_count == 50

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "acme")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 50
    assert all(row.is_enabled is False for row in rows)
    assert all(row.group_id is None for row in rows)
    assert all(row.source_kind == "ingested" for row in rows)
    assert ("acme", "1.0", "acme-rest") in all_connectors_v2()
