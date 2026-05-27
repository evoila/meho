# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/connectors`` -- the per-tenant targets list (table view).

Initiative #340 (G10.3 Connectors + Targets UI), Task #873 (T1) work
item #1. The route serves two response shapes from one handler:

* **Full page** (normal browser navigation) -- returns the
  ``connectors/list.html`` page extending ``base.html``. The chrome
  (navbar, sidebar, footer) matches the rest of the operator console.

* **HTMX fragment** (``HX-Request: true`` header set, sent by the
  HTMX 2 ``sse`` / ``hx-get`` directives) -- returns the
  ``connectors/_table_rows.html`` partial so a sort header click or
  product-filter change only re-renders the table body without a
  full-page reload.

Tenant scoping is non-overrideable. The substrate query passes
``session_ctx.tenant_id`` from the chassis-validated
:class:`~meho_backplane.ui.auth.middleware.UISessionContext`; no
query parameter, header, or path segment carries a tenant id, so a
cross-tenant target row never enters the rendered set (per the
acceptance criterion on #873).

URL contract::

    GET /ui/connectors
        [?sort=name|product|host|last_probed_at|status
         &dir=asc|desc
         &product=<exact-match slug>]

Out-of-range ``sort`` / ``dir`` -> 422 (Pydantic enum validator at the
HTTP boundary). The handler defaults to ``sort=name`` / ``dir=asc`` --
a stable, human-meaningful order matching the `/api/v1/targets` list
route's ``ORDER BY name`` (G0.3-T3 / #254).

last_probed_at + status
-----------------------

The ``last_probed_at`` column surfaces the timestamp of the target's
most recent successful probe. It is sourced from
``targets.updated_at`` -- the
:func:`~meho_backplane.api.v1.targets.probe_target` handler refreshes
``updated_at`` on every successful probe persist (and is the only
mutator that also writes the ``fingerprint`` column), so on a target
that has been probed at least once the column is the probe timestamp;
on a target that has never been probed the column is the row's
``created_at``. ``status`` is the rendered classification ``ok`` /
``stale`` / ``never``: ``never`` when ``fingerprint IS NULL``,
``stale`` when the probe is older than the freshness window
(:data:`_STALE_THRESHOLD` -- 24 h), ``ok`` otherwise. Both columns are
computed in the handler so the substrate ``Target`` model stays a
plain CRUD shape; the substrate would have to teach itself the
freshness window otherwise and the freshness contract is a UI-side
concern (operators tune the window; agents read the raw timestamp via
``/api/v1/targets``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_list_router"]


class _SortColumn(StrEnum):
    """Closed enum of sort columns exposed in the URL.

    Mirrors the human-meaningful columns in the rendered table. An
    out-of-enum value fails Pydantic validation at the HTTP boundary
    with a 422 carrying the candidate list in the error context. The
    enum's ``str`` mixin keeps the template's ``{{ sort }}`` rendering
    + ``href`` building stable -- ``str(_SortColumn.NAME)`` is
    ``"name"`` (not ``"_SortColumn.NAME"``).
    """

    NAME = "name"
    PRODUCT = "product"
    HOST = "host"
    LAST_PROBED_AT = "last_probed_at"
    STATUS = "status"


class _SortDirection(StrEnum):
    """Sort direction -- ``asc`` (default) or ``desc``."""

    ASC = "asc"
    DESC = "desc"


#: Threshold for the ``stale`` status classification. A target whose
#: most recent successful probe is older than this is rendered with a
#: warning pill so the operator notices the staleness before relying
#: on the cached fingerprint. 24 h matches the freshness signal the
#: G0.6 dispatcher's connector-resolution log line uses to flag a
#: cold fingerprint (the resolver does not refuse to dispatch on
#: stale fingerprint -- it logs and proceeds; the UI flag is the
#: operator-facing complement).
_STALE_THRESHOLD: Final[timedelta] = timedelta(hours=24)


def _coerce_utc_aware(ts: datetime) -> datetime:
    """Normalise *ts* to a tz-aware UTC :class:`datetime`.

    The ``DateTime(timezone=True)`` ORM column round-trips tz-aware
    on PostgreSQL but the SQLite ``aiosqlite`` driver hands back
    naive datetimes (the chassis test fixture leaves the connection's
    ``detect_types`` flag unset; the column is stored as an ISO
    string and reconstructed without the offset). The handler does
    timedelta arithmetic between the live "now" (tz-aware) and the
    row's ``updated_at``, so a naive value raises ``TypeError: can't
    subtract offset-naive and offset-aware datetimes`` mid-render.
    Coercing here keeps the substrate dialect-portable without
    teaching the column shape about the dialect mismatch.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _target_status(target: TargetORM, *, now: datetime) -> Literal["ok", "stale", "never"]:
    """Classify a target's freshness for the table's status column.

    ``never`` when ``fingerprint IS NULL`` -- the target has never
    been probed. ``stale`` when the row's ``updated_at`` is older
    than :data:`_STALE_THRESHOLD` (the probe path is the only writer
    that bumps ``updated_at`` alongside the ``fingerprint`` column,
    so a stale ``updated_at`` is a stale probe). ``ok`` otherwise.

    ``now`` is injected so the test suite can pin a deterministic
    "now" without monkeypatching :func:`datetime.now`.
    """
    if target.fingerprint is None:
        return "never"
    # ORM stores ``updated_at`` tz-aware on PG; on the SQLite test
    # dialect the round-trip drops tzinfo so we coerce here before
    # the comparison.
    if now - _coerce_utc_aware(target.updated_at) >= _STALE_THRESHOLD:
        return "stale"
    return "ok"


def _apply_sort(
    stmt: Select[tuple[TargetORM]],
    *,
    sort: _SortColumn,
    direction: _SortDirection,
) -> Select[tuple[TargetORM]]:
    """Apply the requested column + direction to *stmt*.

    The ``status`` and ``last_probed_at`` columns are computed in the
    handler (not stored), so the SQL-level ``ORDER BY`` covers the
    other three columns directly; status / last_probed_at fall back
    to ``updated_at`` (the underlying timestamp the status derives
    from) for stable database-level ordering, with a Python-side
    re-sort layered on top so the rendered order still honours the
    semantic status classification. The semantics:

    * ``name`` / ``product`` / ``host`` -- direct column sort, the SQL
      ``ORDER BY`` is the rendered order.
    * ``last_probed_at`` -- ``ORDER BY updated_at`` (same column).
    * ``status`` -- ``ORDER BY updated_at`` (a proxy); the handler then
      re-sorts the page's rows by the classified status string after
      :func:`_target_status` runs so ``never`` < ``stale`` < ``ok``
      ordering reflects the freshness ladder.
    """
    column_map = {
        _SortColumn.NAME: TargetORM.name,
        _SortColumn.PRODUCT: TargetORM.product,
        _SortColumn.HOST: TargetORM.host,
        _SortColumn.LAST_PROBED_AT: TargetORM.updated_at,
        _SortColumn.STATUS: TargetORM.updated_at,
    }
    column = column_map[sort]
    if direction == _SortDirection.DESC:
        return stmt.order_by(column.desc(), TargetORM.name)
    return stmt.order_by(column.asc(), TargetORM.name)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request.

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives
    drive (https://htmx.org/reference/#request_headers). The handler
    branches on this header to decide between rendering the full page
    (a normal navigation) and the table-rows fragment (a sort or
    filter swap). The check is case-insensitive because HTTP header
    names are case-insensitive by spec.
    """
    return request.headers.get("hx-request", "").lower() == "true"


def _next_direction_factory(
    current_sort: _SortColumn,
    current_direction: _SortDirection,
) -> object:
    """Return a Jinja-callable closure computing the next sort direction.

    Clicking the currently-active sort column toggles asc/desc; clicking
    a different column resets to asc. The returned object is a small
    one-arg callable the template invokes as
    ``next_direction_for(col_value)``; the template never has to learn
    about ``_SortColumn`` / ``_SortDirection`` enums.
    """

    def _call(target: str) -> str:
        if current_sort.value == target:
            return (
                _SortDirection.DESC.value
                if current_direction == _SortDirection.ASC
                else _SortDirection.ASC.value
            )
        return _SortDirection.ASC.value

    return _call


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom
#: matching the chassis dashboard + topology + memory routes (no
#: function calls in default argument positions).
_require_ui_session_dep = Depends(require_ui_session)
_get_raw_session_dep = Depends(get_raw_session)
_role_probe_dep = Depends(resolve_role_probe)


async def _list_targets(
    db_session: AsyncSession,
    *,
    tenant_id: object,
    product_filter: str | None,
    sort: _SortColumn,
    direction: _SortDirection,
) -> list[TargetORM]:
    """Pull the tenant's targets honouring the active filter + sort.

    Tenant scoping is the first ``WHERE`` clause -- the
    cross-tenant-isolation acceptance criterion is enforced at the
    substrate layer, never at the template. ``product_filter`` is
    exact-match (matching the ``/api/v1/targets`` list route's
    ``?product=`` shape from #254) so the dropdown's
    selected-or-cleared state maps 1:1 to the active query.
    """
    stmt = select(TargetORM).where(TargetORM.tenant_id == tenant_id)
    if product_filter:
        stmt = stmt.where(TargetORM.product == product_filter)
    stmt = _apply_sort(stmt, sort=sort, direction=direction)
    result = await db_session.execute(stmt)
    return list(result.scalars().all())


async def _distinct_products(
    db_session: AsyncSession,
    *,
    tenant_id: object,
) -> list[str]:
    """Return the distinct ``product`` values present in this tenant.

    Drives the product-filter dropdown's option list. Sourced from
    the live data (not a closed enum) because the connector registry
    grows as new connectors land -- ingested / typed / composite
    connectors all create their own product slugs, and an operator's
    catalogue may carry mature products plus an experimental one in
    the same tenant. A ``SELECT DISTINCT`` round trip on the
    targets table costs one extra query per page render; the table
    is already paged so the cost is bounded.
    """
    stmt = (
        select(TargetORM.product)
        .where(TargetORM.tenant_id == tenant_id)
        .distinct()
        .order_by(TargetORM.product)
    )
    result = await db_session.execute(stmt)
    return [row for row in result.scalars().all() if row]


_STATUS_ORDER: Final[dict[str, int]] = {"never": 0, "stale": 1, "ok": 2}


def _render_rows(
    targets: list[TargetORM],
    *,
    now: datetime,
    sort: _SortColumn,
    direction: _SortDirection,
) -> list[dict[str, object]]:
    """Project rows into the template-friendly dict shape.

    Each dict carries the column values plus the computed status
    string and a flag for the VPN icon. When sorting by ``status``
    we apply the rendered-order re-sort here (the SQL-level sort
    falls back to ``updated_at``); for the other columns the SQL
    order is the rendered order.
    """
    rows = [
        {
            "name": t.name,
            "aliases": list(t.aliases),
            "product": t.product,
            "host": t.host,
            "auth_model": t.auth_model,
            "vpn_required": t.vpn_required,
            "last_probed_at": _coerce_utc_aware(t.updated_at),
            "status": _target_status(t, now=now),
        }
        for t in targets
    ]
    if sort == _SortColumn.STATUS:
        rows.sort(
            key=lambda r: (
                _STATUS_ORDER[str(r["status"])],
                str(r["name"]),
            ),
            reverse=direction == _SortDirection.DESC,
        )
    return rows


async def _render(
    request: Request,
    *,
    sort: _SortColumn,
    direction: _SortDirection,
    product_filter: str | None,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the list page or the table-rows fragment.

    Branches on ``HX-Request``. Both branches receive the same context
    shape so the fragment template and the full-page template remain
    interchangeable -- swapping ``connectors/list.html`` for
    ``connectors/_table_rows.html`` and back never requires a context
    rewrite.
    """
    targets = await _list_targets(
        db_session,
        tenant_id=session_ctx.tenant_id,
        product_filter=product_filter,
        sort=sort,
        direction=direction,
    )
    products = await _distinct_products(db_session, tenant_id=session_ctx.tenant_id)
    now = datetime.now(UTC)
    rows = _render_rows(targets, now=now, sort=sort, direction=direction)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = {
        "page_title": "Targets",
        "active_surface": "connectors",
        "rows": rows,
        # Template macro ``_relative_time`` reads ``now_utc`` to render
        # the "X ago" column. Sharing one "now" across every row keeps
        # the relative timestamps consistent within a single page
        # render (otherwise per-row calls to ``datetime.now`` would
        # drift across millisecond boundaries).
        "now_utc": now,
        "product_options": products,
        "product_filter": product_filter or "",
        "sort": sort,
        "direction": direction,
        "next_direction_for": _next_direction_factory(sort, direction),
        "csrf_token": csrf_token,
        # tenant_admin gate for the "Create target" button (T2 #874).
        # The create / edit routes re-check the role server-side via
        # ``resolve_operator_or_403``; the template hides the affordance
        # from operators who can't use it so the button only surfaces to
        # tenant_admins. Fails soft to ``False`` (button hidden) on a
        # transient JWT-validation hiccup -- the write routes remain the
        # security authority.
        "is_tenant_admin": is_tenant_admin,
        # The footer in ``base.html`` reads ``ready`` to colour the
        # readiness pill; the connectors surface doesn't poll readiness
        # (the dashboard owns that), so ship ``False`` here so Jinja's
        # ``StrictUndefined`` env does not raise on the read.
        "ready": False,
    }
    template_name = (
        "connectors/_table_rows.html" if _is_htmx_request(request) else "connectors/list.html"
    )
    response = get_templates().TemplateResponse(request, template_name, context)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def build_list_router() -> APIRouter:
    """Construct the targets-list :class:`APIRouter`.

    Registers the single ``GET /ui/connectors`` route serving both
    the full page and the HTMX fragment from one handler. The route
    name (``ui_connectors_list``) is referenced by ``url_for`` in the
    list template's sort header links and the chassis sidebar -- a
    rename here must update both in lockstep.
    """
    router = APIRouter(tags=["ui-connectors"])

    async def _handler(
        request: Request,
        sort: _SortColumn = Query(default=_SortColumn.NAME),
        direction: _SortDirection = Query(default=_SortDirection.ASC, alias="dir"),
        product: str | None = Query(default=None, max_length=100),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_raw_session_dep,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/connectors``. See module docstring for the
        URL contract.

        ``dir`` (not ``direction``) is the spelling on the URL the
        issue body pinned -- the alias above keeps the Python kwarg
        readable (``direction``) without breaking the URL contract
        the operator sees in the address bar.
        """
        return await _render(
            request,
            sort=sort,
            direction=direction,
            product_filter=product,
            session_ctx=session_ctx,
            db_session=db_session,
            is_tenant_admin=role_probe.is_tenant_admin,
        )

    router.add_api_route(
        "/ui/connectors",
        _handler,
        methods=["GET"],
        name="ui_connectors_list",
        response_class=HTMLResponse,
    )
    return router
