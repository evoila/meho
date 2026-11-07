<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Topology onboarding — operator recipe for a new tenant

> Operator-facing runbook for turning on the MEHO topology graph for a
> tenant: enabling scheduled refresh, verifying `dependents` against a
> known target, and what performance to expect. The implementation
> lives in
> [`backend/src/meho_backplane/topology/`](../../backend/src/meho_backplane/topology/);
> the architecture rationale is in
> [docs/architecture/topology.md](../architecture/topology.md). This doc
> is the surface a tenant operator reads when they want
> `meho topology dependents <resource>` to return real data.

## What you get

Once topology is on for your tenant, every governed target's
infrastructure (VMs, hosts, datastores, pods, namespaces, mounts,
network paths, …) is kept in a per-tenant graph you can query before a
destructive op:

- `meho topology dependents <name>` — what depends on this resource
  (the blast radius).
- `meho topology dependencies <name>` — what this resource depends on.
- `meho topology path <from> <to>` — is there a route between these
  two, and what is it.

Everything is tenant-scoped: you only ever see your own tenant's graph,
and another tenant never sees yours — even when target names overlap.

## 1. Enable scheduled refresh

Scheduled refresh is on by default once the backplane is deployed — the
lifespan starts a background loop that sweeps **every tenant's targets**
on a cadence. No per-tenant opt-in flag exists; a tenant's targets are
refreshed as soon as they are registered.

Tune the cadence with the `TOPOLOGY_REFRESH_INTERVAL_SECONDS` env var
on the backplane deployment (default `3600` — one hour). Lower it for
faster drift detection at the cost of more vendor-API discovery load;
raise it for quieter, slower-moving inventories. Two backplane replicas
sharing one Postgres are safe: each `(tenant, target)` refresh takes a
PG advisory lock, so they never stampede the same target, and a target
whose connector keeps failing is put on exponential backoff (capped at
4 h) rather than retried every cadence.

To seed the graph immediately rather than wait for the next sweep, run
an on-demand refresh per target:

```
meho topology refresh <target-name>
```

This resolves the target's connector, calls its topology probe, and
reconciles the snapshot into the graph in one transaction. It prints
the per-class diff (`nodes: +A -R ~U` / `edges: +A -R ~U`) and the
wall-clock. Re-running it is idempotent — unchanged rows just refresh
their `last_seen`.

> A connector only contributes topology if it implements
> `discover_topology`. In this repository only the Kubernetes connector
> does: it reports the cluster, its namespaces and its nodes, linked by
> `belongs-to` edges. A connector that doesn't yields an empty snapshot:
> the refresh succeeds with all counts zero and prints a `note:` line
> that names the product and lists the products that do discover
> topology. For other systems, add edges by hand with
> `meho topology annotate` (see below). To find connectors that can reach more of your estate,
> `meho targets discover <product>` lists candidate targets a connector
> sees but you haven't registered yet (it does **not** auto-create
> them — review, then `meho targets import`).

## 2. Verify `dependents` against a known target

Pick a target you registered and refreshed, then walk its reverse
closure:

```
meho topology refresh kube-prod
meho topology dependents kube-prod
```

You should get a depth-ordered table: the anchor at row 0 (depth 0),
its direct dependents at depth 1, transitive ones deeper. For a
Kubernetes target, expect its namespaces and nodes to appear, connected
by `belongs-to` edges. Edges added by hand (`runs-on`, `depends-on`, …)
show up in the same walk.

Useful checks:

- **Tenant isolation.** Ask another tenant's operator to run the same
  command against a same-named target — each of you gets only your own
  graph; neither leaks into the other.
- **Ambiguity.** If a bare name resolves to more than one node *kind*
  in your tenant the backplane returns a 409 naming the kinds; re-run
  with `--node-kind <one of them>`.
- **Scope a walk.** `--kind <edge_kind>` restricts the traversal to one
  edge kind (e.g. `--kind runs-on`); `--depth N` caps the walk
  (`1..64`, default 16). `meho topology path <from> <to> [--max-hops N]`
  (`1..32`, default 8) returns the shortest route or nothing if
  unreachable.
- **Live rows only.** The walks include soft-deleted (stale) rows by
  default. Add `--include-stale=false` to see only rows the last
  refresh still found.
- **Machine output.** Add `--json` to any verb for stable structured
  output to pipe into other tooling.

For `dependents` and `dependencies`, a name that is not in this
tenant's graph returns HTTP 404 `node_untracked` — distinguishable from
"exists but has no dependents", which returns just the anchor row.

## 3. Performance expectations

These are **documented expectations on the test fixture, not an
enforced SLO** (the >10k-node case is a v0.3 concern). The recursive
traversal is depth-capped at 16 by default.

| Operation | Expectation on a ~10k-node / ~10k-edge tenant graph |
|---|---|
| `topology dependents` depth 16 | < 100 ms |
| `topology path` (BFS) | < 150 ms |
| `topology refresh` (one target; insert/update bottleneck) | < 500 ms |

The 10k-node figure comes from the parametric acceptance fixture
`backend/tests/fixtures/topology_10k_nodes.py`, exercised by
`backend/tests/integration/test_topology_g91_acceptance.py` against a
real Postgres in CI. If your tenant's traversals are materially slower
than this, the likely cause is a missing/disabled traversal index on
`graph_edge` — file an issue with the tenant size and the slow verb.

## What's next

- **Curated edges (G9.2, [#364](https://github.com/evoila/meho/issues/364)).**
  Auto-discovery only emits the four high-confidence edge kinds.
  Cross-system relationships (`authenticates-via`, `depends-on`,
  `replicates-to`, `backed-up-by`, `routes-via`, `policy-binds`)
  need explicit operator assertion via `meho topology annotate`
  (tenant-admin). The full curated-edge surface (CLI verbs, REST
  routes, MCP tools, §6 conflict-resolution rules) is documented in
  [topology-annotation.md](./topology-annotation.md).
- **History (G9.3, [#365](https://github.com/evoila/meho/issues/365)).**
  A refresh that no longer sees a node soft-deletes it (`last_seen`
  cleared, the row is retained — never SQL-deleted). By default a
  soft-deleted node is still reachable by the traversal verbs
  (`dependents` / `dependencies` / `path`); pass `--include-stale=false`
  (REST `include_stale=false`) to drop stale rows from the walk. Treat
  the graph as last-refresh-wins: a stale edge persists until the next
  successful refresh of its owning target re-derives the snapshot. G9.3
  (shipped) adds the *separate* `meho topology history|diff|timeline`
  point-in-time verbs over the retained rows to query when a resource
  appeared or disappeared. A weekly clean-up deletes topology history
  older than 90 days (chart value `topology.historyRetentionDays`).
