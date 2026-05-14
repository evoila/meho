# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.ingest.register_ingested`.

Coverage matrix (G0.7-T2 / Task #403 acceptance criteria):

* :func:`register_ingested_operations` first-call path -- inserts a
  row per operation with ``source_kind='ingested'``,
  ``handler_ref=None``, ``is_enabled=False``, ``method`` + ``path``
  populated from the proto, every other field carried verbatim.
* :class:`IngestionResult` shape -- inserted / updated / skipped
  counts + ``connector_registered`` + ``operations_grouped`` flags.
* Body-hash skip semantics -- second call with identical operations
  is a no-op for the embedding pipeline (assertion: stub mock
  ``encode_one.call_count`` does not advance on the second batch).
* Multi-spec merge -- two ingests with distinct ``spec_source``
  under the same connector_id produce rows whose ``tags`` are
  distinguishable via the ``"spec:<source>"`` marker.
* :exc:`OpIdCollision` -- ingesting a batch with two ops sharing
  ``op_id`` raises before any row is persisted; the exception
  names both colliding op_ids.
* Connector class auto-registration on first ingest -- the v2
  registry now contains an entry for the
  ``(product, version, impl_id)`` triple, of a class that subclasses
  :class:`GenericRestConnector`.
* Subsequent ingest under the same connector_id leaves the existing
  v2 registry entry intact (``connector_registered=False`` on the
  :class:`IngestionResult`).
* Embeddings -- the 384-dim vector returned by the mocked service
  is what ends up on the row.
* Tenant scoping -- ``tenant_id=None`` (default) writes built-in
  rows; ``tenant_id=<uuid>`` writes tenant-scoped rows; the two
  coexist without colliding.
* Operations land in ``staged``-equivalent state: ``is_enabled=False``
  on every per-op row (the parent group is not created by this
  helper -- T3 #404 runs grouping).

The embedding service is mocked via the explicit
``embedding_service=`` parameter so tests don't pull fastembed or
ONNX runtime; the mock's ``call_count`` is what proves the
skip-re-embed branch is being exercised on idempotent re-calls.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import all_connectors_v2, clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    GenericRestConnector,
    IngestionResult,
    OpIdCollision,
    parse_openapi,
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
def _clear_connector_registry() -> Iterator[None]:
    """Reset the connector registry between tests.

    Each test that exercises auto-registration adds entries; without
    a per-test reset, later tests would either collide with prior
    entries or see ``connector_registered=False`` from the start.
    """
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """An :class:`AsyncMock` standing in for :class:`EmbeddingService`."""
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _proto(
    op_id: str,
    *,
    method: str = "GET",
    path: str | None = None,
    summary: str = "Summary",
    description: str = "Description",
    tags: list[str] | None = None,
    safety_level: str = "safe",
    requires_approval: bool = False,
) -> EndpointDescriptorProto:
    """Construct a parser-shaped proto for tests.

    Mirrors the shape :func:`parse_openapi` produces: ``op_id`` =
    ``f"{method}:{path}"``, ``method`` upper-cased, ``parameter_schema``
    populated with a minimal JSON Schema 2020-12 object.
    """
    return EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path or f"/{op_id.split(':', 1)[1]}",
        summary=summary,
        description=description,
        tags=tags or ["pets"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema={"type": "object"},
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=requires_approval,
    )


# ---------------------------------------------------------------------------
# IngestionResult + first-call path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_call_inserts_rows_with_ingested_source_kind(
    stub_embedding_service: AsyncMock,
) -> None:
    """First call inserts each proto as an ``is_enabled=False`` ingested row."""
    operations = [
        _proto("GET:/pets", path="/pets", summary="List pets"),
        _proto("POST:/pets", method="POST", path="/pets", summary="Add a pet"),
    ]

    result = await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=operations,
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
                    .where(EndpointDescriptor.product == "petstore")
                    .order_by(EndpointDescriptor.op_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 2
    for row in rows:
        assert row.tenant_id is None
        assert row.product == "petstore"
        assert row.version == "1.0"
        assert row.impl_id == "petstore-rest"
        assert row.source_kind == "ingested"
        assert row.handler_ref is None
        assert row.is_enabled is False
        assert row.embedding == [0.25] * 384
        assert "spec:petstore.yaml" in row.tags
        # Original tag preserved alongside the spec_source marker.
        assert "pets" in row.tags
        # group_id stays NULL -- T3 #404 runs grouping next.
        assert row.group_id is None


@pytest.mark.asyncio
async def test_first_call_persists_method_and_path(
    stub_embedding_service: AsyncMock,
) -> None:
    """Ingested rows preserve ``method`` + ``path`` (typed rows leave both NULL)."""
    operations = [_proto("GET:/api/vcenter/cluster", path="/api/vcenter/cluster")]

    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=operations,
        embedding_service=stub_embedding_service,
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
    assert row.method == "GET"
    assert row.path == "/api/vcenter/cluster"
    assert row.handler_ref is None


# ---------------------------------------------------------------------------
# Body-hash skip path (idempotency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_call_with_identical_ops_skips_reembed(
    stub_embedding_service: AsyncMock,
) -> None:
    """Re-running with the same args is a no-op for the embedding pipeline.

    The load-bearing assertion: re-ingesting an unchanged spec must
    not re-embed every operation. The skip-re-embed path matches
    the typed-register precedent and is the operationally critical
    path on connector init / spec re-ingest at scale.
    """
    operations = [
        _proto("GET:/pets", path="/pets", summary="List pets"),
        _proto("POST:/pets", method="POST", path="/pets", summary="Add a pet"),
    ]
    kwargs: dict[str, Any] = {
        "product": "petstore",
        "version": "1.0",
        "impl_id": "petstore-rest",
        "spec_source": "petstore.yaml",
        "operations": operations,
        "embedding_service": stub_embedding_service,
    }
    first = await register_ingested_operations(**kwargs)
    assert first.inserted_count == 2
    assert first.connector_registered is True
    assert stub_embedding_service.encode_one.call_count == 2

    second = await register_ingested_operations(**kwargs)
    assert second.inserted_count == 0
    assert second.updated_count == 0
    assert second.skipped_count == 2
    assert second.connector_registered is False, (
        "Second ingest must not re-register the connector class"
    )
    assert stub_embedding_service.encode_one.call_count == 2, (
        "Embedding service must not be invoked when every row's embedding text is unchanged"
    )


@pytest.mark.asyncio
async def test_second_call_with_changed_summary_triggers_reembed(
    stub_embedding_service: AsyncMock,
) -> None:
    """Changing ``summary`` on a re-ingest re-embeds that row only."""
    op_v1 = _proto("GET:/pets", path="/pets", summary="List pets")
    op_v2 = _proto("GET:/pets", path="/pets", summary="List pets with pagination")

    await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[op_v1],
        embedding_service=stub_embedding_service,
    )
    assert stub_embedding_service.encode_one.call_count == 1

    result = await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[op_v2],
        embedding_service=stub_embedding_service,
    )
    assert result.updated_count == 1
    assert result.skipped_count == 0
    assert stub_embedding_service.encode_one.call_count == 2


# ---------------------------------------------------------------------------
# Multi-spec merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_spec_merge_tags_rows_distinctly(
    stub_embedding_service: AsyncMock,
) -> None:
    """Two specs under one connector_id produce rows tagged by spec_source.

    Models vSphere's vcenter.yaml + vi-json.yaml merge: both go under
    ``connector_id="vmware-rest-9.0"`` and the operator can grep for
    ``spec:vcenter.yaml`` vs ``spec:vi-json.yaml`` in the row tags
    to know which spec contributed which op.
    """
    spec_a_ops = [
        _proto("GET:/api/vcenter/cluster", path="/api/vcenter/cluster"),
    ]
    spec_b_ops = [
        _proto(
            "POST:/ClusterComputeResource/{moId}/Method",
            method="POST",
            path="/ClusterComputeResource/{moId}/Method",
        ),
    ]

    result_a = await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=spec_a_ops,
        embedding_service=stub_embedding_service,
    )
    assert result_a.connector_registered is True

    result_b = await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vi-json.yaml",
        operations=spec_b_ops,
        embedding_service=stub_embedding_service,
    )
    # Connector class already registered on the first call.
    assert result_b.connector_registered is False

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
    tag_by_op_id = {row.op_id: row.tags for row in rows}
    assert "spec:vcenter.yaml" in tag_by_op_id["GET:/api/vcenter/cluster"]
    assert "spec:vi-json.yaml" in tag_by_op_id["POST:/ClusterComputeResource/{moId}/Method"]


# ---------------------------------------------------------------------------
# OpIdCollision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_op_id_collision_within_batch_raises(
    stub_embedding_service: AsyncMock,
) -> None:
    """Two ops sharing an ``op_id`` in one batch raise :exc:`OpIdCollision`."""
    operations = [
        _proto("GET:/pets", path="/pets", summary="First list-pets"),
        _proto("GET:/pets", path="/pets", summary="Conflicting list-pets"),
    ]
    with pytest.raises(OpIdCollision) as excinfo:
        await register_ingested_operations(
            product="petstore",
            version="1.0",
            impl_id="petstore-rest",
            spec_source="petstore.yaml",
            operations=operations,
            embedding_service=stub_embedding_service,
        )
    assert excinfo.value.op_ids == ["GET:/pets"]
    assert excinfo.value.product == "petstore"
    assert excinfo.value.version == "1.0"
    assert excinfo.value.impl_id == "petstore-rest"
    assert "GET:/pets" in str(excinfo.value)


@pytest.mark.asyncio
async def test_op_id_collision_lists_all_duplicates(
    stub_embedding_service: AsyncMock,
) -> None:
    """``OpIdCollision`` names every distinct duplicate, not just the first."""
    operations = [
        _proto("GET:/pets", path="/pets"),
        _proto("GET:/pets", path="/pets"),
        _proto("GET:/owners", path="/owners"),
        _proto("GET:/owners", path="/owners"),
    ]
    with pytest.raises(OpIdCollision) as excinfo:
        await register_ingested_operations(
            product="petstore",
            version="1.0",
            impl_id="petstore-rest",
            spec_source="petstore.yaml",
            operations=operations,
            embedding_service=stub_embedding_service,
        )
    assert excinfo.value.op_ids == ["GET:/owners", "GET:/pets"]


@pytest.mark.asyncio
async def test_op_id_collision_raised_before_any_db_write(
    stub_embedding_service: AsyncMock,
) -> None:
    """Collision detection runs before the upsert loop -- no partial state."""
    operations = [
        _proto("GET:/pets", path="/pets", summary="ok"),
        _proto("GET:/pets", path="/pets", summary="duplicate"),
        _proto("GET:/owners", path="/owners"),
    ]
    with pytest.raises(OpIdCollision):
        await register_ingested_operations(
            product="petstore",
            version="1.0",
            impl_id="petstore-rest",
            spec_source="petstore.yaml",
            operations=operations,
            embedding_service=stub_embedding_service,
        )
    # No rows persisted.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "petstore")
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


# ---------------------------------------------------------------------------
# Connector class auto-registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_ingest_registers_connector_class(
    stub_embedding_service: AsyncMock,
) -> None:
    """First ingest of a triple auto-registers a :class:`GenericRestConnector` shim."""
    assert (("petstore", "1.0", "petstore-rest")) not in all_connectors_v2()

    result = await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[_proto("GET:/pets", path="/pets")],
        base_url="https://petstore.example.com",
        embedding_service=stub_embedding_service,
    )
    assert result.connector_registered is True

    registry = all_connectors_v2()
    key = ("petstore", "1.0", "petstore-rest")
    assert key in registry
    cls = registry[key]
    assert issubclass(cls, GenericRestConnector)
    assert cls.product == "petstore"
    assert cls.version == "1.0"
    assert cls.impl_id == "petstore-rest"
    # Conservative default: ">=1.0,<2.0" for a "1.0" version.
    assert cls.supported_version_range == ">=1.0,<2.0"
    assert cls._base_url_override == "https://petstore.example.com"


@pytest.mark.asyncio
async def test_second_ingest_does_not_re_register_connector_class(
    stub_embedding_service: AsyncMock,
) -> None:
    """Second ingest under the same triple skips connector class registration."""
    common_kwargs: dict[str, Any] = {
        "product": "petstore",
        "version": "1.0",
        "impl_id": "petstore-rest",
        "operations": [_proto("GET:/pets", path="/pets")],
        "embedding_service": stub_embedding_service,
    }
    first = await register_ingested_operations(spec_source="petstore.yaml", **common_kwargs)
    assert first.connector_registered is True
    cls_first = all_connectors_v2()[("petstore", "1.0", "petstore-rest")]

    # Second ingest of a different spec under the same connector.
    second = await register_ingested_operations(
        spec_source="petstore-admin.yaml",
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        operations=[_proto("DELETE:/pets/{petId}", method="DELETE", path="/pets/{petId}")],
        embedding_service=stub_embedding_service,
    )
    assert second.connector_registered is False
    # The registered class is the SAME object (no re-synthesis).
    cls_second = all_connectors_v2()[("petstore", "1.0", "petstore-rest")]
    assert cls_first is cls_second


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_scoped_rows_coexist_with_built_in(
    stub_embedding_service: AsyncMock,
) -> None:
    """Built-in and tenant-scoped rows with the same op_id coexist by partial index."""
    tenant_id = uuid.uuid4()
    op = _proto("GET:/pets", path="/pets")

    await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[op],
        tenant_id=None,
        embedding_service=stub_embedding_service,
    )
    await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=[op],
        tenant_id=tenant_id,
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id == "GET:/pets")
                )
            )
            .scalars()
            .all()
        )
    tenant_ids = {row.tenant_id for row in rows}
    assert tenant_ids == {None, tenant_id}


# ---------------------------------------------------------------------------
# Integration with parser (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_with_real_parsed_spec(
    stub_embedding_service: AsyncMock,
) -> None:
    """Parse the petstore fixture, register, assert rows + connector registered.

    Smoke test against the real T1 parser output -- the helper must
    accept :class:`EndpointDescriptorProto` produced by
    :func:`parse_openapi` without any shape massaging at the call
    site.
    """
    operations = parse_openapi("tests/fixtures/openapi/petstore_30.yaml")

    result = await register_ingested_operations(
        product="petstore",
        version="3.0",
        impl_id="petstore-rest",
        spec_source="petstore_30.yaml",
        operations=operations,
        base_url="https://petstore.example.com",
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == len(operations) > 0
    assert result.connector_registered is True

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "petstore")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == len(operations)
    for row in rows:
        assert row.source_kind == "ingested"
        assert row.is_enabled is False
        assert row.method is not None
        assert row.path is not None
        assert "spec:petstore_30.yaml" in row.tags
        assert row.embedding == [0.25] * 384

    # Connector resolvable in the v2 registry.
    assert ("petstore", "3.0", "petstore-rest") in all_connectors_v2()


# ---------------------------------------------------------------------------
# Caller-owned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caller_owned_session_defers_commit(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """When *session* is passed, the helper does not commit -- caller controls boundaries."""
    operations = [_proto("GET:/pets", path="/pets")]
    result = await register_ingested_operations(
        product="petstore",
        version="1.0",
        impl_id="petstore-rest",
        spec_source="petstore.yaml",
        operations=operations,
        session=session,
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == 1

    # Rows visible in the SAME session before commit.
    in_session = (
        await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "GET:/pets")
        )
    ).scalar_one_or_none()
    assert in_session is not None

    # Rollback drops everything -- the helper did not commit.
    await session.rollback()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        post_rollback = (
            await fresh.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "GET:/pets")
            )
        ).scalar_one_or_none()
    assert post_rollback is None
