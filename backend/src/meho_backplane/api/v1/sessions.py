# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/sessions`` -- the agent session store.

* ``POST /api/v1/sessions`` -- upload one session as a gzip tar archive
  (``multipart/form-data``: the ``archive`` file and the session fields).
  Every person of the tenant may upload their own sessions, one at a time.
* ``GET /api/v1/sessions`` -- list the tenant's sessions (role
  ``operator`` and up), optionally only those of one time window
  (``since`` / ``until``).
* ``GET /api/v1/sessions/{session_id}`` -- one session with its file
  hashes and uploads (``operator`` and up).
* ``GET /api/v1/sessions/{session_id}/uploads/{seq}`` -- the stored archive
  of one upload (``operator`` and up; a quarantined upload needs
  ``tenant_admin``).
* ``DELETE /api/v1/sessions/{session_id}`` -- delete a session and its
  files (``tenant_admin``).
* ``GET`` / ``PUT /api/v1/sessions/retention`` -- the tenant's retention
  period (``tenant_admin``).

Only a person may call these routes. Errors: ``404 session_not_found``
(also for another tenant's session), ``409 session_conflict`` /
``session_owned_by_another_person`` / ``session_upload_in_progress``,
``413 session_archive_too_large``, ``422 archive_*`` /
``since_after_until`` / ``*_needs_time_zone``, ``429
session_upload_already_running`` / ``session_uploads_busy``, ``503
session_store_not_configured``, ``502 session_store_unavailable``. See
``docs/codebase/session-store.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.rbac import require_human_principal
from meho_backplane.session_store.archive import ArchiveError
from meho_backplane.session_store.config import SessionStoreConfig, load_config
from meho_backplane.session_store.object_store import (
    ObjectStore,
    ObjectStoreError,
    SessionStoreNotConfiguredError,
    get_object_store,
)
from meho_backplane.session_store.rights import SessionRight, has_right
from meho_backplane.session_store.schemas import (
    SessionDetail,
    SessionListResponse,
    SessionRetention,
    SessionRetentionUpdate,
    SessionState,
    SessionUploadResult,
)
from meho_backplane.session_store.service import (
    QuarantinedError,
    SessionNotFoundError,
    SessionWindow,
    delete_session,
    get_retention,
    list_sessions,
    read_session,
    read_upload,
    set_retention,
)
from meho_backplane.session_store.upload import SessionConflictError, UploadMeta, upload_session
from meho_backplane.session_store.upload_slots import UploadBusyError, upload_slot

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

#: A session id: letters, digits, ``.``, ``_`` and ``-``, 1 to 128 characters.
SESSION_ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_RESERVED_SESSION_ID: Final[str] = "retention"

_CHUNK: Final[int] = 1024 * 1024


def _require(right: SessionRight) -> Callable[[Operator], Operator]:
    def check(operator: Operator = Depends(require_human_principal)) -> Operator:
        if not has_right(operator, right):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN, detail="insufficient_role"
            )
        return operator

    return check


_upload_right = Depends(_require(SessionRight.UPLOAD))
_list_right = Depends(_require(SessionRight.LIST))
_read_right = Depends(_require(SessionRight.READ))
_delete_right = Depends(_require(SessionRight.DELETE))
_retention_right = Depends(_require(SessionRight.SET_RETENTION))

SessionIdPath = Annotated[str, Path(pattern=SESSION_ID_PATTERN)]


def _configured() -> tuple[SessionStoreConfig, ObjectStore]:
    config = load_config()
    try:
        return config, get_object_store(config)
    except SessionStoreNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session_store_not_configured",
        ) from exc


def _unavailable(exc: ObjectStoreError) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY, detail="session_store_unavailable"
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="session_not_found")


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{name}_needs_time_zone",
        )
    return value


async def _read_limited(archive: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await archive.read(_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
                detail="session_archive_too_large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/retention", response_model=SessionRetention)
async def get_session_retention(
    operator: Annotated[Operator, _retention_right],
) -> SessionRetention:
    """Return the retention period of the caller's tenant."""
    return await get_retention(operator, load_config())


@router.put("/retention", response_model=SessionRetention)
async def put_session_retention(
    body: SessionRetentionUpdate,
    request: Request,
    operator: Annotated[Operator, _retention_right],
) -> SessionRetention:
    """Set the retention period of the caller's tenant; ``null`` uses the default."""
    return await set_retention(operator, body.retention_days, load_config(), path=request.url.path)


@router.post("", response_model=SessionUploadResult)
async def upload_agent_session(
    request: Request,
    operator: Annotated[Operator, _upload_right],
    archive: Annotated[UploadFile, File(description="The session as one gzip tar archive.")],
    session_id: Annotated[str, Form(pattern=SESSION_ID_PATTERN)],
    project: Annotated[str, Form(min_length=1, max_length=200)],
    app: Annotated[str, Form(min_length=1, max_length=64)],
    app_version: Annotated[str | None, Form(max_length=64)] = None,
    started_at: Annotated[datetime | None, Form()] = None,
    ended_at: Annotated[datetime | None, Form()] = None,
    state: Annotated[SessionState, Form()] = "open",
    outcome: Annotated[str | None, Form(max_length=200)] = None,
) -> SessionUploadResult:
    """Upload one session, or its new tail. A retry of the same bytes changes nothing."""
    if session_id == _RESERVED_SESSION_ID:
        # GET /api/v1/sessions/retention is the retention setting, not a session.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail="session_id_reserved"
        )
    config, store = _configured()
    meta = UploadMeta(
        session_id=session_id,
        project=project,
        app=app,
        app_version=app_version,
        started_at=_aware(started_at, "started_at"),
        ended_at=_aware(ended_at, "ended_at"),
        state=state,
        outcome=outcome,
    )
    try:
        with upload_slot(operator):
            data = await _read_limited(archive, config.max_upload_bytes)
            return await upload_session(
                operator, meta, data, store=store, config=config, path=request.url.path
            )
    except UploadBusyError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.reason,
            headers={"Retry-After": "60"},
        ) from exc
    except ArchiveError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.reason
        ) from exc
    except SessionConflictError as exc:
        detail: Any = {"reason": exc.reason, "paths": exc.paths} if exc.paths else exc.reason
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=detail) from exc
    except ObjectStoreError as exc:
        raise _unavailable(exc) from exc


@router.get("", response_model=SessionListResponse)
async def list_agent_sessions(
    request: Request,
    operator: Annotated[Operator, _list_right],
    project: Annotated[str | None, Query(max_length=200)] = None,
    state: Annotated[SessionState | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(pattern=r"^[0-9]{1,9}$")] = None,
    since: Annotated[
        datetime | None, Query(description="Only sessions with an upload at or after this time.")
    ] = None,
    until: Annotated[
        datetime | None, Query(description="Only sessions that started before this time.")
    ] = None,
) -> SessionListResponse:
    """List the tenant's sessions, newest upload first.

    With ``since`` and ``until`` (ISO 8601, with a time zone) only the
    sessions active in that time: the last upload is at or after
    ``since``, and the start is before ``until``.
    """
    window = SessionWindow(since=_aware(since, "since"), until=_aware(until, "until"))
    if window.since is not None and window.until is not None and window.since > window.until:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail="since_after_until"
        )
    return await list_sessions(
        operator,
        path=request.url.path,
        project=project,
        state=state,
        window=window,
        limit=limit,
        offset=int(cursor) if cursor else 0,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def show_agent_session(
    session_id: SessionIdPath, request: Request, operator: Annotated[Operator, _read_right]
) -> SessionDetail:
    """Return one session with its file hashes and uploads."""
    try:
        return await read_session(operator, session_id, path=request.url.path)
    except SessionNotFoundError as exc:
        raise _not_found() from exc


@router.get(
    "/{session_id}/uploads/{seq}",
    response_class=Response,
    responses={200: {"content": {"application/gzip": {}}}},
)
async def download_agent_session_upload(
    session_id: SessionIdPath,
    seq: Annotated[int, Path(ge=1)],
    request: Request,
    operator: Annotated[Operator, _read_right],
) -> Response:
    """Return the stored (checked) archive of one upload."""
    _, store = _configured()
    try:
        data = await read_upload(operator, session_id, seq, store=store, path=request.url.path)
    except SessionNotFoundError as exc:
        raise _not_found() from exc
    except QuarantinedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="session_upload_quarantined"
        ) from exc
    except ObjectStoreError as exc:
        raise _unavailable(exc) from exc
    return Response(content=data, media_type="application/gzip")


@router.delete("/{session_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_agent_session(
    session_id: SessionIdPath, request: Request, operator: Annotated[Operator, _delete_right]
) -> Response:
    """Delete one session and its files."""
    _, store = _configured()
    try:
        await delete_session(operator, session_id, store=store, path=request.url.path)
    except SessionNotFoundError as exc:
        raise _not_found() from exc
    except ObjectStoreError as exc:
        raise _unavailable(exc) from exc
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
