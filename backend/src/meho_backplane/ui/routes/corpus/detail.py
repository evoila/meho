# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Docs-corpus per-collection detail + re-probe + confirm-gated enable/disable.

Initiative #1836 (G10.10 Doc Collections lifecycle UI), Task #1883 (T2).
T1 (#1882) shipped the admin Collections lifecycle table + register modal
on ``/ui/corpus``; this module completes the self-service loop with the
per-collection surface the table rows link to:

* ``GET /ui/corpus/collections/{collection_key}`` -- the detail page.
  Reconstructs the operator via :func:`_resolve_operator`, resolves the row
  tenant-first via :func:`resolve_doc_collection` (404 on an unknown /
  cross-tenant key), and renders the full :class:`DocCollection` read shape
  (:func:`project_doc_collection`): identity header + readiness card +
  -- **only when the operator is a ``tenant_admin``** -- the server-side-only
  ``backend{type, ref}`` record (#1548). The ``is_tenant_admin`` flag comes
  from :func:`resolve_role_probe` (soft-hide); a plain operator never sees
  the ``ref`` value.
* ``POST /ui/corpus/collections/{collection_key}/probe`` -- the HTMX
  re-probe action. ``tenant_admin``-gated server-side via
  :func:`resolve_operator_or_403`. Calls the in-process
  :func:`probe_collection` service inside the route's open transaction
  (mirrors ``api/v1/doc_collections.py``); a :class:`CorpusUnavailable`
  rolls the transaction back and renders a 503 alert fragment (the row's
  ``status`` is left untouched -- success-only write-back), a
  :class:`DocCollectionStateError` renders a 409 alert. On success the
  refreshed readiness card is swapped into ``#collection-readiness-card``.
* ``GET`` / ``POST`` ``/ui/corpus/collections/{collection_key}/disable`` --
  the confirm modal + the disable submit. ``disable`` is
  **availability-destructive** (a disabled collection fails ``search_docs``
  with a terminal ``403 collection_disabled`` for ALL searchers), so the
  modal spells out that impact, mirroring the connectors delete-confirm.
* ``POST /ui/corpus/collections/{collection_key}/enable`` -- the enable
  submit. ``enable`` is non-destructive, so the detail page carries a plain
  confirmed button (no scary modal).

Both enable / disable call the in-process :func:`set_collection_enabled`
service (returns ``False`` on the idempotent no-op -> "already X, nothing
changed"); a :class:`DocCollectionStateError` surfaces as a legible 409
``invalid_collection_transition`` alert, never a 500 / stack trace.

Why in-process services, not the Bearer REST API
------------------------------------------------

The probe / enable / disable verbs route through the in-process
:mod:`~meho_backplane.docs_collections.service` primitives (the same ones
``api/v1/doc_collections.py`` fronts), not an HTTP call back to the Bearer
API. The UI write and the REST write share one resolver + lifecycle-guard +
success-only-write-back code path so a future change to the state machine
takes effect on both surfaces at once -- the same posture T1's register
handler and the connectors re-probe handler use.

Transaction shape
-----------------

The probe / enable / disable handlers depend on :func:`get_session` (the
transactional dependency): the route's outer ``session.begin()`` commits on
a clean return and rolls back on a raise. :func:`probe_collection` /
:func:`set_collection_enabled` ``flush`` but never commit, so the route owns
commit / rollback. A 503 / 409 alert is *returned* (not raised) with the
matching status code; because the probe write is success-only, nothing was
flushed on the 503 path, so the commit-on-clean-return is a no-op and the
row's status is genuinely unchanged.

Route ordering (load-bearing)
-----------------------------

The action sub-routes carry an extra literal segment
(``/{collection_key}/probe`` / ``/disable`` / ``/enable``) so they are
unambiguous, but the bare ``GET /ui/corpus/collections/{collection_key}``
detail route is registered **after** all literal-prefixed routes -- and the
aggregating :func:`~meho_backplane.ui.routes.corpus.build_corpus_router`
includes the literal ``/register`` collections router (T1 #1882) **before**
this detail router -- so first-match-wins never binds a literal segment
(``register``) as a ``collection_key``. Mirrors
:func:`meho_backplane.ui.routes.connectors.build_router`.

CSRF
----

Every state-changing verb (probe / enable / disable) rides the double-submit
``meho_csrf`` cookie via its element's own ``hx-headers`` ``X-CSRF-Token``
(HTMX does not propagate ``hx-headers`` to a descendant form -- the #1693
class). Each card / modal render mints a token and re-sets the cookie so the
double-submit pair lines up.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_session
from meho_backplane.docs_collections import (
    DocCollectionStateError,
    probe_collection,
    project_doc_collection,
    resolve_doc_collection,
    set_collection_enabled,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_operator_or_403,
    resolve_role_probe,
)
from meho_backplane.ui.routes.corpus.routes import _resolve_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["build_corpus_collection_detail_router"]

_log = structlog.get_logger(__name__)

#: Collection keys cap at 128 chars in the schema (``DocCollectionCreate``),
#: so a longer path segment cannot exist in the table -- reject it with the
#: same 404 the resolver would raise after the round-trip, saving the query
#: on the fuzzer-spam vector (the connectors detail idiom).
_COLLECTION_KEY_MAX = 128

#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom matching
#: the connectors detail / probe routes (no function calls in default
#: argument positions).
_require_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)
_role_probe_dep = Depends(resolve_role_probe)
_require_admin_dep = Depends(resolve_operator_or_403)


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    The value MUST equal the token the rendered markup echoes via
    ``hx-headers`` or the CSRF middleware rejects the next state-changing
    submit (``value_mismatch``). Mirrors the SameSite=Strict + Secure +
    non-HttpOnly posture every UI surface's CSRF cookie carries (HTMX must
    read it to populate ``X-CSRF-Token``).
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _project_detail(collection: Any) -> dict[str, Any]:
    """Project the resolved ORM row into the detail template's context dict.

    Goes through :func:`project_doc_collection` (the single ORM->wire
    projection the create route also returns) so the detail page never drifts
    from the read shape, then dumps it to a plain dict for the template. The
    ``backend{type, ref}`` record rides through verbatim -- the template,
    gated on ``is_tenant_admin``, decides whether to render it.
    """
    return project_doc_collection(collection).model_dump()


def _render_readiness_card(
    request: Request,
    *,
    collection: Any,
    session_ctx: UISessionContext,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the readiness-card fragment + re-set the CSRF cookie.

    Used both by the full detail render (``{% include %}``d) and by the
    re-probe success path (returned standalone for the HTMX swap into
    ``#collection-readiness-card``). Centralised so the two paths carry the
    same context shape -- a widening on one path cannot break the other.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "collection": _project_detail(collection),
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "corpus/_readiness_card.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _render_probe_alert(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
) -> HTMLResponse:
    """Render the re-probe failure alert fragment for the readiness-card slot.

    Returned in place of the readiness card (same ``id`` wrapper) so the HTMX
    swap drops the alert into ``#collection-readiness-card`` without
    disturbing the rest of the detail page; a follow-up successful re-probe
    swaps the real card back into the same slot.
    """
    return get_templates().TemplateResponse(
        request,
        "corpus/_probe_alert.html",
        {"title": title, "message": message},
        status_code=status_code,
    )


async def _render_detail(
    request: Request,
    *,
    collection_key: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Render the per-collection detail page for *collection_key*.

    Reconstructs the operator for tenant scope, resolves the row tenant-first
    (404 on unknown / cross-tenant), and renders the full read shape. The
    ``backend{type, ref}`` record is passed to the template but only rendered
    when ``is_tenant_admin`` -- a plain operator never sees the ``ref`` value.
    """
    if not collection_key or len(collection_key) > _COLLECTION_KEY_MAX:
        raise HTTPException(status_code=404, detail=f"collection {collection_key!r} not found")
    operator = await _resolve_operator(session_ctx)
    collection = await resolve_doc_collection(db_session, collection_key, operator.tenant_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "active_surface": "corpus",
        "page_title": f"Docs Corpus · {collection.collection_key}",
        "collection": _project_detail(collection),
        "is_tenant_admin": role_probe.is_tenant_admin,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "corpus/detail.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _do_probe(
    request: Request,
    *,
    collection_key: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    operator: Operator,
) -> HTMLResponse:
    """Re-run the collection probe and return the refreshed card or an alert.

    Resolves the row tenant-first, then calls :func:`probe_collection` inside
    the route's open transaction (the ``get_session`` dependency owns commit /
    rollback). A :class:`CorpusUnavailable` -> 503 alert; the row's ``status``
    is left untouched because the service's write-back runs only after the
    probe returns (success-only) -- nothing was flushed on the failure path.
    A :class:`DocCollectionStateError` -> 409 alert (e.g. probing a disabled
    collection). On success the refreshed readiness card is returned for the
    ``#collection-readiness-card`` swap.
    """
    collection = await resolve_doc_collection(db_session, collection_key, operator.tenant_id)
    try:
        await probe_collection(db_session, operator, collection)
    except CorpusUnavailable as exc:
        _log.warning(
            "ui_doc_collection_probe_unavailable",
            collection_key=collection_key,
            tenant_id=str(operator.tenant_id),
            backend_status=exc.status,
        )
        return _render_probe_alert(
            request,
            title="Backend unavailable",
            message=(
                "The collection's backend is unreachable, unconfigured, or "
                "routes to no registered backend. The cached readiness is "
                "left unchanged; fix the backend and re-probe."
            ),
            status_code=503,
        )
    except DocCollectionStateError as exc:
        return _render_probe_alert(
            request,
            title="Probe not allowed in this state",
            message=(
                f"A probe cannot transition collection {collection_key!r} from "
                f"{exc.from_status!r} to {exc.to_status!r} "
                "(invalid_collection_transition). A disabled collection must be "
                "enabled before it can be probed."
            ),
            status_code=409,
        )
    _log.info(
        "ui_doc_collection_reprobe_success",
        collection_key=collection.collection_key,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        status=collection.status,
    )
    return _render_readiness_card(
        request,
        collection=collection,
        session_ctx=session_ctx,
        is_tenant_admin=True,
    )


def _render_disable_modal(
    request: Request,
    *,
    collection: Any,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the disable-confirm modal fragment + re-set the CSRF cookie.

    ``disable`` is availability-destructive, so the modal (mirroring the
    connectors delete-confirm) spells out the terminal-403 impact on every
    searcher before the operator confirms. Mints + re-sets the ``meho_csrf``
    cookie so the modal's own ``hx-headers`` echo lines up.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "collection_key": collection.collection_key,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "corpus/_disable_modal.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def _set_enabled(
    request: Request,
    *,
    collection_key: str,
    enabled: bool,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    operator: Operator,
) -> HTMLResponse:
    """Enable / disable the collection via the in-process service.

    Resolves the row tenant-first, calls :func:`set_collection_enabled`
    (idempotent no-op returns ``False``), and maps a forbidden transition to
    a legible 409 ``invalid_collection_transition`` alert rather than a 500.
    On success returns 204 + ``HX-Redirect`` to the detail page so HTMX
    re-renders the page with the new status pill + action buttons.
    """
    collection = await resolve_doc_collection(db_session, collection_key, operator.tenant_id)
    try:
        changed = await set_collection_enabled(db_session, collection, enabled=enabled)
    except DocCollectionStateError as exc:
        verb = "enable" if enabled else "disable"
        return _render_probe_alert(
            request,
            title=f"Cannot {verb} this collection",
            message=(
                f"Collection {collection_key!r} cannot be {verb}d from its "
                f"current state ({exc.from_status!r}) "
                "(invalid_collection_transition)."
            ),
            status_code=409,
        )
    _log.info(
        "ui_doc_collection_lifecycle_set",
        collection_key=collection.collection_key,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        enabled=enabled,
        changed=changed,
    )
    from urllib.parse import quote

    return HTMLResponse(
        status_code=204,
        headers={"HX-Redirect": f"/ui/corpus/collections/{quote(collection_key, safe='')}"},
    )


def build_corpus_collection_detail_router() -> APIRouter:
    """Construct the ``/ui/corpus/collections/{collection_key}*`` router.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without shared route state -- the corpus / connectors
    router convention. Registration order is **load-bearing**: the
    literal-suffixed action routes (``/{collection_key}/probe`` / ``/disable``
    / ``/enable``) register before the bare ``GET /{collection_key}`` detail
    route. Their extra literal segment already makes them unambiguous against
    the bare-param GET, but registering the bare detail GET last keeps the
    first-match-wins discipline explicit. The aggregating
    :func:`~meho_backplane.ui.routes.corpus.build_corpus_router` includes the
    T1 collections router (carrying the literal ``/register`` route) before
    this router so ``register`` never binds as a ``collection_key`` --
    mirrors :func:`meho_backplane.ui.routes.connectors.build_router`.
    """
    router = APIRouter(tags=["ui-corpus"])

    async def _probe_handler(
        request: Request,
        collection_key: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST /ui/corpus/collections/{collection_key}/probe``."""
        return await _do_probe(
            request,
            collection_key=collection_key,
            session_ctx=session_ctx,
            db_session=db_session,
            operator=operator,
        )

    async def _disable_modal_handler(
        request: Request,
        collection_key: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET /ui/corpus/collections/{collection_key}/disable`` -- confirm modal."""
        del operator  # gate only; the modal render needs no operator context.
        collection = await resolve_doc_collection(db_session, collection_key, session_ctx.tenant_id)
        return _render_disable_modal(request, collection=collection, session_ctx=session_ctx)

    async def _disable_handler(
        request: Request,
        collection_key: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST /ui/corpus/collections/{collection_key}/disable``."""
        return await _set_enabled(
            request,
            collection_key=collection_key,
            enabled=False,
            session_ctx=session_ctx,
            db_session=db_session,
            operator=operator,
        )

    async def _enable_handler(
        request: Request,
        collection_key: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST /ui/corpus/collections/{collection_key}/enable``."""
        return await _set_enabled(
            request,
            collection_key=collection_key,
            enabled=True,
            session_ctx=session_ctx,
            db_session=db_session,
            operator=operator,
        )

    async def _detail_handler(
        request: Request,
        collection_key: str,
        session_ctx: UISessionContext = _require_session_dep,
        db_session: AsyncSession = _get_session_dep,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """``GET /ui/corpus/collections/{collection_key}`` -- the detail page."""
        return await _render_detail(
            request,
            collection_key=collection_key,
            session_ctx=session_ctx,
            db_session=db_session,
            role_probe=role_probe,
        )

    # Literal-suffixed action routes FIRST -- their extra literal segment makes
    # them unambiguous against the bare ``{collection_key}`` GET, but we keep
    # the bare detail GET last so the first-match-wins discipline is explicit.
    router.add_api_route(
        "/ui/corpus/collections/{collection_key}/probe",
        _probe_handler,
        methods=["POST"],
        name="ui_corpus_collection_probe",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/corpus/collections/{collection_key}/disable",
        _disable_modal_handler,
        methods=["GET"],
        name="ui_corpus_collection_disable_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/corpus/collections/{collection_key}/disable",
        _disable_handler,
        methods=["POST"],
        name="ui_corpus_collection_disable",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/corpus/collections/{collection_key}/enable",
        _enable_handler,
        methods=["POST"],
        name="ui_corpus_collection_enable",
        response_class=HTMLResponse,
    )
    # Bare detail GET LAST -- after every literal-prefixed route.
    router.add_api_route(
        "/ui/corpus/collections/{collection_key}",
        _detail_handler,
        methods=["GET"],
        name="ui_corpus_collection_detail",
        response_class=HTMLResponse,
    )
    return router
