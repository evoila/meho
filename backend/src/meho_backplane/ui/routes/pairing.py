# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/pairing`` — the add-on pairing registry + health panel (#3025).

The operator-console face of the add-on pairing contract (Initiative #2900).
One glance answers "which add-ons are paired / which are still
contract-compatible with this backplane / which have gone quiet?": each row
shows a pairing's add-on name, its negotiated contract version, whether that
version is still compatible with the current backplane build (both-direction
skew re-evaluated live via
:func:`~meho_backplane.operations.addon_pairing_contract.is_contract_compatible`),
its last liveness heartbeat, and when it was paired.

The route serves two response shapes from one handler (the sensors / runners
mould): the full ``pairing/list.html`` page on a browser navigation, and the
``pairing/_table_rows.html`` fragment on an ``HX-Request`` (the 30s
auto-refresh poll).

Reads at ``operator`` visibility through the in-process
:class:`~meho_backplane.operations.addon_pairing.AddonPairingService`
``list_`` (the same accessor the Bearer ``GET /api/v1/addons/pairings``
route uses) rather than the REST surface, because a browser carrying only
the BFF session cookie cannot authenticate the Bearer route. Tenant scoping
is non-overrideable — the service's first WHERE clause is the session's
``tenant_id``. Read-only: pair / unpair stay on the REST surface.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.operations.addon_pairing import AddonPairingService
from meho_backplane.operations.addon_pairing_contract import (
    BACKPLANE_CONTRACT_VERSION,
    is_contract_compatible,
)
from meho_backplane.operations.addon_pairing_schemas import PairedAddonRead
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.checks.views import coerce_utc_aware
from meho_backplane.ui.templating import get_templates

__all__ = ["build_pairing_router"]

#: Hard cap on the pairings a single list render considers. The registry is a
#: glance surface; a tenant with more paired add-ons than this has a problem
#: the list view is not the place to page through.
_LIST_LIMIT = 200

#: Module-level ``Depends`` closure — ruff B008 idiom, matching the sensors /
#: runners routes.
_require_session_dep = Depends(require_ui_session)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def project_pairing_to_row(pairing: PairedAddonRead) -> dict[str, object]:
    """Project one pairing read-model onto the flat template row.

    ``contract_compatible`` is recomputed against the current backplane
    contract so a pairing left behind by an upgrade reads as incompatible
    without a re-handshake; ``compat_badge`` selects the DaisyUI badge class.
    """
    compatible = is_contract_compatible(
        addon_contract_version=pairing.addon_contract_version,
        addon_min_backplane_version=pairing.addon_min_backplane_version,
    )
    return {
        "addon": pairing.name,
        "contract_version": pairing.contract_version,
        "contract_compatible": compatible,
        "compat_badge": "badge-success" if compatible else "badge-error",
        "compat_label": "compatible" if compatible else "incompatible",
        "last_seen": coerce_utc_aware(pairing.last_seen_at),
        "paired_at": coerce_utc_aware(pairing.paired_at),
        "owner_sub": pairing.owner_sub,
    }


def build_pairing_router() -> APIRouter:
    """Construct the add-on pairing registry :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without sharing route state — the chassis convention.
    Registers the single ``GET /ui/pairing`` route serving both the full page
    and the HTMX fragment from one handler.
    """
    router = APIRouter(tags=["ui-pairing"])

    async def _handler(
        request: Request,
        session_ctx: UISessionContext = _require_session_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/pairing``. See module docstring."""
        pairings = await AddonPairingService().list_(
            session_ctx.tenant_id,
            limit=_LIST_LIMIT,
        )
        rows = [project_pairing_to_row(p) for p in pairings]
        context: dict[str, object] = {
            "page_title": "Pairing",
            "active_surface": "pairing",
            "rows": rows,
            "backplane_contract_version": BACKPLANE_CONTRACT_VERSION,
            "now_utc": datetime.now(UTC),
        }
        template_name = (
            "pairing/_table_rows.html" if _is_htmx_request(request) else "pairing/list.html"
        )
        return get_templates().TemplateResponse(request, template_name, context)

    router.add_api_route(
        "/ui/pairing",
        _handler,
        methods=["GET"],
        name="ui_pairing_list",
        response_class=HTMLResponse,
    )
    return router
