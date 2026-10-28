# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector-id parsing + ``endpoint_descriptor`` lookup helpers.

The G0.6 dispatcher (T5, #396) takes a ``connector_id`` string of the
form ``"<impl_id>-<version>"`` and resolves it into the
``(product, version, impl_id)`` natural-key triple the
``endpoint_descriptor`` table is keyed on. This module owns:

* :func:`parse_connector_id` -- the parser. See its docstring for the
  encoding contract and the v1-style backward-compatible fallback.
* :func:`lookup_descriptor` -- descriptor lookup in the caller's
  *visible scope* (see :func:`visible_scope_clause`). Returns ``None``
  if no enabled descriptor matches.
* :func:`visible_scope_clause` -- the one tenant-scope predicate that
  dispatch, preview, ``list_operation_groups`` and ``search_operations``
  share, so they always agree on which rows a tenant can use.
* :func:`descriptor_exists_any_state` -- ``is_enabled``-agnostic
  presence probe. Used only to classify a non-dispatchable sub-op as
  *present-but-disabled* versus *truly absent*; never used to dispatch.
* :func:`count_known_ops` -- count of enabled descriptors for a given
  ``(product, version, impl_id)``. Returned in the ``unknown_op`` error
  payload so the operator has a "did you mean…" signal without the
  full enumeration the meta-tools (T8) provide.
* :func:`find_connectors_with_op` -- the other connectors the caller can
  see that have the same ``op_id`` enabled. Named in the ``unknown_op``
  error so a caller who picked the wrong connector version learns where
  the op lives (#4031).

The split lets the dispatcher's :func:`dispatch` body keep step 2's
descriptor resolution one helper call instead of three.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, select
from sqlalchemy.orm import aliased

from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup

__all__ = [
    "connector_class_registered",
    "connector_exists",
    "count_known_ops",
    "descriptor_exists_any_state",
    "find_connectors_with_op",
    "lookup_descriptor",
    "parse_connector_id",
    "visible_scope_clause",
]


# Pattern for connector ids of the form ``"<head>-<version>"`` where
# ``head`` may itself contain hyphens (``"harbor-rest"`` etc.) and
# ``version`` is the tail segment. The version segment is pinned to
# ``[0-9][A-Za-z0-9._]*`` so it always starts with a digit -- this
# avoids ambiguous splits like ``"foo-bar"`` (no version) and keeps
# ``"vault-1.x"`` parsing into ``head="vault"`` / ``version="1.x"``.
_CONNECTOR_ID_TAIL_VERSION = re.compile(r"^(?P<head>.+)-(?P<version>[0-9][A-Za-z0-9._]*)$")


def parse_connector_id(connector_id: str) -> tuple[str, str, str]:
    """Split ``connector_id`` into ``(product, version, impl_id)``.

    The connector_id convention from the parent Initiative (#388):

    * ``"harbor-rest-2.x"`` -> product=``"harbor"``, version=``"2.x"``,
      impl_id=``"harbor-rest"``.
    * ``"vault"`` (v1-style, single-key product) -> product=``"vault"``,
      version=``""``, impl_id=``""``. Backward-compatible with the
      shipped v1 registrations.
    * ``"k8s-1.x"`` -> product=``"k8s"``, version=``"1.x"``,
      impl_id=``"k8s"``.

    The parser is forgiving: an unparseable id falls back to
    ``(connector_id, "", "")`` so the lookup just misses cleanly with
    ``unknown_op`` rather than throwing on input that the operator
    typo-ed. The natural-key index on ``endpoint_descriptor`` is what
    actually catches "no such connector" -- the parser is just a
    canonicaliser.
    """
    match = _CONNECTOR_ID_TAIL_VERSION.match(connector_id)
    if match is None:
        # No version suffix -- treat as v1-style single-product slug.
        return connector_id, "", ""
    head = match.group("head")
    version = match.group("version")
    # ``head`` is the full impl_id including a possible product prefix
    # (``"harbor-rest"``). The product is the first hyphen segment of
    # the impl_id (``"harbor"``); the rest carries the impl
    # discriminator. Single-segment heads (``"vault-1.x"``) produce
    # impl_id == product, matching how typed registrations encode the
    # single-impl case.
    product = head.split("-", 1)[0] if "-" in head else head
    return product, version, head


def visible_scope_clause(
    model: Any,
    *,
    tenant_id: UUID | None,
    product: str | SQLColumnExpression[str],
    version: str | SQLColumnExpression[str],
    impl_id: str | SQLColumnExpression[str],
) -> ColumnElement[bool]:
    """Return the ``tenant_id`` predicate for the rows a tenant may use.

    *model* is :class:`EndpointDescriptor` or :class:`OperationGroup`
    (any mapped class with a ``tenant_id`` column) and the predicate
    applies to that class's rows for *(product, version, impl_id)*.

    The tenant's own connector row is authoritative (#3870). When the
    tenant has **any** ``endpoint_descriptor`` row for the connector
    triple, in any ``is_enabled`` state, only the tenant's rows are
    visible. The built-in rows (``tenant_id IS NULL``) for that triple
    are not used at all. An op that is disabled or absent on the
    tenant's row is therefore not usable, even if the built-in row has
    it enabled. When the tenant has no row for the triple, the built-in
    rows are visible as before.

    So at most one of the two buckets is visible for a given triple.
    Dispatch, preview, ``list_operation_groups`` and
    ``search_operations`` all filter on this one predicate, so they
    cannot disagree.

    ``tenant_id=None`` is the operator-less chassis path: only built-in
    rows are visible.

    *product* / *version* / *impl_id* are usually plain strings. They may
    also be the columns of *model* itself; the ownership probe then
    correlates per row, so one query can span many connectors and still
    apply the same rule to each one (:func:`find_connectors_with_op`).

    The ownership probe runs over an alias of ``endpoint_descriptor`` so
    it never correlates with an outer query over the same table.
    """
    if tenant_id is None:
        return model.tenant_id.is_(None)  # type: ignore[no-any-return]
    owned = aliased(EndpointDescriptor)
    tenant_owns_connector = (
        select(owned.id)
        .where(
            owned.tenant_id == tenant_id,
            owned.product == product,
            owned.version == version,
            owned.impl_id == impl_id,
        )
        .exists()
    )
    return (model.tenant_id == tenant_id) | and_(  # type: ignore[no-any-return]
        model.tenant_id.is_(None), ~tenant_owns_connector
    )


async def lookup_descriptor(
    *,
    tenant_id: UUID,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
) -> EndpointDescriptor | None:
    """Look up an :class:`EndpointDescriptor` for *(product, version, impl_id, op_id)*.

    Tenant scoping is :func:`visible_scope_clause`: when the tenant has
    its own row for the connector, only that row counts and a disabled
    or absent op there resolves to ``None``. There is no fallback to the
    built-in row (#3870). Otherwise the built-in row is used. Because
    only one bucket is ever visible, one SELECT finds at most one row.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                visible_scope_clause(
                    EndpointDescriptor,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                ),
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id == op_id,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        return result.scalar_one_or_none()


async def descriptor_exists_any_state(
    *,
    tenant_id: UUID,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
) -> bool:
    """Return whether a descriptor row exists for the op id, **ignoring** ``is_enabled``.

    The ``is_enabled``-agnostic sibling of :func:`lookup_descriptor`,
    used **only to classify** a sub-op that :func:`lookup_descriptor`
    could not resolve. It never returns a descriptor and is never used
    to dispatch -- a disabled op stays non-dispatchable. Its job is to
    tell *present-but-disabled* (a row exists, ``is_enabled = false``)
    apart from *truly absent* (no row at all) for callers that need to
    distinguish the two remediation paths (re-enable vs ingest).

    Scoping is the same :func:`visible_scope_clause` that
    :func:`lookup_descriptor` uses: the tenant's own rows when it has a
    row for the connector, the built-in rows otherwise. A single
    ``LIMIT 1`` existence probe -- the caller has already established
    (via :func:`lookup_descriptor` returning ``None``) that no *enabled*
    row matches, so the only open question is presence in any state.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor.id)
            .where(
                visible_scope_clause(
                    EndpointDescriptor,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                ),
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id == op_id,
            )
            .limit(1)
        )
        return result.first() is not None


async def count_known_ops(
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
) -> int:
    """Count *caller-visible* enabled descriptors for *(product, version, impl_id)*.

    Returned in the ``unknown_op`` error's ``extras`` so the caller has
    a "did you mean…" signal without enumerating every op id (the
    actual enumeration belongs to the ``list_operation_groups`` /
    ``search_operations`` meta-tools shipped in T8 #399).

    The count is scoped to what the calling operator can dispatch: the
    same :func:`visible_scope_clause` :func:`lookup_descriptor` uses (the
    tenant's own rows when it has a row for the connector, the built-in
    rows otherwise). It never counts another tenant's rows. Without that
    the count is a cross-tenant oracle — a connector private to tenant B
    would leak its enabled-op count into a tenant-A caller's
    ``unknown_op`` error payload.

    ``tenant_id=None`` is the operator-less chassis / CLI dispatch path
    (the typed-connector ``execute`` shims, which resolve descriptors
    against global rows only): the predicate collapses to global rows —
    matching those callers' own ``tenant_id IS NULL`` descriptor query.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor.id).where(
                visible_scope_clause(
                    EndpointDescriptor,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                ),
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        return len(result.all())


#: Cap on how many other connectors an ``unknown_op`` error names. A
#: handful is enough to point the caller at the right one; the cap keeps
#: the error small when an op id is shared by many connectors.
_CONNECTORS_WITH_OP_CAP: int = 10


async def find_connectors_with_op(
    *,
    tenant_id: UUID | None,
    op_id: str,
    exclude: tuple[str, str, str],
) -> list[str]:
    """Return the ids of other caller-visible connectors that have *op_id* enabled.

    Runs only on the ``unknown_op`` error path (#4031). It finds the
    connectors a caller probably meant, for example the other version of
    the same product. *exclude* is the parsed *(product, version,
    impl_id)* triple of the connector the caller named.

    Tenant scope is :func:`visible_scope_clause`, applied per connector:
    the tenant's own rows when it has a row for that connector, the
    built-in rows otherwise. Another tenant's connector is never named.
    ``tenant_id=None`` sees built-in rows only, the same as
    :func:`count_known_ops`.

    Each id is rendered as ``<impl_id>-<version>`` (``<product>`` for a
    v1-style row with no version) and kept only when it parses back to
    the same triple, so every named id can be passed to dispatch as is.
    The list is sorted and capped at :data:`_CONNECTORS_WITH_OP_CAP`.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(
                EndpointDescriptor.product,
                EndpointDescriptor.version,
                EndpointDescriptor.impl_id,
            )
            .where(
                visible_scope_clause(
                    EndpointDescriptor,
                    tenant_id=tenant_id,
                    product=EndpointDescriptor.product,
                    version=EndpointDescriptor.version,
                    impl_id=EndpointDescriptor.impl_id,
                ),
                EndpointDescriptor.op_id == op_id,
                EndpointDescriptor.is_enabled.is_(True),
            )
            .distinct()
        )
        triples = {(row.product, row.version, row.impl_id) for row in result}
    triples.discard(exclude)
    connector_ids: set[str] = set()
    for product, version, impl_id in triples:
        connector_id = f"{impl_id}-{version}" if impl_id and version else product
        if parse_connector_id(connector_id) == (product, version, impl_id):
            connector_ids.add(connector_id)
    return sorted(connector_ids)[:_CONNECTORS_WITH_OP_CAP]


async def connector_exists(
    *,
    tenant_id: UUID,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return whether *caller-visible* operations data exists for *(product, version, impl_id)*.

    "Exists" is deliberately decoupled from ``is_enabled`` /
    ``review_status``: a connector that has registered descriptors or
    groups but has none *enabled* yet is still a *known* connector. The
    meta-tools use this to tell "unknown connector_id" (no rows at all
    for the triple — surface a 404 so a malformed/mis-shaped id fails
    loud) apart from "known connector, zero enabled groups" (rows exist
    but none enabled — a meaningful empty list, ``200 []``).

    Existence is scoped to what the calling operator can see: built-in /
    global rows (``tenant_id IS NULL``) plus this tenant's own rows
    (``tenant_id == tenant_id``). This mirrors the tenant boundary the
    data-returning queries enforce (``list_operation_groups`` /
    ``search_operations`` in ``meta_tools``). Without it the existence
    probe would be a cross-tenant presence oracle — a connector private
    to tenant B would make the gate return ``True`` for a tenant-A
    caller, yielding ``200 []`` where the caller-visible answer is
    "unknown" and the correct response is ``404``.

    The DB is the source of truth rather than the in-memory connector
    registry: every registered connector (typed, v1-compat, and
    ingested generic) writes ``endpoint_descriptor`` / ``operation_group``
    rows, and the registry is process-local while the rows are durable.
    Two cheap ``LIMIT 1`` existence probes — descriptors first (the
    common case), groups as the fallback for a connector whose groups
    were seeded ahead of its operations.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptor_hit = await session.execute(
            select(EndpointDescriptor.id)
            .where(
                (EndpointDescriptor.tenant_id.is_(None))
                | (EndpointDescriptor.tenant_id == tenant_id),
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
            )
            .limit(1)
        )
        if descriptor_hit.first() is not None:
            return True
        group_hit = await session.execute(
            select(OperationGroup.id)
            .where(
                (OperationGroup.tenant_id.is_(None)) | (OperationGroup.tenant_id == tenant_id),
                OperationGroup.product == product,
                OperationGroup.version == version,
                OperationGroup.impl_id == impl_id,
            )
            .limit(1)
        )
        return group_hit.first() is not None


def connector_class_registered(
    *,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return whether a v2 connector *class* is registered for the parsed triple.

    The discriminator behind the "registered but not ingested" signal.
    :func:`connector_exists` probes only DB rows (``endpoint_descriptor`` /
    ``operation_group``); a connector whose class is registered via
    :func:`~meho_backplane.connectors.registry.register_connector_v2` but
    has not yet had operations ingested / typed-registered has *zero* DB
    rows, so ``connector_exists`` returns ``False`` for it. That case is
    not "unknown connector" — it is "known class, awaiting ingest". This
    helper tells the two apart so the meta-tools can return an
    operator-actionable ``connector_not_ingested`` hint instead of an
    opaque unknown-connector error.

    *(product, version, impl_id)* is the triple
    :func:`parse_connector_id` derived from the caller's ``connector_id``.
    The v2 registry is keyed on the *registration* triple, which for most
    connectors equals the parsed triple but for SDDC differs (registry
    ``product="sddc-manager"`` vs parsed ``product="sddc"``). To match the
    exact rows ``GET /api/v1/connectors`` labels ``state="registered"``
    (see :func:`~meho_backplane.operations.ingest.list_connectors._class_side_only_items`),
    each registry entry is rendered to its ``connector_id`` and re-parsed;
    a registry entry counts as a hit only when its re-parsed triple equals
    the caller's. This mirrors the listing's lossless-round-trip contract
    (#773) so a ``connector_not_ingested`` answer here implies a
    ``state="registered"`` row there, and vice versa.

    v1-compat shim entries (the registry's ``(product, "", "")`` rows the
    v1 :func:`~meho_backplane.connectors.registry.register_connector`
    writes) are skipped: an empty ``version`` / ``impl_id`` is a
    resolver-internal compatibility detail, never a separately registered
    connector, and never the source of a ``state="registered"`` listing
    row.

    Registry-only (in-memory, process-local), so no DB round-trip — the
    caller has already established (via :func:`connector_exists`) that the
    DB has no rows for the triple.
    """
    for _reg_product, reg_version, reg_impl_id in all_connectors_v2():
        if not reg_version or not reg_impl_id:
            # v1-compat shim (product, "", "") — not a registered connector.
            continue
        connector_id = f"{reg_impl_id}-{reg_version}"
        parsed_product, parsed_version, parsed_impl_id = parse_connector_id(connector_id)
        if (parsed_product, parsed_version, parsed_impl_id) == (product, version, impl_id):
            return True
    return False
