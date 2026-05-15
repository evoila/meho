# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.1-T8 JSONFlux handle force-mode acceptance test.

v0.2 ships only :class:`~meho_backplane.operations.reducer.PassThroughReducer`
— the production reducer never produces a :class:`ResultHandle`. The
real reducer (set-shaped payload reduction, MinIO/S3 spill,
``result_query`` / ``result_aggregate`` meta-tools) lands in a
follow-on Initiative. Until then, the JSONFlux *seam* between
dispatcher and reducer is exercised here via a test-only
:class:`ForceHandleReducer` that wraps every payload in a synthetic
handle. The test:

* Dispatches ``GET:/vcenter/vm`` against vcsim's 50-VM seed.
* Asserts the returned :class:`~meho_backplane.connectors.schemas.OperationResult`
  carries a populated :attr:`OperationResult.handle` (UUID id,
  non-empty summary_md, dict schema_, ≥1 sample row, ttl_seconds set).
* Asserts the inlined :attr:`OperationResult.result` carries the
  reduced summary (row_count + sample) rather than the full set.

What the test does NOT cover
============================

* Real reducer logic — MinIO/S3 spill, schema inference from payload,
  the 50-row/4KB threshold heuristic. Those land with the production
  reducer.
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
from tests.acceptance._canary_fixtures import IngestedCanaryVcsim


class ForceHandleReducer:
    """Test-only reducer that always produces a :class:`ResultHandle`.

    Exists to exercise the JSONFlux dispatcher seam against a real
    response — proves the dispatcher calls the reducer, threads the
    handle through :func:`wrap_ok_result` onto the
    :class:`OperationResult.handle` field, and ships the summary
    on :attr:`OperationResult.result` (not the raw payload).

    Set-shaped payload handling:

    * ``dict`` with ``"value"`` key (vCenter REST's set-shape) →
      ``rows = payload["value"]``; ``total_rows = len(rows)``.
    * ``list`` → ``total_rows = len(payload)``.
    * Anything else → ``total_rows = 1``, ``sample_rows = ()``.

    The handle's ``schema_`` is a placeholder ``{"type": "array",
    "items": {"type": "object"}}`` — a real reducer would infer the
    schema from the payload, but the dispatcher seam test only needs
    a non-empty dict to pass the
    :class:`~meho_backplane.connectors.schemas.ResultHandle`
    validator's frozen requirement.
    """

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        """Always return ``(summary_dict, ResultHandle)``."""
        del schema, context  # synthetic reducer ignores both
        if isinstance(payload, dict) and "value" in payload:
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
    ``ingested_canary_vcsim`` fixture's own teardown handles its
    own slice of that work, but explicit reset here keeps the
    fixture independent of evaluation order.
    """
    set_default_reducer(ForceHandleReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle(
    force_handle_reducer: None,
    ingested_canary_vcsim: IngestedCanaryVcsim,
) -> None:
    """Dispatching ``GET:/vcenter/vm`` against vcsim populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam:

    * ``status == 'ok'`` — dispatch succeeded.
    * ``handle`` is a :class:`ResultHandle` — the reducer's return
      value flowed through :func:`wrap_ok_result`.
    * ``handle.total_rows == 50`` — matches vcsim's seeded
      :data:`DEFAULT_VCSIM_TOPOLOGY` VM count.
    * ``handle.summary_md`` is non-empty + mentions the row count —
      the reducer composed its summary correctly.
    * ``handle.schema_`` is a non-empty mapping — the validator's
      frozen-after-construction guarantee held.
    * ``handle.sample_rows`` is populated — the reducer captured at
      least one row from vcsim's response.
    * ``result['row_count'] == 50`` — the dispatcher inlined the
      reducer's summary on the operation envelope (not the raw
      50-VM list).
    """
    result_envelope = await call_operation(
        ingested_canary_vcsim.operator,
        {
            "connector_id": ingested_canary_vcsim.connector_id,
            "op_id": "GET:/vcenter/vm",
            "target": {"name": ingested_canary_vcsim.target_name},
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
    # ``model_dump`` ships the handle as a dict on the wire; the
    # assertions below match the JSON shape rather than re-instantiating
    # ResultHandle. handle_id is serialised as a UUID string by
    # :meth:`pydantic.BaseModel.model_dump(mode='json')`.
    uuid.UUID(handle["handle_id"])  # raises if not a valid UUID4 form
    assert handle["total_rows"] == 50, (
        f"expected 50 VMs from vcsim's seed topology; got handle.total_rows={handle['total_rows']}"
    )
    assert handle["summary_md"], "handle.summary_md must be non-empty"
    assert "50" in handle["summary_md"], (
        f"expected summary_md to mention the row count; got {handle['summary_md']!r}"
    )
    assert isinstance(handle["schema_"], dict) and handle["schema_"], (
        f"handle.schema_ must be a non-empty mapping; got {handle['schema_']!r}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from vcsim's 50-VM list; got sample_rows={sample_rows!r}"
    )

    # The inlined result is the reducer's summary, not the raw 50-VM
    # set. ``row_count`` matches the handle's total; ``sample`` is
    # the same list-of-dicts the handle's sample_rows carries (just
    # serialised differently — dict vs MappingProxyType).
    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == 50, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
