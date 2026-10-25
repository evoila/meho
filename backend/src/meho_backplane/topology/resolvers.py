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
traversal verbs in :mod:`meho_backplane.topology.query` resolve their
anchors through :func:`resolve_node` too, so the "name → several rows
in this tenant" rule stays single-sourced between traversal and the
write-path callers.

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

from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy import Select, select

from meho_backplane.db.models import GraphNode
from meho_backplane.topology.identity_mode import NOT_LEGACY_SCOPE

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AmbiguousNodeError",
    "NodeNotFoundError",
    "node_identity_statement",
    "pick_unique_node",
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

    def __init__(self, name: str, kinds: list[str], scopes: list[str] | None = None) -> None:
        self.name = name
        self.kinds = kinds
        #: Set when one ``(kind, name)`` exists in several source scopes
        #: (after the #3749 cut-over); ``None`` for a kind ambiguity.
        self.scopes = scopes
        if scopes:
            message = (
                f"node {kinds[0]!r} {name!r} is ambiguous in this tenant — "
                f"several sources report it (scopes {scopes!r})"
            )
        else:
            message = (
                f"node name {name!r} is ambiguous in this tenant — it exists "
                f"as kinds {sorted(kinds)!r}; pass kind= to disambiguate"
            )
        super().__init__(message)


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

    1. Candidates are the tenant's rows named ``name`` (and of ``kind``
       when it is supplied). ``legacy:<uuid>`` rows are never candidates:
       the scope cut-over (#3749) set them aside because no target
       vouched for them (see
       :data:`~meho_backplane.topology.identity_mode.NOT_LEGACY_SCOPE`).
    2. **0 candidates** → :class:`NodeNotFoundError` (the name does not
       exist in this tenant — including the case where it exists only
       in *another* tenant; cross-tenant references never resolve to
       the other tenant's node).
    3. Candidates of **more than one kind** and no ``kind`` →
       :class:`AmbiguousNodeError` listing the kinds. The caller must
       re-issue with ``kind=``.
    4. **More than one candidate of one kind** → only possible after
       the cut-over, when two sources own the same name (two clusters
       each with a ``default`` namespace).
       :class:`AmbiguousNodeError` lists their ``scopes``. The resolver
       never picks one.
    5. **Exactly one candidate** → return it.

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
        kind: Optional ``graph_node.kind`` pin. When supplied, only
            rows of that kind are candidates.

    Returns:
        The matching :class:`~meho_backplane.db.models.GraphNode` ORM
        row. Callers that need a Pydantic read shape convert via
        ``Model.model_validate(node, from_attributes=True)``.

    Raises:
        NodeNotFoundError: Nothing matches in this tenant.
        AmbiguousNodeError: Bare-name lookup hit multiple kinds (pass
            ``kind=``), or several sources own the same ``(kind, name)``
            (``scopes`` is set).
    """
    stmt = _candidates(select(GraphNode), tenant_id, name, kind)
    rows = list((await session.execute(stmt)).scalars().all())
    return pick_unique_node(rows, name, kind)


class _Candidate(Protocol):
    """What :func:`pick_unique_node` reads from a candidate row."""

    @property
    def kind(self) -> str: ...

    @property
    def source_scope(self) -> str | None: ...


def node_identity_statement(tenant_id: UUID, name: str, kind: str | None = None) -> Select[Any]:
    """The :func:`resolve_node` candidate query with identity columns only.

    Selects ``id, kind, name, source, source_scope, last_seen`` and never
    ``properties``, so a caller that must stay within a memory budget
    (the bounded closure, #3764) can find the root without loading its
    properties. The caller runs the statement and hands the rows to
    :func:`pick_unique_node`, which applies the resolution rules.
    """
    return _candidates(
        select(
            GraphNode.id,
            GraphNode.kind,
            GraphNode.name,
            GraphNode.source,
            GraphNode.source_scope,
            GraphNode.last_seen,
        ),
        tenant_id,
        name,
        kind,
    )


def pick_unique_node[C: _Candidate](rows: list[C], name: str, kind: str | None) -> C:
    """Apply the :func:`resolve_node` rules 2-5 to the candidate *rows*."""
    if not rows:
        raise NodeNotFoundError(name, kind=kind)
    kinds = sorted({row.kind for row in rows})
    if len(kinds) > 1:
        raise AmbiguousNodeError(name, kinds)
    if len(rows) > 1:
        raise AmbiguousNodeError(
            name, kinds, scopes=sorted(row.source_scope or "unresolved" for row in rows)
        )
    return rows[0]


def _candidates[S: Select[Any]](stmt: S, tenant_id: UUID, name: str, kind: str | None) -> S:
    """Rule 1: the tenant's rows named *name* (and of *kind*), never ``legacy:`` rows."""
    stmt = stmt.where(
        GraphNode.tenant_id == tenant_id,
        GraphNode.name == name,
        NOT_LEGACY_SCOPE,
    )
    if kind is not None:
        stmt = stmt.where(GraphNode.kind == kind)
    return stmt
