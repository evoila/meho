---
name: prefer-meho
description: >
  MEHO-first routing for any work in a repo wired to a MEHO backplane:
  which governed surface serves each evidence or execution need, and how
  to fall back safely. Use at the start of every session and whenever you
  are about to read, change, look up, remember, coordinate, or run a
  procedure — prefer MEHO MCP tools and CLI verbs over local script
  wrappers, raw API calls, or local files unless explicitly told otherwise.
---

<!--
GENERATED FILE — DO NOT EDIT.
Rendered from docs/examples/consumer-onboarding/contract/meho-first-routing.md
by scripts/ci/gen_consumer_routing.py. Edit the contract source and re-run
the generator; backend/tests/test_consumer_routing_render.py fails CI on drift.
-->

# MEHO-first operations

This repo operates infrastructure **through** a MEHO backplane. Prefer MEHO surfaces over local fallbacks unless the operator explicitly says otherwise. Each area has its own skill (`meho:operations`, `meho:knowledge`, `meho:memory`, `meho:broadcast`) with the concrete verbs; this skill is the routing spine they share.

## Why MEHO first

MEHO writes an append-only audit row for every operation, broadcasts a live
event for every operation, and enforces tenant + role policy on every
operation. None of those guarantees hold for a local `scripts/*.sh`
wrapper, a raw `curl`, or a hand-edited file. Routing through the backplane
by default keeps every action on one identity + audit plane, and turns a
genuine capability gap into a filed, tracked issue instead of a silent
old-habit reach.

## Backplane discovery — the first act of every session

Before doing any work, establish **which backplane serves this repo** and
that you can reach it. This is one cheap step at session start; every
routing decision after it assumes a known, reachable backplane.

- **Find it.** The wiring lives in the repo: the MEHO Claude Code plugin
  (whose `.mcp.json` points MCP clients at the tenant's backplane over the
  `mcp-remote` stdio shim) and the `MEHO_INSTANCE` environment variable the
  CLI reads (e.g. `https://meho.example.com` — do not hard-code it in
  command examples). If no token is cached,
  `meho login "$MEHO_INSTANCE"` obtains one via the backplane's OAuth 2.0
  device-code flow.
- **Confirm it.** `meho status` (CLI) or the `meho_status` MCP tool reports
  reachability, your tenant, and your role.
- **No backplane configured** — no plugin, no `.mcp.json`, no
  `MEHO_INSTANCE` — is an **onboarding gap to raise, not a licence to work
  locally**. Say so and get the repo wired before falling through to local
  tools.
- **Configured but unreachable** (VPN, pod, cert, or auth outage) is the
  transient fallback path: notify the operator and fall back for the
  session.

## Route by evidence need

Once the backplane is known, route each need to its governed surface
first. The local column is the **break-glass** fallback you reach only
after the break-glass protocol below — never as a first move. Tool names
are the exact registered MCP names (see `docs/codebase/mcp.md`); the CLI
verbs run the same governed dispatch.

| The task needs … | MEHO surface (reach here first) | Local break-glass |
|---|---|---|
| an operator **preference or note** | MCP `search_memory` / `add_to_memory` · CLI `meho memory …` / `meho remember` (scopes `user` / `user-tenant` / `tenant`) | a local memory file |
| a **prior incident or team lesson** | MCP `search_knowledge` / `add_to_knowledge` · CLI `meho kb …` | `grep` / edit under a local `kb/` |
| a **vendor- or version-specific fact** | MCP `list_doc_collections` → `search_docs` / `ask_docs` (capability-gated — see note) | local `docs/` sidecars |
| the **live state of a target** | MCP `search_operations` → `preview_operation` → `call_operation`, with `result_query` to page a set-shaped result (never a raw dump) · CLI `meho operation …` / `meho <connector> <op>` | a local connector wrapper |
| **dependencies / blast radius** | MCP `query_topology` (+ `list_targets` for inventory) · CLI `meho targets …` | reading a local `targets.yaml` |
| **who did what / forensics** | CLI `meho audit …` (the working path); MCP `query_audit` is operator-gated (`mcp:admin`) | local wrapper logs |
| **coordination** with other operators | MCP `meho_broadcast_recent` / `meho_broadcast_announce` / `meho_broadcast_watch` (MCP-only — no CLI verbs yet, see note) | recording intent in the work ticket |
| a **guided multi-step procedure the agent drives itself** | MCP `meho_runbook_list_templates` / `meho_runbook_start` / `meho_runbook_next` / `meho_runbook_abort` | following a local runbook doc by hand |
| **recurring, durable, multi-step** work | MCP `meho_automation_list` — route-if-discovered (see note) | a local script or manual sequence |

## Notes on the rows that carry a nuance

- **Docs are capability-gated.** `search_docs` / `ask_docs` answer only over
  collections the tenant has enabled — check `list_doc_collections` first.
  If there is no collection for the product, **say so** rather than
  answering a vendor-version question from training data.
- **Broadcast has no CLI/REST parity yet**
  ([evoila/meho#3470](https://github.com/evoila/meho/issues/3470)); until it
  ships, a CLI-only session records its intent in the work ticket instead of
  announcing on the stream.
- **Automation is route-if-discovered, and it is not a runbook.**
  `meho_automation_list` reports whether the automation add-on is paired for
  this tenant (`required_addon_family="automation"`; absent otherwise) and
  what surface it advertises. Today an operator launches and observes a
  blueprint run through the automation console / REST — there is no CLI, and
  no agent-side launch or status tool — so the agent's job is to **identify
  the right blueprint and hand off, not to drive the run.** Runbooks
  (agent-driven, step-at-a-time, on the default surface) and automation
  blueprints (operator-launched, durable, a paired add-on family) are **not
  interchangeable**.

## Evidence quality stays visible

Every answer that rests on retrieved docs, memory, or knowledge **cites its
provenance**: the source, the observation time, the applicable product
version, and any coverage gaps ("not in the corpus"; "memory scoped to one
operator"; "knowledge last verified <date>"). A retrieved or remembered
fact **never substitutes for a required live observation** — when the
question is about a live target's current state, the corpus or memory tells
you what to *expect*, and the governed read tells you what is *true*. Label
**hypothesis vs verified** explicitly; an unmarked guess is worse than no
answer.

## Close the loop

A **verified** outcome goes back to the right store, through the backplane,
so the next session inherits it:

- an operator **preference** → scoped memory (`add_to_memory`, the narrowest
  scope that fits);
- a **reusable lesson** → tenant knowledge (`add_to_knowledge`);
- a **repeatable procedure** → propose a runbook template (agent-driven) or
  an automation blueprint (operator-launched).

Mark **hypothesis vs verified** on the way in, and keep provenance (what was
observed, when, against which target). Writing back through the backplane
rather than a local file is what makes the outcome audited and visible to
the team.

**A session proposes; it never approves.** Parking a destructive operation
for two-person review is a session action, but approving or rejecting that
parked operation — and granting an elevated role — are **human decisions
with no MCP path under any scope**: `meho_approvals_approve`,
`meho_approvals_reject`, and `meho_agents_grant_elevate` exist in the
console / CLI only, and an MCP `tools/call` for them answers with a
remediation naming that path. Never wait on your own approval.

## Constraints for agent (service-principal) sessions

- A client-credential service principal **has no agent session id** today
  ([evoila/meho#3446](https://github.com/evoila/meho/issues/3446)), so
  audit-replay-by-session does not reconstruct its work — query the audit
  log by `work_ref` instead.
- The **CLI has no broadcast verbs yet**
  ([evoila/meho#3470](https://github.com/evoila/meho/issues/3470)); the
  broadcast tools are MCP-only until parity ships.
- A service principal **can park a destructive op for two-person approval
  without holding a standing grant**
  ([evoila/meho#3478](https://github.com/evoila/meho/issues/3478)) — the
  propose-and-park path is open to an agent even where direct execution is
  not. The approval decision itself remains human (see *Close the loop*).

## Break-glass — preference is not permission

**Break-glass** is the word for any reach past the backplane: a local
wrapper, a raw vendor call, or a local `kb/` / memory file used because the
governed surface could not serve the need. It is never silent and never
casual — it is an **explicit, recorded** action, and for a genuine
capability gap a **filed** one.

"MEHO can't do it" must be **established, not assumed**: check
`search_operations` / `list_operation_groups` / `list_targets` against the
live deploy before declaring a gap. Then, before breaking glass:

1. **Notify** the operator in the conversation — what MEHO surface you
   tried, why it can't cover the action (the verbatim error where one
   exists), and the local invocation you are about to run instead.
2. **File / cite** the gap — a genuine MEHO capability gap is one upstream
   issue per *distinct* gap (repeat encounters cite, they don't re-file); a
   target simply not registered yet is a consumer-side registration ticket,
   not an upstream bug.
3. **Record** the deviation on the work ticket, and **fall back** for that
   one action. The next action starts again at the top of the routing rule.

A capability gap the change depends on must be linked from the change (the
signal or the registration ticket travels with the PR) — preference states
where you *should* route; it is not permission to route past the backplane
unrecorded.

## What stays local

- **Repo-discipline rules** — PR cadence, ticket + PR workflow — apply to
  repo work, not infra ops.
- **Per-machine credentials** — Vault is canonical for shared secrets; the
  operator's MEHO token lives in the local keyring /
  `~/.config/meho/credentials.json`. The broadcast feed never carries
  credentials.
- **Repo-internal generators and sidecar conventions.**

## Versioning

This plugin's version rides MEHO releases (see `.claude-plugin/plugin.json`).
After a MEHO upgrade, re-install the plugin (`/plugin install meho@meho`) to
pick up refreshed routing rules — this replaces the copy-and-merge template
refresh for Claude Code consumers.
