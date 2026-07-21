# The Topology feature, judged against its purpose

> **Engineering review · G9 Topology**
> Analysis by Damir Topić & Claude · 2026-07-15 · evoila/meho @ `1be8cac3` (v0.22.0)
> Every claim carries a file:line citation and survived adversarial re-verification.

---

## Verdict

**A well-built substrate pointed slightly off-target.** The thesis this review measures against: topology exists so the agent (or humans) can trace dependencies through and across systems — DNS → K8s → VMware → physical hardware — and recognising and adding those nodes and edges is hard, so it is the *agent's* responsibility, with human approval.

The mechanics that thesis needs are genuinely excellent: traversal is completely node-kind-agnostic (one recursive CTE crosses `vm`/`host`/`vault-role`/`namespace` in a single cycle-safe, tenant-hard-scoped statement), six curated edge kinds exist precisely because probes cannot infer cross-system links, and every mutation gets same-transaction audit plus full before/after history. **The query layer would execute the canonical trace today — if the data could exist.** Three deliberate design choices prevent the data from existing in the thesis's shape: a closed node vocabulary the agent cannot extend, no identity-resolution substrate to stitch one machine's identities across systems, and an explicitly rejected approval loop. A fourth problem is empirical: auto-discovery shipped for exactly one connector, inverting the designed 70/30 auto/curated split.

### The canonical trace today

```
 ✗ dns-record ──(no kind)──▶ service ──routes-through──▶ pod ──runs-on──▶ node
                                                                            │
                                                                        runs-on*
                                                                            ▼
                          ✗ chassis/rack ◀──(no kind)── host ◀──runs-on*── vm
```

Ends marked ✗ are unrepresentable — no node kind exists for them. Middle hops are representable, but every edge marked `*` must be hand-curated: no probe emits it, and nothing tells anyone that k8s node `worker-1` *is* VMware VM `worker-1.corp.local`.

---

## What the feature is

Two live tables (`graph_node`, `graph_edge`, migration 0007) plus append-only history twins (0012). Nodes are keyed `(tenant_id, kind, name)`; edges are directed, typed by a closed 10-kind vocabulary (4 auto-discoverable, 6 curated cross-system), unique per `(tenant, from, to, kind)`. Traversal is PostgreSQL `WITH RECURSIVE … CYCLE` — dependents, dependencies, shortest path — capped at depth 16/64. Refresh diffs connector `discover_topology()` snapshots transactionally under advisory locks; curated writes flow through `annotate_edge`/`create_or_get_node` with §6 supersede/conflict arbitration. Four sibling fronts on one substrate: REST (12 routes), Go CLI (11 verbs), HTMX console (graph/table/timeline/diff), and MCP — reads on the operator-role `query_topology` meta-tool, writes on three `tenant_admin`-only admin tools.

---

## What holds up

**STRENGTH — One graph, kind-agnostic traversal — confirmed.**
A single `find_dependents` call crosses `target → vm → host → datastore → vault-role` in one statement; the only kind predicate in the SQL is the optional anchor pin. Cycle-safe (`CYCLE` + depth bound), min-depth DAG dedup, and cross-tenant traversal is structurally impossible — no front accepts a `tenant_id` argument at all.

**STRENGTH — Cross-system edges are first-class, with real human-vs-machine arbitration.**
The six curated-only kinds (`authenticates-via`, `depends-on`, `replicates-to`, `backed-up-by`, `routes-via`, `policy-binds`) exist explicitly because "auto-discovery cannot infer" them — the thesis premise, conceded in the schema from day one. Curated assertions durably supersede auto edges (sticky `superseded_by`, excluded from every traversal leg), incompatible kinds coexist with bidirectional `conflicts_with`, and refresh strips reserved markers from connector hints fail-closed so a hostile probe cannot forge a supersede.

**STRENGTH — The agent is mechanically a first-class author — the *only* author of manual nodes.**
`meho.topology.create_node` exists exclusively on the MCP surface — no REST route, no CLI verb, no UI form. Humans literally cannot seed a manual node. Writes are idempotent on the unique keys, kinds are validated pre-DB with candidate lists, and dangling edges are impossible by construction (both endpoints must resolve).

**STRENGTH — Post-hoc accountability is unusually rigorous.**
Every mutation writes one audit row plus append-only history rows with full `{before, after}` snapshots in the *same transaction*, sharing a pre-allocated `audit_id`; heartbeats are suppressed so the timeline is forensically trustworthy; wrong assertions are one `unannotate` away from clean. ~70 temporal tests pin this. The depth-16 perf flake was engineered away properly (load-invariant ratio gate), not timeout-widened.

---

## Where it fights the thesis

### 1 · CRITICAL GAP — The closed 14-kind node vocabulary makes the canonical trace unrepresentable

Node kinds are a closed tuple enforced by DB CHECK plus pre-write validation (`db/models.py:1673`, `topology/nodes.py:162`): no `dns-record`, no `database`, no `certificate`, nothing below `host` for hardware. Never widened since migration 0007 (only edge kinds were, in 0010). The actor the thesis makes responsible for *recognising new resource classes* is exactly the actor structurally barred from introducing one — it takes a coordinated Alembic migration. Live symptom: three shipped tool descriptions advertise `keycloak-realm` as seedable (`mcp/tools/topology.py:1510`), and the validator rejects it at runtime. An agent that trusts its own tool contract fails.

### 2 · GAP — No identity-resolution substrate across systems

`GraphNode` has no external-id, alias, or same-as mechanism — its full column set is id, tenant, kind, name, target, properties, discovered_by, timestamps. If k8s sees node `worker-1` and a future vSphere populator sees VM `worker-1.corp.local`, nothing links or even suggests they are one machine; name-keyed rows silently split one physical box into unrelated nodes, breaking the trace at precisely the cross-system seams the feature exists for. The agent must discover every correlation itself and assert it as an edge — with no substrate help and no suggestion pipeline.

### 3 · CRITICAL GAP — The "agent proposes, human approves" loop is absent — by explicit decision, using a coarser mechanism than the codebase already owns

Every topology write commits immediately. No approval machinery exists anywhere in the package (grep for `approval|policy_gate` over topology, MCP and API layers: zero hits) — while dangerous connector ops get the full mold: `requires_approval` → `policy_gate` → durable `ApprovalRequest` queue with params-hash anti-substitution and resume (`operations/_validate.py:115`). That substrate even *floors AGENT principals to needs-approval* per `principal_kind` (`operations/_validate.py:208-227`) — and topology never consults `principal_kind` at all. So an agent is either read-only (operator token) or ungated-write (tenant_admin token); the middle the thesis calls for doesn't exist.

Issue #364 rejected an annotation approval queue explicitly, deferring it to a v0.2.next policy engine that never shipped — while justifying the tenant_admin gate with the very threat ("silently shrinks the auto-flagged blast radius of an op they then run") the approval mold was built for. Fairness note: pre-delegated authority plus perfect recoverability *is* a coherent alternative — but only if agent writes are reviewable, and they aren't queryable: authorship rests on string convention (`discovered_by` mixes connector slugs and JWT subs), the annotate audit row omits `actor_sub`, and no shipped surface can answer "what did the agent assert this week?".

### 4 · GAP — Auto-discovery coverage inverted the designed 70/30 split

Goal #220 / decision #6 designed ~70% auto-discovered, ~30% curated. What shipped: exactly one populator — `KubernetesConnector` out of ~25 registered products — emitting only `target`/`namespace`/`node` nodes and `belongs-to` edges. No shipped code path auto-writes `runs-on`, `mounts`, or `routes-through` despite the "auto-discoverable" label; the vSphere and Vault populators specified in #363 were deferred to G3.x and never landed. In practice the graph is majority-curated — the system drifted toward agent/human authorship by coverage failure rather than design, without the approval, provenance, or ergonomic support that posture needs. Blast-radius answers for non-k8s products depend entirely on curation having happened — and were silently wrong (empty list read as "safe to delete") for over a year until #1357.

---

## Defects (verified)

**BUG — Refresh "adoption" silently clobbers agent-curated nodes.**
Curated *edges* are carefully protected from refresh; curated *nodes* are not, because `graph_node` has no `source` column. When any target's probe snapshot contains a matching `(kind, name)`, the reconciler wholesale overwrites the row — `row.properties = dict(hint.properties)`; `row.target_id = target_id` (`topology/refresh.py:364-365`) — destroying agent-recorded rationale and handing lifecycle to that target: its future refreshes can then soft-delete the node. Recovery only via history, within the 90-day retention window.

**BUG — `find_path` materializes every path in both directions — exponential on dense graphs.**
The bidirectional CTE doubles every edge (undirected `bi_edge`), accumulates *all* simple paths up to `max_hops`, and joins the target only in the final `SELECT … ORDER BY hops LIMIT 1` — no target-hit termination, no cross-branch frontier dedup. Any operator-role caller can select `max_hops=32` (`api/v1/topology.py:512`). The only perf fixture is a sparse hub-and-chains forest whose path count is linear — but the production graph, being majority-curated cross-system mesh, is exactly the dense shape where this goes exponential. Never exercised in CI.

**FRICTION — Inventory and blast-radius views disagree about stale rows — indefinitely.**
`list_edges` excludes soft-deleted rows; every traversal verb includes them, with no `exclude_stale` parameter anywhere (`topology/query.py:741` vs `:235-327`). Since graph rows are never hard-deleted, a disappeared node stays in blast-radius output *forever* unless its owning target refreshes clean — and curated edges have no staleness mechanism at all. Two first-class views contradicting each other about the same edge is a trust problem for a feature whose product is trustworthy answers.

**FRICTION — The headline workflow returns a flat list, not a chain — and the agent's write surface has avoidable asymmetries.**
"Show me everything this VM ultimately serves" returns a flat depth-sorted node table carrying only the *last* hop's edge kind — the actual chain needs a tenant-wide `list-edges` join to reconstruct. Closure depth defaults 16 while `path` defaults 8 hops, so a chain dependents surfaces may silently not be found by path. On the write side, the one propose→plan→apply flow that exists — `bulk_import` with `dry_run` returning a per-row plan — is reachable from REST, CLI and UI but has **no MCP tool**: the agent loops single annotate calls with no pre-apply plan. The agent also cannot delete a node (a typo'd `create_node` is permanent from the agent surface; #2485 open), and the MCP annotate response omits which auto edges the write just superseded — the agent removes probe evidence from every blast-radius answer without in-band notice.

---

## What the issue history says

The original intent (Goal #220, closed) matches roughly *half* the thesis. Cross-system blast-radius tracing is there verbatim — "rke2-meho has 3 nodes which run on these ESXi hosts which sit on this NSX segment," with a Done-when requiring a real ticket's plan to be reshaped by a dependents query. But the issues cast the agent as a first-class *reader*; the author of curated edges was the human tenant_admin. Agent authorship arrived as a retrofit (`annotate`/`unannotate` #598, `create_node` #778 — the latter because an MCP-only agent in a fresh tenant literally could not seed the graph). The topology-aware policy engine that was to consume the graph — and to carry the deferred approval flow — has never landed. The thesis is therefore not what was built; it is a *sharper* statement of intent than the issues themselves made, and the gap analysis above is the distance between the two.

---

## Recommendations, ranked by leverage

1. **Wire agent topology writes through the existing approval substrate.** Consult `principal_kind` in `annotate`/`create_node`/`unannotate`; floor AGENT principals to needs-approval, keep human tenant_admin writes immediate. This implements the thesis's loop with a primitive the codebase already owns (`operations/_validate.py:208-227`) — no new substrate, consistent with how every other dangerous surface is gated.
2. **Give `graph_node` a `source` column and refresh-merge discipline for curated nodes**, mirroring `_refresh_curated_edge`. Stops the adoption clobber — the most bug-shaped finding here.
3. **Open the node vocabulary deliberately.** Either widen it (dns-record, dns-zone, database, certificate, appliance, chassis; and settle `keycloak-realm`, which three shipped descriptions already promise) or add a governed `custom:<slug>` namespace. Today the agent's core responsibility is blocked by a migration.
4. **Add an identity seam:** a same-as/alias mechanism (reserved properties key or column) plus resolver support, so a k8s node and a vSphere VM can be asserted — or agent-suggested and human-approved — as one machine. This is the thesis's approval loop applied where it matters most.
5. **Bound `find_path`** (target-hit early termination or tighter ceilings) and add a dense-mesh perf fixture plus refresh-vs-annotate concurrency tests — the untested load profile is the one production will have.
6. **Agent ergonomics parity:** an MCP `bulk_import(dry_run=…)` (the dry-run plan is precisely the agent-shaped propose→review→apply flow), superseded-edge ids in the annotate response, node delete (#2485), and a staleness opt-out on traversal.
7. **Typed provenance:** record `actor_sub`/`principal_kind` on annotate audit rows and expose resolved actors on history/timeline surfaces, so "review the agent's assertions" becomes a query instead of a string-convention join.

---

## Review of the colleagues' review

*Pending — the shared artifact (`claude.ai/code/artifact/9d10bef6-…`) is not accessible from this account ("not found or not shared"). Once it is shared or its content is pasted, a point-by-point comparison against the verified findings above will be added here: which of their claims our evidence confirms, which it corrects, and what each review saw that the other missed.*

---

**Method.** 37-agent review: nine parallel subsystem readers (data model, traversal, mutation surface, discovery/refresh, temporal layer, human surfaces, docs drift, tests, issue history), a three-lens assessment panel (cross-system tracing · agent authorship & approval · operability/scale/UX), then independent adversarial verification of all 25 material findings — 16 confirmed, 9 partially true with corrections woven into this document, 0 refuted. Repo state: evoila/meho `main @ 1be8cac3`.
