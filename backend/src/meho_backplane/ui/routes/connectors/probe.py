# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /ui/connectors/{name}/probe`` -- tenant_admin re-probe action.

Initiative #340 (G10.3 Connectors + Targets UI), Task #873 (T1) work
item #6. The re-probe button on the detail page (fingerprint card)
posts here via HTMX (``hx-post`` + ``hx-target="#fingerprint-card"``
+ ``hx-swap="outerHTML"``). The handler:

1. Lifts the full operator via
   :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`,
   which re-validates the access token through the chassis JWT chain
   and gates on :class:`~meho_backplane.auth.operator.TenantRole.TENANT_ADMIN`
   server-side. A non-admin call raises 403 -- the template's
   ``hx-on::after-request`` handler renders the alert.

2. Resolves the target tenant-scoped via
   :func:`~meho_backplane.targets.resolver.resolve_target`. 404 on
   a cross-tenant or near-miss name (alias-aware match).

3. Re-runs the connector probe through the same shared helper
   :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
   the ``/api/v1/targets/{name}/probe`` REST route uses, so the two
   surfaces stay byte-compatible -- a future change to the
   resolver's tie-break ladder takes effect on both at once. The
   resolver's two error labels are surfaced as alert HTML fragments
   so HTMX can swap them into the fingerprint card slot without a
   full-page reload:

   * ``no_connector`` -> 501 + alert fragment naming the missing
     ``(product, version)`` pair. The cached fingerprint (if any)
     survives unchanged on the DB row.
   * ``ambiguous_connector`` -> 409 + alert fragment naming the
     candidate set and the remediation step (set
     ``target.preferred_impl_id`` to one of them). Same survival
     semantics.

4. Persists the new :class:`FingerprintResult` to
   ``targets.fingerprint`` and bumps ``updated_at`` -- the same
   write the REST route does, in the same shape, so a re-render of
   the detail page after a UI re-probe is indistinguishable from a
   re-render after a CLI re-probe.

5. Returns the freshly-rendered ``_fingerprint_card.html`` fragment.
   The HTMX swap replaces the previous card in place.

The handler is **server-side authoritative** on the RBAC gate; the
template hides the button when the operator isn't admin, but a
crafted POST hitting this endpoint with a stolen / forged form still
fails on the 403 from
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.resolver import resolve_connector_or_label
from meho_backplane.db.engine import get_session
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.detail import _project_target
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["build_probe_router"]

_log = structlog.get_logger(__name__)


def _render_alert(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
) -> HTMLResponse:
    """Render the alert HTML fragment for a probe failure.

    Returned in place of the fingerprint card so the HTMX swap drops
    the alert into the slot without disturbing the rest of the
    detail page. The ``status_code`` is propagated so the template's
    ``hx-on::after-request`` handler can branch on the response code
    (e.g. open the alert toast at the top of the page in addition to
    swapping the slot).
    """
    return get_templates().TemplateResponse(
        request,
        "connectors/_probe_alert.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom.
_require_ui_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)
_require_admin_operator_dep = Depends(resolve_operator_or_403)


async def _do_probe(
    request: Request,
    *,
    target_name: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    operator: Operator,
) -> HTMLResponse:
    """Re-run the connector probe and return the refreshed card or alert."""
    target = await resolve_target(db_session, session_ctx.tenant_id, target_name)
    cls, label, message = resolve_connector_or_label(target)
    if label == "no_connector":
        return _render_alert(
            request,
            title="No matching connector",
            message=message or f"no connector registered for product={target.product!r}",
            status_code=501,
        )
    if label == "ambiguous_connector":
        return _render_alert(
            request,
            title="Connector resolution ambiguous",
            message=message
            or (
                f"connector resolution ambiguous for product={target.product!r}; "
                "set target.preferred_impl_id to disambiguate."
            ),
            status_code=409,
        )
    assert cls is not None  # narrow for mypy; resolver contract guarantees this
    # Forward the route operator so the connector's fingerprint reads
    # per-target Vault credentials under the operator's identity, the
    # same code path the dispatch surface uses. G0.16-T4 (#1306)
    # converged probe + dispatch on this signature; pre-fix the UI
    # re-probe path passed nothing (so the connector synthesised a
    # system operator with a placeholder JWT) and Vault rejected the
    # JWT/OIDC login as ``malformed jwt: must have three parts`` on
    # the four connectors whose fingerprint authenticates via Vault
    # (k8s-1.x, vmware-rest-9.0, sddc-rest-9.0, nsx-rest-4.2).
    fp = await cls().fingerprint(target, operator=operator)
    # ``model_dump(mode='json')`` produces a JSONB-safe dict; plain
    # ``model_dump()`` would leak Python-native types into the JSON
    # column. Mirror the REST route's write shape exactly.
    target.fingerprint = fp.model_dump(mode="json")
    target.updated_at = datetime.now(UTC)
    await db_session.flush()
    _log.info(
        "ui_target_reprobe_success",
        target_id=str(target.id),
        target_name=target.name,
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=operator.sub,
    )
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = {
        "target": _project_target(target),
        "is_tenant_admin": True,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "connectors/_fingerprint_card.html",
        context,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def build_probe_router() -> APIRouter:
    """Construct the re-probe :class:`APIRouter`.

    Registers the single ``POST /ui/connectors/{name}/probe`` route.
    The chassis :class:`~meho_backplane.ui.csrf.CSRFMiddleware`
    enforces the double-submit cookie chain on every state-changing
    UI verb; the template's ``hx-headers`` directive (set on the
    page wrapper) echoes ``X-CSRF-Token`` into the outbound request
    so the gate passes by construction.
    """
    router = APIRouter(tags=["ui-connectors"])

    async def _handler(
        request: Request,
        name: str,
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_session_dep,
        operator: Operator = _require_admin_operator_dep,
    ) -> HTMLResponse:
        """``POST /ui/connectors/{name}/probe``."""
        if not name or len(name) > 200:
            raise HTTPException(status_code=404, detail=f"target {name!r} not found")
        return await _do_probe(
            request,
            target_name=name,
            session_ctx=session_ctx,
            db_session=db_session,
            operator=operator,
        )

    router.add_api_route(
        "/ui/connectors/{name}/probe",
        _handler,
        methods=["POST"],
        name="ui_connectors_reprobe",
        response_class=HTMLResponse,
        responses={
            403: {
                "description": (
                    "Operator is not a tenant_admin. The re-probe verb is "
                    "tenant_admin-only, mirroring the ``/api/v1/targets/"
                    "{name}/probe`` REST route's posture."
                ),
                "content": {"text/html": {}},
            },
            404: {
                "description": ("Target name does not resolve in the caller's tenant."),
                "content": {"text/html": {}},
            },
            409: {
                "description": (
                    "Resolver reported ambiguous connector resolution. The "
                    "alert fragment names the candidate set."
                ),
                "content": {"text/html": {}},
            },
            501: {
                "description": (
                    "Resolver found no matching connector for the target's "
                    "(product, fingerprint.version) pair."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router
