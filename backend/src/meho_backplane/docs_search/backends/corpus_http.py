# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The first concrete search backend: the JWT-forward corpus client (G4.6-T2 #1551).

:class:`CorpusHttpBackend` re-homes today's single-corpus federation
client (:func:`~meho_backplane.auth.corpus.search_corpus`) behind the
:class:`~meho_backplane.docs_search.backends.base.SearchBackend`
interface, so the ``collection → backend`` router can select it by type
exactly like any future adapter. It wraps the existing, well-tested
transport rather than duplicating the httpx body — every fail-closed
property (forwarded ``raw_jwt``, bounded timeout, one typed
:class:`~meho_backplane.auth.corpus.CorpusUnavailable`) is inherited
unchanged.

This adapter fronts **whatever backend the ops corpus itself proxies** —
it is the operator-JWT-forward path, not a backend-specific client. A
second adapter that talks a managed RAG directly with its own service-
account auth (e.g. ``vertex-rag``) is a deliberate later Task; it is
**not** built here (see #1548 out-of-scope).

Per-collection routing
----------------------

The adapter is a stateless singleton; per-collection configuration rides
on the collection's ``backend.ref`` and is passed per call:

* ``backend.ref["endpoint"]`` (alias ``url``) — the corpus search URL for
  this collection. Absent → the legacy ``settings.corpus_url`` global, so
  an unmigrated single-collection deploy keeps working with no ``ref``.
* ``backend.ref["audience"]`` — the RFC 8707 resource indicator for this
  collection. Absent → ``settings.corpus_audience``.

A ``backend.ref`` that names neither (and a deploy whose legacy
``corpus_url`` is also empty) is **unconfigured** and fails closed with
:class:`CorpusUnavailable` — the same 503 arm as today, no new taxonomy.

Readiness + per-project rebuild serialization (T6 #1555)
-------------------------------------------------------

:meth:`probe` reads the corpus's readiness (:func:`corpus_status`) and
maps it to a typed
:class:`~meho_backplane.docs_search.backends.base.BackendReadiness`.

The managed-RAG "rebuilds serialize per project" constraint lives
**inside this adapter** — a per-project :class:`asyncio.Lock` the probe
holds while it talks to the corpus — rather than as a substrate-level
scheduler (substrate minimalism, #1177). The lock key is the resolved
corpus endpoint, so two concurrent probes against the *same* project's
backend serialize while probes against *different* projects run
concurrently; the serialized state is surfaced to operators via the
``status='rebuilding'`` column the probe route writes, not via a new
queue primitive.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from meho_backplane.auth.corpus import (
    CorpusSearchResponse,
    corpus_status,
    search_corpus,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.docs_search.backends.base import BackendReadiness, SearchBackend
from meho_backplane.settings import get_settings

__all__ = ["CORPUS_HTTP_BACKEND_TYPE", "CorpusHttpBackend"]

#: The routing discriminator for the JWT-forward corpus client. A
#: collection whose ``backend.type`` equals this string resolves to
#: :class:`CorpusHttpBackend`. Named for the transport (operator-JWT-
#: forward over HTTP), not for whatever the ops corpus proxies behind it.
CORPUS_HTTP_BACKEND_TYPE = "corpus-http"


class CorpusHttpBackend(SearchBackend):
    """JWT-forward corpus client behind the :class:`SearchBackend` seam.

    Behaviourally identical to a direct
    :func:`~meho_backplane.auth.corpus.search_corpus` call: forwards the
    operator JWT, bounds the request by ``settings.corpus_timeout_seconds``,
    and fails closed to one :class:`CorpusUnavailable`. The only addition
    is per-collection endpoint / audience resolution from ``backend.ref``.
    """

    backend_type = CORPUS_HTTP_BACKEND_TYPE

    def __init__(self) -> None:
        # Per-project rebuild serialization. One lock per resolved corpus
        # endpoint (a project's backend), minted on first use. A
        # ``defaultdict`` rather than an explicit pre-seed because the set
        # of collections is operator-data, not known at construction; the
        # adapter is a process-singleton so this map is per-process,
        # exactly the scope "serialize per project" needs (no cross-pod
        # coordination — that would be the substrate scheduler #1177 rules
        # out). The map only grows with the number of distinct endpoints,
        # which is bounded by the collection count.
        self._project_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def search(
        self,
        operator: Operator,
        query: str,
        *,
        backend_ref: Mapping[str, Any] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> CorpusSearchResponse:
        """Federate the query to this collection's corpus as *operator*.

        Resolves the endpoint and audience from *backend_ref*, falling
        back to the legacy ``settings.corpus_url`` / ``corpus_audience``
        globals when absent, then delegates to the shared transport.
        """
        endpoint, audience = _resolve_endpoint_audience(backend_ref)
        return await search_corpus(
            operator,
            query,
            metadata_filters=metadata_filters,
            limit=limit,
            corpus_url=endpoint,
            audience=audience,
        )

    async def probe(
        self,
        operator: Operator,
        *,
        backend_ref: Mapping[str, Any] | None = None,
    ) -> BackendReadiness:
        """Read this collection's corpus readiness, serialized per project.

        Resolves the endpoint / audience from *backend_ref* (legacy
        globals when absent) and reads the corpus's readiness via
        :func:`~meho_backplane.auth.corpus.corpus_status`, holding the
        per-project lock for the resolved endpoint so two concurrent
        probes against the same project's backend serialize (the
        "rebuilds serialize per project" constraint, in-adapter). A
        reachable corpus whose ANN index is not yet built maps to
        ``index_built=False`` so the probe route writes ``rebuilding`` /
        ``provisioning`` rather than ``ready``.

        Propagates :class:`~meho_backplane.auth.corpus.CorpusUnavailable`
        on every fail-closed branch (unconfigured / unreachable / non-2xx
        / malformed) so the route persists nothing on a failed probe
        (success-only write-back).
        """
        endpoint, audience = _resolve_endpoint_audience(backend_ref)
        # Key the serialization lock on the resolved project endpoint. The
        # empty-string key covers the legacy single-collection deploy (one
        # global corpus) so even its concurrent probes serialize.
        lock_key = endpoint or ""
        async with self._project_locks[lock_key]:
            status = await corpus_status(
                operator,
                corpus_url=endpoint,
                audience=audience,
            )
        return BackendReadiness(
            reachable=True,
            index_built=status.index_built,
            doc_count=status.doc_count,
            last_ingested_at=status.last_ingested_at,
            detail={"probe_method": "corpus-status"},
        )

    def is_configured(self) -> bool:
        """Whether the corpus endpoint is configured at the deploy level.

        The coarse ``/ready`` signal: ``settings.corpus_url`` set. This is
        the legacy global endpoint — a deploy with per-collection
        ``backend.ref`` endpoints but no global ``corpus_url`` still routes
        per-collection at search time, but the deploy-level reachability
        gate keys off the global config the unmigrated path needs. A
        credential-free, synchronous check: no JWT, no round-trip.
        """
        return bool(get_settings().corpus_url)


def _resolve_endpoint_audience(
    backend_ref: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Extract ``(endpoint, audience)`` overrides from *backend_ref*.

    Returns ``None`` for either value the ref does not name so the
    transport falls back to the legacy global setting (the single-
    collection deploy). ``endpoint`` accepts ``"endpoint"`` or the
    shorter ``"url"`` alias; only non-blank string values are honoured so
    a ``ref`` carrying ``{"endpoint": ""}`` does not mask the legacy
    fallback with an empty override.
    """
    if not backend_ref:
        return None, None
    endpoint = _str_or_none(backend_ref.get("endpoint")) or _str_or_none(backend_ref.get("url"))
    audience = _str_or_none(backend_ref.get("audience"))
    return endpoint, audience


def _str_or_none(value: Any) -> str | None:
    """Return *value* as a stripped non-empty str, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
