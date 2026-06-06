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
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.auth.corpus import CorpusSearchResponse, search_corpus
from meho_backplane.auth.operator import Operator
from meho_backplane.docs_search.backends.base import SearchBackend

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
