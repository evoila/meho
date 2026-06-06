# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Doc-collection lifecycle: the ``status`` state machine + readiness mapping (T6 #1555).

The ``doc_collections.status`` column is a four-state lifecycle enum
(``provisioning`` / ``ready`` / ``rebuilding`` / ``disabled``, T1 #1550).
This module owns the **rules** that govern moving between those states and
the mapping from a backend's typed
:class:`~meho_backplane.docs_search.backends.base.BackendReadiness` snapshot
to the resulting status. It is the docs analogue of the connector
enable/disable state machine
(:class:`~meho_backplane.operations.ingest.exceptions.InvalidStateTransitionError`,
``api/v1/connectors_ingest.py``); the lifecycle stays here, beside the
resolver, rather than inline in a route so the probe route, the
enable/disable route, and the search path read one source of truth.

Two transition surfaces
-----------------------

* **Operator transitions** (``enable`` / ``disable``) —
  :data:`OPERATOR_TRANSITIONS`. ``disable`` is reachable from any live
  state; ``enable`` returns a disabled collection to service. Driven by
  the tenant-admin-gated enable/disable route.
* **Probe transitions** — :func:`status_for_readiness` maps a successful
  probe's :class:`BackendReadiness` to the status the row should hold,
  then :func:`apply_probe_transition` guards that move against
  :data:`PROBE_TRANSITIONS`. A probe never re-enables a ``disabled``
  collection — an operator's explicit disable wins over a liveness signal.

Fail-closed, idempotent
-----------------------

A forbidden transition raises :class:`DocCollectionStateError` (HTTP 409),
mirroring the connector machine's ``InvalidStateTransitionError`` → 409.
A transition whose source already equals its target is a **no-op** (the
idempotency the acceptance criteria require), not an error — re-enabling
an enabled collection or re-probing a steady-state ``ready`` collection
returns without a spurious 409.

Search-time guard
-----------------

:func:`ensure_collection_searchable` is the typed guard the
collection-scoped search path (T3 #1552) calls to fail typed against a
not-``ready`` collection: ``rebuilding`` / ``provisioning`` →
:class:`DocCollectionNotReadyError` (409, retryable), ``disabled`` →
:class:`DocCollectionDisabledError` (403). T6 ships the guard; T3 wires
it into the ``search_docs`` route — the mechanism here, the call site
there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException

if TYPE_CHECKING:
    from meho_backplane.docs_search.backends.base import BackendReadiness

__all__ = [
    "DOC_COLLECTION_STATUSES",
    "OPERATOR_TRANSITIONS",
    "PROBE_TRANSITIONS",
    "STATUS_DISABLED",
    "STATUS_PROVISIONING",
    "STATUS_READY",
    "STATUS_REBUILDING",
    "DocCollectionDisabledError",
    "DocCollectionNotReadyError",
    "DocCollectionStateError",
    "apply_operator_transition",
    "apply_probe_transition",
    "ensure_collection_searchable",
    "status_for_readiness",
]

STATUS_PROVISIONING: Final = "provisioning"
STATUS_READY: Final = "ready"
STATUS_REBUILDING: Final = "rebuilding"
STATUS_DISABLED: Final = "disabled"

#: The four lifecycle states, matching the ``ck_doc_collections_status``
#: CHECK constraint (migration 0037). The single source for "is this a
#: valid status string" on the Python side.
DOC_COLLECTION_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_PROVISIONING, STATUS_READY, STATUS_REBUILDING, STATUS_DISABLED}
)

#: Legal **probe-driven** moves: ``source → {allowed targets}``. A probe
#: brings a freshly-provisioned or rebuilding collection to ``ready`` once
#: the index is built, and moves a ``ready`` collection to ``rebuilding``
#: when the index is no longer answerable. A probe never touches a
#: ``disabled`` collection — operator intent outranks a liveness signal.
PROBE_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    STATUS_PROVISIONING: frozenset({STATUS_READY, STATUS_REBUILDING}),
    STATUS_READY: frozenset({STATUS_REBUILDING}),
    STATUS_REBUILDING: frozenset({STATUS_READY}),
    STATUS_DISABLED: frozenset(),
}

#: Legal **operator** moves driven by the enable/disable route. ``disable``
#: is reachable from any live state; ``enable`` returns a disabled
#: collection to ``provisioning`` (a probe then promotes it to ``ready``
#: once the index confirms). A disabled→disabled or the matching
#: same-state re-call is the idempotent no-op the route swallows.
OPERATOR_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    STATUS_PROVISIONING: frozenset({STATUS_DISABLED}),
    STATUS_READY: frozenset({STATUS_DISABLED}),
    STATUS_REBUILDING: frozenset({STATUS_DISABLED}),
    STATUS_DISABLED: frozenset({STATUS_PROVISIONING}),
}


class DocCollectionStateError(HTTPException):
    """A requested ``status`` transition is forbidden by the lifecycle.

    Raised by :func:`apply_operator_transition` / :func:`apply_probe_transition`
    when ``to_status`` is not a legal successor of ``from_status``.
    Extends :class:`fastapi.HTTPException` with status **409** so it
    propagates cleanly through FastAPI route handlers — the same 409-on-
    forbidden-transition contract the connector enable/disable routes
    surface (``InvalidStateTransitionError`` → 409). The detail names both
    states so an operator sees *why* the move was rejected without parsing
    a generic conflict.
    """

    def __init__(self, *, collection_key: str, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            status_code=409,
            detail={
                "error": "invalid_collection_transition",
                "collection_key": collection_key,
                "from_status": from_status,
                "to_status": to_status,
            },
        )


class DocCollectionNotReadyError(HTTPException):
    """A collection cannot serve search because its index is not ready.

    The typed search-time failure for a ``provisioning`` / ``rebuilding``
    collection — HTTP **409** (a conflict with the collection's current
    lifecycle state, retryable once the rebuild finishes), never a silent
    empty 200. The search path (T3 #1552) raises this via
    :func:`ensure_collection_searchable`. ``retryable=True`` distinguishes
    it from the disabled 403 so a client can back off rather than treat it
    as terminal.
    """

    def __init__(self, *, collection_key: str, status: str) -> None:
        self.status = status
        super().__init__(
            status_code=409,
            detail={
                "error": "collection_not_ready",
                "collection_key": collection_key,
                "status": status,
                "retryable": True,
            },
        )


class DocCollectionDisabledError(HTTPException):
    """A disabled collection cannot serve search.

    The typed search-time failure for a ``disabled`` collection — HTTP
    **403** (the collection is hidden from service by operator action, not
    a transient state), distinct from the retryable 409 a rebuild yields.
    """

    def __init__(self, *, collection_key: str) -> None:
        super().__init__(
            status_code=403,
            detail={
                "error": "collection_disabled",
                "collection_key": collection_key,
                "retryable": False,
            },
        )


def status_for_readiness(readiness: BackendReadiness) -> str:
    """Map a successful probe's :class:`BackendReadiness` to a target status.

    * reachable **and** index built → ``ready`` (live for search).
    * reachable but index **not** built → ``rebuilding`` (a managed-RAG
      index rebuild is in flight, or the corpus was registered but never
      ingested — either way not yet answerable).

    An unreachable backend never reaches this function: the probe raises
    :class:`~meho_backplane.auth.corpus.CorpusUnavailable` before returning
    a :class:`BackendReadiness`, so the route persists nothing
    (success-only write-back) and the status is left untouched. This
    function therefore only ever resolves the *reachable* axis.
    """
    if readiness.index_built:
        return STATUS_READY
    return STATUS_REBUILDING


def apply_probe_transition(
    *,
    collection_key: str,
    from_status: str,
    to_status: str,
) -> str:
    """Resolve the post-probe status, guarding the move against the machine.

    Returns *to_status* when the move is legal, the unchanged *from_status*
    when it is a no-op (source already at target — the idempotent
    re-probe), and raises :class:`DocCollectionStateError` (409) when the
    move is forbidden (e.g. a probe trying to wake a ``disabled``
    collection). The caller persists the returned status only when it
    differs from *from_status*.
    """
    return _resolve_transition(
        transitions=PROBE_TRANSITIONS,
        collection_key=collection_key,
        from_status=from_status,
        to_status=to_status,
    )


def apply_operator_transition(
    *,
    collection_key: str,
    from_status: str,
    to_status: str,
) -> str:
    """Resolve an operator enable/disable move, guarding it against the machine.

    Same contract as :func:`apply_probe_transition` but against
    :data:`OPERATOR_TRANSITIONS`: a same-state re-call is the idempotent
    no-op the enable/disable route returns 204/200 for without a write; a
    forbidden move raises :class:`DocCollectionStateError` (409).
    """
    return _resolve_transition(
        transitions=OPERATOR_TRANSITIONS,
        collection_key=collection_key,
        from_status=from_status,
        to_status=to_status,
    )


def _resolve_transition(
    *,
    transitions: Mapping[str, frozenset[str]],
    collection_key: str,
    from_status: str,
    to_status: str,
) -> str:
    """Shared guard: no-op on same-state, raise 409 on a forbidden move."""
    if from_status == to_status:
        # Idempotent: the row is already where the caller wants it. No
        # write, no audit, no 409 — the same swallow the connector
        # enable/disable machine does for an already-at-target group.
        return from_status
    allowed = transitions.get(from_status, frozenset())
    if to_status not in allowed:
        raise DocCollectionStateError(
            collection_key=collection_key,
            from_status=from_status,
            to_status=to_status,
        )
    return to_status


def ensure_collection_searchable(*, collection_key: str, status: str) -> None:
    """Raise the typed failure when *status* cannot serve search (T6 mechanism).

    The guard the collection-scoped search path (T3 #1552) calls before
    federating a query. ``ready`` returns silently; every other state
    raises so the route fails typed rather than returning an empty 200:

    * ``provisioning`` / ``rebuilding`` → :class:`DocCollectionNotReadyError`
      (409, retryable).
    * ``disabled`` → :class:`DocCollectionDisabledError` (403).

    T6 ships this guard; T3 wires the call into the ``search_docs`` route.
    A status outside the known enum is treated as not-ready (fail-closed)
    rather than waved through.
    """
    if status == STATUS_READY:
        return
    if status == STATUS_DISABLED:
        raise DocCollectionDisabledError(collection_key=collection_key)
    raise DocCollectionNotReadyError(collection_key=collection_key, status=status)
