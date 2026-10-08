# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/read_docs`` -- read more around a docs hit (#3948).

The REST face of the shared :func:`~meho_backplane.docs_search.read_docs`
service, with the same body and result as the MCP ``read_docs`` tool. A
caller sends the opaque ``read_handle`` a ``search_docs`` hit or an
``ask_docs`` citation carried, plus the collection it came from, and gets
back the text around the hit, the whole page, or the hit's section.

Status codes
------------

* **200** -- a :class:`~meho_backplane.docs_search.DocsReadResult`. ``text``
  is ``null`` when the file allows only its link (``disclosure`` is
  ``link``) or its type cannot be read (``reason`` says which).
* **404** -- every refusal, with one fixed body: an unknown, not-entitled or
  disabled collection, a collection without read, or a handle the backend
  refuses. The body never says which, so the route is no probe for which
  collections exist or what they hold.
* **409** -- the handle is too old or the page changed: search again and use
  the new handle (``detail.error = "search_again"``).
* **422** -- a malformed body, or a missing ``collection``.
* **429** -- this person read too much; ``Retry-After`` says how many seconds
  to wait, when the backend said.
* **503** -- the collection is still provisioning / rebuilding
  (``detail.error = "collection_not_ready"``), or its backend is unavailable
  (``detail.error = "read_unavailable"``). Retryable.

Audit and privacy
-----------------

``operator`` role minimum, like ``search_docs``. The route binds
``audit_op_id = "meho.docs.read"``, ``audit_op_class = "read"``, the
collection and the mode before the read. The read handle and the cursor are
never bound, logged or echoed: they carry a few words of the hit.
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.docs_search import (
    DOCS_SOURCE_NOT_FOUND,
    CollectionNotReadyError,
    DocsReadNotFoundError,
    DocsReadRateLimitedError,
    DocsReadResult,
    DocsReadSearchAgainError,
    MissingDocsFilterError,
    build_docs_scope,
    read_docs,
    resolve_readable_collection,
)
from meho_backplane.docs_search.read import READ_AROUND_MAX

__all__ = ["ReadDocsRequest", "router"]

router = APIRouter(prefix="/api/v1", tags=["docs"])

#: Module-level ``Depends`` closure for the RBAC gate (ruff B008 idiom).
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Longest ``read_handle`` / ``cursor`` accepted (the corpus parse's bound).
_MAX_READ_TOKEN = 8192

#: The one 404 body every read refusal returns.
_NOT_FOUND_DETAIL: dict[str, str] = {"error": "not_found", "message": DOCS_SOURCE_NOT_FOUND}

_log = structlog.get_logger(__name__)


class ReadDocsRequest(BaseModel):
    """POST body for ``/api/v1/read_docs``, the same fields as the MCP tool.

    ``collection`` is typed optional so a missing value gets the docs
    surfaces' own 422 naming the mandatory scope. ``product`` / ``version``
    are needed only on a collection whose scope gates send hard filters: the
    handle is bound to the filters of the hit's search. ``extra="forbid"``
    rejects unknown fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection: str | None = Field(default=None, max_length=128)
    read_handle: str = Field(min_length=1, max_length=_MAX_READ_TOKEN)
    mode: Literal["around", "page", "section"] = "around"
    before: int = Field(default=1, ge=0, le=READ_AROUND_MAX)
    after: int = Field(default=1, ge=0, le=READ_AROUND_MAX)
    cursor: str | None = Field(default=None, min_length=1, max_length=_MAX_READ_TOKEN)
    product: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=128)


@router.post(
    "/read_docs",
    response_model=DocsReadResult,
    responses={
        404: {
            "description": (
                "Every read refusal, with one fixed body: an unknown, "
                "not-entitled or disabled collection, a collection without "
                "read, or a read handle the backend refuses. The body never "
                "says which."
            ),
        },
        409: {
            "description": (
                "The read handle is too old or the page changed "
                "(`detail.error = 'search_again'`). Search again and use the "
                "new handle."
            ),
        },
        422: {"description": "A malformed body, or no `collection`."},
        429: {
            "description": (
                "Too many reads by this person (`detail.error = "
                "'rate_limited'`). `Retry-After` gives the seconds to wait "
                "when known."
            ),
        },
        503: {
            "description": (
                "The collection is still provisioning / rebuilding "
                "(`detail.error = 'collection_not_ready'`) or its backend is "
                "unavailable (`detail.error = 'read_unavailable'`). Retryable."
            ),
        },
    },
)
async def read_docs_endpoint(
    body: ReadDocsRequest,
    operator: Annotated[Operator, _require_operator],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocsReadResult:
    """Read the text around a docs hit, the whole page or the hit's section."""
    try:
        scope = build_docs_scope(body.collection, body.product, body.version)
    except MissingDocsFilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    structlog.contextvars.bind_contextvars(
        audit_op_id="meho.docs.read",
        audit_op_class="read",
        audit_collection=scope.collection_key,
        audit_read_mode=body.mode,
    )

    try:
        collection = await resolve_readable_collection(session, operator, scope.collection_key)
        return await read_docs(
            operator,
            body.read_handle,
            scope=scope,
            collection=collection,
            mode=body.mode,
            before=body.before,
            after=body.after,
            cursor=body.cursor,
        )
    except DocsReadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NOT_FOUND_DETAIL,
        ) from exc
    except DocsReadSearchAgainError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "search_again", "message": str(exc)},
        ) from exc
    except DocsReadRateLimitedError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "retry_after": exc.retry_after, "message": str(exc)},
            headers=headers,
        ) from exc
    except CollectionNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "collection_not_ready", "message": str(exc)},
        ) from exc
    except CorpusUnavailable as exc:
        _log.warning(
            "read_docs_backend_unavailable",
            operator_sub=operator.sub,
            collection=scope.collection_key,
            corpus_status=exc.status,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_unavailable", "message": str(exc)},
        ) from exc
