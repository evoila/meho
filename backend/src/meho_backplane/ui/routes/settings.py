# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings file page of the web console: upload, preview, apply.

* ``GET  /ui/settings``         -- the page with the upload form.
* ``POST /ui/settings/preview`` -- what the file would change, and every
  mistake with its reason. Writes nothing.
* ``POST /ui/settings/apply``   -- applies the file that the preview showed.

Both POST routes call the same code as ``POST /api/v1/settings/import``:
:func:`~meho_backplane.instance_settings.service.build_plan` for the preview
and :func:`~meho_backplane.instance_settings.service.apply_import` for the
apply. The caller is the console operator, so ``web_session`` is true. That
makes the changes that apply only for a person in the web console (targets,
``run_approval_target_kinds`` turned on, ``announce_gate_enabled`` turned
off) apply here; a call with a token on the REST route still gets 403.

Who may use the page: a person (403 ``human_principal_required`` for an
agent, service or runner login) with ``tenant_admin`` or ``platform_admin``
(403 ``insufficient_role`` for anyone else). A ``tenant_admin`` applies a file
for their own tenant only; ``build_plan`` refuses a file that names another
tenant. The CSRF check of the console runs on both POST routes.

The upload is multipart: the CSRF middleware caps a url-encoded body at
256 KiB, and a settings file may be 4 MiB. The preview keeps no state: its
apply form carries the exact file bytes as base64, and the apply checks the
file again before it writes.

Audit: the apply binds the ``settings.import`` audit op id, the same as the
REST route. The chassis audit middleware writes the row before the answer
leaves; when that write fails, the answer is a 500, never the success page.
The preview is marked as a preview, so it writes no execution row.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from meho_backplane.audit_cutover import note_preview
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_session
from meho_backplane.instance_settings.console_only import CONSOLE_ONLY_POLICIES, SETTINGS_PAGE
from meho_backplane.instance_settings.diff import SECTIONS
from meho_backplane.instance_settings.schema import MAX_FILE_BYTES
from meho_backplane.instance_settings.service import (
    ImportPlan,
    SettingsForbiddenError,
    apply_import,
    build_plan,
)
from meho_backplane.ui.auth.human import ensure_ui_human
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import lift_operator_from_session
from meho_backplane.ui.templating import get_templates

__all__ = ["SETTINGS_PATH", "build_settings_router", "diff_rows", "require_ui_settings_admin"]

_log = structlog.get_logger(__name__)

SETTINGS_PATH: Final[str] = SETTINGS_PAGE

_OP_ID_PREVIEW: Final[str] = "settings.import.preview"
_OP_ID_IMPORT: Final[str] = "settings.import"

#: The apply form's base64 field holds a 4 MiB file, plus a third for base64.
_MAX_FORM_PART_BYTES: Final[int] = MAX_FILE_BYTES * 4 // 3 + 1024

_NO_FILE: Final[str] = "Choose a settings file first."

_SECTION_TITLES: Final[dict[str, str]] = {
    "tenants": "Tenant",
    "policies": "Policy",
    "conventions": "Convention",
    "targets": "Target",
}

_require_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)


async def require_ui_settings_admin(
    session_ctx: UISessionContext = _require_session_dep,
) -> Operator:
    """A person with ``tenant_admin`` or ``platform_admin``, else 403.

    The person check runs first, as on the REST import, so an agent,
    service or runner login learns the real reason.
    """
    operator = await lift_operator_from_session(session_ctx)
    ensure_ui_human(operator)
    if operator.platform_admin or operator.tenant_role is TenantRole.TENANT_ADMIN:
        return operator
    _log.info(
        "ui_settings_insufficient_role",
        operator_sub=operator.sub,
        tenant_role=operator.tenant_role.value,
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_role")


_settings_admin_dep = Depends(require_ui_settings_admin)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "), sort_keys=True)


def diff_rows(diff: dict[str, Any]) -> list[dict[str, str]]:
    """One row for each added, changed or "not in file" entry, in section order.

    Every entry of the diff becomes a row; nothing is cut.
    """
    rows: list[dict[str, str]] = []
    for section in SECTIONS:
        part = diff.get(section) or {}
        for kind, field in (
            ("added", "added"),
            ("changed", "changed"),
            ("not in file", "not_in_file"),
        ):
            for item in part.get(field) or []:
                if kind == "changed":
                    detail = f"{_compact(item.get('before'))} -> {_compact(item.get('after'))}"
                elif "value" in item:
                    detail = _compact(item["value"])
                else:
                    detail = ""
                rows.append(
                    {
                        "section": _SECTION_TITLES[section],
                        "kind": kind,
                        "tenant": str(item.get("tenant", "")),
                        "key": str(item.get("key", "")),
                        "detail": detail,
                    }
                )
    return rows


def _console_only_notes(paths: list[str]) -> list[str]:
    """One sentence for each change that only a person in the web console may make."""
    notes = []
    for path in paths:
        what = CONSOLE_ONLY_POLICIES[path.rsplit(".", 1)[1]][1]
        notes.append(
            f"`{path}` {what}. MEHO accepts this change only from a person in the web console."
        )
    return notes


def _render(
    request: Request,
    session_ctx: UISessionContext,
    template: str,
    context: dict[str, Any],
) -> HTMLResponse:
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request, template, {"csrf_token": csrf_token, **context}
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def _refused(request: Request, session_ctx: UISessionContext, reason: str) -> HTMLResponse:
    """A refusal as an HTML fragment with status 200, so htmx shows it on the page."""
    return _render(request, session_ctx, "settings/_refused.html", {"reason": reason})


async def _read_file(request: Request) -> bytes | None:
    """The file of the upload field, or of the apply form's base64 field.

    ``None`` when the form holds neither. One byte more than the limit is
    read, so the file check gives its "larger than" sentence.
    """
    form = await request.form(max_files=1, max_fields=4, max_part_size=_MAX_FORM_PART_BYTES)
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        content = await upload.read(MAX_FILE_BYTES + 1)
        return content or None
    encoded = form.get("file_b64")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True) or None
    except binascii.Error:
        return None


def _preview_context(plan: ImportPlan, content: bytes) -> dict[str, Any]:
    rows = diff_rows(plan.diff) if plan.diff else []
    return {
        "mistakes": plan.mistakes,
        "warnings": plan.warnings,
        "console_only": _console_only_notes(plan.console_only),
        "rows": rows,
        "has_changes": any(row["kind"] != "not in file" for row in rows),
        "file_b64": base64.b64encode(content).decode("ascii"),
    }


async def _page_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _settings_admin_dep,
) -> HTMLResponse:
    """``GET /ui/settings``: the upload form."""
    return _render(
        request,
        session_ctx,
        "settings/index.html",
        {
            "active_surface": "settings",
            "is_platform_admin": operator.platform_admin,
        },
    )


async def _preview_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _settings_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/settings/preview``: the mistakes and the diff. Writes nothing."""
    structlog.contextvars.bind_contextvars(audit_op_id=_OP_ID_PREVIEW, audit_op_class="read")
    note_preview()
    content = await _read_file(request)
    if content is None:
        return _refused(request, session_ctx, _NO_FILE)
    try:
        plan = await build_plan(db_session, operator, content)
    except SettingsForbiddenError as exc:
        return _refused(request, session_ctx, str(exc))
    return _render(request, session_ctx, "settings/_preview.html", _preview_context(plan, content))


async def _apply_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _settings_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/settings/apply``: checks the file again, then adds or updates."""
    # "read" until the service is about to write (it binds "write" there), so
    # a refused or unchanged file leaves no write in the audit log.
    structlog.contextvars.bind_contextvars(audit_op_id=_OP_ID_IMPORT, audit_op_class="read")
    content = await _read_file(request)
    if content is None:
        return _refused(request, session_ctx, _NO_FILE)
    try:
        result = await apply_import(db_session, operator, content)
    except SettingsForbiddenError as exc:
        return _refused(request, session_ctx, str(exc))
    if result.plan.mistakes:
        return _render(
            request, session_ctx, "settings/_preview.html", _preview_context(result.plan, content)
        )
    return _render(
        request,
        session_ctx,
        "settings/_result.html",
        {
            "applied": result.applied,
            "revision_id": result.revision_id,
            "failed": result.failed,
            "warnings": result.plan.warnings,
            "rows": diff_rows(result.diff) if result.diff else [],
        },
    )


def build_settings_router() -> APIRouter:
    """Construct the settings file :class:`APIRouter`."""
    router = APIRouter(tags=["ui-settings"])
    router.add_api_route(
        SETTINGS_PATH,
        _page_handler,
        methods=["GET"],
        name="ui_settings",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        f"{SETTINGS_PATH}/preview",
        _preview_handler,
        methods=["POST"],
        name="ui_settings_preview",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        f"{SETTINGS_PATH}/apply",
        _apply_handler,
        methods=["POST"],
        name="ui_settings_apply",
        response_class=HTMLResponse,
    )
    return router
