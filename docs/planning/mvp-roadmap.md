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
| **v0.2** | **MVP1** | Substrate complete + vSphere (REST + vi-json + composites) + KB | in flight (~19 open tasks) |
| **v0.2.1** | **MVP1 hardening** | Dogfood corrective — make the shipped v0.2 actually consumer-usable (7 upstream wall signals) | all 8 tasks closed; blocks new v0.3/v0.4 starts until done |
| **v0.3** | **MVP2** | k8s + Vault + bind9 + topology graph | filed, mostly unstaffed |
| **v0.4** | **MVP3** | NSX + SDDC + Harbor + agent memory | partially filed |
| **v0.5** | **MVP4** | VCF mgmt plane + broadcast *complete* (live SSE + historical query) | partially filed |
| **v0.6** | **MVP5** | pfSense + gcloud + Hetzner Robot + tenant conventions | partially filed |
| **v0.7** | **MVP6** | **Agent runtime — floor** (G11.1 runtime + G11.2 identity/RBAC/approval + G11.3 scheduler) | filed (17 tasks); unstaffed |
| **v0.8** | **MVP7** | **Agent runtime — hardening** (G11.4 sanitization + G11.5 providers/budgets + G11.6 reference patterns) | initiatives filed; child tasks unfiled |
| **v0.9** | **MVP8** | Operator web UI (broadcast / KB / connectors / memory / topology) + topology time-travel | filed |
| **v0.10** | **MVP9** | Audit replay (forensic session traversal) | initiative filed; tasks unfiled |
| **v0.11** | **MVP10** | Holodeck connector — closes the G3 wrapper-retirement story (deferred; unblocked, ready now) | filed (3 tasks) |

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
(topology time-travel queries) ships in **v0.9** alongside the operator web UI,
where the Topology UI gives time-travel real reach. (Originally floated for
v0.7; moved with the UI in the 2026-05-22 replan — see Cross-cutting.)

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

## v0.8 — MVP7 — Agent runtime — hardening (G11 waves 2–4)

### What ships

Completes the agent-runtime capability. The floor (v0.7) is not
production-safe until sanitization lands, so these ship as the immediate
follow-on, ahead of the UI:

- **C1+C2 — Safety (G11.4)** — sanitization middleware (declarative regex hot
  path + Presidio for free-text) on *every* connector response, "store raw in
  audit, show redacted to caller"; plus the agent audit/replay extension
  (`agent_session_id` lineage, raw+redacted side-by-side, redaction manifest).
  This is what satisfies the consumer's "zero raw credentials in any LLM
  prompt" bar.
- **C4+C3 — Portability + cost (G11.5)** — LLM-provider abstraction (Anthropic
  + Bedrock + OpenAI + on-prem vLLM/Ollama + **VCF Private AI Foundation**) so
  the same agents ship to SaaS-OK and air-gapped tenants; plus per-identity
  token budgets (the cost kill switch).
- **R1–R4 — Reference patterns (G11.6)** — runnable sample agents + docs in
  `examples/` (tiered triage, operator-approval gate, kb write-back,
  local-Claude-as-triage). Not MEHO surface — composition examples on the
  primitives.

### Initiatives

| Initiative | # |
|---|---|
| [G11.4 Safety — sanitization + audit/replay](https://github.com/evoila/meho/issues/805) | #805 |
| [G11.5 Portability + cost — providers + budgets](https://github.com/evoila/meho/issues/806) | #806 |
| [G11.6 Reference patterns (R1–R4)](https://github.com/evoila/meho/issues/807) | #807 |

*Child tasks for #805 / #806 / #807 are outlined in each Initiative body but
not yet filed as issues — file them before this version locks.*

---

## v0.9 — MVP8 — operator web UI + topology time-travel

**Pushed back from v0.7** by the 2026-05-22 agent-runtime reprioritisation.
Content is unchanged — the operator console, plus G9.3 topology time-travel
which travels with it (its value is gated by the Topology UI).

### What ships

- **Operator console** at `/ui/*` — HTMX 2 + Jinja2 + Tailwind 4 + DaisyUI 5
  + Alpine.js + Cytoscape.js island
  - Frontend chassis (G10.0)
  - Activity broadcast UI (G10.1) — live SSE feed + filters + wall-monitor mode
  - Knowledge base UI (G10.2) — search + view + drag-and-drop upload
  - Connectors + Targets UI (G10.3) — table + per-target detail + ops matrix
  - Memory UI (G10.4) — 5-scope filtered list + scope-promotion + expiry viz
  - Topology UI (G10.5) — tabular + Cytoscape.js graph + dependents/dependencies/path
  - **Agent surfaces** (run / inspect / approve) land here as a G10.x slice now
    that the agent runtime (G11) ships first
- **Discovery history (G9.3)** — pulled from v0.3 so the Topology UI can show
  time-travel topology meaningfully

### Initiatives

| Initiative | # |
|---|---|
| [G10.0 Frontend chassis](https://github.com/evoila/meho/issues/337) | #337 |
| [G10.1 Broadcast UI](https://github.com/evoila/meho/issues/338) | #338 |
| [G10.2 KB UI](https://github.com/evoila/meho/issues/339) | #339 |
| [G10.3 Connectors + Targets UI](https://github.com/evoila/meho/issues/340) | #340 |
| [G10.4 Memory UI](https://github.com/evoila/meho/issues/341) | #341 |
| [G10.5 Topology UI](https://github.com/evoila/meho/issues/342) | #342 |
| [G9.3 Discovery history](https://github.com/evoila/meho/issues/365) | #365 *(moved from v0.3)* |

---

## v0.10 — MVP9 — audit replay (post-MVP forensics)

**Pushed back from v0.8** by the agent-runtime reprioritisation; content
unchanged.

### What ships

- **Audit replay** — `meho audit replay <session-id>` reconstructs the full
  forensic trace of one agent session as a chronologically-ordered,
  parent/child tree of every operation. Recursive-CTE traversal over
  `audit_log.parent_audit_id`. Closes the third leg of G8. (Now also traverses
  the `agent_session_id` lineage added in v0.8 / G11.4.)

### Initiatives

| Initiative | # |
|---|---|
| [G8.2 Audit replay](https://github.com/evoila/meho/issues/377) | #377 |

*Audit query core (G8.1) shipped in v0.5. Only the replay/graph-traversal
half remains.*

---

## v0.11 — MVP10 — Holodeck connector (G3 closer)

**Deferred from v0.7** in the 2026-05-22 replan — lowest scheduled priority,
but **unblocked and ready now**: it inherits the shipped `SshConnector`, and
its tasks (#853–#855) are filed and ready for
`/auto-implement-initiative #371`. It blocks nothing and nothing blocks it, so
any contributor can pull it forward opportunistically; it sits at the tail only
because agent runtime + UI + audit replay outrank it.

### What ships

- **Holodeck** (typed-SSH; PowerShell-over-SSH) — ~8 read-only inspection ops
  against the VMware Holodeck nested-VCF-lab appliance (HoloRouter has no REST
  API). Closes the G3 wrapper-retirement story — every consumer `scripts/*.sh`
  wrapper now has a MEHO parallel. Pod-clone provisioning stays a future Runbook
  under G11. See [#371](https://github.com/evoila/meho/issues/371).

### Initiatives

| Initiative | # |
|---|---|
| [G3.8 Holodeck typed-SSH](https://github.com/evoila/meho/issues/371) | #371 |

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

### Items pushed back

*All four below are consequences of the 2026-05-22 agent-runtime
reprioritisation; none changed in content, only in sequence.*

- **Operator web UI (G10.0–G10.5)** — moved v0.7 → **v0.9**. Reason: agent
  runtime (G11) reprioritised ahead of it. Ships one release later, unchanged.
- **Audit replay (G8.2)** — moved v0.8 → **v0.10**, displaced by the two
  agent-runtime releases. Content unchanged.
- **Holodeck (G3.8)** — moved v0.7 → **v0.11** (tail). Reason: tier-4,
  event-shaped, lowest operator-frequency connector; deferred so agent runtime
  leads. Unblocked and ready — can be pulled forward opportunistically.
- **G9.3 Discovery history (topology time-travel)** — moved v0.3 → **v0.9**
  (was floated for v0.7). Reason: value is gated by the Topology UI, so it
  travels with the UI wherever the UI lands.

### Ownership today — the structural risk

| Version | Owned Initiatives | Unstaffed | Risk |
|---|---|---|---|
| v0.2 (in flight) | G3.1 (Tarik) | G0.3, G0.6, G0.7, G4.1 | **HIGH** — load-bearing substrate has no named owner |
| v0.3 | — | all | UNSTAFFED |
| v0.4 | — | all | UNSTAFFED |
| v0.5 | — | all | UNSTAFFED |
| v0.6 | — | all | UNSTAFFED |
| v0.7 | — | G11.1, G11.2, G11.3 (all) | **HIGH** — new lead release; the agent-runtime floor has no named owner |
| v0.8 | — | G11.4, G11.5, G11.6 (all; child tasks unfiled) | UNSTAFFED |
| v0.9 | G10.0–G10.4 (`@damir-topic`), G10.5 + G9.3 (`@zdamir`) | — | staffed |
| v0.10 | — | G8.2 | UNSTAFFED |
| v0.11 | G3.8 (`@kr3s0`) | — | staffed; ready to auto-implement |

**Owner assignment is the single largest unmitigated risk to this roadmap.**
Even MVP1 — partially merged, in flight — has zero named owners on four of its
six driving Initiatives.

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
