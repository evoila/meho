# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration tests for the gh-rest composites (G3.11-T4 #1224, #2081).

Mirrors :mod:`tests.test_connectors_vmware_rest_composites_register`.
T4 shipped ``gh.composite.pr_status_summary``; #2081 adds the four
board + sub-issue composites. The per-composite assertions are the
same shape: ``source_kind="composite"`` row, the right ``safety_level``
+ ``requires_approval`` posture, canonical module-level ``handler_ref``,
group resolution (``pulls`` / ``board`` / ``issues``), parameter schema
round-trips with ``required`` keys and ``additionalProperties:false``,
response schema persists, tags, idempotent re-registration, and the
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
    project_item_add_composite,
    project_item_set_field_composite,
    project_view_composite,
    register_github_composite_operations,
    sub_issue_add_composite,
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

_EXPECTED_OP_IDS: tuple[str, ...] = (
    "gh.composite.pr_status_summary",
    "gh.composite.project_view",
    "gh.composite.project_item_add",
    "gh.composite.project_item_set_field",
    "gh.composite.sub_issue_add",
)

_EXPECTED_HANDLER_REF_BY_OP: dict[str, str] = {
    "gh.composite.pr_status_summary": (
        "meho_backplane.connectors.github.composites._read.pr_status_summary_composite"
    ),
    "gh.composite.project_view": (
        "meho_backplane.connectors.github.composites._board.project_view_composite"
    ),
    "gh.composite.project_item_add": (
        "meho_backplane.connectors.github.composites._board.project_item_add_composite"
    ),
    "gh.composite.project_item_set_field": (
        "meho_backplane.connectors.github.composites._board.project_item_set_field_composite"
    ),
    "gh.composite.sub_issue_add": (
        "meho_backplane.connectors.github.composites._sub_issues.sub_issue_add_composite"
    ),
}

_EXPECTED_GROUP_KEY_BY_OP: dict[str, str] = {
    "gh.composite.pr_status_summary": "pulls",
    "gh.composite.project_view": "board",
    "gh.composite.project_item_add": "board",
    "gh.composite.project_item_set_field": "board",
    "gh.composite.sub_issue_add": "issues",
}

# Governance posture per op: reads are safe, the three writes are caution;
# none floors to requires_approval (board-hygiene writes -- see _board /
# _sub_issues module docstrings).
_EXPECTED_SAFETY_BY_OP: dict[str, str] = {
    "gh.composite.pr_status_summary": "safe",
    "gh.composite.project_view": "safe",
    "gh.composite.project_item_add": "caution",
    "gh.composite.project_item_set_field": "caution",
    "gh.composite.sub_issue_add": "caution",
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
async def test_register_github_composite_operations_inserts_all_composites(
    stub_embedding_service: AsyncMock,
) -> None:
    """The registrar lands every gh-rest composite (pr-status + board + sub-issue)."""
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
    assert stub_embedding_service.encode_one.call_count == len(_EXPECTED_OP_IDS)


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
    """Running the registrar twice -> N rows persist; embeddings stay at N (skip-re-embed)."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    first_count = stub_embedding_service.encode_one.call_count
    assert first_count == len(_EXPECTED_OP_IDS)

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
    assert len(rows) == len(_EXPECTED_OP_IDS)


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


# ---------------------------------------------------------------------------
# Direct-session migration (#2255) + apparatus retirement (#2259): the
# composite reads through the ``GitHubRestConnector`` session directly, so it
# declares no descriptor-routed dispatch surface, and the gh-only
# ``UnbackedEnabledCompositeError`` load guard is gone (the platform-wide
# invariant #2252 is the sole remaining check).
# ---------------------------------------------------------------------------


def test_composite_declares_no_dispatch_surface() -> None:
    """The gh composite registers no descriptor-routed dispatch surface (#2259).

    ``gh.composite.pr_status_summary`` reads through the connector session
    directly, so it never resolves an ``endpoint_descriptor`` row and has no
    surface to declare to the two-world invariant's registry -- the empty
    registry is the fresh-deploy shape.
    """
    from meho_backplane.operations.composite_invariant import (
        registered_composite_dispatch_surfaces,
    )

    assert "gh.composite.pr_status_summary" not in registered_composite_dispatch_surfaces()


def test_register_module_has_no_backing_machinery() -> None:
    """``_register`` retired the gh-only backing guard + registrar (#2259)."""
    from meho_backplane.connectors.github.composites import _register as reg

    assert not hasattr(reg, "register_composite_backing")
    assert not hasattr(reg, "_register_and_assert_composite_backings")
    # The gh-only load guard is deleted; the platform invariant supersedes it.
    assert not hasattr(reg, "UnbackedEnabledCompositeError")


def test_handler_is_module_level_coroutine_function() -> None:
    """The handler is a plain module-level ``async def`` -- no closures / partials / lambdas.

    ``derive_handler_ref()`` rejects those at registration time, so a
    regression here would surface before the registrar even runs.
    """
    import inspect

    assert inspect.iscoroutinefunction(pr_status_summary_composite)
    assert "<locals>" not in pr_status_summary_composite.__qualname__
    assert pr_status_summary_composite.__qualname__ != "<lambda>"


# ---------------------------------------------------------------------------
# #2081 board (Projects-v2) + sub-issue composites
# ---------------------------------------------------------------------------

_NEW_OP_IDS: tuple[str, ...] = (
    "gh.composite.project_view",
    "gh.composite.project_item_add",
    "gh.composite.project_item_set_field",
    "gh.composite.sub_issue_add",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _NEW_OP_IDS)
async def test_new_composite_registers_with_expected_posture(
    op_id: str,
    stub_embedding_service: AsyncMock,
) -> None:
    """Each #2081 composite lands as a ``composite`` row with the right posture (AC #1/#2/#3).

    Asserts the descriptor is present (so ``list_operations`` / meta-tools
    surface it), routed to the expected group, carries the canonical
    module-level ``handler_ref``, the documented ``safety_level``, and
    ``requires_approval=False`` -- the board-hygiene write posture.
    """
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        descriptor = (
            await fresh.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
        ).scalar_one()
        group = (
            await fresh.execute(
                select(OperationGroup).where(OperationGroup.id == descriptor.group_id)
            )
        ).scalar_one()
    assert descriptor.source_kind == "composite"
    assert descriptor.tenant_id is None
    assert descriptor.is_enabled is True
    assert descriptor.product == "gh"
    assert descriptor.version == "3"
    assert descriptor.impl_id == "gh-rest"
    assert descriptor.handler_ref == _EXPECTED_HANDLER_REF_BY_OP[op_id]
    assert descriptor.safety_level == _EXPECTED_SAFETY_BY_OP[op_id]
    assert descriptor.requires_approval is False
    assert group.group_key == _EXPECTED_GROUP_KEY_BY_OP[op_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_key", "op_id"),
    [("board", "gh.composite.project_view"), ("issues", "gh.composite.sub_issue_add")],
)
async def test_new_groups_created_with_when_to_use(
    group_key: str,
    op_id: str,
    stub_embedding_service: AsyncMock,
) -> None:
    """The ``board`` and ``issues`` groups are created with a curated ``when_to_use`` (AC #3).

    ``list_operation_groups`` surfaces ``when_to_use`` verbatim so an LLM
    client can pick the right group before ``search_operations`` -- a
    missing / placeholder blurb would defeat the group selector.
    """
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        group = (
            await fresh.execute(
                select(OperationGroup).where(
                    OperationGroup.group_key == group_key,
                    OperationGroup.product == "gh",
                    OperationGroup.impl_id == "gh-rest",
                )
            )
        ).scalar_one()
    assert group.review_status == "enabled"
    assert isinstance(group.when_to_use, str) and group.when_to_use.strip()


@pytest.mark.asyncio
async def test_projectv2_and_sub_issue_ops_carry_discovery_tags(
    stub_embedding_service: AsyncMock,
) -> None:
    """Board ops tag ``projectv2``; the sub-issue op tags ``sub_issue`` (searchable surface)."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = {
            row.op_id: row
            for row in (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_NEW_OP_IDS))
                )
            )
            .scalars()
            .all()
        }
    assert "projectv2" in rows["gh.composite.project_item_add"].tags
    assert "projectv2" in rows["gh.composite.project_item_set_field"].tags
    assert "projectv2" in rows["gh.composite.project_view"].tags
    assert "sub_issue" in rows["gh.composite.sub_issue_add"].tags


@pytest.mark.asyncio
async def test_new_write_ops_persist_additional_properties_false_params(
    stub_embedding_service: AsyncMock,
) -> None:
    """Write composites' parameter schemas round-trip with ``additionalProperties:false``."""
    await register_github_composite_operations(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = {
            row.op_id: row
            for row in (
                await fresh.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_NEW_OP_IDS))
                )
            )
            .scalars()
            .all()
        }
    for op_id in _NEW_OP_IDS:
        schema = dict(rows[op_id].parameter_schema)
        assert schema["additionalProperties"] is False, op_id
        assert schema["required"], op_id


def test_new_handlers_are_module_level_coroutines() -> None:
    """The #2081 handlers are module-level ``async def`` -- no closures / lambdas / partials."""
    import inspect

    for handler in (
        project_view_composite,
        project_item_add_composite,
        project_item_set_field_composite,
        sub_issue_add_composite,
    ):
        assert inspect.iscoroutinefunction(handler), handler
        assert "<locals>" not in handler.__qualname__, handler
        assert handler.__qualname__ != "<lambda>", handler
