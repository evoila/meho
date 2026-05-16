# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration tests for the 5 vmware-rest read composites.

Coverage matrix (G3.1-T5 / #508 acceptance criteria):

* All 5 expected ``op_id`` rows land in ``endpoint_descriptor`` with
  ``source_kind="composite"``.
* Each row carries the right ``safety_level="safe"`` +
  ``requires_approval=False`` overrides (i.e. T4's
  ``dangerous`` / ``True`` defaults are intentionally NOT inherited).
* Each row's ``handler_ref`` resolves to the dotted path of the
  module-level handler (no closures / partials).
* Each row's ``group_id`` resolves to the expected ``group_key``
  (``cluster`` / ``events`` / ``performance`` / ``storage`` /
  ``networking``).
* Idempotent re-registration: running the registrar twice is a no-op
  for the embedding pipeline (body-hash skip from T4's inherited
  shared upsert path).
* Parameter-schema persistence: each row's ``parameter_schema`` round-
  trips verbatim.
* Side-effect import: importing
  :mod:`meho_backplane.connectors.vmware_rest` queues
  :func:`register_vmware_composite_operations` onto the typed-op
  registrar list (lifespan-wiring smoke test).

Mirrors :mod:`tests.test_operations_composite_register` for the
substrate-level coverage; this module's tests focus on the
*per-connector* contract that 5 named composites end up registered
with the right overrides.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vmware_rest.composites import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.typed_register import (
    _TYPED_OP_REGISTRARS,
    clear_typed_op_registrars,
)
from meho_backplane.settings import get_settings

_EXPECTED_OP_IDS: tuple[str, ...] = (
    "vmware.composite.cluster.drs_recommendations",
    "vmware.composite.event.tail",
    "vmware.composite.performance.summary",
    "vmware.composite.datastore.usage",
    "vmware.composite.network.portgroup.audit",
)


_EXPECTED_HANDLER_REF_BY_OP: dict[str, str] = {
    "vmware.composite.cluster.drs_recommendations": (
        "meho_backplane.connectors.vmware_rest.composites._read."
        "cluster_drs_recommendations_composite"
    ),
    "vmware.composite.event.tail": (
        "meho_backplane.connectors.vmware_rest.composites._read.event_tail_composite"
    ),
    "vmware.composite.performance.summary": (
        "meho_backplane.connectors.vmware_rest.composites._read.performance_summary_composite"
    ),
    "vmware.composite.datastore.usage": (
        "meho_backplane.connectors.vmware_rest.composites._read.datastore_usage_composite"
    ),
    "vmware.composite.network.portgroup.audit": (
        "meho_backplane.connectors.vmware_rest.composites._read.network_portgroup_audit_composite"
    ),
}


_EXPECTED_GROUP_KEY_BY_OP: dict[str, str] = {
    "vmware.composite.cluster.drs_recommendations": "cluster",
    "vmware.composite.event.tail": "events",
    "vmware.composite.performance.summary": "performance",
    "vmware.composite.datastore.usage": "storage",
    "vmware.composite.network.portgroup.audit": "networking",
}


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
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry + typed-op registrars.

    ``_TYPED_OP_REGISTRARS`` is a process-global list the chassis
    lifespan iterates at startup. ``test_importing_..._queues_composite_registrar``
    calls :func:`clear_typed_op_registrars` and then ``importlib.reload``\\ s
    only the vmware composites package — which re-registers *just* that
    one registrar, permanently dropping every other registrar (Vault,
    K8s, …) for the rest of the pytest session. A later app-booting
    test (``test_mcp_tool_meho_status``) then runs lifespan over the
    truncated list and mis-wires its health probes (observed:
    ``vault.reachable`` reported wrong). Snapshot + restore the list
    around every test in this module so the mutation can't leak —
    same discipline already applied to the connector registry above.
    """
    saved_registrars = list(_TYPED_OP_REGISTRARS)
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _TYPED_OP_REGISTRARS[:] = saved_registrars


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the upsert doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Five composites land with the right overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_vmware_composite_operations_inserts_five_rows(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar lands all 5 op_ids in ``endpoint_descriptor``."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == set(_EXPECTED_OP_IDS)
    # Embedding service called once per composite -- 13 total after
    # T6 (#509) shipped the 8 write composites alongside these 5.
    assert stub_embedding_service.encode_one.call_count == 13


@pytest.mark.asyncio
async def test_every_composite_row_uses_safe_no_approval_overrides(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row carries ``safety_level="safe"`` + ``requires_approval=False``.

    Load-bearing override of T4's ``dangerous`` / ``True`` defaults --
    every composite in this Task is read-only, so the policy gate
    should not pop the approval queue.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        assert row.safety_level == "safe", f"{row.op_id}: expected safe, got {row.safety_level!r}"
        assert row.requires_approval is False, (
            f"{row.op_id}: expected requires_approval=False, got {row.requires_approval!r}"
        )


@pytest.mark.asyncio
async def test_every_composite_row_carries_composite_source_kind(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row has ``source_kind="composite"`` (routes the dispatcher's composite branch)."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        assert row.source_kind == "composite"
        assert row.tenant_id is None
        assert row.is_enabled is True
        assert row.method is None
        assert row.path is None


@pytest.mark.asyncio
async def test_handler_ref_round_trips_to_module_level_dotted_path(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row's ``handler_ref`` is the canonical module-level dotted path."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    by_op = {row.op_id: row for row in rows}
    for op_id, expected_ref in _EXPECTED_HANDLER_REF_BY_OP.items():
        assert by_op[op_id].handler_ref == expected_ref


@pytest.mark.asyncio
async def test_group_resolution_lands_each_composite_in_its_named_group(
    stub_embedding_service: AsyncMock,
) -> None:
    """The 5 composites land in 5 distinct groups by ``group_key``."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        descriptor_rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
        group_rows = (await fresh.execute(select(OperationGroup))).scalars().all()
    groups_by_id = {g.id: g for g in group_rows}
    for desc in descriptor_rows:
        assert desc.group_id is not None, f"{desc.op_id} has no group_id"
        group = groups_by_id[desc.group_id]
        expected_key = _EXPECTED_GROUP_KEY_BY_OP[desc.op_id]
        assert group.group_key == expected_key, (
            f"{desc.op_id}: expected group {expected_key!r}, got {group.group_key!r}"
        )
        assert group.product == "vmware"
        assert group.version == "9.0"
        assert group.impl_id == "vmware-rest"


@pytest.mark.asyncio
async def test_parameter_schema_persists_with_required_fields(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row's ``parameter_schema`` round-trips with the documented required fields."""
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    by_op = {row.op_id: row for row in rows}
    # cluster.drs_recommendations requires ``cluster``.
    cluster_schema: dict[str, Any] = dict(
        by_op["vmware.composite.cluster.drs_recommendations"].parameter_schema
    )
    assert cluster_schema["required"] == ["cluster"]
    assert "cluster" in dict(cluster_schema["properties"])
    # performance.summary requires ``entity_moid``.
    perf_schema: dict[str, Any] = dict(
        by_op["vmware.composite.performance.summary"].parameter_schema
    )
    assert perf_schema["required"] == ["entity_moid"]
    # event.tail has no required keys (everything has a default).
    event_schema: dict[str, Any] = dict(by_op["vmware.composite.event.tail"].parameter_schema)
    assert event_schema["required"] == []
    # All schemas pin ``additionalProperties=False`` so typo'd keys
    # surface clearly at dispatcher-side validation.
    for op_id in _EXPECTED_OP_IDS:
        schema: dict[str, Any] = dict(by_op[op_id].parameter_schema)
        assert schema["additionalProperties"] is False, (
            f"{op_id}: parameter_schema missing additionalProperties:False"
        )


@pytest.mark.asyncio
async def test_response_schema_persists_for_every_composite(
    stub_embedding_service: AsyncMock,
) -> None:
    """Each row persists a non-null ``response_schema`` describing the handler's return shape.

    Parity with the ``vault.kv.read`` precedent (the only other typed-op
    surface that ships an explicit response schema). The meta-tools
    surface this on ``describe_operation`` calls without needing a
    schema-construction round-trip.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    by_op = {row.op_id: row for row in rows}
    # Each row carries a response_schema dict (non-null, non-empty).
    for op_id in _EXPECTED_OP_IDS:
        schema = by_op[op_id].response_schema
        assert isinstance(schema, dict), (
            f"{op_id}: expected response_schema dict, got {type(schema).__name__}"
        )
        assert schema.get("type") == "object", (
            f"{op_id}: response_schema must be a JSON-Schema object"
        )
        assert "properties" in schema, f"{op_id}: response_schema missing 'properties' key"
    # Spot-check the load-bearing top-level keys per composite.
    cluster_resp: dict[str, Any] = dict(
        by_op["vmware.composite.cluster.drs_recommendations"].response_schema
    )
    cluster_props = dict(cluster_resp["properties"])
    assert {"cluster", "drs", "recommendations_history"} <= set(cluster_props)
    assert set(cluster_resp["required"]) == {"cluster", "drs"}

    event_resp: dict[str, Any] = dict(by_op["vmware.composite.event.tail"].response_schema)
    event_props = dict(event_resp["properties"])
    assert {"events", "count", "moId", "max_events_applied"} <= set(event_props)

    perf_resp: dict[str, Any] = dict(by_op["vmware.composite.performance.summary"].response_schema)
    perf_props = dict(perf_resp["properties"])
    assert {"entity_moid", "available_counters", "samples"} <= set(perf_props)

    ds_resp: dict[str, Any] = dict(by_op["vmware.composite.datastore.usage"].response_schema)
    assert "datastores" in dict(ds_resp["properties"])

    net_resp: dict[str, Any] = dict(
        by_op["vmware.composite.network.portgroup.audit"].response_schema
    )
    assert "portgroups" in dict(net_resp["properties"])


@pytest.mark.asyncio
async def test_tags_include_composite_and_read_only(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every composite row carries ``composite`` + ``read-only`` tags.

    Both tags are load-bearing for downstream filtering: operators
    that want to inspect read composites only run
    ``meho operation list ... --tag read-only`` and the table-of-five
    surfaces immediately.
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        assert "composite" in row.tags
        assert "read-only" in row.tags


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_vmware_composite_operations_is_idempotent(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar twice -> 5 read rows persist; embedding stays at 13.

    The second run's body-hash skip path is what holds across both
    read and write composites; this test asserts the read rows still
    persist after the combined registrar (5 reads + 8 writes / T6).
    """
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    first_count = stub_embedding_service.encode_one.call_count
    assert first_count == 13

    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    # Skip-re-embed path -- second run is a no-op for the embedding
    # pipeline because the text composes to the same body-hash.
    assert stub_embedding_service.encode_one.call_count == first_count

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_EXPECTED_OP_IDS))
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 5


# ---------------------------------------------------------------------------
# Side-effect import wires the registrar into the lifespan list
# ---------------------------------------------------------------------------


def test_importing_vmware_rest_subpackage_queues_composite_registrar() -> None:
    """Importing the ``vmware_rest`` package queues the composite registrar.

    Asserts the contract that
    ``meho_backplane.connectors.vmware_rest/__init__.py`` re-exports
    the ``composites`` subpackage *and* the composites' ``__init__``
    calls
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    at module-import time. The chassis lifespan iterates this list in
    :func:`run_typed_op_registrars`, so a missing registration here
    would translate to "no rows landed during startup".
    """
    clear_typed_op_registrars()
    # Force a re-import so the module-top-level
    # ``register_typed_op_registrar`` call fires under the cleared list.
    import meho_backplane.connectors.vmware_rest.composites as composites_pkg

    importlib.reload(composites_pkg)

    assert any(
        r.__name__ == "register_vmware_composite_operations" for r in _TYPED_OP_REGISTRARS
    ), (
        "expected register_vmware_composite_operations on the typed-op "
        f"registrar list, got names "
        f"{[getattr(r, '__name__', repr(r)) for r in _TYPED_OP_REGISTRARS]}"
    )


# ---------------------------------------------------------------------------
# Module-level handler identity (no closures, partials, lambdas)
# ---------------------------------------------------------------------------


def test_all_handlers_are_module_level_coroutine_functions() -> None:
    """Each handler is a plain module-level ``async def``.

    Anchors the issue body's invariant: ``derive_handler_ref()`` at
    registration time would reject closures / partials / lambdas, so
    a regression that wraps a handler in ``functools.partial`` would
    surface here before the registrar even runs.
    """
    import inspect

    for handler in (
        cluster_drs_recommendations_composite,
        event_tail_composite,
        performance_summary_composite,
        datastore_usage_composite,
        network_portgroup_audit_composite,
    ):
        assert inspect.iscoroutinefunction(handler), f"{handler!r} is not a coroutine function"
        assert "<locals>" not in handler.__qualname__
        assert handler.__qualname__ != "<lambda>"
