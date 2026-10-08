# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared ``read_docs`` service: read more around a docs hit (#3948).

A docs hit is one chunk of a page. ``read_docs`` lets an agent or a person
read more around it: the chunks before and after the hit (``around``), the
whole page (``page``), or the section the hit sits in (``section``). Every
face (the MCP tool, ``POST /api/v1/read_docs``, ``meho docs read`` and the
cited-source page in the console) goes through :func:`read_docs` here, so
they share one rule set.

How a read works
================

A search hit or an ``ask_docs`` citation carries an opaque ``read_handle``
when its collection offers read
(:meth:`~meho_backplane.docs_search.backends.base.SearchBackend.supports_read`,
opted in per collection with ``backend.ref["read"] = "upstream"``). The caller
sends the handle back with the collection key; :func:`read_docs` resolves the
collection's backend and calls its
:meth:`~meho_backplane.docs_search.backends.base.SearchBackend.read`. The
reply's text stays untrusted: the MCP face wraps it, the console escapes it.

One "not found" for every refusal
=================================

A collection the caller cannot see or is not entitled to, a disabled
collection, a collection without read, and a handle the backend refuses (bad,
foreign, or bound to other filters) all raise the same
:class:`DocsReadNotFoundError` with the same message,
:data:`DOCS_SOURCE_NOT_FOUND`. So a caller cannot use ``read_docs`` to learn
which collections exist or what they hold. :func:`resolve_readable_collection`
folds the collection-access errors into it; :func:`read_docs` adds the rest.

MCP, REST and the CLI show this one answer for every refusal. The console's
cited-source page is the exception: its read ``POST`` runs the page's own
gate first, the same as its ``GET`` view, so an unknown collection still gets
a 404 and a not-entitled one a 403 that names the missing capability. Only
the refusals after that gate reach this rule there; they show one neutral
"not available" note.

Two refusals are told apart because the caller can act on them and they leak
nothing (the backend only answers them for a handle it signed):

* :class:`DocsReadSearchAgainError` -- the handle is too old or the page
  changed. A new search gives a new handle.
* :class:`DocsReadRateLimitedError` -- this person read too much. It carries
  the seconds to wait, when the backend said.

A collection that is known and entitled but still provisioning or rebuilding
raises :class:`~meho_backplane.docs_search.CollectionNotReadyError` (retry
later), and a backend that is down raises
:class:`~meho_backplane.auth.corpus.CorpusUnavailable`, as on search.

What is never logged
====================

The read handle and the cursors carry a few words of the hit, so they are as
sensitive as the hit text. No log line, error message or audit row carries
them. The ``docs_read_completed`` / ``docs_read_refused`` records name the
collection, the outcome and the ``operator_sub`` (as the search logs do),
and a completed read also the mode; never the handle, the cursor or the
caller id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.corpus import CorpusReadError, ReadDisclosure, ReadMode, ReadReason
from meho_backplane.docs_search.backends import resolve_backend
from meho_backplane.docs_search.citation_links import public_source_url
from meho_backplane.docs_search.collection_access import (
    CollectionDisabledError,
    CollectionForbiddenError,
    UnknownCollectionError,
    resolve_entitled_ready_collection,
)
from meho_backplane.docs_search.service import forwarded_scope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator
    from meho_backplane.docs_collections import DocCollection
    from meho_backplane.docs_search.service import DocsScope

__all__ = [
    "DOCS_SOURCE_NOT_FOUND",
    "READ_AROUND_MAX",
    "READ_SEARCH_AGAIN",
    "DocsReadError",
    "DocsReadNotFoundError",
    "DocsReadRateLimitedError",
    "DocsReadResult",
    "DocsReadSearchAgainError",
    "read_docs",
    "resolve_readable_collection",
]

_log = structlog.get_logger(__name__)

#: The one message every read refusal carries on MCP, REST and the CLI.
DOCS_SOURCE_NOT_FOUND: Final[str] = "docs source not found"

#: The message of a read whose handle must be renewed by a new search.
READ_SEARCH_AGAIN: Final[str] = (
    "this read handle is too old or the page has changed; search again and use the new read_handle"
)

#: Most chunks ``around`` reads before or after the hit (the backend's limit).
READ_AROUND_MAX: Final[int] = 3


class DocsReadError(Exception):
    """Base class of the typed ``read_docs`` refusals each face maps."""


class DocsReadNotFoundError(DocsReadError):
    """Any read refusal: one message, so the caller cannot tell the cases apart."""

    def __init__(self) -> None:
        super().__init__(DOCS_SOURCE_NOT_FOUND)


class DocsReadSearchAgainError(DocsReadError):
    """The read handle is too old or the page changed: search again."""

    def __init__(self) -> None:
        super().__init__(READ_SEARCH_AGAIN)


class DocsReadRateLimitedError(DocsReadError):
    """This person read too much. ``retry_after`` is in seconds, when known."""

    def __init__(self, retry_after: int | None) -> None:
        self.retry_after = retry_after
        wait = f"wait {retry_after} seconds" if retry_after is not None else "wait a moment"
        super().__init__(f"too many docs reads; {wait} and try again")


class DocsReadResult(BaseModel):
    """The result of one ``read_docs`` call, the same on every face.

    * ``mode`` -- what was read: ``around``, ``page`` or ``section``.
    * ``text`` -- the text, or ``None`` when there is none to return: the
      file's owner allows only its link (``disclosure`` is ``link``), or the
      backend cannot read the file's type (``reason`` says which).
    * ``title`` -- the source's title, when known.
    * ``source_url`` -- the public ``http(s)`` link to the source, when there
      is one. Never a storage path.
    * ``disclosure`` -- ``full`` (text may be returned) or ``link`` (only the
      link).
    * ``reason`` -- why ``text`` is ``None``: ``link_only``,
      ``pdf_not_supported`` or ``type_not_supported``.
    * ``located`` -- whether the backend found the hit in the file (``None``
      when it does not say).
    * ``truncated`` -- whether the backend cut the reply at its size cap.
    * ``next`` / ``up`` -- opaque cursors: pass one back as ``cursor`` (with
      the same ``read_handle``) to read on, or ``None``.

    Frozen so a face cannot change it after the read.
    """

    model_config = ConfigDict(frozen=True)

    mode: ReadMode
    text: str | None = None
    title: str | None = None
    source_url: str | None = None
    disclosure: ReadDisclosure
    reason: ReadReason | None = None
    located: bool | None = None
    truncated: bool = False
    next: str | None = None
    up: str | None = None


async def resolve_readable_collection(
    session: AsyncSession,
    operator: Operator,
    collection_key: str,
) -> DocCollection:
    """Resolve *collection_key* for a read, folding refusals into "not found".

    Runs the shared resolve + entitle + readiness gate
    (:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`).
    An unknown, not-entitled or disabled collection raises
    :class:`DocsReadNotFoundError`, the same error a refused handle gives.
    A collection that is known and entitled but not ready yet still raises
    :class:`~meho_backplane.docs_search.CollectionNotReadyError`: only an
    entitled caller reaches that check, so it reveals nothing new.
    """
    try:
        return await resolve_entitled_ready_collection(session, operator, collection_key)
    except (UnknownCollectionError, CollectionForbiddenError, CollectionDisabledError) as exc:
        _log.info(
            "docs_read_refused",
            operator_sub=operator.sub,
            collection_key=collection_key,
            reason=type(exc).__name__,
        )
        raise DocsReadNotFoundError() from exc


async def read_docs(
    operator: Operator,
    read_handle: str,
    *,
    scope: DocsScope,
    collection: DocCollection,
    mode: ReadMode = "around",
    before: int = 1,
    after: int = 1,
    cursor: str | None = None,
) -> DocsReadResult:
    """Read around a hit in *collection*, named by its *read_handle*.

    *collection* has already passed :func:`resolve_readable_collection`. This
    resolves its backend, refuses a collection without read (not found), and
    calls the backend's read with the hard filters the collection's scope
    gates would have sent on search (:func:`~meho_backplane.docs_search.forwarded_scope`):
    the handle is bound to the hit's search filters. A soft ``scope`` is never
    sent on a read.

    The reply is projected: the source becomes a public link or ``None``
    (:func:`~meho_backplane.docs_search.citation_links.public_source_url`),
    never a storage path.

    Args:
        operator: The verified operator.
        read_handle: The opaque handle from a hit or a citation. Never logged.
        scope: The validated scope (collection key plus the optional
            ``product`` / ``version`` the hit was searched with).
        collection: The resolved, entitled, ready collection.
        mode: ``around``, ``page`` or ``section``.
        before: Chunks before the hit for ``around``, 0 to 3.
        after: Chunks after the hit for ``around``, 0 to 3.
        cursor: A ``next`` / ``up`` cursor from an earlier reply, or ``None``.

    Raises:
        DocsReadNotFoundError: the collection does not offer read, or the
            backend refused the handle.
        DocsReadSearchAgainError: the handle must be renewed by a new search.
        DocsReadRateLimitedError: this person read too much.
        CorpusUnavailable: the backend is unconfigured, down or answered
            something unusable.
    """
    resolved = resolve_backend(collection)
    if not resolved.backend.supports_read(resolved.ref):
        _log.info(
            "docs_read_refused",
            operator_sub=operator.sub,
            collection_key=scope.collection_key,
            reason="read_not_enabled",
        )
        raise DocsReadNotFoundError()

    forwarded = forwarded_scope(scope, resolved.ref)
    try:
        upstream = await resolved.backend.read(
            operator,
            read_handle,
            backend_ref=resolved.ref,
            mode=mode,
            before=before,
            after=after,
            cursor=cursor,
            filters=dict(forwarded.filters) or None,
        )
    except CorpusReadError as exc:
        _log.info(
            "docs_read_refused",
            operator_sub=operator.sub,
            collection_key=scope.collection_key,
            reason=exc.kind,
        )
        if exc.kind == CorpusReadError.KIND_SEARCH_AGAIN:
            raise DocsReadSearchAgainError() from exc
        if exc.kind == CorpusReadError.KIND_RATE_LIMITED:
            raise DocsReadRateLimitedError(exc.retry_after) from exc
        raise DocsReadNotFoundError() from exc

    result = DocsReadResult(
        mode=upstream.mode,
        text=upstream.text,
        title=upstream.title,
        source_url=public_source_url(
            upstream.source_uri,
            title=upstream.title,
            upstream_url=upstream.upstream_url,
        ),
        disclosure=upstream.disclosure,
        reason=upstream.reason,
        located=upstream.located,
        truncated=upstream.truncated,
        next=upstream.next,
        up=upstream.up,
    )
    _log.info(
        "docs_read_completed",
        operator_sub=operator.sub,
        collection_key=scope.collection_key,
        mode=result.mode,
        disclosure=result.disclosure,
        reason=result.reason,
        located=result.located,
        truncated=result.truncated,
        has_next=result.next is not None,
        text_chars=len(result.text) if result.text is not None else 0,
        scope_forwarded=forwarded.mode,
    )
    return result
