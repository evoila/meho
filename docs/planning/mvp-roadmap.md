# MEHO MVP roadmap — versioned deploys

Maps the MVP sequence to release versions, names what ships in each, and links the
GitHub Initiatives that close each MVP.

**Relationship to [v0.2-decisions.md](v0.2-decisions.md):** v0.2-decisions remains
the locked-decisions reference for *what's in scope at all* for the v0.2-and-beyond
horizon. **This document fragments that horizon into deliverable deploys.** When
this file says "v0.2 ships only MVP1," that supersedes the looser "everything is
v0.2" framing on the board.

## TL;DR

| Version | MVP | Headline | Status |
|---|---|---|---|
| **v0.1** | (pre-MVP) | FastAPI chassis, Keycloak JWT, audit middleware, Helm chart, CI, broadcast SSE feed | **shipped — on `main`** |
| **v0.2** | **MVP1** | Substrate complete + vSphere (REST + vi-json + composites) + KB | **shipped — tag `v0.2.0` (2026-05-16)** |
| **v0.2.1** | **MVP1 hardening** | Dogfood corrective — make the shipped v0.2 actually consumer-usable (7 upstream wall signals) | **shipped — tag `v0.2.1`** |
| **v0.3** | **MVP2** | k8s + Vault + bind9 + topology graph | **shipped — tag `v0.3.1` (2026-05-21)**; connectors at State 1, execution gated by [#944](https://github.com/evoila/meho/issues/944) — see overlay |
| **v0.4** | **MVP3** | NSX + SDDC + Harbor + agent memory | **shipped — folded into `v0.5.0` (no own tag cut)**; connectors at State 1 |
| **v0.5** | **MVP4** | VCF mgmt plane + broadcast *complete* (live SSE + historical query) | **shipped — tag `v0.5.1` (2026-05-22)**; connectors at State 1, execution gated by [#944](https://github.com/evoila/meho/issues/944) |
| **v0.6** | **MVP5** | pfSense + gcloud + Hetzner Robot + tenant conventions | **shipped — tag `v0.6.0` (2026-05-26)** |
| **v0.7** | **MVP6** | **Agent runtime — floor** (G11.1 runtime + G11.2 identity/RBAC/approval + G11.3 scheduler) | **shipped — tag `v0.7.0` (2026-05-27)** |
| **v0.8** | **MVP7** | **Consolidated post-v0.7 release** — agent runtime hardening (G11.4–G11.6) + operator web UI (G10.0–G10.5) + topology time-travel (G9.3) + audit replay (G8.2) + Holodeck (G3.8) + github-rest connector (G3.11) + broadcast meta-tools (G6.4) + retrieval enhancements (G4.4) + v0.6/v0.7 dogfood hardening cycles (G0.13/G0.14/G0.15) | **shipped — tag `v0.8.0` (2026-05-28)** |
| **v0.9** | **MVP8** | **Runbooks** (G12 — schema + template + run lifecycle + session priming + CLI) + CLI hygiene (G0.12) + v0.8.x consumer closed-loop dogfood hardening (G0.16/G0.17/G0.18) + release-tooling unblock (G0.11 partial: #1126/#1127) | **shipped — tag `v0.9.0` (2026-05-31)** — all of G12 + G0.12 + G0.16/17/18 closed; rest of G0.11 → v0.10 |
| **v0.10** | **MVP9** | **Connector write surface + operator UX** — G3 remainder: argocd reads (G3.12 #1387) + keycloak reads (G3.13 #1388) + k8s / vault / VCF write activation (G3.14 #1398 / G3.15 #1399 / G3.16 #1400) + approval-policy hardening for connector writes (G11.7 #1397) + Runbooks UI (G10.6 #1381). G0.11 substrate remainder (#1111, #292) + next dogfood cycle ride along. | **◀ next to ship** — planning; write activations (G3.14/15/16) gated on production ingest LLM client [#1386](https://github.com/evoila/meho/issues/1386) + G11.7 approval path |

---

## Capability-first overlay (added 2026-05-22)

**Read [release-plan.md](release-plan.md) alongside this table.** A full-board +
code audit on 2026-05-22 found that the version status above measures
*initiative closure*, not *usable capability*: v0.2–v0.5 read as shipped while
**no REST connector can execute against a real target** and **set-shaped
responses are not reduced**. Two cross-cutting gates — invisible because they
are not connector-tier line items — block usability across those versions:

| Gate | Initiative | Status | Blocks |
|---|---|---|---|
| **JSONFlux reducer** (real reduction, not pass-through; [postulate 6](../../CLAUDE.md)) | [G0.6.1 #750](https://github.com/evoila/meho/issues/750) | open, 0/5, **was off-map** | safe output for *every* connector at scale |
| **Connector credential broker** (operator-context Vault read) — vmware vertical slice | [G3.9 #939](https://github.com/evoila/meho/issues/939) | open, 0/4, **new** | v0.2 vSphere actually executing |
| **Credential-loader fan-out** (nsx/sddc/harbor, VCF, k8s) | [G3.10 #944](https://github.com/evoila/meho/issues/944) | open, 0/4, **new** | v0.3/v0.4/v0.5 connectors executing |

**Connector versions ship at [State 1–3](../codebase/connector-release-readiness.md), not binary "done":**

- **v0.2** vSphere, **v0.3** k8s, **v0.4** nsx/sddc/harbor, **v0.5** VCF — all
  shipped/planned at **State 1 (cataloged)**. They reach **State 2 (executes)**
  only when #750 + #939 (v0.2) and #944 (v0.3–v0.5) land. Slot #939 into
  **v0.2.1**; #944 spans **v0.3/v0.4/v0.5**; #750 into **v0.2.1**.
- Release notes cite the connector's state + live auth models, never "shipped"
  for a cataloged-only connector (the convention from
  [connector-release-readiness.md](../codebase/connector-release-readiness.md)).

The capability-first sequencing (R1 gates → R2 fan-out → R3 UI/replay → R4
runtime) lives in [release-plan.md](release-plan.md); it re-orders *delivery*
without renumbering the versions here.

---

## v0.1 — chassis (already shipped)

What's on `main` today, treated as the floor every MVP builds on:

- FastAPI backplane with Keycloak JWT auth + per-tenant audit middleware
- WORM `audit_log` table — append-only, synchronous, the source of truth
- Helm chart, CI lanes (Python + Go + Helm + image build + secret scan + Semgrep + DCO)
- Vault subchart + `VaultConnector` reference (G0.2 #223 ✓)
- Retrieval substrate (pgvector + fastembed + hybrid BM25/cosine RRF — G0.4 #225 ✓)
- MCP server bootstrap (OAuth-2.1 resource-server — G0.5 #226 ✓)
- Tenant model code-tasks (G0.1 #222 ✓ — parent issue still flagged OPEN as paperwork)
- **Broadcast SSE feed core (G6.1 #228 ✓)** — `/api/v1/feed`, per-tenant Valkey
  Streams, publish-on-audit-write hook. Agents can subscribe today.

---

## v0.2 — MVP1 — substrate + vSphere + KB

### What an operator gets when they `helm upgrade` v0.2

- Multi-tenant JWT extraction, per-tenant audit scoping, role-enum RBAC
- Targets-as-data (no more `targets.yaml` on disk)
- **Operation registry + dispatcher + JSONFlux + composite recursion** — the
  load-bearing substrate every later connector dispatches through
- Spec-ingestion pipeline (OpenAPI 3.0/3.1) with operator review queue
- **vSphere connector**, ingesting **both** spec files (see "Scope clarifications")
- **Knowledge base** — agent and operator surface to ingest, search, serve
  markdown knowledge entries (`search_knowledge` / `add_to_knowledge` MCP tools)
- **Retrieval eval + cutover tooling** — three-surface eval corpus (KB +
  operations seeded; memory deferred to v0.4) with precision@5 / MRR /
  coverage metrics + grep baseline + CI gate (G4.3). This is the **quality
  bar** for the new retrieval substrate; without it MVP1 ships blind.

### Initiatives that close on v0.2 release

| Initiative | # | Open tasks |
|---|---|---|
| [G0.1 Tenant model](https://github.com/evoila/meho/issues/222) | #222 | 0 — close parent paperwork |
| [G0.3 Targets-as-data](https://github.com/evoila/meho/issues/224) | #224 | #255, #256, #257 |
| [G0.6 Op registry + dispatcher](https://github.com/evoila/meho/issues/388) | #388 | #412, #472, #475 |
| [G0.7 Spec ingestion pipeline](https://github.com/evoila/meho/issues/389) | #389 | #403, #404, #405, #406, #407, #408, #409 |
| [G3.1 vSphere REST + vi-json + composites](https://github.com/evoila/meho/issues/227) | #227 | code-tasks all ✓; E2E canary is G0.7-T8 #408 |
| [G4.1 KB migration + verbs](https://github.com/evoila/meho/issues/331) | #331 | #415, #416, #417, #418, #419, #420 (PR #430 in flight) |
| [G4.3 Retrieval migration tooling](https://github.com/evoila/meho/issues/373) | #373 | #441 (PR #473), #442, #443, #445, #446, #464 |

### Critical scope clarifications

These are restated here because they keep slipping out of conversations:

1. **vSphere ingests BOTH `vcenter.yaml` AND `vi-json.yaml`.** One connector
   (`vmware-rest-9.0`), one `endpoint_descriptor` table, two spec files
   merged at ingest. `vcenter.yaml` (961 paths) covers the modern REST
   automation API. `vi-json.yaml` (2,195 paths) covers Performance Manager,
   EventManager, host-network atomic mutations, and several inventory edges
   the modern REST spec doesn't cover. **Without vi-json, govc parity is
   ~60%; with both, ~95%.** Already in scope on [#227](https://github.com/evoila/meho/issues/227) —
   pinning here so it can't quietly fall out.

2. **Composite operations are MVP1 scope, not deferred polish.** ~13
   hand-authored composites cover the govc-parity workflows that *no single
   API call satisfies*: `vm.create` (folder lookup → config spec → POST →
   NIC attach → power-on), `host.evacuate` (lookup → list VMs → migrate
   each → enter maintenance), `cluster.patch`, `vm.snapshot.revert`,
   `event.tail`, `performance.summary`, etc. They run through
   [G0.6-T7's composite recursion infrastructure](https://github.com/evoila/meho/issues/398)
   (merged). **Without composites the connector is a thin pass-through and
   operators still need wrappers** — defeating the whole point of MEHO.
   Full list on [#227 §8](https://github.com/evoila/meho/issues/227).

3. **Broadcast SSE is already live (G6.1 #228 closed).** Agents subscribing
   to `meho://tenant/{id}/feed` get real-time activity from day one of v0.2.
   No additional v0.2 work needed for the live half.

### Done-when (v0.2 ships when …)

1. Operator runs `meho connector ingest --spec vcenter.yaml --spec vi-json.yaml`
   → ~3,156 endpoint rows enter `endpoint_descriptor` scoped to `vmware-rest-9.0`
2. Operator runs `meho connector review vmware-rest-9.0` → edits group
   `when_to_use` strings → `meho connector enable vmware-rest-9.0`
3. Agent calls `search_operations(connector_id="vmware-rest-9.0", query="list VMs in cluster")`
   and dispatches via `call_operation(...)`
4. Agent successfully executes the 13 composites end-to-end against vcsim or a
   real lab vCenter (`vmware.composite.host.evacuate` and friends)
5. Agent calls `search_knowledge(...)` and `add_to_knowledge(...)` against the
   tenant KB seeded from the consumer's 44-entry `kb/` shelf
6. SSE feed emits a broadcast event for every audited operation; a second
   agent subscribed to `meho://tenant/{id}/feed` sees the first agent's work
7. **Retrieval eval gate is green in CI** (`meho retrieval eval` ≥ baseline
   on KB + operations corpora; memory corpus deferred to v0.4)

### Cross-MVP scope note on G4.3

G4.3 #373 ships in v0.2 because retrieval-quality verification is what
gates MVP1's confidence to ship. Two of its tasks are **forward-investment**
for later MVPs:
- T4 #443 Memory eval corpus → exercises the Memory layer that doesn't
  exist until v0.4. Ships the YAML now so v0.4 can light it up immediately.
- T7 #446 Final retire-checklist + migration-blocker label → drives the
  cutover from legacy retrieval paths; full operationalisation lands as old
  paths get retired across v0.2-v0.4.

That's deliberate: investing in eval coverage early is cheaper than racing
to write tests retroactively as each consumer surface lands.

---

## v0.2.1 — MVP1 hardening — G0.8 dogfood corrective

**Slotted ahead of v0.3.** v0.2.0 tagged and deployed smoke-green, but
seven consumer dogfood signals proved the smoke-green was hollow: the
acceptance smoke passed only because `dry_run` ingests never write, so a
fresh runbook-following deploy could do zero tenant-scoped writes. There
is no value shipping MVP2 on top of an MVP1 the consumer cannot write to,
so G0.8 corrects it before v0.3 work proceeds.

### What ships

- **Tenant-row JIT seed** so a fresh deploy's first real write succeeds
  (#628) — the hard FK wall the dry-run smoke masked
- **Chart Valkey env-var alignment** so `/ready`'s broadcast leg and
  `helm upgrade` stop failing (#583)
- **Vault connector reads the operator JWT from request context** —
  drops the pre-#224 `raw_jwt` stub (#629)
- **MCP `MCP_RESOURCE_URI` default / fail-loud + actionable 401** (#633)
- **`connector_id` documented + 404-on-unknown** (no more empty-200
  "empty catalog" trap) (#630)
- **`retrieve/usage` counted-surface documented** (#632)
- **`/version` build-stamping** — `git_sha` / `build_date` /
  `chart_version` (#631)
- **v0.2 acceptance smoke extended** to a real federated non-dry-run
  write so this "green-but-hollow" class cannot recur (#668)

### Initiatives

| Initiative | # |
|---|---|
| [G0.8 v0.2 dogfood hardening](https://github.com/evoila/meho/issues/634) | #634 |
| [G0.6.1 Real JSONFlux reducer (execution gate)](https://github.com/evoila/meho/issues/750) | #750 |
| [G3.9 Connector credential broker + vmware vertical slice (execution gate)](https://github.com/evoila/meho/issues/939) | #939 |

Parent goal G0 [#221](https://github.com/evoila/meho/issues/221). All 8
G0.8 child Tasks (#628 #583 #629 #633 #630 #632 #631 #668) closed; the three
hard blockers (#628 / #583 / #629) cleared, lifting the v0.3/v0.4 freeze.

**Execution gates added 2026-05-22 (see [release-plan.md](release-plan.md)).**
v0.2 shipped vSphere at **State 1 (cataloged)**. It reaches **State 2
(executes against a real target)** only when #750 (safe reduction) and #939
(operator-context credential read) land — both folded into v0.2.1 because the
hardening theme is "make the shipped v0.2 actually usable."

> **`/meho-status` `VERSION_MAP` drift (surfaced, not silently
> reconciled):** the `meho-status` skill's hardcoded `VERSION_MAP` does
> not yet carry G0.8 / v0.2.1. Per that skill's hard rule 7 this doc is
> canonical; the map is the thing that's now behind and must be
> reconciled by the skill's maintainer — this roadmap edit does not
> touch the skill.

---

## v0.3 — MVP2 — tier-1 connectors + topology

### What ships

- **k8s connector** (kubernetes-asyncio, typed) — read-heavy ops, replaces the
  operator's daily `kubectl-vcf.sh` wrapper
- **Vault connector** (typed) — KV-v2 + sys + auth read/list ops; first
  G6-credential-read classifier exerciser
- **bind9 connector** (typed-SSH) — first `SshConnector` child; atomic-apply
  discipline
- **Topology graph** — schema + auto-discovery from every connector + three
  query verbs (`dependents` / `dependencies` / `path`) + curated cross-system
  edges + annotation flow

### Initiatives

| Initiative | # |
|---|---|
| [G3.2 k8s-1.x](https://github.com/evoila/meho/issues/320) | #320 |
| [G3.3 Vault op surface](https://github.com/evoila/meho/issues/366) | #366 |
| [G3.4 bind9-9.x typed-SSH](https://github.com/evoila/meho/issues/367) | #367 |
| [G9.1 Graph schema + auto-discovery + verbs](https://github.com/evoila/meho/issues/363) | #363 |
| [G9.2 Curated cross-system edges](https://github.com/evoila/meho/issues/364) | #364 |
| [G3.10 Credential-loader fan-out (execution gate; spans v0.3–v0.5 connectors)](https://github.com/evoila/meho/issues/944) | #944 |

**Execution gate:** [G3.10 #944](https://github.com/evoila/meho/issues/944) is
one initiative that takes the v0.3 (k8s), v0.4 (nsx/sddc/harbor) and v0.5 (VCF)
connectors from **State 1 → State 2**. It is mapped here at v0.3 as its earliest
gated version; depends on [#939](https://github.com/evoila/meho/issues/939). See
[release-plan.md](release-plan.md) R2.

**Resolved:** [G9.3 Discovery history](https://github.com/evoila/meho/issues/365)
(topology time-travel queries) ships in **v0.8** (consolidated post-v0.7
release) alongside the operator web UI, where the Topology UI gives time-travel
real reach. (Originally floated for v0.7; moved with the UI in the 2026-05-22
replan — see Cross-cutting.)

### Closed alongside (historical, retro-slotted 2026-05-28)

| Initiative | # | Tag | Note |
|---|---|---|---|
| [G0.9 v0.3.1 dogfood hardening](https://github.com/evoila/meho/issues/737) | #737 | v0.3.1 | 10-task post-v0.3 consumer-feedback wall |
| [G0.9.1 v0.3.2 dogfood hardening](https://github.com/evoila/meho/issues/772) | #772 | v0.3.1+ | Second-cycle dogfood; landed before v0.4 cut |

---

## v0.4 — MVP3 — tier-2 connectors + memory

### What ships

- **NSX-T 4.2** (generic-ingested)
- **SDDC Manager 9.0** (generic-ingested)
- **Harbor 2.x** (generic-ingested)
- **Memory layer** — server-side replacement for laptop-local
  `~/.claude/.../memory/` markdown files. Five scopes (user / user×tenant /
  user×target / tenant / target), four verbs (recall / remember / forget /
  list), auto-expiry background task, scope-promotion verb, laptop-local
  migration UX

### Initiatives

| Initiative | # |
|---|---|
| [G3.5 NSX + SDDC + Harbor (tier-2 batch)](https://github.com/evoila/meho/issues/368) | #368 |
| [G5.1 Memory storage + verbs](https://github.com/evoila/meho/issues/332) | #332 |
| [G5.2 Auto-expiry + promote + per-scope RBAC](https://github.com/evoila/meho/issues/374) | #374 |
| [G5.3 Laptop-local migration UX](https://github.com/evoila/meho/issues/375) | #375 |

### Closed alongside (historical, retro-slotted 2026-05-28)

| Initiative | # | Note |
|---|---|---|
| [G4.2 Docs sidecar pipeline](https://github.com/evoila/meho/issues/372) | #372 | Port `gen-spec-sidecar`; closed without dedicated tasks (handled inline in v0.4 work) |

---

## v0.5 — MVP4 — VCF mgmt plane + broadcast **complete**

### Broadcast philosophy — what "perfect broadcast" means

There are exactly **two** things agents need from broadcast:

1. **Live awareness** — see what other agents are doing in real time.
2. **Historical recall** — query "what happened in this tenant over the past
   X days" to ground new work in recent activity (avoid redoing the same
   investigation, see who already touched a target, learn from outcomes).

**Both halves run on one audit log** (the WORM `audit_log` table in the chassis).
Two surfaces sit on top of it:

| Need | Surface | Status |
|---|---|---|
| Live awareness | SSE feed at `/api/v1/feed`; MCP `broadcast_recent(since, filter)` and `broadcast_watch(filter)` per [CLAUDE.md](../../CLAUDE.md) | **Already shipped (G6.1 #228 ✓)** |
| Historical recall | Audit query at `/api/v1/audit/query`; MCP `query_audit(filters)`; `broadcast_recent(since=7d, ...)` extends to deep history | **Pulled into v0.5 from v0.8** — [G8.1 #334](https://github.com/evoila/meho/issues/334) |
| PII discipline | Per-call opt-in + tenant-convention opt-out + per-channel detail level (credential reads and audit queries stay aggregate-only by default) | [G6.3 #376](https://github.com/evoila/meho/issues/376) |

**No chat-tool mirroring.** Broadcast lives where agents live: as MCP tools
and an SSE feed agents subscribe to. Anyone can build a Slack/Discord/email
subscriber externally; the backplane doesn't ship one.

### What ships in v0.5

- **VCF management plane** — VCF Operations 9.0 + VCF Logs 9.0 + VCF Fleet 9.0
  + VCF Automation 9.0 (all generic-ingested; per-product auth divergence stays)
- **PII opt-in / opt-out** controls
- **Audit query core** — `meho audit query / recent / show / who-touched /
  my-recent` (CLI + REST + MCP tool). Pulled from MVP7 because **this is what
  closes the "historical recall" half of perfect broadcast.**

### Initiatives

| Initiative | # |
|---|---|
| [G3.6 VCF mgmt plane (4 connectors)](https://github.com/evoila/meho/issues/369) | #369 |
| [G6.3 PII opt-in/out controls](https://github.com/evoila/meho/issues/376) | #376 |
| [G8.1 Audit query core](https://github.com/evoila/meho/issues/334) | #334 *(moved from v0.8)* |

### Dropped from scope

- **[G6.2 Slack mirror #333](https://github.com/evoila/meho/issues/333)** —
  chat-tool mirroring deferred indefinitely. **Recommend closing as `wontfix`**
  per the broadcast philosophy above.

### Closed alongside (historical, retro-slotted 2026-05-28)

| Initiative | # | Note |
|---|---|---|
| [SonarCloud signal cleanup](https://github.com/evoila/meho/issues/921) | #921 | Config tuning + flag triage; one-off code-quality hygiene (no Goal slug) |

---

## v0.6 — MVP5 — tier-3 standalone + conventions / runbooks

### What ships

- **pfSense** (typed-SSH), **gcloud** (transport TBD), **Hetzner Robot**
  (generic-ingested)
- **Tenant conventions** — CLAUDE.md-equivalent standing instructions
  auto-loaded into the agent session preamble per tenant. Layer-1 server-side
  `tenant_conventions` table + Layer-2 starter onboarding template
  (`docs/examples/consumer-onboarding/`)

### Initiatives

| Initiative | # |
|---|---|
| [G3.7 tier-3 standalone](https://github.com/evoila/meho/issues/370) | #370 |
| [G7.1 Tenant conventions + Layer-2 starter](https://github.com/evoila/meho/issues/229) | #229 |

### Closed alongside (historical, retro-slotted 2026-05-28)

| Initiative | # | Note |
|---|---|---|
| [G0.10 Planning + board hygiene](https://github.com/evoila/meho/issues/949) | #949 | One-off board-state cleanup; closed inside the v0.6 window |

---

## v0.7 — MVP6 — Agent runtime — floor (G11 wave 1)

**Reprioritised 2026-05-22:** agent runtime (Goal G11) moves ahead of the
operator web UI and Holodeck. MEHO becomes a first-class **agent host** —
long-running LLM agents that observe, reason, and (under governance) act,
running *inside* MEHO's process boundary on the same identity + RBAC + audit +
dispatch machinery as human operators. G11 is UI-independent by design (its own
scope note: "agent surfaces in G10's UI are a later G10.x slice"), so it can
lead while the UI waits. See [Goal #800](https://github.com/evoila/meho/issues/800).

### What ships

The **runtime floor** — the three primitives that make MEHO an agent host:

- **P1 — Agent runtime (G11.1)** — in-process Pydantic AI tool-use loop behind
  an `AgentRun` seam; sync + async (handle/SSE) invocation; agent-invokes-agent.
  Every tool call routes through the normal dispatch + RBAC + audit path.
- **P3 — Agent identity + RBAC + approval (G11.2)** — agents as Keycloak
  principals; RFC 8693 delegation (`sub`=user, `act`=agent); the v0.2
  `requires_approval` hard-deny becomes the durable per-(principal, op, target)
  approval queue (auto-execute | needs-approval | deny) it always foresaw.
- **P2 — Scheduler (G11.3)** — durable cron + one-off + event(outbox) triggers
  firing agent runs; the floor of 24/7 operation. Roll-our-own vs DBOS settled
  by a spike task.

Builds only on shipped substrate (G0 identity/audit/dispatch, G4 knowledge,
G5 memory). Surfaced via CLI + MCP per v0.1 — no web-UI dependency.

### Initiatives

| Initiative | # | Tasks |
|---|---|---|
| [G11.1 Agent runtime (P1)](https://github.com/evoila/meho/issues/802) | #802 | #808–#813 (6) |
| [G11.2 Agent identity + RBAC + approval (P3)](https://github.com/evoila/meho/issues/803) | #803 | #815–#820 (6) |
| [G11.3 Scheduler (P2)](https://github.com/evoila/meho/issues/804) | #804 | #822–#826 (5) |

---

## v0.8 — MVP7 — Consolidated post-v0.7 release

**Renumbered 2026-05-28.** Originally v0.8 was agent-runtime-hardening only;
v0.9 was the operator UI; v0.10 was audit replay; v0.11 was Holodeck. All four
sets of work landed (or are landing) on `main` against the v0.7 tag without an
intermediate cut. Rather than ship v0.8 → v0.9 → v0.10 → v0.11 in rapid
succession for work that's already merged, v0.8 collapses everything into one
deployable release. **Tagged as `v0.8.0` on 2026-05-28.** Contents below.

### What ships

- **Agent runtime hardening** (G11.4–G11.6) — sanitization middleware
  (C1+C2), LLM-provider abstraction + per-identity budgets (C4+C3), runnable
  reference patterns (R1–R4). Production-safety floor for the runtime that
  landed in v0.7.
- **Operator web UI** (G10.0–G10.5) — `/ui/*` HTMX 2 console (chassis +
  broadcast / KB / connectors / memory / topology surfaces). Agent surfaces
  (run/inspect/approve) land here as a G10.x slice.
- **Topology time-travel** (G9.3) — discovery audit log → graph history,
  surfaced through the Topology UI.
- **Audit replay** (G8.2) — `meho audit replay <session-id>` reconstructs the
  forensic trace of one agent session. Recursive-CTE traversal over
  `audit_log.parent_audit_id`; also traverses `agent_session_id` lineage from
  G11.4.
- **Holodeck connector** (G3.8) — typed-SSH read-only ops against the VMware
  Holodeck nested-VCF appliance. Closes the G3 wrapper-retirement story.
- **github-rest connector** (G3.11) — first GitHub connector (off-roadmap;
  filed and closed inside the v0.7→v0.8 window).
- **Broadcast meta-tools** (G6.4) — `broadcast_recent` + `broadcast_watch`
  refinements (off-roadmap; close-ready).
- **Retrieval enhancements** (G4.4) — metadata-filters / RBAC plumbing on
  `search_memory` and `retrieve` (off-roadmap; close-ready).
- **Dogfood hardening cycles** (G0.13 + G0.14 + G0.15) — v0.6.0 close-loop +
  v0.6.0 post-validate + v0.7.0 close-loop. Originally would have shipped as
  v0.6.1 / v0.7.1 patch tags; folded into v0.8 instead.

### Initiatives

| Initiative | # | State |
|---|---|---|
| [G11.4 Safety — sanitization + audit/replay](https://github.com/evoila/meho/issues/805) | #805 | closed |
| [G11.5 Portability + cost — providers + budgets](https://github.com/evoila/meho/issues/806) | #806 | closed |
| [G11.6 Reference patterns (R1–R4)](https://github.com/evoila/meho/issues/807) | #807 | closed |
| [G10.0 Frontend chassis](https://github.com/evoila/meho/issues/337) | #337 | closed |
| [G10.1 Broadcast UI](https://github.com/evoila/meho/issues/338) | #338 | closed |
| [G10.2 KB UI](https://github.com/evoila/meho/issues/339) | #339 | closed |
| [G10.3 Connectors + Targets UI](https://github.com/evoila/meho/issues/340) | #340 | closed |
| [G10.4 Memory UI](https://github.com/evoila/meho/issues/341) | #341 | closed |
| [G10.5 Topology UI](https://github.com/evoila/meho/issues/342) | #342 | closed |
| [G9.3 Discovery history](https://github.com/evoila/meho/issues/365) | #365 | closed |
| [G3.8 Holodeck typed-SSH](https://github.com/evoila/meho/issues/371) | #371 | closed |
| [G8.2 Audit replay](https://github.com/evoila/meho/issues/377) | #377 | closed |
| [G6.4 Broadcast meta-tools](https://github.com/evoila/meho/issues/1090) | #1090 | closed |
| [G0.13 v0.6.0 dogfood hardening](https://github.com/evoila/meho/issues/1130) | #1130 | closed |
| [G0.14 v0.6.0 post-validate hardening](https://github.com/evoila/meho/issues/1139) | #1139 | closed |
| [G4.4 Retrieval enhancements](https://github.com/evoila/meho/issues/1178) | #1178 | closed |
| [G0.15 v0.7.0 closed-loop hardening](https://github.com/evoila/meho/issues/1209) | #1209 | closed |
| [G3.11 github-rest typed connector](https://github.com/evoila/meho/issues/1220) | #1220 | closed |

### Done — v0.8.0 shipped 2026-05-28

- [x] PR [#1244](https://github.com/evoila/meho/pull/1244) (G11.6-T4 R4 example) merged → G11.6 #807 reached DoD.
- [x] Remaining close-ready Initiatives admin-closed in the release-cutting session.
- [x] Tagged `v0.8.0` per [RELEASING.md](../RELEASING.md); GitHub Release notes carry the [0.8.0 CHANGELOG](../../CHANGELOG.md#080---2026-05-28) section.

---

## v0.9 — MVP8 — Runbooks (G12) + substrate hardening

**Planning milestone.** Renumbered 2026-05-28 from "operator web UI" (which
folded forward into v0.8) to **Runbooks**. The MEHO Runbooks Goal (G12) was
filed late and didn't fit the old MVP6/MVP7 schedule; this is its window.
G0.11 (CI / test-infra / release tooling) and G0.12 (CLI hygiene — migrate
hand-rolled HTTP CLI to the generated client) travel with it as substrate
hardening that doesn't earn its own milestone but blocks future velocity.

### What ships

- **Runbook schema + dispatcher correlation** (G12.1) — the data model and
  the wiring that lets a runbook step correlate with the dispatch + audit it
  triggers.
- **Runbook template lifecycle** (G12.2) — draft / edit / publish flow for
  runbook templates.
- **Runbook run lifecycle + adherence floor** (G12.3) — what it means for a
  run to actually follow its template; the opacity floor that gives runbooks
  governance-grade value.
- **Runbook session priming** (G12.4) — `initialize.instructions`-style
  preamble injection so an agent starts a session with its runbook context
  already loaded.
- **Runbook CLI surface** (G12.5) — `meho runbook` verbs (template / run /
  list / show / etc.).
- **Release-tooling unblock** (G0.11 partial) — the two release-blocking
  children of #956 ship with v0.9.0: goreleaser `draft: false` (#1127) so the
  GitHub Release auto-publishes, and the `/release` skill Phase 2/3 fixes
  (#1126). The rest of G0.11 is deferred to v0.10.
- **CLI hygiene** (G0.12) — the hand-rolled HTTP CLI verbs migrated to the
  generated openapi client (#1118, 16/16 ✅).

### Initiatives

| Initiative | # | State |
|---|---|---|
| [G12.1 Runbook schema + dispatcher correlation](https://github.com/evoila/meho/issues/1196) | #1196 | ✅ closed (2/2) |
| [G12.2 Runbook template lifecycle](https://github.com/evoila/meho/issues/1197) | #1197 | ✅ closed (5/5) |
| [G12.3 Runbook run lifecycle + adherence floor](https://github.com/evoila/meho/issues/1198) | #1198 | in flight (7/9) |
| [G12.4 Runbook session priming](https://github.com/evoila/meho/issues/1199) | #1199 | ✅ closed (3/3) |
| [G12.5 Runbook CLI surface](https://github.com/evoila/meho/issues/1200) | #1200 | ✅ closed (3/3) |
| [G0.12 CLI hygiene — generated-client migration](https://github.com/evoila/meho/issues/1118) | #1118 | ✅ closed (16/16) |
| [G0.16 v0.8.0 closed-loop dogfood hardening](https://github.com/evoila/meho/issues/1302) | #1302 | ✅ closed (6/6) |
| [G0.17 v0.9.0 closed-loop dogfood hardening](https://github.com/evoila/meho/issues/1329) | #1329 | ✅ closed (1/1) |
| [G0.18 v0.8.1 closed-loop dogfood hardening](https://github.com/evoila/meho/issues/1353) | #1353 | ✅ closed (11/11) |

G0.16–G0.18 are the consumer closed-loop dogfood-hardening cycles (RDC #771 /
#789 feedback) that landed on `main` after the v0.8.x cuts; they ship in v0.9.0.
**G0.11** (CI / test-infra / release-tooling hardening, #956) is **deferred to
v0.10** — only its two release-blocking children (#1126, #1127) land with
v0.9.0. G12.3 (#1198) closed before the tag, so all of G12 ships in v0.9.0.

---

## v0.10 — MVP9 — Connector write surface + operator UX

**Planning milestone.** Slotted 2026-05-31, immediately after the `v0.9.0`
tag. This is the **connector write-surface release**: it takes the G3 connector
set from State-1 reads to State-2 writes, hardens the approval path that gates
those writes, and lands the Runbooks UI. The G0.11 substrate remainder + next
dogfood cycle ride along (the user's call 2026-05-31) rather than getting their
own tag.

### What ships

- **argocd connector — L1-typed GitOps reads** (G3.12 #1387) — read-side
  control-plane parity. **Not** gated on the ingest LLM client.
- **keycloak connector — L1-typed Admin-REST reads** (G3.13 #1388) — read-side
  IAM parity. **Not** gated on the ingest LLM client; the keycloak *write*
  surface is a follow-up under the same Initiative.
- **kubernetes write/exec op surface** (G3.14 #1398) — apply / scale / rollout
  / exec, all `requires_approval=True`.
- **vault write/admin op surface** (G3.15 #1399) — promote `kv.put`/`delete`
  and admin ops, all `requires_approval=True`.
- **VCF write activation** (G3.16 #1400) — activate the 8 already-authored
  vmware write composites (`vm.create`, `host.evacuate`, `host.detach_from_vds`,
  …). **Activation, not authoring** — making `preflight_l2_dependencies()` pass
  after ingest groups + enables the underlying L2 sub-ops.
- **Approval-policy hardening for connector writes** (G11.7 #1397) — route
  human `requires_approval` dispatch to the queue (not hard-deny), self-approval
  guard, resume-target fix, write-op secret redaction (#1401). **This gates the
  usable path for every write op above.**
- **Runbooks UI** (G10.6 #1381) — browse + author runbook templates
  (Jinja2 / HTMX), the operator-facing surface on top of the v0.9 Runbooks core.
- **Production ingest LLM client** (G3.17 #1407) — wire the agent runtime's
  Anthropic client into the ingest grouping pass at lifespan startup so
  `--catalog` ingest works on deployed backplanes. **The prerequisite that
  unblocks the write half** (hard-gates G3.16; gates G3.14/G3.15 L2 dispatch).
  Adopts #1386 as its T1.
- **G0.11 substrate remainder** (riding along) — testcontainers
  `LogMessageWaitStrategy` (#1111) + SonarCloud CPD exclusions (#292), the tail
  of #956 left after the two release-blocking children shipped in v0.9.0. Plus
  the next consumer dogfood-hardening cycle.

### Initiatives

| Initiative | # | State | Tasks |
|---|---|---|---|
| [G3.12 argocd connector — L1 reads](https://github.com/evoila/meho/issues/1387) | #1387 | open | 0/4 |
| [G3.13 keycloak connector — L1 reads](https://github.com/evoila/meho/issues/1388) | #1388 | open | 0/4 |
| [G3.14 kubernetes write/exec op surface](https://github.com/evoila/meho/issues/1398) | #1398 | open | 0/2 |
| [G3.15 vault write/admin op surface](https://github.com/evoila/meho/issues/1399) | #1399 | open | 0/5 |
| [G3.16 VCF write activation](https://github.com/evoila/meho/issues/1400) | #1400 | open | 0/3 |
| [G3.17 production ingest LLM client](https://github.com/evoila/meho/issues/1407) | #1407 | open | 0/2 (adopts #1386 as T1) |
| [G11.7 approval-policy hardening](https://github.com/evoila/meho/issues/1397) | #1397 | open | 0/2 |
| [G10.6 Runbooks UI](https://github.com/evoila/meho/issues/1381) | #1381 | open | 1/4 |
| [G0.11 CI/test-infra/release-tooling (remainder)](https://github.com/evoila/meho/issues/956) | #956 | open (carried from v0.9) | 10/12 |

### Sequencing / dependencies

The write half of the release has a hard prerequisite **outside the seven
Initiatives**:

- **[#1386](https://github.com/evoila/meho/issues/1386) — production ingest LLM
  client at lifespan startup** (open, unlabelled) gates `--catalog` ingest on a
  deployed backplane, which is what groups + enables the L2 sub-ops the write
  surfaces dispatch through. **G3.16 explicitly cannot start until #1386
  lands**, and the L2 dispatch path of G3.14/G3.15 depends on it too. **Slot
  #1386 first** or the write half of v0.10 can't ship.
- **G11.7 #1397** (incl. #1401) must land before any write op is usable end-to-
  end — it's the human-approve queue path every `requires_approval=True` op
  routes through.

Read-side (G3.12 argocd, G3.13 keycloak) and G10.6 (Runbooks UI) carry **no**
ingest-client dependency and can land in parallel from day one.

**Decomposition (done 2026-05-31):** G3.15 (#1399 → #1409–#1413, 5 tasks),
G3.16 (#1400 → #1414–#1416, 3 tasks), and G3.17 (#1407 → #1386 as T1 + #1408)
are decomposed and on the board (Status: Todo). G3.12/G3.13 (#1387/#1388) and
G10.6 (#1381) retain their pre-existing child tasks.

---

## Cross-cutting

### Connector cadence

Tier is fixed by operator value, not architectural dependency:

| Tier | Connector | Kind | Ships |
|---|---|---|---|
| 1 | vSphere (REST + vi-json + composites) | generic-ingested | v0.2 |
| 1 | k8s | typed (kubernetes-asyncio) | v0.3 |
| 1 | Vault | typed | v0.3 |
| 1 | bind9 | typed-SSH | v0.3 |
| 2 | NSX-T 4.2 | generic-ingested | v0.4 |
| 2 | SDDC Manager 9.0 | generic-ingested | v0.4 |
| 2 | Harbor 2.x | generic-ingested | v0.4 |
| 3 | VCF Ops / Logs / Fleet / Automation | generic-ingested | v0.5 |
| 3 | pfSense | typed-SSH | v0.6 |
| 3 | gcloud | transport TBD | v0.6 |
| 3 | Hetzner Robot | generic-ingested | v0.6 |
| — | Holodeck | typed-SSH | v0.11 *(deferred 2026-05-22; ready now)* |

### Items dropped from scope

- **G6.2 Slack mirror (#333)** — not shipping. Recommend `wontfix`.

### Items pulled forward

- **Agent runtime (Goal G11) — off-roadmap → v0.7 + v0.8.** Pulled in and
  prioritised ahead of the operator UI in the 2026-05-22 replan: MEHO-as-agent-
  host (P1 runtime + P2 scheduler + P3 identity/RBAC/approval in v0.7; then
  C1–C4 + reference patterns in v0.8). Builds on already-shipped substrate and
  is UI-independent by design. This **redefines MVP6/MVP7** (previously
  Holodeck + UI / audit replay) — see TL;DR and the per-version sections.
- **G8.1 Audit query core** — moved v0.8 → v0.5. Reason: it's what gives
  agents "what happened in the past X days," which is half of the broadcast
  contract.

### Items pushed back / consolidated

- **Operator web UI (G10.0–G10.5)**, **Audit replay (G8.2)**, **Holodeck
  (G3.8)**, **G9.3 Discovery history** — all originally targeted at separate
  releases (v0.7/v0.8/v0.9/v0.10/v0.11). All landed (or close-ready) on `main`
  during the v0.7→v0.8 window without intermediate cuts. **Consolidated into
  v0.8 on 2026-05-28** rather than cutting four near-empty tags.
- **Runbooks (Goal G12)** — filed 2026-05; given its own milestone as **v0.9**.
- **CI/test/release hardening (G0.11)** + **CLI hygiene (G0.12)** — substrate
  work that doesn't earn its own milestone; travels with v0.9.

### Ownership today (updated 2026-05-31)

| Version | Status | Notes |
|---|---|---|
| v0.2 → v0.9 | **shipped** | tags `v0.2.0` through `v0.9.0` cut |
| v0.10 (◀ next) | **mostly unstaffed** | 7 open Initiatives: G3.12/13/14/15/16 + G11.7 + G10.6, all `unassigned` except G0.11 #956 (@ddzafic, carried). Connector lane → propose @kr3s0; G11.7 approval/backplane → @zdamir; G10.6 frontend → @damir-topic |

**Immediate planning need for v0.10:** (1) **land G3.17 #1407** (ingest LLM
client, prerequisite #1386 = T1) — without it the write half (G3.14/15/16)
can't ship; (2) ~~decomposition~~ — done 2026-05-31 (G3.15/G3.16/G3.17 tasks
filed + on board); (3) **assign owners** to the connector wave — propose
@kr3s0 for G3.12–G3.17, @zdamir for G11.7 (approval-queue/backplane),
@damir-topic for G10.6 (frontend). Read-side connectors (G3.12/G3.13) + G10.6
carry no ingest-client dependency and can start immediately.

---

## Maintenance

When a version ships:
1. Tag the release in git.
2. Tick the version row in TL;DR with the closed-on date.
3. If any Initiative slipped, move it down one MVP and note the slip in the
   "Items pushed back" section.

When an Initiative gets re-scoped:
1. Update the relevant version section here.
2. Re-link from `v0.2-decisions.md` if a locked decision changed.

This file is authoritative for **delivery sequencing**. v0.2-decisions.md is
authoritative for **in-scope-at-all** decisions. They cite each other.
