# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``update_doc_collections`` — the registry in-place update MCP tool (#3601).

The update sibling of ``create_doc_collections`` / ``delete_doc_collections``
(``mcp/tools/doc_collections_create.py`` / ``_delete.py``): ``create``
registers a collection, ``delete`` deregisters a disabled tenant-owned one,
and this **repoints** an existing one in place. A migration-seeded
collection that carries its own ``backend.ref["endpoint"]`` keeps dialing
the old endpoint when a deployment moves its corpus — create 409s on the
key, delete refuses a global row — so ``search_docs`` / ``ask_docs`` fail
closed with no governed repair. This closes that gap.

Same two gates as the create / delete tools
============================================

* ``required_capability="meho-docs"`` (G4.5-T1 #1519) — a tenant that has
  not provisioned the add-on never sees the tool in ``tools/list`` and a
  direct ``tools/call`` is rejected before the handler runs.
* ``required_role=TENANT_ADMIN``, ``op_class="write"`` — an update mutates
  the registry, so it carries the tenant_admin floor.

Beyond the role floor, editing a **global** (platform-owned) row
additionally requires ``platform_admin`` — enforced in the shared service
(:func:`~meho_backplane.docs_collections.update_doc_collection`) so REST and
MCP gate identically. The tool is a thin front: it resolves the
``collection_key`` tenant-first, builds a
:class:`~meho_backplane.docs_collections.DocCollectionUpdate` from the
remaining arguments, and forwards to the service, which owns the
``backend.type`` registry validation, the ``https`` / SSRF endpoint screen,
the readiness reset on a backend change, and the
``op_id="meho.docs.collections.update"`` audit bind. ``tenant_id`` is never
an argument (``additionalProperties: false`` rejects a smuggled one), so a
cross-tenant edit is structurally impossible. The typed refusals map to the
JSON-RPC ``INVALID_PARAMS`` envelope (the MCP analogue of the REST 404 / 403
/ 422).
"""

from __future__ import annotations

from typing import Any, Final, cast

from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.docs_collections import (
    DocCollectionBackendTypeError,
    DocCollectionEndpointError,
    DocCollectionGlobalUpdateForbiddenError,
    DocCollectionNotFoundError,
    DocCollectionUpdate,
    project_doc_collection,
    resolve_doc_collection,
    update_doc_collection,
)
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_DOCS_CAPABILITY: Final[str] = "meho-docs"
_OP_CLASS_WRITE: Final[str] = "write"
_UPDATE_TOOL_NAME: Final[str] = "update_doc_collections"


_UPDATE_DOC_COLLECTIONS_DESCRIPTION: Final[str] = (
    "Update an existing documentation collection's mutable fields in place — "
    "the in-place repoint half of the doc-collection registry (tenant_admin "
    "only). Its primary use is repointing a collection's `backend.ref` "
    "endpoint after a deployment moves its corpus: `create` cannot repair an "
    "existing key (it conflicts) and `delete` refuses a global row, so this "
    "is the only governed way to change a live collection's backend "
    "binding.\n\n"
    "Tenant-scoped automatically — no `tenant_id` argument (cross-tenant "
    "edits are structurally impossible). Only the fields you pass are "
    "changed; `collection_key` names the row and is never edited. A supplied "
    "`backend` must be a registered search backend and (for `corpus-http`) an "
    "https:// endpoint that resolves to a public address — the same registry "
    "+ SSRF screen `create` applies; to fall back to the deployment's global "
    "corpus URL, send `backend.ref = {}`. A backend change resets the "
    "collection to `provisioning` and clears its cached liveness, so run a "
    "`meho.docs.collections` probe afterwards to promote it back to `ready`.\n\n"
    "REFUSES (returns -32602):\n"
    "  - `global_collection_update_forbidden` — the key resolves to a global "
    "(platform-owned) row and you lack the platform_admin capability; only "
    "your own tenant's collections are editable with tenant_admin alone.\n"
    "  - `unknown_backend_type` / `endpoint_not_allowed` — the supplied "
    "backend is unroutable or its endpoint is non-https / non-public.\n"
    "  - not found — no collection with that key is visible to your tenant "
    "(the error lists the `known_keys`).\n\n"
    "WHEN TO CALL: a collection's corpus endpoint moved (e.g. http → https) "
    "and its searches now fail closed. Returns the full updated collection."
)


_UPDATE_DOC_COLLECTIONS_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "collection_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "The `collection_key` of the collection to update (e.g. "
                "`vmware`). Resolved tenant-first: a tenant-curated row "
                "shadowing a global key is the one updated. Names the row; "
                "never itself changed."
            ),
        },
        "backend": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The search-backend type to route to (e.g. "
                        "`corpus-http`). Must be a registered backend."
                    ),
                },
                "ref": {
                    "type": "object",
                    "description": (
                        "Per-collection backend config the adapter reads "
                        "(e.g. {'endpoint': 'https://corpus/v1/search'} for "
                        "`corpus-http`; {} falls back to the deployment's "
                        "global corpus URL)."
                    ),
                },
            },
            "required": ["type", "ref"],
            "additionalProperties": False,
            "description": (
                "Replacement {type, ref} backend routing record. Supplying "
                "it repoints the collection and resets it to provisioning."
            ),
        },
        "description": {
            "type": ["string", "null"],
            "description": "New free-text description.",
        },
        "when_to_use": {
            "type": ["string", "null"],
            "description": (
                "New 'pick this collection when…' blurb surfaced verbatim by "
                "`list_doc_collections`."
            ),
        },
        "products": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Replacement product list, e.g. ['vsphere', 'nsx'].",
        },
    },
    "required": ["collection_key"],
    "additionalProperties": False,
}


_UPDATE_DOC_COLLECTIONS_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
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
        "last_ingested_at": {"type": ["string", "null"]},
        "doc_count": {"type": ["integer", "null"]},
        "readiness": {"type": ["object", "null"]},
        "extras": {"type": "object"},
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


async def _update_doc_collections_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch an ``update_doc_collections`` call to :func:`update_doc_collection`.

    Splits the ``collection_key`` (which names the row) from the mutable
    fields, re-validates the latter through :class:`DocCollectionUpdate` (a
    belt-and-suspenders boundary that also enforces the at-least-one-field +
    non-null-backend rules), resolves the row tenant-first, and forwards to
    the service, which owns the platform-seat gate, the registry / endpoint
    screen, the readiness reset, and the audit bind. The typed refusals map
    to ``INVALID_PARAMS`` — the MCP analogue of the REST 404 / 403 / 422 —
    carrying the structured detail in ``error.data`` so an agent can branch
    on the stable ``error`` / ``kind`` code.
    """
    collection_key = arguments["collection_key"]
    update_args = {k: v for k, v in arguments.items() if k != "collection_key"}
    try:
        body = DocCollectionUpdate.model_validate(update_args)
    except ValidationError as exc:
        raise McpInvalidParamsError(
            "invalid update_doc_collections arguments",
            data={"errors": exc.errors(include_url=False)},
        ) from exc

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            async with session.begin():
                collection = await resolve_doc_collection(
                    session, collection_key, operator.tenant_id
                )
                row = await update_doc_collection(session, operator, collection, body)
                # Project inside the transaction so the frozen wire model is
                # built off attributes still attached to the live session.
                updated = project_doc_collection(row)
        except DocCollectionNotFoundError as exc:
            raise McpInvalidParamsError(
                f"no doc collection {collection_key!r} is visible to your tenant",
                data=cast("dict[str, Any]", exc.detail),
            ) from exc
        except DocCollectionGlobalUpdateForbiddenError as exc:
            raise McpInvalidParamsError(str(exc), data=exc.detail) from exc
        except DocCollectionBackendTypeError as exc:
            raise McpInvalidParamsError(str(exc), data=exc.detail) from exc
        except DocCollectionEndpointError as exc:
            raise McpInvalidParamsError(str(exc), data=exc.detail) from exc

    return updated.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        feature="doc_collections",
        name=_UPDATE_TOOL_NAME,
        surface=ToolSurface.OPERATOR,
        description=_UPDATE_DOC_COLLECTIONS_DESCRIPTION,
        inputSchema=_UPDATE_DOC_COLLECTIONS_INPUT_SCHEMA,
        outputSchema=_UPDATE_DOC_COLLECTIONS_OUTPUT_SCHEMA,
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
        required_capability=_DOCS_CAPABILITY,
    ),
    handler=_update_doc_collections_handler,
)
