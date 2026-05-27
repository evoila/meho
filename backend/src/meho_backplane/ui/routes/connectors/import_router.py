# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route registration for the bulk ``targets.yaml`` import UI (Task #875).

Thin FastAPI wrappers that parse the multipart form (paste text + an
optional uploaded file), resolve the tenant_admin-gated
:class:`~meho_backplane.auth.operator.Operator`, and hand off to the
render / submit helpers in
:mod:`~meho_backplane.ui.routes.connectors.import_view`. Split from the
parse / classify logic so neither module exceeds the chassis ~600-line
cap and the mapping helpers stay unit-testable without a FastAPI
:class:`Request` fixture.

Route inventory (all tenant_admin-gated server-side via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`):

* ``GET  /ui/connectors/import``         -- the full import page.
* ``POST /ui/connectors/import``         -- parse + render the preview.
* ``POST /ui/connectors/import/confirm`` -- apply the plan in-process.

Registration order is **load-bearing**: every path here is a literal
prefix (``/ui/connectors/import`` ...) that must register before the
parametrised ``GET /ui/connectors/{name}`` detail route, otherwise the
literal ``"import"`` token is captured as a target ``name`` by the
detail handler. The umbrella
:func:`~meho_backplane.ui.routes.connectors.build_router` includes this
router before the detail router for that reason.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_session
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.import_view import (
    render_import_page,
    render_preview,
    submit_confirm,
)
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403

__all__ = ["build_import_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom matching
#: the chassis list / detail / forms routers.
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_operator_or_403)
_get_session_dep = Depends(get_session)

#: Upper bound on the YAML payload the parse path accepts. The CSRF
#: middleware's 256 KiB body cap only runs for
#: ``application/x-www-form-urlencoded`` requests; a multipart upload is
#: instead bounded by Starlette's ``MultiPartParser.max_part_size``
#: (1 MiB default, 422 on exceed). This tighter app-level cap rejects an
#: oversized paste or upload before the parse allocates, matching the
#: form-field caps the T2 create / edit router sets.
_YAML_MAX_BYTES = 256 * 1024


async def _resolve_yaml_text(pasted: str | None, upload: UploadFile | None) -> str:
    """Resolve the submitted YAML to a single string.

    An uploaded file takes precedence over a non-empty paste (the file
    is the more deliberate gesture). The bytes are decoded as UTF-8
    with ``errors="replace"`` so a stray non-UTF-8 byte surfaces as a
    YAML parse error in the preview rather than a 500; the parse path
    owns the operator-facing error message.
    """
    if upload is not None and upload.filename:
        raw = await upload.read()
        return raw.decode("utf-8", errors="replace")
    return pasted or ""


async def _import_page_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/connectors/import`` -- the full import page."""
    del operator  # gate only; the page render needs no operator context.
    return await render_import_page(request, session_ctx)


async def _import_preview_handler(
    request: Request,
    pasted: str | None = Form(default=None, max_length=_YAML_MAX_BYTES),
    upload: UploadFile | None = File(default=None),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/connectors/import`` -- parse + render the preview table.

    ``operator`` is resolved purely as the tenant_admin gate; the
    preview is read-only (it classifies but does not write), so the
    existing-name lookup runs under ``session_ctx.tenant_id``.
    """
    del operator  # gate only; preview is read-only.
    yaml_text = await _resolve_yaml_text(pasted, upload)
    return await render_preview(request, session_ctx, db_session, yaml_text=yaml_text)


async def _import_confirm_handler(
    request: Request,
    pasted: str | None = Form(default=None, max_length=_YAML_MAX_BYTES),
    upload: UploadFile | None = File(default=None),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/connectors/import/confirm`` -- apply the plan in-process."""
    yaml_text = await _resolve_yaml_text(pasted, upload)
    return await submit_confirm(request, session_ctx, operator, db_session, yaml_text=yaml_text)


def build_import_router() -> APIRouter:
    """Construct the bulk-import :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the list / detail / probe / forms router convention. All paths are
    literal prefixes that must register before the parametrised detail
    route (handled by the include order in
    :func:`~meho_backplane.ui.routes.connectors.build_router`).
    """
    router = APIRouter(tags=["ui-connectors"])
    router.add_api_route(
        "/ui/connectors/import",
        _import_page_handler,
        methods=["GET"],
        name="ui_connectors_import_page",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/import",
        _import_preview_handler,
        methods=["POST"],
        name="ui_connectors_import_preview",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/import/confirm",
        _import_confirm_handler,
        methods=["POST"],
        name="ui_connectors_import_confirm",
        response_class=HTMLResponse,
    )
    return router
