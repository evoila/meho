# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.6-T2 dispatch smoke — the 8 curated vROps read ops over respx.

Closes the substrate-proves-out half of the vROps v0.5 ship for
Initiative #369: each entry in
:data:`~tests.acceptance._vrops_canary_fixtures.VROPS_CANARY_CORE_OP_IDS`
is dispatched against a respx-mocked vROps appliance and the
dispatcher returns a structured
:class:`~meho_backplane.connectors.schemas.OperationResult` for every
one.

The full env-gated suite-api ingestion canary (live
``vcf-operations-9.0/suite-api.yaml`` through
:class:`IngestionPipelineService`) is a follow-up to this Task — it
requires the vROps spec-shelf wired to the meho-runners pool, the
same env-gated pattern :mod:`tests.acceptance._vcenter_spec` codifies
for vSphere. Until then, the dispatch leg is exercised directly
against the ingested descriptor set the :data:`VROPS_CANARY_CORE_OP_IDS`
constants describe — same posture
:mod:`tests.acceptance.test_g35_nsx_dispatch_smoke` took for NSX.

Why respx and not a real vROps appliance: vROps has no public CI
simulator. respx mocks the exact wire contract the connector calls,
so the Basic-auth header + per-op HTTP request all fire through the
connector's real pooled :class:`httpx.AsyncClient`.

Skip conditions:

* No Postgres — the ``pg_engine`` fixture skips when the integration
  database isn't reachable (same gate as every DB-backed acceptance
  test).
"""

from __future__ import annotations

import pytest

from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._vrops_canary_fixtures import (
    VROPS_CANARY_CORE_OP_IDS,
    IngestedVropsCanary,
)

#: Op ids the smoke test dispatches. Sourced from
#: :data:`~tests.acceptance._vrops_canary_fixtures.VROPS_CANARY_CORE_OP_IDS`;
#: one of them carries a path template (``{id}`` on the resource-get op)
#: that needs substitution at dispatch time. The smoke supplies the canary
#: resource's UUID so the substituted URL hits the registered respx route.
SMOKE_OP_IDS: tuple[str, ...] = VROPS_CANARY_CORE_OP_IDS

#: Per-op parameter map. Op ids without ``{var}`` templates get an
#: empty dict (the respx route matches by literal path); the one
#: resource-by-id op gets the substitution matching the seeded
#: ``VROPS_CANARY_RESOURCE_DETAIL`` mock route.
SMOKE_PARAMS: dict[str, dict[str, object]] = {
    "GET:/suite-api/api/resources/{id}": {
        "id": "00000000-0000-4000-8000-000000000000",
    },
}


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS, ids=lambda op: op)
async def test_dispatch_smoke_vrops_core_op_returns_ok(
    op_id: str,
    ingested_vrops_canary: IngestedVropsCanary,
) -> None:
    """Each curated vROps core op dispatches over respx and returns ``status='ok'``.

    Per-op parameterisation surfaces in CI's report as one test case
    per op; a single regressing op fails its own case and leaves the
    others green. Asserting only ``status='ok'`` (not the shape of
    ``result``) keeps the smoke focused on the dispatch leg — payload
    shape is the JSONFlux force-handle test's job.

    ``call_operation`` is the agent surface so the smoke drives the
    same code path the LLM-driven agent would, not the direct
    ``dispatch()`` API.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        ingested_vrops_canary.operator,
        {
            "connector_id": ingested_vrops_canary.connector_id,
            "op_id": op_id,
            "target": {"name": ingested_vrops_canary.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"vROps op {op_id!r} failed against the respx-mocked vROps appliance at "
        f"{ingested_vrops_canary.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
