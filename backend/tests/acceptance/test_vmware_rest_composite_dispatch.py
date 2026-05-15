# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.1-T8 composite-dispatch test — exercises a read composite against vcsim.

Soft-dependent on G3.1-T5 (#508 — read composites). When the
``vmware.composite.datastore.usage`` op is registered against the
v2 registry (i.e. T5 has merged + been imported), this module
dispatches it against vcsim and asserts the aggregated payload
carries metadata for the 2 datastores in the seed topology. When
T5 hasn't merged yet (the default state at this Task's filing
time), the test skips with a clear reason citing the composite-
not-registered state.

This skip-don't-fail shape avoids a hard cross-PR dependency: T8
lands its scope (CI integration + JSONFlux + agent-flow) without
waiting on T5, and once T5 merges and re-runs CI, this test
flips from SKIPPED to PASS naturally.

Why this matters
================

Composites + ingested ops share the same dispatcher entry point
(:func:`meho_backplane.operations.dispatch`); the dispatcher
branches on :class:`EndpointDescriptor.source_kind`. A composite
end-to-end test proves the composite branch's recursive
``dispatch_child`` call path works against a real upstream
(vcsim), not just against in-process stubs (which the T5 unit
tests cover). Drift between the two paths is exactly the bug
class the acceptance suite exists to catch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._canary_fixtures import (
    CANARY_CONNECTOR_ID,
    CANARY_IMPL_ID,
    CANARY_PRODUCT,
    CANARY_VERSION,
    IngestedCanaryVcsim,
)

#: Composite op_id the test dispatches when T5 has merged.
#: Per T5 (#508) scope: aggregates per-datastore usage metadata
#: across every datastore vcenter knows about; v0.2 implementation
#: dispatches ``GET:/vcenter/datastore`` (list) followed by per-DS
#: ``GET:/vcenter/datastore/{ds}`` calls (detail). The composite's
#: dotted handle is the production-shipped op_id.
_COMPOSITE_OP_ID: str = "vmware.composite.datastore.usage"


async def _composite_registered() -> bool:
    """Return ``True`` iff T5's composite op_id has been registered.

    Two signals must both hold:

    * The v2 registry contains the
      ``(vmware, 9.0, vmware-rest)`` triple (always true once the
      ``vmware_rest`` package has been imported).
    * An :class:`EndpointDescriptor` row exists with
      ``source_kind='composite'`` + ``op_id=_COMPOSITE_OP_ID`` —
      T5 registers this via ``register_composite_operation()``.

    Both checks are read-only.
    """
    registry = all_connectors_v2()
    if (CANARY_PRODUCT, CANARY_VERSION, CANARY_IMPL_ID) not in registry:
        return False
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == CANARY_PRODUCT,
            EndpointDescriptor.version == CANARY_VERSION,
            EndpointDescriptor.impl_id == CANARY_IMPL_ID,
            EndpointDescriptor.op_id == _COMPOSITE_OP_ID,
            EndpointDescriptor.source_kind == "composite",
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None


async def test_composite_datastore_usage_against_vcsim(
    ingested_canary_vcsim: IngestedCanaryVcsim,
) -> None:
    """Dispatch the read composite against vcsim's 2-datastore seed.

    When T5 (#508) has merged, the composite is registered and the
    test dispatches it via ``call_operation``, asserts ``status='ok'``,
    and asserts the aggregated payload carries 2 datastores
    (matching :data:`tests.acceptance._vcsim.DEFAULT_VCSIM_TOPOLOGY.datastores`).

    When T5 hasn't merged yet, the test skips early. The skip path
    is deliberately verbose so a casual reader sees the soft-dep
    explanation without digging through the module docstring.
    """
    if not await _composite_registered():
        pytest.skip(
            f"Composite {_COMPOSITE_OP_ID!r} is not registered. This "
            "test soft-depends on G3.1-T5 (#508 read composites); the "
            "test will flip from SKIPPED to PASS naturally once #508 "
            "merges and its register_composite_operation() call runs "
            "at module-import time."
        )

    result_envelope = await call_operation(
        ingested_canary_vcsim.operator,
        {
            "connector_id": CANARY_CONNECTOR_ID,
            "op_id": _COMPOSITE_OP_ID,
            "target": {"name": ingested_canary_vcsim.target_name},
            "params": {},
        },
    )

    assert result_envelope["status"] == "ok", (
        f"composite dispatch did not succeed: {result_envelope!r}"
    )
    payload = result_envelope.get("result")
    assert payload is not None, f"expected aggregated payload on result; got {result_envelope!r}"

    # The composite's exact aggregate shape is T5's contract — we
    # don't know it from T8's vantage point. Assert on the loosest
    # invariant T5's body promises: the aggregate carries one entry
    # per datastore + the count matches vcsim's seed. T5 may surface
    # this as a list (``[{"datastore": "...", ...}, ...]``) or a
    # dict keyed by datastore (``{"datastore-1": {...}, ...}``);
    # support both.
    if isinstance(payload, list):
        ds_count = len(payload)
    elif isinstance(payload, dict):
        # Composite handlers commonly wrap the list under a top-level
        # ``datastores`` / ``items`` / ``value`` key. Pick whichever
        # T5 chose by checking the union.
        for key in ("datastores", "items", "value"):
            inner = payload.get(key)
            if isinstance(inner, list):
                ds_count = len(inner)
                break
        else:
            # Treat the dict as keyed-by-datastore.
            ds_count = len(payload)
    else:
        raise AssertionError(
            f"unexpected composite payload shape: {type(payload).__name__} body={payload!r}"
        )

    assert ds_count == 2, (
        f"expected 2 datastores from vcsim's seed topology; "
        f"got ds_count={ds_count} from payload={payload!r}"
    )
