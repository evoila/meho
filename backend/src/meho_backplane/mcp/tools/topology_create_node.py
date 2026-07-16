# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.topology.create_node`` — manual seed for the topology graph.

Initiative #772 (G0.9.1), Task #778 (T6, Signal #14). Closes the
empty-tenant bootstrap gap: a fresh tenant has zero ``graph_node`` rows
(no probe has run yet) and the rest of the topology MCP surface
(``meho.topology.annotate``) requires both endpoints to already exist.
Before this tool, the only way to create a node was the CLI verb
``meho topology refresh <target>``; that is not reachable from an MCP
session, so an agent driving the bootstrap path hit
``-32602 no graph_node matched ...`` with no in-tool remediation.

The tool is a **thin admin meta-tool** in the ``meho.*`` namespace
(``tenant_admin`` only, ``op_class='write'``) that forwards to
:func:`~meho_backplane.topology.nodes.create_or_get_node`. The service
primitive owns validation / upsert / audit / broadcast — this front
performs no DB work of its own. The shape mirrors the
:func:`~meho_backplane.topology.annotate.annotate_edge` admin tool
(``meho.topology.annotate`` in :mod:`meho_backplane.mcp.tools.topology`)
so an operator carries one mental model across the topology write
surface.

Why a separate module
=====================

``meho_backplane.mcp.tools.topology`` is already four tools and >1100
lines; adding a fifth would push the module further past the
codebase's 600-line per-file guidance. The MCP tool registry
auto-discovers every module under ``meho_backplane.mcp.tools`` (see
``mcp.registry.eager_import_mcp_modules``), so a separate file
registers identically without any wiring changes.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    KIND_SLUG_MAX_LENGTH,
    KIND_SLUG_MIN_LENGTH,
    KIND_SLUG_PATTERN,
    WELL_KNOWN_NODE_KINDS,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.topology.nodes import (
    InvalidNodeKindError,
    create_or_get_node,
)

__all__: list[str] = []


_CREATE_NODE_TOOL_NAME: Final[str] = "meho.topology.create_node"

#: Well-known graph-node kinds, materialised once at module load.
#: Sourced from :data:`WELL_KNOWN_NODE_KINDS` so the description's
#: suggestion list tracks the documented core set automatically. The
#: vocabulary is open (T1 #2534): the schema constrains `kind` by slug
#: pattern, not by membership.
_NODE_KIND_VALUES: Final[list[str]] = sorted(WELL_KNOWN_NODE_KINDS)


_CREATE_NODE_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "pattern": KIND_SLUG_PATTERN,
            "minLength": KIND_SLUG_MIN_LENGTH,
            "maxLength": KIND_SLUG_MAX_LENGTH,
            "description": (
                "Node kind: a lowercase slug (letters/digits joined "
                "by `.`, `_` or `-`; 2-63 chars). The vocabulary is "
                "open — any slug matching the pattern is accepted — "
                "but prefer a well-known kind when one fits: "
                + ", ".join(f"`{k}`" for k in _NODE_KIND_VALUES)
                + ". Novel kinds (`dns-record`, `keycloak-realm`, "
                "`database`, ...) are the right call when no "
                "well-known kind describes the resource class. "
                "Inner-graph kinds like `vault-role`, `vault-mount`, "
                "`principal` are the canonical use case: those rows "
                "cannot be auto-discovered (no probe walks the Vault "
                "policy tree as a topology source) and must be seeded "
                "manually before `meho.topology.annotate` can reference "
                "them."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "`graph_node.name` to create. Unique within "
                "`(tenant, kind, name)`; a repeat call with the same "
                "triple is idempotent (refreshes `last_seen` + merges "
                "manual-seed properties)."
            ),
        },
        "note": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional free-text annotation stored on "
                "`graph_node.properties.note`. Use to record the "
                "operational rationale for the manual seed — 'Vault "
                "role pinned by INVENTORY.md L42; rotated 2026-04-22'."
            ),
        },
        "evidence_url": {
            "type": ["string", "null"],
            "maxLength": 2048,
            "description": (
                "Optional URL the operator attached as evidence "
                "(typically an INVENTORY.md anchor / runbook). Stored "
                "on `graph_node.properties.evidence_url`."
            ),
        },
    },
    "required": ["kind", "name"],
    "additionalProperties": False,
}


_CREATE_NODE_DESCRIPTION: Final[str] = (
    "Manually seed a `graph_node` row in the operator's tenant "
    "(tenant_admin only). Idempotent on the `(tenant, kind, name)` "
    "triple: a repeat call refreshes `last_seen` and merges the "
    "manual-seed properties rather than erroring. Tenant-scoped "
    "automatically — no `tenant_id` argument (cross-tenant creation is "
    "structurally impossible).\n\n"
    "WHEN TO CALL: a fresh tenant has zero nodes (no probe has run "
    "yet) and you need to assert a curated edge — `meho.topology."
    "annotate` cannot resolve endpoints that do not yet exist as "
    "`graph_node` rows. Seed both endpoints first, then annotate. "
    "Example bootstrap flow:\n"
    "  1. `meho.topology.create_node {kind: principal, name: "
    "k8s-sa-prod}`\n"
    "  2. `meho.topology.create_node {kind: vault-role, name: "
    "rdc-vault}`\n"
    "  3. `meho.topology.annotate {from_name: k8s-sa-prod, kind: "
    "authenticates-via, to_name: rdc-vault}`\n\n"
    "Also useful for curated inner-graph nodes the probes cannot "
    "infer (Vault roles, Keycloak realms, externally-managed "
    "principals) so a subsequent annotation can reference them.\n\n"
    "DO NOT use to mirror nodes the refresh service already writes — "
    "running `meho topology refresh <target>` is the canonical path "
    "for probe-derivable nodes (`vm`, `pod`, `service`, ...). Manual "
    "seeds for those kinds work (idempotent on the unique index) but "
    "duplicate the refresh service's job; if you find yourself doing "
    "it routinely, run a refresh instead.\n\n"
    "Returns `{node_id, kind, name, was_created}`. `was_created=true` "
    "means a fresh row was inserted; `false` means an existing row "
    "was refreshed (the call is still a success — the bootstrap "
    "precondition is satisfied either way)."
)


async def _create_node_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``meho.topology.create_node`` call to :func:`create_or_get_node`.

    The dispatcher has already jsonschema-validated *arguments* against
    :data:`_CREATE_NODE_INPUT_SCHEMA`, so ``kind`` is guaranteed to
    match the slug pattern and ``name`` is guaranteed non-empty.
    Tenant scope comes from *operator* inside the service — never from
    *arguments* (``additionalProperties: false`` already rejects a
    smuggled ``tenant_id`` at the schema layer).

    :class:`InvalidNodeKindError` is structurally unreachable from this
    front — ``kind`` is pattern-pinned by the inputSchema and the
    service validates the same grammar — but the catch is retained as
    a belt-and-suspenders guard against a future drift between the
    schema's ``pattern`` and the service-side
    :data:`~meho_backplane.db.models.KIND_SLUG_PATTERN`.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            result = await create_or_get_node(
                session,
                operator,
                kind=arguments["kind"],
                name=arguments["name"],
                note=arguments.get("note"),
                evidence_url=arguments.get("evidence_url"),
            )
        except InvalidNodeKindError as exc:
            raise McpInvalidParamsError(str(exc)) from exc

    return {
        "node_id": str(result.node.id),
        "kind": result.node.kind,
        "name": result.node.name,
        "was_created": result.was_created,
    }


register_mcp_tool(
    definition=ToolDefinition(
        name=_CREATE_NODE_TOOL_NAME,
        description=_CREATE_NODE_DESCRIPTION,
        inputSchema=_CREATE_NODE_INPUT_SCHEMA,
        outputSchema={
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "kind": {"type": "string"},
                "name": {"type": "string"},
                "was_created": {"type": "boolean"},
            },
            "required": ["node_id", "kind", "name", "was_created"],
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_create_node_handler,
)
