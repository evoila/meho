# Query your topology graph

Before you delete a namespace, detach a datastore, or shut down a host,
one question matters more than any other: **what else breaks?** MEHO
answers it from a tenant-scoped **topology graph** — nodes (the
resources it knows about) joined by directed edges (the dependencies
between them). One meta-tool, `query_topology`, walks that graph in
every direction an operator or agent needs: what depends on a resource
(the blast-radius check), what a resource depends on, the route between
two resources, and the graph's change history.

This guide covers the model, how tenant scoping keeps it isolated, the
traversals you will actually run, and how a resource gets *into* the
graph in the first place.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and an authenticated session
      ([Connect clients](../clients/index.md)).
    - Reads (`dependents`, `dependencies`, `path`, `list-edges`,
      `timeline`, `diff`, `history`) need the **operator** role.
      Curated writes (`annotate`, `unannotate`, `bulk-import`,
      `create-node`) need **tenant_admin**.
    - Topology is **Beta** — see the
      [feature-maturity index](../reference/maturity.md#topology) for
      the road to GA. The load-bearing gap today: **automatic
      discovery is Kubernetes-only**, so a freshly registered vCenter
      or Vault target starts *untracked* until you annotate it (see
      [Getting resources into the graph](#getting-resources-into-the-graph)).

## The model

A node is a resource — a `target`, a `vm`, a `host`, a `namespace`, a
`datastore`. An edge is a directed dependency:

> An edge `from --kind--> to` reads **"`from` depends on `to`"**. A
> `vm` `runs-on` a `host` means the vm depends on the host.

That one rule fixes the direction of every traversal:

| You want | Traversal | Reads as |
|---|---|---|
| What breaks if I remove X | **dependents** (reverse) | everything that depends on X |
| What X needs to function | **dependencies** (forward) | everything X depends on |
| Is X connected to Y at all | **path** (undirected) | shortest hop chain, either direction |

Every node carries a `source`: **`auto`** (discovered by a probe) or
**`curated`** (asserted by an operator, or promoted from auto). A
refresh never overwrites a curated node — operator intent outranks the
probe's view. Well-known edge kinds include `runs-on`, `mounts`,
`routes-through`, and `belongs-to`; the vocabulary is open (any
lowercase slug), so connectors and operators can name relationships the
core set doesn't.

## Tenant scoping is structural, not a filter

`query_topology` takes **no tenant argument**. The traversal filters
`graph_node.tenant_id` and `graph_edge.tenant_id` against your JWT's
tenant in both the anchor lookup and every recursive step, so a
cross-tenant read is not "denied" — it is *unrepresentable*. A name
that exists only in another tenant reads exactly like a name that
exists nowhere: the untracked answer, never another tenant's graph.

## The surface at a glance

`query_topology` is **parametric**: one `kind` argument selects the
read shape. There is deliberately no `topology.dependents` tool and no
`list_edges` tool — that would be the per-op-tool anti-pattern. The CLI
splits the same shapes into named verbs.

| Read shape | MCP (`query_topology`) | CLI |
|---|---|---|
| Reverse closure (blast radius) | `{kind: "dependents", target}` | `meho topology dependents <name>` |
| Forward closure | `{kind: "dependencies", target}` | `meho topology dependencies <name>` |
| Shortest path | `{kind: "path", from_name, to_name}` | `meho topology path <from> <to>` |
| Flat edge listing | `{kind: "edges", ...}` | `meho topology list-edges` |
| Change feed (tenant-wide) | `{kind: "timeline", since}` | `meho topology timeline` |
| Net delta (two timestamps) | `{kind: "diff", ts1, ts2}` | `meho topology diff <ts1> <ts2>` |
| Per-resource history | `{kind: "history", target}` | `meho topology history <name>` |

## Blast radius: `dependents`

The one you run before anything destructive. "Is it safe to delete
`customer-a-prod`?"

```bash
meho topology dependents customer-a-prod
```

The anchor itself is **row 0** of the result, depth-ordered outward.
So a one-row answer means *"tracked, and nothing depends on it"* — a
genuine green light — while an empty/untracked answer means the graph
has no signal at all (a very different thing; see the failure table).

The agent surface returns the same walk as a flat list of
`TopologyNode` rows:

```json
{
  "kind": "dependents",
  "nodes": [
    {"id": "…", "kind": "namespace", "name": "customer-a-prod",
     "source": "auto", "depth": 0, "via_edge_kind": null,
     "parent_node_id": null, "via_edge_id": null},
    {"id": "…", "kind": "service", "name": "checkout",
     "source": "auto", "depth": 1, "via_edge_kind": "belongs-to",
     "parent_node_id": "…", "via_edge_id": "…"}
  ]
}
```

Each non-root node carries `parent_node_id` and `via_edge_id` — the
node it hangs off and the exact edge walked — so the whole dependency
**chain** reconstructs from the flat list without a second call. Scope
the walk with `--depth N` (default 16, ceiling 64) and
`--kind <edge_kind>` to follow one relationship kind only.

`dependencies` is the mirror image — same shape, same flags, forward
direction:

```bash
meho topology dependencies checkout --kind runs-on
```

## Reachability: `path`

"Is there any route from this ingress to that datastore?" Path search
is **undirected** (it walks edges both ways) and unweighted, so it
follows connectivity rather than edge orientation:

```bash
meho topology path ingress-web datastore-ssd-01
# ingress/ingress-web -> service/checkout -> vm/app-1 -> host/esx-3 (3 hops)
```

Unreachable within the hop budget (`--max-hops`, default 8, ceiling 32)
is a **valid answer, not an error**:

```bash
meho topology path ingress-web datastore-cold-99
# no path from "ingress-web" to "datastore-cold-99" within the hop budget
```

Over MCP the same result is `{kind: "path", path: <TopologyPath>|null}`
— `null` is the unreachable answer. A `TopologyPath` carries `nodes`
(ordered `from` → `to`) and `total_hops`.

## Surveying and change history

`list-edges` is the flat inventory of relationships — filterable by
`--kind`, `--source curated|auto`, endpoints (`--from` / `--to`), and
`--conflicts` (edges where a curated annotation contradicts a
probe-derived one and needs review):

```bash
meho topology list-edges --source curated
meho topology list-edges --conflicts        # what needs an operator's eye
```

The G9.3 history trio answers "what changed, and when":

```bash
meho topology timeline --since 24h          # tenant-wide change feed
meho topology diff 2026-07-31T09:00:00Z 2026-07-31T11:00:00Z
meho topology history checkout --include-edges
```

`timeline` is cursor-paginated (a non-null `next_cursor` means more
rows); `diff` is the net per-resource delta between two timestamps
(hard-capped at 1000 entries with a "narrow the window" hint); `history`
carries the full `snapshot.before` / `snapshot.after` per row for
forensic "what was the exact state before this change?" questions.

## Getting resources into the graph

A resource has to be *in* the graph before a traversal can find it.
Two ways it gets there:

1. **Automatic discovery (Kubernetes only, today).** Probing a
   Kubernetes target populates its nodes and edges, and a background
   sweep keeps them fresh. Force a refresh on demand:

    ```bash
    meho topology refresh lab-rke2
    # nodes: +42 -0 ~3    edges: +51 -0 ~0    (1240 ms)
    ```

    A refresh against a **non-Kubernetes** target is a no-op *by
    coverage gap* — no populator ships for it yet — and the result
    says so, listing which products do have populators.

2. **Curated annotation (any resource).** For the relationships probes
   cannot infer — a vCenter that `belongs-to` a site, a Vault role a
   service `depends-on` — a tenant_admin asserts the edge by hand:

    ```bash
    meho topology annotate app-1 depends-on vault-role-app
    ```

    On an empty tenant, seed the endpoints first with
    `meho topology create-node` (annotate requires both endpoints to
    exist). Curated edges are idempotent and survive every refresh.

This is why a sensor's investigator correlation, or a blast-radius
check, "degrades to per-sensor findings without topology anchors" on
non-k8s estates: until you annotate them, the graph has no edges to
walk.

!!! warning "An `awaiting_approval` on a topology write is by design"

    Curated writes carry `caution` classification. A **human**
    tenant_admin executes them immediately, but an **agent** principal's
    annotate parks in the approval queue like any other governed write —
    the gate lives in the dispatcher, not the front. See
    [Approvals and break-glass](approvals-and-break-glass.md).

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| CLI: *"`X` is not tracked in the topology graph — run `meho topology refresh` or annotate"* (HTTP 404 `node_untracked`; MCP `{status: "node_untracked", nodes: []}`) | The anchor resolves to no node in your tenant. **Do not read this as "safe to delete"** — the call produced no blast-radius signal at all. Auto-discovery is k8s-only, so every non-k8s target starts here. | `meho topology refresh <target>` (k8s) or `meho topology annotate` the relationships (non-k8s). |
| *"node `X` is ambiguous across kinds: host, vm — re-run with `--node-kind`"* (HTTP 409) | The bare name maps to more than one node kind in the tenant. | Pin the anchor with `--node-kind <kind>` (CLI) / `node_kind` (MCP). Note `--kind` is the *edge* filter, not the anchor pin — it will not clear this. |
| `dependents` returns exactly one row | Not an error — that row is the anchor itself (depth 0). It means "tracked, nothing depends on it." | This is your green light for a destructive op. |
| `path` returns `null` / "no path" | Unreachable within `--max-hops`, a missing endpoint, or a cross-tenant endpoint — all the same answer by design. | Raise `--max-hops`, or check the endpoint names exist (`meho topology list-edges`). |
| `refresh` reports all-zero counts with a "no populator" note | The target's product has no topology populator on this version — the refresh could never change anything. | Curate the edges by hand (`annotate`), or track the populator's issue from the note. |
| `403 insufficient_role` on `annotate` / `unannotate` | Curated writes need **tenant_admin**; your JWT is operator. | Use a tenant_admin session. |

**Next:** [Broadcast — cross-operator awareness](broadcast.md).
