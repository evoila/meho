# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.topology.bulk_import`` — batch curated-edge authoring for agents.

Task #2539 (Initiative #2533). Gives the agent surface the
propose→plan→apply loop humans already have on the REST / CLI / console
bulk-import fronts. Before this tool an agent seeding a cross-system
inventory looped single ``meho.topology.annotate`` calls with no
pre-apply plan and no way to approve the batch in one shot.

Two behaviours on one tool, split on ``dry_run``:

* ``dry_run=true`` (the default) — the free, read-shaped **plan** step.
  The front calls
  :func:`~meho_backplane.topology.bulk_import.bulk_import_edges` with
  ``dry_run=True`` directly (no dispatch, no policy gate): it resolves
  every endpoint, classifies each row create / update / conflict, writes
  nothing, and returns the per-row plan. Never parks. A validation
  failure surfaces every row's diagnostic together on the -32602
  ``error.data`` member — the same "fix the whole file in one pass"
  shape the REST front's ``422 invalid_bulk`` gives.
* ``dry_run=false`` — the gated **apply**. The front dispatches the
  ``topology.bulk_import`` typed op (registered by
  :mod:`meho_backplane.connectors.topology.ops`) through
  :func:`~meho_backplane.mcp.tools.topology.dispatch_topology_write`, so
  the exact same #2537 substrate that gates the single writes gates the
  batch: an AGENT principal parks the whole batch as **one**
  :class:`~meho_backplane.db.models.ApprovalRequest` carrying the batch
  params, and a human ``tenant_admin`` executes immediately. On approve,
  the stored batch applies atomically (all-or-nothing transaction).

Why the two paths are not both dispatched: the dry-run is read-shaped
and must never park, but the typed op carries the agents-park safety
dial — routing dry-run through it would park an agent's harmless plan
request. So dry-run bypasses the dispatcher and the apply-only typed op
carries no ``dry_run`` param (the parked request is exactly the batch to
apply).

Why no JSONFlux result handle: the topology MCP surface bounds
set-shaped responses with a hard row cap, not a handle —
``query_topology`` returns up to 1000 edges inline. The dry-run plan is
capped at :data:`~meho_backplane.connectors.topology.schemas.BULK_IMPORT_MAX_EDGES`
rows at the tool boundary (``inputSchema`` ``maxItems``), so it follows
the same convention its sibling read tool established.

Why a separate module: mirrors ``topology_create_node.py`` — the
registry auto-discovers every module under ``meho_backplane.mcp.tools``,
so a fifth topology tool registers without growing
``mcp/tools/topology.py`` further past the 600-line file guidance.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.topology.ops import TOPOLOGY_BULK_IMPORT_OP_ID
from meho_backplane.connectors.topology.schemas import (
    BULK_IMPORT_MAX_EDGES,
    BULK_IMPORT_RESPONSE_SCHEMA,
    BULK_IMPORT_TOOL_INPUT_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.topology import dispatch_topology_write, with_parked_shape
from meho_backplane.topology.bulk_import import (
    BulkImportValidationError,
    build_bulk_import_rows,
    bulk_import_edges,
    serialize_bulk_result,
    serialize_bulk_validation_error,
)

__all__: list[str] = []


_BULK_IMPORT_TOOL_NAME: Final[str] = "meho.topology.bulk_import"


_BULK_IMPORT_DESCRIPTION: Final[str] = (
    "Batch-assert curated `graph_edge` rows in one atomic pass "
    "(tenant_admin only). The agent-surface equivalent of the REST / "
    "CLI / console bulk import: seed a whole cross-system inventory "
    "declaratively instead of looping single `meho.topology.annotate` "
    "calls. Each row is one annotate's params — `{from_name, kind, "
    "to_name, from_node_kind?, to_node_kind?, note?, evidence_url?}` — "
    f"and a batch is 1 to {BULK_IMPORT_MAX_EDGES} rows. Tenant-scoped "
    "automatically.\n\n"
    "REQUIRES: both endpoints of every row must already exist as "
    "`graph_node` rows in the tenant. A row naming a missing endpoint "
    "fails validation; seed endpoints first with "
    "`meho.topology.create_node`.\n\n"
    "PROPOSE THEN APPLY:\n"
    "  1. `dry_run: true` (the DEFAULT) returns the per-row plan "
    "(`action` is `create` / `update` / `conflict`) and writes "
    "nothing. It never parks — use it freely to preview the batch and "
    "catch validation errors. A failing batch returns -32602 with every "
    "row's diagnostic on `error.data.errors` so you fix the whole set "
    "in one pass.\n"
    "  2. `dry_run: false` applies the whole batch in one all-or-"
    "nothing transaction (a single invalid row rolls everything back). "
    "AGENT PRINCIPALS: the apply does not execute immediately — the "
    "whole batch parks as ONE durable approval request and the tool "
    "returns `{status: awaiting_approval, approval_request_id, ...}`; a "
    "human operator approves it and all rows land atomically with the "
    "exact proposed params. Human tenant_admin calls apply "
    "immediately.\n\n"
    "Returns `{dry_run, created, updated, conflicts, rows: [{index, "
    "action, edge_id, from_name, from_kind, to_name, to_kind, kind, "
    "superseded, conflicts}]}`. `edge_id` is null on dry-run rows (no "
    "row exists yet). `superseded` lists auto edges the row would "
    "displace; `conflicts` lists incompatible-kind edges over the same "
    "endpoint pair (§6 diagnostics). Node bulk import is out of scope — "
    "this seeds edges only, matching the underlying service."
)


async def _bulk_import_dry_run(
    operator: Operator, rows_params: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run the free, read-shaped plan pass and return it (never parks).

    Calls the service validation pass directly — no dispatch, no policy
    gate — so an agent's harmless preview is not gated. A
    :class:`BulkImportValidationError` becomes a -32602 carrying every
    row's structured diagnostic on ``error.data``.
    """
    rows = build_bulk_import_rows(rows_params)
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            result = await bulk_import_edges(session, operator, rows, dry_run=True)
    except BulkImportValidationError as exc:
        raise McpInvalidParamsError(
            str(exc),
            data=serialize_bulk_validation_error(exc),
        ) from exc
    return serialize_bulk_result(result)


async def _bulk_import_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Route a ``meho.topology.bulk_import`` call: dry-run direct, apply gated.

    ``dry_run`` defaults to true (see the inputSchema). The free dry-run
    calls the service directly; the gated apply dispatches the
    ``topology.bulk_import`` typed op through
    :func:`~meho_backplane.mcp.tools.topology.dispatch_topology_write`
    with only ``rows`` (no ``dry_run``), so the parked
    :class:`~meho_backplane.db.models.ApprovalRequest` carries exactly
    the batch to apply. The tool boundary already enforced the 1-to-
    :data:`BULK_IMPORT_MAX_EDGES` row bound via the inputSchema
    ``minItems`` / ``maxItems``, so an oversized batch never reaches this
    handler (rejected as -32602, the REST-422 analogue).
    """
    dry_run = bool(arguments.get("dry_run", True))
    rows_params: list[dict[str, Any]] = arguments["rows"]
    if dry_run:
        return await _bulk_import_dry_run(operator, rows_params)
    # Gated apply — dispatch the apply-only typed op. dispatch_topology_write
    # returns the executed plan (human) or the awaiting_approval envelope
    # (agent park), and maps BulkImportValidationError → -32602.
    return await dispatch_topology_write(
        operator, TOPOLOGY_BULK_IMPORT_OP_ID, {"rows": rows_params}
    )


register_mcp_tool(
    definition=ToolDefinition(
        name=_BULK_IMPORT_TOOL_NAME,
        description=_BULK_IMPORT_DESCRIPTION,
        inputSchema=BULK_IMPORT_TOOL_INPUT_SCHEMA,
        outputSchema=with_parked_shape(BULK_IMPORT_RESPONSE_SCHEMA),
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_bulk_import_handler,
)
