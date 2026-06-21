# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render + submit helpers for the connector **review drawer** (Task #1887).

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1887 (T3). After a spec is ingested (T2) it lands as LLM-grouped
operations the operator must govern per-op before the connector is
dispatchable. This module renders the review drawer (a per-group
``<details>`` accordion) and applies the per-op edits.

Split from :mod:`~meho_backplane.ui.routes.connectors.review_router`
(the ``forms.py`` / ``forms_router.py`` render-vs-routes convention) so
the render helpers stay unit-testable without a live FastAPI
:class:`Request` and neither module exceeds the chassis ~600-line cap.

Big-payload guard
-----------------

The vmware-rest review payload is thousands of ops, so the drawer shell
(:func:`render_review_drawer`) renders the groups as **collapsed**
``<details>`` carrying only ``name`` / ``review_status`` / ``op_count``
-- it does NOT render the per-op rows. Each group's body lazy-loads via
``hx-get=".../review/groups/{group_key}"`` on the ``<details>`` ``toggle``
event (:func:`render_group_body`), so a connector with thousands of ops
never renders every op on first paint.

In-process BFF
--------------

The per-op edit (:func:`submit_op_edit`) builds an
:class:`~meho_backplane.operations.ingest.api_schemas.EditOpBody` and
calls the shipped REST handler
:func:`~meho_backplane.api.v1.connectors_ingest.edit_op_endpoint`
**in-process** (the ``forms_router.py`` pattern) so the UI write and the
Bearer-API write share one validation + audit + warnings code path --
never an HTTP call back to ``/api/v1``. The read drawer calls
:func:`~meho_backplane.api.v1.connectors_ingest.get_review_endpoint` the
same way.

``op_id`` round-trip
--------------------

``connector_id`` (``<impl_id>-<version>``) and ``group_key`` (snake_case,
max 64) are slash-free, so the drawer + group-body routes use plain
string path params. ``op_id`` is the natural key ``f"{method}:{path}"``
(e.g. ``GET:/api/vcenter/cluster``) which CONTAINS slashes, so the per-op
PATCH route uses the ``{op_id:path}`` converter -- the only param here
that needs it -- and this module passes the captured ``op_id`` straight
through to ``edit_op_endpoint`` intact.

Governance footgun
------------------

A loosening edit (``is_enabled=true``, ``requires_approval=false``, or a
``safety_level`` relaxation) widens the boundary, so each loosening
control is confirm-gated via ``hx-confirm`` in the rendered row before it
fires. Tightening edits (disable, add approval, raise safety) apply
directly. The confirm decision is markup-only -- the server applies
whatever legal edit arrives (the REST handler is the authority).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.api.v1.connectors_ingest import (
    edit_op_endpoint,
    get_review_endpoint,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.ingest.api_schemas import EditOpBody
from meho_backplane.operations.ingest.payload import (
    ConnectorReviewGroup,
    ConnectorReviewOp,
    ConnectorReviewPayload,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "CONNECTOR_ID_MAX",
    "GROUP_KEY_MAX",
    "OP_ID_MAX",
    "render_group_body",
    "render_op_row",
    "render_review_drawer",
    "submit_op_edit",
]

_log = structlog.get_logger(__name__)

#: ``connector_id`` is ``<impl_id>-<version>``; both halves are bounded in
#: the schema, so a longer path segment cannot name a real row -- reject it
#: cheaply with the same 404 the resolver would raise after a round trip
#: (the registry-actions idiom against fuzzer spam).
CONNECTOR_ID_MAX = 256

#: ``group_key`` is snake_case, schema-capped at 64 chars.
GROUP_KEY_MAX = 64

#: ``op_id`` is ``f"{method}:{path}"``; an HTTP path can be long, but a
#: segment past this is not a real op -- bound it before any DB work.
OP_ID_MAX = 2048


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    Value MUST equal the token the rendered markup echoes via
    ``hx-headers`` or the CSRF middleware rejects the next per-op edit
    submit. Same SameSite=Strict + Secure + non-HttpOnly posture every UI
    surface's CSRF cookie carries (HTMX must read it to populate
    ``X-CSRF-Token``). Re-minted on every fragment render so a long-lived
    drawer never drifts from the cookie.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _validate_connector_id(connector_id: str) -> None:
    """Reject an over-long ``connector_id`` segment with 404 before any work."""
    if not connector_id or len(connector_id) > CONNECTOR_ID_MAX:
        raise HTTPException(status_code=404, detail=f"connector {connector_id!r} not found")


def _validate_group_key(group_key: str) -> None:
    """Reject an over-long ``group_key`` segment with 404 before any work."""
    if not group_key or len(group_key) > GROUP_KEY_MAX:
        raise HTTPException(status_code=404, detail=f"group {group_key!r} not found")


def _validate_op_id(op_id: str) -> None:
    """Reject an over-long ``op_id`` segment with 404 before any work."""
    if not op_id or len(op_id) > OP_ID_MAX:
        raise HTTPException(status_code=404, detail=f"operation {op_id!r} not found")


def _group_summary_context(group: ConnectorReviewGroup) -> dict[str, Any]:
    """Project a group into the collapsed-accordion summary dict.

    Deliberately omits ``ops`` -- the shell must not carry the per-op rows
    (the big-payload guard). The body lazy-loads via the group-body route.
    """
    return {
        "group_key": group.group_key,
        "name": group.name,
        "when_to_use": group.when_to_use,
        "review_status": group.review_status,
        "op_count": group.op_count,
    }


def _op_context(op: ConnectorReviewOp) -> dict[str, Any]:
    """Project one :class:`ConnectorReviewOp` into the row template dict."""
    return {
        "op_id": op.op_id,
        "summary": op.summary,
        "description": op.description,
        "custom_description": op.custom_description,
        "safety_level": op.safety_level,
        "requires_approval": op.requires_approval,
        "is_enabled": op.is_enabled,
        "tags": op.tags,
    }


def _render_error_panel(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
    candidates: list[dict[str, Any]] | None = None,
) -> HTMLResponse:
    """Render the shared inline error panel (the T1 ``_registry_error.html``).

    Reused verbatim so the drawer's ``409 connector_scope_ambiguous``
    (candidate list), ``404``, and ``400`` faults surface as actionable
    panels rather than a 5xx -- the renderer is shape-generic.
    """
    return get_templates().TemplateResponse(
        request,
        "connectors/_registry_error.html",
        {
            "title": title,
            "message": message,
            "candidates": candidates or [],
        },
        status_code=status_code,
    )


def _panel_from_http_exception(
    request: Request,
    exc: HTTPException,
    *,
    connector_id: str,
) -> HTMLResponse:
    """Map an in-process REST :class:`HTTPException` to an inline panel.

    Branches on the structured ``detail`` shape the shipped handlers
    raise: ``get_review_endpoint`` raises ``409`` with a
    ``connector_scope_ambiguous`` dict carrying ``candidates`` (a label
    mapping to both a tenant + a built-in row), or ``404`` with a plain
    string; ``edit_op_endpoint`` raises ``404`` (unknown op / connector)
    or ``400`` (empty body / out-of-enum) with a plain string. Anything
    else re-raises unchanged so an unexpected fault still surfaces as a
    real error rather than a mislabelled panel.
    """
    detail = exc.detail
    if (
        exc.status_code == 409
        and isinstance(detail, dict)
        and detail.get("detail") == "connector_scope_ambiguous"
    ):
        candidates = detail.get("candidates")
        return _render_error_panel(
            request,
            title="Connector scope is ambiguous",
            message=(
                f"{connector_id!r} resolves to both a tenant-curated row and a "
                "built-in row, so the review cannot pick one without guessing. "
                "Disambiguate via the MCP sibling (the built-in / global scope is "
                "tenant_admin-only and not curatable from this console)."
            ),
            status_code=409,
            candidates=candidates if isinstance(candidates, list) else [],
        )
    if exc.status_code == 404:
        return _render_error_panel(
            request,
            title="Not found",
            message=(
                f"{detail or connector_id!r} is unknown, belongs to another "
                "tenant, or was already deleted. Refresh the registry list."
            ),
            status_code=404,
        )
    if exc.status_code == 400:
        return _render_error_panel(
            request,
            title="Edit rejected",
            message=str(detail),
            status_code=400,
        )
    raise exc


async def render_review_drawer(
    request: Request,
    *,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the review drawer shell: a collapsed per-group accordion.

    Calls :func:`get_review_endpoint` in-process (OPERATOR read) and
    renders the groups as collapsed ``<details>`` showing ``name`` /
    ``review_status`` / ``op_count`` ONLY -- the per-op rows are NOT
    rendered here (the big-payload guard). Each group body lazy-loads via
    ``hx-get`` on ``<details>`` open. A ``409 connector_scope_ambiguous``
    or ``404`` renders an inline panel instead of a 5xx. ``is_tenant_admin``
    drives the edit-control soft-hide (operators see the read drawer with
    no edit affordances).

    The payload's ``kind`` / ``dispatchable`` (#1979 / #1971) ride into
    the context so the drawer header badges a profiled-but-staged
    connector distinctly (#1980) -- display-only, no governance logic.
    """
    _validate_connector_id(connector_id)
    try:
        payload: ConnectorReviewPayload = await get_review_endpoint(
            connector_id=connector_id,
            operator=operator,
        )
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "connector_id": payload.connector_id,
        "product": payload.product,
        "version": payload.version,
        "impl_id": payload.impl_id,
        "total_op_count": payload.total_op_count,
        "groups": [_group_summary_context(group) for group in payload.groups],
        "kind": payload.kind,
        "dispatchable": payload.dispatchable,
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "connectors/_review_drawer.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


async def render_group_body(
    request: Request,
    *,
    connector_id: str,
    group_key: str,
    session_ctx: UISessionContext,
    operator: Operator,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the per-op rows for ONE group (the lazy-loaded body).

    Re-reads the full payload via :func:`get_review_endpoint` in-process
    and renders only the matched group's ops -- the route the drawer's
    ``hx-get`` targets on ``<details>`` open. An unknown ``group_key``
    (not in the payload) renders an inline 404 panel. Each op row carries
    the inline edit controls (soft-hidden for non-admins via
    ``is_tenant_admin``).
    """
    _validate_connector_id(connector_id)
    _validate_group_key(group_key)
    try:
        payload = await get_review_endpoint(connector_id=connector_id, operator=operator)
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)

    group = next((g for g in payload.groups if g.group_key == group_key), None)
    if group is None:
        return _render_error_panel(
            request,
            title="Group not found",
            message=(
                f"{group_key!r} is not a group of {connector_id!r}. The connector "
                "may have been re-ingested; refresh the drawer."
            ),
            status_code=404,
        )

    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "connector_id": payload.connector_id,
        "group_key": group.group_key,
        "ops": [_op_context(op) for op in group.ops],
        "is_tenant_admin": is_tenant_admin,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(
        request,
        "connectors/_review_group_body.html",
        context,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def render_op_row(
    request: Request,
    *,
    connector_id: str,
    op: ConnectorReviewOp,
    is_tenant_admin: bool,
    csrf_token: str,
    warnings: list[dict[str, Any]] | None = None,
) -> HTMLResponse:
    """Render a single op row (the ``hx-swap="outerHTML"`` unit).

    Used by the per-op edit submit to re-render just the edited row.
    ``warnings`` carries the ``EditOpResponse.warnings`` advisories (the
    ``unreplaced_auto_shim`` enable-time advisory) so the row surfaces the
    dispatch dead end inline -- the edit still landed (warnings never
    block), the operator just learns the consequence here.
    """
    response = get_templates().TemplateResponse(
        request,
        "connectors/_review_op_row.html",
        {
            "connector_id": connector_id,
            "op": _op_context(op),
            "is_tenant_admin": is_tenant_admin,
            "csrf_token": csrf_token,
            "warnings": warnings or [],
        },
    )
    return response


def _coerce_safety_level(value: str | None) -> Any:
    """Map an empty / unchanged form value to ``None`` (no edit to this field).

    The row's ``safety_level`` ``<select>`` always submits a value; the
    other fields are independent. An empty string means "leave it" so it
    does not clobber the stored level via an out-of-enum 400. A real value
    flows to ``EditOpBody`` where Pydantic's ``Literal`` rejects anything
    outside the enum (surfaced as the friendly 400 panel).
    """
    return value or None


def _coerce_custom_description(value: str | None) -> str | None:
    """Map an empty ``custom_description`` form value to ``None``.

    ``EditOpBody.custom_description`` is ``min_length=1``; an empty string
    would 422 at the schema boundary, so an unset field flows as ``None``
    (no edit to the column) rather than an empty write.
    """
    if value is None:
        return None
    return value or None


async def submit_op_edit(
    request: Request,
    *,
    connector_id: str,
    op_id: str,
    custom_description: str | None,
    safety_level: str | None,
    requires_approval: bool | None,
    is_enabled: bool | None,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Apply one per-op edit, then re-render just that op row.

    Builds an :class:`EditOpBody` from the submitted controls and calls
    :func:`edit_op_endpoint` in-process (TENANT_ADMIN -- the route gates
    it). The ``op_id`` arrives intact from the ``{op_id:path}`` route (a
    key like ``GET:/api/vcenter/cluster`` round-trips without URL-encoding
    breakage) and is passed straight through.

    On success the response's ``warnings`` (the ``unreplaced_auto_shim``
    enable-time advisory) ride into the re-rendered row -- the edit landed,
    the advisory is informational. A ``404`` (unknown op / connector),
    ``409`` (ambiguous scope), or ``400`` (empty body / out-of-enum)
    renders an inline panel rather than a 5xx.

    An empty body (no field submitted with a value) is rejected by the
    REST handler's own ``ValueError`` -> 400; we surface that as the
    "Edit rejected" panel rather than pre-empting it, so the UI and REST
    surfaces share one "at least one field" rule.
    """
    _validate_connector_id(connector_id)
    _validate_op_id(op_id)
    try:
        body = _build_edit_body(
            custom_description=custom_description,
            safety_level=safety_level,
            requires_approval=requires_approval,
            is_enabled=is_enabled,
        )
    except ValueError as exc:
        # An out-of-enum safety_level (Pydantic ``Literal`` violation)
        # surfaces as the friendly 400 panel, not a framework 422.
        return _render_error_panel(
            request,
            title="Edit rejected",
            message=f"The edit to {op_id!r} is invalid: {exc}.",
            status_code=400,
        )

    try:
        result = await edit_op_endpoint(
            connector_id=connector_id,
            op_id=op_id,
            body=body,
            operator=operator,
        )
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)

    _log.info(
        "ui_connector_review_op_edited",
        connector_id=connector_id,
        op_id=op_id,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
        warning_count=len(result.warnings),
    )
    return await _render_edited_row(
        request,
        connector_id=connector_id,
        op_id=op_id,
        warnings=[warning.model_dump() for warning in result.warnings],
        session_ctx=session_ctx,
        operator=operator,
    )


def _build_edit_body(
    *,
    custom_description: str | None,
    safety_level: str | None,
    requires_approval: bool | None,
    is_enabled: bool | None,
) -> EditOpBody:
    """Build an :class:`EditOpBody` from the submitted controls.

    Coerces empty form values to ``None`` (no edit to that field) so an
    unset control never clobbers the stored value via an empty write /
    out-of-enum. Raises :class:`ValueError` on an out-of-enum
    ``safety_level`` (Pydantic ``Literal``), which the caller maps to the
    friendly 400 panel.
    """
    return EditOpBody(
        custom_description=_coerce_custom_description(custom_description),
        safety_level=_coerce_safety_level(safety_level),
        requires_approval=requires_approval,
        is_enabled=is_enabled,
    )


async def _render_edited_row(
    request: Request,
    *,
    connector_id: str,
    op_id: str,
    warnings: list[dict[str, Any]],
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Re-read the edited op + render its row (the ``hx-swap`` unit).

    The REST handler returns only the warnings envelope, not the row, so
    re-read the payload to reflect the persisted state. The op is in
    exactly one group; a miss (re-ingest / delete race between the write
    and the re-read) renders a 404 panel rather than a blank row.
    """
    payload = await get_review_endpoint(connector_id=connector_id, operator=operator)
    edited = _find_op(payload, op_id)
    if edited is None:
        return _render_error_panel(
            request,
            title="Operation not found",
            message=(
                f"{op_id!r} was edited but is no longer in {connector_id!r}. Refresh the drawer."
            ),
            status_code=404,
        )
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = render_op_row(
        request,
        connector_id=connector_id,
        op=edited,
        is_tenant_admin=True,
        csrf_token=csrf_token,
        warnings=warnings,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


def _find_op(payload: ConnectorReviewPayload, op_id: str) -> ConnectorReviewOp | None:
    """Return the op matching *op_id* across every group, or ``None``."""
    for group in payload.groups:
        for op in group.ops:
            if op.op_id == op_id:
                return op
    return None
