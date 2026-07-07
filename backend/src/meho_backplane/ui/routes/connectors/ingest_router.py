# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route registration for the connector-ingest modal + job-poll (Task #1886).

Thin wrappers that parse FastAPI params + resolve the TENANT_ADMIN-gated
:class:`~meho_backplane.auth.operator.Operator` dependency, then hand off
to the render / submit / poll helpers in
:mod:`~meho_backplane.ui.routes.connectors.ingest_modal`. Split from the
render logic (the ``forms.py`` / ``forms_router.py`` precedent) so neither
module exceeds the chassis-wide ~600-line cap and the helpers stay
unit-testable without a FastAPI :class:`Request` fixture.

Route inventory (all TENANT_ADMIN-gated server-side via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`,
mirroring the REST ``_require_admin`` gate on ``ingest_endpoint`` /
``get_ingest_job_endpoint``):

* ``GET  /ui/connectors/registry/ingest``                 -- the ingest modal.
* ``POST /ui/connectors/registry/ingest``                 -- the submit handler.
* ``GET  /ui/connectors/registry/ingest/jobs/{job_id}``   -- the async job poll.

Registration order is **load-bearing**: the three literal
``/ui/connectors/registry/ingest`` routes are mounted on their own router
and the umbrella :func:`~meho_backplane.ui.routes.connectors.build_router`
includes this router **before** the registry-actions router (so
``"ingest"`` is never captured by ``/ui/connectors/registry/{connector_id}``)
AND before the detail router (so it is never captured by
``GET /ui/connectors/{name}``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.ingest_modal import (
    CATALOG_ENTRY_MAX,
    IMPL_ID_MAX,
    PRODUCT_MAX,
    VERSION_MAX,
    poll_job,
    render_modal,
    submit_ingest,
)
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403

__all__ = ["build_ingest_router"]

#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom.
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_operator_or_403)


def build_ingest_router() -> APIRouter:
    """Construct the ingest modal + job-poll :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    build parallel routers without shared route state -- the list /
    detail / corpus / registry-actions router convention. The three
    literal ``/ui/connectors/registry/ingest`` routes must register
    before the ``/ui/connectors/{name}`` detail catch-all AND before the
    ``/ui/connectors/registry/{connector_id}/...`` param routes
    (first-match-wins) so ``ingest`` is never captured as a target
    ``name`` or a ``connector_id``; the include order in
    :func:`~meho_backplane.ui.routes.connectors.build_router` enforces
    both.
    """
    router = APIRouter(tags=["ui-connectors"])

    async def _modal_handler(
        request: Request,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET /ui/connectors/registry/ingest`` -- the ingest modal fragment."""
        return await render_modal(request, session_ctx=session_ctx, operator=operator)

    async def _submit_handler(
        request: Request,
        # ``Form(default="")`` (not ``Form(...)``) so an empty / omitted
        # field flows to the handler's friendly pre-check rather than
        # tripping FastAPI's own raw-body 422 -- the modal owns the
        # validation, not the framework boundary.
        mode: str = Form(default="catalog"),
        catalog_entry: str = Form(default="", max_length=CATALOG_ENTRY_MAX),
        product: str = Form(default="", max_length=PRODUCT_MAX),
        version: str = Form(default="", max_length=VERSION_MAX),
        impl_id: str = Form(default="", max_length=IMPL_ID_MAX),
        spec_uri: list[str] = Form(default_factory=list),
        dry_run: bool = Form(default=False),
        # Write scope (#2209): ``global`` (the omit-equals-global REST
        # default per #2085) or ``tenant`` (the operator's own tenant,
        # resolved server-side -- the form never posts a raw UUID).
        scope: str = Form(default="global", max_length=16),
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST /ui/connectors/registry/ingest`` -- build one shape + ingest."""
        return await submit_ingest(
            request,
            mode=mode,
            catalog_entry=catalog_entry,
            product=product,
            version=version,
            impl_id=impl_id,
            spec_uris=spec_uri,
            dry_run=dry_run,
            scope=scope,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _poll_handler(
        request: Request,
        job_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET /ui/connectors/registry/ingest/jobs/{job_id}`` -- poll a job."""
        return await poll_job(
            request,
            job_id=job_id,
            operator=operator,
            session_ctx=session_ctx,
        )

    router.add_api_route(
        "/ui/connectors/registry/ingest",
        _modal_handler,
        methods=["GET"],
        name="ui_connectors_registry_ingest_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/ingest",
        _submit_handler,
        methods=["POST"],
        name="ui_connectors_registry_ingest_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/ingest/jobs/{job_id}",
        _poll_handler,
        methods=["GET"],
        name="ui_connectors_registry_ingest_job",
        response_class=HTMLResponse,
    )
    return router
