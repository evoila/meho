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
`dependents` / `dependencies` / `path`) and `list_targets` register in
`mcp/tools/topology.py` and call `query.py` / `select(TargetORM)`
directly (sibling fronts on one backplane, not REST wrappers). G9.1-T8
(#456) shipped the closing acceptance suite
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

## REST API surface (T5, #453)

`backend/src/meho_backplane/api/v1/topology.py` is the HTTP front for
the read + write halves. Five routes total — four on the topology
router, one on the targets router:

| Method + path | Wraps | op_id | RBAC |
|---|---|---|---|
| `GET /api/v1/topology/dependents/{name}` | `find_dependents` | `topology.dependents` | operator |
| `GET /api/v1/topology/dependencies/{name}` | `find_dependencies` | `topology.dependencies` | operator |
| `GET /api/v1/topology/path?from=A&to=B` | `find_path` | `topology.path` | operator |
| `POST /api/v1/topology/refresh/{target_name}` | `refresh_target_topology` | `topology.refresh` | operator |
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
  an explicit `kind`.
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
  tenant-scoped via `resolve_target`. Cross-tenant traversal *and*
  cross-tenant refresh are impossible by construction (proven by the
  two-tenant integration test).

Tests: `backend/tests/test_api_v1_topology.py` +
`backend/tests/test_api_v1_targets_discover.py` (service layer patched,
SQLite); `backend/tests/integration/test_topology_api.py` (all 5 routes
end-to-end + the cross-tenant boundary against a real
`pgvector/pgvector:pg16` container).

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
