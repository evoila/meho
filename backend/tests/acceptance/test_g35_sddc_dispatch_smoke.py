# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T5 dispatch smoke — the 9 curated SDDC Manager read ops over respx.

Closes the substrate-proves-out half of the SDDC Manager v0.2 ship for
Initiative #368: each entry in
:data:`~meho_backplane.connectors.sddc_manager.core_ops.SDDC_CORE_OPS` is
dispatched against a respx-mocked SDDC Manager appliance and the dispatcher
returns a structured :class:`~meho_backplane.connectors.schemas.OperationResult`
for every one.

Why respx and not a real SDDC Manager target: SDDC Manager has no public CI
simulator. Per Initiative #368's DoD, SDDC Manager uses the vcsim-style
record/replay fixture pattern (a known limitation; documented in the
Initiative DoD). The full env-gated spec-canary ingest is a follow-up to
this Task (planned for G3.5-T6 #618); until then, the dispatch leg is
exercised directly against the curated descriptor set the
:data:`SDDC_CORE_OPS` constants describe.

Skip conditions: no Postgres — the ``pg_engine`` fixture skips when the
integration database isn't reachable.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.sddc_manager import SDDC_CORE_OPS
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._sddc_canary_fixtures import IngestedSddcCanary

SMOKE_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in SDDC_CORE_OPS)

#: Per-op parameter map. Only ``GET:/v1/domains/{id}`` carries a path
#: template (``{id}``) that needs substitution. The smoke passes
#: ``id=domain-mgmt`` to hit the registered respx route.
SMOKE_PARAMS: dict[str, dict[str, object]] = {
    "GET:/v1/domains/{id}": {"id": "domain-mgmt"},
}


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS, ids=lambda op: op)
async def test_dispatch_smoke_sddc_core_op_returns_ok(
    op_id: str,
    ingested_sddc_canary: IngestedSddcCanary,
) -> None:
    """Each curated SDDC Manager core op dispatches over respx and returns ``status='ok'``.

    HTTP Basic auth is computed by the connector on each request (no
    session establish); the connector's stub credentials loader returns a
    static pair. Asserting only ``status='ok'`` keeps the smoke focused
    on the dispatch leg — payload shape is the JSONFlux force-handle
    test's job.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        ingested_sddc_canary.operator,
        {
            "connector_id": ingested_sddc_canary.connector_id,
            "op_id": op_id,
            "target": {"name": ingested_sddc_canary.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"SDDC Manager op {op_id!r} failed against the respx-mocked appliance at "
        f"{ingested_sddc_canary.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
