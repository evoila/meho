# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the two-world registration-time invariant (#2252).

Goal #2247 / Initiative #2248. A code-shipped op (``source_kind='typed'``
or ``'composite'``) must never dispatch through an *ingested*
``endpoint_descriptor`` row. This module covers both faces of the
enforcement:

* :func:`assert_no_ingested_dispatch_dependency` — the per-op primitive,
  exercised against the autouse-migrated SQLite engine with synthetic
  descriptor rows (not gh/vmware specifically), per the task's
  "test with a synthetic connector" acceptance criterion:
  - a declared sub-op resolving to an ``ingested`` row -> raises, naming
    the op + every offending sub-op;
  - sub-ops resolving to ``composite`` / ``typed`` rows -> passes;
  - a declared sub-op absent from the table -> passes (absence means the
    op is simply not ingested here, not this invariant's concern);
  - ``*.composite.*`` recursion sub-ops -> skipped, never probed.

* :func:`assert_registered_composites_have_no_ingested_dispatch` — the
  platform-wide sweep over the dispatch-surface registry: it raises for a
  synthetic composite that declares an ingested sub-op, passes for one
  that declares only code-shipped sub-ops, and is a clean no-op for the
  empty registry (the production shape, since every shipped composite
  dispatches its sub-ops directly on the connector session).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import composite_invariant as _invariant
from meho_backplane.operations.composite_invariant import (
    IngestedDispatchDependencyError,
    assert_no_ingested_dispatch_dependency,
    assert_registered_composites_have_no_ingested_dispatch,
    register_composite_dispatch_surface,
    reset_composite_dispatch_surface_registry,
)
from meho_backplane.settings import get_settings

# Synthetic connector under test — deliberately not gh/vmware.
_CONNECTOR_ID = "synth-1.0"
_PRODUCT = "synth"
_VERSION = "1.0"
_IMPL_ID = "synth"

_SUB_OP_A = "GET:/widgets"
_SUB_OP_B = "GET:/widgets/{id}"
_TYPED_SUB_OP = "synth.widget.list"


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
def _reset_dispatch_surface_registry() -> Iterator[None]:
    """Isolate the process-wide dispatch-surface registry across tests.

    Snapshot the registry, clear it to a known synthetic surface for the
    test, then restore the snapshot so a sibling module is not left with a
    mutated registry. In production the registry is empty (every shipped
    composite dispatches its sub-ops directly on the connector session), but
    a stray entry from another test must not leak in either direction.
    """
    saved = dict(_invariant._REGISTRY)
    reset_composite_dispatch_surface_registry()
    yield
    reset_composite_dispatch_surface_registry()
    _invariant._REGISTRY.update(saved)


async def _seed_descriptor(*, op_id: str, source_kind: str, is_enabled: bool = True) -> None:
    """Insert one built-in / global ``endpoint_descriptor`` row for the synthetic connector."""
    sessionmaker = get_sessionmaker()
    now = datetime.now(UTC)
    async with sessionmaker() as session, session.begin():
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=op_id,
                source_kind=source_kind,
                method=None,
                path=None,
                handler_ref=None,
                summary=f"row for {op_id}",
                description=f"row for {op_id}",
                group_id=None,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=is_enabled,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=now,
                updated_at=now,
            )
        )


# ---------------------------------------------------------------------------
# per-op primitive: assert_no_ingested_dispatch_dependency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raises_when_sub_op_resolves_to_ingested() -> None:
    """A declared sub-op resolving to an ``ingested`` row fails closed."""
    await _seed_descriptor(op_id=_SUB_OP_A, source_kind="ingested")
    with pytest.raises(IngestedDispatchDependencyError) as excinfo:
        await assert_no_ingested_dispatch_dependency(
            op_id="synth.composite.widget.audit",
            connector_id=_CONNECTOR_ID,
            sub_op_ids=(_SUB_OP_A,),
        )
    message = str(excinfo.value)
    assert "synth.composite.widget.audit" in message
    assert _SUB_OP_A in message


@pytest.mark.asyncio
async def test_raises_even_when_ingested_row_is_disabled() -> None:
    """Enablement is not filtered — a disabled ingested row is still a violation."""
    await _seed_descriptor(op_id=_SUB_OP_A, source_kind="ingested", is_enabled=False)
    with pytest.raises(IngestedDispatchDependencyError):
        await assert_no_ingested_dispatch_dependency(
            op_id="synth.composite.widget.audit",
            connector_id=_CONNECTOR_ID,
            sub_op_ids=(_SUB_OP_A,),
        )


@pytest.mark.asyncio
async def test_reports_every_offending_sub_op() -> None:
    """The error lists all ingested sub-ops, not just the first found."""
    await _seed_descriptor(op_id=_SUB_OP_A, source_kind="ingested")
    await _seed_descriptor(op_id=_SUB_OP_B, source_kind="ingested")
    with pytest.raises(IngestedDispatchDependencyError) as excinfo:
        await assert_no_ingested_dispatch_dependency(
            op_id="synth.composite.widget.audit",
            connector_id=_CONNECTOR_ID,
            sub_op_ids=(_SUB_OP_A, _SUB_OP_B),
        )
    message = str(excinfo.value)
    assert _SUB_OP_A in message
    assert _SUB_OP_B in message


@pytest.mark.asyncio
async def test_passes_when_sub_ops_are_code_shipped() -> None:
    """Sub-ops resolving to ``composite`` / ``typed`` rows are allowed."""
    await _seed_descriptor(op_id="synth.composite.widget.other", source_kind="composite")
    await _seed_descriptor(op_id=_TYPED_SUB_OP, source_kind="typed")
    # No raise.
    await assert_no_ingested_dispatch_dependency(
        op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_TYPED_SUB_OP,),
    )


@pytest.mark.asyncio
async def test_passes_when_sub_op_absent() -> None:
    """A sub-op with no descriptor row at all is not this invariant's concern."""
    # Nothing seeded for _SUB_OP_A — absence is the retired composite_l2_missing class.
    await assert_no_ingested_dispatch_dependency(
        op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_SUB_OP_A,),
    )


@pytest.mark.asyncio
async def test_skips_composite_recursion_sub_ops() -> None:
    """``*.composite.*`` sub-ops are skipped even when an ingested row shares the name."""
    # Seed an ingested row whose op_id carries the composite infix; the walk
    # must skip it (registrar-guaranteed recursion), so no violation fires.
    await _seed_descriptor(op_id="synth.composite.widget.other", source_kind="ingested")
    await assert_no_ingested_dispatch_dependency(
        op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=("synth.composite.widget.other",),
    )


# ---------------------------------------------------------------------------
# platform-wide sweep over the dispatch-surface registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_raises_for_registered_synthetic_composite() -> None:
    """The registry sweep applies the primitive to every registered surface."""
    await _seed_descriptor(op_id=_SUB_OP_A, source_kind="ingested")
    register_composite_dispatch_surface(
        composite_op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_SUB_OP_A,),
    )
    with pytest.raises(IngestedDispatchDependencyError):
        await assert_registered_composites_have_no_ingested_dispatch()


@pytest.mark.asyncio
async def test_sweep_passes_for_code_shipped_only_registry() -> None:
    """A registry whose composites only depend on code-shipped sub-ops passes."""
    await _seed_descriptor(op_id=_TYPED_SUB_OP, source_kind="typed")
    register_composite_dispatch_surface(
        composite_op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_TYPED_SUB_OP,),
    )
    # No raise.
    await assert_registered_composites_have_no_ingested_dispatch()


@pytest.mark.asyncio
async def test_sweep_passes_for_empty_registry() -> None:
    """The production shape: no composite declares a descriptor-routed surface.

    Every shipped composite dispatches its sub-ops directly on the connector
    session (Goal #2247), so nothing registers a dispatch surface and the
    sweep is a clean no-op. The seam still exists for a future
    ``dispatch_child``-routed composite; this asserts the empty case passes.
    """
    assert _invariant.registered_composite_dispatch_surfaces() == {}
    await assert_registered_composites_have_no_ingested_dispatch()
