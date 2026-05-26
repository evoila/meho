# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Target create / edit forms (DaisyUI modal, HTMX) for the connectors UI.

Initiative #340 (G10.3 Connectors + Targets UI), Task #874 (T2) work
items #3 / #4. T1 (#873) shipped the read surface (list + detail +
re-probe); this module layers the **write** surface on top:

* **``GET /ui/connectors/create``** -- HTMX-loaded create modal. The
  product ``<select>`` is server-rendered from
  :func:`~meho_backplane.connectors.registry.registered_product_tokens`
  -- the same set the ``POST /api/v1/targets`` handler validates
  ``product`` against, so a product an operator can pick is always a
  product the create call will accept (no dropdown / validator drift).
* **``POST /ui/connectors/create``** -- submit handler. Builds a
  :class:`~meho_backplane.targets.schemas.TargetCreate` from the form
  fields and delegates to the REST handler
  :func:`~meho_backplane.api.v1.targets.create_target` in-process so
  the UI and the REST surface share one validation + product-check +
  audit-binding code path (the same posture T1's re-probe handler uses
  by sharing ``resolve_connector_or_label`` with the REST probe route).
  Success -> 204 + ``HX-Redirect: /ui/connectors`` (re-renders the
  list with the new row); a Pydantic ``ValidationError`` (port out of
  1-65535, empty name, ...) re-renders the modal with per-field error
  messages and HTTP 422.
* **``GET /ui/connectors/{name}/edit``** -- HTMX-loaded edit modal,
  fields pre-populated server-side from the resolved target.
* **``PATCH /ui/connectors/{name}``** -- submit handler. Builds a
  :class:`~meho_backplane.targets.schemas.TargetUpdate` and delegates
  to :func:`~meho_backplane.api.v1.targets.update_target`.

RBAC posture
------------

Create / edit are **tenant_admin only** and the gate is server-side:
every route here depends on
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`,
which lifts the full :class:`~meho_backplane.auth.operator.Operator`
from the BFF session, re-validates the access token through the
chassis JWT chain, and raises 403 for a non-admin caller. The list /
detail templates additionally hide the create / edit affordances from
non-admins (UX), but a crafted POST / PATCH still hits the 403 -- the
template hiding is not the security boundary.

The lifted ``Operator`` is passed into the in-process REST handler so
the create / update logic runs under the caller's tenant scope; the
handler's own ``Depends(require_role(TENANT_ADMIN))`` is not re-run on
the in-process call, which is why the 403 gate lives on *this*
module's route deps.

CSRF posture
------------

``POST`` / ``PATCH`` under ``/ui/`` are gated by the chassis
:class:`~meho_backplane.ui.csrf.CSRFMiddleware` (signed double-submit)
before the handler runs. The modal form inherits the ``X-CSRF-Token``
header from the page-level ``hx-headers`` directive; each modal render
also re-mints + re-sets the ``meho_csrf`` cookie so a freshly-issued
token lines up with the cookie.
"""

from __future__ import annotations

import structlog
from fastapi import Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.targets import create_target, update_target
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import registered_product_tokens
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "parse_aliases",
    "render_create_modal",
    "render_edit_modal",
    "submit_create",
    "submit_edit",
]

_log = structlog.get_logger(__name__)


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the chassis CSRF cookie posture for the modal renders.

    Same attributes T1's list / detail / probe renders set so the
    freshly-minted double-submit token lines up with the cookie the
    browser sends back on the form submit.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _product_options() -> list[str]:
    """Return the sorted product tokens the create dropdown offers.

    Sourced from
    :func:`~meho_backplane.connectors.registry.registered_product_tokens`
    -- the canonical set ``POST /api/v1/targets`` validates ``product``
    against. ``GET /api/v1/connectors`` surfaces the operator's
    *ingested* connectors, but the create-time validator keys on the
    registered-connector products; sourcing the dropdown from the same
    set the validator uses means a selectable product is always an
    acceptable product (the alternative -- driving the dropdown from
    the ingest list -- would let an operator pick a product the POST
    then rejects with a 422). Sorted for a stable render order.
    """
    return sorted(registered_product_tokens())


def parse_aliases(raw: str | None) -> list[str]:
    """Split the comma-separated ``aliases`` form field into a list.

    Empty / whitespace entries are dropped and duplicates de-duplicated
    while preserving first-seen order. Returns ``[]`` (never ``None``)
    when *raw* is missing so the :class:`TargetCreate` /
    :class:`TargetUpdate` builders never need a separate ``None``
    branch. Mirrors the comma-list shape the memory create modal uses
    for tags (:func:`~meho_backplane.ui.routes.memory._modal_shared.parse_tags`).
    """
    if not raw or not raw.strip():
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in raw.split(","):
        cleaned = part.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _validation_errors_by_field(exc: ValidationError) -> dict[str, str]:
    """Project a Pydantic :class:`ValidationError` into a field->message map.

    Keys are the first ``loc`` element (the field name); the value is
    the human-readable ``msg`` Pydantic produced (e.g. "Input should be
    less than or equal to 65535" for a port out of range, "String
    should have at least 1 character" for an empty name). The template
    renders the message under the matching field. A field with multiple
    errors keeps the first -- the form surfaces one message per field,
    which is the convention DaisyUI's ``input-error`` + label pattern
    expects.
    """
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        field = str(loc[0]) if loc else "__root__"
        errors.setdefault(field, str(err.get("msg", "invalid value")))
    return errors


async def render_create_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded create modal fragment.

    Called by ``GET /ui/connectors/create`` (tenant_admin-gated on the
    route dep). The product dropdown is server-rendered from the
    registered-connector product set; the auth_model dropdown from the
    :class:`~meho_backplane.connectors.schemas.AuthModel` enum.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "connectors/_create_modal.html",
        _build_form_context(csrf_token, mode="create"),
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_edit_modal(
    request: Request,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    *,
    target_name: str,
) -> HTMLResponse:
    """Render the edit modal pre-populated from the resolved target.

    Called by ``GET /ui/connectors/{name}/edit`` (tenant_admin-gated).
    Resolves the target tenant-scoped via
    :func:`~meho_backplane.targets.resolver.resolve_target` (404 on a
    cross-tenant or unknown name, so an admin in tenant B can never
    load tenant A's edit form). ``name`` and ``product`` render as
    read-only context for the operator (``name`` is immutable per the
    G0.3 contract; ``product`` is patchable but edited through the same
    dropdown the create form uses).
    """
    target = await resolve_target(db_session, session_ctx.tenant_id, target_name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context = _build_form_context(csrf_token, mode="edit")
    context["target"] = {
        "name": target.name,
        # The aliases input is a comma-separated free-text field (parsed
        # back to a list by ``parse_aliases`` on submit), so pre-populate
        # it as the joined string rather than the list repr.
        "aliases": ", ".join(target.aliases),
        "product": target.product,
        "host": target.host,
        # ``port`` is rendered into a numeric ``value=""`` attribute;
        # ``None`` must render as empty (not the literal "None"), so the
        # ``field_value`` macro's ``not in (None, "")`` guard already
        # drops it -- but stringify a present port so the macro's
        # value-echo path renders the digits.
        "port": str(target.port) if target.port is not None else None,
        "fqdn": target.fqdn,
        "secret_ref": target.secret_ref,
        "auth_model": target.auth_model,
        "vpn_required": target.vpn_required,
        "notes": target.notes,
    }
    response = get_templates().TemplateResponse(
        request,
        "connectors/_edit_modal.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _build_form_context(csrf_token: str, *, mode: str) -> dict[str, object]:
    """Build the template context shared by the create + edit modals."""
    return {
        "page_title": "Connectors",
        "active_surface": "connectors",
        "ready": False,
        "csrf_token": csrf_token,
        "mode": mode,
        "product_options": _product_options(),
        "auth_models": [m.value for m in AuthModel],
        # Bare-form re-render carries no errors / submitted values.
        "errors": {},
        "values": {},
    }


def _redirect_to_list() -> HTMLResponse:
    """Return a 204 + ``HX-Redirect`` so HTMX reloads the targets list.

    The canonical HTMX post-mutation navigation pattern
    (https://htmx.org/headers/hx-redirect/). The list re-renders with
    the freshly created / edited row -- satisfying the #874 acceptance
    criterion "success -> updated list". Mirrors the memory create
    handler's success shape.
    """
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/connectors"},
    )


async def submit_create(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    db_session: AsyncSession,
    *,
    name: str,
    product: str,
    host: str,
    port: str | None,
    auth_model: str,
    secret_ref: str | None,
    vpn_required: bool,
    aliases_raw: str | None,
    notes: str | None,
) -> HTMLResponse:
    """Build a :class:`TargetCreate` and delegate to the REST create handler.

    Form fields arrive as strings (``port`` is a free-text input so an
    empty submit is ``None`` rather than 0); we hand the raw shapes to
    :class:`TargetCreate` so Pydantic runs the *same* coercion +
    validation the REST POST body runs (port 1-65535, name min_length=1,
    auth_model enum membership). A :class:`ValidationError` re-renders
    the modal with per-field messages + HTTP 422; a valid model is
    passed straight to :func:`~meho_backplane.api.v1.targets.create_target`
    so the product-registry check, the duplicate-name 409, the audit
    binding, and the broadcast hook all fire exactly as they do on the
    REST surface.
    """
    raw_values = {
        "name": name,
        "product": product,
        "host": host,
        "port": port or "",
        "auth_model": auth_model,
        "secret_ref": secret_ref or "",
        "vpn_required": vpn_required,
        "aliases": aliases_raw or "",
        "notes": notes or "",
    }
    try:
        body = TargetCreate(
            name=name,
            product=product,
            host=host,
            port=_coerce_port(port),
            auth_model=AuthModel(auth_model),
            secret_ref=secret_ref or None,
            vpn_required=vpn_required,
            aliases=parse_aliases(aliases_raw),
            notes=notes or None,
        )
    except (ValidationError, ValueError) as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="create",
            exc=exc,
            values=raw_values,
        )

    await create_target(body=body, operator=operator, session=db_session)
    _log.info(
        "ui_target_create",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=body.name,
    )
    del request  # HX-Redirect needs no request context.
    return _redirect_to_list()


async def submit_edit(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    db_session: AsyncSession,
    *,
    target_name: str,
    product: str,
    host: str,
    port: str | None,
    auth_model: str,
    secret_ref: str | None,
    vpn_required: bool,
    aliases_raw: str | None,
    notes: str | None,
) -> HTMLResponse:
    """Build a :class:`TargetUpdate` and delegate to the REST update handler.

    The edit form posts every field every time (it's pre-populated, so
    the operator's view of "the target" is the full set), so the PATCH
    body carries the full editable surface rather than a sparse diff.
    ``name`` is not patchable (G0.3 contract: rename = delete + create)
    so it only addresses the row -- it is never in the
    :class:`TargetUpdate` body. A :class:`ValidationError` re-renders
    the edit modal with per-field messages; a valid model goes to
    :func:`~meho_backplane.api.v1.targets.update_target`, which runs the
    product-registry validation and the 404-on-cross-tenant resolve.
    """
    raw_values = {
        "name": target_name,
        "product": product,
        "host": host,
        "port": port or "",
        "auth_model": auth_model,
        "secret_ref": secret_ref or "",
        "vpn_required": vpn_required,
        "aliases": aliases_raw or "",
        "notes": notes or "",
    }
    try:
        body = TargetUpdate(
            product=product,
            host=host,
            port=_coerce_port(port),
            auth_model=AuthModel(auth_model),
            secret_ref=secret_ref or None,
            vpn_required=vpn_required,
            aliases=parse_aliases(aliases_raw),
            notes=notes or None,
        )
    except (ValidationError, ValueError) as exc:
        return _render_form_with_errors(
            request,
            csrf_session_id=str(session_ctx.session_id),
            mode="edit",
            exc=exc,
            values=raw_values,
        )

    await update_target(
        name=target_name,
        body=body,
        operator=operator,
        session=db_session,
    )
    _log.info(
        "ui_target_edit",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        name=target_name,
    )
    del request
    return _redirect_to_list()


def _coerce_port(raw: str | None) -> int | None:
    """Coerce the free-text ``port`` field to ``int | None``.

    An empty / whitespace input is ``None`` (port is optional). A
    non-empty value is parsed to ``int``; a non-numeric value raises
    :class:`ValueError`, which the submit handlers catch alongside
    :class:`ValidationError` so a typo'd port ("80a") surfaces as a
    field error rather than a 500. The 1-65535 range check is left to
    :class:`TargetCreate` / :class:`TargetUpdate` so the bound lives in
    one place (the schema) rather than being duplicated here.
    """
    if raw is None or not raw.strip():
        return None
    return int(raw.strip())


def _render_form_with_errors(
    request: Request,
    *,
    csrf_session_id: str,
    mode: str,
    exc: ValidationError | ValueError,
    values: dict[str, object],
) -> HTMLResponse:
    """Re-render the modal form fragment carrying per-field errors + 422.

    The HTMX form targets itself with ``hx-swap="outerHTML"`` so the
    422 response body (the re-rendered form) replaces the form in
    place; the operator keeps their typed values (echoed back via
    ``values``) and sees the field-level messages. 422 is the
    spec-correct status for "the submitted form failed validation" and
    matches the status the REST ``POST /api/v1/targets`` returns for the
    same Pydantic failure.
    """
    if isinstance(exc, ValidationError):
        errors = _validation_errors_by_field(exc)
    else:
        # A bare ValueError comes only from the port coercion; attribute
        # it to the port field so the message lands under the right input.
        errors = {"port": "port must be a whole number between 1 and 65535"}
    csrf_token = mint_csrf_token(csrf_session_id)
    context = _build_form_context(csrf_token, mode=mode)
    context["errors"] = errors
    context["values"] = values
    if mode == "edit":
        # The edit modal reads ``target.name`` for the form action URL;
        # echo it back from the submitted values so the re-render posts
        # to the same target.
        context["target"] = {"name": values.get("name", "")}
    template = (
        "connectors/_create_modal.html" if mode == "create" else "connectors/_edit_modal.html"
    )
    response = get_templates().TemplateResponse(
        request,
        template,
        context,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )
    _set_csrf_cookie(response, csrf_token)
    return response
