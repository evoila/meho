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

The two policy changes need a reason, by the reason step of turning approval
off for an operation (:mod:`meho_backplane.ui.reason`): the apply form has a
reason field, blanks around the reason are removed, and an empty reason is no
reason. Without one the apply is refused and writes nothing. The audit row of
the apply holds the reason under ``reason``. The other direction (gate on,
target kinds off) and every other change need no reason.

Who may use the page: a person (403 ``human_principal_required`` for an
agent, service or runner login) with ``tenant_admin`` or ``platform_admin``
(403 ``insufficient_role`` for anyone else). A ``tenant_admin`` applies a file
for their own tenant only; ``build_plan`` refuses a file that names another
tenant. The CSRF check of the console runs on both POST routes.

The upload is multipart: the CSRF middleware caps a url-encoded body at
256 KiB, and a settings file may be 4 MiB. The preview keeps no state: its
apply form carries the exact file bytes as base64, and the apply checks the
file again before it writes.

A refusal (another tenant, a file this caller may not apply, or a missing
reason) is a 403
with the reason as an HTML fragment. The page script
(``static/src/app/settings-file.js``) swaps every error answer of these two
forms into the page, and shows a fixed error text for an answer that is not
HTML.

Audit: the apply binds the ``settings.import`` audit op id, the same as the
REST route. The apply owns its transaction:
:func:`~meho_backplane.audit.commit_with_request_audit_row` adds the
request's one audit row to the same transaction as the settings and the
revision, and commits all of them together. The row is the one the audit
middleware would write (``legacy_request``, or ``operation_execution``
with ``AUDIT_WRAPPER_ROWS_RETIRED=true``), with the target, the login
details and the duration; the middleware then writes no row of its own.
When any step of the apply fails (a read, a write, the revision or the
commit), MEHO rolls everything back, keeps only the audit keys that name the
attempt (the operation, the file hash and the tenants), and answers 500
with an error text; the middleware records that failed request. The
preview is marked as a preview, so it writes no execution row; a refused
preview leaves its refusal row.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from meho_backplane.audit import _PREALLOCATED_AUDIT_ID_KEY, commit_with_request_audit_row
from meho_backplane.audit_cutover import note_preview
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_raw_session, get_session
from meho_backplane.instance_settings.console_only import CONSOLE_ONLY_POLICIES, SETTINGS_PAGE
from meho_backplane.instance_settings.diff import SECTIONS
from meho_backplane.instance_settings.schema import MAX_FILE_BYTES
from meho_backplane.instance_settings.service import (
    ImportPlan,
    SettingsForbiddenError,
    apply_import,
    attempted_tenant_ids,
    build_plan,
)
from meho_backplane.targets.schemas import TargetCreate
from meho_backplane.ui.auth.human import ensure_ui_human
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.reason import MAX_REASON_CHARS, clean_reason
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
_AUDIT_FAILED: Final[str] = "MEHO could not write the audit record of this import."
_APPLY_FAILED: Final[str] = "MEHO could not apply this file because of an internal error."
_REASON_TOO_LONG: Final[str] = f"The reason may have at most {MAX_REASON_CHARS} characters."
_NEEDS_REASON: Final[str] = "It needs a reason: fill in the Reason field."
_CONSOLE_ONLY: Final[str] = "MEHO accepts this change only from a person in the web console."

_SECTION_TITLES: Final[dict[str, str]] = {
    "tenants": "Tenant",
    "policies": "Policy",
    "conventions": "Convention",
    "targets": "Target",
}

#: The form fields of both POST routes. ``_read_file`` takes ``file`` when it
#: holds a file, else ``file_b64``.
_FILE_FIELDS: Final[dict[str, Any]] = {
    "file": {
        "type": "string",
        "format": "binary",
        "description": "The meho-settings.yaml file (the upload form).",
    },
    "file_b64": {
        "type": "string",
        "description": "The same file bytes as base64 (the Apply form of the preview).",
    },
}
_REASON_FIELD: Final[dict[str, Any]] = {
    "reason": {
        "type": "string",
        "maxLength": MAX_REASON_CHARS,
        "description": (
            "Why you make the change. Needed when the file turns announce_gate_enabled "
            "off or run_approval_target_kinds on."
        ),
    },
}


def _form_body(fields: dict[str, Any]) -> dict[str, Any]:
    schema = {"type": "object", "properties": fields}
    return {
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": schema}}}
    }


_require_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)
#: The apply commits or rolls back itself (see the module docstring).
_raw_session_dep = Depends(get_raw_session)


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


def _console_only_notes(plan: ImportPlan) -> list[str]:
    """One sentence for each change that only a person in the web console may make.

    The two policies of ``CONSOLE_ONLY_POLICIES``, which also need a reason,
    then each target the file adds or changes.
    """
    notes = []
    for path in plan.console_only:
        what = CONSOLE_ONLY_POLICIES[path.rsplit(".", 1)[1]][1]
        notes.append(f"`{path}` {what}. {_CONSOLE_ONLY} {_NEEDS_REASON}")
    for tenant_plan in plan.tenants:
        for i, name, body in tenant_plan.targets:
            what = "adds the target" if isinstance(body, TargetCreate) else "changes the target"
            notes.append(
                f"`spec.tenants[{tenant_plan.index}].targets[{i}]` {what} `{name}`. {_CONSOLE_ONLY}"
            )
    return notes


def _render(
    request: Request,
    session_ctx: UISessionContext,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request, template, {"csrf_token": csrf_token, **context}, status_code=status_code
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


def _refused(
    request: Request,
    session_ctx: UISessionContext,
    reason: str,
    *,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """A refusal as an HTML fragment. The page script swaps an error status in too."""
    return _render(
        request, session_ctx, "settings/_refused.html", {"reason": reason}, status_code=status_code
    )


async def _read_reason(request: Request) -> str | None:
    """The reason of the apply form without blanks around it, or ``None``.

    Call it after :func:`_read_file`, which parses the form with its limits;
    Starlette keeps the parsed form on the request.
    """
    value = (await request.form()).get("reason")
    return clean_reason(value) if isinstance(value, str) else None


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
    except ValueError:  # binascii.Error, and a str with a non-ASCII character
        return None


def _preview_context(plan: ImportPlan, content: bytes) -> dict[str, Any]:
    rows = diff_rows(plan.diff) if plan.diff else []
    return {
        "mistakes": plan.mistakes,
        "warnings": plan.warnings,
        "console_only": _console_only_notes(plan),
        "reason_needed": bool(plan.console_only),
        "max_reason_chars": MAX_REASON_CHARS,
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
        return _refused(request, session_ctx, str(exc), status_code=status.HTTP_403_FORBIDDEN)
    return _render(request, session_ctx, "settings/_preview.html", _preview_context(plan, content))


#: The audit keys that name the attempted import. A rolled back apply keeps
#: only these: every other key the apply bound describes a saved change.
_ATTEMPT_AUDIT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "audit_op_id",
        "audit_op_class",
        "audit_settings_import",
        "audit_settings_file_sha256",
        "audit_settings_tenant_ids",
    }
)


def _drop_saved_change_keys(before: set[str]) -> None:
    """Unbind the keys the rolled back apply bound, except the attempt keys."""
    added = set(structlog.contextvars.get_contextvars()) - before - _ATTEMPT_AUDIT_KEYS
    structlog.contextvars.unbind_contextvars(*added)


async def _undo_apply(
    db_session: AsyncSession, operator: Operator, content: bytes, before: set[str]
) -> None:
    """Roll a failed apply back; the failure row then names only the attempt."""
    _drop_saved_change_keys(before)
    structlog.contextvars.bind_contextvars(
        audit_settings_import=True,
        audit_settings_file_sha256=hashlib.sha256(content).hexdigest(),
    )
    try:
        await db_session.rollback()
    except Exception:
        # The connection is gone, so the database ends the transaction itself.
        _log.exception("ui_settings_apply_rollback_failed")
    try:
        tenant_ids = await attempted_tenant_ids(operator, content)
    except Exception:
        _log.exception("ui_settings_apply_tenants_unknown")
    else:
        structlog.contextvars.bind_contextvars(audit_settings_tenant_ids=tenant_ids)


async def _apply_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _settings_admin_dep,
    db_session: AsyncSession = _raw_session_dep,
) -> HTMLResponse:
    """``POST /ui/settings/apply``: checks the file again, then adds or updates.

    Commits the settings, the revision and the audit row together, or none.
    """
    # "read" until the service is about to write (it binds "write" there), so
    # a refused or unchanged file leaves no write in the audit log.
    structlog.contextvars.bind_contextvars(audit_op_id=_OP_ID_IMPORT, audit_op_class="read")
    content = await _read_file(request)
    if content is None:
        return _refused(request, session_ctx, _NO_FILE)
    reason = await _read_reason(request)
    if reason is not None and len(reason) > MAX_REASON_CHARS:
        return _refused(
            request,
            session_ctx,
            _REASON_TOO_LONG,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    keys_before = set(structlog.contextvars.get_contextvars())
    failure = _APPLY_FAILED
    try:
        result = await apply_import(db_session, operator, content, reason=reason)
        if result.audit_id is None:
            # Nothing to write: the file has mistakes or changes nothing.
            await db_session.rollback()
        else:
            failure = _AUDIT_FAILED
            # The middleware must not write a second row with this id.
            structlog.contextvars.unbind_contextvars(_PREALLOCATED_AUDIT_ID_KEY)
            # The key an operation policy edit writes its reason under.
            structlog.contextvars.bind_contextvars(audit_reason=reason)
            await commit_with_request_audit_row(db_session, request.scope, result.audit_id)
    except SettingsForbiddenError as exc:
        _drop_saved_change_keys(keys_before)
        await db_session.rollback()
        return _refused(request, session_ctx, str(exc), status_code=status.HTTP_403_FORBIDDEN)
    except asyncio.CancelledError:
        # The session rolls back when it closes; the audit row names the attempt.
        _drop_saved_change_keys(keys_before)
        raise
    except Exception:
        _log.exception("ui_settings_apply_failed")
        await _undo_apply(db_session, operator, content, keys_before)
        return _refused(
            request,
            session_ctx,
            failure,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
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
            "reason": reason if result.audit_id is not None else None,
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
        openapi_extra=_form_body(_FILE_FIELDS),
    )
    router.add_api_route(
        f"{SETTINGS_PATH}/apply",
        _apply_handler,
        methods=["POST"],
        name="ui_settings_apply",
        response_class=HTMLResponse,
        openapi_extra=_form_body({**_FILE_FIELDS, **_REASON_FIELD}),
    )
    return router
