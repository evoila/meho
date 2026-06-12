# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Probe write-back + operator lifecycle service for doc collections (T6 #1555).

Two write paths against a :class:`~meho_backplane.db.models.DocCollection`
row, both modelled on the ``probe_target`` / connector-enable precedents:

* :func:`probe_collection` — resolve the row's backend, read its typed
  :class:`~meho_backplane.docs_search.backends.base.BackendReadiness`, and
  persist ``readiness`` / ``doc_count`` / ``last_ingested_at`` + the
  ``status`` transition **on success only**. A probe that raises
  :class:`~meho_backplane.auth.corpus.CorpusUnavailable` leaves the row
  untouched — the same success-only write-back ``probe_target`` uses for
  ``Target.fingerprint`` (``api/v1/targets.py``). The caller (the probe
  route) owns the transaction boundary; this function flushes but never
  commits, so the route's ``async with session.begin()`` is the commit /
  rollback unit.
* :func:`set_collection_enabled` — the operator enable/disable transition,
  guarded by :data:`~meho_backplane.docs_collections.lifecycle.OPERATOR_TRANSITIONS`
  and idempotent (a re-call against an already-at-target row writes
  nothing).

Both reflect state the backend / operator owns; neither triggers ingest
or a rebuild (out of scope per #1555 — the heavy ingest is the ops side,
meho probes + reflects).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections.lifecycle import (
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    apply_operator_transition,
    apply_probe_transition,
    status_for_readiness,
)
from meho_backplane.docs_search.backends import BackendReadiness, resolve_backend

__all__ = [
    "probe_collection",
    "set_collection_enabled",
]

_log = structlog.get_logger(__name__)


async def probe_collection(
    session: AsyncSession,
    operator: Operator,
    collection: DocCollectionORM,
) -> BackendReadiness:
    """Probe *collection*'s backend and persist its liveness on success.

    Resolves the row's ``backend{type, ref}`` to its concrete adapter
    (:func:`~meho_backplane.docs_search.backends.resolve_backend`), reads
    the typed :class:`BackendReadiness`, then — **only when the probe
    succeeds** — writes ``readiness`` / ``doc_count`` / ``last_ingested_at``
    and transitions ``status`` (``provisioning`` / ``rebuilding`` →
    ``ready`` once the index is built, ``ready`` → ``rebuilding`` when it
    is not).

    Args:
        session: Active async session inside the route's open
            transaction. This function flushes the row write but does not
            commit; the route's ``session.begin()`` owns commit / rollback.
        operator: The verified operator whose JWT the backend adapter
            forwards to probe under the operator identity.
        collection: The resolved ORM row to probe and write back to.

    Returns:
        The :class:`BackendReadiness` snapshot that was persisted.

    Raises:
        CorpusUnavailable: the backend is unconfigured / unreachable /
            non-2xx / malformed, **or** the row routes to no registered
            backend. The row is left untouched (success-only write-back);
            the route maps this to HTTP 503.
        DocCollectionStateError: the readiness implies a status the
            lifecycle forbids from the current state (e.g. a probe against
            a ``disabled`` row). The row is left untouched; HTTP 409.
    """
    # ``resolve_backend`` raises CorpusUnavailable for an unroutable row —
    # the same 503 arm the search path uses, no new taxonomy. Bundles the
    # adapter with the row's ``backend.ref``.
    resolved = resolve_backend(collection)
    readiness = await resolved.backend.probe(operator, backend_ref=resolved.ref)

    target_status = status_for_readiness(readiness)
    new_status = apply_probe_transition(
        collection_key=collection.collection_key,
        from_status=collection.status,
        to_status=target_status,
    )

    # Success-only write-back. Everything below this line runs only after
    # the probe returned (a raise above left the row untouched).
    collection.readiness = dict(readiness.detail)
    collection.doc_count = readiness.doc_count
    collection.last_ingested_at = readiness.last_ingested_at
    collection.status = new_status
    collection.updated_at = datetime.now(UTC)
    await session.flush()

    _log.info(
        "doc_collection_probed",
        collection_key=collection.collection_key,
        tenant_scope="tenant" if collection.tenant_id is not None else "global",
        index_built=readiness.index_built,
        doc_count=readiness.doc_count,
        status=new_status,
    )
    return readiness


async def set_collection_enabled(
    session: AsyncSession,
    collection: DocCollectionORM,
    *,
    enabled: bool,
) -> bool:
    """Enable or disable *collection*, guarded + idempotent.

    ``enabled=False`` transitions any live state to ``disabled``;
    ``enabled=True`` returns a disabled collection to ``provisioning`` (a
    follow-up probe promotes it to ``ready``). A re-call against an
    already-at-target row is a no-op — no write, no timestamp bump.

    Args:
        session: Active async session inside the route's transaction.
        collection: The resolved ORM row to transition.
        enabled: ``True`` → enable (→ ``provisioning``); ``False`` →
            ``disabled``.

    Returns:
        ``True`` when the row's status actually changed (a write
        happened), ``False`` on the idempotent no-op path.

    Raises:
        DocCollectionStateError: the move is forbidden from the current
            state; HTTP 409. The row is left untouched.
    """
    target_status = STATUS_PROVISIONING if enabled else STATUS_DISABLED
    new_status = apply_operator_transition(
        collection_key=collection.collection_key,
        from_status=collection.status,
        to_status=target_status,
    )
    if new_status == collection.status:
        # Idempotent no-op — already at target.
        return False
    collection.status = new_status
    collection.updated_at = datetime.now(UTC)
    await session.flush()
    _log.info(
        "doc_collection_lifecycle_set",
        collection_key=collection.collection_key,
        enabled=enabled,
        status=new_status,
    )
    return True
