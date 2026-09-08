---
name: memory
description: >
  Prefer MEHO memory for operator preferences and durable notes in a
  MEHO-wired repo. Use when recording a behavioural preference, an
  operator note, or team-shared knowledge that should follow the operator
  or tenant across machines — reach for `add_to_memory` / `meho remember`
  / `meho memory …` instead of writing to a local memory file.
---

<!--
GENERATED FILE — DO NOT EDIT.
Rendered from docs/examples/consumer-onboarding/contract/meho-first-routing.md
by scripts/ci/gen_consumer_routing.py. Edit the contract source and re-run
the generator; backend/tests/test_consumer_routing_render.py fails CI on drift.
-->

# Memory — prefer MEHO

MEHO memory carries operator preferences and durable notes across machines and scopes them correctly. Prefer it over per-laptop local memory files. See `meho:prefer-meho` for the full route-by-evidence-need table.

## Memory — recording preferences and notes

MEHO memory carries operator preferences and durable notes across machines
and scopes them correctly. Prefer it over per-laptop local memory files for
anything that should persist beyond one checkout. Pick the narrowest scope
that fits:

- `add_to_memory` (MCP) / `meho remember "…" --scope user` (CLI) — a
  behavioural preference that follows the operator across every tenant.
- `meho remember "…" --scope user-tenant` (the default) — the operator's
  notes scoped to one tenant.
- `meho remember "…" --scope tenant` — team-shared knowledge visible to the
  whole tenant (requires the `tenant_admin` role).

## Memory — recalling and managing

- `search_memory` (MCP) / `meho memory list` (CLI) — find or enumerate
  entries in scope.
- `meho memory recall <scope>/<slug>` — read one entry.
- `meho memory forget <scope>/<slug>` — remove one entry.
- `meho memory promote <scope>/<slug>` — raise an entry to a broader scope.

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
