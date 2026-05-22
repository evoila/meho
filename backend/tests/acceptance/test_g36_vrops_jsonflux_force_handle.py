# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.6-T2 JSONFlux force-mode acceptance for the vROps connector.

v0.2 ships only :class:`~meho_backplane.operations.reducer.PassThroughReducer`
— the production reducer never produces a :class:`ResultHandle`. The
real reducer (set-shaped payload reduction, MinIO/S3 spill,
``result_query`` / ``result_aggregate`` meta-tools) is **explicitly
out of scope** at the Goal #214 level.

This test mirrors :mod:`tests.acceptance.test_g35_nsx_jsonflux_force_handle`
verbatim, swapping in vROps as the dispatched connector: it installs
a test-only :class:`ForceHandleReducer` that wraps every payload in a
synthetic handle, dispatches ``GET:/suite-api/api/resources`` against
the seeded vROps core, and asserts the
:class:`~meho_backplane.connectors.schemas.OperationResult`'s
``handle`` field carries a populated :class:`ResultHandle`.

vROps' suite-api wraps list payloads under noun-specific keys
(``resourceList``, ``alerts``, ``symptoms``, etc.) rather than the
generic ``results`` / ``value`` keys NSX and vSphere use. The
:class:`ForceHandleReducer` below adds the noun-specific keys to its
discovery loop so the dispatcher-seam test still observes the
expected row count without leaking vendor-specific shape into the
production reducer ABC.
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
from tests.acceptance._vrops_canary_fixtures import (
    VROPS_CANARY_RESOURCES,
    VROPS_FORCE_HANDLE_LIST_OP_ID,
    IngestedVropsCanary,
)


class ForceHandleReducer:
    """Test-only reducer that always produces a :class:`ResultHandle`.

    Recognises both the cross-connector list shapes (``elements``,
    ``results``, ``value``) and the vROps-specific wrapper keys
    (``resourceList``, ``alerts``, ``alertDefinitions``, ``symptoms``,
    ``recommendations``, ``superMetrics``).

    * ``list`` → total = ``len(payload)``.
    * ``dict`` with one of the recognised list-wrapper keys → rows = the
      wrapped list.
    * Anything else → total = 1, sample = ().
    """

    _LIST_KEYS: tuple[str, ...] = (
        "elements",
        "results",
        "value",
        "resourceList",
        "alerts",
        "alertDefinitions",
        "symptoms",
        "recommendations",
        "superMetrics",
    )

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        """Always return ``(summary_dict, ResultHandle)``."""
        del schema, context
        if isinstance(payload, list):
            total = len(payload)
            sample = tuple(payload[:5]) if payload else ()
        elif isinstance(payload, dict):
            for key in self._LIST_KEYS:
                rows = payload.get(key)
                if isinstance(rows, list):
                    total = len(rows)
                    sample = tuple(rows[:5]) if rows else ()
                    break
            else:
                total = 1
                sample = ()
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
    """Install :class:`ForceHandleReducer` as the dispatcher's default."""
    set_default_reducer(ForceHandleReducer())
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_force_handle_reducer_populates_operation_result_handle_for_vrops(
    force_handle_reducer: None,
    ingested_vrops_canary: IngestedVropsCanary,
) -> None:
    """Dispatching the vROps resource list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for vROps:

    * ``status == 'ok'`` — dispatch succeeded (HTTP Basic + GET
      against the resource-list path).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return
      value flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` matches the seeded resource count from
      :data:`VROPS_CANARY_RESOURCES`.
    * ``handle.summary_md`` is non-empty and mentions the row count —
      the reducer composed its summary correctly.
    * ``handle.schema_`` is a non-empty mapping.
    * ``handle.sample_rows`` is populated — ≥1 row from the seeded
      resource list.
    * ``result['row_count']`` matches the handle's total — the
      dispatcher inlined the reducer's summary on the operation
      envelope (not the raw resource list).
    """
    resource_list = VROPS_CANARY_RESOURCES["resourceList"]
    assert isinstance(resource_list, list)
    expected_rows = len(resource_list)

    result_envelope = await call_operation(
        ingested_vrops_canary.operator,
        {
            "connector_id": ingested_vrops_canary.connector_id,
            "op_id": VROPS_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_vrops_canary.target_name},
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
    uuid.UUID(handle["handle_id"])
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} resources from the seeded VROPS_CANARY_RESOURCES; "
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
        f"expected ≥1 sample row from the seeded resource list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
