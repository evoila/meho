# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backplane→corpus federation client (G4.5-T2 #1520).

The ``search_docs`` add-on (Initiative #1518) routes vendor-document
queries through the backplane to an **external** corpus service the ops
team runs, rather than ingesting the corpus into MEHO's own substrate.
This module is the one place that federation happens: a thin async
``httpx`` client that POSTs a search request to ``settings.corpus_url``
carrying ``Authorization: Bearer <operator.raw_jwt>`` — so the corpus's
own audit log sees the operator identity, exactly as
:func:`~meho_backplane.auth.vault.vault_client_for_operator` forwards the
operator JWT to Vault's OIDC auth.

Fail-closed by construction. The corpus being unconfigured
(``corpus_url`` unset), unreachable (network / timeout), or returning a
non-2xx status all collapse to one typed :class:`CorpusUnavailable`,
which the consuming ``search_docs`` route (T3, #1521) maps to HTTP 503 —
never a silent empty result. This mirrors the
``LlmClientUnavailable`` → 503 precedent (#1386).

The corpus request/response contract is a **consumer-side** dependency
(the corpus is owned elsewhere), so it is modelled behind a small typed
Pydantic adapter (:class:`CorpusChunk` / :class:`CorpusSearchResponse`)
that can be pinned without churning the call sites. The route, the
mandatory REQUIRE_FILTERS posture, and the central audit binding are
**out of scope here** — they land in T3.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings

__all__ = [
    "CorpusChunk",
    "CorpusSearchResponse",
    "CorpusUnavailable",
    "search_corpus",
]

_log = structlog.get_logger(__name__)


class CorpusUnavailable(RuntimeError):  # noqa: N818 -- "Unavailable" reads better than "Error" in the 503 detail
    """Raised when the external corpus cannot serve a search request.

    One typed error for every fail-closed branch — unconfigured,
    unreachable, or a non-2xx response — so the consuming ``search_docs``
    route (T3, #1521) maps it onto HTTP 503 without branching on the
    cause. ``status`` carries the upstream HTTP status code when the
    failure was a non-2xx corpus response (``None`` for an unconfigured
    or unreachable corpus) so callers can log the cause without parsing
    the message; the raw response body is **never** attached, so a
    corpus error page cannot leak through the 503.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class CorpusChunk(BaseModel):
    """One cited chunk returned by the external corpus.

    A deliberately small, frozen adapter over the corpus's response
    contract. The corpus is owned by the ops team, so its wire shape can
    drift; pinning the fields MEHO actually consumes here means a corpus
    change that adds fields is absorbed silently (``extra="ignore"``)
    while a change that drops a consumed field fails loudly at parse
    time — surfaced as :class:`CorpusUnavailable` rather than a partial
    result. ``metadata`` keeps the corpus's per-chunk attributes (e.g.
    ``product`` / ``version`` the T3 filters key off) without MEHO
    having to model every one.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    chunk_id: str
    document_id: str
    content: str
    source_url: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CorpusSearchResponse(BaseModel):
    """Parsed corpus search result: the cited chunks for a query.

    Frozen adapter; ``chunks`` is the ordered hit list (best first, as
    the corpus ranks them). The consuming route (T3) projects these into
    MEHO's own cited-chunk surface and binds the central audit row.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    chunks: list[CorpusChunk] = Field(default_factory=list)


async def search_corpus(
    operator: Operator,
    query: str,
    *,
    metadata_filters: dict[str, Any] | None = None,
    limit: int = 10,
) -> CorpusSearchResponse:
    """Search the external corpus as *operator*, returning cited chunks.

    POSTs a JSON search request to ``settings.corpus_url`` with
    ``Authorization: Bearer <operator.raw_jwt>`` so the corpus
    authenticates and audits the call as the operator. The request is
    bounded by ``settings.corpus_timeout_seconds`` across connect / read
    / write so a slow or hung corpus raises rather than blocking the
    event loop. ``settings.corpus_audience`` (RFC 8707), when set, is
    forwarded as the requested resource indicator; the corpus may use it
    to bind the token to itself.

    Args:
        operator: The verified operator whose JWT is forwarded.
        query: The free-text search query.
        metadata_filters: Optional binary ``{key: scalar}`` narrowing
            (e.g. ``{"product": "vmware", "version": "9.0"}``). The
            mandatory product/version REQUIRE_FILTERS posture is enforced
            by the consuming route (T3, #1521), **not** here — this
            transport forwards whatever filters it is given.
        limit: Maximum number of chunks to request.

    Raises:
        CorpusUnavailable: when ``corpus_url`` is unset (unconfigured),
            the corpus is unreachable / times out, or it returns a
            non-2xx status. The upstream status is carried on the
            exception (``status``) for non-2xx responses; the raw
            response body is never included.
    """
    settings = get_settings()
    corpus_url = settings.corpus_url
    if not corpus_url:
        # Fail-closed: an unconfigured corpus is unavailable, not empty.
        raise CorpusUnavailable("corpus_url is not configured")

    payload: dict[str, Any] = {"query": query, "limit": limit}
    if metadata_filters:
        payload["metadata_filters"] = metadata_filters
    if settings.corpus_audience:
        payload["audience"] = settings.corpus_audience

    headers = {"Authorization": f"Bearer {operator.raw_jwt}"}
    timeout = httpx.Timeout(settings.corpus_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(corpus_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # ConnectError / TimeoutException / any transport failure — the
        # corpus is unreachable. Log the cause by type (never the JWT or
        # the query body) and fail closed.
        _log.warning("corpus_unreachable", error=type(exc).__name__)
        raise CorpusUnavailable(f"corpus unreachable: {type(exc).__name__}") from exc

    if response.status_code // 100 != 2:
        # Non-2xx: surface the status for observability, but never the
        # response body — a corpus error page must not leak through the
        # 503 the route renders.
        _log.warning("corpus_request_failed", status=response.status_code)
        raise CorpusUnavailable(
            f"corpus returned HTTP {response.status_code}",
            status=response.status_code,
        )

    try:
        body: Any = response.json()
    except ValueError as exc:
        # A 2xx with a non-JSON body is a broken corpus contract —
        # fail closed rather than leaking a raw JSONDecodeError.
        _log.warning("corpus_response_not_json")
        raise CorpusUnavailable("corpus returned a non-JSON body") from exc

    try:
        return CorpusSearchResponse.model_validate(body)
    except ValueError as exc:
        # Schema drift (a consumed field dropped / wrong type) is also a
        # broken contract; fail closed without echoing the payload.
        _log.warning("corpus_response_invalid_schema")
        raise CorpusUnavailable("corpus response did not match the expected schema") from exc
