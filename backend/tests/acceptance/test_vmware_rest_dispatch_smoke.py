# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.1-T8 dispatch smoke — read-only vmware-rest ops against vcsim.

Closes the "Step 7" gap the G0.7 canary runbook flagged: the
:class:`~meho_backplane.connectors.vmware_rest.VmwareRestConnector`
(merged in #498) dispatches 5 representative read ops against a
running vcsim and the dispatcher returns a structured
:class:`~meho_backplane.connectors.schemas.OperationResult` for each
one. The smoke test verifies the dispatch leg end-to-end:

- Connector resolved by ``(product, version, impl_id)`` triple via the
  v2 registry.
- Session lifecycle exercised:
  :meth:`VmwareRestConnector.auth_headers` mints a ``vmware-api-session-id``
  via ``POST /api/session`` against vcsim's no-auth surface.
- Per-op HTTP request fires through the connector's pooled
  :class:`httpx.AsyncClient` with TLS verification disabled (vcsim's
  self-signed cert).
- :func:`~meho_backplane.operations.dispatcher.dispatch` produces a
  ``status='ok'`` result for every probed op.

Five op_ids span the canonical inventory surface so a regression in
any single resolver path (cluster lookup, host enumeration, datastore
shape, network listing, ``GET /api/about`` short-circuit) surfaces
here rather than in a single-op test that could miss it. The set
matches the issue body's ``GET:/api/about, GET:/vcenter/cluster,
GET:/vcenter/host, GET:/vcenter/datastore, GET:/vcenter/network``
list.

Skip conditions:

* No vcsim — the ``vcsim_endpoint`` fixture in conftest skips with a
  clear reason when neither ``MEHO_VCSIM_URL`` nor a Docker socket
  resolves.
* No vCenter OpenAPI spec — ingestion needs ``vcenter.yaml`` to
  populate the dispatchable ``endpoint_descriptor`` rows; without it
  the test skips with the canary's standard reason.

Why ingestion is part of the smoke test
=======================================

The smoke test dispatches **ingested** ops (``source_kind='ingested'``)
rather than typed ones, because the production agent surface for
vmware-rest is the ingested corpus (T3 #520 landed ``vcenter.yaml``
ingestion full). Bypassing ingestion with hand-rolled descriptor
rows would prove the dispatcher works in isolation but not the
production-shipped read path. The cost (one ingest per test run) is
paid once via the session-scoped :class:`~tests.acceptance._canary_fixtures._IngestedCanary`
helper.
"""

from __future__ import annotations

import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._canary_fixtures import IngestedCanaryVcsim
from tests.acceptance._vcsim import VcsimEndpoint

#: Read-only ops the smoke test dispatches. Every one is a
#: ``GET`` against an inventory endpoint vcsim implements; each
#: must succeed with ``status='ok'``. The ``/api/about`` op
#: short-circuits inside the connector's typed-op handler and
#: doesn't require an active vcenter session, exercising the
#: session-optional code path.
SMOKE_OP_IDS: tuple[str, ...] = (
    "GET:/api/about",
    "GET:/vcenter/cluster",
    "GET:/vcenter/host",
    "GET:/vcenter/datastore",
    "GET:/vcenter/network",
)


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS)
async def test_dispatch_smoke_op_returns_ok(
    op_id: str,
    ingested_canary_vcsim: IngestedCanaryVcsim,
    vcsim_endpoint: VcsimEndpoint,
) -> None:
    """Each probed op dispatches against vcsim and returns ``status='ok'``.

    Per-op parameterisation surfaces in CI's report as one test
    case per op; a single regressing op fails its own case and
    leaves the others green. Asserting only ``status='ok'`` (not
    the shape of ``result``) keeps the smoke test resilient to
    vcsim's release-to-release inventory shape variance — the
    JSONFlux force-mode and agent-flow tests cover shape.

    ``call_operation`` is the agent surface so the smoke test
    drives the same code path the LLM-driven agent would, not the
    direct ``dispatch()`` API. Audit + broadcast hooks fire on
    every dispatch (their assertions live in the canary's vcsim
    extension #519, not here — this test focuses on result
    status).
    """
    operator: Operator = ingested_canary_vcsim.operator
    target_name = ingested_canary_vcsim.target_name

    result = await call_operation(
        operator,
        {
            "connector_id": ingested_canary_vcsim.connector_id,
            "op_id": op_id,
            "target": {"name": target_name},
            "params": {},
        },
    )

    assert result["status"] == "ok", (
        f"op {op_id!r} failed against vcsim at "
        f"{vcsim_endpoint.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
