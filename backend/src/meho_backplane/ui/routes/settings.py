# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings page: export, import with a preview, history (#4048).

The console face of ``/api/v1/settings`` (``docs/codebase/settings.md``). Every
handler calls the same code as the REST routes, in-process, so the page shows
the same sentences and the same diff as ``meho settings import --preview``:
:func:`~meho_backplane.api.v1.instance_settings.run_import` and
:func:`~meho_backplane.api.v1.instance_settings.export`. The rows of the diff
come from :func:`diff_rows`, the twin of ``diffRows`` in
``cli/internal/cmd/settings/render.go``; both are tested against one fixture.

* ``GET  /ui/settings``                -- the page.
* ``GET  /ui/settings/export``         -- downloads ``meho-settings.yaml``.
* ``POST /ui/settings/import/preview`` -- the mistakes and the diff (HTMX).
* ``POST /ui/settings/import/apply``   -- the apply (HTMX).

The first login: while :func:`~meho_backplane.instance_settings.service.is_first_run`
holds, ``GET /ui/`` sends an admin here (:func:`lands_on_settings`) and the page
offers two choices, the form or a file. There is no page before login.

The gates are the REST ones: ``tenant_admin`` or ``platform_admin``, and a
person for the import. The upload is multipart, because the CSRF middleware
caps a url-encoded body at 256 KiB and a settings file may be 4 MiB. The preview
holds no state: the apply form carries the exact file bytes as base64, so the
revision's ``file_sha256`` is the digest of the file the operator picked.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from meho_backplane.api.v1.instance_settings import (
    SettingsImportResponse,
    export,
    require_human_settings_admin,
    require_settings_admin,
    run_import,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_session, get_sessionmaker
from meho_backplane.instance_settings.schema import MAX_FILE_BYTES
from meho_backplane.instance_settings.service import (
    SettingsForbiddenError,
    is_first_run,
    list_history,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.roles import resolve_ui_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["SETTINGS_PATH", "build_settings_router", "diff_rows", "lands_on_settings"]

_log = structlog.get_logger(__name__)

SETTINGS_PATH: Final[str] = "/ui/settings"

_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("tenants", "Tenants"),
    ("policies", "Policies"),
    ("conventions", "Conventions"),
    ("targets", "Targets"),
)

#: The apply form's base64 field: a 4 MiB file, plus a third for base64.
_MAX_FORM_PART_BYTES: Final[int] = MAX_FILE_BYTES * 4 // 3 + 1024

_HISTORY_LIMIT: Final[int] = 50

_require_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _change_detail(item: dict[str, Any]) -> str:
    """``a -> b`` for a policy; ``key: a -> b; ...``, in key order, for an item."""
    before, after = item.get("before"), item.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return f"{_compact(before)} -> {_compact(after)}"
    return "; ".join(
        f"{key}: {_compact(before.get(key))} -> {_compact(after[key])}" for key in sorted(after)
    )


def diff_rows(diff: dict[str, Any]) -> list[dict[str, str]]:
    """One row per line of the preview, in the order the CLI prints them."""
    rows: list[dict[str, str]] = []
    for name, title in _SECTIONS:
        section = diff.get(name) or {}
        for kind, field in (
            ("added", "added"),
            ("changed", "changed"),
            ("not in file", "not_in_file"),
        ):
            for item in section.get(field) or []:
                rows.append(
                    {
                        "section": title,
                        "kind": kind,
                        "tenant": item.get("tenant", ""),
                        "key": item.get("key", ""),
                        "detail": _change_detail(item) if kind == "changed" else "",
                    }
                )
    return rows


def _is_settings_admin(operator: Operator) -> bool:
    return operator.platform_admin or operator.tenant_role == TenantRole.TENANT_ADMIN


async def lands_on_settings(session_ctx: UISessionContext) -> bool:
    """Whether ``GET /ui/`` sends this person to the settings page.

    True for an admin on a first-run install. It runs on every dashboard load
    and fails soft: a landing check must not 5xx the dashboard.
    """
    try:
        async with get_sessionmaker()() as session:
            if not await is_first_run(session):
                return False
    except SQLAlchemyError as exc:
        _log.warning("ui_settings_first_run_unknown", reason=type(exc).__name__)
        return False
    operator = await resolve_ui_operator(session_ctx)
    return operator is not None and _is_settings_admin(operator)


async def _operator_or_403(session_ctx: UISessionContext) -> Operator:
    operator = await resolve_ui_operator(session_ctx)
    if operator is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_role")
    return operator


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


async def _page_context(db_session: AsyncSession, operator: Operator) -> dict[str, Any]:
    return {
        "page_title": "Settings",
        "active_surface": "settings",
        "first_run": await is_first_run(db_session),
        "revisions": await _history_rows(db_session, operator),
        "export_mistakes": [],
    }


async def _history_rows(db_session: AsyncSession, operator: Operator) -> list[dict[str, Any]]:
    rows = await list_history(db_session, operator, limit=_HISTORY_LIMIT)
    return [{**row, "changed": _summary_text(row["summary"])} for row in rows]


def _summary_text(summary: dict[str, Any]) -> str:
    parts = [
        f"{name} +{counts.get('added', 0)} ~{counts.get('changed', 0)}"
        for name, counts in ((name, summary.get(name) or {}) for name, _title in _SECTIONS)
        if counts.get("added", 0) or counts.get("changed", 0)
    ]
    return ", ".join(parts) or "-"


async def _page_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``GET /ui/settings``."""
    operator = require_settings_admin(await _operator_or_403(session_ctx))
    return _render(
        request, session_ctx, "settings/index.html", await _page_context(db_session, operator)
    )


async def _export_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> Response:
    """``GET /ui/settings/export``: the file, or the page with the reasons it was refused."""
    operator = require_settings_admin(await _operator_or_403(session_ctx))
    try:
        return await export(operator, db_session, None)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT or not isinstance(exc.detail, dict):
            raise
        context = await _page_context(db_session, operator)
        return _render(
            request,
            session_ctx,
            "settings/index.html",
            {**context, "export_mistakes": exc.detail["mistakes"]},
            status_code=status.HTTP_409_CONFLICT,
        )


async def _read_file(request: Request) -> bytes | None:
    """The file from the upload field or the apply form's base64 field; ``None`` if neither."""
    form = await request.form(max_files=1, max_fields=1, max_part_size=_MAX_FORM_PART_BYTES)
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.filename:
        # One byte over the cap, so the parser gives its "larger than" sentence.
        return await upload.read(MAX_FILE_BYTES + 1)
    encoded = form.get("file_b64")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return None


def _preview_context(answer: SettingsImportResponse, content: bytes) -> dict[str, Any]:
    rows = diff_rows(answer.diff) if answer.diff else []
    return {
        "answer": answer,
        "rows": rows,
        "has_changes": any(row["kind"] != "not in file" for row in rows),
        "not_in_file": any(row["kind"] == "not in file" for row in rows),
        "file_b64": base64.b64encode(content).decode("ascii"),
    }


async def _import(
    request: Request,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
    *,
    preview: bool,
) -> HTMLResponse:
    """Run the preview or the apply and render the fragment.

    A refusal is a 200 fragment: HTMX does not swap a 4xx, and the sentence must
    reach the page. The role gates above stay real 403s.
    """
    operator = require_human_settings_admin(await _operator_or_403(session_ctx))
    content = await _read_file(request)
    if content is None:
        return _render(
            request,
            session_ctx,
            "settings/_refused.html",
            {"sentences": ["Choose a settings file first."]},
        )
    try:
        answer = await run_import(db_session, operator, content, preview=preview)
    except SettingsForbiddenError as exc:
        return _render(
            request,
            session_ctx,
            "settings/_refused.html",
            {"sentences": [str(exc)]},
        )
    if answer.mistakes or preview:
        return _render(
            request, session_ctx, "settings/_preview.html", _preview_context(answer, content)
        )
    return _render(
        request,
        session_ctx,
        "settings/_result.html",
        {"answer": answer, "revisions": await _history_rows(db_session, operator)},
    )


async def _preview_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/settings/import/preview``: writes nothing."""
    return await _import(request, session_ctx, db_session, preview=True)


async def _apply_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/settings/import/apply``: re-checks the file, then adds or updates."""
    return await _import(request, session_ctx, db_session, preview=False)


def build_settings_router() -> APIRouter:
    """Construct the settings :class:`APIRouter`."""
    router = APIRouter(tags=["ui-settings"])
    router.add_api_route(
        SETTINGS_PATH,
        _page_handler,
        methods=["GET"],
        name="ui_settings",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        f"{SETTINGS_PATH}/export",
        _export_handler,
        methods=["GET"],
        name="ui_settings_export",
        response_class=Response,
    )
    router.add_api_route(
        f"{SETTINGS_PATH}/import/preview",
        _preview_handler,
        methods=["POST"],
        name="ui_settings_import_preview",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        f"{SETTINGS_PATH}/import/apply",
        _apply_handler,
        methods=["POST"],
        name="ui_settings_import_apply",
        response_class=HTMLResponse,
    )
    return router
