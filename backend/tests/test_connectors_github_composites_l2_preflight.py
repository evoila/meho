# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the gh-rest L2 dependency pre-flight check (G3.11-T4 #1224).

Mirrors :mod:`tests.test_connectors_vmware_rest_composites_l2_preflight`
(the G0.14-T10 / #1183 precedent). Coverage matrix:

* Cache miss + all L2 sub-ops present -> :func:`preflight_l2_dependencies`
  returns ``None`` and populates the cache; second call short-circuits.
* Cache miss + at least one L2 sub-op missing -> raises
  :class:`CompositeL2DependencyMissing` with every missing op_id listed
  and the catalog command resolved.
* Negative result is NOT cached -- a subsequent call after the catalog
  is ingested sees the up-to-date state.
* Composite-to-composite sub-ops (``gh.composite.*``) are skipped by
  the pre-flight (they cannot fail this way; their handlers run their
  own pre-flight).
* The pre-flight runs once per composite-op_id per process -- repeated
  calls for the same composite are O(1) cache hits.

The tests stub :func:`~meho_backplane.operations._lookup.lookup_descriptor`
via :class:`monkeypatch.setattr` so they don't require a populated
``endpoint_descriptor`` table; the contract under test is the helper's
walk + caching, not the descriptor table.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from meho_backplane.connectors.github.composites import _preflight
from meho_backplane.connectors.github.composites._preflight import (
    preflight_l2_dependencies,
    reset_preflight_cache,
)
from meho_backplane.operations import CompositeL2DependencyMissing

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000beef")


@pytest.fixture(autouse=True)
def _reset_cache_around_each_test() -> Iterator[None]:
    """Empty the pre-flight cache before + after every test in this module."""
    reset_preflight_cache()
    yield
    reset_preflight_cache()


def _patch_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    present: set[str],
) -> list[str]:
    """Stub :func:`lookup_descriptor` to behave as if ``present`` is the registered set."""
    calls: list[str] = []

    async def _stub_lookup_descriptor(
        *, tenant_id: Any, product: str, version: str, impl_id: str, op_id: str
    ) -> object | None:
        calls.append(op_id)
        if op_id in present:
            return object()  # truthy non-None descriptor stand-in
        return None

    monkeypatch.setattr(_preflight, "lookup_descriptor", _stub_lookup_descriptor)
    return calls


@pytest.mark.asyncio
async def test_preflight_all_sub_ops_present_returns_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: every sub-op resolves; second call is a cache hit."""
    sub_ops: tuple[str, ...] = (
        "GET:/repos/{owner}/{repo}/pulls/{pull_number}",
        "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
        "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    )
    calls = _patch_lookup(monkeypatch, present=set(sub_ops))
    composite_op_id = "gh.composite.pr_status_summary"

    await preflight_l2_dependencies(
        composite_op_id=composite_op_id,
        sub_op_ids=sub_ops,
        connector_id="gh-rest-3",
        tenant_id=_TENANT_ID,
    )
    assert calls == list(sub_ops)
    assert composite_op_id in _preflight._PREFLIGHT_CACHE

    # Second call: cache hit, no extra lookups.
    await preflight_l2_dependencies(
        composite_op_id=composite_op_id,
        sub_op_ids=sub_ops,
        connector_id="gh-rest-3",
        tenant_id=_TENANT_ID,
    )
    assert calls == list(sub_ops), "cache hit on second call -> no extra lookups"


@pytest.mark.asyncio
async def test_preflight_missing_sub_op_raises_with_full_missing_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing L2: every absent op_id surfaces in one exception payload."""
    _patch_lookup(
        monkeypatch,
        present={"GET:/repos/{owner}/{repo}/pulls/{pull_number}"},  # only one of three
    )
    sub_ops: tuple[str, ...] = (
        "GET:/repos/{owner}/{repo}/pulls/{pull_number}",
        "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
        "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    )
    with pytest.raises(CompositeL2DependencyMissing) as exc_info:
        await preflight_l2_dependencies(
            composite_op_id="gh.composite.pr_status_summary",
            sub_op_ids=sub_ops,
            connector_id="gh-rest-3",
            tenant_id=_TENANT_ID,
        )
    exc = exc_info.value
    assert exc.composite_op_id == "gh.composite.pr_status_summary"
    # Both missing ops surfaced; ordering matches sub-op declaration.
    assert exc.missing_op_ids == (
        "GET:/repos/{owner}/{repo}/commits/{ref}/check-runs",
        "GET:/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    )
    assert exc.catalog_command == "meho connector ingest --catalog gh/3"
    # Negative result NOT cached: a retry after operator ingestion must
    # re-walk and pass.
    assert "gh.composite.pr_status_summary" not in _preflight._PREFLIGHT_CACHE


@pytest.mark.asyncio
async def test_preflight_skips_composite_sub_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gh.composite.*`` sub-ops are not walked (no DB calls)."""
    calls = _patch_lookup(monkeypatch, present=set())
    await preflight_l2_dependencies(
        composite_op_id="gh.composite.future_recursive",
        sub_op_ids=("gh.composite.pr_status_summary",),
        connector_id="gh-rest-3",
        tenant_id=_TENANT_ID,
    )
    assert calls == [], "composite sub-ops are skipped, not walked"
    # Cache key still landed (subsequent calls are O(1)).
    assert "gh.composite.future_recursive" in _preflight._PREFLIGHT_CACHE


@pytest.mark.asyncio
async def test_preflight_re_runs_after_cache_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test seam: clearing the cache forces a re-walk on the next call."""
    sub_ops: tuple[str, ...] = ("GET:/repos/{owner}/{repo}/pulls/{pull_number}",)
    calls = _patch_lookup(monkeypatch, present=set(sub_ops))
    composite_op_id = "gh.composite.pr_status_summary"

    await preflight_l2_dependencies(
        composite_op_id=composite_op_id,
        sub_op_ids=sub_ops,
        connector_id="gh-rest-3",
        tenant_id=_TENANT_ID,
    )
    assert calls == list(sub_ops)
    reset_preflight_cache()
    await preflight_l2_dependencies(
        composite_op_id=composite_op_id,
        sub_op_ids=sub_ops,
        connector_id="gh-rest-3",
        tenant_id=_TENANT_ID,
    )
    # Reset forced a second walk -- same op observed twice.
    assert calls == list(sub_ops) + list(sub_ops)
