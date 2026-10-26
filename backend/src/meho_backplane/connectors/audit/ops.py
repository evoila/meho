# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``audit.loose_rows`` op + its registrar (#4670).

The op lists the tenant's **loose rows**: audit records whose
``parent_audit_id`` names a row that does not exist. They appear when
MEHO stops before a multi-step operation saves its own row
(``docs/decisions/composite-audit-when-a-step-waits.md``). MEHO never
repairs them, so an operator needs a standing check. This op is that
check's read: a Sensor pins it (see :mod:`.sensor_template`), the check
runner dispatches it once an hour, and the assertion reads
``recent_count``.

Natural key ``(product="audit", version="1.x", impl_id="audit-integrity")``,
so the wire ``connector_id`` is ``audit-integrity-1.x`` (the
``targets-registry-1.x`` mold, #2861). The op id ``audit.loose_rows``
classifies as ``audit_query`` in :func:`~meho_backplane.broadcast.events.classify_op`,
so its broadcast event is aggregate-only and the row list never rides the
feed.

Read-only: the handler runs two ``SELECT`` statements through
:func:`~meho_backplane.audit_query.find_loose_rows` and writes nothing to
``audit_log``. The dispatch itself is audited like every other operation;
that row is the record of the check, not a repair.

Result shape (set-shaped, so JSONFlux applies): ``{"rows": [...],
"count", "recent_count", "recent_since", "truncated"}``. Over the §4
threshold the reducer moves ``rows`` behind a result handle; the
``result_scalars`` hint keeps the four scalars inline so a Sensor's
``$.recent_count`` select still resolves.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.audit_query import (
    DEFAULT_RECENT_WINDOW,
    LOOSE_ROWS_MAX_LIMIT,
    find_loose_rows,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "AUDIT_INTEGRITY_CONNECTOR_ID",
    "AUDIT_LOOSE_ROWS_OP_ID",
    "AUDIT_LOOSE_ROWS_PARAMETER_SCHEMA",
    "AUDIT_LOOSE_ROWS_RESPONSE_SCHEMA",
    "audit_loose_rows",
    "register_audit_integrity_operations",
]

#: Wire connector_id for the synthetic audit-integrity product. Must stay
#: parser-compatible: digit-led version suffix, product recoverable as the
#: head's first hyphen segment.
AUDIT_INTEGRITY_CONNECTOR_ID: Final[str] = "audit-integrity-1.x"

#: Op id — the ``audit_log.path`` string the check's own dispatch row carries.
AUDIT_LOOSE_ROWS_OP_ID: Final[str] = "audit.loose_rows"

_DEFAULT_LIMIT: Final[int] = 200
_MIN_RECENT_WINDOW_SECONDS: Final[int] = 60
_MAX_RECENT_WINDOW_SECONDS: Final[int] = 30 * 24 * 3600

AUDIT_LOOSE_ROWS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": LOOSE_ROWS_MAX_LIMIT,
            "default": _DEFAULT_LIMIT,
            "description": (
                "Most rows to list, newest first. The counts are always exact; "
                "`truncated` says when the list is shorter than `count`."
            ),
        },
        "recent_window_seconds": {
            "type": "integer",
            "minimum": _MIN_RECENT_WINDOW_SECONDS,
            "maximum": _MAX_RECENT_WINDOW_SECONDS,
            "default": int(DEFAULT_RECENT_WINDOW.total_seconds()),
            "description": (
                "Age bound for `recent_count`: loose rows younger than this many "
                "seconds. Default one day."
            ),
        },
    },
    "additionalProperties": False,
}

AUDIT_LOOSE_ROWS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "ts": {"type": "string", "format": "date-time"},
                    "principal_sub": {"type": "string"},
                    "op_id": {"type": "string"},
                    "event_class": {"type": "string"},
                    "path": {"type": "string"},
                    "parent_audit_id": {"type": "string", "format": "uuid"},
                },
                "required": [
                    "id",
                    "ts",
                    "principal_sub",
                    "op_id",
                    "event_class",
                    "path",
                    "parent_audit_id",
                ],
            },
            "description": "Loose rows, newest first, at most `limit`.",
        },
        "count": {"type": "integer", "description": "Exact total of loose rows in the tenant."},
        "recent_count": {
            "type": "integer",
            "description": "Exact number of loose rows with `ts >= recent_since`.",
        },
        "recent_since": {
            "type": "string",
            "format": "date-time",
            "description": "Start of the recent window (now minus `recent_window_seconds`).",
        },
        "truncated": {
            "type": "boolean",
            "description": "True when `rows` holds fewer rows than `count`.",
        },
    },
    "required": ["rows", "count", "recent_count", "recent_since", "truncated"],
}

_GROUP_KEY = "integrity"
_GROUP_WHEN_TO_USE = (
    "Read-only checks over MEHO's own audit log. Use to find audit records "
    "that point to a parent row that does not exist (a multi-step operation "
    "that is still running, or that stopped before its own row was saved). "
    "Nothing here writes or repairs rows."
)

_DESCRIPTION = (
    "List the tenant's loose audit rows: step records and approval requests "
    "whose `parent_audit_id` names a row that does not exist. Returns the "
    "rows (id, time, principal, operation, parent id), the exact total "
    "`count`, and `recent_count` for rows younger than `recent_window_seconds` "
    "(default one day). Tenant-scoped automatically. Read-only: the audit log "
    "stays append-only. Meant to be pinned by a Sensor (see "
    "`meho_backplane.connectors.audit.sensor_template`)."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Check whether MEHO lost the last save of a multi-step operation. A "
        "multi-step operation saves its own row last, so a young loose row "
        "may belong to an operation that is still running; check again "
        "later. A loose row is never something to repair: look at the rows "
        "under it, and check the approval queue before running the operation "
        "again."
    ),
    "parameter_hints": {
        "limit": "Optional. Rows to list (1-1000, default 200). Counts stay exact.",
        "recent_window_seconds": "Optional. Age bound for recent_count (default 86400).",
    },
    "output_shape": "{'rows': [...], 'count', 'recent_count', 'recent_since', 'truncated'}",
    # Keep the counts inline when the reducer moves ``rows`` behind a
    # handle: a Sensor's ``$.recent_count`` select must always resolve.
    "result_scalars": {"keys": ["count", "recent_count", "recent_since", "truncated"]},
}


async def audit_loose_rows(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List the operator's tenant's loose audit rows.

    Op-id: ``audit.loose_rows``. Targetless typed op; *target* is always
    ``None``. The dispatcher has validated *params* against
    :data:`AUDIT_LOOSE_ROWS_PARAMETER_SCHEMA`. ``tenant_id`` comes from
    *operator*, never from *params*.
    """
    limit = int(params.get("limit", _DEFAULT_LIMIT))
    window_seconds = params.get("recent_window_seconds")
    recent_window = (
        DEFAULT_RECENT_WINDOW if window_seconds is None else timedelta(seconds=int(window_seconds))
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await find_loose_rows(
            tenant_id=operator.tenant_id,
            session=session,
            limit=limit,
            recent_window=recent_window,
        )
    return result.model_dump(mode="json")


async def register_audit_integrity_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``audit.loose_rows`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent. ``safety_level="safe"``
    is load-bearing: the Sensor create guard accepts only ``safe`` ops and
    the runner re-checks it at dispatch.
    """
    await register_typed_operation(
        product="audit",
        version="1.x",
        impl_id="audit-integrity",
        op_id=AUDIT_LOOSE_ROWS_OP_ID,
        handler=audit_loose_rows,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="List audit rows whose parent row does not exist (loose rows).",
        description=_DESCRIPTION,
        parameter_schema=AUDIT_LOOSE_ROWS_PARAMETER_SCHEMA,
        response_schema=AUDIT_LOOSE_ROWS_RESPONSE_SCHEMA,
        tags=["audit", "read", "integrity", "check"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
