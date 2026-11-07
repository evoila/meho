---
name: memory
description: >
  Use when deciding where a personal preference, an operator note, or a
  lesson goes in a MEHO-wired repo. A personal preference or note stays
  in the engineer's laptop memory. A lesson the team should reuse goes
  into MEHO's knowledge store with `add_to_knowledge`.
---

<!--
GENERATED FILE — DO NOT EDIT.
Rendered from docs/examples/consumer-onboarding/contract/meho-first-routing.md
by scripts/ci/gen_consumer_routing.py. Edit the contract source and re-run
the generator; backend/tests/test_consumer_routing_render.py fails CI on drift.
-->

# Memory — preferences and lessons

A personal preference or note stays in the engineer's laptop memory. A lesson the team should reuse goes into MEHO's knowledge store. See `meho:prefer-meho` for the full route-by-evidence-need table.

## Memory — personal preferences and notes

A personal preference or note stays in the engineer's laptop memory. Do not
write it to MEHO. A lesson the team should reuse does not belong in laptop
memory: add it to MEHO's knowledge store with `add_to_knowledge`. When a
personal note turns out to be a team fact, move it there.

## Memory — recalling and managing

- `search_memory` (MCP) / `meho memory list` (CLI) — find or enumerate
  entries in scope.
- `meho memory recall <scope>/<slug>` — read one entry.
- `meho memory forget <scope>/<slug>` — remove one entry.
- `meho memory promote <scope>/<slug>` — raise an entry to a broader scope.

## Close the loop

A **verified** outcome goes back to the right store, through the backplane,
so the next session inherits it:

- a **reusable lesson** → MEHO's knowledge store (`add_to_knowledge`), never
  a file in a repo's knowledge folder;
- a **personal preference or note** → stays in the engineer's laptop memory.
  When it turns out to be a team fact, move it to MEHO's knowledge store
  (`add_to_knowledge`);
- a **repeatable procedure** → propose a runbook template (agent-driven) or
  an automation blueprint (operator-launched).

Mark **hypothesis vs verified** on the way in, and keep provenance (what was
observed, when, against which target). Writing a lesson back through the
backplane rather than a local file is what makes it audited and visible to
the team.

**A session proposes; it never approves.** Parking a destructive operation
for two-person review is a session action, but approving or rejecting that
parked operation — and granting an elevated role — are **human decisions
with no MCP path under any scope**: `meho_approvals_approve`,
`meho_approvals_reject`, and `meho_agents_grant_elevate` exist in the
console / CLI only, and an MCP `tools/call` for them answers with a
remediation naming that path. Never wait on your own approval.
