# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5 dispatch smoke — the 9 typed Harbor read ops dispatch zero-catalog.

Each entry in :data:`~meho_backplane.connectors.harbor.HARBOR_TYPED_OPS`
is dispatched through ``call_operation`` against a respx-mocked Harbor
registry with **no ingest / catalog fixture setup** — only the
code-shipped typed registrar (``source_kind="typed"``) run by the
``typed_harbor_canary`` fixture — and the dispatcher returns
``status="ok"`` for every one.

This is the substrate-proves-out half of the #2856 conversion: the read
core dispatches on a fresh boot with zero catalog ingest, mirroring the
NSX (#2302) and SDDC Manager (#2306) typed-read conversions under Task
#2358 and closing Goal #2247's per-deploy catalog-state failure class for
Harbor.

Why respx and not a real Harbor target: Harbor has no public CI simulator
equivalent to vcsim, so the dispatch leg is exercised against the seeded
typed descriptor set over a respx-mocked Harbor REST surface (the same
record/replay posture the container E2E in
:mod:`tests.integration.test_connectors_harbor_container` complements with
a live ``goharbor/harbor-core:v2.11.0`` stack).

Skip conditions:

* No Postgres — the ``pg_engine`` fixture skips when the integration
  database isn't reachable (same gate as every DB-backed acceptance test).
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.harbor import HARBOR_TYPED_OPS
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._harbor_canary_fixtures import TypedHarborCanary

#: Op ids the smoke test dispatches — the 9 typed read ops, sourced from
#: :data:`~meho_backplane.connectors.harbor.HARBOR_TYPED_OPS`.
SMOKE_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in HARBOR_TYPED_OPS)

#: Per-op path parameters. Ops without required path params get an empty
#: dict; the templated ops get the substitution values matching the respx
#: routes seeded in
#: :func:`tests.acceptance._harbor_canary_fixtures._register_harbor_routes`
#: (project_name="library", repository_name="ubuntu", reference="latest").
SMOKE_PARAMS: dict[str, dict[str, object]] = {
    "harbor.project.info": {"project_name": "library"},
    "harbor.repository.list": {"project_name": "library"},
    "harbor.repository.info": {"project_name": "library", "repository_name": "ubuntu"},
    "harbor.artifact.list": {"project_name": "library", "repository_name": "ubuntu"},
    "harbor.artifact.info": {
        "project_name": "library",
        "repository_name": "ubuntu",
        "reference": "latest",
    },
}


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS, ids=lambda op: op)
async def test_dispatch_smoke_harbor_typed_read_returns_ok(
    op_id: str,
    typed_harbor_canary: TypedHarborCanary,
) -> None:
    """Each typed Harbor read dispatches over respx and returns ``status='ok'``.

    No ingest / catalog fixture: only the code-shipped typed registrar ran
    (``typed_harbor_canary``), proving the #2856 read core dispatches on a
    fresh boot with zero catalog state. Per-op parameterisation surfaces
    one CI case per op; a single regressing op fails its own case and
    leaves the others green. Asserting only ``status='ok'`` (not the shape
    of ``result``) keeps the smoke focused on the dispatch leg — payload
    shape is the JSONFlux force-handle test's job.

    HTTP Basic auth fires on every request (no session-establish step).
    The stub credentials loader returns a static pair; the respx router
    matches by URL path without asserting auth headers.

    The robot list op (``harbor.robot.list``) is included in the
    parametrised set; its respx-mocked response carries no ``secret``
    field — the robot-secret invariant is asserted separately in
    :mod:`tests.test_connectors_harbor_typed_reads`.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        typed_harbor_canary.operator,
        {
            "connector_id": typed_harbor_canary.connector_id,
            "op_id": op_id,
            "target": {"name": typed_harbor_canary.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"Harbor typed op {op_id!r} failed against the respx-mocked registry at "
        f"{typed_harbor_canary.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
