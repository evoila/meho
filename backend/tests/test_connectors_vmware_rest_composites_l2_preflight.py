# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the L2 dependency pre-flight check (G0.14-T10 / #1151).

Coverage matrix:

* Cache miss + all L2 sub-ops present -> :func:`preflight_l2_dependencies`
  returns ``None`` and populates the cache; second call short-circuits.
* Cache miss + at least one L2 sub-op missing -> raises
  :class:`CompositeL2DependencyMissing` with every missing op_id listed
  and the catalog command resolved.
* Negative result is NOT cached -- a subsequent call after the catalog
  is ingested sees the up-to-date state.
* Composite-to-composite sub-ops (``vmware.composite.*``) are skipped
  by the pre-flight (they cannot fail this way; their handlers run
  their own pre-flight).
* The pre-flight runs once per composite-op_id per process -- repeated
  calls for the same composite are O(1) cache hits.
* The dispatcher (via :func:`~meho_backplane.operations.dispatcher.dispatch`)
  converts the structured exception into a
  :func:`~meho_backplane.operations._errors.result_composite_l2_missing`
  shape. The shape complies with the T11 convention
  (``docs/codebase/error-message-shape.md``): stable code, human-readable
  message naming missing ops + the catalog command, structured
  ``extras`` payload.

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

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _preflight
from meho_backplane.connectors.vmware_rest.composites._preflight import (
    preflight_l2_dependencies,
    reset_preflight_cache,
)
from meho_backplane.connectors.vmware_rest.composites._read import (
    datastore_usage_composite,
)
from meho_backplane.operations import CompositeL2DependencyMissing
from meho_backplane.operations._errors import result_composite_l2_missing

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


_TENANT_ID = UUID("00000000-0000-0000-0000-00000000beef")


def _make_operator() -> Operator:
    """Synthetic operator for pre-flight tests."""
    return Operator(
        sub="op-preflight-test",
        name="Preflight Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


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
    """Stub :func:`lookup_descriptor` to behave as if ``present`` is the registered set.

    Returns the list of sub-op-ids the stub was called against (read
    by the caller to assert call shape).
    """
    calls: list[str] = []

    async def _stub_lookup_descriptor(
        *, tenant_id: Any, product: str, version: str, impl_id: str, op_id: str
    ) -> object | None:
        calls.append(op_id)
        if op_id in present:
            return object()  # truthy non-None descriptor stand-in.
        return None

    monkeypatch.setattr(
        _preflight,
        "lookup_descriptor",
        _stub_lookup_descriptor,
    )
    return calls


# ---------------------------------------------------------------------------
# Cache miss / cache hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_all_sub_ops_present_returns_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: every sub-op resolves; second call is a cache hit."""
    calls = _patch_lookup(
        monkeypatch,
        present={"GET:/vcenter/datastore", "GET:/vcenter/datastore/{datastore}"},
    )
    composite_op_id = "vmware.composite.datastore.usage"
    sub_ops: tuple[str, ...] = (
        "GET:/vcenter/datastore",
        "GET:/vcenter/datastore/{datastore}",
    )
    # First call walks the DB.
    await preflight_l2_dependencies(
        composite_op_id=composite_op_id,
        sub_op_ids=sub_ops,
        connector_id="vmware-rest-9.0",
        tenant_id=_TENANT_ID,
    )
    assert calls == list(sub_ops)
    assert composite_op_id in _preflight._PREFLIGHT_CACHE

    # Second call: no extra calls.
    await preflight_l2_dependencies(
        composite_op_id=composite_op_id,
        sub_op_ids=sub_ops,
        connector_id="vmware-rest-9.0",
        tenant_id=_TENANT_ID,
    )
    assert calls == list(sub_ops), "cache hit on second call -> no extra lookups"


@pytest.mark.asyncio
async def test_preflight_missing_sub_op_raises_with_full_missing_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing L2: every absent op id surfaces in one exception payload."""
    _patch_lookup(
        monkeypatch,
        present={"GET:/vcenter/datastore"},  # only one of three present
    )
    sub_ops: tuple[str, ...] = (
        "GET:/vcenter/datastore",
        "GET:/vcenter/datastore/{datastore}",
        "GET:/vcenter/vm",
    )
    with pytest.raises(CompositeL2DependencyMissing) as exc_info:
        await preflight_l2_dependencies(
            composite_op_id="vmware.composite.datastore.usage",
            sub_op_ids=sub_ops,
            connector_id="vmware-rest-9.0",
            tenant_id=_TENANT_ID,
        )
    exc = exc_info.value
    assert exc.composite_op_id == "vmware.composite.datastore.usage"
    # Both missing ops are surfaced; ordering matches sub-op declaration.
    assert exc.missing_op_ids == (
        "GET:/vcenter/datastore/{datastore}",
        "GET:/vcenter/vm",
    )
    assert exc.catalog_command == "meho connector ingest --catalog vmware/9.0"
    # Negative result NOT cached: a retry after operator ingestion must
    # re-walk and pass.
    assert "vmware.composite.datastore.usage" not in _preflight._PREFLIGHT_CACHE


@pytest.mark.asyncio
async def test_preflight_skips_composite_sub_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vmware.composite.*`` sub-ops are not walked (no DB calls)."""
    calls = _patch_lookup(monkeypatch, present=set())
    await preflight_l2_dependencies(
        composite_op_id="vmware.composite.host.evacuate",
        # Only a composite-to-composite sub-op + nothing else.
        sub_op_ids=("vmware.composite.vm.migrate",),
        connector_id="vmware-rest-9.0",
        tenant_id=_TENANT_ID,
    )
    assert calls == [], "composite sub-ops are skipped, not walked"
    # Cache key still landed (subsequent calls are O(1)).
    assert "vmware.composite.host.evacuate" in _preflight._PREFLIGHT_CACHE


@pytest.mark.asyncio
async def test_preflight_composite_and_raw_mix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed sub-op tuples: raw ops walked; composite ops skipped."""
    calls = _patch_lookup(
        monkeypatch,
        present={"GET:/vcenter/vm", "PATCH:/vcenter/host/{host}/maintenance?action=enter"},
    )
    await preflight_l2_dependencies(
        composite_op_id="vmware.composite.host.evacuate",
        sub_op_ids=(
            "GET:/vcenter/vm",
            "vmware.composite.vm.migrate",  # skipped
            "PATCH:/vcenter/host/{host}/maintenance?action=enter",
        ),
        connector_id="vmware-rest-9.0",
        tenant_id=_TENANT_ID,
    )
    assert calls == [
        "GET:/vcenter/vm",
        "PATCH:/vcenter/host/{host}/maintenance?action=enter",
    ]


@pytest.mark.asyncio
async def test_preflight_cache_per_composite_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache is per composite-op_id -- one composite's hit doesn't speak for another."""
    calls = _patch_lookup(monkeypatch, present={"GET:/vcenter/datastore"})
    await preflight_l2_dependencies(
        composite_op_id="vmware.composite.datastore.usage",
        sub_op_ids=("GET:/vcenter/datastore",),
        connector_id="vmware-rest-9.0",
        tenant_id=_TENANT_ID,
    )
    assert calls == ["GET:/vcenter/datastore"]
    # Sibling composite that shares one sub-op -- still walks because its
    # own cache key isn't primed.
    await preflight_l2_dependencies(
        composite_op_id="vmware.composite.event.tail",
        sub_op_ids=("GET:/vcenter/datastore",),
        connector_id="vmware-rest-9.0",
        tenant_id=_TENANT_ID,
    )
    assert calls == ["GET:/vcenter/datastore", "GET:/vcenter/datastore"]


# ---------------------------------------------------------------------------
# Composite handler integration
# ---------------------------------------------------------------------------


class _UnusedDispatchChild:
    """``dispatch_child`` stand-in that asserts it's never called.

    The pre-flight should raise *before* any sub-op dispatch fires --
    this stand-in fails the test if the composite proceeds past
    pre-flight when a sub-op is missing.
    """

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        raise AssertionError(
            f"dispatch_child should never be called when preflight failed; "
            f"got call for op_id={op_id!r}"
        )


@pytest.mark.asyncio
async def test_datastore_usage_composite_raises_when_l2_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composite handler raises before any ``dispatch_child`` call.

    Acceptance criterion (issue body): the chosen option's pre-flight
    must produce a structured error (here: the
    :class:`CompositeL2DependencyMissing` exception) listing the missing
    sub-ops + the catalog command before the composite touches its
    sub-op chain.
    """
    _patch_lookup(monkeypatch, present=set())  # no L2 ops registered
    with pytest.raises(CompositeL2DependencyMissing) as exc_info:
        await datastore_usage_composite(
            operator=_make_operator(),
            target=object(),
            params={},
            dispatch_child=_UnusedDispatchChild(),
        )
    # Every L2 sub-op the composite declares lands in the missing set.
    assert set(exc_info.value.missing_op_ids) == {
        "GET:/vcenter/datastore",
        "GET:/vcenter/datastore/{datastore}",
        "GET:/vcenter/vm",
    }
    assert exc_info.value.catalog_command == "meho connector ingest --catalog vmware/9.0"


# ---------------------------------------------------------------------------
# Structured result envelope (T11 convention)
# ---------------------------------------------------------------------------


def test_result_composite_l2_missing_shape_matches_t11_convention() -> None:
    """The structured result complies with ``docs/codebase/error-message-shape.md``.

    Three load-bearing fields per the convention:

    1. Stable code (``composite_l2_missing``) under
       ``extras["error_code"]``.
    2. Human-readable ``error`` message that names: missing op ids,
       remediation imperative (catalog command), doc reference.
    3. Structured ``extras`` payload with the structured fields
       (``missing_op_ids``, ``catalog_command``).
    """
    out = result_composite_l2_missing(
        op_id="vmware.composite.datastore.usage",
        missing_op_ids=("GET:/vcenter/datastore", "GET:/vcenter/vm"),
        catalog_command="meho connector ingest --catalog vmware/9.0",
        duration_ms=1.0,
    )
    assert out.status == "error"
    assert out.op_id == "vmware.composite.datastore.usage"
    assert out.error is not None
    assert "composite_l2_missing" in out.error
    assert "GET:/vcenter/datastore" in out.error
    assert "GET:/vcenter/vm" in out.error
    assert "meho connector ingest --catalog vmware/9.0" in out.error
    assert "docs/codebase/connectors-vmware-rest.md" in out.error
    assert out.extras == {
        "error_code": "composite_l2_missing",
        "missing_op_ids": ["GET:/vcenter/datastore", "GET:/vcenter/vm"],
        "catalog_command": "meho connector ingest --catalog vmware/9.0",
    }
