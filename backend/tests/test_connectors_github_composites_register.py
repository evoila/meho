# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration tests for the gh-rest composites (G3.11-T4 #1224).

Mirrors :mod:`tests.test_connectors_vmware_rest_composites_register`.
T4 ships exactly one composite -- ``gh.composite.pr_status_summary`` --
so the coverage scope is narrower than the vmware-rest precedent but
the per-composite assertions are identical: ``source_kind="composite"``
row, ``safety_level="safe"`` + ``requires_approval=False`` overrides,
canonical module-level ``handler_ref``, group resolution into
``pulls``, parameter schema round-trips with ``required`` keys and
``additionalProperties:false``, response schema persists, tags include
``composite`` + ``read-only``, idempotent re-registration, and the
side-effect import wires the registrar onto the lifespan list.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.github.composites import (
    pr_status_summary_composite,
    register_github_composite_operations,
)
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.typed_register import (
    _TYPED_OP_REGISTRARS,
    clear_typed_op_registrars,
)
from meho_backplane.settings import get_settings

_EXPECTED_OP_IDS: tuple[str, ...] = ("gh.composite.pr_status_summary",)

_EXPECTED_HANDLER_REF_BY_OP: dict[str, str] = {
    "gh.composite.pr_status_summary": (
        "meho_backplane.connectors.github.composites._read.pr_status_summary_composite"
    ),
}

_EXPECTED_GROUP_KEY_BY_OP: dict[str, str] = {
    "gh.composite.pr_status_summary": "pulls",
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
    """Snapshot+restore the typed-op registrar list around each test.

    Same discipline as the vmware-rest register test: a registrar-
    reload test mutates the list permanently and would mis-wire other
    lifespan-driven tests later in the session.
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
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Composite lands with the right shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_github_composite_operations_inserts_pr_status_summary(
    stub_embedding_service: AsyncMock,
) -> None:
    """The registrar lands ``gh.composite.pr_status_summary`` in ``endpoint_descriptor``."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
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
    assert stub_embedding_service.encode_one.call_count == 1


@pytest.mark.asyncio
async def test_composite_row_uses_safe_no_approval_overrides(
    stub_embedding_service: AsyncMock,
) -> None:
    """Row carries ``safety_level="safe"`` + ``requires_approval=False`` (issue body AC #5)."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
    assert row.safety_level == "safe", f"expected safe, got {row.safety_level!r}"
    assert row.requires_approval is False, (
        f"expected requires_approval=False, got {row.requires_approval!r}"
    )


@pytest.mark.asyncio
async def test_composite_row_carries_composite_source_kind(
    stub_embedding_service: AsyncMock,
) -> None:
    """Row has ``source_kind="composite"`` so the dispatcher routes to the composite branch."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
    assert row.source_kind == "composite"
    assert row.tenant_id is None
    assert row.is_enabled is True
    assert row.method is None
    assert row.path is None
    # Connector key triple matches the connector's v2 registration.
    assert row.product == "gh"
    assert row.version == "3"
    assert row.impl_id == "gh-rest"


@pytest.mark.asyncio
async def test_handler_ref_round_trips_to_module_level_dotted_path(
    stub_embedding_service: AsyncMock,
) -> None:
    """The persisted ``handler_ref`` is the canonical module-level dotted path."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
    assert row.handler_ref == _EXPECTED_HANDLER_REF_BY_OP["gh.composite.pr_status_summary"]


@pytest.mark.asyncio
async def test_group_resolution_lands_composite_in_pulls_group(
    stub_embedding_service: AsyncMock,
) -> None:
    """The composite lands in the ``pulls`` operation group."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        descriptor = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
        group_rows = (await fresh.execute(select(OperationGroup))).scalars().all()
    assert descriptor.group_id is not None
    group = next(g for g in group_rows if g.id == descriptor.group_id)
    assert group.group_key == "pulls"
    assert group.product == "gh"
    assert group.version == "3"
    assert group.impl_id == "gh-rest"


@pytest.mark.asyncio
async def test_parameter_schema_persists_with_required_fields(
    stub_embedding_service: AsyncMock,
) -> None:
    """``parameter_schema`` round-trips with ``required`` + ``additionalProperties:false``."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
    schema: dict[str, Any] = dict(row.parameter_schema)
    assert set(schema["required"]) == {"owner", "repo", "pull_number"}
    assert schema["additionalProperties"] is False
    props = dict(schema["properties"])
    assert {"owner", "repo", "pull_number"} <= set(props)


@pytest.mark.asyncio
async def test_response_schema_persists_with_documented_keys(
    stub_embedding_service: AsyncMock,
) -> None:
    """``response_schema`` carries the seven top-level keys the handler returns."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
    schema: dict[str, Any] = dict(row.response_schema)
    assert schema.get("type") == "object"
    expected_keys = {
        "pr",
        "checks",
        "reviews",
        "mergeable",
        "mergeable_state",
        "checks_status",
        "review_status",
    }
    assert expected_keys <= set(dict(schema["properties"]))
    assert set(schema["required"]) == expected_keys


@pytest.mark.asyncio
async def test_tags_include_composite_and_read_only(
    stub_embedding_service: AsyncMock,
) -> None:
    """The composite row's tags include ``composite`` + ``read-only`` for filtering."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "gh.composite.pr_status_summary"
                )
            )
        ).scalar_one()
    assert "composite" in row.tags
    assert "read-only" in row.tags
    assert "pulls" in row.tags


@pytest.mark.asyncio
async def test_register_github_composite_operations_is_idempotent(
    stub_embedding_service: AsyncMock,
) -> None:
    """Running the registrar twice -> 1 row persists; embedding stays at 1 (skip-re-embed)."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    first_count = stub_embedding_service.encode_one.call_count
    assert first_count == 1

    await register_github_composite_operations(embedding_service=stub_embedding_service)
    assert stub_embedding_service.encode_one.call_count == first_count, (
        "second run should hit the body-hash skip path"
    )

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
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Side-effect import wires the registrar into the lifespan list
# ---------------------------------------------------------------------------


def test_importing_github_composites_queues_registrar() -> None:
    """Importing :mod:`meho_backplane.connectors.github.composites` queues the registrar."""
    clear_typed_op_registrars()
    import meho_backplane.connectors.github.composites as composites_pkg

    importlib.reload(composites_pkg)

    assert any(
        r.__name__ == "register_github_composite_operations" for r in _TYPED_OP_REGISTRARS
    ), (
        "expected register_github_composite_operations on the typed-op "
        f"registrar list, got names "
        f"{[getattr(r, '__name__', repr(r)) for r in _TYPED_OP_REGISTRARS]}"
    )


def test_importing_github_composites_registers_l2_backing() -> None:
    """Import-time backing registration wires the composite's L2 surface (G0.25-T6 #1757).

    The op listing reads this registry to mark the composite ``unbacked``
    while its L2 sub-ops are absent. The registered ``sub_op_ids`` must be
    the SAME tuple the dispatch-time preflight walks (``_read.py``'s
    ``_SUB_OPS_PR_STATUS_SUMMARY``) and the ``catalog_command`` the same
    one ``CompositeL2DependencyMissing`` carries, so the listing and the
    dispatch can't drift.
    """
    from meho_backplane.connectors.github._catalog_command import (
        catalog_command_for_github_rest,
    )
    from meho_backplane.connectors.github.composites._read import (
        _CONNECTOR_ID,
        _SUB_OPS_PR_STATUS_SUMMARY,
    )
    from meho_backplane.operations.composite_backing import registered_composite_backing

    # The package import (already triggered by this module's top-level
    # imports) runs the import-time registration as a side effect.
    backing = registered_composite_backing("gh.composite.pr_status_summary")
    assert backing is not None, "expected gh.composite.pr_status_summary to register a backing"
    assert backing.connector_id == _CONNECTOR_ID
    assert backing.sub_op_ids == _SUB_OPS_PR_STATUS_SUMMARY
    assert backing.catalog_command == catalog_command_for_github_rest()


# ---------------------------------------------------------------------------
# Connector-load assertion: an enabled composite's L2 backing must be wired
# (#2050 -- guard against the enabled-but-composite_l2_missing regression)
# ---------------------------------------------------------------------------


def test_load_assertion_passes_on_the_shipped_specs() -> None:
    """The real load path registers + asserts without raising (fresh-deploy safe).

    The composite ships *enabled* with its raw L2 sub-ops uningested on a
    fresh deploy, so the guard must assert the *backing wiring* (the
    listing's ``unbacked`` safety net), not DB resolvability -- which is
    legitimately empty before ``meho connector ingest --catalog gh/3``.
    Re-running the load path is idempotent and must not raise.
    """
    from meho_backplane.connectors.github.composites import _register as reg

    # No exception, and every raw-L2 composite has a matching backing.
    reg._register_and_assert_composite_backings()

    from meho_backplane.operations.composite_backing import registered_composite_backing

    for spec in reg._COMPOSITES:
        if not reg._composite_dispatches_raw_l2(spec):
            continue
        backing = registered_composite_backing(spec.op_id)
        assert backing is not None
        assert backing.sub_op_ids == spec.sub_op_ids


def test_load_assertion_raises_when_enabled_raw_l2_composite_has_no_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw-L2 composite that reaches the upsert without a backing fails loudly.

    Simulates the regression the guard exists to catch: a future composite
    added to ``_COMPOSITES`` (or an edit that drops its backing
    registration) would otherwise ship *enabled* on the listing with no
    ``unbacked`` marker, dead-ending at ``composite_l2_missing`` on first
    dispatch. Neutering the backing registration leaves the assert loop
    with an empty registry, so the guard must raise.
    """
    from meho_backplane.connectors.github.composites import _register as reg
    from meho_backplane.operations import composite_backing as backing_mod

    saved = dict(backing_mod._REGISTRY)
    backing_mod._REGISTRY.clear()
    monkeypatch.setattr(reg, "register_composite_backing", lambda **_kwargs: None)
    try:
        with pytest.raises(reg.UnbackedEnabledCompositeError, match="registered no composite"):
            reg._register_and_assert_composite_backings()
    finally:
        backing_mod._REGISTRY.clear()
        backing_mod._REGISTRY.update(saved)


def test_load_assertion_raises_on_sub_op_id_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backing whose ``sub_op_ids`` drift from the spec fails loudly.

    The listing's ``unbacked`` probe and the dispatch-time preflight both
    read the sub-op tuple; a drift between the backing and the spec would
    make them disagree on what the composite hits. The guard catches it at
    connector load.
    """
    from meho_backplane.connectors.github.composites import _register as reg
    from meho_backplane.operations import composite_backing as backing_mod

    saved = dict(backing_mod._REGISTRY)
    backing_mod._REGISTRY.clear()

    def _register_drifting_backing(**_kwargs: object) -> None:
        raw = next(s for s in reg._COMPOSITES if reg._composite_dispatches_raw_l2(s))
        backing_mod.register_composite_backing(
            composite_op_id=raw.op_id,
            connector_id="gh-rest-3",
            sub_op_ids=(*raw.sub_op_ids, "GET:/repos/{owner}/{repo}/extra-drift"),
            catalog_command="meho connector ingest --catalog gh/3",
        )

    monkeypatch.setattr(reg, "register_composite_backing", _register_drifting_backing)
    try:
        with pytest.raises(reg.UnbackedEnabledCompositeError, match="drift"):
            reg._register_and_assert_composite_backings()
    finally:
        backing_mod._REGISTRY.clear()
        backing_mod._REGISTRY.update(saved)


def test_load_assertion_skips_recursion_only_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composite dispatching only ``gh.composite.*`` sub-ops needs no backing.

    Composite-to-composite recursion sub-ops are guaranteed by the
    lifespan registrar, never catalog-ingested, so the listing can never
    mark such a composite ``unbacked`` -- the guard must skip it even with
    an empty backing registry.
    """
    from meho_backplane.connectors.github.composites import _register as reg
    from meho_backplane.operations import composite_backing as backing_mod

    recursion_only = reg._CompositeSpec(
        op_id="gh.composite.only_recursion",
        handler=pr_status_summary_composite,
        summary="s",
        description="d",
        parameter_schema={},
        response_schema={},
        group_key="pulls",
        tags=["composite"],
        safety_level="safe",
        requires_approval=False,
        sub_op_ids=("gh.composite.pr_status_summary",),
    )
    assert reg._composite_dispatches_raw_l2(recursion_only) is False

    saved = dict(backing_mod._REGISTRY)
    backing_mod._REGISTRY.clear()
    monkeypatch.setattr(reg, "_COMPOSITES", (recursion_only,))
    monkeypatch.setattr(reg, "register_composite_backing", lambda **_kwargs: None)
    try:
        # No backing registered, but the recursion-only composite is skipped,
        # so the guard does not raise.
        reg._register_and_assert_composite_backings()
    finally:
        backing_mod._REGISTRY.clear()
        backing_mod._REGISTRY.update(saved)


def test_handler_is_module_level_coroutine_function() -> None:
    """The handler is a plain module-level ``async def`` -- no closures / partials / lambdas.

    ``derive_handler_ref()`` rejects those at registration time, so a
    regression here would surface before the registrar even runs.
    """
    import inspect

    assert inspect.iscoroutinefunction(pr_status_summary_composite)
    assert "<locals>" not in pr_status_summary_composite.__qualname__
    assert pr_status_summary_composite.__qualname__ != "<lambda>"
