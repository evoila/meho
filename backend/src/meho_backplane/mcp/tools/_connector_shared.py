# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared constants + coercion helpers for the connector admin MCP tools.

The connector admin surface is split across two tool modules by
responsibility:

* :mod:`meho_backplane.mcp.tools.connector_ingest` — the ingest
  pipeline tools (``meho.connector.ingest`` + ``meho.connector.ingest_status``)
  that wrap :class:`IngestionPipelineService` + the in-memory job registry.
* :mod:`meho_backplane.mcp.tools.connector_admin` — the review /
  edit / state-machine tools (``list`` / ``review`` / ``edit_group`` /
  ``edit_op`` / ``enable`` / ``disable``) that wrap :class:`ReviewService`
  and :func:`list_ingested_connectors`.

Both modules share the ``connector_id`` / ``tenant_id`` schema snippets,
the op-class taxonomy strings, and the JSON-safe serialiser; they live
here so a future convention tweak edits one string instead of two
modules — the single-source discipline #407 called out for the
``connector_id`` description.
"""

from __future__ import annotations

import json
from typing import Any, Final
from uuid import UUID

from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.ingest import (
    InvalidSchemaError,
    InvalidSpecError,
    LlmOutputInvalid,
    OpIdCollision,
    UncoveredVersionLabel,
    UnsupportedSpecError,
    UpstreamNotSpecError,
    VersionMismatchError,
    build_invalid_schema_detail,
    build_invalid_spec_detail,
    build_llm_output_invalid_detail,
    build_op_id_collision_detail,
    build_uncovered_version_label_detail,
    build_unsupported_spec_detail,
    build_upstream_not_spec_detail,
    build_version_mismatch_detail,
)

# Op-class strings keep parity with the audit table conventions used
# by :mod:`meho_backplane.broadcast.classify` for the credential /
# audit / read / write taxonomy. List + review + ingest_status are
# read; the ingest + mutators are write.
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"


# Shared snippet documenting the connector_id format on every tool
# that accepts one. Authored once so a future convention tweak edits
# one string instead of six.
_CONNECTOR_ID_DESCRIPTION: Final[str] = (
    "Operator-facing connector identifier — '<impl_id>-<version>' "
    "(e.g. 'vmware-rest-9.0', 'vault-1.x'). Split into the parsed "
    "(product, version, impl_id) triple by the same rule the CLI "
    "uses."
)

#: Optional tenant scope shared across every tool. Omit (or pass
#: ``null``) for the built-in / global connector pool — that scope is
#: ``tenant_admin``-only. Pass the operator's own tenant UUID to
#: operate on tenant-curated rows.
_TENANT_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": ["string", "null"],
    "format": "uuid",
    "description": (
        "Tenant scope for this operation. Omit or pass null for "
        "built-in / global connectors (tenant_admin only). Pass the "
        "operator's own tenant UUID for tenant-curated rows; cross-"
        "tenant requests are rejected."
    ),
}


def _coerce_tenant_id(raw: Any) -> UUID | None:
    """Convert the ``tenant_id`` argument from JSON-RPC into ``UUID | None``.

    The JSON-Schema-validated value is always either a UUID string or
    ``None``; the helper preserves ``None`` and parses the string. A
    malformed UUID surfaces at JSON-Schema validation time (because of
    the ``format: uuid`` annotation interpreted by
    :mod:`jsonschema`'s format-checker chain). This helper assumes the
    schema validator has already run.
    """
    if raw is None:
        return None
    return UUID(raw)


def _model_dump_json_safe(model: Any) -> dict[str, Any]:
    """Serialise a Pydantic model to a JSON-safe dict.

    Uses ``mode="json"`` so UUIDs and datetimes serialise as strings
    rather than native Python objects (the dispatcher's
    :func:`json.dumps` over the response would otherwise raise
    :class:`TypeError`). Mirrors the meho_status reference tool's
    serialisation discipline.
    """
    raw = model.model_dump(mode="json")
    # Round-trip via ``json`` to surface any latent non-serialisable
    # field at registration time rather than at the dispatcher's
    # ``json.dumps`` call. The cost is one extra encode/decode per
    # tool call; the benefit is that a Pydantic field shape that
    # ``mode="json"`` can't handle (e.g. a future ``set[UUID]``) is
    # rejected here with the offending field named in the traceback
    # rather than as a generic dispatcher error.
    rehydrated: dict[str, Any] = json.loads(json.dumps(raw))
    return rehydrated


#: The typed ``SpecError`` siblings the inline ingest path maps onto a
#: JSON-RPC ``-32602`` caller-input error. Kept as a tuple next to
#: :func:`raise_invalid_params_for_spec_error` so the ``except`` target in
#: :mod:`meho_backplane.mcp.tools.connector_ingest` and the dispatch table
#: can't fall out of sync — adding a sibling means touching both.
#: ``VersionMismatchError`` / ``UncoveredVersionLabel`` are the #777
#: originals; the remaining six complete the pattern (#1534).
SPEC_ERROR_TYPES: Final = (
    VersionMismatchError,
    UncoveredVersionLabel,
    UpstreamNotSpecError,
    UnsupportedSpecError,
    InvalidSchemaError,
    InvalidSpecError,
    OpIdCollision,
    LlmOutputInvalid,
)


def raise_invalid_params_for_spec_error(exc: Exception) -> None:
    """Map a typed ingest ``SpecError`` onto :class:`McpInvalidParamsError`.

    Every sibling is a caller-input mistake — wrong OpenAPI flavour, an
    invalid spec, an HTML upstream, an op-id collision, a bad grouping-LLM
    response — so it surfaces as JSON-RPC ``-32602`` carrying the rendered
    message plus the shared structured ``data`` envelope from
    :mod:`operations/ingest/error_envelopes` (the #777 pattern, completed
    for the sibling set in #1534). The builders are the single source of
    truth shared with the REST route, so the two surfaces can't drift.

    Lives in the shared module (rather than ``connector_ingest``) so the
    error-mapping table and the exception tuple have one grep-friendly
    home, and so ``connector_ingest`` stays under the code-quality
    file-size budget.

    Always raises when *exc* is one of :data:`SPEC_ERROR_TYPES` (the only
    types the caller funnels in); the trailing ``raise`` is a defensive
    re-raise that keeps the function total for a type that slips past the
    dispatch table.
    """
    if isinstance(exc, VersionMismatchError):
        data = build_version_mismatch_detail(exc)
    elif isinstance(exc, UncoveredVersionLabel):
        data = build_uncovered_version_label_detail(exc)
    elif isinstance(exc, UpstreamNotSpecError):
        data = build_upstream_not_spec_detail(
            upstream_url=exc.upstream_url,
            content_type=exc.content_type,
        )
    elif isinstance(exc, UnsupportedSpecError):
        data = build_unsupported_spec_detail(exc)
    elif isinstance(exc, InvalidSchemaError):
        # InvalidSchemaError before InvalidSpecError — a broken $ref is the
        # narrower domain than a structurally invalid root document.
        data = build_invalid_schema_detail(exc)
    elif isinstance(exc, InvalidSpecError):
        data = build_invalid_spec_detail(exc)
    elif isinstance(exc, OpIdCollision):
        data = build_op_id_collision_detail(exc)
    elif isinstance(exc, LlmOutputInvalid):
        data = build_llm_output_invalid_detail(exc)
    else:  # pragma: no cover — defensive; caller funnels only SPEC_ERROR_TYPES
        raise exc
    raise McpInvalidParamsError(str(exc), data=data) from exc
