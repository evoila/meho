# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T8 JSONFlux force-mode acceptance for the Harbor connector.

v0.2 ships only :class:`~meho_backplane.operations.reducer.PassThroughReducer`
— the production reducer never produces a :class:`ResultHandle`. This test
mirrors :mod:`tests.acceptance.test_g35_sddc_jsonflux_force_handle` verbatim,
swapping in Harbor as the dispatched connector: it installs a test-only
:class:`ForceHandleReducer` that wraps every payload in a synthetic handle,
dispatches ``harbor.artifact.list`` against the seeded Harbor core, and asserts
the :class:`~meho_backplane.connectors.schemas.OperationResult`'s ``handle``
field carries a populated :class:`ResultHandle`.

Harbor's artifact list returns a plain JSON array (not a pagination envelope
like NSX's ``results[]`` or SDDC's ``elements[]``). The
:class:`ForceHandleReducer` handles this shape via its ``list`` branch so the
dispatcher-seam test doesn't depend on the specific list-key name.
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
from tests.acceptance._harbor_canary_fixtures import (
    HARBOR_CANARY_ARTIFACTS,
    HARBOR_FORCE_HANDLE_LIST_OP_ID,
    HARBOR_FORCE_HANDLE_PARAMS,
    IngestedHarborCanary,
)


class ForceHandleReducer:
    """Test-only reducer that always produces a :class:`ResultHandle`.

    Recognises four payload shapes Harbor and sibling connectors use:

    * ``list`` → total = ``len(payload)``.
    * ``dict`` with ``"elements"`` key (SDDC Manager paginated envelope).
    * ``dict`` with ``"results"`` key (NSX policy/manager API list shape).
    * ``dict`` with ``"value"`` key (vCenter REST list shape).
    * Anything else → total = 1, sample = ().
    """

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
            for key in ("elements", "results", "value"):
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


async def test_force_handle_reducer_populates_operation_result_handle_for_harbor(
    force_handle_reducer: None,
    ingested_harbor_canary: IngestedHarborCanary,
) -> None:
    """Dispatching the Harbor artifact list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for Harbor:

    * ``status == 'ok'`` — dispatch succeeded (HTTP Basic auth + GET
      against the artifact list path).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return value
      flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` matches the seeded artifact count from
      :data:`HARBOR_CANARY_ARTIFACTS`.
    * ``handle.summary_md`` is non-empty and mentions the row count.
    * ``handle.schema_`` is a non-empty mapping.
    * ``handle.sample_rows`` is populated — at least one row from the
      seeded artifact list.
    * ``result['row_count']`` matches the handle's total.

    The artifact list response is a plain JSON array (not a pagination
    envelope), so the :class:`ForceHandleReducer`'s ``list`` branch is
    exercised here — different from the NSX and SDDC tests which hit the
    ``results[]`` and ``elements[]`` branches respectively.
    """
    expected_rows = len(HARBOR_CANARY_ARTIFACTS)

    result_envelope = await call_operation(
        ingested_harbor_canary.operator,
        {
            "connector_id": ingested_harbor_canary.connector_id,
            "op_id": HARBOR_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_harbor_canary.target_name},
            "params": HARBOR_FORCE_HANDLE_PARAMS,
        },
    )

    assert result_envelope["status"] == "ok", (
        f"force-handle dispatch did not succeed: {result_envelope!r}"
    )

    handle = result_envelope.get("handle")
    assert handle is not None, (
        f"expected OperationResult.handle to be populated by ForceHandleReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )
    uuid.UUID(handle["handle_id"])
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} artifacts from the seeded HARBOR_CANARY_ARTIFACTS; "
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
        f"expected ≥1 sample row from the seeded artifact list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
