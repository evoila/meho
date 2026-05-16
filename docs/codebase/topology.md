# topology — graph refresh service + scheduled background task

## Overview

`backend/src/meho_backplane/topology/` is the **write** half of the G9.1
topology graph (Initiative #363). It takes a connector's discovery
snapshot and reconciles it into the `graph_node` + `graph_edge` tables
that G9.1-T1 (#448) created and that G9.1-T4's recursive-CTE traversal
(`dependents` / `dependencies` / `path`) reads.

Two entry points:

- `refresh_target_topology(target, operator) -> RefreshResult` — one
  on-demand refresh of a single target.
- `start_topology_refresh_scheduler() -> asyncio.Task` — the lifespan
  background loop that sweeps every tenant's targets on a cadence.

Read paths (the three query verbs), the REST/CLI/MCP fronts, and the
`docs/architecture/topology.md` operator doc are **out of scope** here —
they land in G9.1-T4 through T8.

## Key types

### `RefreshResult` — frozen Pydantic v2

Returned by `refresh_target_topology`. Per-object-class disjoint counts:
a node is in exactly one of `added_nodes` / `updated_nodes` /
`removed_nodes` (or none, when unchanged); same for edges.
`duration_ms` covers the whole resolve + discover + reconcile + commit
cycle. `target_id` echoes the refreshed target.

## Control flow

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

## Dependencies

- `meho_backplane.connectors.resolver.resolve_connector` +
  `meho_backplane.operations._handler_resolve.get_or_create_connector_instance`
  — connector resolution (G0.6).
- `meho_backplane.connectors.schemas` — `TopologyHints` / `NodeHint` /
  `EdgeHint` (G9.1-T2 #449).
- `meho_backplane.db.models` — `GraphNode` / `GraphEdge` / `AuditLog` /
  `Target` / `Tenant`.
- `meho_backplane.broadcast` — `BroadcastEvent` / `publish_event`
  (fail-open, G6.1).
- `meho_backplane.settings` — `topology_refresh_interval_seconds`.
- `meho_backplane.metrics` — `TOPOLOGY_REFRESH_TOTAL` counter
  (`outcome` label: `ok` / `error` / `skipped_locked`).

## Known issues / out of scope

- Streaming refresh progress for very large topologies — v0.2 is
  single-shot; deferred per Initiative #363.
- Graph history / time-travel — soft-delete here only makes a node
  invisible to default queries; the history surface is G9.3.
- Per-connector `discover_topology` overrides — each G3.x Initiative.
- The advisory lock is a multi-replica stampede guard only; a single
  process serialises naturally and the SQLite test path no-ops it.

## References

- Task #450 (G9.1-T3), Initiative #363, prerequisites #448 / #449.
- Audit/broadcast pattern mirrored from
  `backend/src/meho_backplane/operations/_audit.py`.
