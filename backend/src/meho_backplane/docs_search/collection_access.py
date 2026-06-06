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
   ``"ready"`` (``provisioning`` / ``rebuilding`` / ``disabled``) is not
   answerable yet; this is the typed :exc:`CollectionNotReadyError`. The
   richer reachability *probe* is T6 (#1555); T3 reads only the registry
   ``status`` column the T1 substrate already carries.

Each surface translates these typed errors into its own transport shape
(HTTP status at the REST route, JSON-RPC ``-32xxx`` at the MCP face) so
the policy stays transport-independent and this module imports nothing
transport-specific.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

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
    "CollectionForbiddenError",
    "CollectionNotReadyError",
    "UnknownCollectionError",
    "collection_capability_key",
    "resolve_entitled_ready_collection",
]

_log = structlog.get_logger(__name__)

#: The lifecycle status a collection must carry to be answerable. The
#: other states (``provisioning`` / ``rebuilding`` / ``disabled``) are
#: not-ready: the catalogue knows the collection but the backend is not
#: serving it yet.
_READY_STATUS = "ready"

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
    """

    def __init__(self, collection_key: str) -> None:
        super().__init__(
            f"not entitled to doc collection {collection_key!r}",
            collection_key=collection_key,
        )


class CollectionNotReadyError(CollectionAccessError):
    """The collection is known + entitled but its backend is not serving yet.

    The registry ``status`` is not ``"ready"`` (``provisioning`` /
    ``rebuilding`` / ``disabled``). Surfaces map this to a 409/503-class
    rejection — the request was well-formed and authorised; the collection
    is simply not answerable at this moment.
    """

    def __init__(self, collection_key: str, status: str) -> None:
        super().__init__(
            f"doc collection {collection_key!r} is not ready (status={status!r})",
            collection_key=collection_key,
        )
        self.status = status


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
    key it can see but is not entitled to, and ``not ready`` only once
    both of those pass.

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
        CollectionNotReadyError: The collection's registry ``status`` is
            not ``"ready"``.
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
            collection_key=row.collection_key,
            required_capability=capability,
        )
        raise CollectionForbiddenError(row.collection_key)

    if row.status != _READY_STATUS:
        _log.info(
            "doc_collection_not_ready",
            tenant_id=str(operator.tenant_id),
            collection_key=row.collection_key,
            status=row.status,
        )
        raise CollectionNotReadyError(row.collection_key, row.status)

    return DocCollection.model_validate(row, from_attributes=True)
