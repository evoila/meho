# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/automation`` — the paired-automation console panel (Task #3029).

The operator-console face of Initiative #2900's paired-surface activation, and
the console twin of the ``meho_automation_list`` meta-tool / ``meho automation
list`` CLI verb. One glance answers "is a governed automation add-on live in
this tenant, and what surface does it advertise?": each provider row shows the
add-on's negotiated contract version, whether that version is still compatible
with this backplane (both-direction skew re-evaluated live), its last liveness
heartbeat, when it was paired, and the surfaces it declares (meta-tool families,
CLI verb families, console panels, event kinds).

The panel is **gated on activation, not registration** (the console analogue of
the meta-tool's true-absence gate): it renders only the surfaces of a paired,
contract-healthy add-on advertising the ``automation`` meta-tool family — read
through the shared
:func:`~meho_backplane.operations.addon_automation.active_automation_surface`
projection every twin uses, so the console never diverges from what the agent
and CLI see. When nothing is active the panel shows the inactive empty state —
the surface "disappears cleanly on unpair".

Serves two response shapes from one handler (the pairing / sensors mould): the
full ``automation/list.html`` page on a browser navigation, and the
``automation/_table_rows.html`` fragment on an ``HX-Request`` (the 30s
auto-refresh poll). Read-only; tenant scoping is non-overrideable (the session's
``tenant_id``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.operations.addon_automation import active_automation_surface
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.checks.views import coerce_utc_aware
from meho_backplane.ui.templating import get_templates

__all__ = ["build_automation_router"]

#: Module-level ``Depends`` closure — ruff B008 idiom, matching the pairing /
#: sensors / runners routes.
_require_session_dep = Depends(require_ui_session)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def build_automation_router() -> APIRouter:
    """Construct the paired-automation panel :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without sharing route state — the chassis convention.
    Registers the single ``GET /ui/automation`` route serving both the full
    page and the HTMX fragment from one handler.
    """
    router = APIRouter(tags=["ui-automation"])

    async def _handler(
        request: Request,
        session_ctx: UISessionContext = _require_session_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/automation``. See module docstring."""
        surface = await active_automation_surface(session_ctx.tenant_id)
        rows = [
            {
                "addon": provider.addon,
                "contract_version": provider.contract_version,
                "contract_compatible": provider.contract_compatible,
                "compat_badge": (
                    "badge-success" if provider.contract_compatible else "badge-error"
                ),
                "compat_label": ("compatible" if provider.contract_compatible else "incompatible"),
                "paired_at": coerce_utc_aware(provider.paired_at),
                "last_seen": coerce_utc_aware(provider.last_seen_at),
                "surfaces": [
                    {
                        "kind": entry.kind.value,
                        "name": entry.name,
                        "display_label": entry.display_label,
                    }
                    for entry in provider.surfaces
                ],
            }
            for provider in surface.providers
        ]
        context: dict[str, object] = {
            "page_title": "Automation",
            "active_surface": "automation",
            "rows": rows,
            "now_utc": datetime.now(UTC),
        }
        template_name = (
            "automation/_table_rows.html" if _is_htmx_request(request) else "automation/list.html"
        )
        return get_templates().TemplateResponse(request, template_name, context)

    router.add_api_route(
        "/ui/automation",
        _handler,
        methods=["GET"],
        name="ui_automation_list",
        response_class=HTMLResponse,
    )
    return router
