# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T8 dispatch smoke — the 9 curated Harbor read ops over respx.

Closes the substrate-proves-out half of the Harbor v0.2 ship for
Initiative #368: each entry in
:data:`~meho_backplane.connectors.harbor.core_ops.HARBOR_CORE_OPS` is
dispatched against a respx-mocked Harbor registry and the dispatcher
returns a structured :class:`~meho_backplane.connectors.schemas.OperationResult`
for every one.

Why respx and not a real Harbor target: Harbor has no public CI simulator
equivalent to vcsim. Per Initiative #368's DoD, Harbor uses the
vcsim-style record/replay fixture pattern (a known limitation; documented
in the Initiative DoD). The full env-gated spec-canary ingest is a
follow-up planned for G3.5-T10 #622; until then, the dispatch leg is
exercised directly against the curated descriptor set the
:data:`HARBOR_CORE_OPS` constants describe.

Skip conditions:

* No Postgres — the ``pg_engine`` fixture skips when the integration
  database isn't reachable (same gate as every DB-backed acceptance
  test).
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.harbor import HARBOR_CORE_OPS
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._harbor_canary_fixtures import IngestedHarborCanary

#: Op ids the smoke test dispatches. Sourced from
#: :data:`~meho_backplane.connectors.harbor.HARBOR_CORE_OPS`.
SMOKE_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in HARBOR_CORE_OPS)

#: Per-op parameter map. Ops without ``{var}`` templates get an empty dict
#: (the respx route matches by literal path). Templated ops get the
#: specific substitution values matching the seeded respx routes in
#: :func:`tests.acceptance._harbor_canary_fixtures._register_harbor_routes`.
SMOKE_PARAMS: dict[str, dict[str, object]] = {
    "GET:/api/v2.0/projects/{project_name}": {
        "project_name": "library",
    },
    "GET:/api/v2.0/projects/{project_name}/repositories": {
        "project_name": "library",
    },
    "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}": {
        "project_name": "library",
        "repository_name": "ubuntu",
    },
    "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts": {
        "project_name": "library",
        "repository_name": "ubuntu",
    },
    "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}": {
        "project_name": "library",
        "repository_name": "ubuntu",
        "reference": "latest",
    },
}


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS, ids=lambda op: op)
async def test_dispatch_smoke_harbor_core_op_returns_ok(
    op_id: str,
    ingested_harbor_canary: IngestedHarborCanary,
) -> None:
    """Each curated Harbor core op dispatches over respx and returns ``status='ok'``.

    Per-op parameterisation surfaces in CI's report as one test case per
    op; a single regressing op fails its own case and leaves the others
    green. Asserting only ``status='ok'`` (not the shape of ``result``)
    keeps the smoke focused on the dispatch leg — payload shape is the
    JSONFlux force-handle test's job.

    HTTP Basic auth fires on every request (no session-establish step).
    The stub credentials loader returns a static pair; the respx router
    matches by URL path without asserting auth headers.

    The robot list op (``GET:/api/v2.0/robots``) is included in the
    parametrised set; its respx-mocked response carries no ``secret``
    field — the robot-secret invariant is asserted separately in
    :mod:`tests.test_connectors_harbor_core_ops`.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        ingested_harbor_canary.operator,
        {
            "connector_id": ingested_harbor_canary.connector_id,
            "op_id": op_id,
            "target": {"name": ingested_harbor_canary.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"Harbor op {op_id!r} failed against the respx-mocked registry at "
        f"{ingested_harbor_canary.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
