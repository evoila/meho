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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog.testing
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import connector_exists, parse_connector_id
from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    GenericRestConnector,
    IngestionPipelineService,
    IngestionResult,
    OpIdCollision,
    ProductImplIdMismatch,
    SpecSource,
    UncoveredVersionLabel,
    check_version_covered_by_registered_class,
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


@pytest.mark.asyncio
async def test_op_id_collision_across_calls_with_different_spec_sources_raises(
    stub_embedding_service: AsyncMock,
) -> None:
    """A second ingest under the same triple with a different ``spec_source``
    sharing an ``op_id`` raises :exc:`OpIdCollision` instead of silently
    overwriting the first row.

    This is the cross-call branch the Task #403 body calls out: *"the
    second call to ``register_ingested_operations()`` UPDATEs the row
    ... T2 detects this and raises ``OpIdCollision``"*. The within-batch
    set scan in ``_detect_op_id_collisions`` can't see across calls; the
    detection has to live in the per-row upsert path against the
    persisted ``spec:<src>`` marker.
    """
    common_kwargs: dict[str, Any] = {
        "product": "petstore",
        "version": "1.0",
        "impl_id": "petstore-rest",
        "embedding_service": stub_embedding_service,
    }
    first_result = await register_ingested_operations(
        spec_source="petstore.yaml",
        operations=[_proto("GET:/pets", path="/pets", summary="First spec list-pets")],
        **common_kwargs,
    )
    assert first_result.inserted_count == 1

    # Snapshot the persisted row so the post-collision assertion can
    # prove the original payload survived unchanged.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        original_row = (
            await fresh.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "GET:/pets")
            )
        ).scalar_one()
        original_summary = original_row.summary
        original_tags = list(original_row.tags or [])
        original_updated_at = original_row.updated_at

    # Track embedding-service call count: the cross-call raise must
    # fire BEFORE the re-embed branch, so the embedding service is
    # not invoked a second time.
    encode_calls_before = stub_embedding_service.encode_one.call_count

    with pytest.raises(OpIdCollision) as excinfo:
        await register_ingested_operations(
            spec_source="petstore-admin.yaml",
            operations=[
                _proto("GET:/pets", path="/pets", summary="Conflicting admin list-pets"),
            ],
            **common_kwargs,
        )
    assert excinfo.value.op_ids == ["GET:/pets"]
    assert excinfo.value.product == "petstore"
    assert excinfo.value.version == "1.0"
    assert excinfo.value.impl_id == "petstore-rest"
    assert excinfo.value.existing_spec_source == "petstore.yaml"
    assert excinfo.value.incoming_spec_source == "petstore-admin.yaml"
    # Both spec sources are named in the rendered message so the
    # operator-facing CLI / API surfaces the disambiguation without
    # extra threading.
    msg = str(excinfo.value)
    assert "petstore.yaml" in msg
    assert "petstore-admin.yaml" in msg

    # No second encode call: the raise short-circuits the re-embed path.
    assert stub_embedding_service.encode_one.call_count == encode_calls_before

    # The original row is unchanged -- the second spec's payload did
    # not overwrite the first spec's summary / tags / updated_at.
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
    assert len(rows) == 1
    surviving = rows[0]
    assert surviving.summary == original_summary
    assert list(surviving.tags or []) == original_tags
    assert "spec:petstore.yaml" in (surviving.tags or [])
    assert "spec:petstore-admin.yaml" not in (surviving.tags or [])
    assert surviving.updated_at == original_updated_at


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
    import socket
    from unittest.mock import patch

    _fixtures = Path(__file__).parent / "fixtures" / "openapi"
    _petstore_30_bytes = (_fixtures / "petstore_30.yaml").read_bytes()
    _petstore_url = "https://specs.example.test/petstore_30.yaml"
    # Patch getaddrinfo so the SSRF guard resolves specs.example.test to a
    # public IP without a real DNS lookup; then mock the HTTP fetch via respx.
    with (
        patch(
            "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
        ),
        respx.mock(assert_all_called=False) as _router,
    ):
        _router.get(_petstore_url).mock(
            return_value=httpx.Response(
                200,
                content=_petstore_30_bytes,
                headers={"content-type": "application/yaml"},
            )
        )
        operations = parse_openapi(_petstore_url)

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


# ---------------------------------------------------------------------------
# G0.9-T9 (#741) — version label coverage pre-flight
# ---------------------------------------------------------------------------


class _FakeRangedConnector(Connector):
    """Hand-rolled connector class for pre-flight tests.

    Stands in for the kind of subclass an operator would register at
    G3.x — pinned ``supported_version_range`` mirrors the real
    :class:`VmwareRestConnector` shape (``">=8.5,<10.0"``) and the
    methods are no-op stubs so the ABC is concrete.
    """

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=8.5,<10.0"
    priority = 1

    async def fingerprint(self, target: Any) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError


def test_check_version_covered_passes_when_label_inside_range() -> None:
    """A class advertising ``">=8.5,<10.0"`` accepts label ``"8.6"`` — no raise."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeRangedConnector,
    )
    # Inside the range → silent pass.
    check_version_covered_by_registered_class(
        product="vmware",
        version="8.6",
        impl_id="vmware-rest",
    )


def test_check_version_covered_raises_when_label_outside_range() -> None:
    """A class with non-matching range → :exc:`UncoveredVersionLabel` with detail."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeRangedConnector,
    )
    with pytest.raises(UncoveredVersionLabel) as excinfo:
        check_version_covered_by_registered_class(
            product="vmware",
            version="7.0",
            impl_id="vmware-rest",
        )
    err = excinfo.value
    assert err.product == "vmware"
    assert err.version == "7.0"
    assert err.impl_id == "vmware-rest"
    # The exception names the existing class + its range so the
    # operator can see what to fix.
    assert err.candidates == [("9.0", "vmware-rest", "_FakeRangedConnector", ">=8.5,<10.0")]
    detail = str(err)
    assert "version='7.0'" in detail
    assert ">=8.5,<10.0" in detail
    assert "_FakeRangedConnector" in detail


def test_check_version_covered_logs_orphan_when_no_class_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No class for ``(product, impl_id)`` → log ``connector_ingest_orphaned_class``; no raise.

    Capture surface (#1254): the test does NOT use
    :func:`structlog.testing.capture_logs` — under pytest-xdist with
    other tests on the same worker mutating
    :func:`structlog.configure` (lifespan boot, observability fixtures),
    ``capture_logs`` has been observed to miss the event even when the
    event was emitted to fd-level stdout (CI iter-1 of T7 #1241/PR
    #1248, xdist ``gw4``). The flake is in the global
    processor-list-swap pattern ``capture_logs`` uses, not in the
    subject code.

    Instead we bind a private :class:`structlog.testing.LogCapture`
    onto a freshly-wrapped logger and monkeypatch the subject
    module's module-level ``_log`` for the test's duration. This is
    process-local, contextvar-free, and immune to any concurrent
    :func:`structlog.configure` call. The original ``_log`` is
    restored automatically by ``monkeypatch`` on test teardown.
    """
    # Registry is empty (autouse fixture cleared it).
    from meho_backplane.operations.ingest import connector_registration

    capture = structlog.testing.LogCapture()
    private_log = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[capture],
    )
    monkeypatch.setattr(connector_registration, "_log", private_log)

    check_version_covered_by_registered_class(
        product="brand-new-vendor",
        version="1.0",
        impl_id="brand-new-impl",
    )

    events = [
        entry
        for entry in capture.entries
        if entry.get("event") == "connector_ingest_orphaned_class"
    ]
    assert len(events) == 1
    assert events[0]["product"] == "brand-new-vendor"
    assert events[0]["version"] == "1.0"
    assert events[0]["impl_id"] == "brand-new-impl"


def test_check_version_covered_ignores_other_impls_of_same_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class for ``(product, impl_id_A)`` does not constrain ingest under ``impl_id_B``.

    The pre-flight filters by the full ``(product, impl_id)`` pair, so
    a coexisting class on the same product but a different impl
    (``vmware-pyvmomi`` vs ``vmware-rest``) leaves the ``impl_id_B``
    pre-flight in the no-class-registered branch and proceeds with
    the orphan warning.

    Capture surface (#1254): binds a private
    :class:`structlog.testing.LogCapture` + monkeypatches the subject
    module's ``_log`` rather than using
    :func:`structlog.testing.capture_logs`, whose global
    processor-list swap can return an empty list when a concurrent
    :func:`structlog.configure` (lifespan boot / observability
    fixtures) races it -- the same flake #1258 fixed for the sibling
    orphan test. Process-local, contextvar-free, auto-restored on
    teardown.
    """
    from meho_backplane.operations.ingest import connector_registration

    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeRangedConnector,
    )

    capture = structlog.testing.LogCapture()
    private_log = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[capture],
    )
    monkeypatch.setattr(connector_registration, "_log", private_log)

    check_version_covered_by_registered_class(
        product="vmware",
        version="7.0",
        impl_id="vmware-pyvmomi",
    )

    orphan_events = [
        entry
        for entry in capture.entries
        if entry.get("event") == "connector_ingest_orphaned_class"
    ]
    assert len(orphan_events) == 1
    assert orphan_events[0]["impl_id"] == "vmware-pyvmomi"


@pytest.mark.asyncio
async def test_register_ingested_blocks_when_version_outside_existing_class_range(
    stub_embedding_service: AsyncMock,
) -> None:
    """End-to-end: ingest under ``(vmware, 7.0, vmware-rest)`` with a
    registered ``VmwareRestConnector``-shaped class → 422-mapped
    :exc:`UncoveredVersionLabel`; no rows persisted.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeRangedConnector,
    )
    with pytest.raises(UncoveredVersionLabel):
        await register_ingested_operations(
            product="vmware",
            version="7.0",
            impl_id="vmware-rest",
            spec_source="vcenter.yaml",
            operations=[_proto("GET:/api/vcenter/cluster", path="/api/vcenter/cluster")],
            embedding_service=stub_embedding_service,
        )
    # No rows persisted — the pre-flight raised before any upsert.
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
    assert rows == []
    # The embedding pipeline was not invoked.
    assert stub_embedding_service.encode_one.call_count == 0


@pytest.mark.asyncio
async def test_register_ingested_warns_and_proceeds_when_no_class_registered(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No class for ``(product, impl_id)`` → ingest proceeds and emits
    ``connector_ingest_orphaned_class``.

    Capture surface (#1254): same private-``LogCapture`` +
    monkeypatched-``_log`` pattern as the sibling orphan tests; the
    orphan event is emitted from ``connector_registration._log`` even
    on the ``register_ingested_operations`` path, so patching that
    module's logger captures it without the
    :func:`structlog.testing.capture_logs` global-swap flake.
    """
    from meho_backplane.operations.ingest import connector_registration

    capture = structlog.testing.LogCapture()
    private_log = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[capture],
    )
    monkeypatch.setattr(connector_registration, "_log", private_log)

    # Registry empty for ``(newvendor, newvendor-rest)``. The triple is
    # aligned (``newvendor-rest-1.0`` parses back to ``newvendor``) so the
    # orphaned-class path is exercised without a product↔impl_id divergence.
    result = await register_ingested_operations(
        product="newvendor",
        version="1.0",
        impl_id="newvendor-rest",
        spec_source="vendor.yaml",
        operations=[_proto("GET:/things", path="/things")],
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == 1
    # The orphan event is logged with the full triple.
    orphan_events = [
        entry
        for entry in capture.entries
        if entry.get("event") == "connector_ingest_orphaned_class"
    ]
    assert len(orphan_events) == 1
    event = orphan_events[0]
    assert event["product"] == "newvendor"
    assert event["version"] == "1.0"
    assert event["impl_id"] == "newvendor-rest"
    # The auto-shim is registered after the orphan log (the ingest
    # proceeded — that's the warn-but-proceed semantics).
    assert ("newvendor", "1.0", "newvendor-rest") in all_connectors_v2()


@pytest.mark.asyncio
async def test_register_ingested_passes_pre_flight_for_compatible_version(
    stub_embedding_service: AsyncMock,
) -> None:
    """Operator-supplied version inside a registered class's range → success path."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeRangedConnector,
    )
    result = await register_ingested_operations(
        # Inside the ``>=8.5,<10.0`` range advertised by the existing class.
        product="vmware",
        version="9.0.3",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto("GET:/api/vcenter/cluster", path="/api/vcenter/cluster")],
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == 1
    # A NEW auto-shim is registered under the (vmware, 9.0.3, vmware-rest) triple
    # alongside the pre-existing (vmware, 9.0, vmware-rest) entry — the pre-flight
    # checks coverage, it does not require the triple to already exist.
    assert ("vmware", "9.0.3", "vmware-rest") in all_connectors_v2()


# ---------------------------------------------------------------------------
# Product identity at the ingest write path (claude-rdc-hetzner-dc#1136,
# Initiative #1810)
#
# Historically the VCF-family connectors registered under a long product
# (``product="vcf-automation"``) while the dispatch/query surface derived
# the short product (``"vcfa"``) from the connector_id's impl_id segment,
# so an ingest under the long product was reconciled down to the short one
# (``_reconciled_row_product`` → ``dispatch_product``) to stay
# dispatchable. #1814 (Initiative #1810) realigned the whole family to
# register under the short token directly, #1816 promoted
# ``register_connector_v2``'s product↔impl_id round-trip check to a
# hard-fail, and #1817 (this task) added a round-trip guard at the ingest
# route boundary and retired the row-reconciliation helpers. The supplied
# product now persists verbatim — a divergent product is rejected at the
# route with a 422 (see
# ``test_api_v1_connectors_ingest.py::test_ingest_divergent_product_rejected_with_422``)
# before it ever reaches ``register_ingested_operations``. These tests pin
# the service-layer end state: aligned ingests persist the supplied
# product unchanged, and a divergent product that bypasses the boundary
# trips the registration hard-fail backstop rather than silently
# reconciling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aligned_product_ingest_persists_supplied_product(
    stub_embedding_service: AsyncMock,
) -> None:
    """A round-tripping product persists verbatim; the auto-shim lands under it.

    ``vmware`` / ``vmware-rest`` round-trips (``vmware-rest-9.0`` parses to
    ``vmware``), so the rows persist under the supplied ``vmware`` and the
    synthesised shim is dispatchable under the same token.
    """
    assert parse_connector_id("vmware-rest-9.0")[0] == "vmware"
    result = await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[_proto("GET:/api/vcenter/cluster", path="/api/vcenter/cluster")],
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == 1
    assert result.connector_registered is True
    assert ("vmware", "9.0", "vmware-rest") in all_connectors_v2()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(EndpointDescriptor))).scalars().all()
    assert rows[0].product == "vmware"


@pytest.mark.asyncio
async def test_divergent_product_ingest_trips_registration_hard_fail(
    stub_embedding_service: AsyncMock,
) -> None:
    """A divergent product reaching the service layer trips the #1816 hard-fail.

    G0.27 / T3 (#1817). The ingest route boundary rejects a divergent
    product with a 422 before the pipeline runs (see
    ``test_api_v1_connectors_ingest.py``), so ``register_ingested_operations``
    is only ever called with a round-tripping product in production. With
    the long↔short row reconciliation retired, a divergent product that
    bypasses the boundary no longer silently lands rows under a
    parser-derived spelling: the auto-shim is synthesised under the
    supplied ``drift-test`` and
    :func:`~meho_backplane.connectors.registry.register_connector_v2`'s
    product↔impl_id round-trip check (#1816) fails closed with a
    ``RuntimeError`` naming the derived ``drift``. This pins the backstop
    that replaced the reconciliation.
    """
    assert parse_connector_id("drift-impl-9.1")[0] == "drift"

    with pytest.raises(RuntimeError, match=r"drift-impl-9\.1.*parses to product 'drift'"):
        await register_ingested_operations(
            product="drift-test",  # diverges from the parser-derived "drift"
            version="9.1",
            impl_id="drift-impl",
            spec_source="vendor.yaml",
            operations=[_proto("GET:/things", path="/things")],
            embedding_service=stub_embedding_service,
        )

    # Nothing was registered under either spelling — the hard-fail aborts
    # before the divergent shim is admitted to the registry.
    snapshot = all_connectors_v2()
    assert ("drift", "9.1", "drift-impl") not in snapshot
    assert ("drift-test", "9.1", "drift-impl") not in snapshot


@pytest.mark.asyncio
async def test_service_ingest_rejects_divergent_product_with_handrolled_impl_no_rows() -> None:
    """The service layer rejects a divergent product whose impl_id is hand-coded.

    G0.27 / T3 (#1817) — the hole #1851 closes. ``register_connector_v2``'s
    #1816 hard-fail is the backstop for a divergent product with **no**
    hand-coded class (see
    ``test_divergent_product_ingest_trips_registration_hard_fail``), but it
    never fires when a hand-coded class already covers the ``impl_id``: the
    ingest guard in
    :func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`
    defers to the hand-coded class and returns ``False`` *without* calling
    ``register_connector_v2``, so a bare
    ``register_ingested_operations(product='vcf-logs', impl_id='vrli-rest')``
    would persist rows under ``vcf-logs`` (the silent non-dispatchable
    shadow #1810 exists to eliminate). The REST 422 guard covered this for
    the route, but the ``meho.connector.ingest`` MCP tool reaches
    ``register_ingested_operations`` directly and bypassed it.

    Moving the round-trip enforcement into
    :meth:`~meho_backplane.operations.ingest.IngestionPipelineService.ingest`
    closes the hole for every entry point: the divergent product is
    rejected with :class:`ProductImplIdMismatch` *before any DB write*, so
    no rows persist. ``VcfLogsConnector`` registers under the canonical
    ``vrli`` (matching the real post-#1814 state); the operator supplying
    ``product='vcf-logs'`` is the divergence.
    """
    from meho_backplane.connectors.vcf_logs import VcfLogsConnector

    # The hand-coded class covers impl_id ``vrli-rest`` under ``vrli`` —
    # exactly the post-#1814 production registration.
    register_connector_v2(
        product="vrli",
        version="9.0",
        impl_id="vrli-rest",
        cls=VcfLogsConnector,
    )
    operator = Operator(
        sub="tenant-admin",
        raw_jwt="test-jwt",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    service = IngestionPipelineService(operator=operator)

    with pytest.raises(ProductImplIdMismatch) as excinfo:
        await service.ingest(
            product="vcf-logs",  # diverges from the parser-derived "vrli"
            version="9.0",
            impl_id="vrli-rest",
            specs=[SpecSource(uri="https://example.test/vrli.yaml")],
            tenant_id=None,
            dry_run=False,
        )
    err = excinfo.value
    assert err.product == "vcf-logs"
    assert err.derived_product == "vrli"
    # The message names both spellings so the agent driving the MCP tool
    # knows the fix.
    rendered = str(err)
    assert "vcf-logs" in rendered
    assert "vrli" in rendered

    # The guard runs before any spec fetch or DB write, so the divergent
    # ingest persisted nothing under either spelling.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(EndpointDescriptor))).scalars().all()
    assert rows == [], f"divergent ingest must persist no rows; got {rows!r}"


@pytest.mark.asyncio
async def test_aligned_ingest_reuses_handrolled_class_no_shim(
    stub_embedding_service: AsyncMock,
) -> None:
    """An aligned ingest whose impl_id is hand-coded reuses it; no shim, rows under the product.

    G0.26-T4 (#1798) ingest guard, post-#1814/#1817. The real
    :class:`VcfLogsConnector` registers under the canonical
    ``product="vrli"`` (``vrli-rest-9.0`` round-trips to ``vrli``), so
    ingesting ``vrli`` / ``vrli-rest`` finds the hand-coded class via the
    impl_id guard, synthesises **no** auto-shim, and persists the rows
    under ``vrli`` — dispatchable through ``VcfLogsConnector``, not a
    shadowing shim. This is the structural fix for the v0.16.0 SEV-2.
    """
    from meho_backplane.connectors.vcf_logs import VcfLogsConnector

    assert parse_connector_id("vrli-rest-9.0")[0] == "vrli"
    register_connector_v2(
        product="vrli",
        version="9.0",
        impl_id="vrli-rest",
        cls=VcfLogsConnector,
    )

    result = await register_ingested_operations(
        product="vrli",  # the parser-derived product the ingest path supplies
        version="9.0",
        impl_id="vrli-rest",
        spec_source="vrli.yaml",
        operations=[_proto("GET:/api/v2/version", path="/api/v2/version")],
        embedding_service=stub_embedding_service,
    )
    assert result.inserted_count == 1
    # No auto-shim was synthesised — the hand-coded class already covers
    # the triple, so the ``connector_registered`` flag stays False.
    assert result.connector_registered is False
    shim_keys = [
        key for key, cls in all_connectors_v2().items() if issubclass(cls, GenericRestConnector)
    ]
    assert shim_keys == [], f"expected no auto-shim for an aligned ingest; got {shim_keys!r}"

    # Rows persist under the supplied (canonical) product and dispatch
    # through VcfLogsConnector.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(EndpointDescriptor))).scalars().all()
    assert len(rows) == 1
    assert rows[0].product == "vrli"
    exists = await connector_exists(
        tenant_id=uuid.uuid4(),
        product="vrli",
        version="9.0",
        impl_id="vrli-rest",
    )
    assert exists is True
