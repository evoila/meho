# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.1-T8 dispatch smoke — read-only vmware-rest ops over respx.

Closes the "Step 7" gap the G0.7 canary runbook flagged: the
:class:`~meho_backplane.connectors.vmware_rest.VmwareRestConnector`
(merged in #498) dispatches 5 representative read ops against a
respx-mocked modern vCenter REST surface and the dispatcher returns
a structured :class:`~meho_backplane.connectors.schemas.OperationResult`
for each one.

Why respx and not vcsim: govmomi's vcsim does not implement the
vCenter REST *resource* API — ``/api/vcenter/{vm,cluster,host,...}``
all 404; it only stubs the vAPI session/tagging/content-library
subset plus the SOAP surface. A vcsim-backed dispatch test of these
ops is therefore unsatisfiable. respx mocks the exact wire contract
the connector calls (see ``_canary_fixtures._register_vcenter_rest_routes``)
so the dispatch leg is still exercised end-to-end:

- Connector resolved by ``(product, version, impl_id)`` triple via the
  v2 registry (the seeded :class:`Target` carries a probe fingerprint
  so the version-range resolver binds it).
- Session lifecycle exercised:
  :meth:`VmwareRestConnector.auth_headers` mints a ``vmware-api-session-id``
  via ``POST /api/session``.
- Per-op HTTP request fires through the connector's real pooled
  :class:`httpx.AsyncClient` (respx intercepts at the transport
  layer; the production follow-redirects + pooling code stays on the
  exercised path).
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

* No Postgres — the ``pg_engine`` fixture skips when the integration
  database isn't reachable (same gate as every DB-backed acceptance
  test). There is no Docker/vcsim dependency anymore: respx runs
  in-process.

Why these are **ingested** ops
==============================

The smoke test dispatches ``source_kind='ingested'`` ops because the
production agent surface for vmware-rest is the ingested corpus. The
descriptor rows are seeded directly by
:func:`~tests.acceptance._canary_fixtures._insert_dispatch_descriptors`
(a minimal six-op set, no LLM/embeddings) — the full-corpus
``vcenter.yaml`` ingest path is the G0.7 canary's job; this module
only needs dispatchable rows.
"""

from __future__ import annotations

import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._canary_fixtures import IngestedCanaryVcsim

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
) -> None:
    """Each probed op dispatches over respx and returns ``status='ok'``.

    Per-op parameterisation surfaces in CI's report as one test
    case per op; a single regressing op fails its own case and
    leaves the others green. Asserting only ``status='ok'`` (not
    the shape of ``result``) keeps the smoke test focused on the
    dispatch leg — the JSONFlux force-mode and agent-flow tests
    cover payload shape.

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
        f"op {op_id!r} failed against the respx-mocked vCenter at "
        f"{ingested_canary_vcsim.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
