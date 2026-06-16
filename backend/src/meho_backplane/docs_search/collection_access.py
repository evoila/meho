# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Collection resolution + per-collection entitlement + readiness (T3 #1552).

The single shared gate every collection-scoped surface — the REST route,
the ``search_docs`` / ``ask_docs`` MCP tools, and the docs-chunk resource —
runs *after* parsing the operator-supplied ``collection`` key and *before*
calling :func:`~meho_backplane.docs_search.search_docs`. Centralising it
here means the three policies (catalogue membership, entitlement, and
readiness) are defined once and cannot drift per surface:

1. **Resolution** — turn the ``collection`` key into its registry row via
   the tenant-first-then-global resolver
   (:func:`~meho_backplane.docs_collections.resolve_doc_collection`). An
   unknown key is the typed :exc:`UnknownCollectionError`.

2. **Per-collection entitlement** (reuses the G4.5-T1 capability
   substrate, zero new tables) — a principal may search a collection only
   when its operator carries the ``meho-docs:<collection_key>`` capability.
   The static ``required_capability="meho-docs"`` gate on the tool still
   governs *visibility* (a tenant without the add-on never sees the tool);
   this finer gate governs *which collections* an entitled tenant may
   actually query. A miss is the typed :exc:`CollectionForbiddenError`.

3. **Readiness** — a collection whose lifecycle ``status`` is not
   ``"ready"`` is not answerable yet, but the rejection branches on *why*:
   a ``provisioning`` / ``rebuilding`` collection is **transiently** not
   ready (:exc:`CollectionNotReadyError`, retryable once the rebuild
   finishes), whereas a ``disabled`` collection is **terminally** hidden by
   operator action (:exc:`CollectionDisabledError`, not retryable). The
   split is operationally load-bearing — a client should back off and retry
   a rebuilding collection but must not retry a disabled one — so the two
   carry distinct transport shapes (409 vs 403 at REST; ``-32603`` vs
   ``-32602`` at the MCP face). The richer reachability *probe* is T6
   (#1555); this gate reads only the registry ``status`` column the T1
   substrate already carries.

Each surface translates these typed errors into its own transport shape
(HTTP status at the REST route, JSON-RPC ``-32xxx`` at the MCP face) so
the policy stays transport-independent and this module imports nothing
transport-specific.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import (
    DocCollection,
    DocCollectionNotFoundError,
    resolve_doc_collection,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_backplane.auth.operator import Operator

__all__ = [
    "CollectionAccessError",
    "CollectionDisabledError",
    "CollectionForbiddenError",
    "CollectionNotReadyError",
    "NoEntitledReadyCollectionError",
    "UnknownCollectionError",
    "collection_capability_key",
    "resolve_entitled_ready_collection",
    "resolve_entitled_ready_collections",
]

_log = structlog.get_logger(__name__)

#: The lifecycle status a collection must carry to be answerable. The
#: other states (``provisioning`` / ``rebuilding`` / ``disabled``) are
#: not-ready: the catalogue knows the collection but the backend is not
#: serving it yet.
_READY_STATUS = "ready"

#: The terminal not-ready status: an operator has hidden the collection
#: from service. Distinct from the transient ``provisioning`` /
#: ``rebuilding`` states because a disabled collection will not become
#: answerable on its own — a client must not retry it. Kept as a literal
#: (rather than imported from ``docs_collections.lifecycle``) so this
#: transport-neutral access module stays free of a lifecycle import; the
#: string is the same ``ck_doc_collections_status`` CHECK-constraint value.
_DISABLED_STATUS = "disabled"

#: The capability-key prefix the per-collection entitlement gate keys on.
#: ``meho-docs:<collection_key>`` reuses the G4.5-T1 capability substrate
#: (``Operator.capabilities`` accepts arbitrary keys), so the finer gate
#: needs no new table.
_CAPABILITY_PREFIX = "meho-docs:"


class CollectionAccessError(Exception):
    """Base for the typed collection-access failures.

    Surfaces translate each subclass into a transport-specific shape; the
    base lets a caller that does not care which arm fired catch the whole
    family. ``known_keys`` is populated only for
    :class:`UnknownCollectionError` (it has the resolver's catalogue
    diagnostics); the other arms leave it ``None``.
    """

    def __init__(self, message: str, *, collection_key: str) -> None:
        super().__init__(message)
        self.collection_key = collection_key


class UnknownCollectionError(CollectionAccessError):
    """The ``collection`` key names no collection visible to the tenant.

    Carries the catalogue of keys the tenant *can* see (global + its own)
    so a surface can render a "did you mean…?" hint without a second query.
    Surfaces map this to 422 (REST) / ``-32602`` (MCP) — an invalid
    ``collection`` argument, not a server fault.
    """

    def __init__(self, collection_key: str, known_keys: list[str]) -> None:
        super().__init__(
            f"unknown doc collection {collection_key!r}",
            collection_key=collection_key,
        )
        self.known_keys = known_keys


class CollectionForbiddenError(CollectionAccessError):
    """The tenant is not entitled to the (otherwise-resolvable) collection.

    The operator passed the static ``meho-docs`` visibility gate (the tool
    is callable) but lacks the finer ``meho-docs:<collection_key>``
    capability for *this* collection. Surfaces map this to a 403-class
    rejection so it reads as "denied", not "not found" — the collection
    exists; the principal just cannot search it.

    The error names the **missing capability** (``required_capability``) and
    the **identity it checked** (``operator_sub`` + ``tenant_id``) so every
    surface can render an *actionable* diagnostic — "your identity ``<sub>``
    (tenant ``<id>``) is missing capability ``meho-docs:<key>``" — instead of
    an opaque "not entitled" / empty result (T2 #1802). The asymmetry that
    motivated this (MCP succeeds where REST / the UI session 403 / empty) is
    a per-audience Keycloak claim divergence: the surface telling the
    operator *which* capability is absent *on which identity* is what lets
    them fix the mapper rather than guess (see
    ``deploy/values-examples/README.md`` § meho-docs entitlement claim).
    """

    def __init__(
        self,
        collection_key: str,
        *,
        required_capability: str,
        operator_sub: str,
        tenant_id: str,
    ) -> None:
        self.required_capability = required_capability
        self.operator_sub = operator_sub
        self.tenant_id = tenant_id
        super().__init__(
            (
                f"identity {operator_sub!r} (tenant {tenant_id}) is not entitled to "
                f"doc collection {collection_key!r}: missing capability "
                f"{required_capability!r}"
            ),
            collection_key=collection_key,
        )


class CollectionNotReadyError(CollectionAccessError):
    """The collection is known + entitled but its backend is not serving yet.

    The registry ``status`` is ``provisioning`` or ``rebuilding`` — a
    **transient** not-ready state. Surfaces map this to a retryable
    409-class rejection (REST 409, MCP ``-32603``): the request was
    well-formed and authorised; the collection is simply not answerable at
    *this* moment and will become so once provisioning / the rebuild
    finishes. A ``disabled`` collection is the sibling :exc:`CollectionDisabledError`
    (terminal) instead, so a client can tell "retry later" from "do not
    retry".
    """

    def __init__(self, collection_key: str, status: str) -> None:
        super().__init__(
            f"doc collection {collection_key!r} is not ready (status={status!r})",
            collection_key=collection_key,
        )
        self.status = status


class CollectionDisabledError(CollectionAccessError):
    """The collection is known + entitled but an operator has disabled it.

    The registry ``status`` is ``disabled`` — a **terminal** not-ready
    state: the collection is hidden from service by deliberate operator
    action, not a transient rebuild, so it will not become answerable on
    its own. Surfaces map this to a terminal rejection distinct from the
    retryable :exc:`CollectionNotReadyError` (REST 403, MCP ``-32602``) so a
    client backs off permanently rather than polling a collection that an
    operator chose to take out of service. It is *not* the entitlement-miss
    :exc:`CollectionForbiddenError` — the tenant holds
    ``meho-docs:<collection_key>``; the collection itself is switched off.
    """

    def __init__(self, collection_key: str) -> None:
        super().__init__(
            f"doc collection {collection_key!r} is disabled",
            collection_key=collection_key,
        )
        self.status = _DISABLED_STATUS


class NoEntitledReadyCollectionError(CollectionAccessError):
    """A cross-collection fan-out resolved to zero answerable collections.

    Raised by :func:`resolve_entitled_ready_collections` (T5 #1554) when the
    requested fan-out set — an explicit ``collections=[…]`` list or the
    ``all`` sentinel — names no collection the tenant is both entitled to
    (``meho-docs:<key>``) **and** that is ``ready``. Distinct from
    :class:`CollectionForbiddenError` (a *single* named collection the
    tenant cannot search): here the *set* collapsed to empty after dropping
    non-entitled / not-ready members, so there is nothing to fan out across.
    Surfaces map this to a 403-class rejection (REST 403, MCP ``-32602``) —
    a well-formed request that the tenant has no answerable collection for.

    ``requested`` is the (sorted, de-duplicated) keys the caller asked for
    (empty for the ``all`` sentinel, where the tenant simply has no entitled
    ready collection at all) so the surface can render *why* nothing
    matched without re-deriving the set.
    """

    def __init__(self, requested: list[str]) -> None:
        self.requested = requested
        named = ", ".join(requested) if requested else "all entitled collections"
        super().__init__(
            f"no entitled, ready doc collection in fan-out scope ({named})",
            collection_key="",
        )


def collection_capability_key(collection_key: str) -> str:
    """Return the per-collection entitlement capability key.

    ``meho-docs:<collection_key>`` — the key an operator's
    :attr:`~meho_backplane.auth.operator.Operator.capabilities` must carry
    to search *collection_key*. Single source of truth so the gate and any
    provisioning tooling agree on the string.
    """
    return f"{_CAPABILITY_PREFIX}{collection_key}"


async def resolve_entitled_ready_collection(
    session: AsyncSession,
    operator: Operator,
    collection_key: str,
) -> DocCollection:
    """Resolve, entitlement-check, and readiness-check *collection_key*.

    The one gate every collection-scoped surface runs after parsing the
    ``collection`` argument. Returns the frozen :class:`DocCollection` read
    shape (not the ORM row) so the caller can pass it to
    :func:`~meho_backplane.docs_search.search_docs` and stash it in logs
    without an open-session lifetime concern.

    The checks run resolve → entitle → readiness in that order so the
    rejection is the most specific true one: a tenant gets
    ``not found`` for a key it cannot see at all, ``forbidden`` for a
    key it can see but is not entitled to, and a readiness rejection only
    once both of those pass. The readiness arm itself branches on the
    *kind* of not-ready: a ``disabled`` collection is the terminal
    :exc:`CollectionDisabledError`; ``provisioning`` / ``rebuilding`` (or
    any other non-``ready`` value, fail-closed) is the retryable
    :exc:`CollectionNotReadyError`. This is the **single** readiness gate
    every collection-scoped search surface runs — there is no second
    duplicate check downstream.

    Args:
        session: Active async DB session.
        operator: The verified operator (carries ``tenant_id`` for the
            tenant-scoped resolve and ``capabilities`` for the entitlement
            check).
        collection_key: The operator-supplied collection key (already
            stripped / non-blank by :func:`build_docs_scope`).

    Returns:
        The resolved, entitled, ready :class:`DocCollection`.

    Raises:
        UnknownCollectionError: No collection with *collection_key* is
            visible to the operator's tenant.
        CollectionForbiddenError: The tenant lacks the
            ``meho-docs:<collection_key>`` capability.
        CollectionDisabledError: The collection's registry ``status`` is
            ``disabled`` (terminal — an operator hid it from service).
        CollectionNotReadyError: The collection's registry ``status`` is
            ``provisioning`` / ``rebuilding`` (or any other non-``ready``
            value), i.e. transiently not answerable.
    """
    try:
        row = await resolve_doc_collection(session, collection_key, operator.tenant_id)
    except DocCollectionNotFoundError as exc:
        # Re-shape the resolver's 404-flavoured HTTPException into the
        # transport-neutral typed error this module owns, preserving the
        # catalogue hint the resolver assembled.
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        known_keys = detail.get("known_keys", [])
        raise UnknownCollectionError(collection_key, list(known_keys)) from exc

    capability = collection_capability_key(row.collection_key)
    if capability not in operator.capabilities:
        _log.warning(
            "doc_collection_entitlement_denied",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            collection_key=row.collection_key,
            required_capability=capability,
        )
        raise CollectionForbiddenError(
            row.collection_key,
            required_capability=capability,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )

    if row.status != _READY_STATUS:
        _log.info(
            "doc_collection_not_ready",
            tenant_id=str(operator.tenant_id),
            collection_key=row.collection_key,
            status=row.status,
        )
        # Branch the not-ready rejection so the terminal disabled state is
        # distinguishable from the transient rebuild states downstream.
        if row.status == _DISABLED_STATUS:
            raise CollectionDisabledError(row.collection_key)
        raise CollectionNotReadyError(row.collection_key, row.status)

    return DocCollection.model_validate(row, from_attributes=True)


def _visible_rows_by_key(
    rows: list[DocCollectionORM],
    tenant_id: Any,
) -> dict[str, DocCollectionORM]:
    """Collapse tenant-visible rows to one per ``collection_key``, tenant-first.

    Mirrors :func:`~meho_backplane.docs_collections.resolve_doc_collection`'s
    tenant-then-global preference, applied across the whole catalogue: a key
    that exists both as a global row and a tenant-curated row resolves to the
    tenant row (the override), so the fan-out sees each key exactly once.
    """
    by_key: dict[str, DocCollectionORM] = {}
    # Two passes so the tenant row deterministically wins regardless of the
    # DB's row order: seed globals first, then let tenant rows overwrite.
    for row in rows:
        if row.tenant_id is None:
            by_key[row.collection_key] = row
    for row in rows:
        if row.tenant_id == tenant_id:
            by_key[row.collection_key] = row
    return by_key


def _keep_if_entitled_ready(operator: Operator, row: DocCollectionORM) -> bool:
    """Whether *row* survives the fan-out's entitlement + readiness filter.

    Returns ``True`` when *operator* carries the ``meho-docs:<key>``
    capability **and** the row is ``ready``. A drop is logged with its
    reason (``not_entitled`` / ``not_ready``) so the fan-out never silently
    truncates the set without a trace (#1554 acceptance).
    """
    capability = collection_capability_key(row.collection_key)
    if capability not in operator.capabilities:
        _log.info(
            "doc_collection_fanout_dropped",
            tenant_id=str(operator.tenant_id),
            collection_key=row.collection_key,
            reason="not_entitled",
        )
        return False
    if row.status != _READY_STATUS:
        _log.info(
            "doc_collection_fanout_dropped",
            tenant_id=str(operator.tenant_id),
            collection_key=row.collection_key,
            reason="not_ready",
            status=row.status,
        )
        return False
    return True


async def resolve_entitled_ready_collections(
    session: AsyncSession,
    operator: Operator,
    *,
    requested_keys: list[str] | None,
) -> list[DocCollection]:
    """Resolve a cross-collection fan-out scope to its answerable collections.

    The fan-out analogue of :func:`resolve_entitled_ready_collection` (T5
    #1554, ``search_docs`` only). Resolves *requested_keys* (an explicit
    ``collections=[…]`` list) or — when ``requested_keys is None`` (the
    ``all`` sentinel) — every collection visible to the tenant, then keeps
    only those the operator is **entitled** to (``meho-docs:<key>``) and that
    are **ready**. Non-entitled and not-ready members are dropped from the
    set rather than failing the whole query (no silent *total* truncation:
    each drop is logged with its reason so an operator can see why a
    collection did not contribute), per the #1554 acceptance criteria. The
    returned list is **sorted by ``collection_key``** so the fan-out order —
    and the derived ``audit_collection`` set — is deterministic.

    Args:
        session: Active async DB session.
        operator: The verified operator (``tenant_id`` scopes the catalogue
            read; ``capabilities`` gates per-collection entitlement).
        requested_keys: The explicit keys to fan out across (deduplicated
            by the caller), or ``None`` for the ``all`` sentinel (every
            visible collection).

    Returns:
        The entitled, ready :class:`DocCollection` read shapes, sorted by
        ``collection_key``.

    Raises:
        NoEntitledReadyCollectionError: the scope resolved to zero entitled,
            ready collections (every requested member was unknown,
            non-entitled, or not-ready, or the tenant has no entitled ready
            collection at all under ``all``).
    """
    stmt = select(DocCollectionORM).where(
        (DocCollectionORM.tenant_id == operator.tenant_id) | (DocCollectionORM.tenant_id.is_(None))
    )
    if requested_keys is not None:
        stmt = stmt.where(DocCollectionORM.collection_key.in_(requested_keys))
    result = await session.execute(stmt)
    by_key = _visible_rows_by_key(list(result.scalars().all()), operator.tenant_id)

    # An explicit key that resolved to no visible row is dropped-and-logged
    # too — the same "no silent truncation" contract the entitlement /
    # readiness drops honour, so an operator who typos a key in a fan-out
    # list sees it named rather than silently missing from the result.
    if requested_keys is not None:
        for missing in sorted(set(requested_keys) - by_key.keys()):
            _log.info(
                "doc_collection_fanout_dropped",
                tenant_id=str(operator.tenant_id),
                collection_key=missing,
                reason="unknown",
            )

    entitled_ready: list[DocCollection] = [
        DocCollection.model_validate(by_key[key], from_attributes=True)
        for key in sorted(by_key)
        if _keep_if_entitled_ready(operator, by_key[key])
    ]

    if not entitled_ready:
        raise NoEntitledReadyCollectionError(sorted(requested_keys or []))

    _log.info(
        "doc_collection_fanout_resolved",
        tenant_id=str(operator.tenant_id),
        scope="all" if requested_keys is None else "explicit",
        queried=[c.collection_key for c in entitled_ready],
    )
    return entitled_ready
