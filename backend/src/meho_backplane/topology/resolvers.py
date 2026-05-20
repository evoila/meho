# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Public name → :class:`GraphNode` resolution for the topology graph.

Task #594 (G9.2-T2). The annotation flow (T3 / T4) needs a reusable way
to resolve an edge endpoint by ``name`` (optionally pinned by ``kind``)
to the matching :class:`~meho_backplane.db.models.GraphNode` row within
a single tenant. The pre-existing ``_assert_anchor_unambiguous`` in
:mod:`meho_backplane.topology.query` (G9.1-T4) only validates anchor
ambiguity for the read verbs — it returns ``None``, never fetches a
row, and is silent on not-found. :func:`resolve_node` is a superset:

* fetches and returns the unique :class:`GraphNode` row, and
* raises a new :class:`NodeNotFoundError` when nothing matches.

The ambiguity check is the same code on both call paths: the read
traversal verbs in :mod:`meho_backplane.topology.query` delegate their
own check to :func:`_collect_distinct_kinds` so the
"name → multiple kinds in this tenant" surface stays single-sourced
between traversal and the new write-path callers.

Not-found semantics intentionally differ between the two surfaces:

* :func:`resolve_node` raises :class:`NodeNotFoundError` on no match —
  the contract the write verbs (annotate / unannotate / list-edges)
  need.
* The traversal verbs in :mod:`query` preserve their G9.1 not-found
  behavior unchanged: a non-existent anchor surfaces today as an empty
  result rather than an exception. Opting traversal into the stricter
  raise-on-not-found contract is deliberately out of scope for this
  task — it would break the existing G9.1 query tests.

Tenant scoping is mandatory and non-optional. A name seeded only in
another tenant resolves to :class:`NodeNotFoundError`, never to that
other tenant's node — the substrate the §11 tenant-boundary test in
Initiative #364 relies on.

:class:`AmbiguousNodeError` lives here (not in :mod:`query`) because
the ambiguity rule is the property of the resolver, not of any one
read verb. :mod:`query` re-exports it so existing
``from meho_backplane.topology.query import AmbiguousNodeError`` call
sites keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text

from meho_backplane.db.models import GraphNode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AmbiguousNodeError",
    "NodeNotFoundError",
    "resolve_node",
]


class AmbiguousNodeError(ValueError):
    """A node name resolved to more than one ``kind`` and no ``kind`` was given.

    ``graph_node`` uniqueness is ``(tenant_id, kind, name)``. When a
    name is requested alone and the tenant holds multiple kinds with
    that name, resolving on all of them would either anchor traversal
    on an unintended kind or pick one arbitrarily — both wrong. The
    resolver refuses instead; the caller must re-issue with an
    explicit ``kind``.

    Subclasses :class:`ValueError` (rather than :class:`HTTPException`)
    so it survives unchanged inside the topology layer and stays
    independent of any HTTP framing. The API layer
    (:mod:`meho_backplane.api.v1.topology`) maps it to a 409 with the
    candidate kinds echoed in ``detail``.
    """

    def __init__(self, name: str, kinds: list[str]) -> None:
        self.name = name
        self.kinds = kinds
        super().__init__(
            f"node name {name!r} is ambiguous in this tenant — it exists "
            f"as kinds {sorted(kinds)!r}; pass kind= to disambiguate"
        )


class NodeNotFoundError(ValueError):
    """No :class:`GraphNode` matched the resolver's ``(tenant_id, name[, kind])``.

    Raised by :func:`resolve_node` when the resolver finds zero rows
    for the requested ``(tenant_id, name)`` — or, when ``kind`` is
    pinned, the ``(tenant_id, kind, name)`` unique key. A name that
    exists only in another tenant resolves here, never to that
    tenant's row: the tenant boundary holds.

    Subclasses :class:`ValueError` for the same reason as
    :class:`AmbiguousNodeError` — kept HTTP-agnostic so the topology
    layer stays usable from CLI / MCP / background workers without
    importing FastAPI. The API layer maps it to a 404 with the
    requested ``name`` (and ``kind``, when supplied) echoed in
    ``detail``.
    """

    def __init__(self, name: str, kind: str | None = None) -> None:
        self.name = name
        self.kind = kind
        descriptor = f"name {name!r}" if kind is None else f"({kind!r}, {name!r})"
        super().__init__(f"no graph_node matched {descriptor} in this tenant")


#: ``SELECT DISTINCT kind FROM graph_node WHERE tenant_id = :tenant_id AND name = :name``.
#:
#: Module-level fully-literal ``text("...")`` so the
#: ``avoid-sqlalchemy-text`` SAST rule does not fire (no string
#: interpolation; every value is a ``:named`` bind). Mirrors the
#: pattern in :mod:`meho_backplane.topology.query`.
_ANCHOR_KINDS_SQL = text(
    """
    SELECT DISTINCT kind
    FROM graph_node
    WHERE tenant_id = :tenant_id
      AND name = :name
    """
)


async def _collect_distinct_kinds(
    session: AsyncSession,
    *,
    tenant_id: str,
    name: str,
) -> list[str]:
    """Return the distinct ``kind`` values for ``(tenant_id, name)``.

    Single source of truth for the ambiguity probe shared by
    :func:`resolve_node` and the traversal verbs'
    ``_assert_anchor_unambiguous`` in :mod:`meho_backplane.topology.query`.
    Returns an empty list when the name does not exist in this tenant.

    Kept private — the public ambiguity surface is
    :class:`AmbiguousNodeError`, raised by callers that decide what to
    do with the kind list. ``tenant_id`` is taken as ``str`` because
    asyncpg's text codec accepts the string form for UUID binds (same
    convention :mod:`query` uses).
    """
    result = await session.execute(
        _ANCHOR_KINDS_SQL,
        {"tenant_id": tenant_id, "name": name},
    )
    return [row._mapping["kind"] for row in result.fetchall()]


async def resolve_node(
    session: AsyncSession,
    tenant_id: UUID,
    name: str,
    kind: str | None = None,
) -> GraphNode:
    """Resolve ``(tenant_id, name[, kind])`` to a unique :class:`GraphNode`.

    The resolver shape the G9.2 annotation flow (#364 — T3 annotate /
    unannotate, T4 list-edges) calls before writing or reading an
    edge endpoint by operator-supplied name. Works for **non-target**
    nodes (``target_id IS NULL``) as well as registered targets —
    annotation routinely references e.g. ``vault-role`` /
    ``keycloak-realm`` rows that are not managed targets.

    Resolution rules:

    1. If ``kind`` is supplied, look up the ``(tenant_id, kind, name)``
       unique row (index ``graph_node_tenant_kind_name_idx``). At most
       one match. Found → return the :class:`GraphNode`; absent →
       :class:`NodeNotFoundError`.
    2. If ``kind`` is omitted, probe the distinct kinds for
       ``(tenant_id, name)``:

       - **0 kinds** → :class:`NodeNotFoundError` (the name does not
         exist in this tenant — including the case where it exists
         only in *another* tenant; cross-tenant references never
         resolve to the other tenant's node).
       - **1 kind** → the ``(tenant_id, kind, name)`` row is unique by
         the index; fetch and return it.
       - **>1 kinds** → :class:`AmbiguousNodeError` listing the
         candidate kinds. The caller must re-issue with ``kind=`` to
         disambiguate.

    Args:
        session: An open :class:`AsyncSession`. The resolver does not
            open or close the session; it is the caller's
            transactional boundary (mirrors the per-request session
            shape used elsewhere in the chassis service layer).
        tenant_id: The tenant scope. Mandatory and non-optional; no
            cross-tenant resolution is possible by construction.
        name: ``graph_node.name`` to resolve. Exact match, no alias
            resolution — graph-node aliasing is the CLI/MCP front's
            job (deferred per ``docs/codebase/topology.md`` "Known
            issues").
        kind: Optional ``graph_node.kind`` pin. When supplied, anchors
            on the ``(tenant_id, kind, name)`` unique index directly
            and skips the ambiguity probe.

    Returns:
        The matching :class:`~meho_backplane.db.models.GraphNode` ORM
        row. Callers that need a Pydantic read shape convert via
        ``Model.model_validate(node, from_attributes=True)``.

    Raises:
        NodeNotFoundError: Nothing matches in this tenant.
        AmbiguousNodeError: Bare-name lookup hit multiple kinds; pass
            ``kind=`` to disambiguate.
    """
    tenant_str = str(tenant_id)

    if kind is None:
        kinds = await _collect_distinct_kinds(session, tenant_id=tenant_str, name=name)
        if not kinds:
            raise NodeNotFoundError(name)
        if len(kinds) > 1:
            raise AmbiguousNodeError(name, kinds)
        kind = kinds[0]

    # Pinned (tenant_id, kind, name) — unique-index lookup, at most one row.
    stmt = select(GraphNode).where(
        GraphNode.tenant_id == tenant_id,
        GraphNode.kind == kind,
        GraphNode.name == name,
    )
    result = await session.execute(stmt)
    node = result.scalar_one_or_none()
    if node is None:
        # Reached only when the caller supplied ``kind`` (the
        # ``kind is None`` branch already proved the row exists).
        # A pinned (tenant_id, kind, name) miss is a clean
        # not-found — the unique index guarantees there is no
        # alternative interpretation.
        raise NodeNotFoundError(name, kind=kind)
    return node
