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

## The kind vocabularies — open, slug-validated, with a documented well-known core

Both `graph_node.kind` and `graph_edge.kind` are **open
vocabularies** ([Initiative #2533](https://github.com/evoila/meho/issues/2533)
T1 [#2534](https://github.com/evoila/meho/issues/2534), reversing the
[#364](https://github.com/evoila/meho/issues/364) v0.2 lock). Any kind
matching the **slug grammar** is valid:

```
^[a-z0-9]+(?:[._-][a-z0-9]+)*$        (2–63 characters)
```

lowercase alphanumeric runs joined by single `.` / `_` / `-`
separators. A novel kind (`dns-record`, `database`, `certificate`,
`chassis`, `resolves-to`, `same-as`, …) enters the graph through a
normal write — no migration, no registry, no per-tenant governance
table ("dumb substrate, smart agent"). Human `tenant_admin` writes
stay zero-friction on every front; for AGENT principals the write
itself becomes approval-gated when T3
[#2537](https://github.com/evoila/meho/issues/2537) lands (until
then agent writes are immediate, like human writes), at which point
a human sees a novel agent-invented kind at the moment it is
proposed.

Enforcement layers:

- **Python (authoritative)** — the full slug pattern is validated at
  every write boundary, single-sourced from
  [`KIND_SLUG_PATTERN`](../../backend/src/meho_backplane/db/models.py)
  (`is_valid_kind_slug`): the service primitives
  (`topology/nodes.py`, `topology/annotate.py`), the REST body
  models (Pydantic `StringConstraints`), and the MCP inputSchemas
  (jsonschema `pattern`). Rejection cites the pattern and echoes the
  well-known kinds as suggestions.
- **DB (backstop)** — migration `0063` replaced the closed IN-list
  CHECKs (`ck_graph_node_kind` 14 members, `ck_graph_edge_kind` 10
  members) with a portable minimal shape CHECK
  (`length(kind) BETWEEN 2 AND 63 AND kind = lower(kind)`); regex
  CHECKs are not portable across PostgreSQL and the SQLite unit
  suite, so the DB layer guards shape, not the full grammar.

The old members survive as the **well-known set** — a documentation
convention, not a gate.
[`WELL_KNOWN_NODE_KINDS`](../../backend/src/meho_backplane/db/models.py)
carries the 14 node kinds (`target`, `vm`, `host`, `network`,
`datastore`, `namespace`, `pod`, `service`, `ingress`, `node`,
`principal`, `vault-role`, `vault-mount`, `volume`);
[`GraphEdgeKind`](../../backend/src/meho_backplane/db/models.py)
carries the ten edge kinds tabulated below. **Prefer a well-known
kind when one fits** — shared vocabulary keeps traversal answers and
cross-operator conventions legible; reach for a novel slug when no
well-known kind describes the resource class or relationship. The
UI surfaces the well-known kinds as `datalist` suggestions with
free-text input; MCP tool descriptions and error messages carry the
same list.

### The `same-as` convention — cross-system identity stitching

When two connectors each discover *the same physical thing* under
different names (the Kubernetes connector's `node` `worker-3` and a
bare-metal inventory's `host` `hetzner-ax41-7`), assert a curated
`same-as` edge between the two nodes:

```
meho topology annotate worker-3 same-as hetzner-ax41-7 \
  --note "same machine; MAC 9c:6b:00:… verified 2026-07-10" \
  --evidence-url https://…/INVENTORY.md#ax41-7
```

`same-as` is symmetric in meaning but stored as a directed edge like
every other kind; pick a consistent direction per tenant (suggested:
from the more specific / ephemeral representation to the more
durable one) and record the evidence. Traversals then reach across
the identity seam like any other edge. Machine-*suggested* matches
(auto-matching by MAC/UUID) are a later initiative; T1 makes the
assertion expressible and traversable today.

### The well-known edge kinds

**Four auto-discoverable kinds** — refresh writes these on every
probe, **for the pair-types a populator covers** (see
[Curated-until-populator-covers policy](#curated-until-populator-covers-policy)
below). Probe-derived edges have to be high-confidence — a wrong
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

### Curated-until-populator-covers policy

The auto/curated split assumes **populator coverage**: an
auto-discoverable kind is left to the probes only where a populator
actually emits it. v0.2 auto-discovery is **Kubernetes-only** — the
base connector's `discover_topology` is a no-op
([`connectors/base.py`](../../backend/src/meho_backplane/connectors/base.py))
and only the Kubernetes connector overrides it
([`connectors/kubernetes/connector.py`](../../backend/src/meho_backplane/connectors/kubernetes/connector.py))
— so for a non-k8s pair (a virtualization-management appliance
`runs-on` its host cluster, a workload `runs-on` a hypervisor) no probe
ever emits `runs-on` / `mounts` / `routes-through` / `belongs-to`.

The policy that follows: **an auto-discoverable kind MAY be curated on
any pair no populator covers.** Choosing the semantically-correct kind
is legitimate there, not a workaround — an operator should not fall
back to a weaker curated-only kind (`depends-on`) just to dodge the
vocabulary. Such a write inserts clean (`source: curated, conflicts:
[]`) **only when no other edge already sits on that ordered pair**,
because §6 detection keys off *existing* edges. The supersede pass
(rule 1) only marks `source='auto'` rows
([`annotate.py:314`](../../backend/src/meho_backplane/topology/annotate.py)),
so it is genuinely dormant on an uncovered pair — there is no auto row to
supersede. The incompatible-kind pass (rule 2), however, carries **no
`source` filter**: it selects every different-kind edge on the same
`(from, to)` pair regardless of origin
([`annotate.py:366-371`](../../backend/src/meho_backplane/topology/annotate.py)).
So the clean-insert guarantee holds only where the pair is otherwise
empty. If a *pre-existing curated* different-kind edge already sits on it
— an operator curated `depends-on(A→B)`, then later curates
`runs-on(A→B)` — rule 2 still fires and the new curated row inserts with
a **non-empty** `conflicts` array. That is by design: rule 2's own
docstring describes exactly this `depends-on` ⇄ `routes-through`
coexistence, where both rows survive and each carries the other's id.
Clean insertion therefore requires *both* no existing `auto` row **and**
no existing different-kind edge (of any source) on the pair.

**Grandfather rule on populator ship.** So that today's correct
modelling stays safe when coverage later arrives, MEHO commits that any
populator newly covering a `(kind, pair-type)` an operator already
curated ships with a one-shot reconciliation that grandfathers those
pre-existing curated edges — they stay visible, not retroactively
§6-conflicted. Coverage is per `(kind, pair-type)`, not per kind
globally: the k8s populator already covers `belongs-to` for k8s pairs
(namespace→target, node→target), so
the trigger is a populator adding a previously-uncovered *pair-type* (a
hypervisor host, an appliance→cluster) under a kind that may already be
covered elsewhere. The substrate already applies this for the
*identical* pair:
[`refresh._refresh_curated_edge`](../../backend/src/meho_backplane/topology/refresh.py)
keeps an operator-curated row operator-owned when a probe re-discovers
the same `(from, to, kind)` (it bumps `last_seen` only; the
`source='curated'` marker and any §6 markers are untouched, and no
competing auto row is inserted). The grandfather rule extends that same
"operator-owned rows survive a populator arrival" principle to the
related-pair interactions a future reconciliation must settle. The
machine-readable `curated_until_populator_covers` bucket and the
reconciliation job itself are deferred to the initiative that ships the
non-k8s populator; recording the commitment now is what makes writing
`runs-on` today safe. The operator runbook
([`topology-annotation.md`](../cross-repo/topology-annotation.md#curating-an-auto-discoverable-kind-before-its-populator-exists))
carries the full recipe.

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
- **Read-verb visibility caveat.** The G9.1-T4 traversal CTE does
  **not** filter `last_seen IS NULL` — a soft-deleted node is still
  reachable by `find_dependents` / `find_dependencies` / `find_path`.
  Point-in-time ("when did this disappear?") reads are served by the
  separate G9.3 history/diff/timeline verbs over the retained rows;
  G9.3 ([#365](https://github.com/evoila/meho/issues/365)) did not add
  `last_seen` filtering to the traversal CTE. Soft-delete is purely a
  *retention* mechanism, not an immediate visibility change for the
  traversal verbs. Operators reading blast radius in v0.2 should treat
  the graph as last-refresh-wins; a stale edge persists until the next
  successful refresh of its owning target re-derives the snapshot.

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
| `find_path` worst case (dense mesh, unreachable target, `max_hops=32`) | < 500 ms (~31k walk rows on the 16-node `MeshSpec` benchmark mesh) |
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

### `find_path` on dense meshes (#2535)

The forest fixture's out-degree-1 shape hides `find_path`'s real cost
profile: the bidirectional walk enumerates **simple paths**, which
grow ~branch_factor^hops on a dense mesh (converging paths, cycles),
not linearly with node count. Two mitigations bound it:

- **Per-branch target pruning** in `_PATH_SQL` — the destination id
  resolves in a non-recursive `target` CTE and the recursive term
  refuses to extend a branch whose frontier row already is the target.
  Behavior is identical (same shortest hop count, same `None`); on the
  20-node pruning mesh the walk drops from 1 544 to 795 materialised
  rows (regression-pinned in
  `backend/tests/integration/test_topology_path_pruning.py`). Global
  cross-branch early termination is **not** expressible — PostgreSQL
  allows exactly one recursive self-reference, outside subqueries, and
  `ORDER BY hops LIMIT 1` must consume the full walk before sorting.
- **The `max_hops` bound** (route ceiling 32) — the only guard when
  the target is unreachable, because pruning never fires. That worst
  case (dense cyclic 16-node mesh, unreachable target, `max_hops=32`)
  is CI-pinned: ~31k walk rows (exact row count asserted — it is a
  deterministic function of the graph, immune to runner load), a
  hops-32/hops-8 wall-clock ratio gate (~7.7 healthy, ceiling 20), and
  a generous 5 s absolute backstop. Measured median on a dev laptop:
  ~120 ms.

The dense shapes come from the `MeshSpec` / `seed_mesh_graph`
generator (same fixture module): layered meshes with configurable
`branch_factor` (converging paths), `cycle_stride` (back-edges),
mixed edge kinds, and optional soft-deleted rows — which the traversal
verbs still walk (soft-delete is retention, not visibility; see
§Soft-delete semantics).

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
