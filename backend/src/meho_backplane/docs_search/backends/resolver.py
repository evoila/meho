# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``collection → backend`` router (G4.6-T2 #1551).

:func:`resolve_backend` is the seam that maps a doc collection to the
concrete :class:`~meho_backplane.docs_search.backends.base.SearchBackend`
that answers its queries. It reads the collection's ``backend.type``
(the operator-set ``{type, ref}`` routing record on the ``doc_collections``
row, T1 #1550) and does a **direct dict lookup** in the registry — no
version tie-break ladder, because a collection binds to exactly one
backend by construction (#1548 design decision 3).

Two entry points, mirroring the connector resolver's
``resolve_connector`` / ``resolve_connector_or_label`` split
(:mod:`meho_backplane.connectors.resolver`):

* :func:`resolve_backend` — returns the adapter or raises
  :class:`~meho_backplane.auth.corpus.CorpusUnavailable`. The seam in
  ``docs_search.search_docs`` uses this directly: an unroutable
  collection collapses to the **existing** 503 arm, no new error
  taxonomy (#1551 acceptance).
* :func:`resolve_backend_or_label` — the non-raising ``(impl, label,
  msg)`` shape future callers (T5 fan-out, T6 readiness) branch on
  without a try/except, exactly like
  :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`.

Legacy single-collection deploy
-------------------------------

``collection=None`` is the **unmigrated** path: a deploy that has not yet
adopted the ``doc_collections`` registry and routes every ``search_docs``
query to the one global ``corpus_url``. It resolves to the
``corpus-http`` adapter with no ``backend.ref``, so the adapter falls
back to the legacy global settings. This keeps T2 a pure router-insertion
that does not require T3's required-``collection`` request param to ship
first (#1548 / #1551 scope split).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import structlog

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.docs_search.backends.base import SearchBackend
from meho_backplane.docs_search.backends.corpus_http import CORPUS_HTTP_BACKEND_TYPE
from meho_backplane.docs_search.backends.registry import get_backend

__all__ = [
    "BackendRef",
    "ResolvedBackend",
    "resolve_backend",
    "resolve_backend_or_label",
]

_log = structlog.get_logger(__name__)

#: The label arm of the non-raising resolver. ``"unknown_backend"`` ==
#: the collection's ``backend.type`` names no registered adapter (or the
#: routing record is malformed / missing a type). One arm only — there is
#: no ambiguity case to disambiguate (direct lookup, no tie-break).
BackendLabel = Literal["unknown_backend"]


class BackendRef:
    """A resolved ``(adapter, ref)`` pair the seam calls ``search`` on.

    Bundles the chosen :class:`SearchBackend` with the collection's
    ``backend.ref`` so the caller passes a single object to the adapter
    instead of re-reading the ref. ``ref`` is ``None`` for the legacy
    single-collection path.
    """

    __slots__ = ("backend", "ref")

    def __init__(self, backend: SearchBackend, ref: Mapping[str, Any] | None) -> None:
        self.backend = backend
        self.ref = ref


#: Backwards-friendly alias used in type hints across the seam.
ResolvedBackend = BackendRef


def _read_routing_record(
    collection: Any | None,
) -> tuple[str, Mapping[str, Any] | None] | None:
    """Read ``(backend_type, backend_ref)`` from *collection*.

    Returns the legacy default ``(corpus-http, None)`` for
    ``collection=None`` (unmigrated deploy). Returns ``None`` when the
    collection's ``backend`` record is present but unroutable — missing
    or non-string ``type`` — so the caller maps it to the unconfigured
    arm. A non-mapping ``ref`` is normalised to ``None``.
    """
    if collection is None:
        return CORPUS_HTTP_BACKEND_TYPE, None

    backend_record = getattr(collection, "backend", None)
    if not isinstance(backend_record, Mapping):
        return None

    backend_type = backend_record.get("type")
    if not isinstance(backend_type, str) or not backend_type:
        return None

    ref = backend_record.get("ref")
    ref_mapping = ref if isinstance(ref, Mapping) else None
    return backend_type, ref_mapping


def resolve_backend(collection: Any | None) -> BackendRef:
    """Resolve *collection* to its search backend, fail-closed.

    Reads ``collection.backend.type`` and looks the adapter up in the
    registry by direct dict lookup. ``collection=None`` routes to the
    legacy ``corpus-http`` adapter with no ref (the single-collection
    deploy that predates the registry).

    Args:
        collection: The resolved doc collection (the
            :class:`~meho_backplane.docs_collections.DocCollection` read
            shape or the ORM row — anything exposing a ``backend``
            mapping with ``{type, ref}``), or ``None`` for the legacy
            single-collection path.

    Returns:
        A :class:`BackendRef` bundling the chosen adapter with the
        collection's ``backend.ref``.

    Raises:
        CorpusUnavailable: when ``backend.type`` is missing / malformed
            or names no registered adapter. This is the **existing** 503
            arm (no new error taxonomy) — an unroutable collection is
            "search unavailable", not a distinct failure mode the agent
            sees.
    """
    backend, label, message = resolve_backend_or_label(collection)
    if label is not None:
        # Unroutable → the existing fail-closed 503 arm. The message names
        # the offending type for operator logs; it never reaches the agent
        # (the route renders a generic 503), so the backend id stays
        # server-side per the backend-agnostic contract.
        raise CorpusUnavailable(message or "doc collection has no routable search backend")
    # label is None ⇒ backend is set (resolver invariant). Narrow for mypy.
    assert backend is not None
    return backend


def resolve_backend_or_label(
    collection: Any | None,
) -> tuple[BackendRef | None, BackendLabel | None, str | None]:
    """Run :func:`resolve_backend`'s lookup, translating failure to a label.

    The non-raising sibling of :func:`resolve_backend`, mirroring
    :func:`~meho_backplane.connectors.resolver.resolve_connector_or_label`
    so the seam, the T5 fan-out, and the T6 readiness probe branch on the
    same ``(impl, label, msg)`` triple instead of each catching
    :class:`CorpusUnavailable`.

    Returns:
        * ``(BackendRef, None, None)`` — routed to a registered adapter.
        * ``(None, "unknown_backend", message)`` — the collection's
          ``backend.type`` is missing / malformed or names no registered
          adapter. ``message`` names the offending type (or "missing")
          for operator diagnostics.
    """
    record = _read_routing_record(collection)
    if record is None:
        key = _collection_key(collection)
        _log.warning(
            "doc_backend_unroutable",
            collection_key=key,
            reason="missing_or_malformed_type",
        )
        return None, "unknown_backend", f"doc collection {key!r} has no backend.type"

    backend_type, ref = record
    impl = get_backend(backend_type)
    if impl is None:
        key = _collection_key(collection)
        _log.warning(
            "doc_backend_unknown_type",
            collection_key=key,
            backend_type=backend_type,
        )
        return (
            None,
            "unknown_backend",
            f"doc collection {key!r} routes to unregistered backend type {backend_type!r}",
        )

    _log.info(
        "doc_backend_resolved",
        collection_key=_collection_key(collection),
        backend_type=backend_type,
        impl=type(impl).__name__,
    )
    return BackendRef(impl, ref), None, None


def _collection_key(collection: Any | None) -> str:
    """Best-effort collection key for diagnostics (never raises)."""
    if collection is None:
        return "<legacy-single-collection>"
    return str(getattr(collection, "collection_key", "<unknown>"))
