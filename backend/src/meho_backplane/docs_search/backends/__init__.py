# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Backend-agnostic search router for doc collections (G4.6-T2 #1551).

Public surface — the four pieces a collection's ``backend{type, ref}``
routing record (T1 #1550) is resolved through:

* :class:`SearchBackend` — the adapter ABC every backend implements
  (``async search`` + the T6 ``probe`` seam).
* :class:`CorpusHttpBackend` — the first concrete adapter, re-homing the
  JWT-forward corpus client.
* :func:`register_backend` / :func:`get_backend` / :func:`all_backends` —
  the tiny type→impl registry (mirrors the connector registry, minus the
  tie-break ladder).
* :func:`resolve_backend` / :func:`resolve_backend_or_label` — the
  ``collection → backend`` router. The raising form drops into the
  ``search_docs`` seam (unroutable → existing 503); the labelled form is
  the ``(impl, label, msg)`` shape T5 fan-out and T6 readiness branch on.

Importing this package self-registers the shipped ``corpus-http`` adapter
(via :mod:`meho_backplane.docs_search.backends.registry`), so a caller
that imports :func:`resolve_backend` has a populated registry without a
separate eager-import step.
"""

from meho_backplane.docs_search.backends.base import BackendReadiness, SearchBackend
from meho_backplane.docs_search.backends.corpus_http import (
    CORPUS_HTTP_BACKEND_TYPE,
    CorpusHttpBackend,
)
from meho_backplane.docs_search.backends.registry import (
    all_backends,
    get_backend,
    register_backend,
)
from meho_backplane.docs_search.backends.resolver import (
    BackendRef,
    ResolvedBackend,
    resolve_backend,
    resolve_backend_or_label,
)

__all__ = [
    "CORPUS_HTTP_BACKEND_TYPE",
    "BackendReadiness",
    "BackendRef",
    "CorpusHttpBackend",
    "ResolvedBackend",
    "SearchBackend",
    "all_backends",
    "get_backend",
    "register_backend",
    "resolve_backend",
    "resolve_backend_or_label",
]
