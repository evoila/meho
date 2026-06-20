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

from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session
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

#: Module-level ``Depends`` closures -- built once (rather than inline) to
#: satisfy ruff B008, matching the operations / approvals / kb routers.
_require_session = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    The dispatch needs a real :class:`Operator` (the meta-tool reads
    ``operator.tenant_id`` for the tenant-scoped resolve, and the keycloak
    read ops authorise the operator-context Vault read that backs the
    admin-token mint). Mirrors the operations console's lift: load the
    decrypted session row and re-verify its access token through the
    chassis JWT chain so a same-session role demotion is caught on the next
    browse rather than cached for the session TTL.

    Raises :class:`fastapi.HTTPException` 401 when the session was revoked
    / expired between the middleware check and here.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    settings = get_settings()
    return await verify_jwt_for_audience(
        f"Bearer {decrypted.access_token}",
        expected_audience=settings.keycloak_audience,
    )


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

    # NOTE: the literal ``/ui/keycloak/clients/{client_uuid}`` route is
    # registered BEFORE the bare ``/ui/keycloak`` index. The only ``{param}``
    # route sits under the distinct ``/ui/keycloak/clients/`` prefix, so a
    # future literal segment (``/ui/keycloak/users`` in T2) registered ahead
    # of any ``{param}`` route binds first (first-match-wins -- the ordering
    # discipline the operations / approvals routers document).

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
