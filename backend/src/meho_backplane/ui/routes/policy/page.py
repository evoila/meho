# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/policy`` -- the tenant's policy, read only.

The page shows two lists:

* the fixed rules (:data:`~meho_backplane.policy.fixed_rules.FIXED_RULES`),
  each with its id ``floor:<id>`` and its sentence. They hold whatever the
  policy says;
* the risk-level edits: the same rows as the override report
  (:func:`~meho_backplane.api.v1.operation_overrides.load_override_rows`,
  which calls :func:`~meho_backplane.operations.op_policy.list_overrides`).

The page changes nothing. It has no form and no save.

With the switch ``CUSTOM_POLICY`` off
(:func:`~meho_backplane.policy.switches.custom_policy_on`), the route does
not match (:class:`_PolicySwitchRoute`). Every method and every caller then
gets the same answer as an address that does not exist, and no check of the
page runs.

With the switch on, the checks, in this order:

1. the read floor of every console page
   (:func:`~meho_backplane.ui.auth.middleware.require_ui_operator`): a
   ``read_only`` session gets ``403 operator_required``;
2. the check of the override report
   (:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`,
   ``tenant_admin``): anyone else gets ``403``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from meho_backplane.api.v1.operation_overrides import load_override_rows
from meho_backplane.policy import fixed_rules
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["POLICY_NEEDS_ADMIN", "build_policy_router"]

#: The 403 text of the page for a person who may not read the overrides.
POLICY_NEEDS_ADMIN = "The policy page needs the tenant_admin role."

_require_operator_dep = Depends(require_ui_operator)


class _PolicySwitchRoute(APIRoute):
    """A route that matches only with the switch ``CUSTOM_POLICY`` on.

    The switch is read on each request to this address, so a change of the
    setting needs no new router. A request to any other address never reads
    the settings here.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match is Match.NONE or not custom_policy_on(get_settings()):
            return Match.NONE, {}
        return match, child_scope


async def _policy_page(
    request: Request,
    session_ctx: UISessionContext = _require_operator_dep,
) -> HTMLResponse:
    """Render the page, after the checks of the module docstring."""
    try:
        operator = await resolve_operator_or_403(request, session_ctx)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        raise HTTPException(status_code=403, detail=POLICY_NEEDS_ADMIN) from exc
    rows = await load_override_rows(operator, reduced_approval=False)
    return get_templates().TemplateResponse(
        request,
        "policy/index.html",
        {
            "active_surface": "policy",
            "fixed_rules": [
                (f"floor:{rule_id}", sentence)
                for rule_id, sentence in fixed_rules.FIXED_RULES.items()
            ],
            "rows": [row.model_dump() for row in rows],
        },
    )


def build_policy_router() -> APIRouter:
    """Construct the policy page router (a factory, like its siblings)."""
    router = APIRouter(tags=["ui-policy"])
    router.add_api_route(
        "/ui/policy",
        _policy_page,
        methods=["GET"],
        name="ui_policy",
        response_class=HTMLResponse,
        # An HTML page for the browser; it stays out of the API file and the Go client.
        include_in_schema=False,
        route_class_override=_PolicySwitchRoute,
    )
    return router
