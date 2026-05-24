# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/broadcast/event/{audit_id}`` -- the event detail drawer.

Initiative #338 (G10.1 Activity broadcast UI), Task #868 (G10.1-T2)
work item #4. Renders the right-hand drawer the feed rows open via
``hx-get`` / ``hx-target="#event-drawer"`` on a click. The drawer shows
the **full audit row detail** behind a broadcast event:

* the full request **payload** (``audit_log.payload``) -- but only when
  the op is not a redacted/aggregate-only class (decision #3); a
  ``credential_read`` / ``credential_mint`` / ``audit_query`` event
  renders the 🔒 placeholder instead, never the raw payload,
* the operation identity (``operator_sub``, ``method``, ``path``,
  ``status_code``, ``occurred_at``, ``duration_ms``),
* the ``request_id``, the ``audit_id`` (the row's own id), and the
  broadcast ``event_id`` (passed through as a query param -- it is the
  ephemeral Valkey-stream id, not a PG column).

Why the path parameter is the audit id, not the broadcast event id
==================================================================

The broadcast ``event_id`` is generated per event and lives only on the
ephemeral Valkey stream (``meho:feed:{tenant}``, MAXLEN-trimmed); it is
**not** a column on any table. The canonical, queryable record of an
operation is the ``audit_log`` row, keyed by ``audit_log.id`` -- which
every :class:`~meho_backplane.broadcast.events.BroadcastEvent` carries
as its ``audit_id`` field. The drawer's payload + ``request_id`` exist
only on that row. So the drawer resolves by **audit id**: the feed row
builder reads ``ev.audit_id`` for the path and passes ``ev.event_id``
as the ``event_id`` query param purely for display. This is the only
shape that can surface the full audit detail the work item requires; a
route keyed on the ephemeral broadcast id would have nothing in PG to
join against.

Aggregate-only discipline (decision #3 / work item #7)
======================================================

The PII contract is enforced at *publish* time -- the broadcast event's
payload is already redacted (:func:`redact_payload`). But the drawer
reads the **audit_log** row, whose ``payload`` is the *unredacted*
canonical record. Rendering that raw payload for a ``credential_read``
op in the drawer would defeat decision #3 -- it would surface, on click,
exactly the secret-bearing detail the feed row deliberately withholds.
So the drawer reproduces the *same* aggregate-only decision the
publisher made:

* When the audit row carries ``payload["broadcast_detail_effective"]``
  (the G6.3 resolver's recorded verdict -- ``"full"`` or
  ``"aggregate"``), the drawer honours it verbatim. This is the
  authoritative answer to "what detail did the feed actually show?",
  including any per-tenant override that flipped a normally-sensitive op
  to full detail.
* Otherwise it falls back to classifying the op via :func:`classify_op`
  and treating the sensitive classes
  (:data:`AGGREGATE_ONLY_OP_CLASSES`) as aggregate-only.

The op id is recovered from ``payload["op_id"]`` when present (the
audit-middleware stamps it for connector-style routes), falling back to
the publisher's own chassis-route heuristic ``http.{method}:{path}`` so
the classification matches what the broadcast publisher computed
byte-for-byte. An unknown op classifies ``other`` (full detail) only
when it is genuinely a non-sensitive request.

Tenant scoping
==============

The audit row is resolved with ``audit_log.tenant_id =
session.tenant_id`` as the first ``WHERE`` predicate, so an id belonging
to another tenant returns the same 404 fragment as a non-existent id --
the boundary is opaque, never leaked. There is no tenant query
parameter.

The route is HTMX-only by design (no full-page render): the drawer is
meaningful only beside the feed. A direct browser nav returns the bare
fragment with no ``base.html`` chrome.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast import classify_op
from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import AuditLog
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.templating import get_templates

__all__ = ["AGGREGATE_ONLY_OP_CLASSES", "build_event_router"]

#: Op classes whose detail is withheld from the broadcast surface per
#: decision #3 -- the same classes :func:`redact_payload` strips at
#: publish time. The drawer never renders the audit row's raw payload
#: for these; it shows the 🔒 aggregate-only placeholder instead. Kept
#: in sync with the redaction contract in
#: :mod:`meho_backplane.broadcast.events`.
AGGREGATE_ONLY_OP_CLASSES: frozenset[str] = frozenset(
    {"credential_read", "credential_mint", "audit_query"}
)

#: Audit-only payload keys the drawer hides from the rendered request
#: payload. ``op_id`` / ``op_class`` are the route-bound classification
#: hints surfaced as first-class drawer fields, not request params;
#: ``broadcast_detail_origin`` / ``broadcast_detail_effective`` are the
#: G6.3 resolver's internal forensic metadata (``tenant_rule:<uuid>``
#: origins are deliberately never shown to broadcast subscribers). The
#: drawer renders the *request* payload, so these are stripped before
#: the ``| tojson`` dump.
_INTERNAL_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"op_id", "op_class", "broadcast_detail_origin", "broadcast_detail_effective"}
)


async def _fetch_audit_row(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    audit_id: uuid.UUID,
) -> AuditLog | None:
    """Resolve ``(tenant_id, audit_id)`` to an ``audit_log`` row.

    Returns ``None`` when no row matches. A cross-tenant id surfaces
    identically -- the tenant boundary is opaque, mirroring the
    topology drawer's ``_fetch_node`` contract.
    """
    stmt = select(AuditLog).where(
        AuditLog.tenant_id == tenant_id,
        AuditLog.id == audit_id,
    )
    result = await db_session.execute(stmt)
    return result.scalar_one_or_none()


def _resolve_op_id(row: AuditLog) -> str:
    """Recover the op id for the row's sensitivity classification.

    The audit middleware stamps the canonical op id into
    ``payload["op_id"]`` for connector-style routes. When absent
    (chassis HTTP routes, non-op requests) we fall back to the
    publisher's own heuristic ``http.{method.lower()}:{path}`` -- the
    exact string
    :func:`meho_backplane.audit._resolve_op_id_and_class_override`
    builds -- so :func:`classify_op` here yields the same class the
    broadcast publisher computed. The ``:`` separator deliberately
    avoids a route ending in ``.list`` being misread as a ``read`` verb
    suffix (the publisher relies on the same guard).
    """
    op_id = row.payload.get("op_id")
    if isinstance(op_id, str) and op_id:
        return op_id
    return f"http.{row.method.lower()}:{row.path}"


def _is_aggregate_only(row: AuditLog, op_class: str) -> bool:
    """Decide whether the drawer withholds the payload (decision #3).

    Honours the G6.3 resolver's recorded verdict
    (``payload["broadcast_detail_effective"]``) when present so the
    drawer matches the detail the feed actually showed -- including a
    per-tenant override that flipped a sensitive op to full detail.
    Falls back to op-class membership in
    :data:`AGGREGATE_ONLY_OP_CLASSES` for rows predating G6.3 (or rows
    written when ``tenant_id`` was unresolved, which carry no effective
    key).
    """
    effective = row.payload.get("broadcast_detail_effective")
    if isinstance(effective, str) and effective:
        return effective == "aggregate"
    return op_class in AGGREGATE_ONLY_OP_CLASSES


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 guard.
_require_ui_session_dep = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)


def build_event_router() -> APIRouter:
    """Construct the broadcast event-detail :class:`APIRouter`.

    Registers ``GET /ui/broadcast/event/{audit_id}``. Returns the drawer
    fragment on success and a small 404 fragment for a missing /
    cross-tenant id; both responses are designed for HTMX swap into
    ``#event-drawer``.
    """
    router = APIRouter(tags=["ui-broadcast"])

    async def _handler(
        request: Request,
        audit_id: uuid.UUID,
        event_id: str = Query(
            default="",
            max_length=128,
            description="Broadcast (Valkey-stream) event id, for display only.",
        ),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/broadcast/event/{audit_id}[?event_id=...]``.

        Resolves the audit row tenant-scoped, classifies its op for the
        aggregate-only gate, and renders ``broadcast/_event_drawer.html``
        with the full detail (or the 🔒 placeholder for sensitive
        classes). A missing / cross-tenant id renders the not-found
        fragment with HTTP 404.
        """
        row = await _fetch_audit_row(
            db_session,
            tenant_id=session_ctx.tenant_id,
            audit_id=audit_id,
        )
        if row is None:
            return get_templates().TemplateResponse(
                request,
                "broadcast/_event_drawer_not_found.html",
                {"audit_id": str(audit_id)},
                status_code=404,
            )

        op_id = _resolve_op_id(row)
        op_class = classify_op(op_id)
        aggregate_only = _is_aggregate_only(row, op_class)
        # Strip the audit-only classification + G6.3 forensic keys so
        # the drawer's "request payload" section shows only the request
        # params. Only computed for the full-detail path -- the
        # aggregate-only branch never renders the payload at all.
        request_payload = (
            {}
            if aggregate_only
            else {k: v for k, v in row.payload.items() if k not in _INTERNAL_PAYLOAD_KEYS}
        )
        context = {
            "row": row,
            "op_id": op_id,
            "op_class": op_class,
            "aggregate_only": aggregate_only,
            "request_payload": request_payload,
            # The ephemeral broadcast id, for display only -- not a PG
            # column. Empty string when the row was opened from a path
            # that did not carry it.
            "event_id": event_id,
        }
        return get_templates().TemplateResponse(request, "broadcast/_event_drawer.html", context)

    router.add_api_route(
        "/ui/broadcast/event/{audit_id}",
        _handler,
        methods=["GET"],
        name="ui_broadcast_event_detail",
        response_class=HTMLResponse,
        responses={
            404: {
                "description": (
                    "Audit id does not exist in this tenant (or exists only "
                    "for another tenant). Returns the not-found drawer fragment."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router
