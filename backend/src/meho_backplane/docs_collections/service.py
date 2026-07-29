# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create + probe write-back + operator lifecycle service for doc collections.

Three write paths against a :class:`~meho_backplane.db.models.DocCollection`
row. :func:`create_doc_collection` (#1739) registers a new row, modelled on
``create_target``: ``tenant_id`` from the operator (never the body), the
``backend.type`` validated against the search-backend registry with a
structured 422 (the ``create_target`` unknown-product shape), an
``IntegrityError`` on a cross-scope ``collection_key`` collision mapped to
409, and the ``meho.docs.collections.create`` audit op bound so the write
joins the ``op_id="meho.docs.*"`` who-touched trail. The other two are
modelled on the ``probe_target`` / connector-enable precedents:

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

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
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
from meho_backplane.docs_collections.schemas import DocCollectionCreate
from meho_backplane.docs_search.backends import BackendReadiness, resolve_backend
from meho_backplane.docs_search.backends.registry import all_backends

__all__ = [
    "DocCollectionBackendTypeError",
    "DocCollectionConflictError",
    "DocCollectionGlobalError",
    "DocCollectionNotDisabledError",
    "create_doc_collection",
    "delete_doc_collection",
    "probe_collection",
    "set_collection_enabled",
]

_log = structlog.get_logger(__name__)

#: Canonical audit op_id for the create — the SAME ``meho.docs.*`` family
#: the list route / lifecycle verbs bind, so a ``query_audit`` filter on
#: ``op_id="meho.docs.*"`` catches the registration transport-independently
#: (G4.5-T8 #1549). ``op_class="write"`` — create mutates the registry.
_CREATE_OP_ID = "meho.docs.collections.create"

#: Canonical audit op_id for the delete (#2487). Same ``meho.docs.*``
#: family as create / list / the lifecycle verbs, so a deregistration is
#: caught by the same ``op_id="meho.docs.*"`` who-touched filter.
#: ``op_class="write"`` — a delete mutates the registry.
_DELETE_OP_ID = "meho.docs.collections.delete"


class DocCollectionBackendTypeError(Exception):
    """The ``backend.type`` is not a registered search backend.

    Carries the structured 422 ``detail`` the create fronts surface so an
    operator who typed an unroutable backend type sees the registered set
    at create time instead of an opaque 503 at probe/search time. The
    detail shape mirrors ``create_target``'s ``unknown_product`` 422 (a
    stable ``kind`` code + the offending value + the valid set + a human
    ``message``).
    """

    def __init__(self, backend_type: str, valid_types: list[str]) -> None:
        self.backend_type = backend_type
        self.valid_types = valid_types
        self.detail: dict[str, object] = {
            "kind": "unknown_backend_type",
            "backend_type": backend_type,
            "valid_backend_types": valid_types,
            "message": (
                f"backend.type={backend_type!r} is not a registered search "
                f"backend; pick one of {valid_types!r}. An unroutable "
                f"collection would commit but fail every probe / search with "
                f"a 503."
            ),
        }
        super().__init__(self.detail["message"])


class DocCollectionConflictError(Exception):
    """A collection with this ``collection_key`` already exists in the scope.

    Raised when the create's flush hits one of the two partial-unique
    indexes (global ``collection_key`` / per-tenant
    ``(tenant_id, collection_key)``). The fronts map it to 409 (REST) /
    ``-32602`` (MCP) so a cross-scope collision is a typed conflict, not
    an opaque 500 / IntegrityError.
    """

    def __init__(self, collection_key: str, *, tenant_scoped: bool) -> None:
        self.collection_key = collection_key
        self.tenant_scoped = tenant_scoped
        scope = "tenant" if tenant_scoped else "global"
        super().__init__(f"doc collection {collection_key!r} already exists in the {scope} scope")


async def create_doc_collection(
    session: AsyncSession,
    operator: Operator,
    body: DocCollectionCreate,
) -> DocCollectionORM:
    """Register a new doc collection in the operator's tenant.

    Mirrors :func:`~meho_backplane.api.v1.targets.create_target`:
    ``tenant_id`` is always the operator's (the body cannot override it);
    ``id`` / timestamps are generated server-side; ``status`` defaults to
    ``provisioning`` (a follow-up probe promotes it to ``ready``).
    ``backend.type`` is validated against
    :func:`~meho_backplane.docs_search.backends.registry.all_backends`
    before the insert so an unroutable type is a structured 422
    (:class:`DocCollectionBackendTypeError`), not a deferred probe-time
    503. A cross-scope ``collection_key`` collision surfaces as a typed
    :class:`DocCollectionConflictError` (409), not an opaque IntegrityError.

    The caller owns the transaction boundary (the route's
    ``async with session.begin()``); this function flushes but never
    commits, so a downstream failure rolls the insert back.

    Args:
        session: Active async session inside the front's open transaction.
        operator: The verified operator; ``operator.tenant_id`` is the
            row's tenant — the body's ``tenant_id`` (if any) is ignored
            (the schema forbids it).
        body: The validated create request.

    Returns:
        The flushed :class:`DocCollectionORM` row (``id`` / timestamps
        populated).

    Raises:
        DocCollectionBackendTypeError: ``backend.type`` is not registered;
            the front maps it to 422.
        DocCollectionConflictError: a collection with this ``collection_key``
            already exists in the operator's scope; the front maps it to 409.
    """
    # Bind the canonical op_id up-front so a persisted audit row is
    # filterable by ``op_id="meho.docs.*"`` even if the insert raises
    # below (G4.5-T8 #1549). ``op_class="write"`` — create mutates the
    # registry.
    structlog.contextvars.bind_contextvars(
        audit_op_id=_CREATE_OP_ID,
        audit_op_class="write",
    )

    # Validate ``backend.type`` against the registry BEFORE the insert so
    # the operator sees the registered set at create time. ``all_backends``
    # is populated at import time (the adapters self-register), so the set
    # is non-empty in every real deploy; an empty registry would be a
    # boot-order bug, and validating against an empty set would reject
    # every create, so — matching ``create_target``'s empty-registry skip —
    # we only enforce when the registry is populated.
    valid_types = sorted(all_backends())
    if valid_types and body.backend.type not in valid_types:
        raise DocCollectionBackendTypeError(body.backend.type, valid_types)

    now = datetime.now(UTC)
    row = DocCollectionORM(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        collection_key=body.collection_key,
        vendor=body.vendor,
        products=list(body.products),
        description=body.description,
        when_to_use=body.when_to_use,
        backend={"type": body.backend.type, "ref": dict(body.backend.ref)},
        status=STATUS_PROVISIONING,
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras=dict(body.extras),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise DocCollectionConflictError(
            body.collection_key,
            tenant_scoped=operator.tenant_id is not None,
        ) from exc

    _log.info(
        "doc_collection_created",
        collection_key=row.collection_key,
        tenant_scope="tenant" if row.tenant_id is not None else "global",
        backend_type=body.backend.type,
        status=row.status,
    )
    return row


class DocCollectionGlobalError(Exception):
    """A tenant admin cannot delete a global (platform-owned) collection row.

    A row with ``tenant_id IS NULL`` is a shared platform-catalogue entry
    every tenant sees; removing it is an ops/platform action, out of the
    tenant API (#2487). Carries a structured ``detail`` the fronts map to a
    typed refusal (REST 403 / MCP ``-32602``) so the caller sees *why* the
    delete was refused, not an opaque conflict. The tenant-owned row that
    *shadows* a global key is deletable — deleting it simply un-shadows the
    global row (the resolver is tenant-first).
    """

    def __init__(self, collection_key: str) -> None:
        self.collection_key = collection_key
        self.detail: dict[str, object] = {
            "error": "global_collection",
            "collection_key": collection_key,
            "message": (
                f"doc collection {collection_key!r} is a global "
                f"(platform-owned) row; a tenant admin cannot delete it. "
                f"Only tenant-owned collections are deletable via this API."
            ),
        }
        super().__init__(self.detail["message"])


class DocCollectionNotDisabledError(Exception):
    """Only a disabled collection can be deleted — disable it first (#2487).

    The delete is a two-step (``disable`` → ``delete``): disabling first
    keeps the existing typed ``collection_disabled`` search rejection as the
    operator-visible warning window before the key 404s. Carries a
    structured ``detail`` (including the current ``status``) the fronts map
    to a typed refusal (REST 409 / MCP ``-32602``).
    """

    def __init__(self, collection_key: str, status: str) -> None:
        self.collection_key = collection_key
        self.status = status
        self.detail: dict[str, object] = {
            "error": "collection_not_disabled",
            "collection_key": collection_key,
            "status": status,
            "message": (
                f"doc collection {collection_key!r} is {status!r}, not "
                f"'disabled'; disable it first, then delete. The disable → "
                f"delete two-step gives searchers a terminal "
                f"'collection_disabled' warning window before the key 404s."
            ),
        }
        super().__init__(self.detail["message"])


async def delete_doc_collection(
    session: AsyncSession,
    operator: Operator,
    collection: DocCollectionORM,
) -> None:
    """Deregister a disabled, tenant-owned collection, freeing its key (#2487).

    The delete half of the registry, the counterpart to
    :func:`create_doc_collection`. Hard-deletes the resolved row (nothing
    FK-references ``doc_collections``, so there is no cascade to reason
    about) which frees its ``collection_key`` for a re-``create`` under the
    same key — the recovery loop that motivated the issue (a collection
    mis-registered under the wrong ``backend.type`` / ``ref`` could never be
    fixed under its own key while ``disable`` only flipped ``status``).

    Two guards, checked in order:

    1. **Tenant-owned only.** A global row (``tenant_id IS NULL``) is a
       shared platform-catalogue entry; a tenant admin removing it would
       delete a row every tenant sees. Refused with
       :class:`DocCollectionGlobalError` (REST 403 / MCP ``-32602``). This
       is checked first: a tenant admin must never be told "disable it
       first" for a global row they cannot delete at all.
    2. **Disabled-first.** A collection that is not ``disabled`` is refused
       with :class:`DocCollectionNotDisabledError` (REST 409 / MCP
       ``-32602``). The disable → delete two-step keeps the typed
       ``collection_disabled`` search rejection as the warning window before
       the key 404s.

    The caller owns the transaction boundary (the route's ``get_session``
    ``session.begin()``); this function flushes the delete but does not
    commit, so a downstream failure rolls it back.

    Args:
        session: Active async session inside the front's open transaction.
        operator: The verified operator (tenant scope already applied by the
            resolver that produced *collection*); bound for audit
            attribution.
        collection: The resolved ORM row to deregister.

    Raises:
        DocCollectionGlobalError: *collection* is a global row; the front
            maps it to 403 / ``-32602``.
        DocCollectionNotDisabledError: *collection* is not ``disabled``;
            the front maps it to 409 / ``-32602``.
    """
    # Bind the canonical op_id up-front so the persisted audit row is
    # filterable by ``op_id="meho.docs.*"`` even when a guard refuses below
    # (the create binds identically). ``op_class="write"`` — a delete
    # mutates the registry.
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DELETE_OP_ID,
        audit_op_class="write",
    )

    if collection.tenant_id is None:
        raise DocCollectionGlobalError(collection.collection_key)
    if collection.status != STATUS_DISABLED:
        raise DocCollectionNotDisabledError(collection.collection_key, collection.status)

    await session.delete(collection)
    await session.flush()

    _log.info(
        "doc_collection_deleted",
        collection_key=collection.collection_key,
        tenant_scope="tenant",
        status=collection.status,
    )


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
