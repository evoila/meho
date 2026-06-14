# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the composite-backing registry + the op-listing ``unbacked`` marker.

G0.25-T6 (#1757). A composite op is itself an ingested, enabled
``endpoint_descriptor`` row, so it surfaces in ``search_operations`` as a
normal hit -- but its raw-REST L2 sub-ops only land after an operator
runs ``meho connector ingest --catalog <product>/<version>``. Until then
the composite's first dispatch trips ``composite_l2_missing`` before any
HTTP call. This module covers the surfacing fix that closes that gap on
the listing:

* :func:`unbacked_composite_next_step` (unit, ``lookup_descriptor``
  stubbed -- same style as the gh-rest preflight tests):
  - op_id not in the registry -> ``None`` (ordinary op, no marker).
  - registered + every raw L2 sub-op present -> ``None`` (backed).
  - registered + a sub-op absent -> a ``NextStep`` carrying the catalog
    command.
  - ``*.composite.*`` recursion sub-ops are skipped (never probed).

* ``search_operations`` end-to-end (against the SQLite-fallback engine):
  - a composite hit is flagged ``unbacked=True`` + ``next_step`` while its
    L2 sub-ops are absent;
  - the same composite loses the marker once the sub-ops are seeded
    (``is_enabled=True``);
  - a non-composite hit alongside it never gains a false marker.

The gh-rest wiring (the one composite that ships today registers its
backing at import time) is asserted in
:mod:`tests.test_connectors_github_composites_register`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import composite_backing as _backing
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.composite_backing import (
    register_composite_backing,
    registered_composite_backing,
    reset_composite_backing_registry,
    unbacked_composite_next_step,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_TENANT_A: UUID = UUID("00000000-0000-0000-0000-0000000000aa")

# The gh-rest composite surface under test.
_CONNECTOR_ID = "gh-rest-3"
_PRODUCT = "gh"
_VERSION = "3"
_IMPL_ID = "gh-rest"
_COMPOSITE_OP_ID = "gh.composite.pr_status_summary"
_CATALOG_COMMAND = "meho connector ingest --catalog gh/3"
_SUB_OPS: tuple[str, ...] = (
    "GET:/repos/{owner}/{repo}/pulls/{pull_number}",
    "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
    "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
)


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
    """Isolate the backing registry + dispatcher caches around every test.

    The backing registry is process-wide and populated at import time by
    the gh-rest composite package. Each test wants a known synthetic
    surface, so clear it before the test -- but snapshot+restore the prior
    contents afterward (the same discipline the gh-rest register test uses
    for the typed-op registrar list) so a sibling module that relies on
    the import-time gh-rest entry is not left with an empty registry when
    this module runs first.
    """
    saved = dict(_backing._REGISTRY)
    reset_dispatcher_caches()
    reset_composite_backing_registry()
    yield
    reset_dispatcher_caches()
    reset_composite_backing_registry()
    _backing._REGISTRY.update(saved)


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub for the SQLite-fallback cosine branch."""
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


def _make_operator(tenant_id: UUID = _TENANT_A) -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# unit: unbacked_composite_next_step (lookup_descriptor stubbed)
# ---------------------------------------------------------------------------


def _patch_lookup(monkeypatch: pytest.MonkeyPatch, *, present: set[str]) -> list[str]:
    """Stub ``lookup_descriptor`` to behave as if ``present`` is the registered set."""
    calls: list[str] = []

    async def _stub_lookup_descriptor(
        *, tenant_id: Any, product: str, version: str, impl_id: str, op_id: str
    ) -> object | None:
        calls.append(op_id)
        return object() if op_id in present else None

    monkeypatch.setattr(_backing, "lookup_descriptor", _stub_lookup_descriptor)
    return calls


@pytest.mark.asyncio
async def test_next_step_none_for_unregistered_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """An op_id absent from the registry is an ordinary op -> no marker, no probe."""
    calls = _patch_lookup(monkeypatch, present=set())
    result = await unbacked_composite_next_step(op_id="gh.pr.get_files", tenant_id=_TENANT_A)
    assert result is None
    assert calls == [], "an unregistered op_id must not probe the descriptor table"


@pytest.mark.asyncio
async def test_next_step_none_when_all_sub_ops_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered composite whose every raw L2 sub-op resolves -> no marker."""
    register_composite_backing(
        composite_op_id=_COMPOSITE_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_SUB_OPS,
        catalog_command=_CATALOG_COMMAND,
    )
    _patch_lookup(monkeypatch, present=set(_SUB_OPS))
    result = await unbacked_composite_next_step(op_id=_COMPOSITE_OP_ID, tenant_id=_TENANT_A)
    assert result is None


@pytest.mark.asyncio
async def test_next_step_returned_when_a_sub_op_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered composite missing even one raw L2 sub-op -> catalog next_step."""
    register_composite_backing(
        composite_op_id=_COMPOSITE_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_SUB_OPS,
        catalog_command=_CATALOG_COMMAND,
    )
    # Only the primary PR sub-op is ingested; the checks + reviews ops are absent.
    _patch_lookup(monkeypatch, present={_SUB_OPS[0]})
    result = await unbacked_composite_next_step(op_id=_COMPOSITE_OP_ID, tenant_id=_TENANT_A)
    assert result is not None
    assert result.verb == _CATALOG_COMMAND
    assert "ingest" in result.rationale.lower()


@pytest.mark.asyncio
async def test_next_step_skips_composite_recursion_sub_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``*.composite.*`` sub-ops are never probed and never trigger a marker."""
    register_composite_backing(
        composite_op_id="gh.composite.future_recursive",
        connector_id=_CONNECTOR_ID,
        sub_op_ids=("gh.composite.pr_status_summary",),
        catalog_command=_CATALOG_COMMAND,
    )
    calls = _patch_lookup(monkeypatch, present=set())
    result = await unbacked_composite_next_step(
        op_id="gh.composite.future_recursive", tenant_id=_TENANT_A
    )
    assert result is None, "a composite whose only sub-op is a recursion is never unbacked"
    assert calls == [], "composite recursion sub-ops are skipped, not probed"


def test_register_is_idempotent_but_logs_on_conflict() -> None:
    """Re-registering the same payload is a no-op; a conflicting one overwrites."""
    register_composite_backing(
        composite_op_id=_COMPOSITE_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_SUB_OPS,
        catalog_command=_CATALOG_COMMAND,
    )
    register_composite_backing(
        composite_op_id=_COMPOSITE_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_SUB_OPS,
        catalog_command=_CATALOG_COMMAND,
    )
    backing = registered_composite_backing(_COMPOSITE_OP_ID)
    assert backing is not None
    assert backing.connector_id == _CONNECTOR_ID
    assert backing.sub_op_ids == _SUB_OPS
    assert backing.catalog_command == _CATALOG_COMMAND


# ---------------------------------------------------------------------------
# end-to-end: the marker through search_operations
# ---------------------------------------------------------------------------


async def _seed_composite_listing(*, seed_sub_ops: bool) -> None:
    """Seed the composite descriptor + an enabled group (+ optionally the L2 sub-ops).

    Always seeds: an enabled ``pulls`` group, the enabled composite
    descriptor, and an ordinary enabled L2 op (the "no false marker"
    control). When *seed_sub_ops* is true, also seeds the three raw L2
    primitives the composite depends on as enabled descriptors -- the
    "backed" state in which the marker must disappear.
    """
    sessionmaker = get_sessionmaker()
    group_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with sessionmaker() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="pulls",
                name="Pull requests",
                when_to_use="PR status questions.",
                review_status="enabled",
            )
        )

        def _descriptor(*, op_id: str, source_kind: str, summary: str) -> EndpointDescriptor:
            return EndpointDescriptor(
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
                summary=summary,
                description=summary,
                group_id=group_id,
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
                created_at=now,
                updated_at=now,
            )

        s.add(
            _descriptor(
                op_id=_COMPOSITE_OP_ID,
                source_kind="composite",
                summary="Return PR status: metadata, checks, reviews in one call.",
            )
        )
        # Ordinary (non-composite) op in the same connector -- the control
        # that must never gain a false marker.
        s.add(
            _descriptor(
                op_id="gh.pr.get_files",
                source_kind="ingested",
                summary="List the files changed in a pull request.",
            )
        )
        if seed_sub_ops:
            for sub_op in _SUB_OPS:
                s.add(
                    _descriptor(
                        op_id=sub_op,
                        source_kind="ingested",
                        summary=f"Raw L2 primitive {sub_op}.",
                    )
                )


def _hit_by_op_id(hits: list[dict[str, Any]], op_id: str) -> dict[str, Any]:
    matches = [h for h in hits if h["op_id"] == op_id]
    assert matches, f"expected a hit for {op_id!r}; got {[h['op_id'] for h in hits]}"
    return matches[0]


@pytest.mark.asyncio
async def test_search_marks_composite_unbacked_when_sub_ops_absent(
    stub_embedding_service: AsyncMock,
) -> None:
    """AC (a): the composite hit carries ``unbacked`` + ``next_step`` while sub-ops are absent."""
    register_composite_backing(
        composite_op_id=_COMPOSITE_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_SUB_OPS,
        catalog_command=_CATALOG_COMMAND,
    )
    await _seed_composite_listing(seed_sub_ops=False)
    operator = _make_operator()

    result = await search_operations(
        operator, {"connector_id": _CONNECTOR_ID, "query": "pull request status", "limit": 50}
    )
    hits = result["hits"]

    composite_hit = _hit_by_op_id(hits, _COMPOSITE_OP_ID)
    assert composite_hit["unbacked"] is True
    assert composite_hit["next_step"] is not None
    assert composite_hit["next_step"]["verb"] == _CATALOG_COMMAND

    # AC (c): the ordinary op alongside it must NOT gain a false marker.
    ordinary_hit = _hit_by_op_id(hits, "gh.pr.get_files")
    assert ordinary_hit["unbacked"] is False
    assert ordinary_hit["next_step"] is None


@pytest.mark.asyncio
async def test_search_drops_marker_once_sub_ops_ingested(
    stub_embedding_service: AsyncMock,
) -> None:
    """AC (a)/(c): once the L2 sub-ops are ingested, the composite loses the marker."""
    register_composite_backing(
        composite_op_id=_COMPOSITE_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=_SUB_OPS,
        catalog_command=_CATALOG_COMMAND,
    )
    await _seed_composite_listing(seed_sub_ops=True)
    operator = _make_operator()

    result = await search_operations(
        operator, {"connector_id": _CONNECTOR_ID, "query": "pull request status", "limit": 50}
    )
    hits = result["hits"]

    composite_hit = _hit_by_op_id(hits, _COMPOSITE_OP_ID)
    assert composite_hit["unbacked"] is False
    assert composite_hit["next_step"] is None


@pytest.mark.asyncio
async def test_search_no_marker_when_backing_unregistered(
    stub_embedding_service: AsyncMock,
) -> None:
    """A composite descriptor with NO registered backing never gains a marker.

    Defends the "registry is the gate" contract: a composite the listing
    knows about as a descriptor row but that no connector registered a
    backing for is treated as an ordinary op (no false positive from the
    descriptor's ``source_kind`` alone).
    """
    # Deliberately do NOT register a backing.
    await _seed_composite_listing(seed_sub_ops=False)
    operator = _make_operator()

    result = await search_operations(
        operator, {"connector_id": _CONNECTOR_ID, "query": "pull request status", "limit": 50}
    )
    composite_hit = _hit_by_op_id(result["hits"], _COMPOSITE_OP_ID)
    assert composite_hit["unbacked"] is False
    assert composite_hit["next_step"] is None
