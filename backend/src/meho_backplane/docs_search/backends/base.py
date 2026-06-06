# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The backend-agnostic search-backend interface (G4.6-T2 #1551).

A :class:`SearchBackend` is the seam that lets one doc collection sit on a
managed RAG and another on ``meho-knowledge`` / Qdrant **behind the same
``search_docs``** — the agent never sees which backend answered. The
``collection → backend{type, ref}`` router
(:func:`~meho_backplane.docs_search.backends.resolver.resolve_backend`)
maps a collection's ``backend.type`` to one of these adapters; the chosen
adapter does the actual federation and returns the corpus-shaped
:class:`~meho_backplane.auth.corpus.CorpusSearchResponse` the
``search_docs`` service already projects into MEHO's cited-chunk surface.

The interface is deliberately the **same shape** the existing
:func:`~meho_backplane.auth.corpus.search_corpus` transport already had —
``async search(operator, query, *, metadata_filters, limit)`` returning a
:class:`CorpusSearchResponse` — so re-homing today's single-corpus client
as the first adapter (``CorpusHttpBackend``) is behaviour-preserving and
the ``CorpusChunk → DocsChunk`` projection downstream is untouched.

Adapters are **stateless singletons**: one instance per ``backend_type``
is registered at import time (mirroring the connector registry,
:mod:`meho_backplane.connectors.registry`). Per-collection configuration
(endpoint, audience) is **not** adapter state — it rides on the
collection's ``backend.ref`` and is passed to :meth:`search` per call, so
a single ``CorpusHttpBackend`` instance serves every collection routed to
the ``corpus-http`` type.

:meth:`probe` is a forward seam for the readiness probe (T6 #1555). It
defaults to raising :class:`NotImplementedError` so an adapter that has
not implemented readiness reporting fails loudly rather than silently
claiming "ready"; T6 overrides it on the adapters that gain a liveness
endpoint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from meho_backplane.auth.corpus import CorpusSearchResponse
from meho_backplane.auth.operator import Operator

__all__ = ["SearchBackend"]


class SearchBackend(ABC):
    """Abstract search backend an entitled doc collection routes to.

    Subclasses advertise themselves with a class-level
    :attr:`backend_type` discriminator — the string the registry keys on
    and the value a collection's ``backend.type`` selects. The router
    does a **direct dict lookup** on that string (no version tie-break
    ladder — collections bind to exactly one backend by construction).

    One required async method, :meth:`search`, plus an optional
    :meth:`probe` forward seam for T6. The signature mirrors the
    re-homed corpus client so the ``search_docs`` service swaps its
    direct transport call for ``backend.search(...)`` with the request
    shape unchanged.
    """

    #: The routing discriminator. A collection whose ``backend.type``
    #: equals this string resolves to this adapter. Set on every concrete
    #: subclass; the registry rejects a duplicate registration of the
    #: same type as a programming bug.
    backend_type: str

    @abstractmethod
    async def search(
        self,
        operator: Operator,
        query: str,
        *,
        backend_ref: Mapping[str, Any] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> CorpusSearchResponse:
        """Search this backend as *operator*, returning corpus-shaped chunks.

        Args:
            operator: The verified operator. An adapter that federates to
                an operator-audited backend forwards the operator JWT
                (``operator.raw_jwt``); an adapter that authenticates with
                its own service credentials (a later Task) uses *operator*
                only for scoping / logging.
            query: The free-text search query.
            backend_ref: The collection's ``backend.ref`` — the
                per-collection routing detail (endpoint, audience, index
                id, …) the adapter needs beyond its type. ``None`` selects
                the adapter's legacy / default configuration (the
                single-collection deploy that predates the registry).
            metadata_filters: Optional binary ``{key: scalar}`` narrowing
                (e.g. ``{"product": "vmware", "version": "9.0"}``). The
                mandatory-filter posture is enforced by the caller, not
                here — the adapter forwards whatever it is given.
            limit: Maximum number of chunks to request.

        Returns:
            A :class:`~meho_backplane.auth.corpus.CorpusSearchResponse`
            of ranked cited chunks (best first).

        Raises:
            CorpusUnavailable: when the backend is unconfigured,
                unreachable, or returns a malformed / non-2xx response.
                Every fail-closed branch collapses to this one typed
                error so the ``search_docs`` route maps it to HTTP 503
                without branching on the cause (no new error taxonomy).
        """
        raise NotImplementedError

    async def probe(
        self,
        backend_ref: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Report this backend's readiness for *backend_ref* (T6 #1555 seam).

        Not implemented in T2. Defaults to raising so an adapter without
        a liveness endpoint fails loudly rather than silently claiming
        "ready"; the readiness probe Task (T6 #1555) overrides it on the
        adapters that gain a liveness check.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement readiness probing")
