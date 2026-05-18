# topology — graph refresh service + recursive-CTE traversal substrate

## Overview

`backend/src/meho_backplane/topology/` is the G9.1 topology graph
package (Initiative #363). It has a **write half** (refresh service +
scheduled background task, G9.1-T3 #450) and a **read half** (the
recursive-CTE query verbs every blast-radius check dispatches through,
G9.1-T4 #451). Both ride the `graph_node` + `graph_edge` tables and ORM
models that G9.1-T1 (#448, migration `0007`) created.

**Write half — entry points:**

- `refresh_target_topology(target, operator) -> RefreshResult` — one
  on-demand refresh of a single target.
- `start_topology_refresh_scheduler() -> asyncio.Task` — the lifespan
  background loop that sweeps every tenant's targets on a cadence.

**Read half — entry points (async, read-only):**

- `find_dependents(operator, name_or_alias, *, kind=None, depth=16, kind_filter=None)`
  — reverse traversal, "what depends on me".
- `find_dependencies(operator, name_or_alias, *, kind=None, depth=16, kind_filter=None)`
  — forward traversal, "what I depend on".
- `find_path(operator, from_name, to_name, *, from_kind=None, to_kind=None, max_hops=8)`
  — shortest unweighted path, or `None` if unreachable.

**Annotation — entry points (async, write half, G9.2-T3 #595):**

- `annotate_edge(session, operator, from_ref, kind, to_ref, *, note=None, evidence_url=None) -> GraphEdge`
  — create or refresh a curated edge. Resolves both endpoints via
  `resolve_node`, validates `kind` against `GraphEdgeKind`,
  idempotent on `(tenant_id, from_node_id, to_node_id, kind)`. Runs
  §6 conflict detection (sticky `superseded_by` for
  same-kind/different-endpoint auto edges; bidirectional
  `conflicts_with` for incompatible kinds over the same endpoint
  pair). Writes one audit row (`op_id="topology.annotate"`,
  `op_class="write"`) and publishes one broadcast event.
- `unannotate_edge(session, operator, *, edge_id=None, from_ref=None, kind=None, to_ref=None) -> UUID`
  — hard-delete a curated edge (selector is either `edge_id` or the
  full triple; both/neither → `UnannotateSelectorError`). Refuses
  `source='auto'` rows with `AutoEdgeDeletionError` (auto edges
  resurrect on next refresh). Clears reciprocal `superseded_by` /
  `conflicts_with` markers the deleted edge left on auto rows.
  Writes one audit row + one broadcast event.

Both service functions own a `session.begin()` block internally and
publish broadcast events after commit (fail-open per the refresh
pattern). The REST routes (T5), CLI verbs (T6), and MCP tools (T7)
funnel through these primitives.

**Resolver — entry point (async, read-only, G9.2-T2 #594):**

- `resolve_node(session, tenant_id, name, kind=None) -> GraphNode` —
  name → row resolver the G9.2 annotation flow (T3 / T4 in
  Initiative #364) calls before writing or reading an edge endpoint.
  Returns the unique `GraphNode` row, or raises
  `AmbiguousNodeError` (bare name maps to multiple kinds in the
  tenant) / `NodeNotFoundError` (no match — including names that
  exist only in another tenant). Works for non-target nodes
  (`target_id IS NULL`) as well as registered targets. The
  ambiguity-probe SQL is shared with the traversal verbs'
  `_assert_anchor_unambiguous`, so the "name → multiple kinds"
  surface is single-sourced; traversal's not-found behavior is
  unchanged (empty result, not a raise) — only `resolve_node`
  surfaces `NodeNotFoundError`.

**Edge listing — entry point (async, read-only, G9.2-T4 #596):**

- `list_edges(session, tenant_id, *, kind=None, source=None,
  from_ref=None, to_ref=None, conflicts_only=False, limit=200,
  offset=0) -> list[TopologyEdge]` — flat tenant-scoped
  filter-composable edge listing. Joins both endpoint nodes so
  `from` / `to` carry `(id, kind, name)` without a second round
  trip. Filters compose: `kind=` restricts to one
  `GraphEdgeKind`, `source=` selects `'auto'` vs `'curated'`,
  `from_ref` / `to_ref` resolve via `resolve_node` (a ref that maps
  to no node yields an empty list, not an error; an ambiguous bare
  name raises `AmbiguousNodeError`), `conflicts_only=True` returns
  exactly the edges whose `properties.conflicts_with` JSONB is a
  non-empty array (the marker G9.2-T3 #595 writes on incompatible-
  kind conflicts — until #595 lands, the predicate is still safe
  via the `jsonb_typeof = 'array'` guard). Soft-deleted rows
  (`last_seen IS NULL`) are excluded by default. Pagination is
  stable: `ORDER BY last_seen DESC NULLS LAST, id` is a strict
  total order, so a two-page sweep reassembles to the unpaged set
  with no gaps or duplicates. Unlike the traversal verbs (which
  take an `Operator` and open their own session), `list_edges`
  takes `session` and `tenant_id` directly — same shape as
  `resolve_node`, so callers can compose the listing inside a
  larger transactional boundary (e.g. the MCP layer batching
  reads, the annotation flow asserting an edge exists).

Every read verb returns **one row per reachable node** (a node reachable
by several converging paths is collapsed to its minimum-depth occurrence
— `CYCLE` alone only dedupes within a single branch). An anchor
requested by bare `name` that resolves to more than one `kind` in the
tenant raises `AmbiguousNodeError`; pass the optional `kind` (or
`from_kind` / `to_kind` for `find_path`) to pin the `(tenant_id, kind,
name)` unique row. The read package never inserts, updates, or deletes.

surface"). The CLI front (T6, #454) is landed and documented below
("CLI front"). The MCP front (T7, #455) is landed too — the two
narrow-waist meta-tools `query_topology` (parametric: `kind` selects
`dependents` / `dependencies` / `path` / `edges`) and `list_targets`
register in `mcp/tools/topology.py` and call `query.py` /
`select(TargetORM)` directly (sibling fronts on one backplane, not
REST wrappers). G9.2-T7 (#598) widened the parametric tool with the
`edges` facet (dispatches to `list_edges` — replaces a standalone
`list_edges` meta-tool) and added the admin-namespace pair
`meho.topology.annotate` / `meho.topology.unannotate`
(`required_role=TENANT_ADMIN`, `op_class="write"`); both admin tools
call `annotate_edge` / `unannotate_edge` directly and are visible only
to a tenant_admin-scoped session. G9.1-T8 (#456) shipped the closing
acceptance suite
(`backend/tests/integration/test_topology_g91_acceptance.py` + the
parametric `backend/tests/fixtures/topology_10k_nodes.py` 10k-node
generator) and the operator-facing docs
[`docs/architecture/topology.md`](../architecture/topology.md) +
[`docs/cross-repo/topology-onboarding.md`](../cross-repo/topology-onboarding.md);
the whole G9.1 surface is now landed.

## Key types

### `RefreshResult` — frozen Pydantic v2 (write half)

Returned by `refresh_target_topology`. Per-object-class disjoint counts:
a node is in exactly one of `added_nodes` / `updated_nodes` /
`removed_nodes`, or in none of them when it is unchanged — there is no
`unchanged` count, an unchanged node simply increments nothing; same
for edges. `duration_ms` covers the whole resolve + discover +
reconcile + commit cycle. `target_id` echoes the refreshed target. The
CLI `refresh` verb renders exactly these as `nodes: +A -R ~U` /
`edges: +A -R ~U` (no fourth column) and surfaces `duration_ms`.

### `TopologyNode` — frozen Pydantic v2 (read half)

| Field | Type | Meaning |
|---|---|---|
| `id` | `UUID` | `graph_node.id`. |
| `kind` | `str` | `graph_node.kind` (closed enum from migration 0007). |
| `name` | `str` | `graph_node.name`, unique within `(tenant_id, kind)`. |
| `properties` | `dict` | `graph_node.properties` JSONB; wrapped in `MappingProxyType` after validation so the frozen model is deeply immutable, serialised back to a plain `dict`. |
| `depth` | `int` | Distance from the query root: root = 0, immediate = 1, transitive = 2, … |
| `via_edge_kind` | `str \| None` | The `graph_edge.kind` of the edge used to reach this node; `None` for the root. |

### `TopologyPath` — frozen Pydantic v2 (read half)

| Field | Type | Meaning |
|---|---|---|
| `nodes` | `tuple[TopologyNode, ...]` | Ordered from the `from` node (`depth == 0`) to the `to` node (`depth == total_hops`). |
| `total_hops` | `int` | Number of edges traversed; equals `len(nodes) - 1`. |

### `TopologyEdgeEndpoint` — frozen Pydantic v2 (edge listing, G9.2-T4)

| Field | Type | Meaning |
|---|---|---|
| `id` | `UUID` | `graph_node.id`. |
| `kind` | `str` | `graph_node.kind`. |
| `name` | `str` | `graph_node.name`. |

Compact node identity carried as `from_endpoint` / `to_endpoint` on
`TopologyEdge`. The full node `properties` bag is intentionally
**not** included — an edge listing is a survey of relationships,
not a node dump; callers that need the bag look the node up
separately via `resolve_node`.

### `TopologyEdge` — frozen Pydantic v2 (edge listing, G9.2-T4)

| Field | Type | Meaning |
|---|---|---|
| `id` | `UUID` | `graph_edge.id`. |
| `from_endpoint` | `TopologyEdgeEndpoint` | The edge's source node identity. The route layer (T5) applies the `from` / `to` alias on `model_dump(by_alias=True)` for the wire shape — the substrate model itself keeps plain attribute names so mypy/static checkers don't lose the kwarg signature. |
| `to_endpoint` | `TopologyEdgeEndpoint` | The edge's destination node identity. |
| `kind` | `str` | One of the ten `GraphEdgeKind` values (closed enum since G9.2-T1 #593). |
| `source` | `str` | `'auto'` (probe-derived) or `'curated'` (operator-asserted). |
| `properties` | `dict` | `graph_edge.properties` JSONB; deep-frozen (same discipline as `TopologyNode.properties`). Carries the conflict markers `conflicts_with` (array, G9.2-T3 #595) and `superseded_by` (UUID, also #595). |
| `last_seen` | `datetime \| None` | The refresh service's "I observed this edge at" timestamp. NULL after a soft-delete; soft-deleted edges are excluded from `list_edges` by default. Also the stable total-order key the helper paginates against. |

## Control flow — write half

### `refresh_target_topology`

1. `resolve_connector(target)` → `get_or_create_connector_instance(cls)`
   (the same cached-singleton path the G0.6 dispatcher uses).
2. `await connector.discover_topology(target)` → a `TopologyHints`
   snapshot (nodes + edges, each with `properties`).
3. Open one transactional session (`sessionmaker() ... session.begin()`).
4. `_reconcile_nodes` — diff the snapshot nodes against existing
   `graph_node` rows for `(tenant_id, target_id)`:
   - INSERT nodes in the snapshot but not the DB.
   - For nodes in both: refresh `last_seen`, and `properties` when they
     changed (a no-change refresh only touches `last_seen`, so the
     `unchanged` path reports zero `updated`).
   - Soft-delete (set `last_seen = NULL`) nodes in the DB but absent
     from the snapshot. A node already soft-deleted is not re-counted.
   Returns two key→id maps: `live` (snapshot-present nodes only) and
   `all` (every node in the target scope, including soft-deleted).
5. `_reconcile_edges` — same diff for edges, keyed by
   `(from_kind, from_name, to_kind, to_name, kind)`. Existing edges are
   loaded by `from_node_id` over the **`all`** node-id set so an edge
   whose endpoint was just dropped is still found and soft-deleted
   rather than orphaned. Discovered edge endpoints resolve through the
   **`live`** map; an edge whose endpoint left the snapshot falls
   through to the soft-delete pass. A discovered edge with no matching
   live node (a malformed connector emitting an edge without its node)
   is logged and skipped — never inserted as a dangling FK.
6. `_write_audit_and_broadcast` adds **one `audit_log` row to the same
   session** — `method="REFRESH"`, `path="topology.refresh"`,
   `payload={op_id, op_class:"read", target_id, <six counts>}`. Because
   the audit row is in the reconcile transaction, the spec's "no
   success without a committed audit row" invariant holds: an audit
   failure rolls the whole refresh back.
7. After commit, publish one `BroadcastEvent` (op_class `read`,
   aggregate counts only — no node/edge names, so the read-class PII
   default holds without a redactor pass). Broadcast is **fail-open**:
   a publish exception is logged, never raised.

A failure anywhere in steps 1–6 raises out of the `session.begin()`
block, rolling everything back: no half-applied graph, no audit row.

### Scheduler

`_scheduler_loop` is a forever loop registered as an `asyncio.Task` in
`main.lifespan` (after connector auto-discovery; cancelled + awaited on
shutdown before the DB/redis pools dispose). Each iteration:

- `_run_one_sweep` — enumerate tenants, then each tenant's targets, and
  call `_refresh_one_target` per target.
- `_refresh_one_target` — skip if inside the target's backoff window;
  open a lock session, `pg_try_advisory_lock(hash(tenant, target))`
  (non-blocking; on non-PostgreSQL the lock is a no-op since the test
  process is single-replica), run the refresh, `pg_advisory_unlock` in
  a `finally`. Success clears backoff; failure increments it
  (`2^n × interval`, capped at 4 h) and is swallowed so one bad target
  never stalls the sweep.
- Sleep `TOPOLOGY_REFRESH_INTERVAL_SECONDS` (default 3600), repeat. A
  sweep-level exception is logged and the loop continues — only
  `CancelledError` stops it.

The advisory-lock key is a blake2b digest of the two UUIDs masked to 63
bits (non-negative, fits asyncpg's signed `bigint` binding).

### Why `asyncio.create_task`, not APScheduler 4.x

Task #450 prefers APScheduler 4.x but explicitly allows reusing an
in-lifespan `asyncio` loop. As of 2026-05 APScheduler 4.x has only ever
shipped `4.0.0aN` alphas, documented by the maintainer as "should NOT be
used in production". The chassis ships to production and follows a "no
new substrate / minimal dependencies" discipline, so the stdlib loop —
the issue's own stated fallback — is the right call. Revisit if 4.x
stabilises and a richer scheduling need (cron, persisted jobs) appears.

## Control flow — read half

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

As of G9.2-T2 (#594), `AmbiguousNodeError` is defined in
`meho_backplane.topology.resolvers` (alongside `resolve_node` and the
new `NodeNotFoundError`) and re-exported by
`meho_backplane.topology.query` for back-compat with pre-G9.2
importers. The kind-collection SQL is single-sourced in the resolver
module; the traversal's `_assert_anchor_unambiguous` and
`resolve_node` call into the same helper, so the "name → multiple
kinds in this tenant" surface stays consistent across the two
call paths. The not-found behavior intentionally differs between the
two surfaces: `resolve_node` raises `NodeNotFoundError`; the
traversal verbs keep G9.1's silent-on-miss contract (empty result,
not an exception) — opting traversal into the stricter raise-on-miss
contract is explicitly out of scope for G9.2-T2.

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

### Superseded-edge exclusion (G9.2-T3 #595)

Every traversal CTE filters
`graph_edge.properties->>'superseded_by' IS NULL` in its recursive
term — an auto edge an operator's curated annotation has marked
superseded drops out of every closure. The guard fires on **four**
edge-pulling sites: the recursive term of both
`_TRAVERSAL_SQL_REVERSE` and `_TRAVERSAL_SQL_FORWARD`, and **both
legs** of the `bi_edge` CTE in `_PATH_SQL` (the forward leg
`from→to` and the reversed leg `to→from`; missing the reversed leg
would let a superseded edge be walked backwards into a shortest path).

The supersede mark is sticky across refresh: `_reconcile_edges` merges
incoming hint `properties` against the reserved keys (`superseded_by`,
`conflicts_with`) so a re-observed auto edge keeps the marker that
`annotate_edge` stamped. The merge is **one-sided**: those reserved
keys are also *stripped from the incoming hint* via
`_strip_reserved_markers` so a buggy or hostile connector emitting
`superseded_by` in its `EdgeHint.properties` cannot smuggle the marker
onto an auto edge from the probe path. The same sanitizer runs on the
insert path (fresh auto edge), so the §6 annotate-only invariant holds
fail-closed regardless of how unrusted the upstream hint is. And for
edges with `source='curated'`, `_reconcile_edges` skips the property
merge entirely — only `last_seen` bumps; the operator-supplied `note`
/ `evidence_url` / `annotated_*` fields are never touched by a refresh.
Only `unannotate_edge` of the curated row clears the marker. The `->>`
operator is PG-only (manual §9.16); the recursive CTE is itself PG-only,
so the guard runs only where the column is JSONB.

## Annotation control flow (write half — G9.2-T3 #595)

### `annotate_edge`

1. Validate `kind` against `GraphEdgeKind` (raise
   `InvalidEdgeKindError` *before* any DB read).
2. `async with session.begin()` — one transaction wraps the whole
   resolve + write + conflict-scan + audit-row.
3. `resolve_node` for both endpoints (`operator.tenant_id` is the
   scope; cross-tenant references resolve to `NodeNotFoundError`).
4. Look up the existing edge for the
   `(tenant_id, from_node_id, to_node_id, kind)` unique tuple. Found
   → merge `properties` and refresh `last_seen` (idempotent). If the
   existing row's `source` is `'auto'` (operator is claiming an
   auto-discovered edge), promote it: set `source='curated'` and
   `discovered_by=operator.sub` so triple-form `unannotate_edge` and
   the next §6 scan treat the row as operator-owned. Absent
   → `INSERT` a fresh row with `source='curated'`,
   `discovered_by=operator.sub`,
   `properties={note, evidence_url, annotated_by, annotated_at}`.
5. **§6 conflict detection.** Same-kind / different-endpoint: scan
   auto edges sharing `from_node_id` + `kind` + `source='auto'` and
   a *different* `to_node_id`; stamp their `properties.superseded_by
   = <curated-id>`. Incompatible-kind: scan edges on the same
   `(from_node_id, to_node_id)` of any *other* kind; append the
   curated id to each row's `properties.conflicts_with` (deduped)
   and reciprocally append each conflicting edge's id to the
   curated row.
6. Add one `audit_log` row in the same session
   (`method="ANNOTATE"`, `path="topology.annotate"`,
   `payload={op_id, op_class:"write", edge_id, from, to, kind, note,
   evidence_url, superseded[], conflicts[]}`, `target_id` =
   from-node's `target_id` when non-null).
7. Commit. Then publish one `BroadcastEvent` (`op_class="write"` —
   set explicitly because the `.annotate` suffix is not in
   `_WRITE_SUFFIXES`; §10 of #364 locks the *write* classification
   so annotations broadcast in full per the G6.1 default classifier).
   Publish is fail-open: a publish exception is logged, never raised.

### `unannotate_edge`

1. Validate the selector — exactly one of `edge_id` or the full
   `(from_ref, kind, to_ref)` triple. Both / neither / partial triple
   → `UnannotateSelectorError`.
2. `async with session.begin()`.
3. Resolve the target row. `edge_id` path: `session.get` + tenant
   check (a row in another tenant looks "not found" — the tenant
   boundary holds). Triple path: `resolve_node` for each endpoint
   + the unique-tuple lookup.
4. Refuse if `edge.source != "curated"` — auto edges resurrect on
   next refresh, so manual deletion is meaningless;
   `AutoEdgeDeletionError`. The API layer maps this to HTTP 409.
5. Scan every edge in the tenant whose `properties.superseded_by` or
   `properties.conflicts_with` references the removed edge id;
   clear the back-references so a superseded auto edge reappears in
   traversal and dangling ids do not linger.
6. `session.delete(edge)` (hard delete — curated history lives on
   via G9.3).
7. Add one `audit_log` row in the same session
   (`method="UNANNOTATE"`, `path="topology.unannotate"`,
   `op_class="write"`). Commit. Publish one broadcast event
   (fail-open).

## REST API surface (T5, #453 + G9.2-T5 #597)

`backend/src/meho_backplane/api/v1/topology.py` is the HTTP front for
the read + write halves. Eight routes total — seven on the topology
router, one on the targets router:

| Method + path | Wraps | op_id | RBAC |
|---|---|---|---|
| `GET /api/v1/topology/dependents/{name}` | `find_dependents` | `topology.dependents` | operator |
| `GET /api/v1/topology/dependencies/{name}` | `find_dependencies` | `topology.dependencies` | operator |
| `GET /api/v1/topology/path?from=A&to=B` | `find_path` | `topology.path` | operator |
| `POST /api/v1/topology/refresh/{target_name}` | `refresh_target_topology` | `topology.refresh` | operator |
| `POST /api/v1/topology/edges` | `annotate_edge` (T3 #595) | `topology.annotate` | **tenant_admin** |
| `DELETE /api/v1/topology/edges/{edge_id}` | `unannotate_edge` (T3 #595) | `topology.unannotate` | **tenant_admin** |
| `GET /api/v1/topology/edges` | `list_edges` (T4 #596) | `topology.list_edges` | operator |
| `GET /api/v1/targets/discover?product=X` | `Connector.list_candidates` | `targets.discover` | operator |

Load-bearing details:

- **Route ordering.** `GET /api/v1/targets/discover` is declared on
  the targets router *before* `GET /api/v1/targets/{name}`. FastAPI
  resolves routes in declaration order, so the literal `/discover`
  segment must come first or it is captured as a target name.
- **`path` query params.** The route binds `?from=` / `?to=` via
  Pydantic `alias` because `from` is a Python keyword. An unreachable
  pair returns HTTP 200 with a `null` body — unreachability is a valid
  answer, not an error.
- **Ambiguous anchor.** `AmbiguousNodeError` (a `ValueError` from the
  query layer) is mapped to HTTP 409 `ambiguous_node` with the
  candidate kinds echoed in `detail` so the caller can re-issue with
  an explicit `kind`. Used by the four read verbs and by `POST /edges`
  / `GET /edges` when an endpoint name is ambiguous.
- **Depth/hop ceilings.** The route caps `depth` at 64 and `max_hops`
  at 32 at the HTTP boundary (over the service defaults of 16 / 8) so
  a hostile query param cannot ask the recursive CTE to walk an
  unbounded closure (#363 performance discipline).
- **`refresh` audit.** The route binds `audit_op_id="topology.refresh"`
  / `audit_op_class="read"` for the chassis HTTP-level audit row; the
  refresh *service* additionally writes its own domain-level audit row
  + one broadcast event with the per-target counts. The two rows are
  intentional — same shape as the operations dispatcher writing a
  domain row alongside the middleware's HTTP row.
- **Curated-edge writes are `tenant_admin`.** `POST /edges` and
  `DELETE /edges/{id}` sit behind `require_role(TenantRole.TENANT_ADMIN)`
  — Initiative #364 §5: annotation is a policy-layer assertion, so an
  operator-level member must not be able to add a `depends-on` edge
  that shrinks the auto-flagged blast radius of an op they then run.
  Same gate the canonical `POST /api/v1/targets` precedent (G0.3)
  uses. `GET /edges` stays at `operator` (a tenant inventory view).
- **§3 auto-edge deletion → 409.** `AutoEdgeDeletionError` (raised when
  `DELETE /edges/{id}` resolves a `source='auto'` row) maps to HTTP 409
  `auto_edge_deletion` with the edge id and a message naming the
  auto-vs-curated rule. Auto edges resurrect on the next refresh, so a
  hard delete is meaningless — the CLI / MCP fronts can prompt the
  operator to annotate-over-auto instead. Missing or cross-tenant ids
  collapse to 404 `edge_not_found` (the tenant boundary is opaque to
  the caller; leaking "exists in another tenant" would violate it).
- **Curated-edge write audit class.** `POST` / `DELETE` bind
  `audit_op_class="write"` explicitly — `.annotate` / `.unannotate`
  are not in the broadcast classifier's `_WRITE_SUFFIXES` so the
  default classifier would fall through to `op_class="other"` and the
  broadcast event would under-emit per §10. `GET /edges` binds
  `op_class="read"`.
- **`POST /edges` body shape.** The request body is
  `{"from": {"name": ..., "kind"?}, "kind": <GraphEdgeKind>,
    "to": {"name": ..., "kind"?}, "note"?, "evidence_url"?}` —
  `from` / `to` are nested `_EdgeEndpoint` objects (mirrors the
  service-layer `NodeRef` dataclass on the wire). `kind` is typed
  against `GraphEdgeKind` so Pydantic rejects unknown kinds at the
  boundary with 422 before the service runs. `extra="forbid"` rejects
  typo'd keys at the boundary too.
- **`GET /edges` query params** are forwarded straight through to
  `list_edges` (`kind?`, `source?` constrained to `auto|curated` by a
  regex pattern, `from?` / `to?` aliased because `from` is a Python
  keyword, `conflicts?` bool, `limit?` capped at 1000, `offset?`). A
  bare `from` / `to` that resolves to multiple kinds surfaces as 409
  `ambiguous_node` — the caller re-issues with an explicit kind. A
  name that resolves to no node yields an empty list, not a 404 —
  consistent with the helper's "missing anchor → empty result" shape.
- **`TopologyEdge` wire alias.** The Pydantic model attributes are
  `from_endpoint` / `to_endpoint` (Python keywords forbid the bare
  forms); `Field(serialization_alias="from"|"to")` restores the wire
  shape Initiative #364 §8 specifies. FastAPI's default
  `response_model_by_alias=True` honours the alias so every response
  body lands as `{from, to, ...}` without manual `model_dump` calls.
- **`targets/discover`.** Iterates every connector implementation
  registered for the product (the v2 registry, which subsumes v1
  registrations; deduped by connector class), calls `list_candidates`
  on each, and merges the results. One connector raising is recorded
  in `skipped` with the exception summary and does not abort the
  sweep. `seed_target` (optional) is resolved tenant-scoped before
  being forwarded. The verb never creates `targets` rows
  (auto-registration is v0.2.next per #363).
- **Tenant scoping.** No route accepts a `tenant_id` from the path,
  query string, or body. The query verbs filter on
  `operator.tenant_id`; `refresh` / `discover` resolve targets
  tenant-scoped via `resolve_target`; `annotate_edge` /
  `unannotate_edge` / `list_edges` resolve endpoints + filter on
  `operator.tenant_id`. Cross-tenant traversal, refresh, annotation,
  and listing are all impossible by construction.

Tests: `backend/tests/test_api_v1_topology.py` +
`backend/tests/test_api_v1_targets_discover.py` (service layer patched,
SQLite); `backend/tests/integration/test_topology_api.py` (read + refresh
end-to-end + the cross-tenant boundary against a real
`pgvector/pgvector:pg16` container). Curated-edge end-to-end coverage
is the T9 integration sweep (#601).

## CLI front (T6, #454)

`cli/internal/cmd/topology/` is the operator front for the four
topology routes; `cli/internal/cmd/targets/discover.go` (sibling of
`targets/list.go`) is the front for `GET /api/v1/targets/discover`.
The split mirrors where the backend registers each route — discover
sits under the `/api/v1/targets` prefix, so its verb sits under the
`meho targets` parent rather than `meho topology`.

| Verb | Route | Default render |
|---|---|---|
| `meho topology refresh <target>` | `POST /topology/refresh/{t}` | `nodes: +A -R ~U` / `edges: +A -R ~U` summary |
| `meho topology dependents <name>` | `GET /topology/dependents/{n}` | `DEPTH / KIND / NAME / VIA` table |
| `meho topology dependencies <name>` | `GET /topology/dependencies/{n}` | same table, mirror direction |
| `meho topology path <from> <to>` | `GET /topology/path?from=&to=` | `kind/name -> … (N hops)` chain |
| `meho targets discover <product>` | `GET /targets/discover?product=` | candidates + skipped tables |

Load-bearing details:

- **No `cli/internal/api_client/topology.go`.** Initiative #363
  names that path, but the CLI codebase convention (documented in
  `cli/internal/cmd/kb/kb.go`) is one in-package
  `resolveBackplane` / `doAuthedRequest` / `renderRequestError` trio
  per verb tree, not a shared client package — a shared helper
  imported from `cmd/*` and a per-tree package closes an import
  cycle. The topology trio lives in `topology/topology.go`; the
  intent ("a Go client for the T5 routes") is satisfied in-package.
- **`--kind` is the edge filter; `--node-kind` disambiguates the
  anchor.** The route takes `kind` (anchor `(tenant_id, kind, name)`
  pin) and `kind_filter` (walk-edge filter) as two distinct params.
  The verb spec in #454 says `--kind <edge_kind>`, so `--kind` maps
  to `kind_filter`; the separate `--node-kind` flag maps to `kind`
  and is the remedy the 409 `ambiguous_node` render points at.
- **Flag→param mapping for `path`.** `from`/`to` are positional
  args sent as the `?from=`/`?to=` query params; `--from-kind` /
  `--to-kind` map to `from_kind` / `to_kind`; `--max-hops` to
  `max_hops`.
- **Client-side range guards.** `--depth` (1..64) and `--max-hops`
  (1..32) mirror the API's `Query(le=...)` ceilings and fail fast
  client-side so the operator sees the constraint instead of a 422,
  matching the `meho targets list --limit` precedent.
- **Tenant boundary surfaces as the not-found / empty / null
  shape.** A cross-tenant target on `refresh` → resolver 404
  (`unexpected_response`, exit 4, near-misses surfaced). A
  cross-tenant node name on `dependents`/`dependencies` → empty list
  → the "no node named …" line (exit 0). A cross-tenant endpoint on
  `path` → `null` → the no-path line (exit 0). The CLI never
  distinguishes "exists in another tenant" from "does not exist" —
  the backend already collapses them, and the CLI render preserves
  that.
- **`path` returns `TopologyPath | null`.** A literal JSON `null`
  (HTTP 200) is the unreachable / missing-endpoint answer. `getPath`
  decodes into a nil `*Path` (the CLI's local mirror type; distinct
  from a decode error);
  `--json` re-emits `null` verbatim so a jq consumer sees one
  stable contract.
- **Exit codes** match the sibling verb trees: 0 ok (including
  empty/no-drift/no-path), 2 auth_expired, 3 unreachable, 4
  unexpected_response (404/409/malformed), 5 insufficient_role.

Tests: `cli/internal/cmd/topology/{topology,e2e}_test.go` (path
builders, renderers, and end-to-end through the auth+transport stack
against `httptest` — including the 409 ambiguous-node, the
cross-tenant 404/empty/null boundary, 403, and 401 cases);
`cli/internal/cmd/targets/discover_test.go` (discover path builder,
tables, happy path, `--json`, cross-tenant seed 404).

## Dependencies

- `meho_backplane.connectors.resolver.resolve_connector` +
  `meho_backplane.operations._handler_resolve.get_or_create_connector_instance`
  — connector resolution (G0.6).
- `meho_backplane.connectors.schemas` — `TopologyHints` / `NodeHint` /
  `EdgeHint` (G9.1-T2 #449).
- `meho_backplane.db.models` — `GraphNode` / `GraphEdge` / `AuditLog` /
  `Target` / `Tenant`. The recursive joins ride
  `graph_edge_tenant_from_idx` / `graph_edge_tenant_to_idx`.
- `meho_backplane.db.engine.get_sessionmaker` — each read verb opens
  its own `AsyncSession` (session-per-call, mirroring the memory / kb
  services). No caller-owned session.
- `meho_backplane.auth.operator.Operator` — `operator.tenant_id` is
  the only tenant boundary; every read statement filters node and edge
  `tenant_id` against it.
- `meho_backplane.broadcast` — `BroadcastEvent` / `publish_event`
  (fail-open, G6.1).
- `meho_backplane.settings` — `topology_refresh_interval_seconds`.
- `meho_backplane.metrics` — `TOPOLOGY_REFRESH_TOTAL` counter
  (`outcome` label: `ok` / `error` / `skipped_locked`).
- SQLAlchemy 2.0 `text()` with `:named` binds — same raw-SQL pattern
  `meho_backplane.retrieval.retriever` uses (`CAST(:x AS text) IS NULL
  OR ...` for optional filters, UUIDs passed as `str`). Every read
  statement is a module-level fully-literal `text("...")`; nothing is
  interpolated so the `avoid-sqlalchemy-text` SAST rule does not fire.

## Known issues / out of scope

- **PostgreSQL-only read path.** The `WITH RECURSIVE ... CYCLE` clause
  is not implemented by SQLite, so the query verbs cannot run on the
  unit suite's per-test SQLite DB. Tests live in
  `backend/tests/integration/test_topology_query.py` against a real
  `pgvector/pgvector:pg16` testcontainer (Docker-gated skip on
  no-Docker sandboxes; runs in CI). The pure Pydantic result-model
  contracts (deep `properties` immutability, `TopologyPath` invariants)
  have no DB dependency and are unit-tested in
  `backend/tests/test_topology_query_schemas.py`.
- **Unweighted only.** `find_path` treats every edge as cost 1.
  Weighted-edge support is deferred to v0.2.next per #363.
- **No graph-node alias resolution yet.** `name_or_alias` is matched
  against `graph_node.name` directly. The T5 query routes pass the
  path/query name straight through — they do *not* alias-resolve graph
  nodes (only the `refresh` / `targets/discover` routes alias-resolve
  the *target* via `resolve_target`). Graph-node alias → name
  resolution is deferred to the CLI/MCP fronts (T6/T7).
- Streaming refresh progress for very large topologies — v0.2 is
  single-shot; deferred per Initiative #363.
- Graph history / time-travel — soft-delete here only makes a node
  invisible to default queries; the history surface is G9.3.
- Per-connector `discover_topology` overrides — each G3.x Initiative.
- The advisory lock is a multi-replica stampede guard only; a single
  process serialises naturally and the SQLite test path no-ops it.

## References

- Parent Initiative: G9.1 #363; prerequisites #448 / #449.
- Write half: G9.1-T3 #450. Read half: G9.1-T4 #451.
- Prerequisite schema: G9.1-T1 #448 (migration `0007`).
- Audit/broadcast pattern mirrored from
  `backend/src/meho_backplane/operations/_audit.py`.
- PostgreSQL recursive CTE + `CYCLE`:
  <https://www.postgresql.org/docs/17/queries-with.html> §7.8.2.2
  (identical in PG 16, the chassis floor).
