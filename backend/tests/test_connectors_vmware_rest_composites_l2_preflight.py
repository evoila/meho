# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the L2 dependency pre-flight check (G0.14-T10 / #1151).

Coverage matrix:

* Cache miss + all L2 sub-ops present -> :func:`preflight_l2_dependencies`
  returns ``None`` and populates the cache; second call short-circuits.
* Cache miss + at least one L2 sub-op missing -> raises
  :class:`CompositeL2DependencyMissing` with every missing op_id listed
  and the catalog command resolved.
* Cache miss + at least one L2 sub-op present-but-disabled -> raises
  :class:`CompositeL2DependencyDisabled` with the disabled op_ids +
  connector_id, NOT ``CompositeL2DependencyMissing`` (#1601). Mixed
  disabled+absent walks raise the disabled exception (documented
  precedence). The ``composite_l2_disabled`` result shape names the real
  per-op enable verb and references no fabricated group-level verb.
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
from meho_backplane.operations import (
    CompositeL2DependencyDisabled,
    CompositeL2DependencyMissing,
)
from meho_backplane.operations._errors import (
    result_composite_l2_disabled,
    result_composite_l2_missing,
)

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
    disabled: set[str] | None = None,
) -> list[str]:
    """Stub the lookup helpers as if ``present`` is enabled and ``disabled`` is ingested-but-off.

    ``present`` is the set of op-ids :func:`lookup_descriptor` resolves
    (an enabled descriptor row). ``disabled`` is the set of op-ids that
    have a descriptor row but ``is_enabled = false``: invisible to
    ``lookup_descriptor`` (returns ``None``) yet present to the
    ``is_enabled``-agnostic :func:`descriptor_exists_any_state` probe.
    Any op-id in neither set is truly absent (no row in any state).

    Returns the list of sub-op-ids :func:`lookup_descriptor` was called
    against (read by the caller to assert call shape).
    """
    disabled = disabled or set()
    calls: list[str] = []

    async def _stub_lookup_descriptor(
        *, tenant_id: Any, product: str, version: str, impl_id: str, op_id: str
    ) -> object | None:
        calls.append(op_id)
        if op_id in present:
            return object()  # truthy non-None descriptor stand-in (enabled row).
        return None

    async def _stub_descriptor_exists_any_state(
        *, tenant_id: Any, product: str, version: str, impl_id: str, op_id: str
    ) -> bool:
        # Present in any state == enabled OR disabled; absent otherwise.
        return op_id in present or op_id in disabled

    monkeypatch.setattr(
        _preflight,
        "lookup_descriptor",
        _stub_lookup_descriptor,
    )
    monkeypatch.setattr(
        _preflight,
        "descriptor_exists_any_state",
        _stub_descriptor_exists_any_state,
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
       diagnostic framing (curation gap), escape-hatch recipe
       (catalog command), and the two doc references (strategic
       framing + vmware-rest L1+L2 dispatch contract).
    3. Structured ``extras`` payload with the structured fields
       (``missing_op_ids``, ``catalog_command``).

    G0.16-T1 (#1303) reframed the human message: the catalog command
    is no longer described as "the remediation path" (which read as
    a recommendation in v0.8.0 and operators followed it into a pod
    crash before #1303 landed the safe shape); it is now described
    as the OpenAPI escape hatch with the L1-wrapper request as the
    recommended path. The structured ``data`` payload is unchanged
    -- agents that branch on ``error_code`` + ``catalog_command``
    keep working without migration.
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
    # Strategic framing doc must be referenced -- the v0.9 reframe
    # is the source of truth for "OpenAPI is the escape hatch, not
    # the daily-driver".
    assert "docs/codebase/api-shape-conventions.md" in out.error
    # The vmware-rest L1+L2 dispatch contract reference stays --
    # operators digging into the diagnostic need both docs.
    assert "docs/codebase/connectors-vmware-rest.md" in out.error
    # Diagnostic framing: the message must call out the curation
    # gap (and name the L1-wrapper request as the recommended path)
    # rather than recommending the catalog command outright.
    assert "curated" in out.error.lower()
    assert "escape hatch" in out.error.lower()
    assert "L1 wrapper" in out.error
    assert out.extras == {
        "error_code": "composite_l2_missing",
        "missing_op_ids": ["GET:/vcenter/datastore", "GET:/vcenter/vm"],
        "catalog_command": "meho connector ingest --catalog vmware/9.0",
    }


# ---------------------------------------------------------------------------
# Disabled vs absent classification (#1601)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_disabled_sub_op_raises_disabled_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ingested-but-disabled sub-op yields ``CompositeL2DependencyDisabled``.

    Acceptance criterion (#1601): an L2 sub-op present in
    ``endpoint_descriptor`` with ``is_enabled = false`` must classify as
    *disabled* (re-enable remediation), NOT *missing* (re-ingest
    remediation).
    """
    _patch_lookup(
        monkeypatch,
        present=set(),
        disabled={"GET:/vcenter/datastore", "GET:/vcenter/datastore/{datastore}"},
    )
    sub_ops: tuple[str, ...] = (
        "GET:/vcenter/datastore",
        "GET:/vcenter/datastore/{datastore}",
    )
    with pytest.raises(CompositeL2DependencyDisabled) as exc_info:
        await preflight_l2_dependencies(
            composite_op_id="vmware.composite.datastore.usage",
            sub_op_ids=sub_ops,
            connector_id="vmware-rest-9.0",
            tenant_id=_TENANT_ID,
        )
    exc = exc_info.value
    assert exc.composite_op_id == "vmware.composite.datastore.usage"
    assert exc.disabled_op_ids == sub_ops
    assert exc.connector_id == "vmware-rest-9.0"
    # Negative result NOT cached: re-enabling then retrying must re-walk.
    assert "vmware.composite.datastore.usage" not in _preflight._PREFLIGHT_CACHE


@pytest.mark.asyncio
async def test_preflight_absent_sub_op_still_raises_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-op with no descriptor row in any state still yields ``...Missing``.

    Acceptance criterion (#1601): the missing-path regression is
    preserved -- a truly-absent op keeps the catalog-ingest remediation.
    """
    _patch_lookup(monkeypatch, present=set(), disabled=set())
    sub_ops: tuple[str, ...] = ("GET:/vcenter/datastore", "GET:/vcenter/vm")
    with pytest.raises(CompositeL2DependencyMissing) as exc_info:
        await preflight_l2_dependencies(
            composite_op_id="vmware.composite.datastore.usage",
            sub_op_ids=sub_ops,
            connector_id="vmware-rest-9.0",
            tenant_id=_TENANT_ID,
        )
    exc = exc_info.value
    assert exc.missing_op_ids == sub_ops
    assert exc.catalog_command == "meho connector ingest --catalog vmware/9.0"


@pytest.mark.asyncio
async def test_preflight_mixed_disabled_and_absent_prefers_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed walk (one disabled, one absent): disabled takes documented precedence.

    Acceptance criterion (#1601): the mixed case has documented
    precedence -- only one exception surfaces, and it is the disabled
    one (the re-enable remediation a default ingested-but-disabled deploy
    needs). The disabled payload carries only the disabled op, not the
    absent one.
    """
    _patch_lookup(
        monkeypatch,
        present={"GET:/vcenter/datastore"},  # enabled, dispatchable
        disabled={"GET:/vcenter/datastore/{datastore}"},  # ingested-but-off
        # "GET:/vcenter/vm" is in neither set -> truly absent.
    )
    sub_ops: tuple[str, ...] = (
        "GET:/vcenter/datastore",
        "GET:/vcenter/datastore/{datastore}",
        "GET:/vcenter/vm",
    )
    with pytest.raises(CompositeL2DependencyDisabled) as exc_info:
        await preflight_l2_dependencies(
            composite_op_id="vmware.composite.datastore.usage",
            sub_op_ids=sub_ops,
            connector_id="vmware-rest-9.0",
            tenant_id=_TENANT_ID,
        )
    # Only the disabled op surfaces; the enabled op is dispatchable and
    # the absent op is subsumed by the higher-precedence disabled signal.
    assert exc_info.value.disabled_op_ids == ("GET:/vcenter/datastore/{datastore}",)


def test_result_composite_l2_disabled_shape_matches_t11_convention() -> None:
    """The disabled result complies with ``docs/codebase/error-message-shape.md``.

    Three load-bearing fields per the convention: a stable
    ``composite_l2_disabled`` code, a human message naming the disabled
    ops + the real per-op enable verb + the connector-level caveat + a
    doc reference, and a structured ``extras`` payload.
    """
    out = result_composite_l2_disabled(
        op_id="vmware.composite.datastore.usage",
        disabled_op_ids=("GET:/vcenter/datastore", "GET:/vcenter/vm"),
        connector_id="vmware-rest-9.0",
        duration_ms=1.0,
    )
    assert out.status == "error"
    assert out.op_id == "vmware.composite.datastore.usage"
    assert out.error is not None
    assert "composite_l2_disabled" in out.error
    assert "GET:/vcenter/datastore" in out.error
    assert "GET:/vcenter/vm" in out.error
    # Names the REAL per-op enable verb (deterministic remediation).
    assert "meho connector edit-op vmware-rest-9.0 <op_id> --enable" in out.error
    # Names the connector-level verb only as the caveated alternative.
    assert "meho connector enable vmware-rest-9.0" in out.error
    # Must NOT steer the operator back to ingest -- the catalog is
    # already ingested in the disabled state.
    assert "connector ingest" not in out.error
    # Must NOT reference the fabricated group-level enable verb.
    assert "edit_group" not in out.error
    assert "edit-group" not in out.error
    assert "docs/codebase/connectors-vmware-rest.md" in out.error
    assert out.extras == {
        "error_code": "composite_l2_disabled",
        "disabled_op_ids": ["GET:/vcenter/datastore", "GET:/vcenter/vm"],
        "connector_id": "vmware-rest-9.0",
    }


def test_disabled_remediation_references_no_fabricated_verb() -> None:
    """No fabricated group-level enable verb anywhere in shipped backend/ or docs/.

    Acceptance criterion (#1601): the disabled remediation must name only
    verbs that exist. The group-level enable verb the original report
    proposed does not exist and must appear nowhere in shipped source or
    docs. The grep mirrors the criterion's
    ``edit_group --enable\\|edit-group --enable`` pattern, excluding
    Python bytecode and *this test file* -- the latter necessarily spells
    the forbidden tokens as its own search needles.
    """
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            "grep",
            "-rn",
            "--include=*.py",
            "--include=*.go",
            "--include=*.md",
            "--exclude=test_connectors_vmware_rest_composites_l2_preflight.py",
            "-e",
            "edit_group --enable",
            "-e",
            "edit-group --enable",
            str(repo_root / "backend"),
            str(repo_root / "docs"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, f"fabricated group-level enable verb found:\n{proc.stdout}"
