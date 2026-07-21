# Topology v2 — ticket drafts (approval bundle, 2026-07-15)

**FILED 2026-07-15** — Goal #2532 · Initiative #2533 · T1 #2534 · T6 #2535 · T2 #2536 · T3 #2537 · T4 #2538 · T5 #2539. Coordination comments posted on #2485 and #2507. This file is the pre-filing draft record; the issues on `evoila/meho` are canonical.

Filing target: `evoila/meho` (labels + Parent-lines hierarchy, no board).
Bundle: **1 new Goal + 1 new Initiative + 6 Tasks.**

Build order (DAG):

- **Wave 1:** T1 (keystone, **1st migration**), T6 (no deps)
- **Wave 2:** T2 (**2nd migration**, Depends-on T1), T3 (Depends-on T1), T4 (Depends-on T1 + T6)
- **Wave 3:** T5 (Depends-on T3)

Migration discipline: exactly one migration-carrying task per wave (T1 → T2). Migration numbers are
assigned at implementation time (next free head), never pinned in bodies.

---
---

## GOAL (new — needs your approval to create)

**Title:** `Goal: Topology as the shared world-model — any system representable, agent+human co-authored, blast-radius/RCA answers from data`

**Labels:** `goal, enhancement, priority:high, effort:large, infrastructure`

**Body:**

## Goal

**Any resource class from any current or future connector is representable in the topology graph, the agent and humans co-author it — agent assertions approval-gated, human assertions immediate — and the graph answers "what breaks if this goes down?" and "why is this broken?" as chains, not flat lists.**

Goal #220 stood up the substrate (schema, traversal, curated edges, history) and closed completed. The 2026-07-15 deep review of the shipped feature (`topology-feature-review.md`, repo root at time of filing) found the substrate excellent but pointed off its purpose on three axes, each locked by a deliberate v0.2 decision that its own consumer never arrived to justify:

1. **Closed vocabularies** (14 node kinds / 10 edge kinds, DB CHECK + Python + MCP enum, #364/#593) make the canonical cross-system trace — DNS record → K8s service → VM → hypervisor host → physical hardware — unrepresentable at both ends. The recorded rationale ("open vocabulary fragments the policy-engine consumer's grammar") defends a policy engine that has never shipped; no open issue tracks one.
2. **Authorship is role-gated, not approval-gated** (#364 §5: "no approval queue for annotations; tenant-admin writes are immediately authoritative"). An agent principal is either read-only (operator token) or ungated-write (tenant_admin token). MEHO's own approval substrate (policy_gate → ApprovalRequest → approve/resume, G11.7) already floors AGENT principals to needs-approval on gated ops — topology never consults it.
3. **Blast-radius answers are flat node lists** (min-depth + last-hop kind only), traversal silently includes soft-deleted rows forever, and `find_path` enumerates the full reachability ball with an operator-selectable `max_hops=32` — untested on the dense curated meshes production graphs will actually be (auto-discovery is k8s-only: 1 populator across ~25 registered products, inverting #220's designed 70/30 auto/curated split).

## Execution Initiatives

- [ ] #TBD — Topology v2: open kind vocabulary, approval-gated agent authorship, chain-shaped blast-radius answers (filed with this Goal)
- Future (not filed today): non-k8s `discover_topology` populators — the follow-up #1357's out-of-scope section promised and that was never filed; belongs here when scheduled.

## References

- Predecessor Goal: #220 (closed completed 2026-06-22; children #363/#364/#365).
- Review that motivates this Goal: `topology-feature-review.md` (2026-07-15, repo root; 37-agent adversarially-verified).
- Decisions being deliberately revisited: #364 (closed-vocabulary lock + no-approval-queue), #593 (10-kind edge enum).
- Approval substrate reused, not rebuilt: `operations/_validate.py` (policy_gate), `operations/approval_queue.py` (park/approve/resume), G11.7 #1401.

---
---

## INITIATIVE (new — needs your approval to create)

**Title:** `Initiative: Topology v2 — open kind vocabulary, approval-gated agent authorship, chain-shaped blast-radius answers`

**Labels:** `initiative, enhancement, priority:high, effort:large, infrastructure`

**Body:**

Parent goal: #TBD-GOAL

## Summary

Six tasks that repoint the shipped G9 topology substrate at its purpose: one graph an agent or human can use to trace dependencies across systems and reason about blast radius / root cause. Binding design principles for every child task:

- **Dumb substrate, smart agent.** Flexibility is delivered as *open, pattern-validated vocabularies plus documented conventions* — no kind registries, no per-tenant governance tables, no weighting DSL. A novel kind enters the graph through a normal write; for agents that write is approval-gated, so the human sees the new kind at the moment it is proposed.
- **Reuse the approval substrate, don't build a second one.** Agent-gating rides `policy_gate` → `ApprovalRequest` → the existing approve/resume surfaces (REST/CLI/MCP/UI), via the `secret.move` targetless typed-op mold. No new queue, no new endpoints.
- **Humans stay immediate.** tenant_admin writes keep today's zero-friction path on every front. Only AGENT principals park.
- **Chain answers, not flat lists.** Blast-radius output must let the caller reconstruct *which node hangs off which* without N follow-up calls.

## Why now

The 2026-07-15 review (`topology-feature-review.md`) verified 25 material findings against the code. The three that block the feature's purpose — closed vocabularies (unrepresentable cross-system traces), absent approval loop (agent over-trusted or excluded), flat/stale blast-radius answers — are all deliberate v0.2 decisions whose stated justifications (policy-engine grammar portability, v0.2.next policy engine carrying approval) never materialized. Meanwhile the graph is majority-curated in practice (k8s is the only populator), so curated-authorship ergonomics and safety are the load-bearing path, not the edge case.

## Grounding (verified in code, 2026-07-15)

- Closed node vocabulary: `_GRAPH_NODE_KINDS` 14-tuple `db/models.py:1673-1688`, CHECK `ck_graph_node_kind` `models.py:1889-1892`, Python raise `topology/nodes.py:162-163`, MCP inputSchema enum `mcp/tools/topology_create_node.py:60,68`.
- Closed edge vocabulary: `GraphEdgeKind` StrEnum `models.py:1691-1749`, CHECK `models.py:2024-2027`, raise `topology/annotate.py:252-255`, MCP enums `mcp/tools/topology.py:1415,1689`. Widening mold: migration `0010:190-194` (`batch_op.drop_constraint(..., type_="check")` + recreate).
- Live drift the open vocabulary retires: `keycloak-realm` advertised as a seedable kind (`mcp/tools/topology.py:1510`, `topology/resolvers.py:188`) but absent from the enum → rejected at runtime.
- No approval on topology writes: every front calls the service primitives in-process (`mcp/tools/topology.py:1543-1595,1773-1824`; `mcp/tools/topology_create_node.py:153-212`; `api/v1/topology.py:746,818`; `ui/routes/topology/edges.py:42-45`); `policy_gate` is invoked from exactly one seam, `operations/dispatcher.py:1964`. Targetless typed-op precedent: `connectors/secret/ops.py:236-263` (`register_typed_operation`, `target=None` always); `ApprovalRequest.target_id` is nullable ("NULL for tenant-wide ops", `models.py:4296-4298`), and target-NULL resume is proven by `tests/test_api_v1_approvals.py:246`.
- Agent/human distinction exists but is unconsulted by topology: `PrincipalKind` `auth/operator.py:95-124`; agent needs-approval floor `operations/_validate.py:222-226`; `safety_level="caution"` + `requires_approval=False` gates agents only (`auth/permissions.py:127-133,358-363` agent path; `_validate.py:129-138` human default-allow).
- Curated-node fragility: `refresh._update_existing_node` overwrites wholesale (`row.properties = dict(hint.properties)`; `row.target_id = target_id`, `refresh.py:364-366`) because `graph_node` has no `source` column; curated *edges* are protected (`refresh.py:619-659,769-770`).
- Flat closure shape: walk CTE projects only `id, kind, name, properties, depth, via_edge_kind` (`query.py:235-327`); predecessor (`w.id`) and edge id (`e.id`) are in scope in the recursive term but not projected. Soft-deleted rows: excluded by `list_edges` (`query.py:741`) yet included by every traversal verb; param precedent `list_nodes(include_soft_deleted=False)` `query.py:1069`.
- `find_path` cost: recursive term is target-blind (`query.py:543-551`); target filtered only in the final select (`query.py:553-562`). Per-branch pruning is expressible; global early termination is not (PG recursive-term restrictions). Perf fixture (`tests/fixtures/topology_10k_nodes.py:53-172`) generates only a hub-and-chains forest — no dense mesh, no cycles, out-degree exactly 1.

**Grounding corrections the child tasks must respect:**
- `keycloak-realm` drift is one MCP site + one resolver docstring, not three MCP sites.
- Superseded-edge ids already land in the audit payload (`annotate.py:1123`); only the MCP return shape omits them (`mcp/tools/topology.py:1597-1615`).
- The UI graph/overlays do NOT consume the traversal verbs (own ORM BFS, `ui/routes/topology/queries.py:254-326`) — closure-shape changes don't touch the console.

## Child tasks

Build order (DAG; Depends-on lines on each task):

- [ ] #TBD — T1 Open the node/edge kind vocabularies — slug-validated open kinds + documented well-known set (keystone) — **1st migration**
- [ ] #TBD — T6 `find_path` per-branch pruning + dense-mesh perf fixture + refresh-vs-annotate concurrency test — no deps
- [ ] #TBD — T2 `graph_node.source` + curated-node durability under refresh *(Depends-on T1)* — **2nd migration**
- [ ] #TBD — T3 Approval-gated agent authorship — topology writes as targetless typed ops, agent MCP fronts dispatch through the gate *(Depends-on T1)*
- [ ] #TBD — T4 Chain-shaped closure answers (`parent_node_id` + `via_edge_id`) + `include_stale` opt-out on traversal *(Depends-on T1, T6)*
- [ ] #TBD — T5 MCP authoring parity — `meho.topology.bulk_import` with free dry-run + gated apply; superseded ids on annotate return *(Depends-on T3)*

Migration discipline: T1 and T2 each carry one migration and sit in different waves; no other task carries one.

## Definition of done

- [ ] An agent can propose a node of a kind that did not exist yesterday (e.g. `dns-record`) and an edge of a novel kind (e.g. `resolves-to`); the proposal parks, a human approves it from an existing approvals surface, and the trace `dns-record → service → vm → host` resolves in one `find_dependents` call with reconstructable adjacency.
- [ ] A human tenant_admin's annotate/create/unannotate remain immediate on every front (no new friction).
- [ ] A curated node survives a connector refresh with its `note`/`evidence_url` intact and cannot be soft-deleted by a probe.
- [ ] `find_path` on a dense (mesh) fixture completes within a documented, CI-pinned envelope.
- [ ] Docs: `docs/architecture/topology.md` vocabulary + authorship sections rewritten; `docs/codebase/topology.md` updated; the `keycloak-realm` drift is gone (kind now valid).

## Out of scope

- Non-k8s `discover_topology` populators (vSphere/Vault/DNS) — future Initiative under the same Goal; promised by #1357's out-of-scope, never filed.
- Node hard-delete — owned by open #2485 (Related; T2's `source` column changes its deletability heuristic — coordinate, don't absorb).
- Identity *suggestion* pipeline (auto-matching k8s node ↔ VM by MAC/UUID). The open vocabulary makes a curated `same-as` edge expressible and traversable today (T1 documents the convention); machine-suggested matches are a later initiative.
- Visual edge authoring in the console; console changes beyond what T4's shape addition requires (none — the UI has its own BFS).
- Any policy engine consuming edge kinds.

## Dependencies

None hard. Adjacent open work: #2485 (same files: `topology/nodes.py`, MCP registry, `api/v1/topology.py` — serialize merges), #2507 (reads the query surface T4 extends — additive fields only, no break).

## References

- `topology-feature-review.md` (repo root, 2026-07-15) — the verified review this Initiative executes.
- Reversed decisions: #364 §5 + vocabulary lock; #593 (10-kind enum, migration 0010).
- Approval mold: `connectors/secret/ops.py:236-263`; `operations/approval_queue.py:740-833` (`resume_dispatch_after_approval`); tests `tests/test_secret_move_approval.py`, `tests/test_approval_queue.py:1500`, `tests/test_api_v1_approvals.py:246`.
- Open-vocabulary comparables: Backstage well-known relations (open set + "Extending the model", https://backstage.io/docs/features/software-catalog/well-known-relations/); CNCF Cartography per-module open label/relationship space (https://cartography-cncf.github.io/cartography/dev/writing-intel-modules.html); ServiceNow CMDB extensible CI classes (https://www.servicenow.com/docs/r/servicenow-platform/cmdb-ci-class-models/cmdb-ci-class-models.html).

---
---

## T1 (keystone, 1st migration)

**Title:** `Task: Open the node/edge kind vocabularies — slug-validated open kinds, well-known set demoted to convention (drops ck_graph_node_kind / ck_graph_edge_kind)`

**Labels:** `task, enhancement, priority:high, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT

## Summary

Keystone of Initiative #TBD-INIT: replace the closed 14-node-kind / 10-edge-kind vocabularies with open, slug-pattern-validated kinds, keeping the existing members as a *documented well-known set*. After this task, any future connector's resource classes (dns-record, database, certificate, chassis…) and relationships (resolves-to, same-as…) are representable without a migration. Approval-gating of *agent* writes (which is what keeps novel agent-invented kinds human-supervised) is T3's seam, not this task's.

## Current state (verified in code, 2026-07-15, post-v0.22.0)

- `_GRAPH_NODE_KINDS` closed 14-tuple: `backend/src/meho_backplane/db/models.py:1673-1688`; DB CHECK `ck_graph_node_kind` `models.py:1889-1892`; mirrored in migration `0007:184-199,286-289`.
- `GraphEdgeKind` StrEnum (10 members) `models.py:1691-1749`; derived tuple `models.py:1757`; CHECK `ck_graph_edge_kind` `models.py:2024-2027`. Widening mold: migration `0010:190-194` — `with op.batch_alter_table("graph_edge") as batch_op: batch_op.drop_constraint("ck_graph_edge_kind", type_="check")` + `create_check_constraint`.
- Python validation: `topology/nodes.py:153-163` (`_validate_kind` → `InvalidNodeKindError`); `topology/annotate.py:244-255` (`GraphEdgeKind(kind)` → `InvalidEdgeKindError`).
- MCP inputSchema enums: node kinds `mcp/tools/topology_create_node.py:42,60,68`; edge kinds `mcp/tools/topology.py:1415` (annotate) and `:1689` (unannotate).
- REST boundary: `POST /api/v1/topology/edges` body types `kind` against `GraphEdgeKind` so unknown kinds 422 pre-service (`api/v1/topology.py`, `_EdgeEndpoint`/body model near `:746`).
- Live drift this task retires: `keycloak-realm` advertised (`mcp/tools/topology.py:1510`, `topology/resolvers.py:188`) but not in the enum → `create_node {kind: "keycloak-realm"}` rejected by inputSchema.
- The lock was deliberate (#364/#593): "operators cannot invent kinds on the fly (would fragment the policy-engine consumer's grammar)". That consumer never shipped and no open issue tracks it — the rationale defends nothing today.
- §6 conflict logic, traversal SQL, history, and the console are kind-agnostic already (no member-list dependency outside the sites above).
- `graph_edge.source` CHECK (`auto|curated`, `models.py:2028-2031`) is NOT vocabulary — unchanged.

## Desired state

- One migration (next free head; `batch_alter_table` mold from `0010:190-194`) drops `ck_graph_node_kind` and `ck_graph_edge_kind`, replacing each with a portable minimal shape CHECK: `length(kind) >= 2 AND length(kind) <= 63 AND kind = lower(kind)` (valid on PostgreSQL 16 and the SQLite unit suite; regex CHECKs are not portable). Note the SQLite batch caveat: `batch_alter_table` table-copy does not copy CHECK constraints — re-declare via the documented workaround (https://alembic.sqlalchemy.org/en/latest/batch.html#working-with-constraints; alembic 1.18.5, `uv.lock:143`).
- Full slug validation lives in Python at every write boundary, single-sourced: pattern `^[a-z0-9]+(?:[._-][a-z0-9]+)*$` (2–63 chars). `nodes._validate_kind` and `annotate._validate_kind` validate against the pattern; `InvalidNodeKindError`/`InvalidEdgeKindError` keep their names and error-envelope mappings, message now cites the pattern + the well-known list as suggestions.
- `_GRAPH_NODE_KINDS` → `WELL_KNOWN_NODE_KINDS`; `GraphEdgeKind` retained as the well-known edge set (used for docs/UI hints and wire back-compat of existing rows), no longer enforced as membership.
- Pydantic boundary: `kind` fields become `Annotated[str, StringConstraints(pattern=...)]` (pydantic 2.13.4, https://docs.pydantic.dev/latest/api/types/) on the REST body models; MCP inputSchemas replace `enum` with `pattern` + description enumerating well-known kinds ("prefer a well-known kind when one fits").
- Docs: `docs/architecture/topology.md` vocabulary section rewritten — well-known kinds table stays, "closed" language replaced with the convention (lowercase slug; prefer well-known; novel kinds arrive via normal writes, agent writes approval-gated per T3); document the `same-as` curated-edge convention for cross-system identity stitching. `docs/codebase/topology.md` validation section updated. The `keycloak-realm` examples become valid as-is.
- Empirical anchors for the pattern (open vocabulary + documented well-known core): Backstage catalog relations (well-known set explicitly non-exhaustive, "Extending the model": https://backstage.io/docs/features/software-catalog/well-known-relations/), CNCF Cartography (open per-module Neo4j label/relationship space with style guidance only: https://cartography-cncf.github.io/cartography/dev/writing-intel-modules.html), ServiceNow CMDB (extensible CI class hierarchy: https://www.servicenow.com/docs/r/servicenow-platform/cmdb-ci-class-models/cmdb-ci-class-models.html).

## Acceptance criteria

- [ ] `meho.topology.create_node {kind: "dns-record"}` and `meho.topology.annotate {kind: "resolves-to"}` succeed end-to-end (integration test, pgvector container); `{kind: "DNS Record!"}` and a 64-char kind are rejected with the typed error naming the pattern (unit test, both node + edge paths).
- [ ] All 14 + 10 existing kinds still validate; existing rows survive the migration (stamp-replay idempotency: pin any reconciliation test to this migration's own revision, not `head` — house lesson from 0049/0050/0054/0055).
- [ ] MCP inputSchemas carry `pattern` (no `enum`); `mcp/tools/topology.py:1510`'s `keycloak-realm` example now round-trips (test creates it).
- [ ] OpenAPI snapshot + generated CLI regenerated (`cd cli && make snapshot-openapi && make generate` — `GraphEdgeKind` enum becomes `string` in the wire schema); UI annotate modal offers well-known kinds as a `datalist` (suggestions) with free-text input, not a closed `<select>`.
- [ ] `docs/architecture/topology.md` + `docs/codebase/topology.md` updated as above; `pytest backend/tests -k "topology"` green; ruff + mypy clean on touched files.

## Out of scope

- Approval-gating of agent writes (T3). Node `source` column (T2). Any traversal/output change (T4/T6). Removing `GraphEdgeKind` from the codebase entirely (it remains the well-known set).

## References

- Parent: #TBD-INIT. Mould: migration `0010:190-194`; validation seams `topology/nodes.py:153-163`, `topology/annotate.py:244-255`.
- Alembic 1.18.5 ops (`drop_constraint(type_="check")`): https://alembic.sqlalchemy.org/en/latest/ops.html; SQLite batch CHECK caveat: https://alembic.sqlalchemy.org/en/latest/batch.html#working-with-constraints.
- PostgreSQL 16 ALTER TABLE: https://www.postgresql.org/docs/16/sql-altertable.html.
- Pydantic 2.13.4 `StringConstraints`: https://docs.pydantic.dev/latest/api/types/.
- Reverses: #364 vocabulary lock, #593. Retires drift: `keycloak-realm` (review finding, 2026-07-15).

---
---

## T2 (2nd migration)

**Title:** `Task: graph_node.source column + curated-node durability — refresh must not clobber or soft-delete curated nodes`

**Labels:** `task, bug, priority:high, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1

## Problem

Curated *edges* are protected from connector refresh (`_refresh_curated_edge` bumps `last_seen` only; operator properties never merged — `topology/refresh.py:619-659`, dispatch guard `:769-770`). Curated *nodes* have no equivalent protection because `graph_node` has no `source` column: `_update_existing_node` overwrites wholesale — `row.properties = dict(hint.properties)`; `row.target_id = target_id` (`refresh.py:364-366`) — whenever any target's probe snapshot contains a matching `(kind, name)`. An agent/operator-seeded node loses its `note`/`evidence_url`/`seeded_by` context, is adopted onto the refreshing target, and that target's future refreshes can then soft-delete it (`_soft_remove_node` `refresh.py:381-397`, loop guards `:482-500`). Recovery only via history within the 90-day retention window. Asymmetric, silent, and destructive to exactly the curated cross-system context the graph exists to hold.

## Current state (verified in code, 2026-07-15)

- No `source` on `graph_node` (`db/models.py:1850-1892` column set); edges have it with CHECK (`models.py:1762,2028-2031`).
- Manual seeds are recognizable only by convention: `create_or_get_node` stamps `properties.seeded_by`/`seeded_at` + `discovered_by=operator.sub`, `target_id=None` (`topology/nodes.py`, control flow in `docs/codebase/topology.md` §create_or_get_node).
- Open #2485 (node delete) keys deletability on `target_id IS NULL` precisely because no `source` exists — its body says so. Coordinate: this task lands the column; #2485 should key on it (Related, comment after filing).

## Desired state

- One migration (next free head): `graph_node.source` TEXT NOT NULL DEFAULT `'auto'`, CHECK in (`auto`,`curated`) — mirror `ck_graph_edge_source` (`models.py:2028-2031`, migration `0007:360-363` mold). Backfill: `source='curated'` where `properties ? 'seeded_by'` (the manual-seed stamp), else `'auto'`.
- `create_or_get_node` writes `source='curated'`; re-seed over an auto row promotes it (mirror the edge promotion, `annotate.py:832-855`).
- Refresh discipline for `source='curated'` nodes, mirroring `_refresh_curated_edge`: probe re-observation bumps `last_seen` only — no property overwrite, no `target_id` adoption; curated nodes are never soft-deleted by any refresh (they have no owning target). Auto-node behavior unchanged.
- `TopologyNode`/node list surfaces expose `source` (additive field; OpenAPI snapshot + CLI regen).

## Acceptance criteria

- [ ] Integration test: seed node with `note`/`evidence_url` → run a refresh whose snapshot contains the same `(kind, name)` → properties intact, `target_id` still NULL, `source='curated'`, `last_seen` bumped; a subsequent refresh with the node absent from the snapshot does NOT soft-delete it.
- [ ] Migration backfill test: pre-existing manually-seeded row (has `seeded_by`) → `curated`; probe-discovered row → `auto`; stamp-replay idempotency pinned to this migration's own revision.
- [ ] Auto-node reconcile behavior byte-identical for `source='auto'` (existing `test_topology_refresh.py` suite green unmodified except added cases).
- [ ] `source` visible on `list_nodes`/REST/MCP node shapes; OpenAPI snapshot + generated CLI regenerated.
- [ ] `tests/integration/` doubles that fabricate `GraphNode` rows gain the new attr (house lesson: integration-double tenant_id trap); ruff + mypy clean.

## Out of scope

- Node delete semantics (#2485 — but its deletability heuristic should move to `source='curated'` once this lands; cross-link). Edge behavior (already correct). Vocabulary (T1).

## References

- Parent: #TBD-INIT. Mould: `_refresh_curated_edge` `refresh.py:619-659`; edge `source` CHECK `models.py:1762,2028-2031`; promotion `annotate.py:832-855`.
- Review finding "Refresh adoption silently clobbers curated node data" (CONFIRMED, `topology-feature-review.md` 2026-07-15).
- Related: #2485.

---
---

## T3

**Title:** `Task: Approval-gated agent authorship — register topology.annotate/create_node/unannotate as targetless typed ops; agent MCP fronts dispatch through policy_gate (humans stay immediate)`

**Labels:** `task, enhancement, priority:high, effort:large, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1

## Summary

Implements the Initiative's authorship model: the agent proposes graph mutations, a human approves them from the existing approvals surfaces; human tenant_admin writes stay immediate everywhere. Mechanism: register the three topology writes as **targetless typed operations** (the `secret.move` mold) with `safety_level="caution"`, `requires_approval=False` — the combination that parks AGENT principals (needs-approval floor) while humans pass the default-allow branch — and switch the three MCP tool handlers from direct service calls to `dispatch()`, the single seam where `policy_gate` runs and where parked requests can actually resume.

## Current state (verified in code, 2026-07-15)

- All fronts bypass the dispatcher: MCP `_annotate_handler`/`_unannotate_handler` → service (`mcp/tools/topology.py:1543-1595,1773-1824`), `_create_node_handler` → service (`mcp/tools/topology_create_node.py:153-212`); REST `api/v1/topology.py:746,818`; UI `ui/routes/topology/edges.py:42-45`. Gating is per-session RBAC (`required_role=TENANT_ADMIN`, `topology_create_node.py:209`) — a visibility check, not a per-op verdict.
- `policy_gate` is called from exactly one place: `operations/dispatcher.py:1964`; NEEDS_APPROVAL → `_handle_needs_approval` → `create_pending_request` (`dispatcher.py:1981-1998,1786,1846-1871`).
- The park/resume substrate is target-optional: `ApprovalRequest.op_id` is free text (`models.py:4403`), `connector_id` need not name a real connector (`secret.move` stores a synthetic id; `connectors/secret/__init__.py:34-38`; `parse_connector_id` only needs the digit-led version suffix, `operations/_lookup.py:77-90`), `target_id` nullable = first-class tenant-wide op (`models.py:4296-4298`); resume re-dispatches stored params with `_approved=True` (`operations/approval_queue.py:740-833,882-895`), hash-verified against substitution (`:1144-1147`), exactly-one-resumer latch (`:679-737`). Target-NULL resume proven: `tests/test_api_v1_approvals.py:246`.
- Principal dials: `requires_approval=True` parks humans too (`_validate.py:115-127`); `safety_level="caution"` + `requires_approval=False` parks agents only (agent floor `_validate.py:222-226` via `auth/permissions.py:127-133,358-363`; human default-allow `_validate.py:129-138`). `PrincipalKind` at `auth/operator.py:95-124`.
- Typed-op registration mold: `register_typed_operation(product="secret", ..., handler=secret_move, ...)` `connectors/secret/ops.py:236-263`; handler signature `async def secret_move(operator, target, params)` with `target=None`.
- Audit provenance gap this task also closes: `annotate.py`'s `_build_audit_row` (`:536,553-565`) sets `operator_sub` but never `actor_sub` (nullable column exists, `models.py:488`) — agent-vs-human authorship is not queryable from topology audit rows.

## Desired state

- New module `backend/src/meho_backplane/connectors/topology_ops.py` (or `topology/ops.py` — implementer's call, secret/net precedent is under `connectors/`): three `register_typed_operation` calls — `topology.annotate`, `topology.create_node`, `topology.unannotate` — product `topology`, synthetic impl id (e.g. `topology-graph`), `target=None`, `safety_level="caution"`, `requires_approval=False`, `op_class="write"`; handlers unwrap params and call the existing service primitives (`annotate_edge`, `create_or_get_node`, `unannotate_edge`) unchanged.
- The three MCP handlers call `dispatch()` with those op_ids instead of the services directly. Agent principal → parks pending (`approval.pending` broadcast; visible in `/ui/approvals`, `meho approvals list`, `meho.approvals.list`); human approve → resume re-executes with original params → edge/node lands. Human tenant_admin via MCP → default-allow → immediate (same UX as today). REST + UI fronts unchanged (human-only surfaces).
- `_build_audit_row` gains `actor_sub=operator.actor_sub`-equivalent stamping (match how `mcp/audit.py:290-294` populates it) so topology audit rows record the acting principal; the parked-request audit rows already carry `policy_decision='needs-approval'` (`dispatcher.py:2019-2025`).
- Idempotency note: `annotate_edge`/`create_or_get_node` are idempotent upserts, so approve-time re-dispatch is safe even against races; the `resumed_at` latch already guarantees at-most-once resume.

## Acceptance criteria

- [ ] Dispatch-level test (mold: `tests/test_secret_move_approval.py:263,305,500`): agent-principal `meho.topology.annotate` parks pending and the edge is NOT written; a different human operator approves via `/decide`; the edge lands with the original params; second decide → 409 `approval_request_already_approved`.
- [ ] Same park→approve→execute cycle proven for `topology.create_node` (novel open-vocabulary kind, e.g. `dns-record` — the T1+T3 composition) and `topology.unannotate`.
- [ ] Human tenant_admin via MCP executes immediately (no ApprovalRequest row created); REST/UI annotate paths byte-identical (existing tests green unmodified).
- [ ] Params-hash substitution defence and target-NULL resume inherited (assert via existing queue tests extended with a topology op_id case).
- [ ] Topology write audit rows carry `actor_sub`; OpenAPI snapshot regenerated if any route metadata changed; ruff + mypy clean.

## Out of scope

- Gating human writes (`requires_approval=True`) — explicitly not wanted; humans stay immediate.
- Bulk import gating (T5 layers on this registration). Read verbs (never gated). Per-(agent, op) permission rows / scoping UI — the default floor is the v1 behavior.

## Security-review note

`safety_level="caution"` (not `dangerous`): graph writes are reversible (unannotate + §6 arbitration + append-only history), unlike credential moves; `dangerous` would DENY agents by default rather than park them (`auth/permissions.py:131-148`), defeating the propose-then-approve loop.

## References

- Parent: #TBD-INIT. Mould: `connectors/secret/ops.py:236-263`; `operations/approval_queue.py:740-833`; tests `test_secret_move_approval.py`, `test_approval_queue.py:1500,1785,2140`, `test_api_v1_approvals.py:246`.
- Review findings 8/9/10 (approval absent; principal_kind unconsulted), `topology-feature-review.md` 2026-07-15.
- G11.7 #1401 (policy_gate house pattern), #1225 (approval-gated write-op mold on a connector).

---
---

## T4

**Title:** `Task: Chain-shaped closure answers — parent_node_id + via_edge_id on traversal results; include_stale opt-out on all three verbs`

**Labels:** `task, enhancement, priority:medium, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1
Depends-on: #TBD-T6

## Summary

Makes blast-radius/RCA output chain-reconstructable in one call and stale-controllable. Today "what depends on this VM" returns a flat min-depth node list carrying only the *last* hop's edge kind — adjacency must be reassembled via extra `list-edges` calls — and traversal silently includes soft-deleted rows forever while `list_edges` excludes them (two first-class views disagree, no per-query control).

## Current state (verified in code, 2026-07-15)

- Walk CTE projects exactly `id, kind, name, properties, depth, via_edge_kind` (`topology/query.py:235-277` reverse, `:285-327` forward); the predecessor `w.id` and edge `e.id` are in scope in the recursive term but not projected. `DISTINCT ON (id) ... ORDER BY id, depth, name` keeps the min-depth row; converging equal-depth parents survive arbitrarily (needs a deterministic tie-break when parent is projected).
- `find_path` tracks no edge ids: `bi_edge` projects `src, dst, kind` only (`query.py:522-532`); `_build_path_nodes` derives `via_edge_kind` positionally (`query.py:580-604`).
- Staleness: traversal has no `last_seen` predicate anywhere (`query.py:702-709` documents this); `list_edges` hardcodes exclusion (`query.py:741`); param precedent `list_nodes(include_soft_deleted=False)` `query.py:1069`; optional-filter SQL idiom `CAST(:x AS type) IS NULL OR ...` `query.py:113-120`.
- Consumers: `TopologyNode` `schemas.py:81-111`, `TopologyPath` `:114-135`; REST routes `api/v1/topology.py:388-459,462-503,506-544`; CLI render `cli/internal/cmd/topology/nodes.go:32-50`, decode `closure.go:208-214` (additive-tolerant), flags `closure.go:26-49,218-234`; MCP facet dumps `model_dump` (`mcp/tools/topology.py:601-665`) — flows through. UI is NOT a consumer (own BFS, `ui/routes/topology/queries.py:254-326`).
- Shape-pinning tests to extend: `tests/integration/test_topology_query.py:183,216,239`; `test_topology_g91_acceptance.py:365`; `test_topology_query_schemas.py:74,98`.

## Desired state

- Closure verbs: project `w.id AS parent_node_id, e.id AS via_edge_id` (anchor row: NULLs, next to the existing `CAST(NULL AS text) AS via_edge_kind` at `query.py:244`); extend the `DISTINCT ON` subquery ORDER BY with a deterministic tie-break so equal-depth converging parents are stable. `TopologyNode` gains nullable `parent_node_id: UUID | None`, `via_edge_id: UUID | None` (additive).
- `find_path`: add `id` to both `bi_edge` legs + an `edge_ids` accumulator; path nodes carry `via_edge_id`.
- `include_stale: bool = True` (default preserves today's last-refresh-wins contract) on `find_dependents`/`find_dependencies`/`find_path`: predicate `AND (CAST(:include_stale AS boolean) OR (n.last_seen IS NOT NULL AND e.last_seen IS NOT NULL))` in both recursive terms AND both `bi_edge` legs (same both-legs rule as the superseded guard, `query.py:66-70`); thread through service params (`query.py:361-369,428-435,482-489,607-615`), REST Query params, CLI flags (`--include-stale=false`), MCP input schema (`mcp/tools/topology.py:~174-436`).
- CLI closure table gains a `PARENT` column; `--json` carries the full shape.

## Acceptance criteria

- [ ] Integration test: a 3-hop diamond graph — closure result reconstructs the exact edge chain from `parent_node_id`/`via_edge_id` alone; converging equal-depth parents deterministic across repeated runs.
- [ ] `find_path` nodes carry `via_edge_id`; positional `via_edge_kind` behavior unchanged.
- [ ] Integration test: soft-deleted node/edge included with `include_stale=true` (default) on all three verbs, excluded with `false` — including the reversed `bi_edge` leg (a stale edge must not be walked backwards into a path).
- [ ] OpenAPI snapshot + generated CLI regenerated; MCP facet exposes both additions; existing flat-shape tests updated, suite green.
- [ ] ruff + mypy clean; `docs/codebase/topology.md` read-half section updated.

## Out of scope

- UI/console changes (not a consumer). Path perf work (T6). Removing the last-refresh-wins default (default stays `include_stale=true`).

## References

- Parent: #TBD-INIT. Ground map: `query.py:235-327,519-604,702-709,1069`; consumers as cited above.
- Review findings 19/20 (flat list; stale-view disagreement), `topology-feature-review.md` 2026-07-15.
- House rule: CLI snapshot regen before commit (`cd cli && make snapshot-openapi && make generate`).

---
---

## T5

**Title:** `Task: MCP authoring parity — meho.topology.bulk_import (free dry-run plan, approval-gated apply) + superseded ids on the annotate return shape`

**Labels:** `task, enhancement, priority:medium, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T3

## Summary

Gives the agent the propose→plan→apply loop humans already have. `bulk_import_edges` — atomic, with a `dry_run` mode returning a per-row create/update/conflict plan — is reachable via REST, CLI, and the console, but has no MCP tool: an agent seeding a cross-system inventory loops single `annotate` calls with no pre-apply plan. Composed with T3: the agent dry-runs freely (read-shaped), and the *apply* dispatches through the approval gate — the human approves one batch instead of N single writes. Also closes the in-band-notice gap: the MCP annotate return omits which auto edges the write just superseded.

## Current state (verified in code, 2026-07-15)

- `bulk_import_edges(session, operator, rows, *, dry_run=False)` exists (`topology/bulk_import.py`); fronts: `POST /api/v1/topology/edges/bulk` (tenant_admin), CLI `meho topology bulk-import --dry-run`, console modal. No MCP tool; dedupe confirms none filed.
- MCP annotate return shape: `edge_id, from, to, kind, source, conflicts` only (`mcp/tools/topology.py:1597-1615`); its own description documents the omission (`:1536-1539`). Superseded ids already computed and stored in the audit payload (`annotate.py:1123`) — surfacing is a return-shape change, no new query.
- T3 lands the typed-op registration module + dispatch-through-gate pattern for topology writes.

## Desired state

- New MCP tool `meho.topology.bulk_import` (tenant_admin, own module per the 600-line file guidance — mold: `mcp/tools/topology_create_node.py`): `dry_run=true` (default) calls the service validation pass directly and returns the per-row plan (read-shaped, never parks); `dry_run=false` dispatches `topology.bulk_import` registered in T3's module (`safety_level="caution"`, `requires_approval=False`) — agent apply parks as ONE ApprovalRequest carrying the batch params; human approve applies the whole batch atomically (existing all-or-nothing transaction).
- MCP annotate return gains `superseded: [edge_id, ...]` (from the plan's superseded pairs); tool description updated (drop the omission caveat at `mcp/tools/topology.py:1536-1539`).
- HTTP bulk cap (1000 rows) honored at the MCP boundary too.

## Acceptance criteria

- [ ] Agent-principal `meho.topology.bulk_import {dry_run: true}` returns the per-row plan immediately with no ApprovalRequest row; `{dry_run: false}` parks one pending request; human approve → all rows land atomically; a validation-failing batch surfaces the per-row diagnostics and writes nothing.
- [ ] Human tenant_admin `{dry_run: false}` via MCP applies immediately (T3 dial inherited).
- [ ] MCP annotate response carries `superseded` ids exactly matching the audit payload's list (test asserts equality).
- [ ] Batch >1000 rows rejected at the tool boundary with the same envelope as REST.
- [ ] OpenAPI snapshot unchanged (MCP-only surface) or regenerated if shared schemas moved; ruff + mypy clean.

## Out of scope

- Node bulk import (edges-only, matching the existing service). Node delete (#2485). New REST/CLI surface (exists).

## References

- Parent: #TBD-INIT. Mould: `topology/bulk_import.py` service; `mcp/tools/topology_create_node.py` (separate-module pattern); T3's registration module.
- Review finding 16 (authoring-toolkit asymmetry; propose-then-apply flow human-only), `topology-feature-review.md` 2026-07-15.

---
---

## T6

**Title:** `Task: find_path per-branch target pruning + dense-mesh perf fixture + refresh-vs-annotate concurrency test`

**Labels:** `task, bug, priority:medium, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT

## Problem

`_PATH_SQL`'s recursive term is target-blind: it enumerates the entire ≤`max_hops` undirected reachability ball (every edge doubled in `bi_edge`) before the final select filters for the target and takes `ORDER BY hops LIMIT 1` (`topology/query.py:543-562`). On a dense curated mesh (branch factor b) row count grows ~b^hops, and any operator-role caller can select `max_hops=32` (`api/v1/topology.py:512`, `_MAX_HOPS_MAX=32` at `:226`). Every perf artifact models a sparse hub-and-chains forest (`tests/fixtures/topology_10k_nodes.py:53-172` — out-degree exactly 1, no mesh, no cycles), so the dense case CI would need to catch is never exercised; nor is any refresh-vs-annotate write race.

## Current state (verified in code, 2026-07-15)

- `bi_edge` CTE `query.py:522-532`; walk accumulators `:533-551`; final-select join + LIMIT `:553-562`.
- PG recursive-CTE limits (postgresql.org/docs/16/queries-with.html): per-branch pruning IS expressible (resolve target id in a non-recursive CTE; `AND w.node_id <> (SELECT id FROM target)` in the recursive term — the recursive self-reference restriction is not violated); global cross-branch termination is NOT expressible; `LIMIT 1` cannot stop evaluation early because `ORDER BY hops` must consume the full walk.
- `GraphSpec` params: `fanout`, `per_chain`, `hub_name`, `edge_kind` only (`topology_10k_nodes.py:53-100`); `TEN_K` preset `:96-100`.
- Zero concurrency tests in the topology suite (no `asyncio.gather`); the advisory-lock path is tested only with a mocked pre-held lock.
- Perf-gate discipline to copy: the depth-16 fix's load-invariant ratio gate (not wall-clock widening) in `test_topology_query.py:433`.

## Desired state

- `_PATH_SQL` gains the target CTE + per-branch stop (a branch never extends past a target hit). Behavior identical (same shortest path or `None`); cost bounded per branch.
- `GraphSpec` extended (or a sibling `MeshSpec`) generating dense meshes: cross-chain links, converging paths, cycles, mixed edge kinds, optional soft-deleted rows — parameters documented.
- New integration benchmark: worst-case `find_path` (unreachable target, dense mesh, `max_hops` at ceiling) pinned with a ratio-style gate + generous absolute ceiling; documents the envelope in `docs/architecture/topology.md` §Performance expectations.
- New concurrency integration test: `asyncio.gather(refresh_target_topology(...), annotate_edge(...))` against the pgvector container — asserts no lost update, no dangling markers, both audit rows present (real advisory-lock path).

## Acceptance criteria

- [ ] Path results byte-identical on the existing suite (shortest path, `None`-unreachable, superseded-both-legs tests green unmodified).
- [ ] `EXPLAIN`-level or row-count assertion proving the pruned walk emits fewer rows than the unpruned equivalent on the mesh fixture (regression-pinned).
- [ ] Worst-case benchmark exists and is CI-green with the documented envelope; `docs/architecture/topology.md` perf table updated.
- [ ] Concurrency test green against the real container (Docker-gated skip mirrors the existing integration suite).
- [ ] ruff + mypy clean.

## Out of scope

- Lowering `_MAX_HOPS_MAX` (a contract change; revisit only if the benchmark shows the pruned walk still blows up). Application-side BFS rewrite. Closure-verb output shape (T4).

## References

- Parent: #TBD-INIT. Ground: `query.py:519-604`; caps `api/v1/topology.py:223-226,512`; fixture `topology_10k_nodes.py:53-172`.
- PostgreSQL 16 recursive queries + CYCLE: https://www.postgresql.org/docs/16/queries-with.html.
- Review findings 18/24 (path materialization; unexercised load profile), `topology-feature-review.md` 2026-07-15.
