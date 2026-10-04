# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one ``ask_docs`` answer seam: upstream answer or local pipeline (#3911).

Every ``ask_docs`` face -- the REST route (``POST /api/v1/ask_docs``), the
``/ui/corpus`` Ask mode and the MCP ``ask_docs`` tool -- composes its answer
through :func:`answer_docs_question`. It picks one of two paths per
collection:

* **Upstream** -- the collection's backend offers its own grounded-answer
  endpoint and the collection opted in
  (:meth:`~meho_backplane.docs_search.backends.SearchBackend.supports_answer`;
  for ``corpus-http``, ``backend.ref["answer"] == "upstream"``). One call to
  :meth:`~meho_backplane.docs_search.backends.SearchBackend.answer`, no
  search call, and the backend's answer + citations are mapped into the
  ``ask_docs`` shape (:func:`_map_upstream_answer`). The backplane's own
  model is not called; the answer is composed by the backend's answer model.
* **Local** -- every other collection: the backplane's expand -> retrieve
  (one search per variant, RRF-merged) -> synthesize pipeline (#1916 /
  #1526), unchanged.

Both paths return the same :class:`AskPipelineOutcome` and classify a
failure into the same ``(leg, cause)`` envelope
(:mod:`~meho_backplane.docs_search.answer_errors`), so the faces stay
path-agnostic. ``answer_source`` on the outcome names the path for the log
and for the two face-level differences: the UI numbers its citation cards to
match the upstream ``[k]`` markers and falls back to one plain search on an
upstream failure, and MCP wraps the upstream answer text as untrusted.

Mapping an upstream answer
--------------------------

* **Hits** project through the same
  :func:`~meho_backplane.docs_search.service._project_chunk` as
  ``search_docs``, so source refs are identical. Their title comes from the
  page identity the backend sends
  (:func:`~meho_backplane.docs_search.citation_links.derive_chunk_title`).
* **Citations** are walked in response order, de-duplicated by
  ``chunk_id`` and matched to a hit **by** ``chunk_id`` -- never by position:
  the backend returns its hits reordered to citation order. A citation with
  no matching hit is a ``synthesis_malformed`` / ``citation_resolution``
  failure, which keeps the "every citation resolves to a retrieved chunk"
  invariant of the local path.
* **Markers**: the backend marks claims ``[N]`` with ``N`` its 0-based
  ``chunk_index``. Each becomes ``[k]``, the 1-based position of that chunk
  in the returned ``citations``; a marker with no matching citation is
  dropped.
* **No hits** answer :data:`~meho_backplane.docs_search.synthesis.NO_GROUNDED_ANSWER`
  with no citations, as the local path does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from meho_backplane.auth.corpus import UpstreamAnswer, UpstreamAnswerTiming, UpstreamHit
from meho_backplane.auth.operator import Operator
from meho_backplane.docs_search.answer_errors import (
    LEG_EXPAND,
    AskDocsAnswerError,
    classify_answer_error,
)
from meho_backplane.docs_search.backends import BackendRef, resolve_backend_or_label
from meho_backplane.docs_search.citation_links import derive_chunk_title
from meho_backplane.docs_search.expansion import expand_docs_query
from meho_backplane.docs_search.fanout import retrieve_multi_query
from meho_backplane.docs_search.service import DocsChunk, DocsScope, _project_chunk
from meho_backplane.docs_search.synthesis import (
    NO_GROUNDED_ANSWER,
    SYNTHESIS_CAUSE_CITATION_RESOLUTION,
    DocsAnswer,
    DocsSynthesisError,
    synthesize_docs_answer,
)

if TYPE_CHECKING:
    from meho_backplane.docs_collections import DocCollection

__all__ = [
    "ANSWER_SOURCE_LOCAL",
    "ANSWER_SOURCE_UPSTREAM",
    "AskPipelineOutcome",
    "answer_docs_question",
]

_log = structlog.get_logger(__name__)

#: ``answer_source`` of an answer composed by the backplane's own pipeline.
ANSWER_SOURCE_LOCAL: Final[str] = "local"
#: ``answer_source`` of an answer composed by the collection's backend.
ANSWER_SOURCE_UPSTREAM: Final[str] = "upstream"

#: An upstream ``[N]`` citation marker, with the horizontal whitespace before
#: it (so a dropped marker takes its leading space along). The digit run is
#: bounded so an absurd bracketed number is left as text, never parsed.
_UPSTREAM_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"([ \t]*)\[(\d{1,6})\]")


@dataclass(frozen=True)
class AskPipelineOutcome:
    """Outcome of an answer run, with the chunks retrieval returned.

    The structured (non-raising) return channel of
    :func:`answer_docs_question`. Exactly one of ``answer`` / ``error`` is set:

    * **success** -> ``answer`` is the grounded :class:`DocsAnswer`, ``error``
      is ``None``, ``retrieved_chunks`` is the retrieval the answer was
      grounded on (the answer's citations are a subset of these).
    * **leg failure** -> ``error`` is the classified
      :class:`~meho_backplane.docs_search.AskDocsAnswerError`, ``answer`` is
      ``None``. ``retrieved_chunks`` holds whatever retrieval returned
      *before* the failing leg: the real chunks for a **post-retrieval** leg
      (``synthesis_malformed`` / ``model_unavailable`` on the local path, a
      ``citation_resolution`` on the upstream path), and **empty** for a
      **pre-retrieval** leg (``expand_failed`` / ``corpus_unavailable``, and
      any upstream failure before the backend returned hits).

    ``answer_source`` names the path that ran: :data:`ANSWER_SOURCE_LOCAL`
    or :data:`ANSWER_SOURCE_UPSTREAM`. ``upstream_timing`` is the backend's
    own timing on the upstream path (``None`` otherwise).

    Everything here is an in-process channel only -- none of it is part of
    the :class:`~meho_backplane.docs_search.AskDocsAnswerError` wire envelope
    or the ``ask_docs`` response. ``retrieved_chunks`` exists so the
    ``/ui/corpus`` Ask BFF can render the grounding it already has when the
    answer fails, while the answer stays fail-closed.
    """

    answer: DocsAnswer | None = None
    error: AskDocsAnswerError | None = None
    retrieved_chunks: list[DocsChunk] = field(default_factory=list)
    answer_source: str = ANSWER_SOURCE_LOCAL
    upstream_timing: UpstreamAnswerTiming | None = None


async def answer_docs_question(
    operator: Operator,
    query: str,
    *,
    scope: DocsScope,
    collection: DocCollection,
    limit: int,
) -> AskPipelineOutcome:
    """Answer *query* over *collection*: the one seam every ``ask_docs`` face calls.

    Resolves the collection's backend and takes the **upstream** path when the
    backend supports an answer for the collection's ``backend.ref``
    (:meth:`~meho_backplane.docs_search.backends.SearchBackend.supports_answer`),
    otherwise the **local** expand -> retrieve -> synthesize pipeline. An
    unroutable collection takes the local path, which fails it on the
    ``corpus_unavailable`` leg exactly as before.

    The caller has already resolved, entitled and readiness-checked
    *collection* and bound the audit row; this never touches either.

    Returns:
        An :class:`AskPipelineOutcome`: a grounded answer, or the classified
        leg failure plus whatever was retrieved before it. A leg failure
        never yields an answer (fail-closed).

    Raises:
        Exception: a non-leg (genuinely unexpected) exception propagates
            unchanged, so a real fault still surfaces as a generic 500 /
            ``-32603`` rather than a mis-labelled leg failure.
    """
    resolved, _label, _message = resolve_backend_or_label(collection)
    if resolved is not None and resolved.backend.supports_answer(resolved.ref):
        outcome = await _answer_upstream(
            operator, query, resolved=resolved, scope=scope, limit=limit
        )
    else:
        outcome = await _answer_locally(
            operator, query, scope=scope, collection=collection, limit=limit
        )
    if outcome.answer is not None:
        timing = outcome.upstream_timing
        _log.info(
            "docs_ask_completed",
            operator_sub=operator.sub,
            collection_key=scope.collection_key,
            answer_source=outcome.answer_source,
            hit_count=len(outcome.retrieved_chunks),
            citation_count=len(outcome.answer.citations),
            upstream_total_ms=timing.total_ms if timing is not None else None,
            upstream_llm_ms=timing.llm_ms if timing is not None else None,
        )
    return outcome


async def _answer_upstream(
    operator: Operator,
    query: str,
    *,
    resolved: BackendRef,
    scope: DocsScope,
    limit: int,
) -> AskPipelineOutcome:
    """Run the upstream path: one backend answer call, mapped (#3911).

    A failed call is classified into its ``(leg, cause)`` by the shared
    :func:`~meho_backplane.docs_search.classify_answer_error` (the transport's
    :class:`~meho_backplane.auth.corpus.CorpusUnavailable` /
    :class:`~meho_backplane.auth.corpus.CorpusAnswerError`). A citation that
    does not resolve to a returned hit is the ``synthesis_malformed`` /
    ``citation_resolution`` failure; the hits ride the outcome.
    """
    try:
        upstream = await resolved.backend.answer(
            operator, query, backend_ref=resolved.ref, limit=limit
        )
    except Exception as exc:
        error = _classify_or_reraise(exc)
        _log_upstream_failure(operator, scope, error)
        return AskPipelineOutcome(error=error, answer_source=ANSWER_SOURCE_UPSTREAM)

    hits = [
        _project_upstream_hit(hit, collection_key=scope.collection_key) for hit in upstream.hits
    ]
    try:
        answer = _map_upstream_answer(upstream, hits)
    except DocsSynthesisError as exc:
        error = _classify_or_reraise(exc)
        _log_upstream_failure(operator, scope, error)
        return AskPipelineOutcome(
            error=error,
            retrieved_chunks=hits,
            answer_source=ANSWER_SOURCE_UPSTREAM,
            upstream_timing=upstream.timing,
        )
    return AskPipelineOutcome(
        answer=answer,
        retrieved_chunks=hits,
        answer_source=ANSWER_SOURCE_UPSTREAM,
        upstream_timing=upstream.timing,
    )


def _project_upstream_hit(hit: UpstreamHit, *, collection_key: str) -> DocsChunk:
    """Project an upstream hit like a ``search_docs`` hit, with a derived title.

    The title rule (:func:`~meho_backplane.docs_search.citation_links.derive_chunk_title`)
    reads the page identity only the answer path carries today; the source
    ref goes through the same ``_project_chunk`` as ``search_docs``.
    """
    title = derive_chunk_title(
        title=hit.title,
        heading_path=hit.heading_path,
        breadcrumb=hit.breadcrumb,
        filename=hit.filename,
    )
    return _project_chunk(hit.model_copy(update={"title": title}), collection_key=collection_key)


def _map_upstream_answer(upstream: UpstreamAnswer, hits: list[DocsChunk]) -> DocsAnswer:
    """Map an upstream answer onto a :class:`DocsAnswer` over *hits*.

    Citations are walked in response order, de-duplicated by ``chunk_id`` and
    matched to *hits* by ``chunk_id`` (never by position). Each upstream
    ``[N]`` marker (``N`` = the citation's ``chunk_index``) is rewritten to
    ``[k]``, the 1-based position of its chunk in the returned citations;
    markers with no citation are dropped. No hits and no citations is the
    deterministic :data:`NO_GROUNDED_ANSWER`.

    Raises:
        DocsSynthesisError: a citation names a ``chunk_id`` the backend did
            not return as a hit (``cause=citation_resolution``).
    """
    if not hits and not upstream.citations:
        return DocsAnswer(answer=NO_GROUNDED_ANSWER, citations=[])

    by_id: dict[str, DocsChunk] = {}
    for hit in hits:
        by_id.setdefault(hit.chunk_id, hit)

    citations: list[DocsChunk] = []
    number_by_id: dict[str, int] = {}
    number_by_index: dict[int, int] = {}
    unknown: list[str] = []
    for citation in upstream.citations:
        chunk = by_id.get(citation.chunk_id)
        if chunk is None:
            unknown.append(citation.chunk_id)
            continue
        number = number_by_id.get(citation.chunk_id)
        if number is None:
            citations.append(chunk)
            number = len(citations)
            number_by_id[citation.chunk_id] = number
        number_by_index.setdefault(citation.chunk_index, number)
    if unknown:
        raise DocsSynthesisError(
            f"upstream answer cited chunk id(s) not in its returned hits: {unknown}",
            cause=SYNTHESIS_CAUSE_CITATION_RESOLUTION,
        )
    return DocsAnswer(
        answer=_renumber_markers(upstream.answer, number_by_index),
        citations=citations,
    )


def _renumber_markers(text: str, number_by_index: dict[int, int]) -> str:
    """Rewrite upstream ``[N]`` markers to the backplane's ``[k]`` numbering.

    ``N`` is the backend's 0-based ``chunk_index``; ``k`` is the 1-based
    position of that chunk in the mapped citations. A marker whose ``N`` has
    no citation is removed together with the spaces before it.
    """

    def _replace(match: re.Match[str]) -> str:
        number = number_by_index.get(int(match.group(2)))
        if number is None:
            return ""
        return f"{match.group(1)}[{number}]"

    return _UPSTREAM_MARKER_RE.sub(_replace, text)


def _log_upstream_failure(operator: Operator, scope: DocsScope, error: AskDocsAnswerError) -> None:
    """Log a failed upstream answer by leg + cause (never the query or a body)."""
    _log.warning(
        "docs_ask_upstream_failed",
        operator_sub=operator.sub,
        collection_key=scope.collection_key,
        answer_source=ANSWER_SOURCE_UPSTREAM,
        leg=error.leg,
        cause=error.cause,
        upstream_status=error.upstream_status,
        retry_after=error.retry_after,
    )


async def _answer_locally(
    operator: Operator,
    query: str,
    *,
    scope: DocsScope,
    collection: DocCollection,
    limit: int,
) -> AskPipelineOutcome:
    """Run the local expand -> retrieve -> synthesize pipeline (#1916).

    Each leg is classified by the shared
    :func:`~meho_backplane.docs_search.classify_answer_error`; the one
    ambiguous failure --
    :class:`~meho_backplane.operations.ingest.LlmClientUnavailable` from the
    shared #1386 client -- is pinned to ``expand_failed`` on the expand leg
    and to the default ``model_unavailable`` on the synthesis leg. An empty
    retrieval is **not** a failure: synthesis short-circuits to the
    deterministic "no grounded answer" without a model call.
    """
    # 1. Expand: rewrite the question into corpus-aware variants. Both an
    # unconfigured model (LlmClientUnavailable) and unusable output
    # (DocsQueryExpansionError) name the ``expand_failed`` leg. This is a
    # *pre-retrieval* leg: no chunks exist yet, so the outcome carries none.
    try:
        variants = await expand_docs_query(query, collection)
    except Exception as exc:
        return AskPipelineOutcome(error=_classify_or_reraise(exc, llm_unavailable_leg=LEG_EXPAND))

    # 2. Retrieve per variant on the same backend and RRF-merge. A down /
    # unconfigured backend (CorpusUnavailable) names the ``corpus_unavailable``
    # leg -- also *pre-retrieval* for fail-open purposes: the retrieval call
    # itself failed, so there are no chunks to surface.
    try:
        retrieval = await retrieve_multi_query(
            operator, variants, scope=scope, collection=collection, limit=limit
        )
    except Exception as exc:
        return AskPipelineOutcome(error=_classify_or_reraise(exc))

    # 3. Synthesize over the merged chunks, answering the operator's
    # *original* question. An unconfigured model names ``model_unavailable``;
    # output breaking the grounding contract names ``synthesis_malformed``
    # (with the parse / citation-resolution sub-cause). Both are
    # *post-retrieval* legs: retrieval already succeeded, so the outcome
    # carries the real ``retrieval.chunks`` for the BFF to fail open to.
    try:
        answer = await synthesize_docs_answer(query, retrieval)
    except Exception as exc:
        return AskPipelineOutcome(
            error=_classify_or_reraise(exc),
            retrieved_chunks=list(retrieval.chunks),
        )
    return AskPipelineOutcome(answer=answer, retrieved_chunks=list(retrieval.chunks))


def _classify_or_reraise(
    exc: Exception,
    *,
    llm_unavailable_leg: str | None = None,
) -> AskDocsAnswerError:
    """Classify *exc* as a leg-named :class:`AskDocsAnswerError`, or re-raise.

    A recognised answer-pipeline leg failure is **returned** as an
    :class:`AskDocsAnswerError` with ``__cause__`` chained to *exc*, so the
    traceback survives a later re-raise. Anything else is **re-raised
    unchanged** so a genuinely unexpected fault still propagates rather than
    being mis-labelled a leg failure.
    """
    classified = (
        classify_answer_error(exc, llm_unavailable_leg=llm_unavailable_leg)
        if llm_unavailable_leg is not None
        else classify_answer_error(exc)
    )
    if classified is None:
        raise exc
    classified.__cause__ = exc
    return classified
