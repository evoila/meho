# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak console UI routes: realm config + client list/detail + scope list.

Initiative #1943 (G10.x Keycloak console), Task #1959 (T1, read-only
scaffold). MEHO already administers a Keycloak realm end-to-end from the
CLI (``meho keycloak realm/client/client-scope ...``), but the operator
console has had no realm-administration surface -- only the narrow
agent-principals kill switch at ``/ui/agents/principals`` (#1831/#1866).
This package adds the read-only realm browser the two write tasks (T2 user
management, T3 scope/mapper management) build on.

Why a session BFF and not a REST surface
----------------------------------------

There is **no** ``/api/v1/keycloak`` REST surface: every CLI keycloak verb
is a thin Cobra layer over ``POST /api/v1/operations/call`` against the
pre-baked ``connector_id="keycloak-admin-26.x"`` (the CLI's
``ConnectorID`` constant, ``cli/internal/cmd/keycloak/keycloak.go``).
Keycloak admin flows ride the operations dispatcher. So this console is
pure UI/BFF assembly: it dispatches the curated ``keycloak.*`` **read**
ops in-process through
:func:`~meho_backplane.operations.meta_tools.call_operation` -- the exact
in-process pattern the MERGED ``/ui/operations`` console uses
(:mod:`meho_backplane.ui.routes.operations.routes`) -- and renders a
domain-shaped UX (realm-config card, client list/detail, client-scope
list) instead of the generic op drawer. A browser carrying only the BFF
session cookie cannot authenticate the Bearer-gated REST routes, so the
in-process call is the only path.

connector_id is pinned (load-bearing)
-------------------------------------

The dispatched ``connector_id`` is the module constant
:data:`KEYCLOAK_CONNECTOR_ID` (``"keycloak-admin-26.x"``) -- never typed
by the operator, never re-derived from the bare product slug
``keycloak``. The bare slug parses to ``(product="keycloak", version="",
impl_id="")`` (:func:`~meho_backplane.operations._lookup.parse_connector_id`)
which names no registered connector and dead-ends the dispatch. The slug
the operator *does* pick is the **target** slug (which Keycloak deployment
to read), carried on the query string; the connector is fixed.

Read ops dispatched (all ``safety_level="safe"`` / no approval)
---------------------------------------------------------------

* ``keycloak.realm.get`` -- the managed realm's top-level config.
* ``keycloak.client.list`` -- the realm's clients (``{rows, total}``).
* ``keycloak.client.get`` -- one client by **internal UUID** (the ``id``
  from a list row, NOT the human ``clientId``).
* ``keycloak.client_scope.list`` -- the realm's client scopes.

(``keycloak.user.list`` / ``keycloak.role_mapping.get`` exist too but are
T2's surface; this scaffold does not touch them.)

Secret redaction is upstream, not here (load-bearing)
-----------------------------------------------------

Every keycloak read op scrubs nested secrets at the connector boundary
(:func:`~meho_backplane.connectors.keycloak.redaction.redact_secret_fields`)
before the result leaves the connector, substituting the ``REDACTED``
sentinel. The UI **trusts** this and never re-exposes the raw envelope:
the templates render projected, named fields (clientId, enabled, redirect
URIs, protocol mappers, ...) -- they never dump the verbatim
``OperationResult`` blob into the page, which would re-surface whatever a
future op forgot to scrub. The realm/client/scope dicts carried inside the
envelope's ``result`` are already secret-free by the connector's
guarantee.

RBAC: reads are operator-tier
-----------------------------

The dispatch is gated only by :func:`require_ui_session`; the underlying
``POST /api/v1/operations/call`` is ``_require_operator``
(``TenantRole.OPERATOR``), so a plain operator can read every surface
here. The page still threads an ``is_tenant_admin`` flag (via the
soft-failing :func:`resolve_role_probe`) so T2/T3 can hide their write
affordances from operators -- but a non-admin render must succeed, which
the RBAC test asserts.

Tenant isolation
----------------

The target list and the dispatch both derive ``tenant_id`` from the
validated session only (via the lifted :class:`Operator`). There is **no**
tenant-override query/form param: a cross-tenant target slug is simply
absent from the operator's target list and dispatches against nothing --
the no-tenant-override posture the kb router documents (cross-tenant slug
-> 404, never a cross-tenant escape). A cross-tenant target/realm selector
is deferred console-wide (#865).

Route ordering
--------------

The literal ``/ui/keycloak/clients/{client_uuid}`` route registers BEFORE
the bare ``/ui/keycloak`` index; the only ``{param}`` route sits under the
distinct ``/ui/keycloak/clients/`` prefix, so a future literal segment
(``/ui/keycloak/users`` in T2) registered ahead of any ``{param}`` route
binds first (first-match-wins -- the discipline the operations router
documents). The router is registered ahead of the stubs aggregate in
:func:`meho_backplane.ui.routes.build_router`.
"""

from __future__ import annotations

import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["KEYCLOAK_CONNECTOR_ID", "KEYCLOAK_PRODUCT", "build_keycloak_router"]

log = structlog.get_logger(__name__)

#: The pinned dispatchable connector id for every keycloak read op. Mirrors
#: the CLI's ``ConnectorID`` constant (``cli/internal/cmd/keycloak/keycloak.go``).
#: The bare product slug ``keycloak`` names no connector
#: (:func:`~meho_backplane.operations._lookup.parse_connector_id`); only
#: this ``<impl_id>-<version>`` form resolves through the dispatcher. Never
#: typed by the operator, never re-derived from the slug.
KEYCLOAK_CONNECTOR_ID: Final[str] = "keycloak-admin-26.x"

#: The ``Target.product`` value the keycloak deployments register under.
#: Used to scope the target picker to keycloak targets the operator's
#: tenant owns.
KEYCLOAK_PRODUCT: Final[str] = "keycloak"

#: Maximum target-slug length accepted on the query string. A target slug
#: is a short name (``rdc-keycloak``); the bound rejects an oversized paste
#: at the form boundary (422) before it reaches the dispatch.
_MAX_TARGET_LENGTH: Final[int] = 256

#: Maximum length accepted for a Vault KV path / username / role-name form
#: field. A KV path is a short slug-ish string; the bound rejects an
#: oversized paste at the form boundary (422) before it reaches the dispatch.
_MAX_FORM_FIELD_LENGTH: Final[int] = 512

#: Maximum length accepted for the create-user ``UserRepresentation`` JSON
#: textarea. A UserRepresentation is small (username + a few attributes);
#: the bound matches the operations Run modal's params textarea.
_MAX_REPRESENTATION_LENGTH: Final[int] = 65536


async def _resolve_admin_or_403(
    request: Request,
    session: UISessionContext = Depends(require_ui_session),
) -> Operator:
    """FastAPI dependency: lift the operator and hard-403 a non-tenant_admin.

    The server-side authority for the three high-blast keycloak writes
    (create / reset-password / role-assign). Mirrors
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
    but raises a keycloak-appropriate 403 ``detail`` (the connectors
    dependency's string is ``"re-probe requires tenant_admin"`` -- wrong
    surface) so a forged POST from a plain operator gets a legible
    ``403`` the template's ``hx-on::after-request`` alert can branch on,
    not a silent dispatch.

    The lift re-verifies the session's access token through the JWT chain
    (catching a same-session role demotion) -- the same round-trip the read
    probe and the operations console use. The soft-failing
    :func:`resolve_role_probe` drives the template's button-hide; THIS gate
    is what actually stops the write.
    """
    operator = await _resolve_operator(session)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="keycloak user management requires tenant_admin",
        )
    return operator


#: Module-level ``Depends`` closures -- built once (rather than inline) to
#: satisfy ruff B008, matching the operations / approvals / kb routers.
_require_session = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)
_require_admin = Depends(_resolve_admin_or_403)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    The dispatch needs a real :class:`Operator` (the meta-tool reads
    ``operator.tenant_id`` for the tenant-scoped resolve, and the keycloak
    read ops authorise the operator-context Vault read that backs the
    admin-token mint). Mirrors the operations console's lift: load the
    decrypted session row and re-verify its access token through the
    chassis JWT chain via
    :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`,
    which silently refreshes once on the ``token_expired`` 401 before
    re-verifying -- so an expired-but-refreshable token serves the surface
    instead of 401-ing mid-session, while a same-session role demotion is
    still caught on the next browse rather than cached for the session TTL.

    Raises :class:`fastapi.HTTPException` 401 when the session was revoked
    / expired between the middleware check and here, or when the refresh
    attempt itself fails (``session_expired`` -- the BFF error handler maps
    it to a login redirect for HTML requests).
    """
    decrypted = await load_fresh_session(session.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    settings = get_settings()
    _refreshed, operator = await verify_access_token_with_refresh(
        decrypted,
        expected_audience=settings.keycloak_audience,
    )
    return operator


async def _list_keycloak_targets(operator: Operator) -> list[dict[str, Any]]:
    """Project the tenant's keycloak targets into the picker's option shape.

    Tenant-scoped to ``operator.tenant_id`` only -- no query/header/path
    tenant override, so a cross-tenant keycloak target never enters the
    rendered set. Filtered to ``product == "keycloak"`` so the picker lists
    only deployments the keycloak read ops can dispatch against.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        result = await db_session.execute(
            select(TargetORM.name)
            .where(
                TargetORM.tenant_id == operator.tenant_id,
                TargetORM.product == KEYCLOAK_PRODUCT,
            )
            .order_by(TargetORM.name)
        )
        return [{"name": name} for name in result.scalars().all()]


def _dispatch_failed(envelope: dict[str, Any]) -> str | None:
    """Return a human error string when *envelope* is not a clean ``ok``.

    ``call_operation`` always returns a structured envelope -- a denied /
    errored / unavailable dispatch lands as ``status != "ok"`` with the
    cause in ``error``, never as an exception. The index surfaces that
    inline so a bad target or an unreachable Keycloak is a legible banner,
    not an empty card.
    """
    if envelope.get("status") == "ok":
        return None
    return envelope.get("error") or envelope.get("status") or "dispatch_failed"


async def _dispatch_read(
    operator: Operator,
    *,
    op_id: str,
    target: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch one keycloak read op in-process against the pinned connector.

    The ``connector_id`` is :data:`KEYCLOAK_CONNECTOR_ID` -- pinned, never
    the bare slug. The structured envelope is returned verbatim; callers
    read ``status`` / ``result`` / ``error`` off it.
    """
    return await call_operation(
        operator,
        {
            "connector_id": KEYCLOAK_CONNECTOR_ID,
            "op_id": op_id,
            "target": target,
            "params": params or {},
        },
    )


async def _dispatch_write(
    operator: Operator,
    *,
    op_id: str,
    target: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch one keycloak WRITE op in-process against the pinned connector.

    Identical wiring to :func:`_dispatch_read` -- the dispatcher's contract
    is "always return a structured envelope": ``ok`` / ``error`` /
    ``denied`` / ``awaiting_approval`` all come back INSIDE the envelope,
    never as an exception. Every keycloak write op is
    ``requires_approval=True``, so the policy gate routes a confirmed write
    to ``status="awaiting_approval"`` with ``extras["approval_request_id"]``
    rather than executing immediately; the result template surfaces that
    id with a deep-link into ``/ui/approvals``.
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


def _parse_representation(raw: str) -> dict[str, Any]:
    """Parse the create-user form's ``UserRepresentation`` JSON textarea.

    A blank field is rejected (a create needs at least a ``username``). A
    non-blank field must be a JSON **object**; a malformed body or a
    non-object value raises :class:`ValueError` with an operator-legible
    message the caller renders inline -- the op's ``representation`` param
    is a dict (the Keycloak ``UserRepresentation``).
    """
    text = raw.strip()
    if not text:
        raise ValueError("Provide a UserRepresentation JSON object (at least a username).")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"representation is not valid JSON: {exc.msg} (line {exc.lineno})"
        raise ValueError(msg) from exc
    if not isinstance(value, dict):
        raise ValueError('representation must be a JSON object (e.g. {"username": "operator-a"}).')
    return value


def _password_secret_params(
    *,
    secret_ref: str,
    secret_mount: str,
    secret_key: str,
    temporary: bool,
) -> dict[str, Any]:
    """Build the Vault-ref password params from the form fields.

    The password VALUE is never collected -- only its Vault KV location
    (``password_secret_ref`` + the optional mount / key / temporary
    overrides), mirroring the CLI's ``passwordSecretFlags`` bundle. Only
    non-empty overrides are written so the connector's defaults (mount
    ``secret``, key ``password``) apply. The password therefore never lands
    in form params, request logs, or the audit row -- the backend reads it
    from Vault at dispatch time.
    """
    params: dict[str, Any] = {}
    ref = secret_ref.strip()
    if ref:
        params["password_secret_ref"] = ref
    mount = secret_mount.strip()
    if mount:
        params["password_secret_mount"] = mount
    key = secret_key.strip()
    if key:
        params["password_secret_key"] = key
    if temporary:
        params["temporary"] = True
    return params


def build_keycloak_router() -> APIRouter:
    """Construct the ``/ui/keycloak*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the convention
    every surface router (operations / approvals / kb / connectors)
    follows. Registered ahead of the stubs aggregate in
    :func:`meho_backplane.ui.routes.build_router`.
    """
    # Imported inside the factory (not at module top) to avoid the import
    # cycle: ``write`` imports the pinned connector id + the target-list
    # helper from this module, so a top-level import here would re-enter this
    # module mid-import.
    from meho_backplane.ui.routes.keycloak.write import build_keycloak_write_router

    router = APIRouter(tags=["ui-keycloak"])

    # ROUTE ORDERING (first-match-wins). The literal user-management routes
    # are registered FIRST, with the literal segments (``users``,
    # ``users/create``) ahead of any ``{user_uuid}`` route, then the literal
    # ``clients/{client_uuid}`` route, then the bare ``/ui/keycloak`` index.
    # The only ``{param}`` routes sit under the distinct ``users/`` and
    # ``clients/`` prefixes; a literal ``users/create`` registered before
    # ``users/{user_uuid}/...`` binds first so ``create`` is never captured
    # as a UUID -- the ordering discipline the operations / approvals routers
    # document.
    _register_user_routes(router)

    @router.get("/ui/keycloak/clients/{client_uuid}", response_class=HTMLResponse)
    async def keycloak_client_detail(
        request: Request,
        client_uuid: str,
        session: UISessionContext = _require_session,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Render the client-detail fragment for *client_uuid* (read-only).

        ``client_uuid`` is the client's **internal UUID** (the ``id`` from a
        ``keycloak.client.list`` row, NOT the human ``clientId``). Dispatches
        ``keycloak.client.get`` and renders projected, redaction-trusting
        fields only -- the client secret is already scrubbed by the
        connector; the raw envelope is NEVER dumped into the page.
        """
        return await _render_client_detail(request, session, client_uuid, target, role_probe)

    @router.get("/ui/keycloak", response_class=HTMLResponse)
    async def keycloak_index(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Render the full-page realm browser.

        Lists the tenant's keycloak targets in a picker; when a target is
        selected (or there is exactly one), dispatches ``keycloak.realm.get``
        + ``keycloak.client.list`` + ``keycloak.client_scope.list`` and
        renders the realm-config card + client list + client-scope list. All
        dispatches pin ``connector_id`` to :data:`KEYCLOAK_CONNECTOR_ID`.
        """
        return await _render_index(request, session, target, role_probe)

    # The client-scope + protocol-mapper authoring writes (T3, #1961). Kept
    # in a sibling module so the read scaffold stays focused; the write routes
    # are tenant_admin-gated + confirm-gated + approval-handoff (see
    # :mod:`meho_backplane.ui.routes.keycloak.write`). Included here -- not
    # registered separately in ``build_router`` -- so the whole keycloak
    # surface ships as one router with consistent route ordering.
    router.include_router(build_keycloak_write_router())

    return router


def _register_user_routes(router: APIRouter) -> None:
    """Register the user-management routes on *router* (Task #1960).

    Split out of :func:`build_keycloak_router` to keep each factory small.
    The literal ``users`` / ``users/create`` segments are registered ahead of
    any ``{user_uuid}`` route so ``create`` is never captured as a UUID
    (first-match-wins). The list read is OPERATOR-tier; the three writes
    (create / reset-password / role-assign) hard-gate on tenant_admin via
    :data:`_require_admin` and render an unmissable confirm before POST.
    """

    # ---- User management: list (read, operator-tier) -------------------
    @router.get("/ui/keycloak/users", response_class=HTMLResponse)
    async def keycloak_user_list(
        request: Request,
        session: UISessionContext = _require_session,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
        username: str = Query(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Render the realm's user list (read; OPERATOR-tier).

        Dispatches ``keycloak.user.list`` (``safety_level="safe"``); an
        optional ``username`` filter maps to Keycloak's ``?username=``.
        Credential material is redacted from every row by the connector;
        this render projects named fields only and never dumps the raw
        envelope. The create / reset / assign affordances are soft-hidden
        from a plain operator (``is_tenant_admin`` from the soft-failing
        role probe); the write POSTs are hard-gated server-side.
        """
        return await _render_user_list(request, session, target, username, role_probe)

    # ---- User management: create (write, tenant_admin) -----------------
    @router.get("/ui/keycloak/users/create", response_class=HTMLResponse)
    async def keycloak_user_create_modal(
        request: Request,
        session: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
    ) -> HTMLResponse:
        """Render the create-user confirm modal (tenant_admin only).

        Collects a ``UserRepresentation`` JSON body + a Vault KV path for
        the password (NO plaintext password field). Mints + re-sets the
        ``meho_csrf`` cookie so the confirm button's own ``hx-headers`` echo
        lines up after the HTMX swap rotated it.
        """
        return _render_user_create_modal(request, session, target)

    @router.post("/ui/keycloak/users/create", response_class=HTMLResponse)
    async def keycloak_user_create(
        request: Request,
        session: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        representation: str = Form(default="", max_length=_MAX_REPRESENTATION_LENGTH),
        password_secret_ref: str = Form(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        password_secret_mount: str = Form(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        password_secret_key: str = Form(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        temporary: bool = Form(default=False),
    ) -> HTMLResponse:
        """Dispatch ``keycloak.user.create``; render the write result inline.

        CSRF-gated (a ``POST`` under ``/ui/``) and tenant_admin-gated. The
        password is supplied as a Vault KV path only -- it never lands in
        form params, request logs, or the audit row. The op is
        ``requires_approval=True``, so a clean dispatch returns
        ``status="awaiting_approval"`` with the approval-request deep-link.
        """
        return await _render_user_create_result(
            request,
            operator,
            target=target,
            representation=representation,
            password_secret_ref=password_secret_ref,
            password_secret_mount=password_secret_mount,
            password_secret_key=password_secret_key,
            temporary=temporary,
        )

    _register_user_password_role_routes(router)


def _register_user_password_role_routes(router: APIRouter) -> None:
    """Register the reset-password + role-assign write routes (Task #1960).

    Split from :func:`_register_user_routes` to keep each registration
    function small. These are the two ``{user_uuid}``-parametrised writes;
    they must be registered AFTER the literal ``users`` / ``users/create``
    routes (the caller orders the two registration calls), so ``create`` is
    never captured as a UUID. Both hard-gate on tenant_admin
    (:data:`_require_admin`) and are CSRF-gated.
    """

    # ---- User management: reset-password (write, tenant_admin) ---------
    @router.get("/ui/keycloak/users/{user_uuid}/reset-password", response_class=HTMLResponse)
    async def keycloak_user_reset_password_modal(
        request: Request,
        user_uuid: str,
        session: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
    ) -> HTMLResponse:
        """Render the reset-password confirm modal (tenant_admin only).

        Keyed on the user's internal UUID. Collects only a Vault KV path
        (NO plaintext password field). Mints + re-sets the ``meho_csrf``
        cookie for the confirm button's own ``hx-headers`` echo.
        """
        return _render_reset_password_modal(request, session, user_uuid, target)

    @router.post("/ui/keycloak/users/{user_uuid}/reset-password", response_class=HTMLResponse)
    async def keycloak_user_reset_password(
        request: Request,
        user_uuid: str,
        session: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        password_secret_ref: str = Form(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        password_secret_mount: str = Form(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        password_secret_key: str = Form(default="", max_length=_MAX_FORM_FIELD_LENGTH),
        temporary: bool = Form(default=False),
    ) -> HTMLResponse:
        """Dispatch ``keycloak.user.reset_password``; render the result inline.

        CSRF-gated and tenant_admin-gated. Keyed on the user UUID. The new
        password is supplied as a Vault KV path only -- never inline. The op
        is ``requires_approval=True`` (``awaiting_approval`` deep-link).
        """
        return await _render_reset_password_result(
            request,
            operator,
            user_uuid=user_uuid,
            target=target,
            password_secret_ref=password_secret_ref,
            password_secret_mount=password_secret_mount,
            password_secret_key=password_secret_key,
            temporary=temporary,
        )

    # ---- User management: role-assign (write, tenant_admin) ------------
    @router.get("/ui/keycloak/users/{user_uuid}/roles/assign", response_class=HTMLResponse)
    async def keycloak_role_assign_modal(
        request: Request,
        user_uuid: str,
        session: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        target: str = Query(default="", max_length=_MAX_TARGET_LENGTH),
    ) -> HTMLResponse:
        """Render the role-assign confirm modal (tenant_admin only).

        A PRIVILEGE GRANT (``safety_level="dangerous"``). Reads the user's
        current realm/client role mappings (``keycloak.role_mapping.get``)
        for context, then collects realm role names to grant. The confirm
        banner names it a privilege grant and surfaces ``dangerous``.
        """
        return await _render_role_assign_modal(request, session, operator, user_uuid, target)

    @router.post("/ui/keycloak/users/{user_uuid}/roles/assign", response_class=HTMLResponse)
    async def keycloak_role_assign(
        request: Request,
        user_uuid: str,
        session: UISessionContext = _require_session,
        operator: Operator = _require_admin,
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        roles: list[str] = Form(default_factory=list),
    ) -> HTMLResponse:
        """Dispatch ``keycloak.role_mapping.assign``; render the result inline.

        CSRF-gated and tenant_admin-gated. A privilege grant. ``roles`` is a
        list of realm role names (string list); the dispatched params are
        ``{"roles": [...], "id": <uuid>}``. The op is
        ``requires_approval=True`` (``awaiting_approval`` deep-link).
        """
        return await _render_role_assign_result(
            request,
            operator,
            user_uuid=user_uuid,
            target=target,
            roles=roles,
        )


async def _render_index(
    request: Request,
    session: UISessionContext,
    target: str,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Assemble + render the realm-browser page context."""
    operator = await _resolve_operator(session)
    targets = await _list_keycloak_targets(operator)

    # Resolve the active target: an explicit (in-list) selection wins;
    # otherwise default to the sole target when exactly one exists. A
    # selection that is NOT in the operator's tenant-scoped list is dropped
    # (treated as no selection) -- the no-tenant-override posture: a
    # cross-tenant slug can never drive a dispatch.
    target = target.strip()
    target_names = {t["name"] for t in targets}
    active_target: str | None = None
    if target and target in target_names:
        active_target = target
    elif not target and len(targets) == 1:
        active_target = targets[0]["name"]

    context: dict[str, Any] = {
        "active_surface": "keycloak",
        "page_title": "Keycloak",
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "targets": targets,
        "active_target": active_target,
        "is_tenant_admin": role_probe.is_tenant_admin,
        "realm": None,
        "clients": [],
        "client_scopes": [],
        "error": None,
    }

    if active_target is not None:
        realm_env = await _dispatch_read(operator, op_id="keycloak.realm.get", target=active_target)
        clients_env = await _dispatch_read(
            operator, op_id="keycloak.client.list", target=active_target
        )
        scopes_env = await _dispatch_read(
            operator, op_id="keycloak.client_scope.list", target=active_target
        )
        # Surface the first dispatch fault inline rather than rendering
        # empty cards over a denied / unreachable Keycloak.
        context["error"] = (
            _dispatch_failed(realm_env)
            or _dispatch_failed(clients_env)
            or _dispatch_failed(scopes_env)
        )
        # The connector guarantees secrets are scrubbed inside ``result``;
        # we read the named projections, never the raw envelope.
        context["realm"] = (realm_env.get("result") or {}).get("realm")
        context["clients"] = (clients_env.get("result") or {}).get("rows", [])
        context["client_scopes"] = (scopes_env.get("result") or {}).get("rows", [])

    return get_templates().TemplateResponse(request, "keycloak/index.html", context)


async def _render_client_detail(
    request: Request,
    session: UISessionContext,
    client_uuid: str,
    target: str,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Dispatch ``keycloak.client.get`` and render the projected detail.

    A blank / cross-tenant ``target`` resolves to nothing the operator can
    dispatch against, so the fragment renders its inline error rather than
    leaking a cross-tenant read. The client secret is already redacted by
    the connector; this render projects named fields only and never dumps
    the raw envelope.
    """
    operator = await _resolve_operator(session)

    target = target.strip()
    targets = await _list_keycloak_targets(operator)
    target_names = {t["name"] for t in targets}
    if not target or target not in target_names:
        return _render_client_detail_error(
            request, client_uuid, target, "Select a Keycloak target first."
        )

    envelope = await _dispatch_read(
        operator,
        op_id="keycloak.client.get",
        target=target,
        params={"id": client_uuid},
    )
    error = _dispatch_failed(envelope)
    client = (envelope.get("result") or {}).get("client") if error is None else None

    context: dict[str, Any] = {
        "client_uuid": client_uuid,
        "target": target,
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "client": client,
        "error": error,
        "is_tenant_admin": role_probe.is_tenant_admin,
    }
    return get_templates().TemplateResponse(request, "keycloak/_client_detail.html", context)


def _render_client_detail_error(
    request: Request,
    client_uuid: str,
    target: str,
    message: str,
) -> HTMLResponse:
    """Render the client-detail fragment with an inline error (HTTP 400)."""
    context: dict[str, Any] = {
        "client_uuid": client_uuid,
        "target": target,
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "client": None,
        "error": message,
        "is_tenant_admin": False,
    }
    return get_templates().TemplateResponse(
        request,
        "keycloak/_client_detail.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


async def _resolve_active_target(operator: Operator, target: str) -> str | None:
    """Resolve a query/form ``target`` against the operator's keycloak targets.

    Returns the target name only when it is in the operator's tenant-scoped
    keycloak target list, or the sole target when exactly one exists and no
    explicit selection was made. A cross-tenant / unknown slug resolves to
    ``None`` -- the no-tenant-override posture: a cross-tenant slug can never
    drive a dispatch.
    """
    target = target.strip()
    targets = await _list_keycloak_targets(operator)
    target_names = {t["name"] for t in targets}
    if target and target in target_names:
        return target
    if not target and len(targets) == 1:
        return str(targets[0]["name"])
    return None


# ---------------------------------------------------------------------------
# User list (read; operator-tier)
# ---------------------------------------------------------------------------


async def _render_user_list(
    request: Request,
    session: UISessionContext,
    target: str,
    username: str,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Assemble + render the user-list page (``keycloak.user.list``).

    OPERATOR-tier read. The write affordances (create / reset / assign) are
    threaded behind ``is_tenant_admin`` so a plain operator never sees them;
    the write POSTs are the server-side authority. Credential material is
    redacted from every row by the connector -- this render projects named
    fields only.
    """
    operator = await _resolve_operator(session)
    targets = await _list_keycloak_targets(operator)
    active_target = await _resolve_active_target(operator, target)
    username_filter = username.strip()

    context: dict[str, Any] = {
        "active_surface": "keycloak",
        "page_title": "Keycloak users",
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "targets": targets,
        "active_target": active_target,
        "username_filter": username_filter,
        "is_tenant_admin": role_probe.is_tenant_admin,
        "users": [],
        "error": None,
    }

    if active_target is not None:
        params: dict[str, Any] = {}
        if username_filter:
            params["username"] = username_filter
        users_env = await _dispatch_read(
            operator, op_id="keycloak.user.list", target=active_target, params=params
        )
        context["error"] = _dispatch_failed(users_env)
        # The connector redacts credential material inside ``result``; read
        # the named ``rows`` projection, never the raw envelope.
        context["users"] = (users_env.get("result") or {}).get("rows", [])

    return get_templates().TemplateResponse(request, "keycloak/users.html", context)


# ---------------------------------------------------------------------------
# Write modals + results (tenant_admin; CSRF-gated; approval handoff)
# ---------------------------------------------------------------------------


def _render_write_result(request: Request, envelope: dict[str, Any]) -> HTMLResponse:
    """Render a keycloak write op's structured envelope inline.

    The shared result fragment for all three writes. The dispatcher's
    contract is "always return a structured envelope" -- ``ok`` /
    ``awaiting_approval`` / ``error`` / ``denied`` all come back inside it
    (HTTP 200). Because every keycloak write is ``requires_approval=True``,
    the expected clean outcome is ``awaiting_approval``: the fragment
    surfaces ``extras["approval_request_id"]`` with a deep-link into
    ``/ui/approvals`` so the operator is never shown a silent / empty
    success.
    """
    return get_templates().TemplateResponse(
        request,
        "keycloak/_write_result.html",
        {"envelope": envelope, "error_message": None},
    )


def _render_write_form_error(request: Request, message: str) -> HTMLResponse:
    """Render the write-result fragment with an inline form error (HTTP 400).

    Covers a malformed ``representation`` JSON or a missing required field
    (no Vault ref / no roles). The op was never dispatched; the operator
    stays in the modal to correct the input.
    """
    return get_templates().TemplateResponse(
        request,
        "keycloak/_write_result.html",
        {"envelope": None, "error_message": message},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _render_user_create_modal(
    request: Request,
    session: UISessionContext,
    target: str,
) -> HTMLResponse:
    """Render the create-user confirm modal with a fresh CSRF token.

    Mints + re-sets the ``meho_csrf`` cookie so the confirm button's own
    ``hx-headers`` double-submit pair lines up after the HTMX swap rotated
    it (the cookie-desync defence the operations Run modal applies).
    """
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "target": target.strip(),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request, "keycloak/_user_create_modal.html", context
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_user_create_result(
    request: Request,
    operator: Operator,
    *,
    target: str,
    representation: str,
    password_secret_ref: str,
    password_secret_mount: str,
    password_secret_key: str,
    temporary: bool,
) -> HTMLResponse:
    """Validate + dispatch ``keycloak.user.create``; render the result."""
    active_target = await _resolve_active_target(operator, target)
    if active_target is None:
        return _render_write_form_error(request, "Select a Keycloak target first.")
    try:
        rep = _parse_representation(representation)
    except ValueError as exc:
        return _render_write_form_error(request, str(exc))

    params: dict[str, Any] = {"representation": rep}
    # Vault-ref password ONLY -- the value is never collected. ``create``
    # allows an SSO-only user with no password, so the ref is optional here.
    params.update(
        _password_secret_params(
            secret_ref=password_secret_ref,
            secret_mount=password_secret_mount,
            secret_key=password_secret_key,
            temporary=temporary,
        )
    )
    envelope = await _dispatch_write(
        operator, op_id="keycloak.user.create", target=active_target, params=params
    )
    return _render_write_result(request, envelope)


def _render_reset_password_modal(
    request: Request,
    session: UISessionContext,
    user_uuid: str,
    target: str,
) -> HTMLResponse:
    """Render the reset-password confirm modal with a fresh CSRF token."""
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "user_uuid": user_uuid,
        "target": target.strip(),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request, "keycloak/_reset_password_modal.html", context
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_reset_password_result(
    request: Request,
    operator: Operator,
    *,
    user_uuid: str,
    target: str,
    password_secret_ref: str,
    password_secret_mount: str,
    password_secret_key: str,
    temporary: bool,
) -> HTMLResponse:
    """Validate + dispatch ``keycloak.user.reset_password``; render the result."""
    active_target = await _resolve_active_target(operator, target)
    if active_target is None:
        return _render_write_form_error(request, "Select a Keycloak target first.")
    if not password_secret_ref.strip():
        return _render_write_form_error(
            request, "A Vault KV path (password secret ref) is required to reset a password."
        )

    # Keyed on the user UUID. Vault-ref password ONLY -- never inline.
    params: dict[str, Any] = {"id": user_uuid}
    params.update(
        _password_secret_params(
            secret_ref=password_secret_ref,
            secret_mount=password_secret_mount,
            secret_key=password_secret_key,
            temporary=temporary,
        )
    )
    envelope = await _dispatch_write(
        operator, op_id="keycloak.user.reset_password", target=active_target, params=params
    )
    return _render_write_result(request, envelope)


async def _render_role_assign_modal(
    request: Request,
    session: UISessionContext,
    operator: Operator,
    user_uuid: str,
    target: str,
) -> HTMLResponse:
    """Render the role-assign confirm modal (a privilege grant).

    Reads the user's CURRENT realm/client role mappings
    (``keycloak.role_mapping.get``) for context so the operator can see what
    the user already holds before granting more. Mints + re-sets the
    ``meho_csrf`` cookie for the confirm button's own ``hx-headers`` echo.
    The confirm banner names it a privilege grant + surfaces ``dangerous``.
    """
    active_target = await _resolve_active_target(operator, target)
    current_roles: list[str] = []
    error: str | None = None
    if active_target is None:
        error = "Select a Keycloak target first."
    else:
        env = await _dispatch_read(
            operator,
            op_id="keycloak.role_mapping.get",
            target=active_target,
            params={"id": user_uuid},
        )
        error = _dispatch_failed(env)
        if error is None:
            mappings = (env.get("result") or {}).get("role_mappings") or {}
            realm_mappings = mappings.get("realmMappings") or []
            current_roles = [
                str(r.get("name")) for r in realm_mappings if isinstance(r, dict) and r.get("name")
            ]

    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "connector_id": KEYCLOAK_CONNECTOR_ID,
        "user_uuid": user_uuid,
        "target": active_target or target.strip(),
        "current_roles": current_roles,
        "error": error,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request, "keycloak/_role_assign_modal.html", context
    )
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_role_assign_result(
    request: Request,
    operator: Operator,
    *,
    user_uuid: str,
    target: str,
    roles: list[str],
) -> HTMLResponse:
    """Validate + dispatch ``keycloak.role_mapping.assign``; render the result.

    A privilege grant. ``roles`` is a string list of realm role names; the
    dispatched params are ``{"roles": [...], "id": <uuid>}``.
    """
    active_target = await _resolve_active_target(operator, target)
    if active_target is None:
        return _render_write_form_error(request, "Select a Keycloak target first.")
    role_names = [r.strip() for r in roles if r.strip()]
    if not role_names:
        return _render_write_form_error(request, "Select at least one realm role to grant.")

    envelope = await _dispatch_write(
        operator,
        op_id="keycloak.role_mapping.assign",
        target=active_target,
        params={"roles": role_names, "id": user_uuid},
    )
    return _render_write_result(request, envelope)
