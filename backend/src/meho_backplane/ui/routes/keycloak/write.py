# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak console write routes: client-scope + protocol-mapper authoring.

Initiative #1943 (G10.x Keycloak console), Task #1961 (T3). Adds the two
approval-gated authoring writes on top of T1's (#1959) read-only realm
browser:

* ``POST /ui/keycloak/client-scopes/create`` -> ``keycloak.client_scope.create``
* ``POST /ui/keycloak/clients/{client_uuid}/protocol-mappers/create``
  -> ``keycloak.protocol_mapper.create``

Both ops are registered ``requires_approval=True`` / ``safety_level="caution"``
(:mod:`meho_backplane.connectors.keycloak.ops_write`). The protocol-mapper
op is the one that wires the ``tenant_id`` / ``tenant_role`` claims the
backplane row-scopes on.

Why a session BFF, not a REST surface
-------------------------------------

Same as the read scaffold (see :mod:`meho_backplane.ui.routes.keycloak.routes`):
there is **no** ``/api/v1/keycloak`` REST surface. Every CLI keycloak write
verb is a thin Cobra layer over ``POST /api/v1/operations/call`` against the
pinned ``connector_id="keycloak-admin-26.x"``. So these writes dispatch the
op ids in-process through
:func:`~meho_backplane.operations.meta_tools.call_operation` -- the exact
confirm-gated, CSRF-protected pattern the MERGED ``/ui/operations`` Run
modal uses (:mod:`meho_backplane.ui.routes.operations.routes`).

Confirm + CSRF
--------------

Each write is reached from an explicit, unmissable confirm modal
(``GET /ui/keycloak/client-scopes/create`` /
``GET /ui/keycloak/clients/{client_uuid}/protocol-mappers/create``) that
names the ``caution`` safety level and the requires-approval handoff before
the operator can POST. The modal mints a fresh CSRF token + re-sets the
``meho_csrf`` cookie (the ``mint_csrf_token`` / ``set_csrf_cookie`` pair) so
the double-submit pair lines up after the HTMX swap rotated it; the confirm
button carries the token on its OWN ``hx-headers`` (HTMX does not inherit
``hx-headers`` to children). Every ``POST`` under ``/ui/`` is CSRF-gated by
the :mod:`meho_backplane.ui.csrf` double-submit middleware regardless.

Approval handoff
----------------

Because both ops are ``requires_approval=True``, the dispatcher's policy
gate returns ``status="awaiting_approval"`` with
``extras["approval_request_id"]`` -- the write never executes immediately.
The result fragment surfaces that id and DEEP-LINKS ``/ui/approvals`` so the
operator hands off to a reviewer rather than seeing a silent success.

RBAC (soft-hide + hard-403)
---------------------------

The underlying dispatch is operator-tier with a policy gate (the Bearer
twin gates only on ``TenantRole.OPERATOR``); ``requires_approval`` -- not
RBAC -- is what routes both writes to ``awaiting_approval``. This BFF layer
adds a ``tenant_admin`` gate on top for both writes, matching the existing
UI write routes (connectors / agents / conventions all gate writes with
``resolve_operator_or_403``), justified by blast radius. The split:

* The create affordances on the read surfaces are hidden from a plain
  operator via the soft-failing :func:`resolve_role_probe`
  (``is_tenant_admin``).
* The confirm modals + the write POST handlers gate server-side with
  :func:`_resolve_keycloak_admin_or_403` so a forged POST from a non-admin
  returns 403 even when the affordance was hidden.

Tenant scope is ``operator.tenant_id`` only -- no tenant-override param (a
cross-tenant target is simply absent from the operator's tenant-scoped
target list and dispatches against nothing).
"""

from __future__ import annotations

import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.routes.connectors.operator import _lift_operator
from meho_backplane.ui.routes.keycloak.routes import (
    KEYCLOAK_CONNECTOR_ID,
    _list_keycloak_targets,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_keycloak_write_router"]

log = structlog.get_logger(__name__)

#: Maximum target-slug length accepted on the query/form boundary. Mirrors
#: the read scaffold's bound -- a target slug is a short name.
_MAX_TARGET_LENGTH: Final[int] = 256

#: Maximum client-UUID length accepted on the protocol-mapper path. A
#: Keycloak internal id is a UUID; the bound rejects an oversized paste.
_MAX_CLIENT_UUID_LENGTH: Final[int] = 256

#: Maximum length of a free-form JSON representation body. The
#: representations authored here (ClientScopeRepresentation /
#: ProtocolMapperRepresentation) are small objects; the bound matches the
#: operations Run modal's params field so an oversized paste is a form-
#: boundary 422 before it reaches the dispatch.
_MAX_REPRESENTATION_LENGTH: Final[int] = 65536

#: Short fields collected directly on the create forms (name / protocol /
#: mapper type). Bounded so an oversized value is rejected at the form edge.
_MAX_FIELD_LENGTH: Final[int] = 512

#: Module-level ``Depends`` / ``Path`` closures -- built once (rather than
#: inline) to satisfy ruff B008, matching the read scaffold + operations router.
_require_session = Depends(require_ui_session)
_client_uuid_path = Path(max_length=_MAX_CLIENT_UUID_LENGTH)


async def _resolve_keycloak_admin_or_403(
    request: Request,
    session_ctx: UISessionContext | None = None,
) -> Operator:
    """Lift the operator + gate to ``tenant_admin`` for a keycloak write.

    A keycloak-local twin of
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
    -- same lift + 403 raise, but with a keycloak-appropriate ``detail``
    string (the connectors one is the surface-specific
    ``"re-probe requires tenant_admin"``). A non-admin POST to either write
    route raises 403 server-side even when the affordance was soft-hidden;
    the dispatch itself never runs.
    """
    if session_ctx is None:
        session_ctx = await require_ui_session(request)
    operator = await _lift_operator(session_ctx)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="keycloak authoring requires tenant_admin",
        )
    return operator


_require_keycloak_admin = Depends(_resolve_keycloak_admin_or_403)


def _resolve_active_target(targets: list[dict[str, Any]], target: str) -> str | None:
    """Return the in-list active target, or ``None`` for a cross-tenant slug.

    The no-tenant-override posture: an explicit selection wins only when it
    is in the operator's tenant-scoped list; a sole target defaults; a
    cross-tenant slug is dropped (treated as no selection) so it can never
    drive a dispatch.
    """
    target = target.strip()
    names = {str(t["name"]) for t in targets}
    if target and target in names:
        return target
    if not target and len(targets) == 1:
        return str(targets[0]["name"])
    return None


def _parse_representation(raw: str, *, base: dict[str, Any]) -> dict[str, Any]:
    """Parse the optional free-form JSON ``representation`` and merge *base*.

    The forms collect the common fields directly (name / protocol / mapper
    type) and accept an optional advanced JSON block for everything else
    (attributes, embedded ``protocolMappers``, mapper ``config``). The
    explicit fields win over the JSON so a typo in the advanced block cannot
    silently drop the named field. A malformed JSON block raises
    :class:`ValueError` (surfaced inline as a 400 form error, not a 422).
    """
    representation: dict[str, Any] = {}
    raw = raw.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Advanced JSON is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Advanced JSON must be a JSON object.")
        representation.update(parsed)
    # The explicit fields win over the advanced block (drop empty values so
    # a blank optional field does not clobber a value set in the JSON).
    representation.update({k: v for k, v in base.items() if v not in ("", None)})
    return representation


# A router factory registering four thin BFF handlers (two confirm-modal GETs
# + two write POSTs), each delegating straight to a module-level ``_render_*``
# helper. The length is decorator + multi-line FastAPI ``Form`` signature
# boilerplate, not per-function logic; splitting the factory would break the
# single-router registration idiom the read scaffold + operations router both
# follow for no readability gain.
# code-quality-allow: function-size -- router factory; length is registration boilerplate
def build_keycloak_write_router() -> APIRouter:
    """Construct the keycloak authoring (write) :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without shared route state -- the convention every
    surface router follows. Registered by
    :func:`~meho_backplane.ui.routes.keycloak.routes.build_keycloak_router`.

    Route ordering: the literal ``/ui/keycloak/client-scopes/create`` and the
    literal ``protocol-mappers/create`` tail under the
    ``/ui/keycloak/clients/{client_uuid}`` prefix register here. They are
    POST routes (the modal-render GETs are literal too), so they never
    collide with the read scaffold's ``GET /ui/keycloak/clients/{client_uuid}``
    ``{param}`` route -- but the literal-before-{param} discipline holds: the
    only ``{param}`` segment any keycloak route carries is ``client_uuid``
    under the distinct ``/ui/keycloak/clients/`` prefix.
    """
    router = APIRouter(tags=["ui-keycloak"])

    @router.get("/ui/keycloak/client-scopes/create", response_class=HTMLResponse)
    async def keycloak_client_scope_create_modal(
        request: Request,
        session: UISessionContext = _require_session,
        operator: Operator = _require_keycloak_admin,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
    ) -> HTMLResponse:
        """Render the create-client-scope confirm modal (tenant_admin only).

        A non-admin GET 403s here too -- the modal is the write surface's
        entry point, so gating it keeps a soft-hidden affordance from being
        reached by URL. The modal mints + re-sets the CSRF cookie so the
        confirm POST's ``hx-headers`` echo lines up after the swap.
        """
        return await _render_scope_create_modal(request, session, target)

    @router.post("/ui/keycloak/client-scopes/create", response_class=HTMLResponse)
    async def keycloak_client_scope_create(
        request: Request,
        operator: Operator = _require_keycloak_admin,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        name: str = Form(max_length=_MAX_FIELD_LENGTH),
        protocol: str = Form(default="openid-connect", max_length=_MAX_FIELD_LENGTH),
        description: str = Form(default="", max_length=_MAX_FIELD_LENGTH),
        representation: str = Form(default="", max_length=_MAX_REPRESENTATION_LENGTH),
    ) -> HTMLResponse:
        """Dispatch ``keycloak.client_scope.create`` (awaiting_approval).

        CSRF-gated (a ``POST`` under ``/ui/``) and tenant_admin-gated. Builds
        ``{"representation": <ClientScopeRepresentation>}`` from the named
        fields + the optional advanced JSON block, pins ``connector_id``, and
        renders the structured envelope inline -- the ``awaiting_approval``
        deep-link is the success path because the op requires approval.
        """
        return await _render_scope_create_result(
            request,
            operator,
            target=target,
            name=name,
            protocol=protocol,
            description=description,
            representation=representation,
        )

    @router.get(
        "/ui/keycloak/clients/{client_uuid}/protocol-mappers/create",
        response_class=HTMLResponse,
    )
    async def keycloak_protocol_mapper_create_modal(
        request: Request,
        client_uuid: str = _client_uuid_path,
        session: UISessionContext = _require_session,
        operator: Operator = _require_keycloak_admin,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
    ) -> HTMLResponse:
        """Render the create-protocol-mapper confirm modal (tenant_admin only).

        The target client is keyed off the route's ``{client_uuid}`` (the
        client's internal UUID), not a free-form field -- so the dispatch
        cannot be re-pointed at another client by a forged form value. A
        non-admin GET 403s here too.
        """
        return await _render_mapper_create_modal(request, session, client_uuid, target)

    @router.post(
        "/ui/keycloak/clients/{client_uuid}/protocol-mappers/create",
        response_class=HTMLResponse,
    )
    async def keycloak_protocol_mapper_create(
        request: Request,
        client_uuid: str = _client_uuid_path,
        operator: Operator = _require_keycloak_admin,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        name: str = Form(max_length=_MAX_FIELD_LENGTH),
        protocol: str = Form(default="openid-connect", max_length=_MAX_FIELD_LENGTH),
        protocol_mapper: str = Form(max_length=_MAX_FIELD_LENGTH),
        representation: str = Form(default="", max_length=_MAX_REPRESENTATION_LENGTH),
    ) -> HTMLResponse:
        """Dispatch ``keycloak.protocol_mapper.create`` (awaiting_approval).

        The target client is keyed on the route's ``{client_uuid}`` -- the
        dispatch params carry ``{"representation": {...}, "id": client_uuid}``
        so the client is fixed by the path, not a free-form field. CSRF- and
        tenant_admin-gated; renders the structured envelope inline (the
        ``awaiting_approval`` deep-link is the success path).
        """
        return await _render_mapper_create_result(
            request,
            operator,
            client_uuid=client_uuid,
            target=target,
            name=name,
            protocol=protocol,
            protocol_mapper=protocol_mapper,
            representation=representation,
        )

    return router


async def _render_scope_create_modal(
    request: Request, session: UISessionContext, target: str
) -> HTMLResponse:
    """Render the create-client-scope confirm modal fragment."""
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "target": target.strip(),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request, "keycloak/_scope_create_modal.html", context
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_mapper_create_modal(
    request: Request, session: UISessionContext, client_uuid: str, target: str
) -> HTMLResponse:
    """Render the create-protocol-mapper confirm modal fragment."""
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "client_uuid": client_uuid,
        "target": target.strip(),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request, "keycloak/_mapper_create_modal.html", context
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_scope_create_result(
    request: Request,
    operator: Operator,
    *,
    target: str,
    name: str,
    protocol: str,
    description: str,
    representation: str,
) -> HTMLResponse:
    """Dispatch ``keycloak.client_scope.create`` and render the result."""
    active_target = _resolve_active_target(await _list_keycloak_targets(operator), target)
    if active_target is None:
        return _render_write_form_error(request, "Select a Keycloak target first.")

    try:
        scope_repr = _parse_representation(
            representation,
            base={"name": name.strip(), "protocol": protocol.strip(), "description": description},
        )
    except ValueError as exc:
        return _render_write_form_error(request, str(exc))

    envelope = await _dispatch_write(
        operator,
        op_id="keycloak.client_scope.create",
        target=active_target,
        params={"representation": scope_repr},
    )
    return _render_write_result(request, envelope)


async def _render_mapper_create_result(
    request: Request,
    operator: Operator,
    *,
    client_uuid: str,
    target: str,
    name: str,
    protocol: str,
    protocol_mapper: str,
    representation: str,
) -> HTMLResponse:
    """Dispatch ``keycloak.protocol_mapper.create`` and render the result.

    The target client is keyed on ``client_uuid`` from the route path, NOT a
    free-form field -- the dispatch params carry ``id=client_uuid`` so the
    client cannot be re-pointed by a forged form value.
    """
    active_target = _resolve_active_target(await _list_keycloak_targets(operator), target)
    if active_target is None:
        return _render_write_form_error(request, "Select a Keycloak target first.")

    try:
        mapper_repr = _parse_representation(
            representation,
            base={
                "name": name.strip(),
                "protocol": protocol.strip(),
                "protocolMapper": protocol_mapper.strip(),
            },
        )
    except ValueError as exc:
        return _render_write_form_error(request, str(exc))

    envelope = await _dispatch_write(
        operator,
        op_id="keycloak.protocol_mapper.create",
        target=active_target,
        # The client is keyed off the route path; ``id`` is the client's
        # internal UUID (the read scaffold's client-detail UUID).
        params={"representation": mapper_repr, "id": client_uuid},
    )
    return _render_write_result(request, envelope)


async def _dispatch_write(
    operator: Operator,
    *,
    op_id: str,
    target: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch one keycloak write op in-process against the pinned connector.

    ``connector_id`` is :data:`KEYCLOAK_CONNECTOR_ID` -- pinned, never the
    bare slug. The structured envelope is returned verbatim; the
    ``requires_approval`` policy gate routes the dispatch to
    ``status="awaiting_approval"`` with ``extras["approval_request_id"]``.
    """
    return await call_operation(
        operator,
        {
            "connector_id": KEYCLOAK_CONNECTOR_ID,
            "op_id": op_id,
            "target": target,
            "params": params,
        },
    )


def _render_write_result(request: Request, envelope: dict[str, Any]) -> HTMLResponse:
    """Render the write-result fragment for *envelope* (HTTP 200).

    The fragment branches on ``status`` -- ``awaiting_approval`` (the
    requires-approval success path; deep-links ``/ui/approvals``) / ``ok`` /
    ``error`` / ``denied`` -- and never dumps the raw envelope blob.
    """
    context: dict[str, Any] = {"envelope": envelope, "error_message": None}
    return get_templates().TemplateResponse(request, "keycloak/_write_result.html", context)


def _render_write_form_error(request: Request, message: str) -> HTMLResponse:
    """Render the write-result fragment with an inline form error (HTTP 400).

    The op was never dispatched (bad target name / malformed advanced JSON);
    the operator stays in the modal.
    """
    context: dict[str, Any] = {"envelope": None, "error_message": message}
    return get_templates().TemplateResponse(
        request,
        "keycloak/_write_result.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )
