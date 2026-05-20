# Topology (G9)

> Reads [CLAUDE.md](../../CLAUDE.md) postulates 1 + 5. Architecture doc
> for the topology graph — the data layer every blast-radius-aware
> surface (policy engine, the topology UI, the "what depends on this?"
> agent question) sits on. Shipped by [Initiative #363 (G9.1)](https://github.com/evoila/meho/issues/363);
> G9.2 extends the edge vocabulary and G9.3 adds the history surface.

## What this surface does

MEHO keeps a per-tenant graph of the resources it governs so an
operator (or an agent, before recommending a destructive op) can ask
"what depends on this?" from data instead of from memory.

- **`graph_node` + `graph_edge` tables** — adjacency-list shape,
  tenant-scoped, created by Alembic migration
  `0007_create_topology_graph`. Adjacency-list + PostgreSQL 16's
  `WITH RECURSIVE … CYCLE` clause was chosen over Apache AGE /
  pgrouting / `ltree`: stock PG 16, no second extension, matches the
  chassis's "no new substrate" discipline. See
  [docs/codebase/topology.md](../codebase/topology.md) for the column
  list and index rationale.
- **`Connector.discover_topology(target) -> TopologyHints`** — the
  per-connector probe hook on the connector ABC
  (`backend/src/meho_backplane/connectors/base.py`). The base class
  returns an empty `TopologyHints`; vSphere / Kubernetes / Vault
  override it. `TopologyHints` / `NodeHint` / `EdgeHint` are frozen
  Pydantic v2 models in
  `backend/src/meho_backplane/connectors/schemas.py`.
- **On-demand + scheduled refresh** — `refresh_target_topology(target,
  operator)` (`topology/refresh.py`) is the single write path: it
  resolves the connector, calls `discover_topology`, diffs the snapshot
  against the existing `(tenant_id, target_id)` rows, and applies
  inserts / updates / soft-deletes in **one transaction** (a
  mid-reconcile crash never leaves the graph half-applied). The
  scheduled loop is `start_topology_refresh_scheduler()`
  (`topology/scheduler.py`), a lifespan-owned `asyncio` task that
  sweeps every tenant's targets on the
  `TOPOLOGY_REFRESH_INTERVAL_SECONDS` cadence (default 3600), per-target
  guarded by a PG advisory lock so two replicas don't stampede the same
  target, with per-target exponential backoff on failure.
- **Three read verbs via recursive CTE** — in `topology/query.py`:
  - `find_dependents(operator, name, *, kind=None, depth=16, kind_filter=None)`
    — reverse closure, "what depends on me".
  - `find_dependencies(operator, name, *, kind=None, depth=16, kind_filter=None)`
    — forward closure, "what I depend on".
  - `find_path(operator, from_name, to_name, *, from_kind=None, to_kind=None, max_hops=8)`
    — shortest unweighted path (bidirectional BFS), or `None` if
    unreachable.

  Every verb returns **one row per reachable node** (a node reached by
  several converging paths is collapsed to its minimum-depth
  occurrence; the `CYCLE` clause alone only dedupes within a single
  branch). A bare `name` that resolves to more than one `kind` in the
  tenant raises `AmbiguousNodeError` — pass `kind` to pin the
  `(tenant_id, kind, name)` unique row.
- **`query_topology` meta-tool** — the single parametric MCP agent
  tool (`backend/src/meho_backplane/mcp/tools/topology.py`). One `kind`
  argument (`dependents` / `dependencies` / `path` / `edges`) selects
  the read shape; G9.2 added the `edges` facet for the flat
  inventory survey alongside the three traversal shapes. Per
  CLAUDE.md postulate 5 the four verbs are **not** four tools — that
  would be the per-op-tool anti-pattern. `list_targets` is the
  sibling meta-tool that enumerates the operator's targets.
  `topology.refresh` and `targets discover` are operator CLI verbs,
  not agent tools (`meho topology refresh|dependents|dependencies|path`,
  `meho targets discover` under `cli/internal/cmd/`).
- **Curated-edge MCP tools** (`meho.topology.annotate` /
  `meho.topology.unannotate`, both `tenant_admin` only) — admin
  meta-tools in the `meho.*` namespace exposing the write half of
  the G9.2 surface. Not on the daily ~17 meta-tool agent surface;
  an `operator`-role session never sees them in `tools/list`.

CLI, REST (`/api/v1/topology*`, `/api/v1/targets/discover`), and the
MCP meta-tools are **sibling fronts on one backplane** — each calls the
`topology/` substrate directly; none is a thin wrapper of another.

## The v0.2 edge-kind vocabulary

G9.2 ([#364](https://github.com/evoila/meho/issues/364)) locks the
edge-kind vocabulary at **ten** members: the four auto-discoverable
kinds G9.1 ships, plus six operator-curated cross-system kinds that
no probe can derive. The vocabulary is closed; widening it is a
coordinated DB + model + decision-row change (migration `0010`
widens the `graph_edge.kind` CHECK from the G9.1 subset; the
[`GraphEdgeKind`](../../backend/src/meho_backplane/db/models.py)
`StrEnum` and the CHECK move in lock-step).

**Four auto-discoverable kinds** — refresh writes these on every
probe. Probe-derived edges have to be high-confidence — a wrong
edge in a `dependents` answer misleads the operator on the very op
the verb is supposed to make safer.

| Edge kind | Meaning | Example |
|---|---|---|
| `runs-on` | execution placement | VM `runs-on` ESXi host; pod `runs-on` node |
| `mounts` | storage attachment | VM `mounts` datastore; pod `mounts` PV |
| `routes-through` | network path | VM `routes-through` portgroup; service `routes-through` to pod |
| `belongs-to` | containment / ownership | cluster `belongs-to` member host; namespace `belongs-to` pod |

**Six curated-only kinds** — operator-asserted via
`meho topology annotate` (CLI), `POST /api/v1/topology/edges` (REST),
or `meho.topology.annotate` (MCP, `tenant_admin` only). These cross
connector boundaries (a Kubernetes ServiceAccount authenticating
against a Vault role, a service depending on a database in a
different product) and cannot be derived from any single probe.

| Edge kind | Meaning | Example |
|---|---|---|
| `authenticates-via` | principal → identity-provider | k8s SA → Vault role (`k8s-sa-foo` `authenticates-via` `vault-role-bar`) |
| `depends-on` | cross-system functional dependency | service → database in another product |
| `replicates-to` | operator-asserted replication | storage / DB node → replica node |
| `backed-up-by` | operator-asserted backup relationship | resource → backup target |
| `routes-via` | operator-asserted network path through an intermediary | `vm-A` `routes-via` `firewall-X` to `vm-B` |
| `policy-binds` | RBAC / policy attachment across connector boundaries | k8s namespace → Vault policy |

The operator-facing recipe for *when* to annotate, the §6 conflict
rules below, and the CLI walkthrough live in
[`docs/cross-repo/topology-annotation.md`](../cross-repo/topology-annotation.md).

## G9.2 curated-edge surface

G9.2 lands three operator-facing fronts over a single substrate.
Each is a sibling of the others; none is a thin wrapper.

- **CLI** — `meho topology annotate <from> <kind> <to>`,
  `meho topology unannotate <id | from kind to>`, and
  `meho topology list-edges [--kind ...] [--source ...] [--conflicts]`.
  Writes require `tenant_admin`; list-edges requires `operator`.
- **REST** — `POST /api/v1/topology/edges`,
  `DELETE /api/v1/topology/edges/{edge_id}`, and
  `GET /api/v1/topology/edges`. The two writes are pinned to
  `tenant_admin`; the GET to `operator`. The list endpoint accepts
  `kind`, `source`, `from`, `to`, `conflicts`, `limit`, `offset`
  query params; `limit` defaults to 200 with a hard ceiling of
  1000 mirroring the substrate cap.
- **MCP** — `meho.topology.annotate` and
  `meho.topology.unannotate` live in the `meho.*` admin namespace
  (tenant_admin only, not on the daily ~17 meta-tool surface). The
  read facet is `query_topology { kind: "edges", ... }` on the
  existing operator-role parametric tool — same primitive, the
  fourth `kind` value alongside `dependents` / `dependencies` /
  `path` (Initiative #364 §9 narrow-waist alignment).

All three fronts call the
[`topology/annotate.py`](../../backend/src/meho_backplane/topology/annotate.py)
substrate (`annotate_edge` / `unannotate_edge`) and the
`list_edges` helper in
[`topology/query.py`](../../backend/src/meho_backplane/topology/query.py)
directly. Tenant scope is lifted from `operator.tenant_id` (the
validated JWT subject) on every front — no front accepts a
`tenant_id` argument.

## §6 conflict-resolution rules

Two recoverable conflict shapes the substrate handles
deterministically — the recoverable-mistake invariant on which
G9.2's annotation surface is built.

### Rule 1 — same kind, different endpoint → curated supersedes

A curated edge `(A, kind, B)` displaces any auto edge from the same
`from_node_id` of the same `kind` to a *different* `to_node_id`. The
displaced auto edges are marked
`properties.superseded_by = <curated-id>`; the traversal verbs in
[`topology/query.py`](../../backend/src/meho_backplane/topology/query.py)
guard with `properties->>'superseded_by' IS NULL` so superseded
rows do not contribute to blast-radius answers.

The supersede mark is **sticky** across refresh: the
[`refresh._reconcile_edges`](../../backend/src/meho_backplane/topology/refresh.py)
pass preserves it even when the probe re-discovers the auto edge.
Only an `unannotate_edge` of the curated row clears the mark — at
which point the auto edge un-supersedes on the next refresh.

### Rule 2 — incompatible kinds, same endpoint pair → coexist with `conflicts_with`

When a curated edge `(A, kind1, B)` is annotated and an existing
edge (auto or curated) over the same `(from_node_id, to_node_id)`
has a *different* `kind`, both rows are kept. Each row's
`properties.conflicts_with` array is appended with the other's id,
bidirectionally. Traversal verbs include both rows (the
`superseded_by` guard does not filter `conflicts_with`); the
downstream policy layer is the consumer that resolves the
contradiction in v0.2.next.

### Surfacing conflicts

`meho topology list-edges --conflicts` (CLI) /
`GET /api/v1/topology/edges?conflicts=true` (REST) /
`query_topology { kind: edges, conflicts: true }` (MCP) returns
every edge whose `properties.conflicts_with` array is non-empty —
the §6 recoverability survey. Pair with `--source curated` to
narrow to operator annotations the probe disagrees with; pair with
`--source auto` to narrow to probe-discovered edges the operator
has overridden.

### Recovery

A wrong annotation is one CLI call away from clean:

```bash
meho topology unannotate <from> <kind> <to>
```

Removing the curated row clears its supersede + conflict markers
on neighbours; superseded auto edges un-supersede on the next
refresh. The mistake is local, recoverable, and audited.

## Soft-delete semantics

A refresh that no longer sees a previously-discovered node or edge
**soft-deletes** it: `last_seen` is set to `NULL` and the row is kept.

- The row itself is **retained** — soft-delete never issues a SQL
  `DELETE`. G9.3 ([#365](https://github.com/evoila/meho/issues/365))
  ships the history surface (`graph_node_history` /
  `graph_edge_history`, `meho topology history|diff|timeline`) that
  queries these retained rows to answer "when did this disappear?".
- A subsequent refresh that re-discovers the node clears `last_seen`
  back to a timestamp (the row is revived in place, not re-inserted —
  the `(tenant_id, kind, name)` natural key is stable, so the tenant's
  node count does not grow on revival).
- **G9.1 read-verb visibility caveat.** The G9.1-T4 traversal CTE does
  **not** yet filter `last_seen IS NULL` — a soft-deleted node is still
  reachable by `find_dependents` / `find_dependencies` / `find_path`
  until G9.3 layers a history-aware (point-in-time) read on top. In
  G9.1, soft-delete is purely a *retention* mechanism for the future
  history surface, not an immediate visibility change. Operators
  reading blast radius in v0.2 should treat the graph as
  last-refresh-wins; a stale edge persists until the next successful
  refresh of its owning target re-derives the snapshot.

## Performance expectations

Documented on the test fixture, **not enforced as an SLO** (Initiative
#363 performance-discipline item 13). The recursive-CTE traversal is
capped at depth 16 by default (configurable per route/param up to 64).
Against a seeded ~10k-node / ~10k-edge tenant graph
(`backend/tests/fixtures/topology_10k_nodes.py`, the parametric
`GraphSpec` / `seed_perf_graph` generator):

| Operation | Documented expectation on the fixture |
|---|---|
| `find_dependents` depth 16 | < 100 ms |
| `find_path` BFS | < 150 ms |
| `refresh_target_topology` (insert/update bottleneck) | < 500 ms |

The `graph_edge_tenant_from_idx` / `graph_edge_tenant_to_idx` indexes
migration `0007` ships are what keep the recursive join sub-linear per
level. The closing acceptance suite
`backend/tests/integration/test_topology_g91_acceptance.py` proves
these against a real `pgvector/pgvector:pg16` container in the split-CI
`python-integration` job; its assertions carry a generous 10x ceiling
so ordinary runner variance doesn't flake the gate while an
order-of-magnitude regression still fails it. The >10k-node case is a
v0.3 concern, addressed only when a real tenant hits it.

## Tenant boundary

Every node, edge, query, and refresh is scoped to
`operator.tenant_id`, lifted from the validated JWT — never from a
request argument. `query_topology` has no `tenant_id` argument at all,
so a cross-tenant probe is structurally impossible. Two tenants can own
same-named targets and nodes (e.g. both have a `rdc-vcenter`); each
tenant's queries resolve only their own rows, and a refresh in one
tenant never touches another's. Cross-tenant refresh is impossible
because a refresh resolves its target by `(tenant_id, name)` — a
tenant cannot name another tenant's target to begin with. This is
proven end-to-end in scenario 1 of the G9.1 acceptance suite.
