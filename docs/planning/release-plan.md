# MEHO release plan — ship *usable* capability, not parts

Companion to [mvp-roadmap.md](mvp-roadmap.md). The roadmap sequences *what gets
built*; this document sequences *what becomes usable*, because the two had
silently diverged: by initiative-closure the board read v0.2–v0.5 as `✓ ready`,
while **not one REST connector could execute against a real target.** This plan
fixes the measurement and the sequence.

Authored 2026-05-22 after a full-board + code audit. Depth-first, with CLI⇄MCP
parity held as a hard constraint every release.

## The one principle

> **A release ships only when it passes a demo test: a named workflow an
> operator *and* an agent can complete end-to-end through MEHO.**
> "Done" means "works against a real target," not "the initiative merged."

Two corollaries:

- **State, not closure.** Every connector is described by the [3-state
  rubric](../codebase/connector-release-readiness.md): State 1 (cataloged) /
  State 2 (one auth model executes) / State 3 (production). Release notes cite
  the state. "Cataloged" is never reported as "shipped."
- **Dual-surface parity.** Each demo test must pass via **both** the `meho`
  CLI and the MCP meta-tools (`call_operation`, etc.). They share one dispatch
  path; the constraint keeps them from drifting.

## Honest capability ledger (2026-05-22)

| Aspect | Goal | Real state | Usable E2E? |
|---|---|---|---|
| Substrate (dispatch / registry / audit / policy / resolver) | G0 | 89/94 tasks; **JSONFlux reducer stubbed** ([#750](https://github.com/evoila/meho/issues/750), pass-through) | ⚠️ small responses only |
| Knowledge base | G4 | 3/3 inis, 14/14 tasks | ✅ |
| Memory | G5 | 3/3, 16/16 | ✅ |
| Broadcast (live SSE feed) | G6 | 3/3, 16/17 | ✅ |
| Audit **query** | G8.1 | done | ✅ |
| Topology / targets-as-data | G9 | 23/26; G9.3 in progress | ✅ mostly |
| Connectors — **discovery** (search + catalog) | G3 | vmware/k8s/nsx/sddc/harbor/VCF cataloged | ✅ |
| Connectors — **execution** (real vendor calls) | G3 | **only `vault-1.x` truly executes**; bind9 caveated; all REST loaders stubbed ([#939](https://github.com/evoila/meho/issues/939)/[#944](https://github.com/evoila/meho/issues/944)) | ❌ |
| Tenant conventions | G7 | 0/1, 0/6 ([#229](https://github.com/evoila/meho/issues/229)) | ❌ not started |
| Audit **replay** | G8.2 | filed, 0 tasks ([#377](https://github.com/evoila/meho/issues/377)) | ❌ not started |
| Operator web UI | G10 | 0/6, 3/20 | ❌ barely started |
| Agentic runtime | G11 | 0/6, 0/17 | ❌ not started |

**Headline:** MEHO today is a working *knowledge / memory / broadcast / audit /
topology backplane with a connector search surface* — but it cannot execute a
vendor REST call against a real target, nor safely reduce a large response.
Both are downstream of two cross-cutting gates.

## The two gates that unlock everything

Neither is a connector-tier line item, which is exactly why they were invisible.
Both gate *usability* across multiple releases.

1. **JSONFlux reducer** — [#750](https://github.com/evoila/meho/issues/750)
   (G0.6.1), in progress, off the version map. [CLAUDE.md postulate
   6](../../CLAUDE.md) calls set-shaped reduction "non-negotiable"; the
   dispatcher now defaults to the real
   [`JsonFluxReducer`](../architecture/jsonflux.md) (T3 #753 merged), so a
   large vCenter list returns a `ResultHandle` instead of dumping raw into
   the agent context — the precise failure MEHO exists to prevent.
2. **Credential execution** — [#939](https://github.com/evoila/meho/issues/939)
   + [#944](https://github.com/evoila/meho/issues/944). Decision:
   operator-context Vault read ([connector-auth.md](../architecture/connector-auth.md)).
   Without it, no REST connector authenticates.

Land both and the dozens of already-cataloged connector ops become usable at
once. **This is the highest-leverage work on the board.**

## Releases (capability-themed, depth-first)

Each maps onto the existing version numbers in [mvp-roadmap.md](mvp-roadmap.md);
the change is the *ordering* (gates first) and the *done-when* (demo test).

### R1 — "Execute one thing, safely" → folds into v0.2.1 / v0.3

**Demo test:** an operator runs `meho vmware vm list --target rdc-vcenter`
*and* an agent calls `call_operation("vmware-rest-9.0", <vm list op>, target)`;
both return a **reduced, handle-backed, audited** result from a **real** vCenter.

| Pulls in | Why |
|---|---|
| [#750](https://github.com/evoila/meho/issues/750) JSONFlux reducer | so the result is safe at scale (postulate 6) |
| [#939](https://github.com/evoila/meho/issues/939) credential broker + vmware slice (#940 threading, #941 helper, #942 vmware loader + lab E2E, #943 Vault-policy runbook) | so the call authenticates as the operator |

**Done-when:** vmware-rest at **State 2**; demo test green via CLI + MCP; lab
E2E (env-gated) passes; no secret in logs/results; release notes say "executes
(shared_service_account); reduced output."
**Lane:** @ikaric / @zdamir (JSONFlux — retrieval/backplane) ∥ @kr3s0 (credential slice).

### R2 — "Execute the working set" → v0.3 / v0.4 / v0.5

**Demo test:** an RDC operator retires `scripts/*.sh` for tier-1/2/3 ops for ≥2
weeks (Goal [#214](https://github.com/evoila/meho/issues/214) done-when), with
per-tenant standing instructions auto-loading.

| Pulls in | |
|---|---|
| [#944](https://github.com/evoila/meho/issues/944) fan-out: nsx/harbor/sddc (#945), vROps/vRLI/Fleet (#946), vcf-automation (#947), k8s (#948) | every REST + k8s connector → State 2 |
| [#229](https://github.com/evoila/meho/issues/229) G7.1 tenant conventions | standing instructions per tenant |

**Done-when:** each connector at State 2 with a recorded-fixture E2E; conventions
load at session preamble (CLI + MCP); Goal #214 done-when met.
**Lane:** @kr3s0 (connectors) ∥ @zdamir (conventions/backplane).

### R3 — "See it" → v0.9 / v0.10

**Demo test:** an operator uses a web console to browse broadcast / KB /
connectors / memory / topology, and replays a past agent session forensically.

| Pulls in | |
|---|---|
| G10 ([#336](https://github.com/evoila/meho/issues/336)) operator web UI | surfaces the already-working substrate that's API/MCP-only today |
| [#377](https://github.com/evoila/meho/issues/377) G8.2 audit replay | forensic session traversal |

**Done-when:** each console reads live data; replay reconstructs a session from
`audit_log`. **Lane:** @damir-topic (UI) ∥ @zdamir (replay/audit).

### R4 — "Run it unattended" → v0.7 / v0.8

**Demo test:** an agent completes a governed multi-step op (approval-gated, scheduled,
sanitized) end-to-end, including a safe store-to-store secret move it never observes.

| Pulls in | |
|---|---|
| G11 ([#800](https://github.com/evoila/meho/issues/800)) agentic runtime (G11.1 runtime, G11.2 identity/RBAC/approval, G11.3 scheduler, G11.4-6 hardening) | the autonomous spine |
| [#581](https://github.com/evoila/meho/issues/581) secret broker | unattended credential moves, agent-blind |

**Done-when:** an unattended agent run passes with approval + audit + scheduling.
**Lane:** @zdamir / @ikaric (runtime + MCP) ∥ @kr3s0 (connector ops).

## Why this ordering

- **Depth-first:** R1 proves *one* connector works end-to-end before fanning out.
  The fastest credible "it executes" moment; de-risks the credential + JSONFlux
  design once, on vSphere, before it's repeated 8×.
- **Already-done aspects aren't re-litigated** — KB/memory/broadcast/audit-query/
  topology work now; R3 *surfaces* them (UI) rather than rebuilding them. "Covers
  all aspects" is satisfied by scheduling the not-started ones (conventions in R2,
  replay + UI in R3, runtime in R4), not by spreading R1 thin.
- **The gates lead.** #750 + #939 before #944, because fan-out to inert
  connectors ships more parts that still don't work.

## What changed vs the old roadmap

1. Two gates promoted to first-class, release-leading work: #750, #939/#944.
2. Release "done" redefined as the demo test (State 2/3), not initiative merge.
3. CLI⇄MCP parity made an explicit per-release constraint.
4. Goal-label gap fixed (G2–G9 were unlabeled and invisible to rollups).

## See also

- [mvp-roadmap.md](mvp-roadmap.md) — version→initiative mapping (now annotated with the gates).
- [connector-release-readiness.md](../codebase/connector-release-readiness.md) — the State 1/2/3 rubric.
- [architecture/connector-auth.md](../architecture/connector-auth.md) — the credential-read decision.
- [research/214-connector-credential-broker.md](../research/214-connector-credential-broker.md) — the credential-broker research.
