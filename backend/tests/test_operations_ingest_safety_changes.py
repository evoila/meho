# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety-class change surfacing at connector re-ingest (#2702).

Sibling of :mod:`tests.test_operations_register_ingested`, focused on
the report-only affordance #2702 adds: a re-ingest that overwrites an
existing row's ``safety_level`` with a different value must surface
``(op_id, old, new)`` on :class:`IngestionResult.safety_changes`, name
every Sensor pinning the reclassified op, and emit one structlog
``ingest_safety_class_changed`` warning per change.

Coverage matrix (the task's acceptance criteria):

* Both existing-row update paths capture the diff -- the body hash
  covers embedding text only, so the skip-re-embed branch (unchanged
  summary / description / tags, changed safety) and the re-embed branch
  (changed text + changed safety) each produce an entry.
* Ops with unchanged safety are absent; an idempotent re-ingest and a
  first ingest both yield ``safety_changes == ()``.
* A Sensor row pinning ``(connector_id, op_id)`` of a reclassified op
  is listed with its id / name / tenant_id; a sensor pinning an
  *unchanged* op is not listed.
* One ``ingest_safety_class_changed`` warning fires per change, keyed
  with op_id, old, new, and the affected sensor count.

The embedding service is mocked via the explicit ``embedding_service=``
parameter (the same seam the register-ingested tests use) so no ONNX /
fastembed dependency is pulled.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, Tenant
from meho_backplane.operations.ingest import (
    EndpointDescriptorProto,
    IngestionResult,
    register_ingested_operations,
)
from meho_backplane.settings import get_settings

_ASSERTION = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}

_CONNECTOR = {"product": "petstore", "version": "1.0", "impl_id": "petstore"}

#: Operator-facing identifier the Sensor rows pin -- the inverse of
#: ``parse_connector_id`` over the triple above (``<impl_id>-<version>``).
_CONNECTOR_ID = "petstore-1.0"


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
    """Reset the v2 connector registry between tests (auto-shim isolation)."""
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


def _proto(
    op_id: str,
    *,
    summary: str = "Summary",
    safety_level: str = "safe",
) -> EndpointDescriptorProto:
    """Parser-shaped proto with a controllable ``safety_level``.

    Keeps summary / description / tags identical across calls unless the
    test overrides ``summary``, so the body-hash branch taken (skip vs
    re-embed) is chosen by the *summary* argument alone.
    """
    return EndpointDescriptorProto(
        op_id=op_id,
        method="GET",
        path=f"/{op_id.split(':', 1)[1]}",
        summary=summary,
        description="Description",
        tags=["pets"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema={"type": "object"},
        safety_level=safety_level,  # type: ignore[arg-type]
        requires_approval=False,
    )


async def _register(
    operations: list[EndpointDescriptorProto],
    embedding_service: AsyncMock,
) -> IngestionResult:
    """One :func:`register_ingested_operations` call under the pinned triple."""
    return await register_ingested_operations(
        product=_CONNECTOR["product"],
        version=_CONNECTOR["version"],
        impl_id=_CONNECTOR["impl_id"],
        spec_source="petstore.yaml",
        operations=operations,
        embedding_service=embedding_service,
    )


async def _seed_sensor(*, name: str, op_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a tenant + a Sensor pinning ``(_CONNECTOR_ID, op_id)``.

    Returns ``(tenant_id, sensor_id)``. Minimal-viable row per the
    :class:`Sensor` model contract (interval cadence, threshold
    assertion); the fields the surfacing reads are ``connector_id`` +
    ``op_id`` + the identity triple (id / name / tenant_id).
    """
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    sensor_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=f"t-{name}", name=f"Tenant {name}"))
        await session.flush()
        session.add(
            Sensor(
                id=sensor_id,
                tenant_id=tenant_id,
                name=name,
                connector_id=_CONNECTOR_ID,
                op_id=op_id,
                assertion=_ASSERTION,
                cadence_kind=SensorCadenceKind.INTERVAL.value,
                interval_seconds=60,
                created_by_sub="user-admin",
            )
        )
        await session.commit()
    return tenant_id, sensor_id


# ---------------------------------------------------------------------------
# Capture on both existing-row update paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_change_captured_on_skip_reembed_path(
    stub_embedding_service: AsyncMock,
) -> None:
    """Unchanged embedding text + changed safety_level → change captured.

    The body hash covers summary / description / tags, not safety
    metadata, so this re-ingest lands on the skip-re-embed branch --
    the branch that previously overwrote ``safety_level`` with no trace.
    """
    await _register(
        [_proto("GET:/pets"), _proto("GET:/owners")],
        stub_embedding_service,
    )

    result = await _register(
        [_proto("GET:/pets", safety_level="caution"), _proto("GET:/owners")],
        stub_embedding_service,
    )

    assert result.skipped_count == 2
    changes = result.safety_changes
    assert [(c.op_id, c.old_safety_level, c.new_safety_level) for c in changes] == [
        ("GET:/pets", "safe", "caution"),
    ]


@pytest.mark.asyncio
async def test_safety_change_captured_on_reembed_path(
    stub_embedding_service: AsyncMock,
) -> None:
    """Changed embedding text + changed safety_level → change captured."""
    await _register([_proto("GET:/pets")], stub_embedding_service)

    result = await _register(
        [_proto("GET:/pets", summary="Rewritten summary", safety_level="dangerous")],
        stub_embedding_service,
    )

    assert result.updated_count == 1
    changes = result.safety_changes
    assert [(c.op_id, c.old_safety_level, c.new_safety_level) for c in changes] == [
        ("GET:/pets", "safe", "dangerous"),
    ]


@pytest.mark.asyncio
async def test_unchanged_safety_yields_no_changes(
    stub_embedding_service: AsyncMock,
) -> None:
    """First ingest and idempotent re-ingest both report zero changes."""
    first = await _register([_proto("GET:/pets")], stub_embedding_service)
    assert first.safety_changes == ()

    rerun = await _register([_proto("GET:/pets")], stub_embedding_service)
    assert rerun.safety_changes == ()


# ---------------------------------------------------------------------------
# Sensor join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_change_lists_pinning_sensors_only(
    stub_embedding_service: AsyncMock,
) -> None:
    """A sensor on the reclassified op is listed; one on an unchanged op is not."""
    await _register(
        [_proto("GET:/pets"), _proto("GET:/owners")],
        stub_embedding_service,
    )
    tenant_id, sensor_id = await _seed_sensor(name="pets-watch", op_id="GET:/pets")
    await _seed_sensor(name="owners-watch", op_id="GET:/owners")

    result = await _register(
        [_proto("GET:/pets", safety_level="caution"), _proto("GET:/owners")],
        stub_embedding_service,
    )

    [change] = result.safety_changes
    assert change.op_id == "GET:/pets"
    assert [(s.id, s.name, s.tenant_id) for s in change.affected_sensors] == [
        (sensor_id, "pets-watch", tenant_id),
    ]


# ---------------------------------------------------------------------------
# Structlog warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warning_fires_per_change_with_sensor_count(
    stub_embedding_service: AsyncMock,
) -> None:
    """One ``ingest_safety_class_changed`` warning per change, fully keyed."""
    await _register(
        [_proto("GET:/pets"), _proto("GET:/owners")],
        stub_embedding_service,
    )
    await _seed_sensor(name="pets-watch", op_id="GET:/pets")

    with structlog.testing.capture_logs() as logs:
        await _register(
            [
                _proto("GET:/pets", safety_level="caution"),
                _proto("GET:/owners", safety_level="dangerous"),
            ],
            stub_embedding_service,
        )

    warnings = [e for e in logs if e["event"] == "ingest_safety_class_changed"]
    assert [
        (
            e["op_id"],
            e["old_safety_level"],
            e["new_safety_level"],
            e["affected_sensor_count"],
            e["log_level"],
        )
        for e in warnings
    ] == [
        ("GET:/pets", "safe", "caution", 1, "warning"),
        ("GET:/owners", "safe", "dangerous", 0, "warning"),
    ]
    assert all(e["connector_id"] == _CONNECTOR_ID for e in warnings)
