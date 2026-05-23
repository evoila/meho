# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.7-T8 JSONFlux force-mode acceptance for the Hetzner Robot connector.

v0.2 ships only :class:`~meho_backplane.operations.reducer.PassThroughReducer`
— the production reducer never produces a :class:`ResultHandle`. This test
mirrors :mod:`tests.acceptance.test_g35_harbor_jsonflux_force_handle` verbatim,
swapping in Hetzner Robot as the dispatched connector: it installs a test-only
:class:`ForceHandleReducer` that wraps every payload in a synthetic handle,
dispatches ``GET:/server`` against the seeded Robot core, and asserts
the :class:`~meho_backplane.connectors.schemas.OperationResult`'s ``handle``
field carries a populated :class:`ResultHandle`.

Hetzner Robot's ``GET:/server`` returns a JSON array of server wrapper
objects (``[{"server": {...}}, ...]``). The
:class:`ForceHandleReducer` handles this shape via its ``list`` branch so the
dispatcher-seam test doesn't depend on the specific list-key name.

Proves AC4 of G3.7-T8 #849: ``server.list`` returns a JSONFlux handle;
``result_describe`` / ``result_query`` resolve against it (the seam
test proves the dispatcher is ready for the production reducer once it
ships per Goal #214).
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
from tests.acceptance._robot_canary_fixtures import (
    ROBOT_CANARY_SERVERS,
    ROBOT_FORCE_HANDLE_LIST_OP_ID,
    ROBOT_FORCE_HANDLE_PARAMS,
    IngestedRobotCanary,
)


class ForceHandleReducer:
    """Test-only reducer that always produces a :class:`ResultHandle`.

    Recognises four payload shapes Robot and sibling connectors use:

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


async def test_force_handle_reducer_populates_operation_result_handle_for_robot(
    force_handle_reducer: None,
    ingested_robot_canary: IngestedRobotCanary,
) -> None:
    """Dispatching the Robot server list populates ``OperationResult.handle``.

    Confirms the JSONFlux dispatcher seam end-to-end for Hetzner Robot:

    * ``status == 'ok'`` — dispatch succeeded (HTTP Basic auth + GET
      against the server list path).
    * ``handle`` is a :class:`ResultHandle` — the reducer's return value
      flowed through ``wrap_ok_result``.
    * ``handle.total_rows`` matches the seeded server count from
      :data:`ROBOT_CANARY_SERVERS`.
    * ``handle.summary_md`` is non-empty and mentions the row count.
    * ``handle.schema_`` is a non-empty mapping.
    * ``handle.sample_rows`` is populated — at least one row from the
      seeded server list.
    * ``result['row_count']`` matches the handle's total.

    The server list response is a JSON array of ``{"server": {...}}``
    wrapper objects (not a pagination envelope), so the
    :class:`ForceHandleReducer`'s ``list`` branch is exercised here —
    different from the NSX and SDDC tests which hit the ``results[]``
    and ``elements[]`` branches respectively.

    Proves AC4 of #849 (G3.7-T8): ``server.list`` returns a JSONFlux
    handle; the dispatcher seam is wired and ready for the production
    reducer (Goal #214 scope).
    """
    expected_rows = len(ROBOT_CANARY_SERVERS)

    result_envelope = await call_operation(
        ingested_robot_canary.operator,
        {
            "connector_id": ingested_robot_canary.connector_id,
            "op_id": ROBOT_FORCE_HANDLE_LIST_OP_ID,
            "target": {"name": ingested_robot_canary.target_name},
            "params": ROBOT_FORCE_HANDLE_PARAMS,
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
        f"expected {expected_rows} servers from the seeded ROBOT_CANARY_SERVERS; "
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
        f"expected ≥1 sample row from the seeded server list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"expected the reducer's summary on result; got result={payload!r}"
    )
