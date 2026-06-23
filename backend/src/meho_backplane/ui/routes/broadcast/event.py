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
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast import classify_op
from meho_backplane.db.engine import get_raw_session
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.broadcast.aggregate_gate import (
    AGGREGATE_ONLY_OP_CLASSES,
    INTERNAL_PAYLOAD_KEYS,
    fetch_audit_row,
    is_aggregate_only,
    resolve_op_id,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["AGGREGATE_ONLY_OP_CLASSES", "build_event_router"]

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
        row = await fetch_audit_row(
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

        op_id = resolve_op_id(row)
        op_class = classify_op(op_id)
        aggregate_only = is_aggregate_only(row, op_class)
        # The "suppress this op" cross-link is gated on the sensitive
        # op-class *set* (decision #3), not the badge colour: of the
        # three, ``credential_read`` / ``credential_mint`` are
        # ``badge-warning`` while ``audit_query`` is ``badge-info``, so
        # gating on the colour would miss audit_query. The link offers a
        # tenant_admin a shortcut into the Overrides tab with this op_id
        # pre-filled; the create still goes through the gated POST.
        is_sensitive = op_class in AGGREGATE_ONLY_OP_CLASSES
        # Strip the audit-only classification + G6.3 forensic keys so
        # the drawer's "request payload" section shows only the request
        # params. Only computed for the full-detail path -- the
        # aggregate-only branch never renders the payload at all.
        request_payload = (
            {}
            if aggregate_only
            else {k: v for k, v in row.payload.items() if k not in INTERNAL_PAYLOAD_KEYS}
        )
        context = {
            "row": row,
            "op_id": op_id,
            "op_class": op_class,
            "aggregate_only": aggregate_only,
            "is_sensitive": is_sensitive,
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
