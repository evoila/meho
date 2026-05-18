# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T2 JSONFlux force-mode acceptance for the NSX connector.

v0.2 ships only :class:`~meho_backplane.operations.reducer.PassThroughReducer`
— the production reducer never produces a :class:`ResultHandle`. The
real reducer (set-shaped payload reduction, MinIO/S3 spill,
``result_query`` / ``result_aggregate`` meta-tools) is **explicitly
out of scope** at the Goal #214 level (*"Real JSONFlux reduction
logic … Out of scope. G0.6 ships the Reducer ABC hook only; v0.2
default is pass-through"*).

This test mirrors :mod:`tests.acceptance.test_vmware_rest_jsonflux_force_handle`
verbatim, swapping in NSX as the dispatched connector: it installs a
test-only :class:`ForceHandleReducer` that wraps every payload in a
synthetic handle, dispatches ``GET:/policy/api/v1/infra/segments``
against the seeded NSX core, and asserts the
:class:`~meho_backplane.connectors.schemas.OperationResult`'s
``handle`` field carries a populated :class:`ResultHandle` (UUID id,
non-empty summary_md, dict schema_, ≥1 sample row, ttl_seconds
set). The dispatcher seam between connector and reducer is what's
under test; the reducer's choice of *what to summarise* and *where
to spill* is a follow-on Initiative's job.

What this test does NOT cover
=============================

* Real reducer logic — MinIO/S3 spill, schema inference from the
  payload, the 50-row/4KB threshold heuristic. Those land with the
  production reducer.
* Audit-row ``handle_id`` propagation — v0.2's audit pipeline does
  not surface the handle's UUID on the audit row's
  :attr:`AuditLog.extras` field. Once the production reducer ships,
  the audit row's payload column gains a ``handle_id`` key; the
  assertion below is intentionally focused on the
  :class:`OperationResult` envelope rather than the audit-row shape
  to keep the test resilient to that future change.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from meho_backplane.connectors.schemas import ResultHandle
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._nsx_canary_fixtures import (
    NSX_CANARY_SEGMENTS,
    NSX_FORCE_HANDLE_LIST_OP_ID,
    IngestedNsxCanary,
)


class ForceHandleReducer:
    """Test-only reducer that always produces a :class:`ResultHandle`.

    Exists to exercise the JSONFlux dispatcher seam against a real
    response — proves the dispatcher calls the reducer, threads the
    handle through :func:`wrap_ok_result` onto the
    :class:`OperationResult.handle` field, and ships the summary
    on :attr:`OperationResult.result` (not the raw payload).

    Set-shaped payload handling matches the vSphere precedent
    (:class:`tests.acceptance.test_vmware_rest_jsonflux_force_handle.ForceHandleReducer`)
    with one NSX-specific addition: NSX list responses carry the
    rows under a ``"results"`` key (rather than vCenter REST's
    ``"value"``), so the dispatch helper recognises that shape too.

    * ``dict`` with ``"results"`` key (NSX policy/manager API list
      shape) → ``rows = payload["results"]``;
      ``total_rows = len(rows)``.
    * ``dict`` with ``"value"`` key (vCenter REST list shape) →
      ``rows = payload["value"]``; ``total_rows = len(rows)``.
    * ``list`` → ``total_rows = len(payload)``.
    * Anything else → ``total_rows = 1``, ``sample_rows = ()``.

    The handle's ``schema_`` is a placeholder ``{"type": "array",
    "items": {"type": "object"}}`` — a real reducer would infer the
    schema from the payload, but the dispatcher-seam test only
    needs a non-empty dict to pass the validator's frozen-after-
    construction requirement.
    """

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        """Always return ``(summary_dict, ResultHandle)``."""
        del schema, context  # synthetic reducer ignores both
        if isinstance(payload, dict) and "results" in payload:
            rows = payload["results"]
            if isinstance(rows, list):
                total = len(rows)
                sample = tuple(rows[:5]) if rows else ()
            else:
                total = 1
                sample = ()
        elif isinstance(payload, dict) and "value" in payload:
            rows = payload["value"]
            if isinstance(rows, list):
                total = len(rows)
                sample = tuple(rows[:5]) if rows else ()
            else:
                total = 1
                sample = ()
        elif isinstance(payload, list):
            total = len(payload)
            sample = tuple(payload[:5]) if payload else ()
        else:
            total = 1
            sample = ()

        handle = ResultHandle(
            handle_id=uuid.uuid4(),
            summary_md=f"force-mode handle ({total} rows)",
            schema_={"type": "array", "items": {"type": "object"}},
            total_rows=total,
            sample_rows=sample if sample else None,
            ttl_seconds=3600,
        )
        summary = {"row_count": total, "sample": list(sample)}
        return summary, handle


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`ForceHandleReducer` as the dispatcher's default.

    The reducer is module-level state on
    :mod:`meho_backplane.operations.dispatcher`; the test's setup
    swaps it in via :func:`set_default_reducer` and teardown restores
    :class:`PassThroughReducer` so a follow-on test in the same
    pytest session sees the v0.2 default behaviour.

    :func:`reset_dispatcher_caches` is called on teardown to drop
    any connector-instance cache the dispatch built up; the
    ``ingested_nsx_canary`` fixture's own teardown handles its
    own slice of that work, but explicit reset here keeps the
    fixture independent of evaluation order.
    """
    set_default_reducer(ForceHandleReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle_for_nsx(
    force_handle_reducer: None,
    ingested_nsx_canary: IngestedNsxCanary,
) -> None:
    """Dispatching the NSX segment list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for NSX:

    * ``status == 'ok'`` — dispatch succeeded (session-create + GET
      against ``/policy/api/v1/infra/segments``).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return
      value flowed through :func:`wrap_ok_result`.
    * ``handle.total_rows`` matches the seeded segment count from
      :data:`NSX_CANARY_SEGMENTS`.
    * ``handle.summary_md`` is non-empty and mentions the row count
      — the reducer composed its summary correctly.
    * ``handle.schema_`` is a non-empty mapping — the validator's
      frozen-after-construction guarantee held.
    * ``handle.sample_rows`` is populated — the reducer captured at
      least one row from the seeded segment list.
    * ``result['row_count']`` matches the handle's total — the
      dispatcher inlined the reducer's summary on the operation
      envelope (not the raw segment list).
    """
    expected_rows = len(NSX_CANARY_SEGMENTS["results"])  # type: ignore[arg-type]

    result_envelope = await call_operation(
        ingested_nsx_canary.operator,
        {
            "connector_id": ingested_nsx_canary.connector_id,
            "op_id": NSX_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_nsx_canary.target_name},
            "params": {},
        },
    )

    assert result_envelope["status"] == "ok", (
        f"force-handle dispatch did not succeed: {result_envelope!r}"
    )

    handle = result_envelope.get("handle")
    assert handle is not None, (
        f"expected OperationResult.handle to be populated by "
        f"ForceHandleReducer; got handle=None on envelope={result_envelope!r}"
    )
    uuid.UUID(handle["handle_id"])  # raises if not a valid UUID4 form
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} segments from the seeded NSX_CANARY_SEGMENTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )
    assert handle["summary_md"], "handle.summary_md must be non-empty"
    assert str(expected_rows) in handle["summary_md"], (
        f"expected summary_md to mention the row count; got {handle['summary_md']!r}"
    )
    assert isinstance(handle["schema_"], dict) and handle["schema_"], (
        f"handle.schema_ must be a non-empty mapping; got {handle['schema_']!r}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from the seeded NSX segment list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
