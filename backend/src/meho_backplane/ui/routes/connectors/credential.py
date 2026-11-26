# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The credential card on a target's page: show it, and store a credential.

``GET /ui/connectors/{name}/credential`` returns the card. The target page
loads it when it opens. ``POST /ui/connectors/{name}/credential`` stores the
credential the person typed.

The card shows the path the credential is stored at (the target's own
``secret_ref``), whether a credential is stored there with its version and
time (never a value), and a form built from the ``credential_ways`` the
connector declares. A connector that declares nothing gets no form: the card
sends the person to the command line. On an install that keeps credentials
in Google Secret Manager the card has no form and names the secret to create.

The submit runs ``vault.kv.put`` in process with only the fields of the chosen
way, through the same helper as the Vault page. So the write waits for a
person's approval, as it does today. The value is typed in the console and
goes only to ``vault.kv.put``: it is never logged, never put in an error text
and never sent back to the page.

Who may submit: the ``operator`` role, the same floor as the Vault page. A
person below it sees the card without a form, and a POST answers 403.
See ``docs/codebase/ui.md``, section "Credential card on the target page".
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.ui.auth.middleware import (
    UISessionContext,
    require_ui_operator,
    require_ui_session,
)
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import parked_request_chain, set_csrf_cookie
from meho_backplane.ui.routes.connectors.credential_plan import (
    CredentialPlan,
    StoredStatus,
    plan_credential_card,
    read_credential_form,
    read_stored_status,
)
from meho_backplane.ui.routes.connectors.forms import target_page_path
from meho_backplane.ui.routes.connectors.operator import lift_operator_from_session
from meho_backplane.ui.routes.vault.routes import _vault_connector_id
from meho_backplane.ui.routes.vault.writes import _dispatch_write
from meho_backplane.ui.templating import get_templates

__all__ = ["build_credential_router"]

_log = structlog.get_logger(__name__)

_OP_PUT: Final[str] = "vault.kv.put"
_MAX_NAME_LENGTH: Final[int] = 200
_GUIDE_URL: Final[str] = (
    "https://evoila.github.io/meho/guides/targets-and-secrets/#store-the-credential-through-meho"
)

_require_session_dep = Depends(require_ui_session)
_require_operator_dep = Depends(require_ui_operator)
_get_raw_session_dep = Depends(get_raw_session)


async def _load_target(db_session: AsyncSession, tenant_id: Any, name: str) -> TargetORM:
    """Resolve the target in the session's tenant; a long name is a 404 at once."""
    if not name or len(name) > _MAX_NAME_LENGTH:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"target {name!r} not found")
    return await resolve_target(db_session, tenant_id, name)


async def _render_card(
    request: Request,
    session_ctx: UISessionContext,
    target: TargetORM,
    operator: Operator,
    *,
    result: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    chosen_way: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render the card. It never gets a submitted value: the form comes back blank."""
    plan: CredentialPlan = await plan_credential_card(operator, target)
    can_write = operator.tenant_role in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN)
    stored = StoredStatus(state="skipped")
    if plan.state == "form" and can_write:
        connector_id = await _vault_connector_id(operator)
        stored = await read_stored_status(operator, connector_id, plan.mount, plan.path)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "target_name": target.name,
        "card_url": f"{target_page_path(target.name)}/credential",
        "plan": plan,
        "stored": stored,
        "can_write": can_write,
        "is_tenant_admin": operator.tenant_role == TenantRole.TENANT_ADMIN,
        "result": result,
        "errors": errors or {},
        "chosen_way": chosen_way or (plan.ways[0].key if plan.ways else ""),
        "guide_url": _GUIDE_URL,
        "csrf_token": csrf_token,
        "chain": await parked_request_chain(operator.tenant_id, result),
    }
    response = get_templates().TemplateResponse(
        request, "connectors/_credential_card.html", context, status_code=status_code
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _store_credential(
    request: Request,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    name: str,
) -> HTMLResponse:
    """Read the form, run ``vault.kv.put`` and render the card with the answer."""
    operator = await lift_operator_from_session(session_ctx)
    target = await _load_target(db_session, session_ctx.tenant_id, name)
    plan = await plan_credential_card(operator, target)
    if plan.state != "form":
        return await _render_card(
            request, session_ctx, target, operator, status_code=status.HTTP_409_CONFLICT
        )
    form = read_credential_form(await request.form(), plan.ways)
    if form.way is None or form.errors:
        return await _render_card(
            request,
            session_ctx,
            target,
            operator,
            errors=form.errors,
            chosen_way=form.way.key if form.way else None,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    connector_id = await _vault_connector_id(operator)
    if connector_id is None:
        result: dict[str, Any] = {"status": "no_vault_connector"}
    else:
        params = {"mount": plan.mount, "path": plan.path, "data": form.data}
        result = await _dispatch_write(operator, connector_id, _OP_PUT, "", params)
    _log.info(
        "ui_target_credential_store",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        target=target.name,
        way=form.way.key,
        status=result.get("status"),
    )
    return await _render_card(
        request, session_ctx, target, operator, result=result, chosen_way=form.way.key
    )


def build_credential_router() -> APIRouter:
    """Construct the router of the credential card (GET the card, POST the credential)."""
    router = APIRouter(tags=["ui-connectors"])

    async def _card_handler(
        request: Request,
        name: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/connectors/{name}/credential``."""
        operator = await lift_operator_from_session(session_ctx)
        target = await _load_target(db_session, session_ctx.tenant_id, name)
        return await _render_card(request, session_ctx, target, operator)

    async def _store_handler(
        request: Request,
        name: str,
        session_ctx: UISessionContext = _require_operator_dep,
        db_session: AsyncSession = _get_raw_session_dep,
    ) -> HTMLResponse:
        """``POST /ui/connectors/{name}/credential``."""
        return await _store_credential(request, session_ctx, db_session, name)

    router.add_api_route(
        "/ui/connectors/{name}/credential",
        _card_handler,
        methods=["GET"],
        name="ui_connectors_credential_card",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/{name}/credential",
        _store_handler,
        methods=["POST"],
        name="ui_connectors_credential_store",
        response_class=HTMLResponse,
    )
    return router
