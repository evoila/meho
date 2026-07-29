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

- `find_dependents(operator, name_or_alias, *, kind=None, depth=16, kind_filter=None, include_stale=True)`
  — reverse traversal, "what depends on me".
- `find_dependencies(operator, name_or_alias, *, kind=None, depth=16, kind_filter=None, include_stale=True)`
  — forward traversal, "what I depend on".
- `find_path(operator, from_name, to_name, *, from_kind=None, to_kind=None, max_hops=8)`
  — shortest unweighted path, or `None` if unreachable.

**Annotation — entry points (async, write half, G9.2-T3 #595):**

- `annotate_edge(session, operator, from_ref, kind, to_ref, *, note=None, evidence_url=None) -> GraphEdge`
  — create or refresh a curated edge. Resolves both endpoints via
  `resolve_node`, validates `kind` against the open slug grammar
  (`KIND_SLUG_PATTERN`, T1 #2534 — any lowercase slug, 2–63 chars;
  `GraphEdgeKind` is the documented well-known set, not a gate),
  idempotent on `(tenant_id, from_node_id, to_node_id, kind)`. Runs
  §6 conflict detection (sticky `superseded_by` for
  same-kind/different-endpoint auto edges; bidirectional
  `conflicts_with` for incompatible kinds over the same endpoint
  pair). Writes one audit row (`op_id="topology.annotate"`,
  `op_class="write"`) and publishes one broadcast event.
  **Precondition:** both endpoints must already exist as `graph_node`
  rows. A fresh tenant has zero nodes; seed them via
  `create_or_get_node` (manual seed) or `refresh_target_topology`
  (probe-driven) first.
- `unannotate_edge(session, operator, *, edge_id=None, from_ref=None, kind=None, to_ref=None) -> UUID`
  — hard-delete a curated edge (selector is either `edge_id` or the
  full triple; both/neither → `UnannotateSelectorError`). Refuses
  `source='auto'` rows with `AutoEdgeDeletionError` (auto edges
  resurrect on next refresh). Clears reciprocal `superseded_by` /
  `conflicts_with` markers the deleted edge left on auto rows.
  Writes one audit row + one broadcast event.

**Manual node seed — entry point (async, write half, G0.9.1-T6 #778):**

- `create_or_get_node(session, operator, *, kind, name, note=None, evidence_url=None) -> CreateNodeResult`
  — manually seed a `graph_node` row in the operator's tenant.
  Validates `kind` against the open slug grammar
  (`KIND_SLUG_PATTERN`; raise `InvalidNodeKindError` *before* any DB
  write — `WELL_KNOWN_NODE_KINDS` is the documented core set, not a
  gate), then idempotent
  upsert on the `graph_node_tenant_kind_name_idx`
  (`(tenant_id, kind, name)`) unique key. Manual seeds set
  `source='curated'` + `discovered_by=operator.sub`; a re-seed over an
  auto-discovered row promotes it to `source='curated'` +
  `discovered_by=operator.sub` (mirrors `annotate_edge`'s
  auto→curated promotion; #2536). The `source` column is what shields
  curated nodes from refresh overwrites, target adoption, and
  refresh-driven soft-deletes. Writes one audit row
  (`op_id="topology.create_node"`, `op_class="write"`,
  `method="CREATE_NODE"`) and publishes one broadcast event
  (fail-open after commit). Closes the **empty-tenant bootstrap gap**:
  before this verb, a fresh tenant could not reach a working topology
  state via MCP because `annotate_edge` requires both endpoints to
  already exist and the only node-creating path was the CLI verb
  `meho topology refresh <target>`. The verb is also the canonical
  path for **curated inner-graph nodes the probes cannot derive**
  (vault-role, keycloak-realm, externally-managed principals).

**Manual node delete — entry point (async, write half, #2485):**

- `delete_node(session, operator, *, node_id) -> DeleteNodeResult`
  (`topology/node_delete.py`) — guarded hard-delete of a
  manually-seeded node. Resolves `node_id` tenant-scoped, then applies
  three guards in order: `NodeNotFoundForDeleteError` (404) when the id
  does not resolve in the tenant (cross-tenant ids indistinguishable
  from missing); `NodeNotDeletableError` (409 `probe_owned_node`) when
  the row is probe-owned — `source != 'curated'` (probe-derived,
  including auto-discovered inner-graph nodes refresh reconciliation
  owns) **or** `target_id IS NOT NULL` (adopted onto a target; would
  resurrect on the next probe); `NodeHasLiveEdgesError` (409
  `node_has_edges`, echoing the blocking `edge_ids`) when any live
  `graph_edge` (`last_seen IS NOT NULL`) references the node. Only
  `source='curated'` **and** `target_id IS NULL` seeds are deletable —
  the delete-half mirror of the §3 auto-edge rule
  `unannotate_edge` enforces. On the happy path it writes one `removed`
  `graph_node_history` tombstone (`before=snapshot` / `after=None`) +
  one audit row (`op_id="topology.delete_node"`, `op_class="write"`,
  `method="DELETE_NODE"`), hard-deletes the row, and publishes one
  broadcast event (fail-open after commit). The DB `ON DELETE CASCADE`
  on `graph_edge` stays a backstop only (tenant purges + test cleanup):
  a bare cascade would drop referencing edges without their
  `graph_edge_history` tombstones, which is exactly why the service
  refuses instead. The node's prior `graph_node_history` rows (and the
  fresh tombstone) survive the hard-delete with `node_id` NULL via the
  `graph_node_history.node_id` `ON DELETE SET NULL` FK, so the timeline
  facet stays renderable.

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
  kind slug (any well-formed slug; the vocabulary is open), `source=`
  selects `'auto'` vs `'curated'`,
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
(`required_role=TENANT_ADMIN`, `op_class="write"`), visible only
to a tenant_admin-scoped session. G0.9.1-T6 (#778) added a third
admin meta-tool `meho.topology.create_node` in
`mcp/tools/topology_create_node.py` (separate module to keep
`mcp/tools/topology.py` from accreting further past the 600-line
guidance; the registry auto-discovers either way) that closes the
empty-tenant bootstrap gap — same `tenant_admin` / `write` shape as
the annotate pair. #2539 added a fourth admin meta-tool
`meho.topology.bulk_import` (own module `mcp/tools/topology_bulk_import.py`,
same separate-module reason) — batch curated-edge authoring for the
agent surface, closing the MCP half of the propose→plan→apply loop the
REST / CLI / console bulk-import fronts already had. #2485 added a fifth
admin meta-tool `meho.topology.delete_node` (own module
`mcp/tools/topology_delete_node.py`, same separate-module reason) — the
guarded hard-delete counterpart to `create_node` that removes a
manually-seeded node by id (same `tenant_admin` / `write` shape).

Since #2537 the MCP write handlers no longer call the service
primitives directly: they route through `operations.dispatch()` with
the targetless typed ops `topology.annotate` / `topology.create_node`
/ `topology.delete_node` / `topology.unannotate` / `topology.bulk_import`
registered by
`connectors/topology/ops.py` under the synthetic connector id
`topology-graph-1.x` (the `secret.move` mold — module-level handlers,
`target=None`, `parse_connector_id`-compatible identity). The
descriptors carry `safety_level="caution"` + `requires_approval=False`:
an AGENT principal's write hits the needs-approval floor in
`policy_gate` and parks as a durable `ApprovalRequest` (the MCP tool
returns a `{status: awaiting_approval, approval_request_id, ...}`
envelope; the write executes with the stored params when a human
approves from any approvals surface), while a human tenant_admin rides
the default-allow branch and executes immediately — same UX as before.
The typed-op handlers unwrap params and call `annotate_edge_with_plan` /
`create_or_get_node` / `unannotate_edge` / `bulk_import_edges` unchanged;
domain errors come
back as `connector_error` results whose `exception_class` the MCP shim
(`dispatch_topology_write` in `mcp/tools/topology.py`) maps back to
JSON-RPC `-32602`. Each gated MCP write therefore produces one extra
audit row (`method="DISPATCH"`, `path=<op_id>`, with
`policy_decision`) alongside the service-level row.

`meho.topology.bulk_import` (#2539) is the one two-behaviour tool. Its
`dry_run` param splits the path: `dry_run=true` (the default,
read-shaped) calls `bulk_import_edges(dry_run=True)` **directly** — no
dispatch, no gate — so an agent's harmless plan preview never parks; a
`BulkImportValidationError` becomes a -32602 whose `error.data` carries
every row's diagnostic (the REST `422 invalid_bulk` analogue).
`dry_run=false` dispatches the apply-only typed op `topology.bulk_import`
(registered with `rows` alone — no `dry_run`, so the parked
`ApprovalRequest` holds exactly the batch to apply) through the same
gate: an AGENT parks the whole batch as one request, a human applies
immediately, and approve-time re-dispatch applies all rows in the
service's all-or-nothing transaction. The 1000-row cap is enforced at
the tool boundary via the `inputSchema` `maxItems` (mirroring the REST
`_BULK_IMPORT_MAX_EDGES` guard); like `query_topology`'s inline edge
list, the plan is returned inline under a hard row cap rather than a
JSONFlux handle. `meho.topology.annotate`'s return shape gained a
`superseded` list (#2539): the ids of the auto edges the assertion
displaced — already stamped on the shared audit / broadcast payload, so
surfacing them on the return is a shape change, not a new query (the
handler reads `plan.audit_payload["superseded"]` from
`annotate_edge_with_plan`, the plan-returning sibling of `annotate_edge`). The REST + UI
write fronts are human-only surfaces and keep calling the service
primitives directly. The three write ops are also discoverable /
dispatchable through the generic agent meta-tools (`search_operations`
/ `call_operation`) with identical gating, since the gate lives in the
dispatcher, not the front.
G9.1-T8 (#456) shipped the closing
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

Two nullable fields discriminate the all-zero-count no-op classes
(#2093): `no_populator_for_product` carries the target's product slug
when the resolved connector inherits the base-class
`discover_topology` no-op (no populator shipped — the refresh was a
no-op **by coverage gap**), and `populated_products` then lists the
registered products that do override it (sorted, deduped across
version/impl registry entries). Both are `null` when a populator ran —
including a run that legitimately reconciled zero changes — so a
consumer distinguishes "nothing changed" from "nothing could ever
change" with a single field check. Populator detection is function
identity against `Connector.discover_topology`
(`refresh._has_populator`), which correctly classifies the
operator-aware keyword-only override on `KubernetesConnector` and the
auto-shim subclasses that inherit the base default. The CLI `refresh`
verb appends a human-readable note when the signal is set, and the
operator console's refresh partial
(`ui/templates/topology/_refresh_result.html`) renders the same note
as a warning callout next to the counts (#2210).

### `TopologyNode` — frozen Pydantic v2 (read half)

| Field | Type | Meaning |
|---|---|---|
| `id` | `UUID` | `graph_node.id`. |
| `kind` | `str` | `graph_node.kind` (open slug vocabulary since migration 0063 / T1 #2534; `WELL_KNOWN_NODE_KINDS` is the documented core set). |
| `name` | `str` | `graph_node.name`, unique within `(tenant_id, kind)`. |
| `source` | `str` | `'auto'` (probe-derived) or `'curated'` (operator-seeded / promoted; #2536). Mirrors `TopologyEdge.source`. |
| `properties` | `dict` | `graph_node.properties` JSONB; wrapped in `MappingProxyType` after validation so the frozen model is deeply immutable, serialised back to a plain `dict`. |
| `depth` | `int` | Distance from the query root: root = 0, immediate = 1, transitive = 2, … |
| `via_edge_kind` | `str \| None` | The `graph_edge.kind` of the edge used to reach this node; `None` for the root. |
| `parent_node_id` | `UUID \| None` | #2538 chain provenance: the `graph_node.id` the walk stepped from; `None` for the root. In a closure result the parent is always itself a row of that result, so the flat list reconstructs the exact dependency chain without follow-up edge lookups. Additive (`None` default). |
| `via_edge_id` | `UUID \| None` | #2538 chain provenance: the `graph_edge.id` that was walked to reach this node; `None` for the root. Additive (`None` default). |

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
| `kind` | `str` | Any kind slug (open vocabulary since migration 0063 / T1 #2534); the ten `GraphEdgeKind` values are the documented well-known set. |
| `source` | `str` | `'auto'` (probe-derived) or `'curated'` (operator-asserted). |
| `properties` | `dict` | `graph_edge.properties` JSONB; deep-frozen (same discipline as `TopologyNode.properties`). Carries the conflict markers `conflicts_with` (array, G9.2-T3 #595) and `superseded_by` (UUID, also #595). |
| `last_seen` | `datetime \| None` | The refresh service's "I observed this edge at" timestamp. NULL after a soft-delete; soft-deleted edges are excluded from `list_edges` by default. Also the stable total-order key the helper paginates against. |

## Control flow — write half

### `refresh_target_topology`

1. `resolve_connector(target)` → `get_or_create_connector_instance(cls)`
   (the same cached-singleton path the G0.6 dispatcher uses).
2. `await connector.discover_topology(target)` → a `TopologyHints`
   snapshot (nodes + edges, each with `properties`). When the resolved
   class does **not** override the base no-op (`_has_populator` is
   false), the eventual `RefreshResult` is stamped with
   `no_populator_for_product` + `populated_products` (#2093); the
   reconcile still runs on the empty snapshot — the audit/broadcast
   contract is unchanged and stale nodes this target adopted earlier
   still soft-delete.
3. Open one transactional session (`sessionmaker() ... session.begin()`).
4. `_reconcile_nodes` — diff the snapshot nodes against existing
   `graph_node` rows. The upsert decision is keyed on the **tenant-wide
   natural key** `(tenant_id, kind, name)` — the same grain as the
   `graph_node_tenant_kind_name_idx` unique index, which is
   target-independent. The existing-node lookup therefore unions, within
   the tenant, every row whose `(kind, name)` is in the snapshot **or**
   whose `target_id` is the refreshing target's id:
   - INSERT nodes in the snapshot with no existing `(tenant, kind, name)`
     row (`source='auto'`).
   - For an **auto** node already present under *any* `target_id`
     (another target's discovery): refresh `last_seen`, apply the probe
     `properties`, and **adopt** the row onto the refreshing target
     (`target_id` claimed) so this target owns its lifecycle going
     forward. A no-change refresh of an already-owned node only touches
     `last_seen`, so the `unchanged` path reports zero `updated`.
   - For a **curated** node (`source='curated'` — operator-seeded via
     `create_or_get_node`, or promoted by a re-seed over an auto row):
     bump `last_seen` only (`_refresh_curated_node`, the node-side
     mirror of `_refresh_curated_edge`; #2536). No property overwrite,
     no `target_id` adoption — the probe's view of an operator-owned
     row is not authoritative. A resurrected curated node
     (`last_seen IS NULL → now`) counts as `updated` and emits a
     history row; a pure heartbeat does neither.
   - Soft-delete (set `last_seen = NULL`) only **auto** nodes owned by
     the refreshing target (`target_id == target_id`) that are absent
     from the snapshot. Rows owned by another target are never
     soft-deleted by a refresh that does not own them, and curated
     nodes are never soft-deleted by *any* refresh (the explicit
     `source == 'curated'` guard is load-bearing for promoted rows,
     which keep the historical `target_id` from their auto days;
     #2536). A node already soft-deleted is not re-counted.
   Returns two key→id maps: `live` (snapshot-present nodes only) and
   `all` (every loaded node, including soft-deleted ones owned by this
   target).

   Keying the lookup on `(tenant_id, target_id)` only was a defect
   (#673): a snapshot node that already existed under a different /
   `NULL` `target_id` was missed, re-INSERTed, and collided with the
   unique index mid-reconcile (the violation surfaced as an
   `IntegrityError` via query-invoked autoflush inside
   `_reconcile_edges`).
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
(`via_edge_kind` / `parent_node_id` / `via_edge_id` NULL), filtered by
`CAST(:kind AS text) IS NULL OR n.kind = :kind` so a pinned `kind`
resolves the `(tenant_id, kind, name)` unique row. The recursive term
joins `graph_edge` to the walk frontier, scoped on `tenant_id` on both
the edge and the destination node, applies `CAST(:kind_filter AS text)
IS NULL OR e.kind = :kind_filter`, bounds `w.depth < :depth`, and
projects the chain provenance (`w.id AS parent_node_id`, `e.id AS
via_edge_id` — #2538). The final projection wraps the filtered walk in
a `SELECT DISTINCT ON (id) ... ORDER BY id, depth, name,
parent_node_id, via_edge_id` subquery (keeping the minimum-depth
occurrence of each node; the provenance columns extend the ORDER BY as
a deterministic tie-break so converging equal-depth parents resolve
identically on every run) and re-orders the result by `(depth, name)`.
`CYCLE` only prevents revisiting a node on the *same* branch; the
`DISTINCT ON` collapse is what makes a converging DAG return one row
per node rather than one row per path.

### Staleness opt-out (`include_stale`, #2538)

Traversal defaults to **last-refresh-wins**: soft-deleted rows
(`last_seen IS NULL`) stay reachable, because the row was real at the
last observation and a blast-radius answer that silently forgets it is
a false negative. `list_edges` takes the opposite default (a live
inventory view). #2538 makes the disagreement per-query controllable:
all three traversal verbs (`find_dependents` / `find_dependencies` /
`find_path`) accept `include_stale: bool = True`. Passing `False` adds
`last_seen IS NOT NULL` predicates via the `CAST(:include_stale AS
boolean) OR ...` idiom:

- closure verbs: on the stepped-to node **and** the walked edge in the
  recursive term;
- `find_path`: on **both** `bi_edge` legs (same both-legs rule as the
  superseded-edge guard — missing the reversed leg would let a stale
  edge be walked backwards into a path) and on the stepped-to node
  (`dn` join in the recursive term), where the `to` endpoint is
  exempted via `EXISTS` against the non-recursive `target` CTE — the
  `from` endpoint enters through the walk's base term, so both named
  endpoints stay reachable and `include_stale=False` reachability is
  symmetric in argument order.

The anchor / endpoint rows named by the caller are exempt — anchor
existence is governed by the `NodeNotFoundError` / silent-`None`
contracts, not staleness. The flag rides the whole stack: REST query
param `include_stale` on all three routes, CLI `--include-stale=false`,
MCP `query_topology.include_stale`.

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

The root is always included at depth 0, so a tracked node with no
dependents returns the one-element `[root]`. G0.18-T4 (#1357,
RDC #789 N2) changed the not-found contract: an anchor with no
matching `graph_node` row in the tenant raises
`NodeNotFoundError` (resolved up front via
`resolvers.resolve_node`) rather than returning `[]`. Pre-G0.18-T4
the empty list conflated "tracked, nothing depends on me" with
"anchor isn't in the graph at all," and the pre-destructive
blast-radius use case mis-read the empty list as "safe to
delete" — a SEV-3 false-negative for every registered non-k8s
target (auto-discovery is k8s-only today; only
`KubernetesConnector` overrides `discover_topology`). The REST
front maps the exception to `404 node_untracked`; the MCP front
returns the typed `{kind, status: "node_untracked", name,
nodes: []}` envelope. The CLI's `formatNotFound` renders the
operator-actionable "register / refresh the target or annotate
the relationship" prompt. `find_path` keeps its G9.1 silent-on-miss
contract — `None` is the recoverable "no route" answer for a
single-anchor walk and is structurally distinct from "this anchor
isn't in the graph."

### Path search

`find_path` builds a `bi_edge` CTE — the union of forward and reversed
tenant-scoped edges (each leg projecting the edge `id` since #2538) —
so reachability is undirected while storage stays directed. The
recursive `walk` accumulates `node_ids`, `edge_kinds`, and `edge_ids`
arrays; `CYCLE node_id SET is_cycle USING visited` plus the `hops <
:max_hops` bound terminate the search. `ORDER BY hops LIMIT 1` yields
a shortest path. A second query materialises the winning path's node
rows; `_build_path_nodes` re-orders them into path sequence and
attaches `depth` / `via_edge_kind` / `via_edge_id` (positionally, hop
`i` belongs to node `i+1`) plus `parent_node_id` (the previous node on
the path).

**Per-branch target pruning (#2535).** The walk enumerates simple
paths, so on a dense mesh its row count grows ~branch_factor^hops. A
non-recursive `target` CTE resolves the destination id once, and the
recursive term carries `NOT EXISTS (SELECT 1 FROM target t WHERE t.id
= w.node_id)` — a branch that reaches the target stops growing.
Extending past a hit can never shorten a path to that same hit (and
the CYCLE guard already forbids re-entering the target on the same
branch), so pruning removes only rows the final select could never
pick: results are identical, cost is bounded per branch. Referencing
the non-recursive `target` CTE from the recursive term's subquery is
legal — PostgreSQL's recursive-term restrictions cover only the
recursive self-reference (`walk`), which must appear exactly once and
not inside a subquery. That same restriction is why **global**
cross-branch early termination cannot be written, and `ORDER BY hops
LIMIT 1` cannot stop evaluation early because the sort consumes the
full walk. The unreachable-target worst case therefore still
enumerates the whole ≤`max_hops` ball; its envelope is pinned by
`tests/integration/test_topology_path_pruning.py` on a dense
`MeshSpec` mesh (row-count pin + load-invariant timing ratio, see
`docs/architecture/topology.md` §Performance expectations).

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

1. Validate `kind` against the open slug grammar
   (`KIND_SLUG_PATTERN` via `is_valid_kind_slug`; raise
   `InvalidEdgeKindError` *before* any DB read — the error message
   names the pattern and echoes the well-known kinds as suggestions).
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

## Diff-on-write history hook (write half — G9.3-T2 #857)

The append-only `graph_node_history` + `graph_edge_history` tables
(shipped by G9.3-T1 #856) are populated by a **single shared writer**
the refresh + annotate paths both call:
`backend/src/meho_backplane/topology/history.py`. The hook emits one
history row per applied live mutation inside the **same**
`session.begin()` block as the mutation itself — atomicity is the
load-bearing contract: the live graph and the history table can
never disagree about which mutations committed.

### Snapshot shape

Every history row's `snapshot` JSONB is
`{"before": <row-json>|None, "after": <row-json>|None}`:

* `created` — `before=None`, `after=<post-insert row>`.
* `updated` — `before=<pre-mutation row>`, `after=<post-mutation row>`.
* `removed` — `before=<final row>`, `after=None`.

The bidirectional projection is what makes `meho topology diff
<ts1> <ts2>` (T4 #860) reconstructible without joining back against
live tables — a tombstone row carries enough state to render the
removed resource. Column selection is deliberately narrow (the
`_NODE_SNAPSHOT_COLUMNS` / `_EDGE_SNAPSHOT_COLUMNS` constants in
`history.py`) rather than `vars(row)` or `sqlalchemy.inspect`-based
reflection: a future column that should *not* enter the snapshot
(e.g. a derived counter) opts in via the projection list.

### audit_id linkage

Every history row carries the **causing operation's**
`audit_log.id` (soft-FK column `audit_id`). The refresh / annotate
service pre-allocates one `audit_id` at the top of the request via
`uuid.uuid4()`, writes one audit row with that id, and threads the
same id down to every history row the operation emits. Re-using one
audit row per operation (rather than per history row) keeps
`audit_log` at one row per operation — the contract `audit_log`
consumers rely on — and lets `meho topology diff` / `history` /
`timeline` (T3-T5) join history back to the principal / session /
target via `audit_log.id`.

### Refresh path

`_reconcile_nodes` and `_reconcile_edges` (in `topology/refresh.py`)
each take an `audit_id` parameter threaded from
`_apply_reconcile` → `refresh_target_topology`. Per applied
mutation:

* **Insert branch** — capture nothing (no prior state); emit
  `created` with `before=None`, `after=node_snapshot(new_row)`.
* **Update branch** — capture `before = node_snapshot(row)` **before**
  reassigning the row's columns (otherwise the snapshot aliases the
  post-mutation state); reassign columns; emit `updated` with
  `after=node_snapshot(row)`. The history row fires under the same
  predicate the refresh `updated` counter uses (`last_seen IS NULL`
  OR `target_id` changed OR properties differ) — a pure `last_seen`
  heartbeat is not a recorded mutation.
* **Soft-remove branch** — capture `before` before nulling
  `last_seen`; emit `removed` with `after=None`.

Edge reconciliation is identical, with one extra wrinkle: the
*resurrected curated edge* case (`last_seen IS NULL → now`) emits an
`updated` history row even though the only changed column is
`last_seen` — the resurrection is operator-observable (the edge
returned to traversal), so it warrants a row. A pure heartbeat on
an already-live curated edge does not. Since #2536 the node pass
carries the same wrinkle: `_refresh_curated_node` emits `updated`
only for a resurrected curated node, never for a heartbeat.

### Annotate path

`annotate_edge_in_txn` emits history rows for **every mutated edge**
inside the same `session.begin()` block as the upsert:

* The curated row itself — `created` (fresh insert) or `updated`
  (idempotent re-annotate or auto→curated promotion). The `after`
  snapshot is captured *after* both §6 conflict scans run so any
  `conflicts_with` marker stamped on the curated row by
  `_mark_incompatible_kinds_conflict` is visible in `snapshot.after`.
* Each auto edge marked `superseded_by` (same-kind /
  different-endpoint, §6 rule 1) — `updated`, with `before` captured
  before the marker write so `snapshot.before` shows the row as the
  operator last saw it on `list-edges`.
* Each edge of a different kind over the same endpoint pair (§6
  rule 2) — `updated` with the same before/after discipline.

`_mark_same_kind_different_endpoint_superseded` and
`_mark_incompatible_kinds_conflict` now return
`list[tuple[GraphEdge, snapshot_before]]` instead of plain id lists;
the audit payload's `superseded` / `conflicts` arrays are derived
from `pair[0].id`.

### Unannotate path

`unannotate_edge` emits:

* One `removed` history row for the curated edge. The history row
  is staged in the session **before** `session.delete(edge)` so the
  insert and the delete flush in a stable order — flushing the
  delete first would let the FK `ON DELETE SET NULL` kick in and
  the freshly-inserted history row would land with
  `edge_id = NULL`, defeating the per-resource history walk in T3
  (the walk filters on `edge_id`).
* One `updated` row per referencing edge whose `superseded_by` /
  `conflicts_with` marker the unannotate just cleared. `before` is
  captured before `_clear_reciprocal_markers` runs so the cleared
  marker is visible in `snapshot.before` and absent in
  `snapshot.after` — the temporal-query verb can render the
  inverse of the original annotate as a single point-in-time
  mutation.

### No own audit rows

The hook never writes its own `audit_log` rows. Acceptance criterion
#5 of #857 — re-emitting an audit row per history row would balloon
the audit log on a topology refresh that touches dozens of resources,
and would also break the "one operation, one audit row" invariant
`audit_log` consumers rely on. The single audit row the refresh /
annotate service writes is the canonical audit record; history rows
reference it.

### Bulk import composition

`bulk_import_edges` (G9.2-T8 #600) calls `annotate_edge_in_txn` once
per row inside a shared `session.begin()` block — every per-row
history emission lands inside the same batch transaction and rolls
back atomically with the batch. No additional wiring was needed for
G9.3-T2: the hook is on the call path the bulk importer already
uses.

## Timeline query (read half — G9.3-T5 #861)

`query_timeline` (in `backend/src/meho_backplane/topology/query.py`)
is the tenant-wide chronological feed of graph changes — "what's
been happening in the graph in the last hour?" without rooting at a
specific resource. It UNIONs `graph_node_history` +
`graph_edge_history` and walks both tables in `(valid_from DESC,
history_id DESC)` order, paginated via opaque forward-only cursor.

### Pagination

OFFSET-based pagination is broken under the diff-on-write hook
(T2 #857) — new history rows land continuously, and offset shifts
every subsequent row. A keyset cursor over the `(valid_from,
history_id, source)` lex order is correctness-preserving: page N+1
starts at the row strictly after page N's last row, and a concurrent
insert either lands below the cursor (appears on a later page) or
above the cursor (stays outside the paged sweep). No row is
duplicated or skipped by the act of paging.

The cursor is opaque (base64-encoded JSON, see
`topology/timeline_cursor.py`) so consumers treat it as a token
rather than parsing it. `source` is the third component because the
two history tables have independent `BIGSERIAL` counters — a node
and an edge can share `(valid_from, history_id)`; alphabetical
order (`"node" > "edge"` in DESC) is the deterministic tie-breaker.

### Query shape

Two parallel single-index scans (one per table) followed by a
Python-side merge, rather than a SQL `UNION ALL`. Two reasons:

1. Each table's `(tenant_id, valid_from DESC)` composite index is
   the natural access path; the planner would otherwise materialise
   the union before sorting, defeating the index.
2. The `--target` filter joins `graph_node` (for the node side) and
   `graph_edge` + endpoint `graph_node` (for the edge side).
   Inlining those joins into a single UNION'd statement would mix
   two access paths in one plan — fragile under data growth.

The merge is a two-pointer pass: pop the larger head from either
list, with the source-discriminator tie-breaker. Memory is
O(per_side_fetch) = O(limit + 1) per side, bounded by `limit ≤ 1000`.

### Filters

- `target_id` — optional `targets.id`. Nodes filter on
  `graph_node.target_id`; edges filter on the endpoint nodes'
  `target_id` (either endpoint touching the target qualifies — an
  edge crossing two targets is part of both timelines).
- `since` / `until` — inclusive `valid_from` bounds. The CLI
  resolves duration shorthand (`24h` / `7d`) client-side to an
  absolute ISO-8601 before crossing the wire.
- `limit` — page size, default 50 per the Task #861 acceptance
  criterion; ceiling 1000.

### Audit class

The REST route binds `audit_op_id="topology.timeline"` /
`audit_op_class="audit_query"` per [decision #3](../decisions/locked-decisions.md)
— temporal graph queries are inspections of system state, parallel
to G8's audit-log query surface. The broadcast event carries only
`{op_id, result_status, row_count}` so the request filter (which
may name a sensitive target) and the row contents never leak onto
the SSE / Slack feed.

## History query (read half — G9.3-T3 #859)

`query_history` (in `backend/src/meho_backplane/topology/query.py`)
is the per-resource history walk — "what changed for THIS specific
resource?" Companion to `query_timeline` (tenant-wide feed,
truncated summary) but with two key differences:

1. **Anchored on one node.** The first thing the substrate does is
   call `resolve_node` to translate the operator-supplied name (and
   optional `kind` disambiguator) into a `graph_node.id`. An unknown
   name or a cross-tenant name surfaces as `NodeNotFoundError`; an
   ambiguous bare name as `AmbiguousNodeError`. The REST + MCP
   fronts map both to operator-actionable diagnostics (404 / 409
   for HTTP, JSON-RPC -32602 for MCP).
2. **Full snapshot per row.** Each `TopologyHistoryEntry` carries
   the row's `snapshot.before` / `snapshot.after` JSONB intact —
   the forensic payload the CLI's `--json` mode and the MCP facet
   need to answer "what was the exact state before this change?".
   The timeline's one-line summary truncation does not apply here.

### Include-edges join

By default `query_history` walks `graph_node_history` only for the
anchor's `node_id`. Passing `include_edges=True` also walks
`graph_edge_history` for every edge incident to the anchor — the
inner subquery is `edge_id IN (SELECT id FROM graph_edge WHERE
from_node_id = anchor OR to_node_id = anchor)`. Tenant scope is
enforced on both the inner and the outer query so a cross-tenant
edge id cannot leak in.

Tombstones (edge-history rows whose `edge_id` was NULLed by `ON
DELETE SET NULL` after the live edge was hard-deleted) drop out of
the inner subquery's id list and therefore stay out of the
per-resource walk — a tombstoned edge has no surviving live row to
associate with the anchor. Operators wanting the full tombstone
replay use `meho topology timeline` (G9.3-T5 #861).

### Indexes

The per-resource walk leans on two tenant-scoped composite indexes
declared by migration 0012 (G9.3-T1 #856):

- `graph_node_history` `(tenant_id, node_id, valid_from DESC)` —
  per-(tenant, node, time) lookup is a single composite-index scan.
- `graph_edge_history` `(tenant_id, edge_id, valid_from DESC)` —
  mirror for the edge side.

Both are sub-millisecond on the test fixture and indexed under
realistic load. The cross-Initiative integration suite
`backend/tests/test_topology_history_integration.py` (G9.3-T7 #862)
proves the envelope at 1M-row scale: it seeds a million-row
`graph_node_history` table (via a recursive-CTE bulk insert) and
asserts `EXPLAIN QUERY PLAN` for the single-node walk picks the
`(tenant_id, node_id, valid_from DESC)` composite index as an
index-only ("COVERING INDEX") scan with no full table scan. The plan
assertion is the load-bearing gate (a hard `<10ms` wall-clock bound
flakes under xdist + coverage); a generous wall-clock bound rides
along as a secondary regression smoke signal. The same suite proves
the full refresh → annotate → refresh chronology round-trips through
`query_history` with correctly-paired `audit_id`s, the `audit_id` →
`audit_log` soft-FK joins on `(tenant_id, principal)`, the
cross-tenant boundary on all three verbs, and the diff 1000-row hard
cap on a high-churn week.

### Pagination

Unlike `query_timeline` (cursor-paginated), `query_history` returns
one page bounded by `_MAX_HISTORY_ROWS = 5000`. Per-resource
history is bounded by the retention window (default 90 days) and
the operator typically wants the complete chronology in one
response. A caller that overflows the cap narrows `since` /
`until`; there is no `next_cursor`.

### Latent SQLite resolver bug fixed by this task

`resolve_node`'s bare-name branch runs a `text()` SQL with a
``tenant_id = :tenant_id`` filter. Before #859 the bind passed
`str(uuid)` (dashed form). On the SQLite test driver the `Uuid()`
column stores 32-char hex without dashes, and the dashed-string
filter silently matched zero rows — `resolve_node` then raised a
spurious `NodeNotFoundError`. Production PG accepts both forms,
masking the bug. #859 pins `_ANCHOR_KINDS_SQL` to a SAUuid bind
type so both dialects round-trip cleanly. The closure / path verbs
were unaffected because they call `_assert_anchor_unambiguous`,
which only raises when 2+ kinds match (a zero-row result is
intentionally silent there — G9.1 contract).

### Audit class

The REST route binds `audit_op_id="topology.history"` /
`audit_op_class="audit_query"` — same shape as `topology.timeline`.
The broadcast event carries only `{op_id, result_status,
row_count}` so the response rows (which may include the snapshot
of a sensitive resource's pre/post payload) never leak onto the
SSE / Slack feed.

## Manual node seed control flow (write half — G0.9.1-T6 #778)

### `create_or_get_node`

1. Validate `kind` against the open slug grammar
   (`KIND_SLUG_PATTERN` via `is_valid_kind_slug`; raise
   `InvalidNodeKindError` *before* any DB read — the error message
   names the pattern and echoes `WELL_KNOWN_NODE_KINDS` as
   suggestions).
2. Pre-allocate `audit_id = uuid.uuid4()` (chassis "audit-id
   pre-allocation" pattern shared with `refresh` / `annotate` —
   the same uuid is threaded into the `audit_log` row and the
   `graph_node_history` row so the temporal-query verbs can join
   history back against audit to recover the causing principal).
3. `async with session.begin()` — one transaction wraps the
   lookup + upsert + history write + audit write.
4. Look up the existing row for the `(tenant_id, kind, name)` unique
   tuple (the `graph_node_tenant_kind_name_idx` index). Found
   → capture `node_snapshot(existing)` as `before` *first*, then
   merge the four manual-seed property keys (`note`,
   `evidence_url`, `seeded_by`, `seeded_at`) onto the existing JSONB
   (auto-discovered keys like `status`, `phase` are preserved),
   refresh `last_seen`, and promote to `source='curated'` +
   `discovered_by=operator.sub` iff the existing row was
   probe-derived (auto→curated promotion; matches `annotate_edge`'s
   shape; #2536 — the `source` flip is what moves the row under the
   refresh service's curated-durability discipline). Absent →
   `INSERT` a fresh row with `source='curated'`,
   `discovered_by=operator.sub`, `target_id=None` (manual seeds
   never adopt onto a target — only the refresh service does that),
   `properties={note, evidence_url, seeded_by, seeded_at}`,
   `first_seen = last_seen = now`; `before` is `None`.
5. Diff-on-write hook (G9.3-T2 #857; create_node side added by
   G0.18-T6 #1359). Call `record_node_change` to add one
   `graph_node_history` row per *meaningful* call (CREATED on fresh
   insert; UPDATED on promotion or property change). An idempotent
   re-seed with the same `(note, evidence_url)` and no promotion is
   heartbeat-only (the `seeded_at` ISO timestamp and `last_seen`
   change every call regardless of intent) and skips the emit per
   `_create_node_is_meaningful` — same heartbeat-strip discipline
   `annotate._annotate_curated_is_meaningful` and
   `refresh._update_existing_node`'s `is_meaningful_update` use.
   The row carries the pre-allocated `audit_id` from step 2.
6. Add one `audit_log` row in the same session
   (`method="CREATE_NODE"`, `path="topology.create_node"`,
   `payload={op_id, op_class:"write", node_id, kind, name,
   was_created, note, evidence_url}`, `target_id` = the seeded
   node's own `target_id` when non-null, else `None`).
7. Commit. Then publish one `BroadcastEvent` (`op_class="write"` —
   set explicitly because the `.create_node` suffix is not in
   `_WRITE_SUFFIXES`; same rationale as `.annotate` /
   `.unannotate`). Publish is fail-open: a publish exception is
   logged, never raised.

**Manual seeds are visible to `kind=history` / `kind=timeline`.**
Before G0.18-T6 (#1359) the create_node hook wrote audit_log +
broadcast but no `graph_node_history` row, so `query_topology
kind=history <manually-seeded-node>` returned empty and
`kind=timeline` omitted the seed even though it surfaced in
`query_audit` — an audit-vs-graph-history asymmetry first surfaced
in RDC #789 finding F-A. The hook now emits one history row per
meaningful call so both verbs reflect manual seeds the same way
they reflect auto-refresh changes. The atomicity contract
`history.py` documents holds: a failure anywhere in the
`session.begin()` block rolls the live row, the history row, and
the audit row back together — the graph and the history table
can never disagree about which mutations committed.

**Idempotency invariant.** A repeat call with the same
`(kind, name)` always returns `was_created=False` after the first
insert — the unique index guarantees one row per triple, and the
refresh service's `_node_key((kind, name))` lookup recognises
operator-seeded rows on the next probe (refresh keys on the same
unique tuple, not on `discovered_by`). A seeded node the refresh
service later re-observes is heartbeat-only, never adopted:
`_refresh_curated_node` bumps `last_seen` and nothing else —
`properties`, `target_id`, and `discovered_by` are untouched
(`source='curated'` is the shield; #2536), and no refresh ever
soft-deletes the row. Audit-trail authorship is likewise permanent:
the `audit_log` row from this verb outlives any number of
subsequent probe re-observations.

**Not a refresh trigger.** This verb is a manual seed for nodes the
operator wants to assert directly (the empty-tenant bootstrap entry
point, or curated inner-graph nodes the probes cannot derive). It
does not run any probe, does not write edges, and does not set
`target_id`. The adopt-onto-target workflow (`target_id` claimed,
properties reshaped from the probe payload) applies to
`source='auto'` rows only; it no longer exists for seeded nodes.
A seeded node stays operator-owned forever — `target_id=None`,
properties exactly as asserted — until the operator deletes it and
lets a refresh re-discover the resource as a fresh `source='auto'`
row.

## Diff query (read half — G9.3-T4 #860)

`backend/src/meho_backplane/topology/query.py::query_diff` is the
graph-level **net delta** between two timestamps -- the heavy temporal
query that surfaces what changed between `ts1` (exclusive) and `ts2`
(inclusive). Each entry folds one resource's in-window history rows
into one of `created` / `updated` / `removed`:

* First in-window row is `created` AND last is not `removed` ->
  `created`.
* Last in-window row is `removed` -> `removed`. (Includes the
  created-and-removed-in-same-window case: post-window state is "gone".)
* Otherwise -> `updated`.

`changed_only=True` suppresses `updated` entries whose every in-window
history row is a `last_seen`-only refresh heartbeat. `kind_filter`
narrows to one resource kind (node `kind` like `vm` or edge `kind`
like `runs-on`), applied **after** the fold so the cap fires on the
post-filter cohort.

**1000-row hard cap.** [Unreleased] enforces a strict cap at
`_DIFF_HARD_CAP = 1000` -- on overflow the result returns
`truncated=True` plus the canonical "narrow the time window"
remediation hint. The CLI / REST / MCP fronts surface the hint
verbatim; the substrate is authoritative. The cap exists because a
hostile / wide time window over a churning tenant could otherwise
return tens of thousands of resources.

**SQL-layer fetch bound (#987).** The cap is enforced twice: once at
the SQL layer to bound *memory*, once in the Python fold to compute
the net delta. The diff statements (`_DIFF_NODE_SQL` /
`_DIFF_EDGE_SQL`) wrap the in-window scan in a `DENSE_RANK` window
that ranks resources by first in-window appearance and keep only the
first `_DIFF_FETCH_RESOURCE_CAP = _DIFF_HARD_CAP + 1` distinct
resources' **complete** row groups (`WHERE grp_rank <= :resource_cap`).
Without this, `fetchall` materialised every in-window row for a wide
window before the Python cap fired -- a memory blow-up (the same risk
the list verbs avoid with `_MAX_EDGE_LIMIT` / `_MAX_NODE_LIMIT`). The
bound is on **distinct resources**, not raw rows, because (a) one
resource can carry many history rows -- a raw `LIMIT` could split a
resource's group mid-fold and corrupt that entry, and (b) the cap
counts post-fold entries (one per resource), so a raw row `LIMIT`
could undercount and never trip `truncated`. The resource key is
`resource_id` when present, else the row's own `history_id`, so a
hard-deleted tombstone (`resource_id IS NULL`) counts as its own
group; the create-then-delete rejoin via the recovered id still
happens Python-side after fetch. The `+ 1` lets the fold see one
resource past the cap and set `truncated` with the same outcome as the
old unbounded fetch for the unfiltered cohort. Under `changed_only` /
`kind_filter` -- which drop entries *after* the fetch -- an
adversarially wide window can report `truncated=False` where the
unbounded fetch would have surfaced a later surviving resource; this
is an accepted, bounded tradeoff (the route layer caps tighter
separately and the unfiltered cohort stays exact).

Surfaces:

* `meho topology diff <ts1> <ts2> [--kind ...] [--changed-only] [--json]`
  -- the CLI verb. ts1 / ts2 accept duration shorthand (24h / 7d /
  30m / 2w) resolved client-side or RFC3339 / ISO-8601 absolute.
  Default output is a structured summary
  (`node  created=N  updated=M  removed=K` per side, total + truncation
  banner); `--json` carries the full `TopologyDiffResult`.
* `GET /api/v1/topology/diff?ts1=...&ts2=...` -- the REST route.
  `op_class="audit_query"` per the G9 audit-query convention
  (broadcast event carries only aggregate counts, never per-entry
  payload).
* `query_topology(kind="diff", ts1=..., ts2=...)` -- the MCP facet on
  the parametric `query_topology` meta-tool.

## REST API surface (T5, #453 + G9.2-T5 #597 + G9.3-T5 #861 + G9.3-T4 #860 + G9.3-T3 #859)

`backend/src/meho_backplane/api/v1/topology.py` is the HTTP front for
the read + write halves. Thirteen routes total — twelve on the topology
router, one on the targets router:

| Method + path | Wraps | op_id | RBAC |
|---|---|---|---|
| `GET /api/v1/topology/dependents/{name}` | `find_dependents` | `topology.dependents` | operator |
| `GET /api/v1/topology/dependencies/{name}` | `find_dependencies` | `topology.dependencies` | operator |
| `GET /api/v1/topology/path?from=A&to=B` | `find_path` | `topology.path` | operator |
| `POST /api/v1/topology/refresh/{target_name}` | `refresh_target_topology` | `topology.refresh` | operator |
| `POST /api/v1/topology/edges` | `annotate_edge` (T3 #595) | `topology.annotate` | **tenant_admin** |
| `DELETE /api/v1/topology/edges/{edge_id}` | `unannotate_edge` (T3 #595) | `topology.unannotate` | **tenant_admin** |
| `DELETE /api/v1/topology/nodes/{node_id}` | `delete_node` (#2485) | `topology.delete_node` | **tenant_admin** |
| `GET /api/v1/topology/edges` | `list_edges` (T4 #596) | `topology.list_edges` | operator |
| `POST /api/v1/topology/edges/bulk` | `bulk_import_edges` (T8 #600) | `topology.bulk_import` | **tenant_admin** |
| `GET /api/v1/topology/timeline` | `query_timeline` (G9.3-T5 #861) | `topology.timeline` | operator |
| `GET /api/v1/topology/diff` | `query_diff` (G9.3-T4 #860) | `topology.diff` | operator |
| `GET /api/v1/topology/history/{name}` | `query_history` (G9.3-T3 #859) | `topology.history` | operator |
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
- **Untracked anchor (closure verbs).** G0.18-T4 (#1357, RDC #789
  N2). `find_dependents` / `find_dependencies` resolve the anchor
  up front via `resolvers.resolve_node`; a `NodeNotFoundError`
  surfaces as HTTP **404 `node_untracked`** with the requested
  `name` (and `kind` when supplied) in `detail`. Distinct slug
  from the annotate flow's `node_not_found` because the operator
  action diverges: closure → "register / refresh the target or
  annotate the relationship"; annotate → "seed the endpoint via
  `meho.topology.create_node`". The OpenAPI spec declares both
  the 404 and 409 shapes on the `dependents` / `dependencies`
  routes via a shared `_CLOSURE_RESPONSES` constant so the
  generated CLI / SDK pick up both error envelopes. Pre-G0.18-T4
  the routes returned `[]` for an untracked anchor, conflating
  it with the tracked-no-deps case and feeding the
  blast-radius-as-safe-to-delete false-negative the RDC dogfood
  cycle caught.
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
- **`refresh` connector-resolution failures.** #2092. The refresh
  service resolves the target's connector via the *raising*
  `resolve_connector` (unlike the dispatcher / probe surfaces, which
  use the fail-soft `resolve_connector_or_label`). The route maps both
  resolver exceptions to structured JSON: `NoMatchingConnector` (e.g. a
  legacy `product="kubernetes"` slug where the connector registers as
  `k8s`) → HTTP **422 `no_matching_connector`** with the offending
  `product` + the resolver message; `AmbiguousConnectorResolution` →
  HTTP **409 `ambiguous_connector`** with the `(product, version,
  impl_id)` candidate triples so the caller can set
  `target.preferred_impl_id` and retry. Both shapes are declared on the
  route via `_REFRESH_RESPONSES` for the generated CLI / SDK. Pre-#2092
  the exceptions leaked through FastAPI's default handler as a bare
  `500 text/plain "Internal Server Error"`. The sibling silent-no-op
  case (a *resolvable* connector without topology support returning
  all-zero counts) is #2093, tracked separately.
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
  `{"from": {"name": ..., "kind"?}, "kind": <kind-slug>,
    "to": {"name": ..., "kind"?}, "note"?, "evidence_url"?}` —
  `from` / `to` are nested `_EdgeEndpoint` objects (mirrors the
  service-layer `NodeRef` dataclass on the wire). `kind` is typed
  against the `_EdgeKindSlug` `StringConstraints` alias
  (`KIND_SLUG_PATTERN`, 2–63 chars) so Pydantic rejects malformed
  kinds at the boundary with 422 before the service runs; any
  well-formed slug — well-known or novel — passes through. `extra="forbid"` rejects
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

## Bulk import (G9.2-T8 #600)

`backend/src/meho_backplane/topology/bulk_import.py` is the batch
annotation service the CLI verb `meho topology bulk-import <file>` and
the REST endpoint `POST /api/v1/topology/edges/bulk` both call. It is
the operator-facing lever for seeding the consumer's prose
`INVENTORY.md` cross-system graph at onboarding without a 30-verb
shell loop.

**Service entry point (async, write half):**

- `bulk_import_edges(session, operator, rows, *, dry_run=False) -> BulkImportResult`
  — runs a two-pass batch annotation. Pass 1 (validation) resolves
  every row's endpoints + kind inside one `session.begin()` block;
  any row's failure aggregates into a single
  `BulkImportValidationError` carrying every row's diagnostic. Pass 2
  (apply) opens a fresh `session.begin()` block and calls
  `annotate_edge_in_txn` for each row inside it, so the whole batch
  commits or rolls back atomically — no partial apply. Dry-run skips
  pass 2 and returns the per-row plan only. Post-commit publishes one
  broadcast event per row via the shared fail-open `_publish` helper;
  per-row audit rows are written inside the apply transaction by
  `annotate_edge_in_txn`, so the audit + broadcast count matches the
  issue criterion: one audit row per edge + one broadcast event per
  edge.

**Atomicity contract.** The whole batch lives in one transaction.
A validation failure on any row rolls back the entire transaction —
no partial apply. The chosen semantics (the issue body offered two
options — atomic OR partial with explicit failure reporting) are
atomic-only in v0.2 because:

- The consumer's INVENTORY.md is the source of truth: a
  partially-applied batch leaves the operator with no clean re-run
  path other than "diff what landed vs the file, fix the file, rerun";
  an atomic failure surfaces the bad row and lets the operator
  fix-and-retry.
- Bulk import is a v0.2 stretch (Initiative #364 §7); a future
  widening to a `--continue-on-error` mode is back-compat and can ship
  in a follow-up.
- The per-row audit + broadcast events still fire — one per applied
  row — so a successful batch is indistinguishable from N single
  annotates at the audit / event level.

**Refactor.** `annotate.py` was extracted into an in-transaction
helper `annotate_edge_in_txn` that returns an `AnnotatePlan`; the
single-edge `annotate_edge` is now a thin wrapper that opens
`session.begin()`, calls the helper, then publishes one broadcast
event after commit. `bulk_import_edges` calls the same helper N times
inside one shared transaction. Backwards compat is preserved — every
existing caller of `annotate_edge` sees the same shape, audit row,
and broadcast event.

**Wire shape.** The REST body is
`{"edges": [{"from", "kind", "to", "note"?, "evidence_url"?}, ...], "dry_run"?: bool}`.
`from` / `to` accept the same `_EdgeEndpoint` `{name, kind?}` shape
the single-edge endpoint uses; `kind` is the `_EdgeKindSlug`
`StringConstraints` alias so a malformed kind slug is rejected at the
Pydantic boundary (422) before any service runs (the vocabulary is
open — well-known and novel slugs both pass). `extra="forbid"` rejects typo'd fields at
the boundary. The response is
`{dry_run, created, updated, conflicts, rows: [...]}` where each row
carries `{index, action, edge_id?, from_name, from_kind, to_name,
to_kind, kind, superseded, conflicts}` so the operator (or the CLI's
human-table renderer) can see the §6 conflict markers attached to
each applied edge.

**HTTP boundary guards.** The route caps the batch size at 1000 rows
(`_BULK_IMPORT_MAX_EDGES`); the consumer's INVENTORY.md lists ~30 v0.2
curated edges, so the cap is well above realistic use but low enough
that a stray hostile body cannot hold a single transaction open
indefinitely. The service layer is unbounded by design — the size
guard belongs at the HTTP boundary.

**CLI verb.** `meho topology bulk-import <file> [--dry-run] [--json]`
reads YAML or JSON (yaml.v3's parser is a JSON superset for the
shapes we accept, so a single `yaml.Unmarshal` call handles both)
and posts the whole batch. Endpoints accept either a bare scalar
(common case) or a `{name, kind}` map (ambiguity disambiguator) via
the custom `UnmarshalYAML`. The 422 `invalid_bulk` envelope is
rendered as a structured per-row list so the operator can pinpoint
each broken edge in the source file without scanning every row.

**Tests:**
- `backend/tests/test_topology_bulk_import.py` — unit suite on
  `sqlite+aiosqlite` (happy-path, idempotency, dry-run, validation
  failures, §6 conflict classification).
- `backend/tests/test_api_v1_topology.py` (the `bulk_import` section)
  — RBAC + 422 envelopes + apply / dry-run round-trip with the
  service mocked.
- `cli/internal/cmd/topology/bulk_import_test.go` — YAML/JSON parser
  + invalid-bulk renderer + end-to-end against an httptest server.

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
| `meho topology dependents <name>` | `GET /topology/dependents/{n}` | `DEPTH / KIND / NAME / VIA / PARENT` table |
| `meho topology dependencies <name>` | `GET /topology/dependencies/{n}` | same table, mirror direction |
| `meho topology path <from> <to>` | `GET /topology/path?from=&to=` | `kind/name -> … (N hops)` chain |
| `meho topology annotate <from> <kind> <to>` | `POST /topology/edges` | `annotated edge: ...` summary |
| `meho topology unannotate <edge-id>` (or tuple) | `DELETE /topology/edges/{id}` | `deleted edge ...` line |
| `meho topology list-edges` | `GET /topology/edges` | `KIND / SOURCE / FROM / TO / LAST_SEEN` table |
| `meho topology bulk-import <file>` | `POST /topology/edges/bulk` | applied/planned counts + per-row table |
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
- **Tenant boundary surfaces as the not-found / 404 / null
  shape.** A cross-tenant target on `refresh` → resolver 404
  (`unexpected_response`, exit 4, near-misses surfaced). A
  cross-tenant node name on `dependents`/`dependencies` → **404
  `node_untracked`** (G0.18-T4 #1357; `unexpected_response`, exit 4,
  CLI renders "not tracked in the topology graph — run `meho
  topology refresh` or `annotate`"). Distinct from `no_target`
  because the operator action diverges (register the target vs.
  refresh the topology). A cross-tenant endpoint on `path` →
  `null` → the no-path line (exit 0). The CLI never distinguishes
  "exists in another tenant" from "does not exist" — the backend
  already collapses them, and the CLI render preserves that.
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
- `meho_backplane.settings` — `topology_refresh_interval_seconds`,
  `topology_history_retention_days`,
  `topology_history_prune_interval_seconds`,
  `topology_history_prune_enabled` (the G9.3-T6 retention knobs).
- `meho_backplane.metrics` — `TOPOLOGY_REFRESH_TOTAL` counter
  (`outcome` label: `ok` / `error` / `skipped_locked`).
- SQLAlchemy 2.0 `text()` with `:named` binds — same raw-SQL pattern
  `meho_backplane.retrieval.retriever` uses (`CAST(:x AS text) IS NULL
  OR ...` for optional filters, UUIDs passed as `str`). Every read
  statement is a module-level fully-literal `text("...")`; nothing is
  interpolated so the `avoid-sqlalchemy-text` SAST rule does not fire.

## History retention prune (G9.3-T6 #858)

The G9.3-T1 (#856) `graph_node_history` / `graph_edge_history` tables
are append-only by contract — the diff-on-write hook (T2) is the only
INSERT writer, and the application never issues UPDATE or DELETE
statements against them under normal operation. Without bounded
retention these tables grow indefinitely; a 1-h refresh cadence on a
churning tenant adds tens of thousands of rows per week.

`backend/src/meho_backplane/topology/history_retention.py` ships the
physical cleanup: an `asyncio.create_task` loop registered in the
FastAPI lifespan that ticks on `TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS`
(default 604800s / weekly) and deletes rows where `valid_from <
now() - TOPOLOGY_HISTORY_RETENTION_DAYS` (default 90 days). Both
DELETEs ride the migration-0012-declared `(tenant_id, valid_from
DESC)` index in reverse, so even multi-million-row history tables
prune in seconds.

**Same `asyncio.create_task` pattern as the G9.1-T3 topology refresh
scheduler and the G5.2-T1 memory-expiry sweeper.** Issue #858
references APScheduler in its task body as a name; the established
chassis precedent is the in-lifespan `asyncio` loop — zero-dependency,
matches "no new substrate" discipline, and gives one disposal pattern
across every long-lived lifespan-owned task.

**Knobs (`backend/src/meho_backplane/settings.py` + Helm
`deploy/charts/meho/values.yaml` `topology.*`):**

- `TOPOLOGY_HISTORY_RETENTION_DAYS` (default `90`, range `[0, 3650]`).
  Rows older than `now() - days` are deleted per tick. **`0` is the
  "keep forever" opt-out sentinel**: the prune loop still runs but
  every tick is a logged heartbeat with no DELETE and no audit row —
  operators picking `0` accept unbounded disk growth in exchange for
  full historical retention. Quarterly-plus retention with bounded
  growth is the export path: `meho topology timeline --json > out.jsonl`
  to cold storage of choice.
- `TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS` (default `604800` / 7d,
  range `[60, 604800]`). Sleep between ticks. Below one minute
  competes with normal write load; weekly cadence is the documented
  ceiling — slower pruning is expressed by raising
  `TOPOLOGY_HISTORY_RETENTION_DAYS`.
- `TOPOLOGY_HISTORY_PRUNE_ENABLED` (default `true`). Skips starting
  the loop entirely in the lifespan when `false` — distinct from
  `RETENTION_DAYS=0`'s heartbeat-only shape. Use `false` only when an
  external retention mechanism (k8s CronJob, archive-then-delete via
  cold storage) reaps rows instead.

**Audit-row shape (one row per non-no-op tick).** Channel `INTERNAL`,
path `topology.history.prune`, operator_sub
`system:topology-history-retention`, tenant_id the per-deploy sentinel
`00000000-0000-0000-0000-0000000858a1` (system-wide ops, not
attributable to a real tenant — `audit_log.tenant_id` is a soft-FK so
the sentinel value writes without a matching `tenant` row). Payload:
`{"dropped_node_rows": N, "dropped_edge_rows": M, "retention_days":
D, "cutoff": <iso-ts>}`. The no-op case (`RETENTION_DAYS=0`) writes
no audit row — weekly empty-payload rows would flood `audit_log` for
no operator value. The G8.2 audit-query verb picks this up via
`meho audit query --op topology.history.prune`.

**Failure isolation.** Per-tick `try` / `except` so a transient DB
blip logs `topology_history_retention_tick_failed` and the loop
continues to the next cadence. Audit-write failures after the DELETE
are caught locally and logged loud-but-non-fatal — the rows are
already gone; surfacing the audit failure is the closest we get to
two-phase commit consistency.

**No per-pod leader election.** Initiative #374 defers leader
election to v0.2.next. Two replicas racing on the same prune tick
issue two identical bounded DELETEs in the same second; the second
one hits zero rows. Below the noise floor of normal DB load — same
calculus the memory-expiry sweeper documents for its own no-leader
shape.

## Known issues / out of scope

- **PostgreSQL-only read path.** The `WITH RECURSIVE ... CYCLE` clause
  is not implemented by SQLite, so the query verbs cannot run on the
  unit suite's per-test SQLite DB. Tests live in
  `backend/tests/integration/test_topology_query.py` (plus the
  dense-mesh pruning/worst-case suite in
  `backend/tests/integration/test_topology_path_pruning.py` and the
  refresh-vs-annotate concurrency suite in
  `backend/tests/integration/test_topology_concurrency.py`) against a
  real `pgvector/pgvector:pg16` testcontainer (Docker-gated skip on
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
- Soft-delete is retention-first for the traversal verbs —
  `find_dependents` / `find_dependencies` / `find_path` keep
  soft-deleted rows reachable **by default** (last-refresh-wins), with
  a per-query `include_stale=False` opt-out since #2538 (see
  §Staleness opt-out above). Only the list verbs (`list_edges` /
  `list_nodes`) exclude soft-deleted rows by default. Point-in-time
  visibility ("when did this disappear?") is answered by the dedicated
  history/diff/timeline verbs (G9.3,
  [#365](https://github.com/evoila/meho/issues/365)) over the retained
  rows.
  See `docs/architecture/topology.md` §Soft-delete semantics; pinned by
  `test_scenario4_soft_delete_retains_row` and the #2538
  `include_stale` toggle tests in `test_topology_query.py`.
- Per-connector `discover_topology` overrides — each G3.x Initiative.
- The advisory lock is a multi-replica stampede guard only; a single
  process serialises naturally and the SQLite test path no-ops it.
  The real-PG lock path (skip while held, proceed after release) is
  pinned by
  `test_scheduler_advisory_lock_skips_and_releases_on_real_pg` in
  `backend/tests/integration/test_topology_concurrency.py`.

## References

- Parent Initiative: G9.1 #363; prerequisites #448 / #449.
- Write half: G9.1-T3 #450. Read half: G9.1-T4 #451.
- Prerequisite schema: G9.1-T1 #448 (migration `0007`).
- Audit/broadcast pattern mirrored from
  `backend/src/meho_backplane/operations/_audit.py`.
- PostgreSQL recursive CTE + `CYCLE`:
  <https://www.postgresql.org/docs/17/queries-with.html> §7.8.2.2
  (identical in PG 16, the chassis floor).
