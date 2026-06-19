# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/connectors/registry`` -- the connector **registry** list.

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1885 (T1). Distinct from the ``/ui/connectors`` **targets** list
(``list_view.py``): that surface lists ``targets`` rows (a connectable
host + credentials); this one lists the connector **registry** -- the
ingested / typed / composite connectors whose grouped operations the
operator curates and enables for dispatch.

The route serves two response shapes from one handler, mirroring
:mod:`~meho_backplane.ui.routes.connectors.list_view`:

* **Full page** (browser navigation) -- ``connectors/registry.html``
  extending ``base.html`` so the chrome matches the rest of the console.
* **HTMX fragment** (``HX-Request: true``) -- the
  ``connectors/_registry_table.html`` ``<tbody>`` partial so a status /
  product filter change re-renders only the table body.

Read RBAC is **operator-level** (per the backend service's own
contract: :func:`list_ingested_connectors` gates visibility on tenant,
not role). The handler lifts the full :class:`Operator` -- the service
needs it to scope visibility (built-ins + the caller's tenant; never
cross-tenant) -- via :func:`resolve_role_probe`'s soft-hide companion;
the per-row write affordances are hidden from non-``tenant_admin``
operators (the ``is_tenant_admin`` template flag), but the write routes
in :mod:`~meho_backplane.ui.routes.connectors.registry_actions` remain
the security authority (``resolve_operator_or_403``).

URL contract::

    GET /ui/connectors/registry
        [?status=staged|enabled|disabled|all
         &product=<exact-match slug>]

``status`` is a closed enum (:class:`_StatusFilter`) -- an out-of-range
value 422s at the Pydantic boundary. The default + "no narrowing" state
is the real ``all`` sentinel (NOT an empty string): the backend
:data:`~meho_backplane.operations.ingest.api_schemas.ConnectorStatusFilter`
enum 422s on an out-of-range value, so the filter ``<select>`` must
submit ``all`` (or omit the param) rather than ``value=""`` -- an empty
string would fail enum validation and make the HTMX swap silently no-op.
``product`` is exact-match, its option list computed in the handler from
the distinct ``product`` values of the rows the service already returned
(no extra DB query -- the service does not need one).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.operations.ingest import (
    ConnectorListItem,
    ConnectorStatusFilter,
    list_ingested_connectors,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.routes.corpus.routes import _resolve_operator
from meho_backplane.ui.templating import get_templates

__all__ = [
    "build_registry_list_router",
    "render_registry_row",
    "render_registry_table",
]


class _StatusFilter(StrEnum):
    """Closed enum mirroring the backend ``ConnectorStatusFilter`` literal.

    Wrapping the service's ``Literal["staged", "enabled", "disabled",
    "all"]`` in a ``StrEnum`` gives the FastAPI query param a real enum
    to validate against -- an out-of-range ``?status=`` 422s at the HTTP
    boundary (the issue's filter contract) rather than reaching the
    service. ``ALL`` is the default + the explicit no-narrowing sentinel:
    the filter ``<select>`` submits ``status=all`` (never ``value=""``),
    which the handler maps to ``status=None`` on the service call so the
    full visible set returns. The ``str`` mixin keeps ``{{ status.value
    }}`` rendering stable (``"all"``, not ``"_StatusFilter.ALL"``).
    """

    STAGED = "staged"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ALL = "all"


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom
#: matching the list / detail / corpus routes (no calls in default
#: argument positions).
_require_ui_session_dep = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)


def _row_context(item: ConnectorListItem) -> dict[str, Any]:
    """Project one :class:`ConnectorListItem` into the template row dict.

    Flattens the wire shape into the fields the row template reads and
    derives the ``is_builtin`` chip (``tenant_id IS NULL`` ⇒ a built-in
    / global connector). Keeping the projection here (not in the
    template) lets the single-row OOB swap path
    (:mod:`~meho_backplane.ui.routes.connectors.registry_actions`) and
    the table-render path share one row shape.
    """
    return {
        "connector_id": item.connector_id,
        "product": item.product,
        "version": item.version,
        "impl_id": item.impl_id,
        "is_builtin": item.tenant_id is None,
        "state": item.state,
        "group_count": item.group_count,
        "staged_group_count": item.staged_group_count,
        "enabled_group_count": item.enabled_group_count,
        "disabled_group_count": item.disabled_group_count,
        "operation_count": item.operation_count,
        "enabled_operation_count": item.enabled_operation_count,
    }


def render_registry_row(
    request: Request,
    *,
    item: ConnectorListItem | None,
    connector_id: str,
    csrf_token: str,
    is_tenant_admin: bool,
    oob: bool,
) -> HTMLResponse:
    """Render a single registry ``<tr>`` (the OOB-swap unit).

    Used by the per-row action handlers
    (:mod:`~meho_backplane.ui.routes.connectors.registry_actions`) to
    swap just the affected row after a verb runs. ``oob=True`` stamps the
    ``hx-swap-oob`` attribute so HTMX replaces the matching ``<tr>`` by
    id without disturbing the rest of the table.

    *item* is ``None`` after a ``delete`` -- the connector is gone from
    the operator-visible read, so the template renders an empty
    out-of-band ``<tr>`` carrying only the row id, which removes the old
    row from the table on swap.
    """
    context: dict[str, Any] = {
        "row": _row_context(item) if item is not None else None,
        "connector_id": connector_id,
        "is_tenant_admin": is_tenant_admin,
        "oob": oob,
        "csrf_token": csrf_token,
    }
    return get_templates().TemplateResponse(
        request,
        "connectors/_registry_row.html",
        context,
    )


def _distinct_products(items: list[ConnectorListItem]) -> list[str]:
    """Return the sorted distinct ``product`` values across *items*.

    Drives the ``?product=`` filter dropdown. Computed from the rows the
    service already returned rather than a second DB round-trip -- the
    service does not need a distinct-product query and the result set is
    the same visibility scope the dropdown should offer.
    """
    return sorted({item.product for item in items})


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    The value MUST equal the token the rendered markup echoes via
    ``hx-headers`` or the CSRF middleware rejects the next state-changing
    submit (``value_mismatch``). Same SameSite=Strict + Secure +
    non-HttpOnly posture every UI surface's CSRF cookie carries (HTMX
    must read it to populate ``X-CSRF-Token``).
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def render_registry_table(
    request: Request,
    *,
    items: list[ConnectorListItem],
    status_filter: _StatusFilter,
    product_filter: str | None,
    session_ctx: UISessionContext,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the registry ``<tbody>`` fragment + re-set the CSRF cookie.

    The shared render path for the HTMX-filter swap (the standalone
    fragment the GET handler returns on ``HX-Request``) and the per-row
    action handlers' full-table re-render fallback. Mints a fresh token
    so the swapped fragment's ``hx-headers`` line up with the cookie.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "rows": [_row_context(item) for item in items],
        "status_filter": status_filter.value,
        "product_filter": product_filter or "",
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "connectors/_registry_table.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render(
    request: Request,
    *,
    status_filter: _StatusFilter,
    product_filter: str | None,
    session_ctx: UISessionContext,
    operator: Operator,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the registry list page or the table-rows fragment.

    Calls :func:`list_ingested_connectors` in-process (operator-scoped
    visibility, ``status`` narrowing) and branches on ``HX-Request``:
    the full page on a browser nav, the ``<tbody>`` fragment on a filter
    swap. ``status=all`` maps to ``status=None`` on the service call so
    the full visible set returns (the ``all`` sentinel never reaches the
    service as a literal).
    """
    service_status: ConnectorStatusFilter | None = (
        None
        if status_filter == _StatusFilter.ALL
        else cast(ConnectorStatusFilter, status_filter.value)
    )
    items = await list_ingested_connectors(operator=operator, status=service_status)
    if product_filter:
        items = [item for item in items if item.product == product_filter]

    if request.headers.get("hx-request", "").lower() == "true":
        return render_registry_table(
            request,
            items=items,
            status_filter=status_filter,
            product_filter=product_filter,
            session_ctx=session_ctx,
            is_tenant_admin=is_tenant_admin,
        )

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "page_title": "Connector Registry",
        "active_surface": "connectors-registry",
        "rows": [_row_context(item) for item in items],
        "product_options": _distinct_products(items),
        "status_filter": status_filter.value,
        "product_filter": product_filter or "",
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "connectors/registry.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def build_registry_list_router() -> APIRouter:
    """Construct the registry-list :class:`APIRouter`.

    Registers the single ``GET /ui/connectors/registry`` route serving
    both the full page and the HTMX fragment from one handler. The route
    name (``ui_connectors_registry_list``) is referenced by the registry
    page template + the sidebar nav -- a rename here must update both.

    The literal ``/ui/connectors/registry`` path MUST register before
    the parametrised ``GET /ui/connectors/{name}`` detail route
    (first-match-wins) so ``"registry"`` is never captured as a target
    ``name``; the include order in
    :func:`~meho_backplane.ui.routes.connectors.build_router` enforces it.
    """
    router = APIRouter(tags=["ui-connectors"])

    async def _handler(
        request: Request,
        status: _StatusFilter = Query(default=_StatusFilter.ALL),
        product: str | None = Query(default=None, max_length=100),
        session_ctx: UISessionContext = _require_ui_session_dep,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/connectors/registry``. See the module docstring."""
        operator = await _resolve_operator(session_ctx)
        return await _render(
            request,
            status_filter=status,
            product_filter=product,
            session_ctx=session_ctx,
            operator=operator,
            is_tenant_admin=role_probe.is_tenant_admin,
        )

    router.add_api_route(
        "/ui/connectors/registry",
        _handler,
        methods=["GET"],
        name="ui_connectors_registry_list",
        response_class=HTMLResponse,
    )
    return router
