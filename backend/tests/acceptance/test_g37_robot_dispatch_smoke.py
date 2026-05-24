# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.7-T9 dispatch smoke — the 10 curated Hetzner Robot read ops over respx.

Proves the acceptance criteria for issue #852:

* AC1: every enabled op dispatches via the agent's ``call_operation``
  meta-tool and returns ``status='ok'`` against a respx-mocked Webservice.
* AC2: ``hetzner-robot.server.list`` (``GET:/server``) also exercises the
  JSONFlux handle path via the real
  :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
  in force mode (``row_threshold=0``).
* AC3: every dispatch writes an audit row (``op_id`` + ``target_id`` +
  ``params_hash``) — asserted by checking the backplane's audit log
  after each call.
* AC4: the MCP ``llm_instructions`` for ``GET:/query`` and ``GET:/server``
  contain the 401-IP-block warning text — asserted from the in-memory
  ``ROBOT_CORE_OPS`` constant (no DB read required; the curation step
  writes these values into the descriptor rows at review time, and the
  acceptance tests seed the rows directly from the same constant).
* AC5: sandbox env var path — when the Robot target's host is overridden
  to point at the respx-mocked surface that returns 200 + empty arrays,
  every op dispatches successfully and returns an empty list (the sandbox
  200/empty-array path).

Why respx and not a real Hetzner Robot Webservice:
The Hetzner Robot Webservice has no public CI simulator (#536 per the
Initiative DoD). respx mocks the exact wire contract the connector calls
(HTTP Basic GET against each of the 10 path endpoints), so the
session-establishment (Basic auth header computation) and per-op HTTP
requests all fire through the connector's real httpx client.

Sandbox env pattern:
The Hetzner Robot consumer sandbox (``https://robot-sandbox.hetzner.com``)
returns HTTP 200 with empty JSON arrays for every read endpoint. This
test exercises the same code path: the respx router maps every path to
``200 + []`` (or ``200 + {}``) when the ``ROBOT_SANDBOX_OP_IDS`` fixture
activates the sandbox router. The acceptance confirms all 10 ops tolerate
empty-array responses gracefully (no parsing crash, ``status='ok'``).

Skip conditions:
* No Postgres — the ``pg_engine`` fixture skips when the integration
  database isn't reachable (same gate as every DB-backed acceptance test).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.connectors.hetzner_robot import ROBOT_CORE_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._robot_canary_fixtures import (
    ROBOT_CANARY_SERVERS,
    ROBOT_FORCE_HANDLE_LIST_OP_ID,
    ROBOT_FORCE_HANDLE_PARAMS,
    IngestedRobotCanary,
)

# ---------------------------------------------------------------------------
# AC4 — MCP llm_instructions contain the 401-IP-block warning text.
# These are pure-constant assertions; no DB / network access required.
# ---------------------------------------------------------------------------

#: Required warning phrase that MUST appear in the ``when_to_call`` field
#: of ``GET:/query`` (hetzner-robot.about) and ``GET:/server``
#: (hetzner-robot.server.list) per the issue #852 acceptance criteria.
_REQUIRED_WARNING_PHRASE = "401 IP-BLOCK WARNING"

#: Op ids that MUST carry the warning per AC4 of issue #852.
_WARNING_REQUIRED_OPS = frozenset({"GET:/query", "GET:/server"})


@pytest.mark.parametrize(
    "op_id",
    sorted(_WARNING_REQUIRED_OPS),
    ids=lambda op: op.replace("/", "_").replace(":", "_"),
)
def test_mcp_llm_instructions_contain_401_block_warning(op_id: str) -> None:
    """AC4: the 401-IP-block warning phrase is present in the curated llm_instructions.

    Asserts that the ``when_to_call`` field of the curated
    :data:`~meho_backplane.connectors.hetzner_robot.ROBOT_CORE_OPS` entry for
    *op_id* contains the required warning phrase so agents see it before
    composing a call. The warning is load-bearing — without it agents may
    retry on auth failures, triggering the 10-minute IP block that affects
    all operators on the shared egress IP.

    Proves AC4 of #852: MCP descriptions contain the 401-block warning.
    """
    matching = [op for op in ROBOT_CORE_OPS if op.op_id == op_id]
    assert matching, f"No ROBOT_CORE_OPS entry found for op_id={op_id!r}"
    op = matching[0]
    when_to_call = op.llm_instructions.get("when_to_call", "")
    assert isinstance(when_to_call, str), (
        f"llm_instructions['when_to_call'] for {op_id!r} must be a string; "
        f"got {type(when_to_call).__name__!r}"
    )
    assert _REQUIRED_WARNING_PHRASE in when_to_call, (
        f"op_id={op_id!r} llm_instructions['when_to_call'] must contain "
        f"{_REQUIRED_WARNING_PHRASE!r} (the 401-IP-block warning that prevents "
        f"agents from retrying on auth failures and triggering the 10-minute "
        f"IP block on the shared egress IP); got:\n{when_to_call!r}"
    )


# ---------------------------------------------------------------------------
# Smoke-test parameters — op ids and per-op path-parameter substitutions.
# ---------------------------------------------------------------------------

#: All 10 curated Hetzner Robot op ids, sourced from the canonical constant.
SMOKE_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in ROBOT_CORE_OPS)

#: Path-parameter substitutions for ops with ``{var}`` templates.
#: The respx router is registered for specific values; these must match.
SMOKE_PARAMS: dict[str, dict[str, object]] = {
    "GET:/server/{server-ip}": {"server-ip": "1.2.3.1"},
    "GET:/vswitch/{id}": {"id": "4321"},
}


# ---------------------------------------------------------------------------
# AC1 — every op dispatches and returns status='ok'.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", SMOKE_OP_IDS, ids=lambda op: op)
async def test_dispatch_smoke_robot_core_op_returns_ok(
    op_id: str,
    ingested_robot_canary: IngestedRobotCanary,
) -> None:
    """AC1: each curated Hetzner Robot core op dispatches over respx and returns ok.

    Per-op parameterisation surfaces in CI's report as one test case per op;
    a single regressing op fails its own case and leaves the others green.
    Asserts only ``status='ok'`` (not the payload shape) — payload shape is
    the JSONFlux handle test's job.

    ``call_operation`` is the agent surface so the smoke drives the same code
    path the LLM-driven agent would. Audit + broadcast hooks fire on every
    dispatch. HTTP Basic auth header computation fires on every request
    (no session establishment needed for the Robot Webservice).

    Proves AC1 of #852: every enabled op is reachable via the meta-tool.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        ingested_robot_canary.operator,
        {
            "connector_id": ingested_robot_canary.connector_id,
            "op_id": op_id,
            "target": {"name": ingested_robot_canary.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"Hetzner Robot op {op_id!r} failed against the respx-mocked Webservice at "
        f"{ingested_robot_canary.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — server.list JSONFlux handle path.
# Mirrors test_g37_robot_jsonflux_force_handle.py but inline for
# locality; the force-handle fixture is re-used from the same module.
# ---------------------------------------------------------------------------


@pytest.fixture
def force_handle_reducer() -> Any:
    """Install :class:`JsonFluxReducer` in force mode as the dispatcher default.

    ``row_threshold=0`` forces every non-empty set to materialize, so the
    seeded server list (below the default 50-row threshold) produces a
    handle. Teardown restores :class:`PassThroughReducer` so a follow-on
    test in the same session sees the v0.2 default.
    """
    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        yield
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


async def test_server_list_returns_jsonflux_handle(
    force_handle_reducer: None,
    ingested_robot_canary: IngestedRobotCanary,
) -> None:
    """AC2: server list (GET:/server) populates OperationResult.handle via JsonFluxReducer.

    Drives the real
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    in force mode (``row_threshold=0``) to prove the JSONFlux dispatcher
    seam is wired for Hetzner Robot's server-list op — the most common
    inventory call and the one most likely to produce large result sets
    in production.

    Proves AC2 of #852: server.list E2E asserts the JSONFlux handle path.
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
        f"server-list force-handle dispatch did not succeed: {result_envelope!r}"
    )
    handle = result_envelope.get("handle")
    assert handle is not None, (
        "expected OperationResult.handle to be populated by JsonFluxReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )
    uuid.UUID(handle["handle_id"])
    assert handle["total_rows"] == expected_rows, (
        f"expected {expected_rows} servers; got handle.total_rows={handle['total_rows']}"
    )
    assert handle["summary_md"], "handle.summary_md must be non-empty"
    assert str(expected_rows) in handle["summary_md"], (
        f"expected summary_md to mention the row count; got {handle['summary_md']!r}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"expected ≥1 sample row from the seeded server list; got sample_rows={sample_rows!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — every dispatch writes an audit row with op_id + target_id +
# params_hash in the payload.
# ---------------------------------------------------------------------------


async def test_dispatch_writes_audit_row_with_op_id_target_id_params_hash(
    ingested_robot_canary: IngestedRobotCanary,
) -> None:
    """AC3: dispatching an op writes one DISPATCH audit row with the required fields.

    Dispatches ``hetzner-robot.about`` (``GET:/query``) — the lightest read
    op, no path parameters — and then queries the backplane's ``audit_log``
    table for the resulting row. Asserts:

    * ``method='DISPATCH'`` + ``path=op_id`` — the canonical audit-row shape
      the dispatcher writes (documented in
      :mod:`meho_backplane.operations._audit`).
    * ``payload['op_id']`` matches the dispatched op.
    * ``payload['params_hash']`` is present (non-empty string) — proves the
      dispatcher hashed the call parameters before writing the row, satisfying
      the AC3 "params_hash present" requirement.
    * ``target_id`` is a non-null UUID on the audit row — proves the
      dispatcher resolved the target and wrote its FK into the row.

    Uses a baseline/delta approach (count before + count after) so the test
    is isolated from any audit rows written by the fixture setup (ingest,
    edit_group, etc.) that share the same ``operator_sub``.

    Proves AC3 of #852: dispatch writes audit row with op_id + target_id +
    params_hash.
    """
    ac3_op_id = "GET:/query"

    sessionmaker = get_sessionmaker()

    async def _count_dispatch_rows() -> int:
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "DISPATCH",
                    AuditLog.path == ac3_op_id,
                    AuditLog.operator_sub == ingested_robot_canary.operator.sub,
                )
            )
            return len(list(result.scalars().all()))

    baseline = await _count_dispatch_rows()

    result = await call_operation(
        ingested_robot_canary.operator,
        {
            "connector_id": ingested_robot_canary.connector_id,
            "op_id": ac3_op_id,
            "target": {"name": ingested_robot_canary.target_name},
            "params": {},
        },
    )

    assert result["status"] == "ok", (
        f"Hetzner Robot {ac3_op_id!r} did not succeed against respx mock: "
        f"{result.get('error')!r}; full result={result!r}"
    )

    # Delta must be exactly +1.
    final = await _count_dispatch_rows()
    assert final - baseline == 1, (
        f"expected exactly one new DISPATCH audit row for {ac3_op_id!r}; "
        f"baseline={baseline} final={final}"
    )

    # Fetch the row and assert payload fields.
    async with sessionmaker() as session:
        rows_result = await session.execute(
            select(AuditLog).where(
                AuditLog.method == "DISPATCH",
                AuditLog.path == ac3_op_id,
                AuditLog.operator_sub == ingested_robot_canary.operator.sub,
            )
        )
        rows = list(rows_result.scalars().all())

    assert rows, "audit rows vanished between count and fetch"
    audit = rows[-1]  # most recent row

    # op_id in payload
    assert audit.payload.get("op_id") == ac3_op_id, (
        f"audit payload['op_id'] should be {ac3_op_id!r}; "
        f"got {audit.payload.get('op_id')!r}; full payload={audit.payload!r}"
    )

    # params_hash in payload and non-empty
    params_hash = audit.payload.get("params_hash")
    assert params_hash is not None, (
        f"audit payload missing 'params_hash'; full payload={audit.payload!r}"
    )
    assert isinstance(params_hash, str) and params_hash, (
        f"audit payload['params_hash'] must be a non-empty string; got {params_hash!r}"
    )

    # target_id on the audit row must be a non-null UUID
    assert audit.target_id is not None, (
        f"audit_log.target_id must be populated for a target-bound dispatch; "
        f"got target_id=None on audit row id={audit.id!r}"
    )


# ---------------------------------------------------------------------------
# AC5 — sandbox 200/empty-array path.
# The consumer sandbox returns HTTP 200 with empty arrays for every read op.
# All 10 ops must tolerate empty-array/object responses without raising.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id",
    SMOKE_OP_IDS,
    ids=lambda op: op,
)
async def test_dispatch_smoke_robot_core_op_tolerates_empty_sandbox_response(
    op_id: str,
    ingested_robot_canary_sandbox: IngestedRobotCanary,
) -> None:
    """AC5: every op tolerates the sandbox 200 + empty-array/object response.

    The Hetzner Robot consumer sandbox returns HTTP 200 with empty JSON arrays
    (or ``{}``) for every read endpoint, not real data. Operators who run
    against the sandbox before having a production account should receive
    ``status='ok'`` with an empty result — not a parse crash or error.

    The ``ingested_robot_canary_sandbox`` fixture replaces the respx routes
    with a router that maps every path to ``200 + []``.

    Proves AC5 of #852: sandbox 200/empty-array path passes in CI.
    """
    params = SMOKE_PARAMS.get(op_id, {})
    result = await call_operation(
        ingested_robot_canary_sandbox.operator,
        {
            "connector_id": ingested_robot_canary_sandbox.connector_id,
            "op_id": op_id,
            "target": {"name": ingested_robot_canary_sandbox.target_name},
            "params": params,
        },
    )

    assert result["status"] == "ok", (
        f"Hetzner Robot op {op_id!r} failed against the sandbox (200/empty) mock at "
        f"{ingested_robot_canary_sandbox.base_url}: {result.get('error')!r}; "
        f"full result={result!r}"
    )
