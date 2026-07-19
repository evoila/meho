# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``delete_doc_collections`` — the registry-delete MCP tool (#2487).

The delete sibling of ``create_doc_collections``
(``mcp/tools/doc_collections_create.py``): ``create`` registers a
collection, this deregisters a disabled, tenant-owned one and frees its
``collection_key`` for a re-create. ``disable`` only flips ``status`` —
the row (and its occupied key) persist — so a collection mis-registered
under the wrong ``backend.type`` / ``ref`` could never be fixed under its
own key; this tool closes that recovery gap.

Same two gates as the create tool
=================================

* ``required_capability="meho-docs"`` (G4.5-T1 #1519) — a tenant that has
  not provisioned the add-on never sees the tool in ``tools/list`` and a
  direct ``tools/call`` is rejected before the handler runs.
* ``required_role=TENANT_ADMIN``, ``op_class="write"`` — a delete mutates
  the registry, so it carries the tenant_admin floor.

The tool is a thin front: it resolves the ``collection_key`` tenant-first
(:func:`~meho_backplane.docs_collections.resolve_doc_collection`) and
forwards to :func:`~meho_backplane.docs_collections.delete_doc_collection`,
which owns the disabled-first + global-vs-tenant guards and the
``op_id="meho.docs.collections.delete"`` audit bind. ``tenant_id`` is never
an argument (``additionalProperties: false`` rejects a smuggled one at the
schema layer, so a cross-tenant delete is structurally impossible). The
typed refusals map to the JSON-RPC ``INVALID_PARAMS`` envelope (the MCP
analogue of the REST 404 / 403 / 409).
"""

from __future__ import annotations

from typing import Any, Final, cast

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.docs_collections import (
    DocCollectionGlobalError,
    DocCollectionNotDisabledError,
    DocCollectionNotFoundError,
    delete_doc_collection,
    resolve_doc_collection,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_DOCS_CAPABILITY: Final[str] = "meho-docs"
_OP_CLASS_WRITE: Final[str] = "write"
_DELETE_TOOL_NAME: Final[str] = "delete_doc_collections"


_DELETE_DOC_COLLECTIONS_DESCRIPTION: Final[str] = (
    "Deregister a disabled, tenant-owned documentation collection and free "
    "its `collection_key` for re-registration — the delete half of the "
    "doc-collection registry (tenant_admin only). `disable` only hides a "
    "collection from search; this hard-deletes the row so a collection "
    "mis-registered under the wrong `backend` can be re-`create`d under the "
    "same key.\n\n"
    "Tenant-scoped automatically — no `tenant_id` argument (cross-tenant "
    "deletion is structurally impossible).\n\n"
    "REFUSES (returns -32602):\n"
    "  - `collection_not_disabled` — the collection is not `disabled`. "
    "Disable it first (the disable → delete two-step keeps a terminal "
    "`collection_disabled` warning window for in-flight searchers before "
    "the key 404s); the error names the current `status`.\n"
    "  - `global_collection` — the key resolves to a global "
    "(platform-owned) row every tenant sees. Only tenant-owned collections "
    "are deletable here; deleting a tenant row that shadows a global key "
    "un-shadows the global row.\n"
    "  - not found — no collection with that key is visible to your "
    "tenant (the error lists the `known_keys`).\n\n"
    "WHEN TO CALL: an operator registered a collection with the wrong "
    "`backend.type` / `ref` and needs to re-create it under the same key. "
    "Returns `{collection_key}` (the freed key)."
)


_DELETE_DOC_COLLECTIONS_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "collection_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "The `collection_key` of the disabled, tenant-owned "
                "collection to deregister (e.g. `vmware`). Resolved "
                "tenant-first: a tenant-curated row shadowing a global key "
                "is the one deleted."
            ),
        },
    },
    "required": ["collection_key"],
    "additionalProperties": False,
}


_DELETE_DOC_COLLECTIONS_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "collection_key": {"type": "string"},
    },
    "required": ["collection_key"],
}


async def _delete_doc_collections_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``delete_doc_collections`` call to :func:`delete_doc_collection`.

    Resolves the ``collection_key`` tenant-first, then forwards to the
    service, which owns the disabled-first + global-vs-tenant guards and
    the audit bind. The typed refusals (not-found / global / not-disabled)
    all map to ``INVALID_PARAMS`` — the MCP analogue of the REST 404 / 403 /
    409 — carrying the structured detail in ``error.data`` so an agent can
    branch on the stable ``error`` code.
    """
    collection_key = arguments["collection_key"]

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            async with session.begin():
                collection = await resolve_doc_collection(
                    session, collection_key, operator.tenant_id
                )
                await delete_doc_collection(session, operator, collection)
        except DocCollectionNotFoundError as exc:
            # ``HTTPException.detail`` is typed ``str | None`` upstream, but the
            # resolver always sets it to the structured ``{error, collection_key,
            # known_keys}`` dict — forward it as the MCP ``error.data``.
            raise McpInvalidParamsError(
                f"no doc collection {collection_key!r} is visible to your tenant",
                data=cast("dict[str, Any]", exc.detail),
            ) from exc
        except DocCollectionGlobalError as exc:
            raise McpInvalidParamsError(str(exc), data=exc.detail) from exc
        except DocCollectionNotDisabledError as exc:
            raise McpInvalidParamsError(str(exc), data=exc.detail) from exc

    return {"collection_key": collection_key}


register_mcp_tool(
    definition=ToolDefinition(
        name=_DELETE_TOOL_NAME,
        description=_DELETE_DOC_COLLECTIONS_DESCRIPTION,
        inputSchema=_DELETE_DOC_COLLECTIONS_INPUT_SCHEMA,
        outputSchema=_DELETE_DOC_COLLECTIONS_OUTPUT_SCHEMA,
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_delete_doc_collections_handler,
)
