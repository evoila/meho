# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-call log fields for the docs surfaces (#3915).

Every docs call (``search_docs`` and ``ask_docs``, on the REST, MCP and
``/ui/corpus`` faces) logs **which** chunks came back and which of them an
answer cited, so a wrong answer can be reconstructed from the logs: did
retrieval miss the right page, or did the answer leg judge it too thin?
This module builds those fields in one place, so the search events and
both ask paths (the local pipeline and an upstream answer endpoint) carry
the same keys.

What the fields carry
---------------------
* :func:`hit_log_fields`: ``hit_count``, ``hit_chunk_ids`` (in rank order,
  capped at :data:`MAX_LOGGED_IDS`) and ``hit_source_refs``, the
  ``source_url`` of each of those hits in the same order. A ``source_url``
  is already normalised when the chunk is projected
  (:func:`~meho_backplane.docs_search.citation_links.normalize_source_ref`):
  a public URL or an opaque ``meho://docs/<collection>/<chunk_id>`` ref,
  never a storage path such as ``gs://``. A fan-out over several
  collections also lists ``hit_collections``, the source collection of each
  listed hit, because a chunk id is unique only within its collection.
* :func:`ask_log_fields`: the hit fields plus ``citation_count``,
  ``cited_chunk_ids``, ``answer_source`` (``local`` or ``upstream``) and,
  when an upstream answer reports them, ``upstream_total_ms`` /
  ``upstream_llm_ms``. Both ask paths log them on ``docs_ask_completed``
  (:func:`~meho_backplane.docs_search.answer.answer_docs_question`); the
  local pipeline's ``docs_ask_synthesized`` / ``docs_ask_no_grounding``
  carry them too.

None of them carries chunk text, answer text or query text.

Query text only behind a flag
-----------------------------
The question is never logged in the clear: the audit row stores its
SHA-256. The query strings a call actually ran (the local expansion
variants) are logged only when ``DOCS_DEBUG_LOG_QUERY_TEXT=true``, by
:func:`log_query_text`, as one ``docs_query_text`` record at debug severity.
The upstream answer path logs no query text yet: the backend does not return
its rewritten query until evoila-bosnia/MEHO.Knowledge#513, and then that
query goes through the same helper with ``source="upstream_rewrite"``. The
backplane's log floor is INFO
(:func:`~meho_backplane.logging.configure_logging`), so that record is
written through a logger of its own with a DEBUG floor: the flag, not the
process log level, decides whether it appears. With the flag off (the
default) nothing is written.

Scoped zero hits
----------------
:func:`note_scoped_zero_hits` logs a ``docs_search_scoped_zero_hits``
warning and increments ``docs_search_scoped_zero_hits_total`` when a search
that asked for a ``product`` or ``version`` returned no chunks. Once the
corpus honours those filters, a vocabulary mismatch (``vcenter`` sent to a
corpus that stamps ``vsphere``) looks exactly like that. The chart's
optional PrometheusRule alerts on the counter (``MehoDocsScopedZeroHits``).
The counter has no labels: ``/metrics`` can be unauthenticated, and the
collection key, product and version are on the warning line instead.
It counts backend searches, not calls: a local ``ask_docs`` runs one search
per expansion variant, so a scoped ask that finds nothing counts up to four
times.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from prometheus_client import Counter

from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.docs_search.service import DocsChunk, DocsScope

__all__ = [
    "DOCS_SEARCH_SCOPED_ZERO_HITS_TOTAL",
    "MAX_LOGGED_IDS",
    "AnswerSource",
    "QueryTextSource",
    "ask_log_fields",
    "hit_log_fields",
    "log_query_text",
    "note_scoped_zero_hits",
]

#: How many chunk ids (and source refs) one log record lists. The request
#: ``limit`` is capped at 50 on every face, so this only bounds a backend
#: that returns more than it was asked for.
MAX_LOGGED_IDS: Final[int] = 50

#: Where an ask's answer was composed: ``local`` is the backplane's own
#: expand, retrieve and synthesize pipeline; ``upstream`` is the
#: collection backend's answer endpoint.
AnswerSource = Literal["local", "upstream"]

#: Which query strings a :func:`log_query_text` record carries:
#: ``expansion`` is the local expansion variants (the operator's question
#: first); ``upstream_rewrite`` is reserved for the rewritten query an
#: upstream answer endpoint will report (not wired yet: it needs the backend
#: to return it, evoila-bosnia/MEHO.Knowledge#513).
QueryTextSource = Literal["expansion", "upstream_rewrite"]

#: Docs searches that requested a ``product`` or ``version`` and returned no
#: chunks. Module-level singleton, like every ``prometheus_client`` metric.
DOCS_SEARCH_SCOPED_ZERO_HITS_TOTAL: Counter = Counter(
    "docs_search_scoped_zero_hits_total",
    "Docs searches that requested a product or version and returned no chunks (#3915).",
)

_log = structlog.get_logger(__name__)


def _query_text_logger() -> Any:
    """Build the ``docs_query_text`` logger: the global processors, a DEBUG floor.

    The process-wide floor is INFO, which drops every ``debug`` call on an
    ordinary logger. This one keeps the configured processors and output but
    has a DEBUG floor of its own, so the record is written whenever
    :func:`log_query_text` lets it through.
    """
    return structlog.wrap_logger(
        None,
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )


_query_text_log = _query_text_logger()


def hit_log_fields(
    chunks: Sequence[DocsChunk], *, with_collections: bool = False
) -> dict[str, Any]:
    """Return the hit fields for a list of retrieved chunks, in rank order.

    With *with_collections* (a fan-out over several collections) the fields
    also list ``hit_collections``, each listed hit's source collection key,
    so a chunk id two collections share stays unambiguous.
    """
    logged = chunks[:MAX_LOGGED_IDS]
    fields: dict[str, Any] = {
        "hit_count": len(chunks),
        "hit_chunk_ids": [chunk.chunk_id for chunk in logged],
        "hit_source_refs": [chunk.source_url for chunk in logged],
    }
    if with_collections:
        fields["hit_collections"] = [chunk.collection for chunk in logged]
    return fields


def ask_log_fields(
    *,
    answer_source: AnswerSource,
    hits: Sequence[DocsChunk],
    citations: Sequence[DocsChunk],
    upstream_total_ms: float | None = None,
    upstream_llm_ms: float | None = None,
) -> dict[str, Any]:
    """Return the fields of an ask's completion record.

    *hits* are the chunks the answer was composed over and *citations* the
    ones it cited. The upstream timings are included only when given.
    """
    fields: dict[str, Any] = {
        "answer_source": answer_source,
        **hit_log_fields(hits),
        "citation_count": len(citations),
        "cited_chunk_ids": [chunk.chunk_id for chunk in citations[:MAX_LOGGED_IDS]],
    }
    if upstream_total_ms is not None:
        fields["upstream_total_ms"] = upstream_total_ms
    if upstream_llm_ms is not None:
        fields["upstream_llm_ms"] = upstream_llm_ms
    return fields


def log_query_text(
    *,
    source: QueryTextSource,
    collection_key: str,
    queries: Sequence[str],
) -> None:
    """Log the query strings a docs call ran, only under the opt-in flag.

    Writes one debug-severity ``docs_query_text`` record when
    ``DOCS_DEBUG_LOG_QUERY_TEXT`` is true, and nothing otherwise.
    """
    if not get_settings().docs_debug_log_query_text:
        return
    _query_text_log.debug(
        "docs_query_text",
        source=source,
        collection_key=collection_key,
        queries=list(queries),
    )


def note_scoped_zero_hits(*, operator_sub: str, scope: DocsScope, hit_count: int) -> None:
    """Warn and count when a search that set ``product`` or ``version`` found nothing."""
    if hit_count > 0 or (scope.product is None and scope.version is None):
        return
    DOCS_SEARCH_SCOPED_ZERO_HITS_TOTAL.inc()
    _log.warning(
        "docs_search_scoped_zero_hits",
        operator_sub=operator_sub,
        collection_key=scope.collection_key,
        product=scope.product,
        version=scope.version,
    )
