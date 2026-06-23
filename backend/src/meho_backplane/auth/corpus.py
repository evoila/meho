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

G4.6-T2 (#1551) re-homes this transport behind the backend-agnostic
search router: :class:`~meho_backplane.docs_search.backends.corpus_http.CorpusHttpBackend`
wraps :func:`search_corpus` as the first
:class:`~meho_backplane.docs_search.backends.base.SearchBackend` adapter,
passing a per-collection ``corpus_url`` / ``audience`` resolved from the
collection's ``backend.ref``. The optional ``corpus_url`` / ``audience``
overrides on :func:`search_corpus` are the seam for that — ``None`` keeps
the legacy global-settings behaviour for an unmigrated single-collection
deploy. The wire adapters (:class:`CorpusChunk` /
:class:`CorpusSearchResponse`) and the one typed
:class:`CorpusUnavailable` stay here, imported by the adapter and every
other consumer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings

__all__ = [
    "CorpusChunk",
    "CorpusSearchResponse",
    "CorpusStatusResponse",
    "CorpusUnavailable",
    "corpus_status",
    "derive_status_url",
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


def _parse_2xx_body[ModelT: BaseModel](
    response: httpx.Response,
    model: type[ModelT],
    *,
    event_prefix: str,
) -> ModelT:
    """Decode a 2xx corpus response body and validate it against *model*.

    Both fail-closed branches map to one :class:`CorpusUnavailable` so the
    consuming route renders a single 503: a non-JSON body and a body that
    does not match the model (a dropped/renamed consumed field, or — for
    :class:`CorpusSearchResponse` — an unrecognised envelope that names
    neither ``chunks`` nor ``results``, #1732). Neither the raw body nor
    the validation error detail is echoed, so a corpus error page or a
    leaky field value cannot ride out through the 503. *event_prefix*
    namespaces the structlog event (``corpus`` vs ``corpus_status``).
    """
    try:
        body: Any = response.json()
    except ValueError as exc:
        _log.warning(f"{event_prefix}_response_not_json")
        raise CorpusUnavailable("corpus returned a non-JSON body") from exc

    try:
        return model.model_validate(body)
    except ValueError as exc:
        _log.warning(f"{event_prefix}_response_invalid_schema")
        raise CorpusUnavailable("corpus response did not match the expected schema") from exc


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

    The text and source-link fields each accept **two** wire names via a
    validation alias (#1732): MEHO.Knowledge's ``/search`` returns
    ``text`` / ``source_uri`` while an earlier corpus shape (and MEHO's
    own internal projection) uses ``content`` / ``source_url``. Accepting
    both keeps the one consumed name (``content`` / ``source_url``) stable
    for downstream callers regardless of which wire dialect the corpus
    speaks. ``populate_by_name=True`` keeps the internal name usable when
    constructing the model directly in tests.

    ``document_id`` is modelled ``str | None`` (#2004). The contract names
    the field ``document_id`` — MEHO.Knowledge speaks that exact key, so
    there is no second wire name to alias (the #1732 fix aliased the three
    fields that *did* drift; ``document_id`` is not one of them). What it
    can be is **absent**: MEHO.Knowledge returns ``document_id: ""`` when a
    chunk has no owning-document concept. ``document_id`` is only ever read
    as a *citation-label fallback* (``title -> document_id -> filename ->
    URL`` in :func:`~meho_backplane.docs_search.citation_links._label_for`)
    and is never used to resolve or ground a citation, so a blank value is
    not a contract breach — but carrying it as a required ``""`` is a lie.
    The validator below normalises blank-after-strip to ``None`` so the
    absence is honestly typed and the label chain skips a cleanly-``None``
    rung rather than a misleading empty string.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    chunk_id: str
    document_id: str | None = None
    content: str = Field(validation_alias=AliasChoices("content", "text"))
    source_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("source_url", "source_uri"),
    )
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("document_id", mode="before")
    @classmethod
    def _blank_document_id_to_none(cls, value: object) -> object:
        """Normalise a blank ``document_id`` to ``None`` (#2004).

        MEHO.Knowledge returns ``document_id: ""`` for a chunk with no
        owning-document concept. A required ``""`` would validate but lie;
        ``Optional`` alone keeps the empty string. Mapping blank-after-strip
        to ``None`` here makes the absence honest, so the citation-label
        fallback skips a cleanly-``None`` rung.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class CorpusSearchResponse(BaseModel):
    """Parsed corpus search result: the cited chunks for a query.

    Frozen adapter; ``chunks`` is the ordered hit list (best first, as
    the corpus ranks them). The consuming route (T3) projects these into
    MEHO's own cited-chunk surface and binds the central audit row.

    The hit list accepts **two** envelope names via a validation alias
    (#1732): MEHO.Knowledge's ``/search`` returns ``{"results": [...]}``
    while an earlier corpus shape (and MEHO's own internal projection)
    uses ``{"chunks": [...]}``. The field is **required** — it carries no
    default — so a 2xx body that names *neither* envelope fails parse
    loudly (surfaced as :class:`CorpusUnavailable`) rather than silently
    validating to an empty hit list. That fail-loud posture is the whole
    point of #1732: a populated corpus returning an unrecognised envelope
    must not read back as "zero hits". ``populate_by_name=True`` keeps the
    internal ``chunks`` name usable when constructing the model directly.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    chunks: list[CorpusChunk] = Field(validation_alias=AliasChoices("chunks", "results"))


async def search_corpus(
    operator: Operator,
    query: str,
    *,
    metadata_filters: dict[str, Any] | None = None,
    limit: int = 10,
    corpus_url: str | None = None,
    audience: str | None = None,
) -> CorpusSearchResponse:
    """Search the external corpus as *operator*, returning cited chunks.

    POSTs a JSON search request to *corpus_url* with
    ``Authorization: Bearer <operator.raw_jwt>`` so the corpus
    authenticates and audits the call as the operator. The request is
    bounded by ``settings.corpus_timeout_seconds`` across connect / read
    / write so a slow or hung corpus raises rather than blocking the
    event loop. *audience* (RFC 8707), when set, is forwarded as the
    requested resource indicator; the corpus may use it to bind the
    token to itself.

    Args:
        operator: The verified operator whose JWT is forwarded.
        query: The free-text search query.
        metadata_filters: Optional binary ``{key: scalar}`` narrowing
            (e.g. ``{"product": "vmware", "version": "9.0"}``). The
            mandatory product/version REQUIRE_FILTERS posture is enforced
            by the consuming route (T3, #1521), **not** here — this
            transport forwards whatever filters it is given.
        limit: Maximum number of chunks to request.
        corpus_url: The corpus search endpoint. ``None`` falls back to
            ``settings.corpus_url`` — the single-collection deploy that
            predates the per-collection backend router (T2 #1551). The
            ``corpus-http`` backend adapter passes the collection's
            ``backend.ref`` endpoint here so each collection can federate
            to its own corpus.
        audience: The RFC 8707 resource indicator to forward. ``None``
            falls back to ``settings.corpus_audience``; an empty string
            forwards no audience.

    Raises:
        CorpusUnavailable: when *corpus_url* is unset (unconfigured),
            the corpus is unreachable / times out, or it returns a
            non-2xx status. The upstream status is carried on the
            exception (``status``) for non-2xx responses; the raw
            response body is never included.
    """
    settings = get_settings()
    resolved_url = corpus_url if corpus_url is not None else settings.corpus_url
    if not resolved_url:
        # Fail-closed: an unconfigured corpus is unavailable, not empty.
        raise CorpusUnavailable("corpus_url is not configured")
    resolved_audience = audience if audience is not None else settings.corpus_audience

    # MEHO.Knowledge's ``/search`` reads ``top_k`` for the hit cap and
    # silently ignores ``limit`` (#1732) — sending ``limit`` let the
    # corpus fall back to its server-side default. Send the key the corpus
    # actually honours so ``limit`` reaches it.
    payload: dict[str, Any] = {"query": query, "top_k": limit}
    if metadata_filters:
        payload["metadata_filters"] = metadata_filters
    if resolved_audience:
        payload["audience"] = resolved_audience

    headers = {"Authorization": f"Bearer {operator.raw_jwt}"}
    timeout = httpx.Timeout(settings.corpus_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(resolved_url, json=payload, headers=headers)
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

    return _parse_2xx_body(response, CorpusSearchResponse, event_prefix="corpus")


class CorpusStatusResponse(BaseModel):
    """Parsed corpus readiness response — the liveness the probe reads back.

    The consumer-side adapter over the corpus's readiness endpoint. The
    probe Task (T6 #1555) reads ``index_built`` / ``doc_count`` /
    ``last_ingested_at`` here and the collection-probe route persists them
    onto the ``doc_collections`` row on success. ``extra="ignore"`` absorbs
    corpus fields MEHO does not consume.

    ``index_built`` is the managed-RAG footgun: ``False`` means the corpus
    is reachable but its ANN index is not yet answerable (a rebuild is in
    flight, or the corpus was registered but never ingested), so the
    search path can fail typed instead of returning an empty 200. Frozen.

    Readiness wire shape (#1732). MEHO.Knowledge has **no** ``/status``
    route; it exposes ``GET /readyz`` returning a ``HealthResponse`` whose
    200 *is* the "ready" signal — it need not carry an ``index_built``
    field. So this adapter:

    * accepts ``index_built`` under any of ``index_built`` / ``ready`` /
      ``index_ready`` (the names a health body may use), and
    * **defaults it to ``True``** — a corpus that answers its readiness
      probe with a 2xx and no explicit readiness flag is treated as
      answerable. A corpus that wants to advertise an in-flight rebuild
      still can, by returning the flag set ``False``.

    ``doc_count`` / ``last_ingested_at`` stay optional liveness, populated
    only when the health body carries them. ``populate_by_name=True`` keeps
    the canonical names usable when constructing the model directly.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    index_built: bool = Field(
        default=True,
        validation_alias=AliasChoices("index_built", "ready", "index_ready"),
    )
    doc_count: int | None = None
    last_ingested_at: datetime | None = None


def derive_status_url(search_url: str) -> str:
    """Derive the corpus readiness URL from its *search_url*.

    MEHO.Knowledge exposes its readiness as ``GET /readyz`` at the service
    root (no ``/status`` route, #1732), so the readiness URL is the search
    URL's **host root** plus ``/readyz``
    (``https://corpus/v1/search`` → ``https://corpus/readyz``). Anchoring
    at the root rather than swapping the final path segment matches a
    health endpoint that lives beside the API version prefix, not inside
    it. Query string and fragment are dropped — they are search-request
    shape, never part of the readiness endpoint.
    """
    parts = urlsplit(search_url)
    return urlunsplit((parts.scheme, parts.netloc, "/readyz", "", ""))


async def corpus_status(
    operator: Operator,
    *,
    corpus_url: str | None = None,
    audience: str | None = None,
) -> CorpusStatusResponse:
    """Read the external corpus's readiness as *operator* (T6 #1555).

    GETs the corpus readiness endpoint (:func:`derive_status_url` of the
    search URL) with ``Authorization: Bearer <operator.raw_jwt>`` so the
    corpus authenticates and audits the probe as the operator — the same
    forward-the-JWT contract as :func:`search_corpus`. Bounded by
    ``settings.corpus_timeout_seconds``; every fail-closed branch
    collapses to one :class:`CorpusUnavailable` so the probe route never
    persists a partial / stale liveness snapshot.

    Args:
        operator: The verified operator whose JWT is forwarded.
        corpus_url: The corpus *search* endpoint (the readiness URL is
            derived from it). ``None`` falls back to ``settings.corpus_url``
            — the legacy single-collection deploy. The ``corpus-http``
            backend adapter passes the collection's ``backend.ref``
            endpoint so each collection probes its own corpus.
        audience: The RFC 8707 resource indicator to forward as a query
            param. ``None`` falls back to ``settings.corpus_audience``; an
            empty string forwards none.

    Raises:
        CorpusUnavailable: when *corpus_url* is unset (unconfigured), the
            corpus is unreachable / times out, returns a non-2xx status,
            or returns a body that does not match
            :class:`CorpusStatusResponse`. The upstream status rides on
            the exception (``status``) for non-2xx; the raw body is never
            attached.
    """
    settings = get_settings()
    resolved_url = corpus_url if corpus_url is not None else settings.corpus_url
    if not resolved_url:
        # Fail-closed: an unconfigured corpus has no readiness to report.
        raise CorpusUnavailable("corpus_url is not configured")
    status_url = derive_status_url(resolved_url)
    resolved_audience = audience if audience is not None else settings.corpus_audience

    params = {"audience": resolved_audience} if resolved_audience else None
    headers = {"Authorization": f"Bearer {operator.raw_jwt}"}
    timeout = httpx.Timeout(settings.corpus_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(status_url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        _log.warning("corpus_status_unreachable", error=type(exc).__name__)
        raise CorpusUnavailable(f"corpus unreachable: {type(exc).__name__}") from exc

    if response.status_code // 100 != 2:
        _log.warning("corpus_status_request_failed", status=response.status_code)
        raise CorpusUnavailable(
            f"corpus returned HTTP {response.status_code}",
            status=response.status_code,
        )

    return _parse_2xx_body(response, CorpusStatusResponse, event_prefix="corpus_status")
