# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Docs-corpus admin Collections lifecycle table + register modal.

Initiative #1836 (G10.10 Doc Collections lifecycle UI), Task #1882 (T1).
The Docs Corpus surface (G10.7 #1777) is search-only; its empty state
tells operators to leave the UI and "ask an administrator" to register a
collection. This module adds the **admin-facing** half: a Collections
lifecycle table (one row per collection: key / vendor / products / status
/ doc_count / last_ingested_at) rendered as a second tab on ``/ui/corpus``,
plus an HTMX-loaded register modal that drives the in-process
``create_doc_collection`` service. T2 (#1883) layers the per-collection
detail + re-probe + enable/disable.

Routes (all under ``/ui/corpus/collections``):

* ``GET /ui/corpus/collections`` -- the lifecycle table. Renders the tab
  body: every collection visible to the operator's tenant, with the
  "Register collection" affordance shown only to a ``tenant_admin``. Both
  the full-page and the HTMX-fragment shapes are served from one handler
  (branch on ``HX-Request``) so a tab-switch loads only the table body.
* ``GET /ui/corpus/collections/register`` -- the HTMX-loaded register
  modal fragment. ``tenant_admin``-gated server-side.
* ``POST /ui/corpus/collections/register`` -- the register submit handler.
  ``tenant_admin``-gated server-side; calls ``create_doc_collection``
  in-process (not the Bearer REST API) so the UI write and the REST write
  share one validation + backend-type-check + conflict + audit code path.

Why the FULL registry, not the entitlement-filtered catalogue
-------------------------------------------------------------

The Search picker lists only the collections the *operator* holds
``meho-docs:<collection_key>`` for (:func:`_list_entitled_collections` /
``GET /api/v1/doc_collections``). That is wrong for an admin lifecycle
table: a ``tenant_admin`` manages rows their personal identity may not be
entitled to search. So this table runs the tenant-first query
(``tenant_id == operator.tenant_id OR tenant_id IS NULL``) + the
tenant-first dedupe (:func:`_dedupe_tenant_first`) with **no capability
filter** -- it lists the tenant's complete registry. The row data is the
registry (identity + liveness), not the entitlement-scoped search catalogue,
so it is safe to display to any operator; only the mutating "Register
collection" affordance is admin-gated.

Error mapping (load-bearing)
----------------------------

``create_doc_collection`` raises :class:`DocCollectionConflictError`
(duplicate ``collection_key``) and :class:`DocCollectionBackendTypeError`
(unregistered ``backend.type``). Neither is a subclass of ``ValueError`` /
``ValidationError``, so the submit handler catches them **explicitly** and
re-renders the modal with a per-field error (409 conflict on
``collection_key``, 422 backend-type on ``backend_type``) -- a legible
message, never a stack trace. A ``ValidationError`` from
:class:`DocCollectionCreate` (blank key, blank product token, malformed
``ref`` JSON) re-renders the modal with per-field messages + 422, mirroring
the connectors ``submit_create``.

RBAC
----

The table is read-only and shows the registry, so it renders for any
operator; :func:`resolve_role_probe` (soft-hide) drives the
``is_tenant_admin`` flag that gates the "Register collection" button. The
register GET (modal) and POST (submit) hard-gate via
:func:`resolve_operator_or_403` -- a crafted POST from a non-admin gets a
server-side 403 regardless of whether the button rendered.

CSRF
----

The register form carries its own ``hx-headers`` ``X-CSRF-Token`` (HTMX
does not propagate ``hx-headers`` to a descendant form -- the #1693 class).
The modal render mints a token and (re-)sets the ``meho_csrf`` cookie so
the double-submit pair lines up; the submit is verified by the chassis
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` before the handler runs.

Route ordering (load-bearing)
------------------------------

The literal ``/ui/corpus/collections/register`` route is registered
**before** ``/ui/corpus/collections`` and before any future
``/ui/corpus/collections/{collection_key}`` param route (T2 #1883) so the
literal ``register`` segment is never captured as a ``collection_key`` --
the first-match-wins discipline
:func:`meho_backplane.ui.routes.connectors.build_router` documents.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_session, get_sessionmaker
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import (
    DocCollectionBackendTypeError,
    DocCollectionConflictError,
    DocCollectionCreate,
    create_doc_collection,
    project_doc_collection_create_response,
)
from meho_backplane.docs_collections.schemas import DocCollectionBackend
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_operator_or_403,
    resolve_role_probe,
)
from meho_backplane.ui.routes.corpus.collections_form import (
    dedupe_tenant_first,
    parse_backend_ref,
    parse_products,
    validation_errors_by_field,
)
from meho_backplane.ui.routes.corpus.routes import _resolve_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["build_corpus_collections_router"]

_log = structlog.get_logger(__name__)

#: Form-field length caps mirroring the :class:`DocCollectionCreate` field
#: bounds. The server-side Pydantic validation is authoritative; these caps
#: bound the form-body parse against a paste-from-clipboard accident before
#: the bytes reach the schema (the connectors forms-router idiom).
_COLLECTION_KEY_MAX = 128
_VENDOR_MAX = 256
_PRODUCTS_MAX = 2048
_DESCRIPTION_MAX = 8192
_WHEN_TO_USE_MAX = 8192
_BACKEND_TYPE_MAX = 128
_BACKEND_REF_MAX = 16384

#: Module-level :class:`Depends` closures -- ruff B008 idiom matching the
#: chassis dashboard / topology / connectors routes (no function calls in
#: default argument positions).
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_operator_or_403)
_role_probe_dep = Depends(resolve_role_probe)
_get_session_dep = Depends(get_session)


async def _list_tenant_collections(operator: Operator) -> list[DocCollectionORM]:
    """List the operator's tenant's FULL doc-collection registry.

    Tenant-scoped (global + this tenant's rows) + tenant-first dedupe, with
    **no capability filter** -- the admin lifecycle table manages every row
    the tenant owns, including ones the operator's own identity is not
    entitled to *search* (the entitlement filter is a search-path concern,
    not a registry-management one). This is the deliberate divergence from
    :func:`meho_backplane.ui.routes.corpus.routes._list_entitled_collections`
    and ``GET /api/v1/doc_collections``, both of which filter by
    ``collection_capability_key(...) in operator.capabilities``.

    Rows are returned sorted by ``collection_key`` for a stable render.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        stmt = select(DocCollectionORM).where(
            (DocCollectionORM.tenant_id == operator.tenant_id)
            | (DocCollectionORM.tenant_id.is_(None)),
        )
        result = await db_session.execute(stmt)
        raw_rows = list(result.scalars().all())

    rows = dedupe_tenant_first(raw_rows)
    rows.sort(key=lambda row: row.collection_key)
    return rows


def _project_row(row: DocCollectionORM) -> dict[str, object]:
    """Project an ORM row into the template-friendly table-row dict.

    Carries the lifecycle table's columns: ``collection_key`` / ``vendor`` /
    ``products`` / ``status`` (rendered as a pill) / ``doc_count`` /
    ``last_ingested_at``. The ``backend`` record is deliberately omitted --
    it is server-side-only (#1548) and surfaces only on the T2 detail page.
    """
    return {
        "collection_key": row.collection_key,
        "vendor": row.vendor,
        "products": list(row.products),
        "status": row.status,
        "doc_count": row.doc_count,
        "last_ingested_at": row.last_ingested_at,
    }


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    Mirrors the SameSite=Strict + Secure + non-HttpOnly posture every UI
    surface's CSRF cookie carries (HTMX must read it to populate
    ``X-CSRF-Token``); the value MUST equal the token the rendered markup
    echoes via ``hx-headers`` or the CSRF middleware rejects the submit.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


async def _render_table(
    request: Request,
    *,
    session_ctx: UISessionContext,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the Collections table -- full tab body or HTMX fragment.

    Branches on ``HX-Request``: a tab-switch (HTMX) loads only the table
    fragment (``corpus/_collections_table.html``); a direct navigation to
    ``/ui/corpus/collections`` renders the full corpus page with the
    Collections tab active. The operator is reconstructed via
    :func:`_resolve_operator` for tenant scope, then the FULL registry is
    listed (no entitlement filter).
    """
    operator = await _resolve_operator(session_ctx)
    rows = [_project_row(row) for row in await _list_tenant_collections(operator)]
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "active_surface": "corpus",
        "page_title": "Docs Corpus",
        "active_tab": "collections",
        "collection_rows": rows,
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
    }
    template = (
        "corpus/_collections_table.html"
        if request.headers.get("hx-request", "").lower() == "true"
        else "corpus/collections.html"
    )
    response = get_templates().TemplateResponse(request, template, context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _build_modal_context(csrf_token: str) -> dict[str, object]:
    """Build the register-modal template context (bare or error re-render)."""
    return {
        "active_surface": "corpus",
        "page_title": "Docs Corpus",
        "csrf_token": csrf_token,
        # Bare-form render carries no errors / submitted values.
        "errors": {},
        "values": {},
    }


async def _render_register_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded register-collection modal fragment.

    Mints a CSRF token + (re-)sets the ``meho_csrf`` cookie so the modal's
    own ``hx-headers`` ``X-CSRF-Token`` echo lines up with the cookie.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "corpus/_register_modal.html",
        _build_modal_context(csrf_token),
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _render_modal_with_errors(
    request: Request,
    *,
    session_id: str,
    errors: dict[str, str],
    values: dict[str, object],
    status_code: int,
) -> HTMLResponse:
    """Re-render the register modal carrying per-field errors + *status_code*.

    The HTMX form swaps ``#corpus-collections-modal-container`` so the
    response body (the re-rendered ``<dialog>``) replaces the modal in place;
    the operator keeps their typed values (echoed via ``values``) and sees
    the field-level messages. Mirrors the connectors
    ``_render_form_with_errors`` shape.
    """
    csrf_token = mint_csrf_token(session_id)
    context = _build_modal_context(csrf_token)
    context["errors"] = errors
    context["values"] = values
    response = get_templates().TemplateResponse(
        request,
        "corpus/_register_modal.html",
        context,
        status_code=status_code,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _build_create_body(
    request: Request,
    *,
    session_id: str,
    raw_values: dict[str, object],
    collection_key: str,
    vendor: str,
    products_raw: str | None,
    description: str | None,
    when_to_use: str | None,
    backend_type: str,
    backend_ref_raw: str | None,
) -> DocCollectionCreate | HTMLResponse:
    """Build a validated :class:`DocCollectionCreate`, or an error re-render.

    Parses the raw ``backend.ref`` JSON textarea + runs the Pydantic
    validation. Returns the validated body on success, or the re-rendered
    modal (422) carrying a per-field error on a malformed ``backend.ref`` or
    a :class:`ValidationError` (blank key / blank product token / blank
    ``backend.type``). Split from :func:`_submit_register` so the submit
    handler stays under the function-size cap.
    """
    try:
        backend_ref = parse_backend_ref(backend_ref_raw)
    except ValueError as exc:
        return _render_modal_with_errors(
            request,
            session_id=session_id,
            errors={"backend_ref": str(exc)},
            values=raw_values,
            status_code=422,
        )
    try:
        return DocCollectionCreate(
            collection_key=collection_key,
            vendor=vendor,
            products=parse_products(products_raw),
            description=description or None,
            when_to_use=when_to_use or None,
            backend=DocCollectionBackend(type=backend_type, ref=backend_ref),
        )
    except ValidationError as exc:
        return _render_modal_with_errors(
            request,
            session_id=session_id,
            errors=validation_errors_by_field(exc),
            values=raw_values,
            status_code=422,
        )


async def _submit_register(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    db_session: AsyncSession,
    *,
    collection_key: str,
    vendor: str,
    products_raw: str | None,
    description: str | None,
    when_to_use: str | None,
    backend_type: str,
    backend_ref_raw: str | None,
) -> HTMLResponse:
    """Register a collection via the in-process service, mapping failures legibly.

    Builds a :class:`DocCollectionCreate` (validation re-renders the modal
    with field errors + 422) and passes it to :func:`create_doc_collection`
    -- **not** the Bearer REST API -- so the UI write and the REST write
    share one validation + backend-type-check + conflict + audit code path;
    ``operator.tenant_id`` scopes the row. The two service exceptions are
    caught **explicitly** (neither subclasses ``ValueError``):
    :class:`DocCollectionBackendTypeError` -> 422 ``backend_type`` error,
    :class:`DocCollectionConflictError` -> 409 ``collection_key`` error.

    Success -> 204 + ``HX-Redirect: /ui/corpus/collections`` so HTMX reloads
    the table with the new ``provisioning`` row.
    """
    session_id = str(session_ctx.session_id)
    raw_values: dict[str, object] = {
        "collection_key": collection_key,
        "vendor": vendor,
        "products": products_raw or "",
        "description": description or "",
        "when_to_use": when_to_use or "",
        "backend_type": backend_type,
        "backend_ref": backend_ref_raw or "",
    }

    built = _build_create_body(
        request,
        session_id=session_id,
        raw_values=raw_values,
        collection_key=collection_key,
        vendor=vendor,
        products_raw=products_raw,
        description=description,
        when_to_use=when_to_use,
        backend_type=backend_type,
        backend_ref_raw=backend_ref_raw,
    )
    if isinstance(built, HTMLResponse):
        return built

    try:
        row = await create_doc_collection(db_session, operator, built)
    except DocCollectionBackendTypeError as exc:
        return _render_modal_with_errors(
            request,
            session_id=session_id,
            errors={"backend_type": str(exc.detail["message"])},
            values=raw_values,
            status_code=422,
        )
    except DocCollectionConflictError as exc:
        return _render_modal_with_errors(
            request,
            session_id=session_id,
            errors={"collection_key": str(exc)},
            values=raw_values,
            status_code=409,
        )

    # Surface the ``next_step`` probe hint in the success log; the committed
    # redirect lands the operator back on the table where the new
    # ``provisioning`` row is visible.
    created = project_doc_collection_create_response(row)
    _log.info(
        "ui_doc_collection_register",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        collection_key=built.collection_key,
        next_step=created.next_step,
    )
    return HTMLResponse(
        status_code=204,
        headers={"HX-Redirect": "/ui/corpus/collections"},
    )


async def _table_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
) -> HTMLResponse:
    """``GET /ui/corpus/collections`` -- the lifecycle table (page or fragment)."""
    return await _render_table(
        request,
        session_ctx=session_ctx,
        is_tenant_admin=role_probe.is_tenant_admin,
    )


async def _register_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/corpus/collections/register`` -- HTMX-loaded register modal."""
    del operator  # gate only; render needs no operator-specific context.
    return await _render_register_modal(request, session_ctx)


async def _register_submit_handler(
    request: Request,
    # ``Form(default="")`` (not ``Form(...)``) on the text fields so an
    # empty / omitted submit flows to the ``DocCollectionCreate`` Pydantic
    # validation (which re-renders the modal with field errors) rather than
    # tripping FastAPI's own raw 422 -- the modal-rendering handler owns
    # validation, not the framework boundary (the connectors forms idiom).
    collection_key: str = Form(default="", max_length=_COLLECTION_KEY_MAX),
    vendor: str = Form(default="", max_length=_VENDOR_MAX),
    products: str | None = Form(default=None, max_length=_PRODUCTS_MAX),
    description: str | None = Form(default=None, max_length=_DESCRIPTION_MAX),
    when_to_use: str | None = Form(default=None, max_length=_WHEN_TO_USE_MAX),
    backend_type: str = Form(default="", max_length=_BACKEND_TYPE_MAX),
    backend_ref: str | None = Form(default=None, max_length=_BACKEND_REF_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/corpus/collections/register`` -- register one collection."""
    return await _submit_register(
        request,
        session_ctx,
        operator,
        db_session,
        collection_key=collection_key,
        vendor=vendor,
        products_raw=products,
        description=description,
        when_to_use=when_to_use,
        backend_type=backend_type,
        backend_ref_raw=backend_ref,
    )


def build_corpus_collections_router() -> APIRouter:
    """Construct the ``/ui/corpus/collections*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without shared route state -- the corpus / connectors
    router convention. Registration order is **load-bearing**: the literal
    ``/ui/corpus/collections/register`` route registers before
    ``/ui/corpus/collections`` and before any future
    ``/ui/corpus/collections/{collection_key}`` param route (T2 #1883) so
    the literal ``register`` segment is never captured as a
    ``collection_key`` -- first-match-wins.
    """
    router = APIRouter(tags=["ui-corpus"])
    # Literal ``/register`` FIRST -- ahead of the table route and any future
    # ``/{collection_key}`` param route (T2 #1883) so ``register`` never
    # binds as a collection key.
    router.add_api_route(
        "/ui/corpus/collections/register",
        _register_modal_handler,
        methods=["GET"],
        name="ui_corpus_collections_register_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/corpus/collections/register",
        _register_submit_handler,
        methods=["POST"],
        name="ui_corpus_collections_register_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/corpus/collections",
        _table_handler,
        methods=["GET"],
        name="ui_corpus_collections_table",
        response_class=HTMLResponse,
    )
    return router
