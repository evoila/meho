# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-principal register + revoke (DaisyUI modal, HTMX).

Initiative #1824 (G10.8 Agents console), Task #1831 (T4). The read
surface (the principals list) lives in
:mod:`~meho_backplane.ui.routes.agents.principals_views`; this module
layers the two write operations on top:

* **``GET /ui/agents/principals/register``** -- HTMX-loaded register
  modal.
* **``POST /ui/agents/principals/register``** -- submit handler. Calls
  :meth:`~meho_backplane.auth.agent_principals.AgentPrincipalService.register`
  in-process (the same code path the REST surface
  :mod:`meho_backplane.api.v1.agent_principals` and the Go CLI use), so
  the UI register creates the Keycloak client + Vault credential through
  the one validated lifecycle. Register is an **upstream side-effecting
  op**: a Keycloak-unconfigured (503) / Keycloak-API (502) / Vault-write
  (502) failure re-renders the modal with the *actionable* backend detail
  text, never a generic "something went wrong".
* **``GET /ui/agents/principals/{name}/revoke``** -- HTMX-loaded revoke
  confirm modal. Revoke is the **kill switch**: it is terminal and
  destructive (disables the Keycloak client and blocks all new token
  grants for the identity), so the modal demands a **type-to-confirm of
  the principal name** before the submit button enables.
* **``POST /ui/agents/principals/{name}/revoke``** -- submit handler.
  Verifies the typed confirmation matches the principal name
  server-side (defence-in-depth on top of the client-side gate), then
  calls
  :meth:`~meho_backplane.auth.agent_principals.AgentPrincipalService.revoke`.

RBAC posture
------------

Register / revoke are **tenant_admin only** and the gate is server-side:
both write routes depend on
:func:`~meho_backplane.ui.routes.agents.operator.resolve_operator_or_403`,
which lifts the full :class:`~meho_backplane.auth.operator.Operator`
from the BFF session, re-validates the access token through the chassis
JWT chain, and raises 403 for a non-admin caller. The list template
additionally hides the affordances from non-admins (UX) -- a crafted
POST still hits the 403.

CSRF posture
------------

``POST`` under ``/ui/`` is gated by the chassis
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` (signed double-submit)
before the handler runs. Each modal render re-mints + re-sets the
``meho_csrf`` cookie and the form declares its own ``hx-headers`` echo so
the double-submit pair lines up (#1693).

Error contract
--------------

The register failure modes map to the same three-clause / bare detail
shapes the REST surface emits, surfaced inline in the modal banner:

* :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminNotConfiguredError`
  -> the gold-standard 503 detail
  (:data:`~meho_backplane.auth.keycloak_admin.KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL`).
* any other
  :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError`
  -> the bare ``keycloak_admin_error`` code (502; the remediation depends
  on the upstream fault and naming one would speculate).
* :class:`~meho_backplane.scheduler.vault_credentials.SchedulerVaultBrokerError`
  -> ``scheduler_vault_write_error`` (502; Vault is configured but the
  credential write failed and the just-created Keycloak client was rolled
  back) -- unless the broker's ``lookup-self`` probe found the scheduler
  token itself dead (#2652), in which case the banner carries the
  gold-standard
  :data:`~meho_backplane.scheduler.vault_credentials.SCHEDULER_VAULT_TOKEN_INVALID_DETAIL`
  naming the re-mint. Same 502 either way; only the remediation differs.
"""

from __future__ import annotations

import structlog
from fastapi import Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.agent_principals import (
    AgentPrincipalCreate,
    AgentPrincipalExistsError,
    AgentPrincipalNotFoundError,
    AgentPrincipalService,
)
from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.scheduler.vault_credentials import (
    SCHEDULER_VAULT_TOKEN_INVALID_DETAIL,
    SchedulerVaultBrokerError,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.principals_views import fetch_principal_or_404
from meho_backplane.ui.templating import get_templates

__all__ = [
    "NAME_MAX",
    "OWNER_SUB_MAX",
    "render_register_modal",
    "render_revoke_modal",
    "submit_register",
    "submit_revoke",
]

_log = structlog.get_logger(__name__)

#: Form-field length caps. The ``name`` cap mirrors the path-param +
#: service safe-name bound; ``owner_sub`` is a Keycloak ``sub`` (a UUID
#: or short login), bounded generously. The server-side validation is
#: authoritative; these caps bound the form-body parse before the bytes
#: reach the service.
NAME_MAX: int = 128
OWNER_SUB_MAX: int = 256


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the chassis CSRF cookie posture for the modal renders."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _redirect_to_list() -> HTMLResponse:
    """Return a 204 + ``HX-Redirect`` so HTMX reloads the principals list."""
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/agents/principals"},
    )


def _register_context(csrf_token: str) -> dict[str, object]:
    """Build the bare register-modal template context (no errors / values)."""
    return {
        "page_title": "Agent principals",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "errors": {},
        "banner": None,
        "values": {},
    }


async def render_register_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded register modal fragment (tenant_admin-gated)."""
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "agents/principals/_register_modal.html",
        _register_context(csrf_token),
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _render_register_with_error(
    request: Request,
    *,
    csrf_session_id: str,
    values: dict[str, object],
    field_errors: dict[str, str] | None = None,
    banner: str | None = None,
    status_code: int,
) -> HTMLResponse:
    """Re-render the register modal carrying a field error and/or a banner.

    ``field_errors`` carries per-field messages (the name / owner_sub
    validation + duplicate-name paths); ``banner`` carries the actionable
    backend detail for the upstream side-effect failures (503 Keycloak
    unconfigured, 502 Keycloak API, 502 Vault write) -- those are not
    field-attributable, so they render as an alert banner above the form,
    not against a single input. The operator keeps their typed ``values``.
    """
    csrf_token = mint_csrf_token(csrf_session_id)
    context = _register_context(csrf_token)
    context["errors"] = field_errors or {}
    context["banner"] = banner
    context["values"] = values
    response = get_templates().TemplateResponse(
        request,
        "agents/principals/_register_modal.html",
        context,
        status_code=status_code,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def submit_register(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    owner_sub: str | None,
) -> HTMLResponse:
    """Register a new agent principal via the lifecycle service.

    Calls the service in-process so the UI register and the REST register
    share the one Keycloak-client-create + Vault-credential-write +
    DB-row-insert path. A clean register returns 204 +
    ``HX-Redirect: /ui/agents/principals``; every failure re-renders the
    modal:

    * empty / bad-character ``name`` (``ValueError``) -> 422 ``name`` field
      error.
    * duplicate ``(tenant, name)`` (:class:`AgentPrincipalExistsError`)
      -> 409 ``name`` field error.
    * Keycloak admin unconfigured -> 503 banner (the gold-standard
      three-clause detail).
    * other Keycloak API failure -> 502 banner (bare code).
    * Vault credential write failure -> 502 banner: the bare
      ``scheduler_vault_write_error`` code when the scheduler token is
      live (policy scope is the fault), the three-clause re-mint detail
      when the broker's ``lookup-self`` probe found it dead (#2652).
    """
    owner_clean = (owner_sub or "").strip() or None
    raw_values: dict[str, object] = {"name": name, "owner_sub": owner_clean or ""}

    service = AgentPrincipalService()
    try:
        await service.register(
            tenant_id=session_ctx.tenant_id,
            created_by_sub=operator.sub,
            payload=AgentPrincipalCreate(name=name, owner_sub=owner_clean),
        )
    except ValueError as exc:
        # The service raises ``ValueError`` for a name outside the safe
        # alphabet (the name validity is enforced in ``register``, not on
        # the Pydantic model).
        return _render_register_with_error(
            request,
            csrf_session_id=str(session_ctx.session_id),
            values=raw_values,
            field_errors={"name": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except AgentPrincipalExistsError:
        return _render_register_with_error(
            request,
            csrf_session_id=str(session_ctx.session_id),
            values=raw_values,
            field_errors={"name": "an agent principal with this name already exists"},
            status_code=status.HTTP_409_CONFLICT,
        )
    except KeycloakAdminNotConfiguredError:
        return _render_register_with_error(
            request,
            csrf_session_id=str(session_ctx.session_id),
            values=raw_values,
            banner=KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except KeycloakAdminError:
        return _render_register_with_error(
            request,
            csrf_session_id=str(session_ctx.session_id),
            values=raw_values,
            banner="keycloak_admin_error",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except SchedulerVaultBrokerError as exc:
        return _render_register_with_error(
            request,
            csrf_session_id=str(session_ctx.session_id),
            values=raw_values,
            banner=(
                SCHEDULER_VAULT_TOKEN_INVALID_DETAIL
                if exc.token_invalid
                else "scheduler_vault_write_error"
            ),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    _log.info(
        "ui_agent_principal_register",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=name,
    )
    del request  # HX-Redirect needs no request context.
    return _redirect_to_list()


async def render_revoke_modal(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
) -> HTMLResponse:
    """Render the revoke (kill-switch) confirm modal.

    404 on an absent / cross-tenant / already-revoked name (the
    ``fetch_principal_or_404`` returns 404 for the first two; an
    already-revoked principal renders the modal but the submit then 404s,
    matching the service's revoke contract). The modal carries the
    type-to-confirm gate: the operator must type the principal name
    verbatim before the destructive submit enables.
    """
    principal = await fetch_principal_or_404(session_ctx, name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agent principals",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "principal": {
            "name": principal.name,
            "keycloak_client_id": principal.keycloak_client_id,
        },
        "banner": None,
    }
    response = get_templates().TemplateResponse(
        request,
        "agents/principals/_revoke_modal.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _render_revoke_with_error(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
    keycloak_client_id: str,
    banner: str,
    status_code: int,
) -> HTMLResponse:
    """Re-render the revoke modal with an actionable banner + a fresh token.

    Used for the upstream side-effect failures (503 Keycloak unconfigured,
    502 Keycloak API) the kill switch can hit -- they re-render the modal
    so the operator sees the actionable detail and can retry, rather than
    a generic error page swapping over the dialog.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Agent principals",
        "active_surface": "agents",
        "csrf_token": csrf_token,
        "principal": {"name": name, "keycloak_client_id": keycloak_client_id},
        "banner": banner,
    }
    response = get_templates().TemplateResponse(
        request,
        "agents/principals/_revoke_modal.html",
        context,
        status_code=status_code,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def submit_revoke(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    name: str,
    confirm_name: str,
) -> HTMLResponse:
    """Revoke an agent principal (the Keycloak kill switch).

    Verifies the typed ``confirm_name`` matches the principal name
    server-side (defence-in-depth: the client-side Alpine gate can be
    bypassed by a crafted POST, so the type-to-confirm is re-checked
    here) before disabling the Keycloak client. A clean revoke returns
    204 + ``HX-Redirect: /ui/agents/principals``; the failure modes:

    * confirmation mismatch -> 422, the modal re-renders with the typed
      banner (no service call is made).
    * absent / cross-tenant / already-revoked name
      (:class:`AgentPrincipalNotFoundError`) -> 404.
    * Keycloak admin unconfigured -> 503 banner; other Keycloak API
      failure -> 502 banner (the DB row is left active so the operator
      can retry once Keycloak recovers).
    """
    del operator  # gate only; the service write is tenant-scoped by session.
    if confirm_name.strip() != name:
        # The client-side gate should prevent this; re-checking it
        # server-side keeps a crafted POST from skipping the confirm. No
        # service call is made -- nothing is revoked.
        principal = await fetch_principal_or_404(session_ctx, name)
        return _render_revoke_with_error(
            request,
            session_ctx,
            name=name,
            keycloak_client_id=principal.keycloak_client_id,
            banner=(
                "the typed name did not match -- type the principal name "
                "exactly to confirm this kill switch"
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    service = AgentPrincipalService()
    try:
        await service.revoke(session_ctx.tenant_id, name)
    except AgentPrincipalNotFoundError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent_principal_not_found",
        ) from exc
    except KeycloakAdminNotConfiguredError:
        principal = await fetch_principal_or_404(session_ctx, name)
        return _render_revoke_with_error(
            request,
            session_ctx,
            name=name,
            keycloak_client_id=principal.keycloak_client_id,
            banner=KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except KeycloakAdminError:
        principal = await fetch_principal_or_404(session_ctx, name)
        return _render_revoke_with_error(
            request,
            session_ctx,
            name=name,
            keycloak_client_id=principal.keycloak_client_id,
            banner="keycloak_admin_error",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    _log.info(
        "ui_agent_principal_revoke",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=name,
    )
    del request
    return _redirect_to_list()
