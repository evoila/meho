# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.5-T2 dispatch smoke — the curated NSX read ops over respx.

Closes the substrate-proves-out half of the NSX v0.2 ship for
Initiative #368: each entry in
:data:`~meho_backplane.connectors.nsx.core_ops.NSX_CORE_OPS` is
dispatched against a respx-mocked NSX manager and the dispatcher
returns a structured :class:`~meho_backplane.connectors.schemas.OperationResult`
for every one.

The full env-gated two-spec ingestion canary (live ``policy.yaml`` +
``manager.yaml`` through :class:`IngestionPipelineService`) is a
follow-up to this Task — it requires the NSX spec-shelf wired to
the meho-runners pool, the same env-gated pattern
:mod:`tests.acceptance._vcenter_spec` codifies for vSphere. Until
then, the dispatch leg is exercised directly against the curated
descriptor set the :data:`NSX_CORE_OPS` constants describe — same
posture :mod:`tests.acceptance.test_vmware_rest_dispatch_smoke`
took for vSphere before #515 wired vcsim.

Why respx and not a real NSX target: NSX has no public CI simulator
(per Initiative #368's DoD, *"NSX has no public CI simulator —
tests use a vcsim-style record/replay pattern against captured
fixtures (a known limitation; documented in the Initiative DoD)"*).
respx mocks the exact wire contract the connector calls, so the
session-establish + XSRF-token-paired auth dance + per-op HTTP
request all fire through the connector's real pooled
:class:`httpx.AsyncClient`.

Skip conditions:

* No Postgres — the ``pg_engine`` fixture skips when the integration
  database isn't reachable (same gate as every DB-backed acceptance
  test).
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.nsx import NSX_CORE_OPS
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._nsx_canary_fixtures import IngestedNsxCanary

#: Op ids the smoke test dispatches. Sourced from
#: :data:`~meho_backplane.connectors.nsx.NSX_CORE_OPS`; two of them
#: carry path templates (``{domain-id}`` /
#: ``{security-policy-id}``) that need substitution at dispatch
#: time. The smoke supplies ``domain-id=default`` and
#: ``security-policy-id=policy-app-tier`` so the substituted URLs
#: hit the registered respx routes.
SMOKE_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in NSX_CORE_OPS)

#: Per-op parameter map. Op ids without ``{var}`` templates get an
#: empty dict (the respx route matches by literal path); the two
#: firewall ops get the substitutions matching the seeded
#: ``security-policies`` + ``rules`` mock routes. The dispatcher's
#: ``_substitute_path`` helper does the URL-template substitution at
#: dispatch time per its existing contract (see
#: :func:`meho_backplane.operations._branches._substitute_path`).
SMOKE_PARAMS: dict[str, dict[str, object]] = {
    "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies": {
        "domain-id": "default",
    },
    "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules": {
        "domain-id": "default",
        "security-policy-id": "policy-app-tier",
    },
}


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS, ids=lambda op: op)
async def test_dispatch_smoke_nsx_core_op_returns_ok(
    op_id: str,
    ingested_nsx_canary: IngestedNsxCanary,
) -> None:
    """Each curated NSX core op dispatches over respx and returns ``status='ok'``.

    Per-op parameterisation surfaces in CI's report as one test
    case per op; a single regressing op fails its own case and
    leaves the others green. Asserting only ``status='ok'`` (not
    the shape of ``result``) keeps the smoke focused on the
    dispatch leg — payload shape is the JSONFlux force-handle
    test's job.

    ``call_operation`` is the agent surface so the smoke drives
    the same code path the LLM-driven agent would, not the direct
    ``dispatch()`` API. Audit + broadcast hooks fire on every
    dispatch (their assertions live in
    :mod:`tests.acceptance.test_g07_vsphere_canary`'s vcsim
    extension #519 for the substrate-level proof; this test
    focuses on result status).

    The session-create + XSRF flow runs implicitly on the first
    dispatched op of the test and is reused for subsequent ones
    via the per-target XSRF token cache; the per-op assertion does
    not need to re-prove it explicitly.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        ingested_nsx_canary.operator,
        {
            "connector_id": ingested_nsx_canary.connector_id,
            "op_id": op_id,
            "target": {"name": ingested_nsx_canary.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"NSX op {op_id!r} failed against the respx-mocked NSX manager at "
        f"{ingested_nsx_canary.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
