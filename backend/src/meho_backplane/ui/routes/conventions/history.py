# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conventions UI history diff panel render helper.

Initiative #1838 (G10.12 Conventions console), Task #1896 (T2). Split
out of :mod:`~meho_backplane.ui.routes.conventions.write` so neither
module exceeds the chassis-wide ~600-line cap, and because the history
panel is the one **read** surface in T2 (OPERATOR-tier, gated like the
detail view) -- keeping it apart from the tenant_admin write helpers
keeps the read/write split legible.

The panel renders the ``tenant_convention_history`` rows newest-first
(the service orders by ``ts DESC``) with a per-row ``body_before`` ->
``body_after`` diff. Bodies render as escaped plain text in the
template's ``<pre>`` blocks, so a body carrying raw HTML never executes
in the diff view.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.conventions.service import (
    ConventionNotFoundError,
    ConventionsService,
)
from meho_backplane.ui.routes.conventions.operator import ConventionsReadContext
from meho_backplane.ui.templating import get_templates

__all__ = ["render_history_panel"]

#: One shared service instance -- stateless (every method takes the
#: session), mirroring the read views' module-level singleton.
_service = ConventionsService()


async def render_history_panel(
    request: Request,
    read_ctx: ConventionsReadContext,
    *,
    session: AsyncSession,
    slug: str,
) -> HTMLResponse:
    """Render the history diff panel, newest-first.

    Each row carries the ``body_before`` -> ``body_after`` diff; the
    CREATE row (``body_before is None``) renders with no "before" pane.
    Bodies are escaped (rendered as plain text in ``<pre>``), so a body
    containing raw HTML never executes in the diff view.
    """
    try:
        entries = await _service.list_history(
            session=session,
            tenant_id=read_ctx.operator.tenant_id,
            slug=slug,
        )
    except ConventionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="convention_not_found") from exc
    rows = [
        {
            "body_before": entry.body_before,
            "body_after": entry.body_after,
            "actor_sub": entry.actor_sub,
            "ts": entry.ts,
            "audit_id": str(entry.audit_id) if entry.audit_id is not None else None,
            "is_create": entry.body_before is None,
        }
        for entry in entries
    ]
    context: dict[str, object] = {"slug": slug, "history": rows}
    return get_templates().TemplateResponse(request, "conventions/_history_panel.html", context)
