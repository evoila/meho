# topology — the read-side recursive-CTE graph traversal substrate

## Overview

`backend/src/meho_backplane/topology/` is the read surface every
blast-radius check and topology question in v0.2 dispatches through. It
ships three async query verbs and the two Pydantic value types around
them:

- `find_dependents(operator, name_or_alias, *, kind=None, depth=16, kind_filter=None)`
  — reverse traversal, "what depends on me".
- `find_dependencies(operator, name_or_alias, *, kind=None, depth=16, kind_filter=None)`
  — forward traversal, "what I depend on".
- `find_path(operator, from_name, to_name, *, from_kind=None, to_kind=None, max_hops=8)`
  — shortest unweighted path, or `None` if unreachable.

Every verb returns **one row per reachable node** (a node reachable by
several converging paths is collapsed to its minimum-depth occurrence —
`CYCLE` alone only dedupes within a single branch). An anchor requested
by bare `name` that resolves to more than one `kind` in the tenant
raises `AmbiguousNodeError` rather than traversing a merged closure;
pass the optional `kind` (or `from_kind` / `to_kind` for `find_path`) to
pin the `(tenant_id, kind, name)` unique row.

The package is **read-only**. `graph_node` / `graph_edge` rows are
written by the refresh service (G9.1-T3, #450); the schema and ORM
models are migration `0007` (G9.1-T1, #448). This package never
inserts, updates, or deletes.

The API (T5), CLI (T6), and MCP (T7) fronts consume `query.py` as a
thin shell and never re-derive the traversal or the tenant boundary.

## Key types

### `TopologyNode` — frozen Pydantic v2

| Field | Type | Meaning |
|---|---|---|
| `id` | `UUID` | `graph_node.id`. |
| `kind` | `str` | `graph_node.kind` (closed enum from migration 0007). |
| `name` | `str` | `graph_node.name`, unique within `(tenant_id, kind)`. |
| `properties` | `dict` | `graph_node.properties` JSONB; wrapped in `MappingProxyType` after validation so the frozen model is deeply immutable, serialised back to a plain `dict`. |
| `depth` | `int` | Distance from the query root: root = 0, immediate = 1, transitive = 2, … |
| `via_edge_kind` | `str \| None` | The `graph_edge.kind` of the edge used to reach this node; `None` for the root. |

### `TopologyPath` — frozen Pydantic v2

| Field | Type | Meaning |
|---|---|---|
| `nodes` | `tuple[TopologyNode, ...]` | Ordered from the `from` node (`depth == 0`) to the `to` node (`depth == total_hops`). |
| `total_hops` | `int` | Number of edges traversed; equals `len(nodes) - 1`. |

## Control flow

### Edge-direction model

An edge `from_node --kind--> to_node` reads "`from_node` depends on
`to_node`" (a `vm` `runs-on` a `host`: the vm depends on the host).

- **dependents** (reverse): from the frontier node `w`, join edges
  where `e.to_node_id = w.id`, step to `e.from_node_id`.
- **dependencies** (forward): join edges where `e.from_node_id = w.id`,
  step to `e.to_node_id`.

`find_dependents` and `find_dependencies` share `_traverse`, which
picks the reverse or forward **fully-literal** `text()` statement
(`_TRAVERSAL_SQL_REVERSE` / `_TRAVERSAL_SQL_FORWARD`) — only the two
join columns differ; tenant scoping, the optional `kind` anchor pin,
the optional `kind_filter`, the depth bound, the CYCLE guard, the
closure-wide dedupe, and the `(depth, name)` ordering are identical.
The two statements are kept separate (rather than one f-string with the
join columns interpolated) so the `avoid-sqlalchemy-text` SAST rule
does not fire — nothing is interpolated, every value is a `:named`
bind.

### Recursive CTE shape

The traversal is a single `WITH RECURSIVE walk AS (...) CYCLE id SET
is_cycle USING path` statement. The anchor row is the root at depth 0
(`via_edge_kind` NULL), filtered by `CAST(:kind AS text) IS NULL OR
n.kind = :kind` so a pinned `kind` resolves the `(tenant_id, kind,
name)` unique row. The recursive term joins `graph_edge` to the walk
frontier, scoped on `tenant_id` on both the edge and the destination
node, applies `CAST(:kind_filter AS text) IS NULL OR e.kind =
:kind_filter`, and bounds `w.depth < :depth`. The final projection
wraps the filtered walk in a `SELECT DISTINCT ON (id) ... ORDER BY id,
depth, name` subquery (keeping the minimum-depth occurrence of each
node) and re-orders the result by `(depth, name)`. `CYCLE` only
prevents revisiting a node on the *same* branch; the `DISTINCT ON`
collapse is what makes a converging DAG return one row per node rather
than one row per path.

### Anchor disambiguation

`graph_node` uniqueness is `(tenant_id, kind, name)`. Resolving an
anchor by `name` alone would match every kind with that name and
silently merge unrelated closures. `_assert_anchor_unambiguous` probes
the distinct kinds for `(tenant_id, name)` before traversing: a no-op
when `kind` is supplied or the name maps to one kind, but a raise of
`AmbiguousNodeError` (a `ValueError` subclass carrying `name` + the
matched `kinds`) when `kind` is omitted and the name spans multiple
kinds. `find_path` applies the same probe independently to each
endpoint. Alias → name resolution remains the T5/T6 router's job; this
substrate matches `graph_node.name` directly.

The root is always included at depth 0, so callers distinguish "node
exists but has no dependents" (one-element list) from "node does not
exist in this tenant" (empty list).

### Path search

`find_path` builds a `bi_edge` CTE — the union of forward and reversed
tenant-scoped edges — so reachability is undirected while storage
stays directed. The recursive `walk` accumulates `node_ids` and
`edge_kinds` arrays; `CYCLE node_id SET is_cycle USING visited` plus
the `hops < :max_hops` bound terminate the search. `ORDER BY hops
LIMIT 1` yields a shortest path. A second query materialises the
winning path's node rows; `_build_path_nodes` re-orders them into path
sequence and attaches `depth` / `via_edge_kind`.

### Cycle safety

The `CYCLE` clause makes PostgreSQL track the visited-node set per
branch and stop recursing into an already-visited node, flagging the
repeat row `is_cycle = true` (filtered out). An `A → B → A` graph
terminates instead of recursing forever. The `depth` / `max_hops`
bound is an independent second guard against acyclic-but-deep graphs.

## Dependencies

- `meho_backplane.db.engine.get_sessionmaker` — each verb opens its
  own `AsyncSession` (session-per-call, mirroring the memory / kb
  services). No caller-owned session.
- `meho_backplane.db.models.GraphNode` / `GraphEdge` — schema +
  indexes (migration `0007`). The recursive joins ride
  `graph_edge_tenant_from_idx` / `graph_edge_tenant_to_idx`.
- `meho_backplane.auth.operator.Operator` — `operator.tenant_id` is
  the only tenant boundary; every statement filters node and edge
  `tenant_id` against it.
- SQLAlchemy 2.0 `text()` with `:named` binds — same raw-SQL pattern
  `meho_backplane.retrieval.retriever` uses (`CAST(:x AS text) IS NULL
  OR ...` for optional filters, UUIDs passed as `str`). Every statement
  is a module-level fully-literal `text("...")`; nothing is
  interpolated so the `avoid-sqlalchemy-text` SAST rule does not fire.

## Known issues

- **PostgreSQL-only.** The `WITH RECURSIVE ... CYCLE` clause is not
  implemented by SQLite, so these verbs cannot run on the unit
  suite's per-test SQLite DB. Tests live in
  `backend/tests/integration/test_topology_query.py` against a real
  `pgvector/pgvector:pg16` testcontainer (Docker-gated skip on
  no-Docker sandboxes; runs in CI). v0.2 production is PostgreSQL, so
  this is a test-harness placement, not a runtime limitation. The pure
  Pydantic result-model contracts (deep `properties` immutability,
  `TopologyPath` invariants) have no DB dependency and are unit-tested
  in `backend/tests/test_topology_query_schemas.py`, which runs on
  every sandbox.
- **Unweighted only.** `find_path` treats every edge as cost 1.
  Weighted-edge support is deferred to v0.2.next per #363.
- **No alias resolution yet.** `name_or_alias` is matched against
  `graph_node.name` directly; alias → name resolution is the API/CLI
  router's job (T5/T6), not this substrate's.

## References

- Parent Initiative: G9.1 #363.
- Prerequisite schema: G9.1-T1 #448 (migration `0007`).
- This task: G9.1-T4 #451.
- PostgreSQL recursive CTE + `CYCLE`:
  <https://www.postgresql.org/docs/17/queries-with.html> §7.8.2.2
  (identical in PG 16, the chassis floor).
