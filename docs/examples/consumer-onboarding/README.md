<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `docs/examples/consumer-onboarding/` — Layer-2 starter for consumer repos

Drop-in template + onboarding guide for consumer repos that operate
infrastructure **through** a MEHO backplane. Copy the files in this
directory into the root of your consumer repo so that any local agent
session (Claude Code, an MCP-aware editor, a CI bot) prefers MEHO
surfaces over per-machine fallbacks.

> **Claude Code users: install the plugin instead.** The MCP wiring and
> the Layer-2 routing rules below ship as a versioned Claude Code plugin —
> no copy-merge, no per-release re-pull:
>
> ```bash
> claude plugin marketplace add evoila/meho
> /plugin install meho@meho
> ```
>
> The plugin lives at
> [`clients/claude-code-plugin/`](../../../clients/claude-code-plugin/) and
> carries the same routing discipline as skills (`meho:prefer-meho`,
> `meho:knowledge`, `meho:memory`, `meho:operations`, `meho:broadcast`)
> plus the `mcp-remote` MCP config. This directory's template remains the
> authoritative source for **non-plugin clients** (Cline, Continue, CI
> bots) that read a `CLAUDE.md`.

## What's here

| File | Purpose |
|---|---|
| `README.md` | This file. Directory pointer — explains the Layer 1 vs Layer 2 framing and when to add files here versus `docs/cross-repo/`. |
| [`CLAUDE.md`](./CLAUDE.md) | The template itself. ~180 lines of routing rules a local Claude session reads on session start, telling it to prefer `meho` CLI verbs over local scripts. Copy into your consumer repo's root (or merge with an existing `CLAUDE.md`). |
| [`ONBOARDING.md`](./ONBOARDING.md) | How to install the template, how to verify a local session is routing through MEHO, how to add tenant-specific overrides, and how to refresh the template when MEHO ships a new minor version. |

## The two-layer surface

MEHO ships **two layers** of operating instructions that meet the
local Claude session from different angles:

* **Layer 1 — server-side tenant conventions.** Database-backed
  rules an admin curates per tenant (`meho conventions list/show/
  edit/history`). Auto-loaded into the MCP `initialize` response's
  `instructions` field for any agent connecting *through* MEHO.
  Bound to the **tenant**; binds every session no matter where it
  runs. Shipped by [G7.1-T1..T5](https://github.com/evoila/meho/issues/229)
  (T4 [#316](https://github.com/evoila/meho/issues/316) shipped the
  assembler that fills the `instructions` field, so a session
  connecting via MCP receives the tenant-conventions preamble at
  session start).
* **Layer 2 — this template.** The in-repo `CLAUDE.md` the operator's
  local Claude session reads when it opens a cloned consumer repo.
  Bound to the **repo on the operator's machine**; teaches the
  session "in this repo, prefer MEHO features over local fallbacks."
  Shipped by this directory.

Layer 1 handles agents connecting *through* MEHO from anywhere;
Layer 2 handles the operator's *local* Claude Code session reading
their cloned repo. Both layers ship in v0.2; the layering is locked
by [decision #5](../../decisions/locked-decisions.md) in the v0.2
planning notes.

## When to add files here

`docs/examples/consumer-onboarding/` is for **consumer-side starter
content** — drop-in files that the operator copies into a repo they
maintain so their local tooling learns about MEHO. This is distinct
from [`docs/cross-repo/`](../../cross-repo/), which holds the
contracts between `evoila/meho` and specific sibling repositories
(e.g. the audience-mapper recipe the operator runs once against
their Keycloak realm).

If a file is something an operator **copies** into a consumer repo,
it belongs here. If it's a procedure they **run once** against a
deployed system (a realm, a Vault mount, a cluster), it belongs in
`docs/cross-repo/`.
