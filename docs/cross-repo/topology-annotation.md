<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Curating the topology graph — operator runbook

> Operator-facing recipe for the G9.2 curated-edge surface — the
> `meho topology annotate / unannotate / list-edges` verb tree, the
> tenant-admin MCP meta-tools, the §6 conflict-resolution rules, and
> the rule of thumb for "annotate this" vs "let the probe write it".
> Architecture sits in
> [`docs/architecture/topology.md`](../architecture/topology.md);
> the engineering-facing internals live in
> [`docs/codebase/topology.md`](../codebase/topology.md). This doc is
> the cookbook every operator reads when they need to make the graph
> understand a relationship the probes can't see.

This is **not**
[`kb-migration.md`](./kb-migration.md) or
[`retrieval-retirement.md`](./retrieval-retirement.md) — those cover
moving content into MEHO. This page is about the graph that connects
the resources already in MEHO so the blast-radius answer at
`query_topology { kind: dependents, target: … }` is right before an
operator (or an agent) proposes a destructive op.

## What this surface is

MEHO keeps a per-tenant graph of every resource it governs so a
question like "what depends on `prod-db-1`?" is answered from data
rather than memory. G9.1 (Initiative
[#363](https://github.com/evoila/meho/issues/363)) shipped the
**auto-discoverable** half — every refresh re-derives the `runs-on`
/ `mounts` / `routes-through` / `belongs-to` edges that probes can
infer end-to-end. G9.2 (Initiative
[#364](https://github.com/evoila/meho/issues/364)) adds the
**operator-curated** half: six edge kinds that cross connector
boundaries (a Kubernetes ServiceAccount authenticating against a
Vault role, a service depending on a database in a different
product) and that no single probe can ever see.

The shape is one closed ten-kind vocabulary, two write verbs
(`annotate` / `unannotate`), one listing verb (`list-edges`), and a
deterministic §6 conflict-resolution policy so a wrong assertion is
recoverable in one CLI call.

CLI, REST, and MCP are **sibling fronts** on the one backplane
substrate ([CLAUDE.md](../../CLAUDE.md) "What MEHO is NOT" bullet 2)
— none is a thin wrapper of another. The CLI sections below show the
canonical operator shape; the REST + MCP summaries land further down
for callers wiring their own tooling.

## Prerequisites

- **Role.** Writes (`annotate` / `unannotate`) require `tenant_admin`
  — the policy-layer assertion that this operator has the authority
  to add or remove a graph edge for their tenant. Reads (`list-edges`
  and every traversal verb) require `operator`. A `read_only` caller
  gets HTTP 403 on the REST front; the MCP front returns JSON-RPC
  `-32602` `forbidden`. Why writes are admin-only: a curated edge
  flips through blast-radius checks tenant-wide, so the same role
  gate Vault's tenant-admin secrets get applies here.
- **A running backplane.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb. Override
  per-invocation with `--backplane <url>` when needed.
- **The endpoints exist.** Both endpoint names must already resolve
  to `graph_node` rows in your tenant (typically because a probe
  discovered them on the last
  `meho topology refresh <target>` sweep). An annotation cannot create
  the endpoints — it asserts a relationship between two already-known
  nodes. If either endpoint is missing, the call returns HTTP 404
  `node_not_found` with the unmatched `(kind, name)` pair so you know
  which side to backfill (refresh the owning target or fix the name).

## When to annotate, when not to

The closed vocabulary splits cleanly into two halves and the rule of
thumb follows from the split:

- **Six curated-only kinds** (`authenticates-via`, `depends-on`,
  `replicates-to`, `backed-up-by`, `routes-via`, `policy-binds`) — no
  probe can see them, so the only way they enter the graph is for an
  operator to assert them. These are the canonical use cases for
  `meho topology annotate`.
- **Four auto-discoverable kinds** (`runs-on`, `mounts`,
  `routes-through`, `belongs-to`) — probes write these on every
  refresh. **Do not annotate them.** A duplicate annotation lands as
  a §6 conflict marker (`conflicts_with` on both rows) and clutters
  the inventory survey without semantic gain. The legitimate reason
  to write one of these kinds yourself is the §6 *supersede* flow
  below: the probe wrote a `runs-on` edge to the wrong endpoint and
  you need the curated row to override it until the next probe
  catches up.

The canonical example: a Kubernetes namespace `customer-a-prod`
depends on a Postgres database `prod-db-1` (a `depends-on` curated
edge), and the namespace's ServiceAccount authenticates against a
Vault role `prod-app-read` (an `authenticates-via` curated edge).
Neither relationship appears in any probe's output:

- The Kubernetes connector sees pods, services, ingresses,
  namespaces — never the upstream Postgres.
- The Vault connector sees roles, mounts, policies — never which
  Kubernetes namespace consumes them.
- The Postgres / vSphere / RDC connectors see their own local view.

Once both edges are annotated, a single
`query_topology { kind: dependents, target: prod-db-1 }` surfaces the
namespace; a single
`query_topology { kind: dependents, target: prod-app-read }` surfaces
both the namespace and (transitively) the ingress in front of it. The
blast-radius story is *the* reason to annotate — without those two
edges, the agent recommending "delete `prod-app-read`" sees no
dependents and proposes the destructive op.

The anti-pattern: do not annotate `pod-x runs-on node-y` because the
Kubernetes probe already writes that edge on every refresh. If your
intent is "I want the graph to record the runtime placement", just
run `meho topology refresh <k8s-target>`. If your intent is "the
probe wrote the wrong host", that's the §6 supersede flow below.

## The closed 10-kind vocabulary

The vocabulary is closed. Widening it is a coordinated DB + model +
decision-row change (a new Alembic migration, a new
[`GraphEdgeKind`](../../backend/src/meho_backplane/db/models.py) enum
member, and a row in
[`docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md))
so the v0.2.next policy-engine grammar parsing `kind` stays portable
across tenants. The same table is rendered by
`meho topology annotate --help` and pinned into the MCP tool's
`inputSchema` enum, so an operator can never spell a kind the backend
won't accept.

The descriptions below match `meho topology annotate --help` verbatim
— if you grep the CLI source
([`cli/internal/cmd/topology/annotate.go`](../../cli/internal/cmd/topology/annotate.go)
`edgeKindVocabulary`), you'll see the same strings. They are the same
strings the MCP `inputSchema` rejects unknown kinds against.

| Kind                 | Source       | Description (verbatim from `--help`)                                                       |
| -------------------- | ------------ | ------------------------------------------------------------------------------------------ |
| `runs-on`            | auto         | vm runs-on host, pod runs-on node (physical/scheduling host)                               |
| `mounts`             | auto         | vm mounts datastore, pod mounts volume (storage attachment)                                |
| `routes-through`     | auto         | ingress routes-through service, service routes-through pod (network)                       |
| `belongs-to`         | auto         | pod belongs-to namespace, vm belongs-to host (logical group membership)                    |
| `authenticates-via`  | curated      | principal -> identity-provider (e.g. k8s-sa-foo -> vault-role-bar)                         |
| `depends-on`         | curated      | cross-system functional dependency (service-X -> database-Y)                               |
| `replicates-to`      | curated      | operator-asserted replication between storage / database nodes                             |
| `backed-up-by`       | curated      | operator-asserted backup relationship                                                      |
| `routes-via`         | curated      | operator-asserted network path through an intermediary (vm-A -> firewall-X -> vm-B)        |
| `policy-binds`       | curated      | RBAC / policy attachment across connector boundaries (k8s-ns -> vault-policy)              |

Rule of thumb in one line: a row marked **auto** is written by a
probe — *do not annotate*. A row marked **curated** is invisible to
probes — annotate it when the relationship exists.

If a kind isn't in the table, the backend will refuse the call at the
HTTP boundary with **422** and the candidate kinds echoed in
`detail.kinds` — no need to grep the source to find the spelling.

## CLI walkthrough

The three verbs land under `meho topology …`. Every verb prints a
human-readable summary by default and accepts `--json` to emit the
raw `TopologyEdge` envelope (annotate / list-edges) or the `204
No Content` analogue (unannotate) for piping into `jq`.

### `meho topology annotate` — assert a curated edge

The argument order is `meho topology annotate <from> <kind> <to>`:

```bash
meho topology annotate customer-a-prod depends-on prod-db-1 \
    --note "consumer-a moved to its own database on 2026-04-17" \
    --evidence-url "https://internal.evoila/runbooks/cust-a-db-migration"
```

Server-side this is **idempotent on `(from, kind, to)`** — a repeat
call refreshes `last_seen` and replaces `properties.note` /
`properties.evidence_url`, never errors with a unique-constraint
violation. That makes the verb safe to script: a configuration
reconciler can issue the same annotate every run and the graph
converges without drift.

Both endpoints are resolved tenant-scoped against `graph_node.name`.
When a bare name resolves to multiple kinds in the tenant (you have a
`vm` *and* a `service` both named `app`), pass `--from-kind` and/or
`--to-kind` to pin the resolution:

```bash
meho topology annotate \
    app depends-on prod-db-1 \
    --from-kind service \
    --note "explicit kind pin because 'app' also names a vm in this tenant"
```

The `--note` flag stores free-form prose on
`graph_edge.properties.note` (max 2000 chars). The `--evidence-url`
flag stores a URL on `graph_edge.properties.evidence_url`. The
canonical use of `evidence_url` is a runbook / INVENTORY anchor; the
G9.3 history surface (Initiative
[#365](https://github.com/evoila/meho/issues/365)) threads these
through into the per-edge timeline so "why does this edge exist?" is
answerable months later. **Recommend always passing `--evidence-url`
on a curated edge** — a graph entry whose justification is forgotten
is debt every operator after you pays interest on.

### `meho topology unannotate` — revoke a curated edge

Two selector forms — pick whichever is shorter at the call site:

```bash
# By edge id (the form list-edges --json emits):
meho topology unannotate 7f3e1a-…-curated-edge-uuid

# By (from, kind, to) triple — the same shape annotate accepts:
meho topology unannotate customer-a-prod depends-on prod-db-1
```

The triple form is client-side: the CLI issues a `GET /edges` to
resolve the unique curated row, then `DELETE /edges/{id}` on its
UUID. Two consequences fall out:

- The triple form returns **404** if no curated edge matches (an
  auto edge of the same `(from, kind, to)` is *not* a match — auto
  edges are not deletable; see below).
- A successful unannotate clears any §6 supersede / conflict markers
  the curated row had stamped on its neighbours, so an auto edge it
  had marked `superseded_by` un-supersedes on the next refresh
  (Initiative #364 §6 recoverability invariant).

**Auto edges refuse deletion.** A `DELETE` against a `source='auto'`
row returns HTTP **409 `auto_edge_deletion`** with the message:

> graph_edge has source='auto'; auto edges resurrect on the next
> refresh, so manual deletion is a no-op. Annotate over the auto
> edge first, then unannotate the curated row.

The CLI surfaces that message verbatim. The reasoning: an auto edge
is the probe's current view of reality. If you delete it, the next
refresh will re-derive it. The recoverable remediation is to assert
the *correct* curated edge of the same kind (which supersedes the
wrong auto edge by §6 rule 1, below) and only later unannotate when
the probe input is fixed and the auto edge is no longer being
re-discovered.

### `meho topology list-edges` — inventory + filtered surveys

```bash
# Every edge in the tenant (default 200 rows; --limit up to 1000):
meho topology list-edges

# Curated edges only — the operator-asserted inventory:
meho topology list-edges --source curated

# Auto edges of a specific kind from a specific endpoint:
meho topology list-edges --source auto --kind runs-on --from prod-db-1

# Conflicts that need operator review (the §6 surfacing query):
meho topology list-edges --conflicts
```

The default human render is an aligned table —
`KIND / SOURCE / FROM / TO / LAST_SEEN` — sorted `(last_seen DESC
NULLS LAST, id)`. The `--json` mode emits the raw `TopologyEdge`
envelope so consumers can pipe `id` values into `unannotate`.

`--conflicts` filters to edges carrying a non-empty
`properties.conflicts_with` marker. That is the **canonical
recoverability query** when something looks wrong: it surfaces every
auto edge that a curated row has superseded *and* every pair of
incompatible-kind edges that coexist over the same endpoint pair.
Pair with `--source curated` to narrow to "annotations I wrote that
the probe disagrees with"; pair with `--source auto` to narrow to
"probes that the curated overrides contradict".

`--from` / `--to` filter on endpoint **name** (not id). A bare name
that resolves to multiple kinds in the tenant returns HTTP 409
`ambiguous_node` — same shape every topology verb uses; the CLI
prints a one-line diagnostic. There is no `--from-kind` /
`--to-kind` on `list-edges`; if a name is ambiguous, narrow the
listing by `--kind` first or use `--json` and filter client-side.

## §6 conflict-resolution rules

The annotation surface has two recoverable conflict shapes the
substrate handles automatically — Initiative #364 §6. Understanding
them is the difference between "this annotation looks wrong, but the
graph is right" and "I need to call ops".

### Rule 1 — same kind, different endpoint → curated wins (sticky)

Scenario: the probe wrote `pod-1 runs-on node-a`, but the pod was
since migrated to `node-b` and the next refresh hasn't caught up. You
need the blast-radius queries to reflect reality *now*.

```bash
meho topology annotate pod-1 runs-on node-b \
    --note "post-migration override; node-a is decommissioned"
```

What the substrate does (in one transaction):

1. Resolves both endpoints tenant-scoped.
2. Upserts the curated `(pod-1, runs-on, node-b)` row with
   `source='curated'`.
3. Finds every auto edge from `pod-1` of kind `runs-on` to a
   *different* `to_node_id` (here: the `pod-1 runs-on node-a` row)
   and stamps it `properties.superseded_by = <curated-id>`.
4. Writes one `audit_log` row (`op_id='topology.annotate'`,
   `op_class='write'`) and one `BroadcastEvent` with the
   superseded-edge ids in the payload.

After commit, the traversal verbs (`find_dependents`,
`find_dependencies`, `find_path`) filter out the superseded auto
edge via the
`properties->>'superseded_by' IS NULL` guard, so blast-radius
queries return the post-migration topology. The supersede mark is
**sticky** across refresh: the refresh service preserves it even
when the probe re-discovers the auto edge, so the override survives
intermittent probe drift.

**Recovery.** When the probe catches up and writes the correct edge
on its own:

```bash
meho topology unannotate pod-1 runs-on node-b
```

The curated row is removed; the auto edge un-supersedes; the graph
is back on probe-discovered ground truth.

### Rule 2 — incompatible kinds, same endpoint pair → coexist with `conflicts_with`

Scenario: the probe wrote `service-X runs-on node-y`, and an
operator annotates `service-X depends-on node-y`. Both rows are
valid expressions of the relationship; they're different *kinds* of
relationship.

What the substrate does:

1. Upserts the curated `(service-X, depends-on, node-y)` row.
2. Finds every existing edge (auto or curated) over the same
   `(from_node_id, to_node_id)` pair with a *different* `kind` —
   here, the `runs-on` row.
3. Appends each side's id to the other's
   `properties.conflicts_with` array (bidirectional).
4. Writes the audit + broadcast row with both ids in the payload.

Both rows remain queryable. Traversal verbs include both (the
guard only filters `superseded_by`, not `conflicts_with`). The
downstream policy layer is the consumer that resolves the
contradiction in v0.2.next; the topology layer surfaces it.

**Recovery.** Surface conflicts that need review:

```bash
meho topology list-edges --conflicts
```

The result is one row per edge whose `properties.conflicts_with`
array is non-empty. Cross-reference both sides, decide which is
correct, and either:

- Unannotate the curated row (if the curated assertion was wrong),
  which clears both sides' `conflicts_with` markers; or
- Fix the probe input so the next refresh stops producing the
  conflicting auto edge.

### Sticky-supersede + refresh interaction

Two invariants worth pinning down for the operator running into a
"wait, why is this edge still hidden?" question:

- The supersede mark **persists** across refresh. A curated edge
  marking `runs-on(pod-1 → node-a)` superseded today will keep that
  auto edge superseded after tomorrow's refresh even when the probe
  re-discovers it. The only thing that clears the mark is the
  `unannotate` of the curated row.
- The conflict mark also persists. A curated `depends-on` marking
  the probe's `runs-on(service-X → node-y)` as `conflicts_with`
  stays on both rows across refresh until the curated row is
  unannotated or the auto edge is no longer rediscovered.

If a blast-radius query "missing" an edge surprises you, the first
diagnostic is `meho topology list-edges --conflicts` — odds are a
curated row is intentionally hiding it.

## Surfaces — REST, MCP, and CLI side-by-side

Same primitives, three operator-facing shapes. Names are
grep-verified against the shipped code in `cli/internal/cmd/topology/`,
`backend/src/meho_backplane/api/v1/topology.py`, and
`backend/src/meho_backplane/mcp/tools/topology.py`.

| Operation        | CLI (operator)                              | REST                                            | MCP                                             |
| ---------------- | ------------------------------------------- | ----------------------------------------------- | ----------------------------------------------- |
| Create / upsert  | `meho topology annotate <from> <kind> <to>` | `POST /api/v1/topology/edges` (tenant_admin)    | `meho.topology.annotate` (tenant_admin)         |
| Revoke           | `meho topology unannotate <id\|triple>`     | `DELETE /api/v1/topology/edges/{edge_id}` (tenant_admin) | `meho.topology.unannotate` (tenant_admin)       |
| List / filter    | `meho topology list-edges [--kind ...]`     | `GET /api/v1/topology/edges` (operator)         | `query_topology { kind: edges, ... }` (operator)|
| Conflict survey  | `meho topology list-edges --conflicts`      | `GET /api/v1/topology/edges?conflicts=true`     | `query_topology { kind: edges, conflicts: true }`|

### REST

The three edge routes live next to G9.1's read + refresh routes on
the `topology` router (`/api/v1/topology` prefix). The full request
shapes:

- `POST /api/v1/topology/edges` — body
  `{"from": {"name": str, "kind"?: str}, "kind": GraphEdgeKind, "to":
  {"name": str, "kind"?: str}, "note"?: str, "evidence_url"?: str}`.
  Returns `201 TopologyEdge` (the typed Pydantic envelope, same
  shape `GET /edges` returns). The body's `kind` is typed against
  the `GraphEdgeKind` enum so an unknown value is rejected with
  **422** before the service runs. **Role:** `tenant_admin`.
- `DELETE /api/v1/topology/edges/{edge_id}` — returns `204 No
  Content` on success; **409 `auto_edge_deletion`** on an auto row
  with the verbatim remediation message; **404** on a missing /
  cross-tenant id. **Role:** `tenant_admin`.
- `GET /api/v1/topology/edges` — query params
  `?kind=&source=&from=&to=&conflicts=&limit=&offset=`. Returns
  `200 [TopologyEdge]` ordered `(last_seen DESC NULLS LAST, id)`.
  Defaults: `limit=200`, hard ceiling `limit=1000` (mirrors the
  substrate cap). **Role:** `operator`.

The `from` / `to` query params on the GET use the `alias` form on
the FastAPI route because `from` is a Python keyword — same pattern
the G9.1 `GET /path` route uses.

### MCP

Three of the four topology MCP tools land for tenant-admin sessions;
the fourth is the read facet on the operator surface.

- `meho.topology.annotate` (tenant_admin) — arguments
  `{from_name, kind, to_name, from_node_kind?, to_node_kind?, note?,
  evidence_url?}`. Returns
  `{edge_id, from: {id, kind, name}, to: {id, kind, name}, kind,
  source, conflicts: [<edge-id>...]}`. The `conflicts` array is the
  §6 rule-2 surfacing on the response shape; superseded auto edges
  are *not* listed on the return shape (inspect them with
  `query_topology { kind: edges, conflicts: true }` if you need to).
- `meho.topology.unannotate` (tenant_admin) — arguments
  `{edge_id}` **or** `{from_name, kind, to_name, from_node_kind?,
  to_node_kind?}`. The two selector forms are mutually exclusive at
  the `inputSchema` `oneOf` layer; a partial triple or both forms
  together is rejected before the handler runs. Returns
  `{edge_id: "<removed-uuid>"}`.
- `query_topology { kind: "edges", ... }` (operator) — the listing
  facet. Optional filters: `kind_filter`, `source`,
  `from_name` / `to_name`, `conflicts: bool`, `limit`, `offset`.
  Returns `{kind: "edges", edges: [TopologyEdge, ...]}`. The same
  meta-tool serves the three traversal shapes (`dependents`,
  `dependencies`, `path`); the `kind` argument is the discriminator
  per [CLAUDE.md](../../CLAUDE.md) postulate 5's narrow-waist
  guidance.

The admin meta-tools live in the `meho.*` admin namespace
(Initiative #364 §9) — not on the daily ~17 meta-tool agent surface.
An `operator`-role MCP session never sees them in `tools/list`; a
direct `tools/call` is refused at the dispatcher's call-time RBAC
re-check with JSON-RPC `-32602 forbidden`.

The MCP write tools accept `from_node_kind` / `to_node_kind`
disambiguation arguments only when bare names are ambiguous — same
semantics as the CLI's `--from-kind` / `--to-kind` flags and the
REST endpoint object's optional `kind` field.

## Audit trail and broadcast

Every annotate / unannotate writes exactly one `audit_log` row and
publishes exactly one `BroadcastEvent`. The bindings are pinned:

- Annotate: `op_id="topology.annotate"`, `op_class="write"`.
- Unannotate: `op_id="topology.unannotate"`, `op_class="write"`.
- List-edges: `op_id="topology.list_edges"`, `op_class="read"`.

`op_class="write"` is set explicitly because the `.annotate` /
`.unannotate` suffixes are not in
`broadcast.events._WRITE_SUFFIXES` and would otherwise classify as
`other`. The audit-broadcast contract follows the rest of the
chassis — one row written *inside* the same transaction as the
graph mutation (the spec's "no success without a committed audit
row" invariant), one broadcast event published *after* commit
(fail-open: a publish exception is logged, never raised).

The broadcast payload for a write carries: the
`(from, kind, to)` triple, the optional `note` / `evidence_url`
(if you provided them), the resulting edge id, and the §6
`superseded` / `conflicts` arrays so a downstream G7 Slack
subscriber surfacing the action knows the full shape of what
changed. The audit row carries the same payload verbatim on
`audit_log.payload`. Query it with
[`meho audit query --op-id "topology.*" --since 7d`](./audit-query.md)
to see the full annotation history for the tenant.

`target_id` on the audit row + broadcast event is populated when
the `from` endpoint is itself a managed target
(`graph_node.target_id IS NOT NULL`), so a broadcast filtered to a
specific target picks up the annotations made *from* that target's
resources. Inner-graph endpoints (pods, services, vault roles —
nodes that don't map 1:1 to a `targets` row) leave `target_id`
NULL, which is the same convention every other topology op uses.

## Tenant boundary

Every annotate / unannotate / list-edges call is scoped to
`operator.tenant_id`, lifted from the validated JWT — **never** from
a request argument. There is no `tenant_id` parameter on any verb,
route, or MCP tool. Two tenants can own same-named nodes (both have
a `prod-db-1`); each tenant's annotations resolve only their own
rows; a tenant cannot reach into another tenant's graph by spelling
the same name. This is proven by the G9.2-T9 integration tests
([`backend/tests/integration/test_topology_annotate.py`](../../backend/tests/integration/test_topology_annotate.py))
which run two tenants side-by-side and assert no row crosses.

The tenant boundary is also **opaque to the caller** on
unannotate-by-id. A cross-tenant edge id and a missing edge id both
return HTTP 404 `edge_not_found` — the substrate never reveals
"this id exists but belongs to another tenant" because doing so
would let a probing operator distinguish "row never existed" from
"row exists in another tenant".

## Edge-case behaviours worth knowing

- **Auto-edge protection.** A `DELETE` against a `source='auto'`
  edge always refuses with 409 — auto rows resurrect on the next
  refresh, so manual deletion is meaningless. Remediation is the
  §6 supersede flow described above (annotate over with the correct
  edge of the same kind), not the DELETE.
- **Idempotent upsert on writes.** Re-annotating an existing curated
  edge refreshes `last_seen` + `properties` rather than erroring on
  the `(tenant_id, from_node_id, to_node_id, kind)` unique
  constraint. Script reconcilers can safely re-issue every run.
- **Ambiguous endpoint names.** A bare name resolving to multiple
  kinds in the tenant surfaces as HTTP **409 `ambiguous_node`** on
  the REST side and as JSON-RPC `-32602` on MCP. The CLI prints the
  candidate kinds and exits non-zero; pass `--from-kind` /
  `--to-kind` (or `from_node_kind` / `to_node_kind` on MCP) to pin
  the resolution.
- **Self-edges.** Asserting `nodeX kind nodeX` returns **422
  `self_edge`** — every curated kind expresses a relationship
  between two distinct nodes.
- **Unknown kind.** A typo'd kind is rejected at the
  Pydantic / `inputSchema` boundary with HTTP 422 (REST) or
  JSON-RPC `-32602` (MCP); the response echoes the closed candidate
  list so the operator can correct without reading the source.
- **Role gating on writes.** `annotate` / `unannotate` require
  `tenant_admin`. An `operator`-role caller never sees the
  `meho.topology.*` tools in `tools/list` and a direct `tools/call`
  is refused with `-32602 forbidden`; on REST the equivalent is
  HTTP 403. This is policy-layer assertion: a curated edge changes
  blast-radius answers tenant-wide, so the same role gate Vault's
  tenant-admin writes get applies here.
- **`note` / `evidence_url` length caps.** Both fields cap at 2000
  characters on the wire (`max_length=2000` on the REST Pydantic
  model; `maxLength=2048` on the MCP `inputSchema` as a marginal
  safety bound). Use the linked runbook for prose longer than a
  paragraph.

## References

- **Parent Initiative:** G9.2
  [#364](https://github.com/evoila/meho/issues/364) — closed
  10-kind vocabulary + curated-edge surface.
- **Parent Goal:** G9
  [#220](https://github.com/evoila/meho/issues/220) — the topology
  graph + history half of the v0.2 chassis.
- **G9.1 substrate prerequisite:**
  [#363](https://github.com/evoila/meho/issues/363) — adjacency-list
  schema, refresh service, recursive-CTE traversal.
- **G9.2 surface prerequisites:** T5 REST routes
  [#597](https://github.com/evoila/meho/issues/597), T6 CLI verbs
  [#599](https://github.com/evoila/meho/issues/599), T7 MCP tools
  [#598](https://github.com/evoila/meho/issues/598), T3 substrate
  [#595](https://github.com/evoila/meho/issues/595), T4 listing
  substrate [#596](https://github.com/evoila/meho/issues/596).
- **Architecture:**
  [`docs/architecture/topology.md`](../architecture/topology.md).
- **Codebase / engineering details:**
  [`docs/codebase/topology.md`](../codebase/topology.md).
- **Sibling operator doc — audit query:**
  [`docs/cross-repo/audit-query.md`](./audit-query.md).
- **Vocabulary decision row:**
  [`docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md)
  decision #6.
- **CLAUDE.md guidance:** postulate 5 (narrow-waist agent surface),
  "What MEHO is NOT" bullets 1 + 2 (no per-op MCP tools; CLI / REST
  / MCP are sibling fronts).
