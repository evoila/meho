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
  - a declared sub-op absent from the table -> passes (absence is the
    retired ``composite_l2_missing`` class, not this invariant);
  - ``*.composite.*`` recursion sub-ops -> skipped, never probed.

* :func:`assert_registered_composites_have_no_ingested_dispatch` — the
  platform-wide sweep over the composite-backing registry, including the
  routing check that github's real ``gh.composite.pr_status_summary``
  backing is folded into the shared invariant and passes on a fresh
  deploy (no gh L2 rows ingested).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import composite_backing as _backing
from meho_backplane.operations.composite_backing import (
    register_composite_backing,
    registered_composite_backings,
    reset_composite_backing_registry,
)
from meho_backplane.operations.composite_invariant import (
    IngestedDispatchDependencyError,
    assert_no_ingested_dispatch_dependency,
    assert_registered_composites_have_no_ingested_dispatch,
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

# github's real composite surface (registered at import time by the
# gh-rest composite package) — the routing check.
_GH_COMPOSITE_OP_ID = "gh.composite.pr_status_summary"


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
def _reset_backing_registry() -> Iterator[None]:
    """Isolate the process-wide backing registry, restoring the import-time set.

    Same discipline as :mod:`tests.test_operations_composite_backing`: clear
    to a known synthetic surface for the test, then restore the entries the
    gh-rest package registered at import so a sibling module relying on them
    is not left with an empty registry.
    """
    saved = dict(_backing._REGISTRY)
    reset_composite_backing_registry()
    yield
    reset_composite_backing_registry()
    _backing._REGISTRY.update(saved)


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
# platform-wide sweep + github routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_raises_for_registered_synthetic_composite() -> None:
    """The registry sweep applies the primitive to every registered backing."""
    await _seed_descriptor(op_id=_SUB_OP_A, source_kind="ingested")
    register_composite_backing(
        composite_op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_SUB_OP_A,),
        catalog_command="meho connector ingest --catalog synth/1.0",
    )
    with pytest.raises(IngestedDispatchDependencyError):
        await assert_registered_composites_have_no_ingested_dispatch()


@pytest.mark.asyncio
async def test_sweep_passes_for_code_shipped_only_registry() -> None:
    """A registry whose composites only depend on code-shipped sub-ops passes."""
    await _seed_descriptor(op_id=_TYPED_SUB_OP, source_kind="typed")
    register_composite_backing(
        composite_op_id="synth.composite.widget.audit",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_TYPED_SUB_OP,),
        catalog_command="meho connector ingest --catalog synth/1.0",
    )
    # No raise.
    await assert_registered_composites_have_no_ingested_dispatch()


@pytest.mark.asyncio
async def test_github_real_backing_routed_through_shared_invariant() -> None:
    """github's real composite backing is swept by the shared invariant and passes fresh.

    Restores the import-time gh-rest backing (the autouse fixture cleared
    it), then runs the platform-wide sweep with no gh L2 rows ingested —
    the fresh-deploy state. The sweep must pass, proving github is folded
    into the one shared check (its bespoke ``UnbackedEnabledCompositeError``
    guard is retired separately in #2259) rather than needing per-connector
    wiring here.
    """
    # Re-register github's real backing (fixture cleared the registry).
    register_composite_backing(
        composite_op_id=_GH_COMPOSITE_OP_ID,
        connector_id="gh-rest-3",
        sub_op_ids=(
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}",
            "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
            "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        ),
        catalog_command="meho connector ingest --catalog gh/3",
    )
    assert _GH_COMPOSITE_OP_ID in registered_composite_backings()
    # Fresh deploy: no gh L2 rows ingested -> the code-shipped composite is
    # not (yet) routed through an ingested row, so the sweep passes.
    await assert_registered_composites_have_no_ingested_dispatch()
