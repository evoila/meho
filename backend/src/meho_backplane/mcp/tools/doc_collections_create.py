# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``create_doc_collections`` — the registry-create MCP tool (#1739).

The write sibling of the ``list_doc_collections`` catalogue tool
(``mcp/tools/doc_collections.py``). ``list_doc_collections`` answers "what
docs can I search?"; this tool registers a new collection so an operator /
agent can seed one without a raw ``INSERT INTO doc_collections`` — closing
the validated, audited create-half gap Ops hit on the first co-located
corpus round-trip (claude-rdc-hetzner-dc#975).

Two gates, mirroring the lifecycle write fronts
================================================

* ``required_capability="meho-docs"`` (G4.5-T1 #1519) — a tenant that has
  not provisioned the add-on never sees the tool in ``tools/list`` and a
  direct ``tools/call`` is rejected before the handler runs. The same gate
  ``list_doc_collections`` / ``search_docs`` declare.
* ``required_role=TENANT_ADMIN``, ``op_class="write"`` — a create mutates
  the registry, so it carries the tenant_admin floor the REST create route
  and the ``meho.connector.*`` write tools use, not the OPERATOR floor the
  read-class catalogue tool uses.

The tool is a thin front: it validates + builds a
:class:`~meho_backplane.docs_collections.DocCollectionCreate` from the
JSON-Schema-validated arguments and forwards to
:func:`~meho_backplane.docs_collections.create_doc_collection`, which owns
``tenant_id``-from-operator, ``backend.type`` registry validation, the
``IntegrityError → conflict`` mapping, and the
``op_id="meho.docs.collections.create"`` audit bind. ``tenant_id`` is never
an argument — ``additionalProperties: false`` already rejects a smuggled
one at the schema layer, so a cross-tenant create is structurally
impossible.

A separate module (not an addition to ``mcp/tools/doc_collections.py``)
mirrors ``topology_create_node`` splitting off ``mcp/tools/topology`` —
the tool registry auto-discovers every module under
``meho_backplane.mcp.tools``, so a new file registers identically without
wiring changes and keeps the read-class catalogue module focused.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.docs_collections import (
    DocCollectionBackendTypeError,
    DocCollectionConflictError,
    DocCollectionCreate,
    create_doc_collection,
    project_doc_collection,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_DOCS_CAPABILITY: Final[str] = "meho-docs"
_OP_CLASS_WRITE: Final[str] = "write"
_CREATE_TOOL_NAME: Final[str] = "create_doc_collections"


_CREATE_DOC_COLLECTIONS_DESCRIPTION: Final[str] = (
    "Register a new documentation collection in your tenant so "
    "`search_docs` / `ask_docs` can route to it — the write half of the "
    "doc-collection registry (tenant_admin only). Seeds identity + the "
    "backend binding; it does NOT trigger ingest or an index rebuild — "
    "`status` defaults to `provisioning` and a follow-up "
    "`meho.docs.collections` probe promotes it to `ready` once its index "
    "confirms.\n\n"
    "Tenant-scoped automatically — there is no `tenant_id` argument "
    "(cross-tenant creation is structurally impossible; the collection is "
    "created in your own tenant). `backend.type` must be a registered "
    "search backend (e.g. `corpus-http`) — an unregistered type is "
    "rejected up front rather than committing an unroutable row that "
    "would fail every later probe / search. A `collection_key` that "
    "already exists in your scope is rejected as a conflict.\n\n"
    "WHEN TO CALL: to register a corpus an operator has stood up "
    "(e.g. a co-located MEHO.Knowledge project) so agents can search it, "
    "instead of seeding the row by a raw database INSERT. Returns the "
    "full created collection `{id, collection_key, vendor, products, "
    "backend, status, ...}`."
)


_CREATE_DOC_COLLECTIONS_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "collection_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Stable operator-chosen id (e.g. `vmware`). The key an "
                "agent passes as `search_docs --collection`; must match the "
                "`meho-docs:<collection_key>` capability that entitles a "
                "tenant to search it. Unique within your scope."
            ),
        },
        "vendor": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "The vendor the corpus covers (e.g. 'VMware by Broadcom').",
        },
        "products": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Products the corpus covers, e.g. ['vsphere', 'nsx']. Optional.",
        },
        "description": {
            "type": ["string", "null"],
            "description": "Optional free-text description of the collection.",
        },
        "when_to_use": {
            "type": ["string", "null"],
            "description": (
                "Optional 'pick this collection when…' blurb surfaced "
                "verbatim by `list_doc_collections` so an agent can choose "
                "the right corpus before searching."
            ),
        },
        "backend": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The search-backend type this collection routes to "
                        "(e.g. `corpus-http`). Must be a registered backend."
                    ),
                },
                "ref": {
                    "type": "object",
                    "description": (
                        "Per-collection backend config the adapter reads "
                        "(e.g. {'endpoint': 'https://corpus/v1/search'} for "
                        "`corpus-http`). Shape is owned by the backend type."
                    ),
                },
            },
            "required": ["type", "ref"],
            "additionalProperties": False,
            "description": "The {type, ref} backend routing record. Required.",
        },
        "extras": {
            "type": "object",
            "description": "Optional forward-compat structured fields with no first-class column.",
        },
    },
    "required": ["collection_key", "vendor", "backend"],
    "additionalProperties": False,
}


_CREATE_DOC_COLLECTIONS_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "tenant_id": {"type": ["string", "null"]},
        "collection_key": {"type": "string"},
        "vendor": {"type": "string"},
        "products": {"type": "array", "items": {"type": "string"}},
        "description": {"type": ["string", "null"]},
        "when_to_use": {"type": ["string", "null"]},
        "backend": {"type": "object"},
        "status": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
    },
    "required": [
        "id",
        "collection_key",
        "vendor",
        "products",
        "backend",
        "status",
    ],
}


async def _create_doc_collections_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``create_doc_collections`` call to :func:`create_doc_collection`.

    The dispatcher has already jsonschema-validated *arguments* against
    :data:`_CREATE_DOC_COLLECTIONS_INPUT_SCHEMA` (so ``collection_key`` /
    ``vendor`` / ``backend`` are present and ``additionalProperties: false``
    has rejected a smuggled ``tenant_id``); the Pydantic
    :class:`DocCollectionCreate` re-validation is a belt-and-suspenders
    boundary that also normalises ``products`` and rejects a blank product
    token. The service owns ``tenant_id``-from-operator + the registry /
    conflict mapping; this front only translates the typed exceptions into
    the JSON-RPC ``INVALID_PARAMS`` envelope (the MCP analogue of the REST
    422 / 409).
    """
    try:
        body = DocCollectionCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(
            "invalid create_doc_collections arguments",
            data={"errors": exc.errors(include_url=False)},
        ) from exc

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            async with session.begin():
                row = await create_doc_collection(session, operator, body)
                # Project inside the transaction so the frozen wire model is
                # built off attributes still attached to the live session.
                created = project_doc_collection(row)
        except DocCollectionBackendTypeError as exc:
            raise McpInvalidParamsError(str(exc), data=exc.detail) from exc
        except DocCollectionConflictError as exc:
            raise McpInvalidParamsError(
                str(exc),
                data={"kind": "collection_conflict", "collection_key": exc.collection_key},
            ) from exc

    return created.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name=_CREATE_TOOL_NAME,
        description=_CREATE_DOC_COLLECTIONS_DESCRIPTION,
        inputSchema=_CREATE_DOC_COLLECTIONS_INPUT_SCHEMA,
        outputSchema=_CREATE_DOC_COLLECTIONS_OUTPUT_SCHEMA,
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_create_doc_collections_handler,
)
