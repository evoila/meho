# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Search-backend registry — a ``dict[type, SearchBackend]`` lookup (G4.6-T2 #1551).

A deliberately tiny registry mirroring
:mod:`meho_backplane.connectors.registry`, **minus the version
tie-break ladder** — a doc collection binds to exactly one backend by
construction, so the resolver is a direct dict lookup, never a
candidate ranking (#1548 design decision 3).

One adapter instance per ``backend_type`` is registered at import time.
Adapters are stateless singletons (per-collection config rides on
``backend.ref``, not on adapter state — see
:mod:`meho_backplane.docs_search.backends.corpus_http`), so a single
instance serves every collection routed to its type.

Keeping the table keyed by type — rather than a literal ``if/elif`` over
the one shipped adapter — means a second backend (the later
``vertex-rag`` Task) is a one-line :func:`register_backend` call, not a
control-flow edit at every call site.

Duplicate registration of the same type raises :exc:`RuntimeError`: two
modules claiming the same backend type is a programming bug that should
surface as a deploy failure, not a silent last-writer-wins.
"""

from __future__ import annotations

import structlog

from meho_backplane.docs_search.backends.base import SearchBackend
from meho_backplane.docs_search.backends.corpus_http import CorpusHttpBackend

__all__ = [
    "all_backends",
    "get_backend",
    "register_backend",
]

_log = structlog.get_logger(__name__)

#: type → singleton adapter instance. Populated at import time below.
_BACKENDS: dict[str, SearchBackend] = {}


def register_backend(backend_type: str, impl: SearchBackend) -> None:
    """Register *impl* under *backend_type* in the search-backend table.

    Called at import time. *impl* is a ready-to-use singleton instance
    (not a class) because adapters are stateless and per-collection
    config is passed per call. The instance's :attr:`backend_type` must
    match *backend_type* — a mismatch is a registration bug.

    Raises:
        TypeError: when *impl* is not a :class:`SearchBackend`, or its
            :attr:`backend_type` disagrees with *backend_type*.
        RuntimeError: when *backend_type* is already registered.
    """
    if not isinstance(impl, SearchBackend):
        raise TypeError(
            f"search backend for type={backend_type!r} must be a SearchBackend: {impl!r}"
        )
    if impl.backend_type != backend_type:
        raise TypeError(
            f"search backend registered under type={backend_type!r} "
            f"advertises backend_type={impl.backend_type!r}"
        )
    if backend_type in _BACKENDS:
        raise RuntimeError(
            f"search backend already registered for type={backend_type!r}: "
            f"existing={type(_BACKENDS[backend_type]).__name__}, attempted={type(impl).__name__}"
        )
    _BACKENDS[backend_type] = impl
    _log.info("search_backend_registered", backend_type=backend_type, impl=type(impl).__name__)


def get_backend(backend_type: str) -> SearchBackend | None:
    """Look up the adapter for *backend_type*. Returns ``None`` if absent."""
    return _BACKENDS.get(backend_type)


def all_backends() -> dict[str, SearchBackend]:
    """Return a snapshot of the registry — for diagnostics / fan-out (T5)."""
    return dict(_BACKENDS)


# Self-register the shipped adapter at import time. The first (and, in T2,
# only) backend is the JWT-forward corpus client.
register_backend(CorpusHttpBackend.backend_type, CorpusHttpBackend())
